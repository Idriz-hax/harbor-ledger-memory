"""Temporary-fixture tests for defensive watcher coalescing."""

from pathlib import Path
from threading import Event, Thread

from harbor_ledger_memory.config import FolderRule, Settings
from harbor_ledger_memory.services.scan import ScanResult
from harbor_ledger_memory.services.live_traversal import LiveTraversalPublisher
from harbor_ledger_memory.vault.boundary import VaultBoundary
from harbor_ledger_memory.watcher import VaultWatchService


class _FakeScanService:
    def __init__(self) -> None:
        self.calls = 0
        self.publisher = None

    def set_live_traversal(self, publisher: object) -> None:
        self.publisher = publisher

    def full_scan(self) -> ScanResult:
        self.calls += 1
        return ScanResult(0, 0, ())


def test_watcher_coalesces_atomic_save_and_ignores_unadmitted_paths(
    tmp_path: Path,
) -> None:
    ai = tmp_path / "AI"
    ai.mkdir()
    note = ai / "note.md"
    note.write_text("# Note\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    fake = _FakeScanService()
    watcher = VaultWatchService(
        VaultBoundary(Settings(HLM_VAULT_PATH=tmp_path)), fake, debounce_seconds=1
    )

    watcher.handle_event(note)
    watcher.handle_event(note)
    watcher.handle_event(outside)
    watcher.handle_event(ai / ".note.md.swp")
    watcher.flush()

    assert watcher.scan_calls == 1


def test_watcher_composes_publisher_into_scan_service(tmp_path: Path) -> None:
    publisher = LiveTraversalPublisher()
    fake = _FakeScanService()
    VaultWatchService(
        VaultBoundary(Settings(HLM_VAULT_PATH=tmp_path)),
        fake,
        live_traversal=publisher,
    )
    assert fake.publisher is publisher


def test_watcher_ignores_denied_subtrees(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed.md"
    denied = tmp_path / "Private" / "secret.md"
    denied.parent.mkdir()
    allowed.write_text("allowed", encoding="utf-8")
    denied.write_text("denied", encoding="utf-8")
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        folder_rules=(FolderRule(path="Private", access="deny"),),
    )
    fake = _FakeScanService()
    watcher = VaultWatchService(VaultBoundary(settings), fake, debounce_seconds=1)

    watcher.handle_event(denied)
    watcher.handle_event(allowed)
    watcher.flush()

    assert watcher.scan_calls == 1
    assert fake.calls == 1


def test_watcher_retries_one_transient_scan_failure(tmp_path: Path) -> None:
    ai = tmp_path / "AI"
    ai.mkdir()
    note = ai / "note.md"
    note.write_text("# Note\n", encoding="utf-8")

    class FlakyScan(_FakeScanService):
        def full_scan(self) -> ScanResult:
            self.calls += 1
            if self.calls == 1:
                raise ValueError("partial write")
            return ScanResult(1, 0, ())

    fake = FlakyScan()
    watcher = VaultWatchService(
        VaultBoundary(Settings(HLM_VAULT_PATH=tmp_path)), fake, debounce_seconds=1
    )
    watcher.handle_event(note)
    assert watcher.flush() is None
    assert watcher.diagnostics[0].retrying is True
    assert watcher.flush() is not None
    assert watcher.scan_calls == 2


def test_watcher_stop_waits_for_active_scan(tmp_path: Path) -> None:
    ai = tmp_path / "AI"
    ai.mkdir()
    note = ai / "note.md"
    note.write_text("# Note\n", encoding="utf-8")

    class BlockingScan(_FakeScanService):
        def __init__(self) -> None:
            super().__init__()
            self.scan_started = Event()
            self.scan_released = Event()
            self.scan_active = Event()

        def full_scan(self) -> ScanResult:
            self.calls += 1
            self.scan_active.set()
            self.scan_started.set()
            try:
                self.scan_released.wait()
            finally:
                self.scan_active.clear()
            return ScanResult(1, 0, ())

    class FakeObserver:
        def schedule(self, handler: object, path: str, recursive: bool) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def join(self) -> None:
            pass

        def is_alive(self) -> bool:
            return True

    fake = BlockingScan()
    watcher = VaultWatchService(
        VaultBoundary(Settings(HLM_VAULT_PATH=tmp_path)),
        fake,
        debounce_seconds=0.05,
        observer_factory=FakeObserver,
    )
    watcher.handle_event(note)
    watcher.start()
    assert fake.scan_started.wait(timeout=2)

    stop_finished = Event()

    def stop_watcher() -> None:
        watcher.stop()
        stop_finished.set()

    stop_thread = Thread(target=stop_watcher)
    stop_thread.start()
    try:
        former_timeout = max(1.0, watcher.debounce_seconds * 4)
        assert not stop_finished.wait(timeout=former_timeout + 0.1)
        assert fake.scan_active.is_set()
    finally:
        fake.scan_released.set()
        stop_thread.join(timeout=2)

    assert not stop_thread.is_alive()
    assert stop_finished.is_set()
    assert not fake.scan_active.is_set()


def test_watcher_restarts_observer_after_it_stops_being_alive(tmp_path: Path) -> None:
    class FakeObserver:
        def __init__(self) -> None:
            self.alive = False
            observers.append(self)
            if len(observers) == 2:
                replacement_started.set()

        def schedule(self, handler: object, path: str, recursive: bool) -> None:
            pass

        def start(self) -> None:
            self.alive = True

        def stop(self) -> None:
            self.alive = False

        def join(self) -> None:
            pass

        def is_alive(self) -> bool:
            return self.alive

    observers: list[FakeObserver] = []
    replacement_started = Event()
    watcher = VaultWatchService(
        VaultBoundary(Settings(HLM_VAULT_PATH=tmp_path)),
        _FakeScanService(),
        debounce_seconds=0.01,
        observer_factory=FakeObserver,
    )
    watcher.start()
    try:
        observers[0].alive = False
        assert replacement_started.wait(timeout=0.2)
    finally:
        watcher.stop()
