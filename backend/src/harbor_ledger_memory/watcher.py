"""Defensive, debounced read-only watching of the admitted vault projection."""

from __future__ import annotations

import inspect
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from harbor_ledger_memory.services.live_traversal import NullLiveTraversalPublisher
from harbor_ledger_memory.services.scan import ScanResult, ScanService
from harbor_ledger_memory.vault.boundary import VaultBoundary, VaultPathError


@dataclass(frozen=True)
class WatchDiagnostic:
    """A local watcher diagnostic; it is never written to the vault."""

    path: str
    message: str
    retrying: bool


class _EventHandler(FileSystemEventHandler):
    def __init__(self, service: VaultWatchService) -> None:
        super().__init__()
        self._service = service

    def on_created(self, event: FileSystemEvent) -> None:
        self._service.handle_event(Path(str(event.src_path)))

    def on_modified(self, event: FileSystemEvent) -> None:
        self._service.handle_event(Path(str(event.src_path)))

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._service.handle_event(Path(str(event.src_path)))

    def on_moved(self, event: Any) -> None:
        self._service.handle_event(Path(str(event.src_path)))
        self._service.handle_event(Path(str(event.dest_path)))


class VaultWatchService:
    """Coalesce filesystem events and ask the full scanner to recover state.

    The watcher intentionally does not implement a second incremental parser.
    A successful flush rescans the bounded projection, while a transient scan
    failure is recorded and retried once on a later flush.
    """

    def __init__(
        self,
        boundary: VaultBoundary,
        scan_service: ScanService,
        *,
        debounce_seconds: float = 0.25,
        observer_factory: Callable[[], Any] = Observer,
        live_traversal: object | None = None,
    ) -> None:
        if debounce_seconds <= 0:
            raise ValueError("debounce_seconds must be positive")
        self.boundary = boundary
        self.scan_service = scan_service
        self.debounce_seconds = debounce_seconds
        self._observer_factory = observer_factory
        self._live_traversal = live_traversal or NullLiveTraversalPublisher()
        set_live_traversal = getattr(scan_service, "set_live_traversal", None)
        if callable(set_live_traversal):
            set_live_traversal(self._live_traversal)
        self._pending: dict[str, float] = {}
        self._retry_counts: dict[str, int] = {}
        self._diagnostics: list[WatchDiagnostic] = []
        self._lock = threading.RLock()
        self._observer: Any = None
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()
        self.scan_calls = 0

    @property
    def diagnostics(self) -> tuple[WatchDiagnostic, ...]:
        """Return watcher diagnostics accumulated since construction."""
        with self._lock:
            return tuple(self._diagnostics)

    def start(self) -> None:
        """Start observing the canonical index root, if not already running."""
        with self._lock:
            if self._observer is not None:
                return
            if not self.boundary.index_root.is_dir():
                raise VaultPathError("index root is not an existing directory")
            observer = self._new_observer()
            self._observer = observer
            self._stopping.clear()
            self._worker = threading.Thread(
                target=self._flush_loop,
                name="hlm-vault-watch",
                daemon=True,
            )
            self._worker.start()

    def stop(self) -> None:
        """Stop observation and flush no additional events after shutdown."""
        with self._lock:
            self._stopping.set()
            observer = self._observer
            worker = self._worker
        if observer is not None:
            observer.stop()
            observer.join()
        if worker is not None and worker is not threading.current_thread():
            worker.join()
        with self._lock:
            if self._observer is observer:
                self._observer = None
            if self._worker is worker:
                self._worker = None

    def handle_event(self, path: Path | str) -> None:
        """Record one event only when its normalized identity is admitted."""
        candidate = Path(path)
        if _is_temporary(candidate):
            return
        if (
            candidate.is_symlink()
            or (candidate.exists() and candidate.is_dir())
            or candidate.suffix.lower() != ".md"
        ):
            return
        try:
            identity = self.boundary.vault_relative_path(candidate).as_posix()
        except (OSError, RuntimeError, ValueError, VaultPathError):
            return
        with self._lock:
            # A repeated event extends the quiet period but remains one scan.
            self._pending[identity] = time.monotonic()

    def flush(self) -> ScanResult | None:
        """Flush pending identities immediately, coalescing them into one scan."""
        with self._lock:
            pending = tuple(sorted(self._pending))
            if not pending:
                return None
            self._pending.clear()
            recovering = any(self._retry_counts.get(path, 0) > 0 for path in pending)

        self.scan_calls += 1
        try:
            if "trace_id" in inspect.signature(self.scan_service.full_scan).parameters:
                result = self.scan_service.full_scan(trace_id=str(uuid4()))
            else:
                # Keep the watcher compatible with small test/dry-run scan
                # implementations that predate the optional trace argument.
                result = self.scan_service.full_scan()
        except Exception as exc:
            with self._lock:
                for path in pending:
                    attempts = self._retry_counts.get(path, 0)
                    if attempts < 1:
                        self._retry_counts[path] = attempts + 1
                        self._pending[path] = time.monotonic()
                    self._diagnostics.append(
                        WatchDiagnostic(
                            path=path,
                            message=f"scan failed: {exc}",
                            retrying=attempts < 1,
                        )
                    )
            return None

        with self._lock:
            transient_diagnostics = tuple(
                diagnostic
                for diagnostic in result.diagnostics
                if diagnostic.code == "file.read"
                or diagnostic.code.startswith("frontmatter.")
            )
            transient_paths = {diagnostic.path for diagnostic in transient_diagnostics}
            for diagnostic in transient_diagnostics:
                attempts = self._retry_counts.get(diagnostic.path, 0)
                self._diagnostics.append(
                    WatchDiagnostic(
                        path=diagnostic.path,
                        message=f"{diagnostic.code}: {diagnostic.message}",
                        retrying=attempts < 1,
                    )
                )
                if attempts < 1:
                    self._retry_counts[diagnostic.path] = attempts + 1
                    self._pending[diagnostic.path] = time.monotonic()
                else:
                    self._retry_counts.pop(diagnostic.path, None)
            for path in pending:
                if path not in transient_paths:
                    self._retry_counts.pop(path, None)
            if recovering:
                self._diagnostics.append(
                    WatchDiagnostic(
                        path=pending[0],
                        message="scan recovered after a transient file change",
                        retrying=False,
                    )
                )
        return result

    def _flush_loop(self) -> None:
        while not self._stopping.wait(self.debounce_seconds):
            self._restart_observer_if_dead()
            with self._lock:
                has_pending = bool(self._pending)
            if has_pending:
                self.flush()

    def _new_observer(self) -> Any:
        observer = self._observer_factory()
        observer.schedule(
            _EventHandler(self), str(self.boundary.index_root), recursive=True
        )
        observer.start()
        return observer

    def _restart_observer_if_dead(self) -> None:
        with self._lock:
            observer = self._observer
            if observer is None or self._stopping.is_set() or observer.is_alive():
                return
            self._observer = None

        observer.stop()
        observer.join()

        with self._lock:
            if self._stopping.is_set() or self._observer is not None:
                return
            self._observer = self._new_observer()


def _is_temporary(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith(".#")
        or name.startswith(".~")
        or name.endswith(("~", ".swp", ".swo", ".swx", ".tmp", ".temp"))
        or name.startswith(".")
    )


__all__ = ["VaultWatchService", "WatchDiagnostic"]
