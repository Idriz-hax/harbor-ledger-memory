"""Canonical access boundary for the configured vault projection."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import unquote

from harbor_ledger_memory.config import FolderAccess, Settings

if TYPE_CHECKING:
    from collections.abc import Callable as _Callable


class VaultPathError(ValueError):
    """Raised when a path cannot be admitted to the configured index root."""


class DirectoryCreationError(VaultPathError):
    """A directory walk failed after creating one or more components."""

    def __init__(self, message: str, created_paths: tuple[PurePosixPath, ...]) -> None:
        super().__init__(message)
        self.created_paths = created_paths


def _no_follow_flag() -> int:
    """Return the no-follow flag, refusing insecure fallback behavior."""
    try:
        return os.O_NOFOLLOW
    except AttributeError as exc:
        raise VaultPathError("secure no-follow directory traversal is unavailable") from exc


@dataclass(frozen=True)
class FileVersion:
    """Immutable fingerprint of a file on disk for version-checked replaces."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True)
class AdmittedFileSnapshot:
    """Immutable bytes and metadata captured for one admitted vault identity."""

    path: PurePosixPath
    content: bytes
    file_size: int
    file_mtime_ns: int
    version: FileVersion | None = None

    @property
    def sha256(self) -> str:
        """Content sha256 of this snapshot.

        Convenience for callers (e.g. ``validate_current`` callbacks) that
        need the content hash of a coherent snapshot: prefers the hash
        captured in the :attr:`version` fingerprint and falls back to
        hashing :attr:`content` when no version was captured.
        """
        if self.version is not None:
            return self.version.sha256
        return hashlib.sha256(self.content).hexdigest()


@dataclass
class WriteResult:
    """Explicit outcome of an atomic write / removal operation.

    Attributes:
        version: FileVersion of the file on disk after the operation.  For a
            successful write this is the version of the published content;
            for a successful removal it is the version of the removed file.
            For a reconciliation-required result it is the version of the
            snapshotted file that was NOT mutated.
        published: True if the link/rename/unlink reached the final path,
            meaning the operation is visible under the target identity.
            A failure before publication raises instead of returning.
        durable: True if the complete durability chain finished: the content
            fsync before publication (writes only) AND the post-publication
            parent directory fsync.  If the post-publication parent fsync
            fails the result is ``published=True, durable=False`` -- the
            operation is visible but its durability is uncertain.  A
            pre-publication content fsync failure raises (nothing published).
        reconciliation_required: True if the operation deliberately did NOT
            mutate the target because, immediately before the mutating
            syscall (rename/unlink), the boundary could no longer prove that
            the directory entry referred to the snapshotted file (the entry
            was swapped or removed by an external actor).  Nothing was
            written or deleted under the target identity; the caller must
            re-read the current state and reconcile.  This is a returned
            outcome, not an exception, because the boundary observed a real
            external change it could not safely act on.
    """

    version: FileVersion | None
    published: bool = False
    durable: bool = False
    reconciliation_required: bool = False


@dataclass(frozen=True)
class DirectoryWriteResult:
    """Outcome of securely creating a directory identity."""

    created_paths: tuple[PurePosixPath, ...]
    published: bool
    durable: bool


class VaultBoundary:
    """Provide the only filesystem path boundary used by the application.

    On construction the index root is opened with O_NOFOLLOW and the FD
    is held for the lifetime of the instance.  All vault operations
    duplicate this FD and traverse through directory file-descriptors --
    the root is never re-opened by pathname after construction.
    """

    def __init__(self, settings: Settings) -> None:
        self._vault_root = settings.vault_path.expanduser().resolve(strict=False)
        self._index_relative = self._decode_path(settings.index_root)
        self._validate_relative(self._index_relative, check_sibling=False)
        self._index_root = self._resolve_under_vault(self._index_relative)
        self._rules = tuple(settings.folder_rules)

        # ---- held index-root FD (O_NOFOLLOW) ----
        no_follow = _no_follow_flag()
        directory = getattr(os, "O_DIRECTORY", 0)
        self._index_root_fd = os.open(
            self._index_root, os.O_RDONLY | directory | no_follow
        )
        self._root_dev = os.fstat(self._index_root_fd).st_dev
        self._root_ino = os.fstat(self._index_root_fd).st_ino

    def __del__(self) -> None:
        """Close the held index-root FD if still open."""
        if hasattr(self, "_index_root_fd") and self._index_root_fd >= 0:
            try:
                os.close(self._index_root_fd)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Properties (unchanged)
    # ------------------------------------------------------------------

    @property
    def index_root(self) -> Path:
        """Return the canonical configured index root."""
        return self._index_root

    @property
    def index_relative(self) -> PurePosixPath:
        """Return the configured vault-relative index root identity."""
        return self._index_relative

    @property
    def effective_read_scope(self) -> str:
        """Return a concise description suitable for status surfaces."""
        root = self._index_relative.as_posix()
        denied = sorted(
            rule.path.as_posix()
            for rule in self._rules
            if rule.access is FolderAccess.DENY
        )
        return root if not denied else f"{root} (deny: {', '.join(denied)})"

    # Compatibility aliases for callers from the fixed-AI implementation.
    @property
    def ai_root(self) -> Path:
        return self._index_root

    @property
    def ai_relative(self) -> PurePosixPath:
        return self._index_relative

    # ------------------------------------------------------------------
    # Access checks
    # ------------------------------------------------------------------

    def access_for(self, relative: PurePosixPath | str) -> FolderAccess:
        """Return the most-specific configured access mode for a vault identity."""
        identity = self._validated_identity(relative)
        matching = [
            rule for rule in self._rules if _is_same_or_descendant(identity, rule.path)
        ]
        if not matching:
            return FolderAccess.READ
        return max(matching, key=lambda rule: len(rule.path.parts)).access

    def is_readable(self, relative: PurePosixPath | str) -> bool:
        """Return whether a vault-relative identity is admitted for reading."""
        try:
            return self.access_for(relative).is_readable
        except VaultPathError:
            return False

    def is_admitted(self, relative: PurePosixPath | str) -> bool:
        """Alias used by catalog/query adapters when filtering stale rows."""
        return self.is_readable(relative)

    # ------------------------------------------------------------------
    # Safe read API (held dir-fd, no-follow)
    # ------------------------------------------------------------------

    def read_file(self, identity: PurePosixPath) -> AdmittedFileSnapshot:
        """Return an immutable snapshot through no-follow dir-FD traversal.

        Raises :class:`VaultPathError` when the path is outside the index
        root, escapes via traversal, names a symlinked target/parent, or
        the index root has been replaced.
        """
        return self._read_snapshot(identity)

    # ------------------------------------------------------------------
    # atomic_create / atomic_replace / remove_if_version
    # ------------------------------------------------------------------

    def atomic_create(
        self,
        identity: PurePosixPath,
        content: bytes,
    ) -> WriteResult:
        """Atomically create *identity* with *content* via held index-root FD.

        Uses dir-fd traversal (never re-opens root by pathname), a temp file
        in the parent directory, fsync-before-rename, and a final parent
        directory fsync for durability.

        If the target already exists (regular file, symlink, or directory),
        raises :class:`VaultPathError` and leaves the vault untouched.
        Temp files are always cleaned up on failure.

        Returns :class:`WriteResult` with the new file's *version*,
        *published* (link reached the target), and *durable* (content fsync
        plus post-publication parent fsync both succeeded).

        Raises :class:`VaultPathError` for path traversal, symlinked
        parents/targets, replaced index root, existing target, or any
        failure before the link (temp file is cleaned up in all of those).
        """
        self._check_index_root_integrity()
        relative = self._relative_within_index(identity)
        parent_fd = self._open_parent_dir(relative)
        filename = relative.parts[-1]
        try:
            return self._create_in_dir(parent_fd, filename, identity, content)
        finally:
            os.close(parent_fd)

    def atomic_mkdir(self, identity: PurePosixPath) -> DirectoryWriteResult:
        """Create an admitted directory path through held, no-follow FDs.

        Existing directories are accepted, while every missing component is
        created beneath the held index-root descriptor.  The parent descriptor
        is fsynced after each successful mkdir; a failed fsync is reported as
        uncertain durability rather than pretending the directory was absent.
        """
        self._check_index_root_integrity()
        relative = self._relative_within_index(identity)
        fd, created, durable = self._walk_dir_fd_with_created(relative)
        try:
            return DirectoryWriteResult(
                created_paths=tuple(created), published=True, durable=durable
            )
        finally:
            os.close(fd)

    def missing_parent_paths(self, identity: PurePosixPath) -> tuple[PurePosixPath, ...]:
        """Return missing parent directories without modifying the vault."""
        self._check_index_root_integrity()
        relative = self._relative_within_index(identity)
        parts = relative.parts[:-1]
        if not parts:
            return ()

        no_follow = _no_follow_flag()
        directory = getattr(os, "O_DIRECTORY", 0)
        current = os.dup(self._index_root_fd)
        try:
            for index, part in enumerate(parts):
                try:
                    next_fd = os.open(
                        part, os.O_RDONLY | directory | no_follow, dir_fd=current
                    )
                except FileNotFoundError:
                    return tuple(
                        self._index_relative / PurePosixPath(*parts[: position + 1])
                        for position in range(index, len(parts))
                    )
                except OSError as exc:
                    raise VaultPathError(
                        f"symlinked directory component in {identity.as_posix()}"
                    ) from exc
                os.close(current)
                current = next_fd
            return ()
        finally:
            os.close(current)

    def atomic_replace(
        self,
        identity: PurePosixPath,
        content: bytes,
        *,
        expected: FileVersion,
        validate_current: _Callable[[AdmittedFileSnapshot], bool] | None = None,
    ) -> WriteResult:
        """Atomically replace *identity* with *content* if version matches.

        Opens the target via held index-root FD, checks that it is a regular
        file (not missing, not a symlink, not a directory), and builds a
        coherent current snapshot: the full FileVersion (all fields) is
        computed from a single O_NOFOLLOW FD whose fstat signature is
        verified before and after the content read.

        The complete version is compared against *expected* on that
        snapshot, and if *validate_current* is provided it is invoked with
        the same coherent current file snapshot (an
        :class:`AdmittedFileSnapshot` carrying both the content bytes and
        the FileVersion) -- both before any write begins.  Returning
        ``False`` aborts the replace (original untouched).

        A mismatch raises :class:`VaultPathError` and preserves the original
        file.

        On success, writes to a temp file (write-all loop + fsync), then
        IMMEDIATELY before publication re-checks the destination entry
        (fresh coherent snapshot, full FileVersion comparison) and renames
        atomically only when the entry still refers to exactly the
        snapshotted content.  If the re-check fails -- the entry was
        swapped (e.g. replaced by a symlink or a different file) or removed
        by an external actor after the initial snapshot -- nothing is
        renamed and the temp file is cleaned up; the returned
        :class:`WriteResult` has ``reconciliation_required=True`` with
        ``published=False`` and the version of the snapshotted file.  A
        post-publication parent directory fsync then finalizes durability.

        Unavoidable final POSIX race: the re-check and the rename are two
        syscalls that cannot be atomic together (POSIX has no conditional
        rename).  An external swap in that final window is indistinguishable
        from one before the re-check; the re-check shrinks the window to the
        minimum but cannot close it, so callers must treat a rename that
        lands over a just-swapped entry as a residual hazard of the same
        class as any concurrent writer.

        Temp files are always cleaned up on any failure before publication.

        Returns :class:`WriteResult` with the new file's *version*,
        *published* (rename reached the target), *durable* (content
        fsync plus post-publication parent fsync both succeeded), and
        *reconciliation_required* (pre-publication re-check failed; nothing
        was renamed).

        Raises :class:`VaultPathError` for path traversal, symlinked
        parents/targets, replaced index root, missing/non-regular target,
        or version mismatch.
        """
        self._check_index_root_integrity()
        relative = self._relative_within_index(identity)
        parent_fd = self._open_parent_dir(relative)
        filename = relative.parts[-1]
        try:
            return self._replace_in_dir(
                parent_fd, filename, identity, content, expected, validate_current
            )
        finally:
            os.close(parent_fd)

    def remove_if_version(
        self,
        identity: PurePosixPath,
        *,
        expected: FileVersion,
    ) -> WriteResult:
        """Atomically remove *identity* if *expected* version matches.

        Opens the target via held index-root FD, verifies it is a regular
        no-follow file, and builds a coherent current snapshot (single FD,
        fstat verified before and after the content read).  Only when the
        complete snapshot version matches *expected* is removal attempted.

        Because ``unlink`` acts on the directory ENTRY, not the snapshotted
        inode, the entry is re-verified IMMEDIATELY before the unlink: a
        fresh coherent snapshot must match the original snapshot's full
        FileVersion, proving the name still refers to exactly the file that
        was version-checked.  If the entry was swapped (e.g. replaced by a
        symlink or a different file) or removed by an external actor after
        the version snapshot, the unlink is NOT performed -- deleting would
        destroy an entry this API never inspected -- and the returned
        :class:`WriteResult` has ``reconciliation_required=True`` with
        ``published=False`` and the version of the snapshotted file that
        was NOT removed.  The caller must re-read the current state and
        reconcile.

        Unavoidable final POSIX race: the re-verify and the unlink are two
        syscalls that cannot be atomic together (POSIX has no conditional
        unlink).  An external swap in that final window is indistinguishable
        from one before the re-verify; the re-verify shrinks the window to
        the minimum but cannot close it.

        On a successful unlink the parent directory is fsynced.

        Returns :class:`WriteResult` with *version* set to the removed
        file's snapshot version, *published* True once the unlink succeeded,
        and *durable* True only if the post-unlink parent fsync succeeded.
        A failed parent fsync reports uncertain durability
        (``published=True, durable=False``) -- it does NOT report a failed
        removal, and the API never claims a rollback succeeded.

        Raises :class:`VaultPathError` for path traversal, symlinked
        parents/targets, replaced index root, missing/non-regular target,
        version mismatch, or a failed unlink (which reports uncertainty
        rather than claiming the removal or any rollback happened).
        """
        self._check_index_root_integrity()
        relative = self._relative_within_index(identity)
        parent_fd = self._open_parent_dir(relative)
        filename = relative.parts[-1]
        try:
            return self._remove_in_dir(parent_fd, filename, identity, expected)
        finally:
            os.close(parent_fd)

    # ------------------------------------------------------------------
    # Backward-compatible API
    # ------------------------------------------------------------------

    def atomic_write(
        self,
        identity: PurePosixPath,
        content: bytes,
        *,
        expected_size: int | None = None,
        expected_mtime_ns: int | None = None,
    ) -> WriteResult:
        """Backward-compatible combined create/replace.

        Delegates to :meth:`atomic_create` when the target is absent.
        When it exists and version info is provided, delegates to
        :meth:`atomic_replace`.  When it exists and no version info is
        given, raises :class:`VaultPathError` (no blind overwrites).
        """
        self._check_index_root_integrity()
        relative = self._relative_within_index(identity)
        parent_fd = self._open_parent_dir(relative)
        filename = relative.parts[-1]
        try:
            try:
                os.stat(filename, dir_fd=parent_fd)
            except FileNotFoundError:
                return self._create_in_dir(parent_fd, filename, identity, content)

            if expected_size is None or expected_mtime_ns is None:
                raise VaultPathError(
                    f"existing target {identity.as_posix()} requires "
                    f"expected_size and expected_mtime_ns to overwrite "
                    f"(no blind overwrites)"
                )
            snapshot = self._read_snapshot(identity)
            if snapshot.version is None:
                raise VaultPathError(
                    f"could not determine version for {identity.as_posix()}"
                )
            expected = FileVersion(
                device=snapshot.version.device,
                inode=snapshot.version.inode,
                size=expected_size,
                mtime_ns=expected_mtime_ns,
                ctime_ns=snapshot.version.ctime_ns,
                sha256=snapshot.version.sha256,
            )
            return self._replace_in_dir(
                parent_fd, filename, identity, content, expected
            )
        finally:
            os.close(parent_fd)

    # ------------------------------------------------------------------
    # Internal: held-fd traversal helpers
    # ------------------------------------------------------------------

    def _check_index_root_integrity(self) -> None:
        """Reject a missing or replaced root using the held FD."""
        try:
            st = os.fstat(self._index_root_fd)
        except OSError as exc:
            raise VaultPathError(
                "index root is no longer accessible (closed or deleted)"
            ) from exc
        if not stat.S_ISDIR(st.st_mode):
            raise VaultPathError("index root is no longer a directory")
        if st.st_dev != self._root_dev or st.st_ino != self._root_ino:
            raise VaultPathError(
                "index root is no longer valid (replaced or symlinked)"
            )
        try:
            if self._index_root.is_symlink():
                raise VaultPathError("index root has been replaced by a symlink")
            resolved = self._index_root.resolve(strict=True)
            probe_st = os.stat(resolved)
            if probe_st.st_dev != self._root_dev or probe_st.st_ino != self._root_ino:
                raise VaultPathError(
                    "index root is no longer valid (replaced or symlinked)"
                )
        except VaultPathError:
            raise
        except OSError:
            raise VaultPathError("index root is no longer accessible")

    def _relative_within_index(self, identity: PurePosixPath) -> PurePosixPath:
        """Return the path relative to index root, or raise."""
        self._validate_relative(identity, check_sibling=False)
        try:
            return identity.relative_to(self._index_relative)
        except ValueError as exc:
            raise VaultPathError("path is outside the configured index root") from exc

    def _open_parent_dir(self, relative: PurePosixPath) -> int:
        """Open the parent directory of *relative* via dup-of-held fd."""
        parts = relative.parts
        if len(parts) < 2:
            return os.dup(self._index_root_fd)
        parent_parts = PurePosixPath(*parts[:-1])
        return self._walk_dir_fd(parent_parts)

    def _walk_dir_fd(self, relative: PurePosixPath) -> int:
        """Walk *relative* from the held index-root fd, no-follow.

        Creates missing intermediate directories with mode 0o755.
        Returns an open fd for the final directory.
        Raises :class:`VaultPathError` for symlinked components.
        """
        current, _created, durable = self._walk_dir_fd_with_created(relative)
        if not durable:
            os.close(current)
            raise VaultPathError(
                f"could not durably create directory path {relative.as_posix()}"
            )
        return current

    def _walk_dir_fd_with_created(
        self, relative: PurePosixPath
    ) -> tuple[int, list[PurePosixPath], bool]:
        """Walk a relative directory and report newly-created components."""
        no_follow = _no_follow_flag()
        directory = getattr(os, "O_DIRECTORY", 0)
        current = os.dup(self._index_root_fd)
        created: list[PurePosixPath] = []
        durable = True
        try:
            for index, part in enumerate(relative.parts):
                try:
                    next_fd = os.open(
                        part, os.O_RDONLY | directory | no_follow, dir_fd=current
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(part, 0o755, dir_fd=current)
                    except FileExistsError:
                        # Another actor won the creation race; the no-follow
                        # open below still validates what now occupies the name.
                        pass
                    else:
                        created.append(
                            self._index_relative
                            / PurePosixPath(*relative.parts[: index + 1])
                        )
                        try:
                            os.fsync(current)
                        except OSError:
                            durable = False
                    try:
                        next_fd = os.open(
                            part, os.O_RDONLY | directory | no_follow, dir_fd=current
                        )
                    except OSError as exc:
                        raise VaultPathError(
                            f"symlinked directory component in {relative.as_posix()}"
                        ) from exc
                except (NotADirectoryError, OSError) as exc:
                    raise VaultPathError(
                        f"symlinked directory component in {relative.as_posix()}"
                    ) from exc
                os.close(current)
                current = next_fd
            return current, created, durable
        except VaultPathError as exc:
            os.close(current)
            if created:
                raise DirectoryCreationError(
                    f"directory creation failed for {relative.as_posix()}: {exc}",
                    tuple(created),
                ) from exc
            raise
        except Exception as exc:
            os.close(current)
            if created:
                raise DirectoryCreationError(
                    f"directory creation failed for {relative.as_posix()}: {exc}",
                    tuple(created),
                ) from exc
            raise

    def _open_relative_file(self, relative: PurePosixPath) -> int:
        """Open an index-relative file without following root/component links."""
        if not relative.parts:
            raise VaultPathError("admitted path must name a file")
        no_follow = _no_follow_flag()
        directory = getattr(os, "O_DIRECTORY", 0)
        current = os.dup(self._index_root_fd)
        try:
            for index, part in enumerate(relative.parts):
                flags = os.O_RDONLY | no_follow
                if index < len(relative.parts) - 1:
                    flags |= directory
                try:
                    next_descriptor = os.open(part, flags, dir_fd=current)
                except OSError as exc:
                    if exc.errno in (
                        errno.ENOTDIR,
                        getattr(errno, "ELOOP", 62),
                    ):
                        raise VaultPathError(
                            f"symlinked directory component in {relative.as_posix()}"
                        ) from exc
                    raise
                os.close(current)
                current = next_descriptor
            return current
        except VaultPathError:
            os.close(current)
            raise
        except Exception:
            os.close(current)
            raise

    # ------------------------------------------------------------------
    # Internal: held-fd recursive directory enumeration
    # ------------------------------------------------------------------

    def _recursive_dir_entries(
        self, fd: int, path_prefix: PurePosixPath, *, include_denied: bool = False
    ) -> Generator[tuple[PurePosixPath, int], None, None]:
        """Recursively yield (relative_path, fd) for directories under *fd*.

        Yields only non-symlinked, readable directories.  Each yielded fd
        is owned by THIS generator: it is closed as soon as iteration moves
        past that entry, when the recursion is exhausted, or when the
        generator is closed early (``close()`` / abandonment), so no child
        directory FD leaks on any path.  Consumers must NOT close yielded
        fds or use them after the iteration advances.

        *path_prefix* is the vault-relative path corresponding to *fd*.
        """
        no_follow = _no_follow_flag()
        directory = getattr(os, "O_DIRECTORY", 0)

        for entry in os.listdir(fd):
            try:
                st = os.lstat(entry, dir_fd=fd)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if stat.S_ISDIR(st.st_mode):
                child_path = path_prefix / entry
                if not include_denied and not self._directory_is_readable_path(
                    child_path
                ):
                    continue
                try:
                    child_fd = os.open(
                        entry, os.O_RDONLY | directory | no_follow, dir_fd=fd
                    )
                except OSError:
                    continue
                try:
                    yield child_path, child_fd
                    yield from self._recursive_dir_entries(
                        child_fd, child_path, include_denied=include_denied
                    )
                finally:
                    os.close(child_fd)

    def _directory_is_readable_path(self, vault_relative: PurePosixPath) -> bool:
        """Check if a vault-relative directory is readable by folder rules."""
        try:
            return self.is_readable(vault_relative)
        except VaultPathError:
            return False

    def iter_directories(self) -> Iterator[PurePosixPath]:
        """Yield all non-symlinked directories under the configured index root.

        Permission settings must remain editable even after a folder is denied,
        so this intentionally includes denied directories while retaining the
        held-FD traversal and no-follow safety guarantees.
        """
        try:
            self._check_index_root_integrity()
        except VaultPathError:
            return

        yield self._index_relative
        entries = self._recursive_dir_entries(
            self._index_root_fd, self._index_relative, include_denied=True
        )
        try:
            for relative, _ in entries:
                yield relative
        finally:
            entries.close()

    # ------------------------------------------------------------------
    # Internal: create / replace / remove in parent directory
    # ------------------------------------------------------------------

    def _create_in_dir(
        self,
        parent_fd: int,
        filename: str,
        identity: PurePosixPath,
        content: bytes,
    ) -> WriteResult:
        """Create *filename* in *parent_fd* atomically (no-clobber)."""
        # Check whether target already exists (any type)
        try:
            os.lstat(filename, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass  # anything that exists is a conflict
        else:
            raise VaultPathError(
                f"target {identity.as_posix()} already exists; "
                f"use atomic_replace to overwrite"
            )

        return self._write_temp_and_rename(
            parent_fd, filename, identity, content, create_only=True
        )

    def _replace_in_dir(
        self,
        parent_fd: int,
        filename: str,
        identity: PurePosixPath,
        content: bytes,
        expected: FileVersion,
        validate_current: _Callable[[AdmittedFileSnapshot], bool] | None = None,
    ) -> WriteResult:
        """Replace *filename* in *parent_fd* if *expected* version matches.

        Builds a coherent snapshot of the current target (single O_NOFOLLOW
        FD, never reopened by pathname), performs the complete version
        comparison and the optional *validate_current* callback on that
        snapshot, and only then starts the write.

        The snapshot's FileVersion is also passed to the publication step,
        which re-checks the destination entry immediately before the
        rename and reports ``reconciliation_required`` (no rename) when the
        entry no longer refers to the snapshotted file.
        """
        snapshot = self._snapshot_target_in_dir(parent_fd, filename, identity)
        if snapshot.version != expected:
            raise VaultPathError(
                f"version conflict for {identity.as_posix()}: "
                f"expected {expected!r}, got {snapshot.version!r}"
            )

        # Optional secondary validation callback -- invoked on the same
        # coherent snapshot (content and version), strictly before any
        # write begins.
        if validate_current is not None:
            if not validate_current(snapshot):
                raise VaultPathError(
                    f"validate_current callback rejected replace for "
                    f"{identity.as_posix()}"
                )

        return self._write_temp_and_rename(
            parent_fd, filename, identity, content, recheck_version=snapshot.version
        )

    def _remove_in_dir(
        self,
        parent_fd: int,
        filename: str,
        identity: PurePosixPath,
        expected: FileVersion,
    ) -> WriteResult:
        """Remove *filename* in *parent_fd* if *expected* version matches.

        Only a version-matching regular no-follow target is removed.  The
        version is taken from a coherent single-FD snapshot; a failed
        unlink reports uncertainty (the file may still exist) rather than
        claiming a rollback succeeded, and a failed post-unlink parent
        fsync reports uncertain durability rather than a failed removal.

        Before the unlink, the directory entry is re-verified against the
        snapshot's full FileVersion: if an external actor swapped or removed
        the entry after the version snapshot, unlinking the NAME could
        destroy an entry this API never inspected, so no unlink happens and
        a reconciliation-required outcome is returned instead.
        """
        snapshot = self._snapshot_target_in_dir(parent_fd, filename, identity)
        if snapshot.version != expected:
            raise VaultPathError(
                f"version conflict for {identity.as_posix()}: "
                f"expected {expected!r}, got {snapshot.version!r}"
            )

        # Re-verify the entry immediately before the unlink: prove the name
        # still refers to exactly the snapshotted file.
        recheck = self._recheck_entry_version(parent_fd, filename, identity)
        if recheck != snapshot.version:
            return WriteResult(
                version=snapshot.version,
                published=False,
                durable=False,
                reconciliation_required=True,
            )

        # Unlink the directory entry (never following links).
        try:
            os.unlink(filename, dir_fd=parent_fd)
        except OSError as exc:
            raise VaultPathError(
                f"failed to unlink {identity.as_posix()}; removal state is "
                f"uncertain and the file may still exist: {exc}"
            ) from exc

        # Fsync parent for durability.  A failure here means the removal is
        # published but its durability is uncertain -- not that the removal
        # failed and was rolled back.
        durable = False
        try:
            os.fsync(parent_fd)
            durable = True
        except OSError:
            durable = False

        return WriteResult(version=snapshot.version, published=True, durable=durable)

    def _recheck_entry_version(
        self, parent_fd: int, filename: str, identity: PurePosixPath
    ) -> FileVersion | None:
        """Return a fresh coherent FileVersion of the destination entry.

        Returns ``None`` when proof is unavailable: the entry is missing,
        is a symlink/directory, or changed while it was being snapshotted.
        Callers treat ``None`` (or any mismatch with the version they
        previously verified) as reconciliation-required rather than
        mutating the entry.
        """
        try:
            snapshot = self._snapshot_target_in_dir(parent_fd, filename, identity)
        except VaultPathError:
            return None
        return snapshot.version

    def _snapshot_target_in_dir(
        self, parent_fd: int, filename: str, identity: PurePosixPath
    ) -> AdmittedFileSnapshot:
        """Open *filename* via *parent_fd* and return a coherent snapshot.

        Returns an :class:`AdmittedFileSnapshot` whose content and
        FileVersion come from the same observation.

        A single O_NOFOLLOW target FD is used for both fstat and the content
        read (the target is never reopened by pathname).  The returned
        fingerprint is built from the pre-read fstat plus the sha256 of the
        bytes actually read; the post-read fstat must agree on
        dev/ino/size/mtime (ctime excluded, since metadata-only operations
        such as unlinking the directory entry bump it without changing
        content), proving no content change happened between the two
        observations and that the fingerprint describes exactly the bytes
        that were hashed.
        """
        no_follow = _no_follow_flag()
        try:
            target_fd = os.open(filename, os.O_RDONLY | no_follow, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise VaultPathError(
                f"target {identity.as_posix()} not found on disk"
            ) from exc
        except IsADirectoryError as exc:
            raise VaultPathError(
                f"target {identity.as_posix()} is a directory, not a regular file"
            ) from exc
        except OSError as exc:
            if exc.errno == getattr(errno, "ELOOP", 62):
                raise VaultPathError(
                    f"target {identity.as_posix()} is a symlink, not a regular file"
                ) from exc
            raise

        try:
            try:
                before = os.fstat(target_fd)
            except OSError as exc:
                raise VaultPathError(
                    f"could not stat {identity.as_posix()}: {exc}"
                ) from exc

            if not stat.S_ISREG(before.st_mode):
                raise VaultPathError(
                    f"target {identity.as_posix()} is not a regular file"
                )

            try:
                current_data = self._read_all_from_fd(target_fd)
                after = os.fstat(target_fd)
            except OSError as exc:
                raise VaultPathError(
                    f"could not read current content of {identity.as_posix()}: {exc}"
                ) from exc
        finally:
            os.close(target_fd)

        # Coherence check: prove that no content change happened between
        # the pre-read and post-read fstat.  ctime is deliberately excluded
        # -- metadata-only operations (e.g. the directory entry being
        # unlinked/replaced while this FD is held) bump ctime without
        # changing the bytes we read, and the fingerprint below is taken
        # from the pre-read observation.
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise VaultPathError(
                f"file changed while it was being snapshotted: {identity.as_posix()}"
            )
        if len(current_data) != after.st_size:
            raise VaultPathError(
                f"file size changed while it was being snapshotted: "
                f"{identity.as_posix()}"
            )

        # Fingerprint from the pre-read observation plus the sha256 of the
        # bytes actually read; the checks above prove this describes exactly
        # those bytes.
        version = FileVersion(
            device=before.st_dev,
            inode=before.st_ino,
            size=before.st_size,
            mtime_ns=before.st_mtime_ns,
            ctime_ns=before.st_ctime_ns,
            sha256=hashlib.sha256(current_data).hexdigest(),
        )

        # Return content and version together so callers (version checks,
        # validate_current, pre-publication re-checks) always reason about
        # one coherent observation, never a version detached from its bytes.
        return AdmittedFileSnapshot(
            path=identity,
            content=current_data,
            file_size=after.st_size,
            file_mtime_ns=after.st_mtime_ns,
            version=version,
        )

    def _read_all_from_fd(self, fd: int) -> bytes:
        """Read all content from an already-open FD without reopening by pathname.

        Reads up to st_size bytes to guarantee completeness.
        The caller must close *fd* afterwards.
        """
        st = os.fstat(fd)
        size = st.st_size
        if size == 0:
            return b""
        data = bytearray()
        offset = 0
        while offset < size:
            chunk = os.read(fd, min(size - offset, 65536))
            if not chunk:
                break
            data.extend(chunk)
            offset += len(chunk)
        return bytes(data)

    def _write_temp_and_rename(
        self,
        parent_fd: int,
        filename: str,
        identity: PurePosixPath,
        content: bytes,
        *,
        create_only: bool = False,
        recheck_version: FileVersion | None = None,
    ) -> WriteResult:
        """Write *content* to a temp file, fsync, publish as *filename*, fsync parent.

        Publication is a no-clobber ``link()`` for create and an atomic
        ``rename()`` for replace.  The published file's version is captured
        from the temp file's FD *before* publication (link/rename do not
        change the inode), so the target is never reopened by pathname.

        When *recheck_version* is given (replace path), the destination
        entry is re-checked IMMEDIATELY before the rename: a fresh coherent
        snapshot must match *recheck_version* exactly.  On mismatch the
        rename is skipped, the temp file is cleaned up, and a
        reconciliation-required :class:`WriteResult` is returned (nothing
        published).  This closes the window in which an external actor
        could swap the entry (e.g. to a symlink or a different file) after
        the caller's version check and have the rename clobber the swap.
        The re-check and the rename remain two separate syscalls -- POSIX
        has no conditional rename -- so a swap in that final window is the
        documented unavoidable residual race.

        Returns :class:`WriteResult` with *published* True once the
        link/rename reached the target, and *durable* True only if the
        content fsync AND the post-publication parent fsync both succeeded.
        The temp file is always cleaned up on any failure before
        publication.
        """
        tmp_name, tmp_fd = self._open_temp_file(parent_fd, filename)
        try:
            # Write-all loop (handles partial writes)
            offset = 0
            while offset < len(content):
                written = os.write(tmp_fd, content[offset:])
                if written == 0:
                    raise OSError(0, "short write", identity.as_posix())
                offset += written

            # Fsync the temp file contents; failure means nothing is
            # published, so raise (temp is cleaned up by the except path).
            try:
                os.fsync(tmp_fd)
            except OSError as fsync_exc:
                raise VaultPathError(
                    f"content fsync failed for {identity.as_posix()}: {fsync_exc}"
                ) from fsync_exc

            # Capture the version of the inode we are about to publish.
            st = os.fstat(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = -1
            new_version = FileVersion(
                device=st.st_dev,
                inode=st.st_ino,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                ctime_ns=st.st_ctime_ns,
                sha256=hashlib.sha256(content).hexdigest(),
            )

            # Replace path: re-check the destination entry IMMEDIATELY
            # before publication.  If an external actor swapped or removed
            # the entry since the caller's version check, renaming would
            # clobber an entry this API never inspected -- so skip the
            # rename, clean up the temp file, and report
            # reconciliation-required instead of publishing.
            if not create_only and recheck_version is not None:
                recheck = self._recheck_entry_version(parent_fd, filename, identity)
                if recheck != recheck_version:
                    self._cleanup_temp(parent_fd, tmp_fd, tmp_name)
                    return WriteResult(
                        version=recheck_version,
                        published=False,
                        durable=False,
                        reconciliation_required=True,
                    )

            # Atomic publication: no-clobber link for create, rename for
            # replace.  After this point the operation is published and no
            # failure below may un-publish it.
            if create_only:
                try:
                    os.link(
                        tmp_name,
                        filename,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                except FileExistsError as exc:
                    raise VaultPathError(
                        f"target {identity.as_posix()} already exists "
                        f"(race condition during link)"
                    ) from exc
            else:
                os.rename(
                    tmp_name,
                    filename,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )

            # Best-effort removal of the extra hardlink name (create only).
            # A failure here cannot un-publish the target.
            if create_only:
                self._try_unlink_tmp(parent_fd, tmp_name)

            # Fsync the parent directory for durability (don't raise).
            durable = False
            try:
                os.fsync(parent_fd)
                durable = True
            except OSError:
                durable = False

            return WriteResult(version=new_version, published=True, durable=durable)
        except VaultPathError:
            self._cleanup_temp(parent_fd, tmp_fd, tmp_name)
            raise
        except Exception as exc:
            self._cleanup_temp(parent_fd, tmp_fd, tmp_name)
            raise VaultPathError(
                f"write failed for {identity.as_posix()}: {exc}"
            ) from exc

    def _open_temp_file(self, parent_fd: int, filename: str) -> tuple[str, int]:
        """Open a fresh temp file in *parent_fd*; return (name, fd)."""
        no_follow = _no_follow_flag()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow
        tmp_name = f".tmp-{os.getpid()}-{filename}"
        try:
            return tmp_name, os.open(tmp_name, flags, 0o644, dir_fd=parent_fd)
        except FileExistsError:
            tmp_name = f".tmp-{os.getpid()}-{filename}-{os.urandom(4).hex()}"
        try:
            return tmp_name, os.open(tmp_name, flags, 0o644, dir_fd=parent_fd)
        except OSError as exc:
            raise VaultPathError(
                f"could not create temp file for {filename}: {exc}"
            ) from exc

    @staticmethod
    def _try_unlink_tmp(parent_fd: int, tmp_name: str) -> None:
        """Best-effort cleanup of a temp file name."""
        try:
            os.unlink(tmp_name, dir_fd=parent_fd)
        except OSError:
            pass

    @staticmethod
    def _cleanup_temp(parent_fd: int, tmp_fd: int, tmp_name: str) -> None:
        """Best-effort temp file + FD cleanup on failure before publication."""
        VaultBoundary._try_unlink_tmp(parent_fd, tmp_name)
        if tmp_fd >= 0:
            try:
                os.close(tmp_fd)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Read snapshot (uses held FD and returns FileVersion)
    # ------------------------------------------------------------------

    def _read_snapshot(self, identity: PurePosixPath) -> AdmittedFileSnapshot:
        """Read one admitted identity through no-follow directory handles."""
        self._check_index_root_integrity()
        if not self.is_readable(identity):
            raise VaultPathError("file is no longer admitted")
        try:
            relative = identity.relative_to(self._index_relative)
        except ValueError as exc:
            raise VaultPathError("path is not admitted to index root") from exc

        try:
            file_descriptor = self._open_relative_file(relative)
        except (FileNotFoundError, OSError) as exc:
            raise VaultPathError(
                f"file not accessible: {identity.as_posix()} ({exc})"
            ) from exc
        try:
            before = os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise VaultPathError(
                    f"admitted path {identity.as_posix()} is not a regular file "
                    f"(may be a symlink)"
                )
            with os.fdopen(file_descriptor, "rb") as stream:
                file_descriptor = -1
                content = stream.read()
                after = os.fstat(stream.fileno())
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

        if _file_signature(before) != _file_signature(after):
            raise VaultPathError("file changed while it was being snapshotted")
        if len(content) != after.st_size:
            raise VaultPathError("file size changed while it was being snapshotted")

        version = FileVersion(
            device=after.st_dev,
            inode=after.st_ino,
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            ctime_ns=after.st_ctime_ns,
            sha256=hashlib.sha256(content).hexdigest(),
        )

        return AdmittedFileSnapshot(
            path=identity,
            content=content,
            file_size=after.st_size,
            file_mtime_ns=after.st_mtime_ns,
            version=version,
        )

    # ------------------------------------------------------------------
    # Path resolution (unchanged)
    # ------------------------------------------------------------------

    def vault_relative_path(self, admitted_path: Path) -> PurePosixPath:
        """Return an admitted filesystem path as a vault-relative POSIX path."""
        try:
            candidate = admitted_path.resolve(strict=False)
            relative = candidate.relative_to(self._vault_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise VaultPathError("path is not admitted to the index root") from exc
        if not self._is_within(candidate, self._index_root):
            raise VaultPathError("path is not admitted to the index root")
        identity = PurePosixPath(relative.as_posix())
        if not self.is_readable(identity):
            raise VaultPathError("path is denied by the configured folder rules")
        return identity

    def resolve_index_path(self, relative: PurePosixPath | str) -> Path:
        """Resolve an index-root-relative path with canonical containment checks."""
        decoded = self._decode_path(PurePosixPath(relative))
        self._validate_relative(decoded)
        try:
            candidate = (self._index_root / Path(*decoded.parts)).resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise VaultPathError("path cannot be resolved") from exc
        if not self._is_within(candidate, self._index_root):
            raise VaultPathError("path escapes index root")
        return candidate

    def resolve_ai_path(self, relative: PurePosixPath | str) -> Path:
        """Compatibility alias for :meth:`resolve_index_path`."""
        value = PurePosixPath(relative)
        if not self._index_relative.parts and value.parts:
            if value.parts[0].startswith("AI-"):
                raise VaultPathError("path is outside index root")
        elif self._index_relative.parts and value.parts:
            root_name = self._index_relative.parts[-1]
            if value.parts[0].startswith(f"{root_name}-"):
                raise VaultPathError("path is outside index root")
        return self.resolve_index_path(value)

    def resolve_vault_path(self, relative: PurePosixPath | str) -> Path:
        """Resolve a vault-relative identity through the configured boundary."""
        decoded = self._decode_path(PurePosixPath(relative))
        self._validate_relative(decoded, check_sibling=False)
        try:
            decoded.relative_to(self._index_relative)
        except ValueError as exc:
            raise VaultPathError("path is outside the configured index root") from exc
        if not self.is_readable(decoded):
            raise VaultPathError("path is denied by the configured folder rules")
        try:
            candidate = (self._vault_root / Path(*decoded.parts)).resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise VaultPathError("path cannot be resolved") from exc
        if not self._is_within(candidate, self._index_root):
            raise VaultPathError("path escapes index root")
        return candidate

    # ------------------------------------------------------------------
    # Iterators (held-fd recursive enumeration)
    # ------------------------------------------------------------------

    def iter_markdown_files(self) -> Iterator[Path]:
        """Yield readable regular Markdown files without following symlinks.

        Uses held index-root FD for traversal instead of ``os.walk``.
        """
        try:
            self._check_index_root_integrity()
        except VaultPathError:
            return

        index_fd = self._index_root_fd

        # Yield files in root first
        yield from self._list_regular_md_files(index_fd, self._index_relative)

        # Then recurse into subdirectories.  The entries generator owns the
        # child directory FDs and closes them on exhaustion or early
        # termination; the explicit close() below makes that deterministic.
        entries = self._recursive_dir_entries(index_fd, self._index_relative)
        try:
            for rel, fd in entries:
                yield from self._list_regular_md_files(fd, rel)
        finally:
            entries.close()

    def _list_regular_md_files(
        self, dir_fd: int, dir_prefix: PurePosixPath
    ) -> Iterator[Path]:
        """Yield non-symlinked .md files in a directory opened via dir_fd.

        *dir_prefix* is the vault-relative path of the directory (e.g.
        ``"AI"`` for the index root, ``"AI/sub"`` for a nested directory).
        """
        for entry in sorted(os.listdir(dir_fd)):
            if not entry.lower().endswith(".md"):
                continue
            # Skip symlinks
            try:
                st = os.lstat(entry, dir_fd=dir_fd)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                continue

            vault_relative = dir_prefix / entry
            if not self.is_readable(vault_relative):
                continue

            candidate = (self._vault_root / Path(*vault_relative.parts)).resolve(
                strict=False
            )
            if not self._is_within(candidate, self._index_root):
                continue
            yield candidate

    # ------------------------------------------------------------------
    # Snapshots (unchanged)
    # ------------------------------------------------------------------

    def iter_admitted_snapshots(self) -> Iterator[AdmittedFileSnapshot]:
        """Yield verified immutable snapshots in normalized identity order.

        All traversal and reads are done through the held index-root FD using
        ``dir_fd`` and ``O_NOFOLLOW`` -- no path is ever resolved by pathname
        after construction.
        """
        try:
            self._check_index_root_integrity()
        except VaultPathError:
            return

        root_fd = self._index_root_fd
        root_rel = self._index_relative

        # Collect all snapshots keyed by vault-relative identity string so we
        # can yield them in sorted order.
        snapshots: dict[str, AdmittedFileSnapshot] = {}

        # --- files directly in the index root ---
        for entry in sorted(os.listdir(root_fd)):
            if not entry.lower().endswith(".md"):
                continue
            vault_rel = root_rel / entry
            if not self.is_readable(vault_rel):
                continue
            try:
                st = os.lstat(entry, dir_fd=root_fd)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            snap = self._read_snapshot_from_dir(root_fd, entry, vault_rel)
            if snap is not None:
                snapshots[vault_rel.as_posix()] = snap

        # --- recursively process subdirectories ---
        # The entries generator owns the child directory FDs and closes
        # them on exhaustion or early termination; the explicit close()
        # below makes that deterministic.
        entries = self._recursive_dir_entries(root_fd, root_rel)
        try:
            for dir_rel, dir_fd in entries:
                for entry in sorted(os.listdir(dir_fd)):
                    if not entry.lower().endswith(".md"):
                        continue
                    vault_rel = dir_rel / entry
                    if not self.is_readable(vault_rel):
                        continue
                    try:
                        st = os.lstat(entry, dir_fd=dir_fd)
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    snap = self._read_snapshot_from_dir(dir_fd, entry, vault_rel)
                    if snap is not None:
                        snapshots[vault_rel.as_posix()] = snap
        finally:
            entries.close()

        for identity_string in sorted(snapshots):
            yield snapshots[identity_string]

    def _read_snapshot_from_dir(
        self,
        dir_fd: int,
        filename: str,
        identity: PurePosixPath,
    ) -> AdmittedFileSnapshot | None:
        """Read one file through an already-open parent directory FD.

        Opens the file with O_NOFOLLOW relative to *dir_fd*, fstats it,
        reads all bytes through the same FD, and returns an
        :class:`AdmittedFileSnapshot`.  Returns ``None`` if the file
        cannot be snapshotted.
        """
        no_follow = _no_follow_flag()
        try:
            fd = os.open(filename, os.O_RDONLY | no_follow, dir_fd=dir_fd)
        except OSError:
            return None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                return None
            content = self._read_all_from_fd(fd)
            after = os.fstat(fd)
        except (OSError, RuntimeError, ValueError):
            return None
        finally:
            os.close(fd)

        if _file_signature(before) != _file_signature(after):
            return None
        if len(content) != after.st_size:
            return None

        version = FileVersion(
            device=after.st_dev,
            inode=after.st_ino,
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            ctime_ns=after.st_ctime_ns,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        return AdmittedFileSnapshot(
            path=identity,
            content=content,
            file_size=after.st_size,
            file_mtime_ns=after.st_mtime_ns,
            version=version,
        )

    # ------------------------------------------------------------------
    # Static / private helpers (unchanged)
    # ------------------------------------------------------------------

    def _directory_is_readable(self, path: Path) -> bool:
        try:
            identity = PurePosixPath(path.relative_to(self._vault_root).as_posix())
        except ValueError:
            return False
        return self.is_readable(identity)

    def _validated_identity(self, relative: PurePosixPath | str) -> PurePosixPath:
        decoded = self._decode_path(PurePosixPath(relative))
        self._validate_relative(decoded, check_sibling=False)
        try:
            decoded.relative_to(self._index_relative)
        except ValueError as exc:
            raise VaultPathError("path is outside the configured index root") from exc
        return decoded

    def _resolve_under_vault(self, relative: PurePosixPath) -> Path:
        candidate = (self._vault_root / Path(*relative.parts)).resolve(strict=False)
        if not self._is_within(candidate, self._vault_root):
            raise VaultPathError("configured index root escapes the vault")
        return candidate

    @staticmethod
    def _is_within(candidate: Path, root: Path) -> bool:
        return candidate == root or root in candidate.parents

    @staticmethod
    def _decode_path(path: PurePosixPath) -> PurePosixPath:
        """Decode nested URL escapes before converting to filesystem parts."""
        decoded = path.as_posix()
        for _ in range(8):
            unescaped = unquote(decoded)
            if unescaped == decoded:
                break
            decoded = unescaped
        else:
            if unquote(decoded) != decoded:
                raise VaultPathError("path encoding is too deeply nested")
        return PurePosixPath(decoded)

    @staticmethod
    def _validate_relative(
        relative: PurePosixPath, *, check_sibling: bool = True
    ) -> None:
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\x00" in relative.as_posix()
            or "\\" in relative.as_posix()
        ):
            raise VaultPathError("path must be relative to index root")
        _ = check_sibling


def _is_same_or_descendant(identity: PurePosixPath, folder: PurePosixPath) -> bool:
    try:
        identity.relative_to(folder)
    except ValueError:
        return False
    return True


def _file_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


__all__ = [
    "AdmittedFileSnapshot",
    "FileVersion",
    "VaultBoundary",
    "VaultPathError",
    "WriteResult",
]
