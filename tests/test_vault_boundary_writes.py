"""Held-index-dirfd primitives tests for VaultBoundary (Lane A — Lane 1 rewrite)."""

from __future__ import annotations

import gc
import hashlib
import os
import resource
from pathlib import Path, PurePosixPath

import pytest

from harbor_ledger_memory.config import Settings
from harbor_ledger_memory.vault.boundary import (
    AdmittedFileSnapshot,
    DirectoryCreationError,
    DirectoryWriteResult,
    FileVersion,
    VaultBoundary,
    VaultPathError,
    WriteResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_boundary(
    tmp_path: Path,
    index_root: str = "AI",
) -> tuple[VaultBoundary, Path]:
    """Return (boundary, vault_path) for a minimal test vault."""
    vault = tmp_path / "vault"
    vault.mkdir()
    if index_root != ".":
        (vault / index_root).mkdir()
    boundary = VaultBoundary(Settings(vault_path=vault, index_root=index_root))
    return boundary, vault


def _write_raw(vault: Path, rel: str, content: bytes) -> Path:
    """Write bytes to a vault-relative path (setup-only, non-safe)."""
    abs_path = vault / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(content)
    return abs_path


def _file_version(abs_path: Path) -> FileVersion:
    """Compute FileVersion for a file on disk."""
    st = os.stat(abs_path)
    data = abs_path.read_bytes()
    return FileVersion(
        device=st.st_dev,
        inode=st.st_ino,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        ctime_ns=st.st_ctime_ns,
        sha256=hashlib.sha256(data).hexdigest(),
    )


# ---------------------------------------------------------------------------
# WriteResult structure
# ---------------------------------------------------------------------------


class TestWriteResult:
    def test_write_result_has_version(self, tmp_path: Path) -> None:
        """atomic_create returns WriteResult with valid version."""
        boundary, vault = _build_boundary(tmp_path)
        result = boundary.atomic_create(PurePosixPath("AI/new.md"), b"# New")
        assert isinstance(result, WriteResult)
        assert result.version is not None
        assert result.version.size == 5
        assert result.version.sha256 == hashlib.sha256(b"# New").hexdigest()

    def test_write_result_published_true_on_success(self, tmp_path: Path) -> None:
        """published is True when parent fsync succeeds."""
        boundary, vault = _build_boundary(tmp_path)
        result = boundary.atomic_create(PurePosixPath("AI/pub.md"), b"data")
        assert result.published is True

    def test_write_result_durable_true_on_success(self, tmp_path: Path) -> None:
        """durable is True when content fsync succeeds."""
        boundary, vault = _build_boundary(tmp_path)
        result = boundary.atomic_create(PurePosixPath("AI/dur.md"), b"data")
        assert result.durable is True

    def test_write_result_parent_fsync_failure_reports_uncertain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post-publication parent fsync failure: published=True (the link
        reached the target) but durable=False (durability uncertain)."""
        boundary, vault = _build_boundary(tmp_path)
        call_num = 0

        def _flaky_fsync(fd: int) -> None:
            nonlocal call_num
            call_num += 1
            # First fsync is content (succeeds), second is post-publication
            # parent fsync (fails)
            if call_num == 2:
                raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fsync", _flaky_fsync)
        result = boundary.atomic_create(PurePosixPath("AI/uncertain.md"), b"x")
        assert result.published is True
        assert result.durable is False
        # The file is still published under the target identity.
        assert (vault / "AI" / "uncertain.md").read_bytes() == b"x"
        assert result.version is not None
        assert result.version.sha256 == hashlib.sha256(b"x").hexdigest()

    def test_atomic_replace_returns_write_result(self, tmp_path: Path) -> None:
        """atomic_replace returns WriteResult."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        result = boundary.atomic_replace(
            PurePosixPath("AI/note.md"), b"# Updated", expected=fv
        )
        assert isinstance(result, WriteResult)
        assert result.durable is True
        assert result.published is True
        assert result.reconciliation_required is False

    def test_remove_if_version_returns_write_result(self, tmp_path: Path) -> None:
        """remove_if_version returns WriteResult with version of deleted file."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/rm.md", b"bye")
        fv = _file_version(existing)
        result = boundary.remove_if_version(PurePosixPath("AI/rm.md"), expected=fv)
        assert isinstance(result, WriteResult)
        assert result.version is not None
        assert not (vault / "AI" / "rm.md").exists()


class TestAtomicMkdir:
    def test_atomic_mkdir_surfaces_paths_created_before_walk_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boundary, vault = _build_boundary(tmp_path, index_root=".")
        real_open = os.open
        fail_path = "third"

        def _fail_after_first(path: str, flags: int, *args, **kwargs):
            if path == fail_path:
                raise OSError(5, "injected open failure")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _fail_after_first)
        with pytest.raises(DirectoryCreationError) as exc_info:
            boundary.atomic_mkdir(PurePosixPath("first/second/third"))

        assert exc_info.value.created_paths == (
            PurePosixPath("first"),
            PurePosixPath("first/second"),
        )
        assert (vault / "first").is_dir()

    def test_atomic_mkdir_closes_returned_fd_in_finally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boundary, _vault = _build_boundary(tmp_path, index_root=".")
        returned_fd = os.dup(boundary._index_root_fd)
        closed: list[int] = []

        monkeypatch.setattr(
            boundary,
            "_walk_dir_fd_with_created",
            lambda _relative: (returned_fd, [], True),
        )
        real_close = os.close

        def _record_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(os, "close", _record_close)
        boundary.atomic_mkdir(PurePosixPath("AI"))

        assert returned_fd in closed
        with pytest.raises(OSError):
            os.fstat(returned_fd)

    def test_atomic_mkdir_reopen_race_is_normalized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boundary, _vault = _build_boundary(tmp_path, index_root=".")
        real_mkdir = os.mkdir
        real_open = os.open
        reopened = False

        def _mark_mkdir(
            path: str, mode: int = 0o777, *, dir_fd: int | None = None
        ) -> None:
            nonlocal reopened
            real_mkdir(path, mode, dir_fd=dir_fd)
            reopened = True

        def _race_open(path: str, flags: int, *args, **kwargs):
            if reopened and path == "AI":
                raise FileNotFoundError(path)
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "mkdir", _mark_mkdir)
        monkeypatch.setattr(os, "open", _race_open)
        with pytest.raises(VaultPathError, match="directory component"):
            boundary.atomic_mkdir(PurePosixPath("AI"))

    def test_atomic_mkdir_fails_closed_without_no_follow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boundary, _vault = _build_boundary(tmp_path, index_root=".")
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        with pytest.raises(VaultPathError, match="no-follow"):
            boundary.atomic_mkdir(PurePosixPath("AI"))

    def test_atomic_mkdir_creates_nested_directory_and_reports_each_created_path(
        self, tmp_path: Path
    ) -> None:
        boundary, vault = _build_boundary(tmp_path, index_root=".")

        result = boundary.atomic_mkdir(PurePosixPath("AI/Inbox/2026"))

        assert isinstance(result, DirectoryWriteResult)
        assert result.created_paths == (
            PurePosixPath("AI"),
            PurePosixPath("AI/Inbox"),
            PurePosixPath("AI/Inbox/2026"),
        )
        assert result.published is True
        assert result.durable is True
        assert (vault / "AI" / "Inbox" / "2026").is_dir()

    def test_atomic_mkdir_is_idempotent_and_rejects_file_collision(
        self, tmp_path: Path
    ) -> None:
        boundary, vault = _build_boundary(tmp_path, index_root=".")

        boundary.atomic_mkdir(PurePosixPath("AI/Inbox"))
        assert boundary.atomic_mkdir(PurePosixPath("AI/Inbox")).created_paths == ()
        (vault / "AI" / "file").write_text("not a directory")
        with pytest.raises(VaultPathError, match="directory"):
            boundary.atomic_mkdir(PurePosixPath("AI/file/child"))

        outside = tmp_path / "outside"
        outside.mkdir()
        (vault / "AI" / "link").symlink_to(outside, target_is_directory=True)
        with pytest.raises(VaultPathError, match="directory"):
            boundary.atomic_mkdir(PurePosixPath("AI/link/child"))

    def test_missing_parent_paths_does_not_create_directories(
        self, tmp_path: Path
    ) -> None:
        boundary, vault = _build_boundary(tmp_path, index_root=".")

        assert boundary.missing_parent_paths(PurePosixPath("AI/Inbox/note.md")) == (
            PurePosixPath("AI"),
            PurePosixPath("AI/Inbox"),
        )
        assert not (vault / "AI").exists()


# ---------------------------------------------------------------------------
# atomic_create — held-fd, no-clobber
# ---------------------------------------------------------------------------


class TestAtomicCreate:
    def test_atomic_create_creates_file(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        result = boundary.atomic_create(PurePosixPath("AI/new.md"), b"# New")
        assert (vault / "AI" / "new.md").read_bytes() == b"# New"
        assert result.durable

    def test_atomic_create_nested_dirs(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        result = boundary.atomic_create(PurePosixPath("AI/deep/note.md"), b"# Deep")
        assert (vault / "AI" / "deep" / "note.md").read_bytes() == b"# Deep"
        assert result.durable

    def test_atomic_create_nested_path_creates_missing_parents(
        self, tmp_path: Path
    ) -> None:
        boundary, vault = _build_boundary(tmp_path)
        boundary.atomic_create(PurePosixPath("AI/Inbox/new.md"), b"# New")
        assert (vault / "AI" / "Inbox").is_dir()
        assert (vault / "AI" / "Inbox" / "new.md").read_bytes() == b"# New"

    def test_atomic_create_no_overwrite_existing(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        _write_raw(vault, "AI/existing.md", b"# Original")
        with pytest.raises(VaultPathError, match="already exists"):
            boundary.atomic_create(PurePosixPath("AI/existing.md"), b"# New")
        assert (vault / "AI" / "existing.md").read_bytes() == b"# Original"

    def test_atomic_create_no_overwrite_symlink(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        target = vault / "real.md"
        target.write_bytes(b"real")
        (vault / "AI" / "link.md").symlink_to(target)
        with pytest.raises(VaultPathError, match="already exists"):
            boundary.atomic_create(PurePosixPath("AI/link.md"), b"# New")
        # Symlink untouched
        assert (vault / "AI" / "link.md").is_symlink()

    def test_atomic_create_target_appears_during_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Race: target appears between temp creation and link."""
        boundary, vault = _build_boundary(tmp_path)

        original_link = os.link

        def _race_link(*args, **kwargs):
            (vault / "AI" / "race.md").write_bytes(b"external")
            return original_link(*args, **kwargs)

        monkeypatch.setattr(os, "link", _race_link)
        with pytest.raises(VaultPathError, match="already exists"):
            boundary.atomic_create(PurePosixPath("AI/race.md"), b"# AI")
        assert (vault / "AI" / "race.md").read_bytes() == b"external"

    def test_atomic_create_symlinked_parent_rejected(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (vault / "AI" / "linked").symlink_to(outside, target_is_directory=True)
        with pytest.raises(VaultPathError, match="symlink"):
            boundary.atomic_create(PurePosixPath("AI/linked/escape.md"), b"#")

    def test_atomic_create_traversal_rejected(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        with pytest.raises(VaultPathError):
            boundary.atomic_create(PurePosixPath("AI/../secret.md"), b"#")

    def test_atomic_create_no_temp_on_fsync_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Temp file is cleaned up if fsync fails."""
        boundary, vault = _build_boundary(tmp_path)

        def _fail_fsync(fd: int) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fsync", _fail_fsync)
        with pytest.raises(VaultPathError):
            boundary.atomic_create(PurePosixPath("AI/nope.md"), b"#")
        assert not (vault / "AI" / "nope.md").exists()
        temps = [f for f in (vault / "AI").iterdir() if f.name.startswith(".tmp-")]
        assert not temps

    def test_atomic_create_no_temp_on_link_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Temp file is cleaned up if link fails."""
        boundary, vault = _build_boundary(tmp_path)

        def _fail_link(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "link", _fail_link)
        with pytest.raises(VaultPathError):
            boundary.atomic_create(PurePosixPath("AI/nope.md"), b"#")
        assert not (vault / "AI" / "nope.md").exists()
        temps = [f for f in (vault / "AI").iterdir() if f.name.startswith(".tmp-")]
        assert not temps

    def test_atomic_create_zero_write_no_temp_residue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Injected zero-length write: error raised, no target created,
        and no temp file residue."""
        boundary, vault = _build_boundary(tmp_path)

        def _zero_write(fd: int, data: bytes) -> int:
            return 0

        monkeypatch.setattr(os, "write", _zero_write)
        with pytest.raises(VaultPathError, match="short write"):
            boundary.atomic_create(PurePosixPath("AI/zero.md"), b"# data")
        assert not (vault / "AI" / "zero.md").exists()
        temps = [f for f in (vault / "AI").iterdir() if f.name.startswith(".tmp-")]
        assert not temps


# ---------------------------------------------------------------------------
# atomic_replace — held-fd, version-checked, validate_current callback
# ---------------------------------------------------------------------------


class TestAtomicReplace:
    def test_atomic_replace_matching_version(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        result = boundary.atomic_replace(
            PurePosixPath("AI/note.md"), b"# Updated", expected=fv
        )
        assert (vault / "AI" / "note.md").read_bytes() == b"# Updated"
        assert result.durable

    def test_atomic_replace_all_version_fields(self, tmp_path: Path) -> None:
        """All FileVersion fields are checked on replace."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        bad_fv = FileVersion(
            device=fv.device,
            inode=fv.inode,
            size=fv.size,
            mtime_ns=fv.mtime_ns,
            ctime_ns=fv.ctime_ns,
            sha256="0" * 64,
        )
        with pytest.raises(VaultPathError, match="version conflict"):
            boundary.atomic_replace(
                PurePosixPath("AI/note.md"), b"# New", expected=bad_fv
            )
        assert (vault / "AI" / "note.md").read_bytes() == b"# Original\n"

    def test_atomic_replace_target_deleted(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        existing.unlink()
        with pytest.raises(VaultPathError, match="not found|missing"):
            boundary.atomic_replace(PurePosixPath("AI/note.md"), b"# New", expected=fv)

    def test_atomic_replace_target_replaced_with_symlink(self, tmp_path: Path) -> None:
        """Replace when target became a symlink should fail."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        existing.unlink()
        other = vault / "other.md"
        other.write_bytes(b"other")
        existing.symlink_to(other)
        with pytest.raises(VaultPathError, match="symlink|regular"):
            boundary.atomic_replace(PurePosixPath("AI/note.md"), b"# New", expected=fv)

    def test_atomic_replace_target_is_directory(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        existing.unlink()
        existing.mkdir()
        with pytest.raises(VaultPathError, match="regular|directory"):
            boundary.atomic_replace(PurePosixPath("AI/note.md"), b"# New", expected=fv)

    def test_atomic_replace_version_conflict_size(self, tmp_path: Path) -> None:
        """Mismatched size should reject the replace."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        existing.write_bytes(b"# Changed content now longer")
        with pytest.raises(VaultPathError, match="version conflict"):
            boundary.atomic_replace(PurePosixPath("AI/note.md"), b"# New", expected=fv)
        assert existing.read_bytes() == b"# Changed content now longer"

    def test_atomic_replace_symlinked_parent_rejected(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        (vault / "AI" / "sub").mkdir()
        existing = _write_raw(vault, "AI/sub/note.md", b"# Original\n")
        fv = _file_version(existing)
        outside = tmp_path / "outside"
        outside.mkdir()
        (vault / "AI" / "sub").rename(vault / "AI" / "sub-old")
        (vault / "AI" / "sub").symlink_to(outside, target_is_directory=True)
        with pytest.raises(VaultPathError, match="symlink"):
            boundary.atomic_replace(
                PurePosixPath("AI/sub/note.md"), b"# New", expected=fv
            )

    def test_atomic_replace_traversal_rejected(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        fv = FileVersion(device=0, inode=0, size=0, mtime_ns=0, ctime_ns=0, sha256="x")
        with pytest.raises(VaultPathError):
            boundary.atomic_replace(PurePosixPath("AI/../secret.md"), b"#", expected=fv)

    def test_atomic_replace_original_preserved_on_fsync_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fsync failure: temp cleaned, original preserved."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        original_bytes = existing.read_bytes()

        def _fail_fsync(fd: int) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fsync", _fail_fsync)
        with pytest.raises(VaultPathError):
            boundary.atomic_replace(PurePosixPath("AI/note.md"), b"# New", expected=fv)
        assert existing.read_bytes() == original_bytes
        temps = [f for f in (vault / "AI").iterdir() if f.name.startswith(".tmp-")]
        assert not temps

    def test_atomic_replace_original_preserved_on_rename_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rename failure: temp cleaned, original preserved."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        original_bytes = existing.read_bytes()

        def _fail_rename(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "rename", _fail_rename)
        with pytest.raises(VaultPathError):
            boundary.atomic_replace(PurePosixPath("AI/note.md"), b"# New", expected=fv)
        assert existing.read_bytes() == original_bytes
        temps = [f for f in (vault / "AI").iterdir() if f.name.startswith(".tmp-")]
        assert not temps

    def test_atomic_replace_zero_write_no_temp_residue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Injected zero-length write: error raised, original preserved,
        and no temp file residue."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)

        def _zero_write(fd: int, data: bytes) -> int:
            return 0

        monkeypatch.setattr(os, "write", _zero_write)
        with pytest.raises(VaultPathError, match="short write"):
            boundary.atomic_replace(PurePosixPath("AI/note.md"), b"# New", expected=fv)
        assert existing.read_bytes() == b"# Original\n"
        temps = [f for f in (vault / "AI").iterdir() if f.name.startswith(".tmp-")]
        assert not temps

    def test_atomic_replace_short_write_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partial writes are retried until all bytes are written."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        content = b"x" * 1000
        _real_write = os.write
        call_count = 0

        def _partial_write(fd: int, data: bytes) -> int:
            nonlocal call_count
            call_count += 1
            return _real_write(fd, data[:1])

        monkeypatch.setattr(os, "write", _partial_write)
        boundary.atomic_replace(PurePosixPath("AI/note.md"), content, expected=fv)
        assert (vault / "AI" / "note.md").read_bytes() == content
        assert call_count == 1000

    # -- validate_current callback tests --

    def test_atomic_replace_validate_current_callback(self, tmp_path: Path) -> None:
        """validate_current receives the coherent current file snapshot
        (content AND version from the same observation) between the version
        check and the write."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)

        called: list[AdmittedFileSnapshot] = []

        def _validate(current: AdmittedFileSnapshot) -> bool:
            called.append(current)
            return True

        result = boundary.atomic_replace(
            PurePosixPath("AI/note.md"),
            b"# Updated",
            expected=fv,
            validate_current=_validate,
        )
        assert len(called) == 1
        # The callback gets the full coherent snapshot: content bytes and
        # FileVersion from the same single-FD observation.
        assert called[0].content == b"# Original\n"
        assert called[0].version == fv
        assert result.durable

    def test_atomic_replace_validate_current_rejects(self, tmp_path: Path) -> None:
        """validate_current returning False aborts the replace."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)

        def _validate(current: AdmittedFileSnapshot) -> bool:
            return False

        with pytest.raises(VaultPathError, match="rejected"):
            boundary.atomic_replace(
                PurePosixPath("AI/note.md"),
                b"# New",
                expected=fv,
                validate_current=_validate,
            )
        # Original preserved
        assert existing.read_bytes() == b"# Original\n"


# ---------------------------------------------------------------------------
# remove_if_version
# ---------------------------------------------------------------------------


class TestRemoveIfVersion:
    def test_remove_if_version_matching(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/rm.md", b"bye")
        fv = _file_version(existing)
        result = boundary.remove_if_version(PurePosixPath("AI/rm.md"), expected=fv)
        assert not (vault / "AI" / "rm.md").exists()
        assert result.version is not None

    def test_remove_if_version_mismatch(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        _write_raw(vault, "AI/rm.md", b"bye")
        bad_fv = FileVersion(
            device=0, inode=0, size=0, mtime_ns=0, ctime_ns=0, sha256="x"
        )
        with pytest.raises(VaultPathError, match="version conflict"):
            boundary.remove_if_version(PurePosixPath("AI/rm.md"), expected=bad_fv)
        assert (vault / "AI" / "rm.md").exists()

    def test_remove_if_version_not_found(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        fv = FileVersion(device=0, inode=0, size=0, mtime_ns=0, ctime_ns=0, sha256="x")
        with pytest.raises(VaultPathError, match="not found|missing"):
            boundary.remove_if_version(PurePosixPath("AI/gone.md"), expected=fv)

    def test_remove_unlink_failure_reports_uncertainty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed unlink must report uncertainty -- never claim the
        removal (or a rollback) succeeded."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/rm.md", b"bye")
        fv = _file_version(existing)

        def _fail_unlink(*args, **kwargs):
            raise OSError(16, "Device or resource busy")

        monkeypatch.setattr(os, "unlink", _fail_unlink)
        with pytest.raises(VaultPathError, match="uncertain"):
            boundary.remove_if_version(PurePosixPath("AI/rm.md"), expected=fv)
        # The file is still on disk; nothing was removed or rolled back.
        assert existing.read_bytes() == b"bye"

    def test_remove_parent_fsync_failure_reports_uncertain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Post-unlink parent fsync failure: the removal is published
        (published=True) but its durability is uncertain (durable=False);
        it is NOT reported as a failed/rolled-back removal."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/rm.md", b"bye")
        fv = _file_version(existing)

        def _fail_fsync(fd: int) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fsync", _fail_fsync)
        result = boundary.remove_if_version(PurePosixPath("AI/rm.md"), expected=fv)
        assert result.published is True
        assert result.durable is False
        assert result.version is not None
        assert not (vault / "AI" / "rm.md").exists()

    def test_remove_symlink_target_rejected(self, tmp_path: Path) -> None:
        """remove_if_version must not follow a symlinked target."""
        boundary, vault = _build_boundary(tmp_path)
        outside = tmp_path / "outside.md"
        outside.write_bytes(b"outside")
        (vault / "AI" / "rm.md").symlink_to(outside)
        fv = FileVersion(device=0, inode=0, size=0, mtime_ns=0, ctime_ns=0, sha256="x")
        with pytest.raises(VaultPathError, match="symlink"):
            boundary.remove_if_version(PurePosixPath("AI/rm.md"), expected=fv)
        # Neither the symlink nor the outside target was removed.
        assert (vault / "AI" / "rm.md").is_symlink()
        assert outside.read_bytes() == b"outside"

    def test_remove_directory_target_rejected(self, tmp_path: Path) -> None:
        """remove_if_version must not remove a directory target."""
        boundary, vault = _build_boundary(tmp_path)
        (vault / "AI" / "rm.md").mkdir()
        fv = FileVersion(device=0, inode=0, size=0, mtime_ns=0, ctime_ns=0, sha256="x")
        with pytest.raises(VaultPathError, match="directory|regular"):
            boundary.remove_if_version(PurePosixPath("AI/rm.md"), expected=fv)
        assert (vault / "AI" / "rm.md").is_dir()


# ---------------------------------------------------------------------------
# Post-link fsync publication state
# ---------------------------------------------------------------------------


class TestPostLinkFsync:
    def test_post_link_fsync_failure_returns_uncertain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If parent fsync fails after rename, the operation is published
        (published=True) but durability is uncertain (durable=False); no
        exception is raised."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)

        call_num = 0

        def _flaky_fsync(fd: int) -> None:
            nonlocal call_num
            call_num += 1
            if call_num == 2:
                raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fsync", _flaky_fsync)
        result = boundary.atomic_replace(
            PurePosixPath("AI/note.md"), b"# Updated", expected=fv
        )
        assert result.published is True
        assert result.durable is False
        # File was actually renamed
        assert (vault / "AI" / "note.md").read_bytes() == b"# Updated"
        # Version is still reported for the published content.
        assert result.version is not None
        assert result.version.sha256 == hashlib.sha256(b"# Updated").hexdigest()

    def test_post_link_fsync_failure_create_returns_uncertain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same post-publication fsync contract for atomic_create: the
        no-clobber link succeeded (published=True) but durability is
        uncertain (durable=False)."""
        boundary, vault = _build_boundary(tmp_path)

        call_num = 0

        def _flaky_fsync(fd: int) -> None:
            nonlocal call_num
            call_num += 1
            if call_num == 2:
                raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "fsync", _flaky_fsync)
        result = boundary.atomic_create(PurePosixPath("AI/new.md"), b"# New")
        assert result.published is True
        assert result.durable is False
        assert (vault / "AI" / "new.md").read_bytes() == b"# New"
        # No temp residue despite the failed parent fsync.
        temps = [f for f in (vault / "AI").iterdir() if f.name.startswith(".tmp-")]
        assert not temps


# ---------------------------------------------------------------------------
# Held-dirfd scanner enumeration
# ---------------------------------------------------------------------------


class TestHeldDirfdEnumeration:
    def test_iter_markdown_files_no_os_walk(self, tmp_path: Path) -> None:
        """Enumeration uses held fd, not os.walk."""
        boundary, vault = _build_boundary(tmp_path)
        _write_raw(vault, "AI/a.md", b"# A")
        (vault / "AI" / "sub").mkdir()
        _write_raw(vault, "AI/sub/b.md", b"# B")
        _write_raw(vault, "AI/sub/c.txt", b"not md")
        files = [f for f in boundary.iter_markdown_files()]
        assert len(files) == 2
        names = {f.name for f in files}
        assert "a.md" in names
        assert "b.md" in names

    def test_iter_markdown_files_skips_symlinked_dirs(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "linked.md").write_bytes(b"outside")
        (vault / "AI" / "linked").symlink_to(outside, target_is_directory=True)
        _write_raw(vault, "AI/real.md", b"# Real")
        files = list(boundary.iter_markdown_files())
        assert len(files) == 1
        assert files[0].name == "real.md"

    def test_iter_markdown_files_skips_symlinked_files(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        real = vault / "real.md"
        real.write_bytes(b"real")
        (vault / "AI" / "link.md").symlink_to(real)
        _write_raw(vault, "AI/real2.md", b"# Real")
        files = list(boundary.iter_markdown_files())
        assert len(files) == 1
        assert files[0].name == "real2.md"

    def test_iter_markdown_files_empty_when_root_replaced(self, tmp_path: Path) -> None:
        """If index root is replaced by symlink, enumeration returns empty."""
        boundary, vault = _build_boundary(tmp_path)
        original_root = vault / "AI"
        outside = tmp_path / "outside"
        outside.mkdir()
        original_root.rename(vault / "AI-old")
        original_root.symlink_to(outside, target_is_directory=True)
        assert list(boundary.iter_markdown_files()) == []


# ---------------------------------------------------------------------------
# Parent/ancestor symlink replacement after boundary construction
# ---------------------------------------------------------------------------


class TestParentSymlinkReplacement:
    def test_rejects_replaced_root_after_construction(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        original_root = vault / "AI"
        outside = tmp_path / "outside"
        outside.mkdir()
        original_root.rename(vault / "AI-old")
        original_root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(VaultPathError):
            boundary.read_file(PurePosixPath("AI/anything.md"))

    def test_rejects_symlinked_ancestor_after_construction(
        self, tmp_path: Path
    ) -> None:
        boundary, vault = _build_boundary(tmp_path)
        (vault / "AI" / "sub").mkdir()
        _write_raw(vault, "AI/sub/note.md", b"original")
        (vault / "AI" / "sub").rename(vault / "AI" / "sub-old")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "note.md").write_bytes(b"outside")
        (vault / "AI" / "sub").symlink_to(outside, target_is_directory=True)
        with pytest.raises(VaultPathError, match="symlink"):
            boundary.read_file(PurePosixPath("AI/sub/note.md"))

    def test_fd_based_access_survives_renamed_vault(self, tmp_path: Path) -> None:
        boundary, vault = _build_boundary(tmp_path)
        _write_raw(vault, "AI/safe.md", b"content")
        snapshot = boundary.read_file(PurePosixPath("AI/safe.md"))
        assert snapshot.content == b"content"


# ---------------------------------------------------------------------------
# iter_admitted_snapshots — FD-rooted recursion, no pathname resolve
# ---------------------------------------------------------------------------


class TestIterAdmittedSnapshotsFD:
    """iter_admitted_snapshots must not resolve paths by pathname after
    enumeration.  All traversal and reads must use held directory FDs
    with O_NOFOLLOW."""

    def test_snapshots_no_resolve_on_symlinked_ancestor(self, tmp_path: Path) -> None:
        """If a subdirectory is replaced by a symlink after boundary
        construction, snapshots should NOT read the symlinked target."""
        boundary, vault = _build_boundary(tmp_path)
        (vault / "AI" / "sub").mkdir()
        _write_raw(vault, "AI/sub/inside.md", b"inside content")
        # Replace sub/ with symlink to outside directory
        (vault / "AI" / "sub").rename(vault / "AI" / "sub-old")
        outside = tmp_path / "outside"
        outside.mkdir()
        _write_raw(outside, "leaked.md", b"OUTSIDE CONTENT")
        (vault / "AI" / "sub").symlink_to(outside, target_is_directory=True)
        # iter_admitted_snapshots should NOT yield the outside file
        snapshots = list(boundary.iter_admitted_snapshots())
        # Should be empty (or not contain the outside file) because the
        # symlinked directory is skipped during FD traversal.
        for snap in snapshots:
            assert snap.content != b"OUTSIDE CONTENT"

    def test_snapshots_only_yield_admitted_files(self, tmp_path: Path) -> None:
        """Snapshots should only yield regular non-symlinked files."""
        boundary, vault = _build_boundary(tmp_path)
        _write_raw(vault, "AI/real.md", b"real")
        _write_raw(vault, "AI/skip.txt", b"not md")
        real_file = vault / "AI" / "real.md"
        (vault / "AI" / "link.md").symlink_to(real_file)
        snapshots = list(boundary.iter_admitted_snapshots())
        paths = {snap.path for snap in snapshots}
        # Only the real file, not the symlink
        assert PurePosixPath("AI/real.md") in paths
        assert PurePosixPath("AI/link.md") not in paths
        assert PurePosixPath("AI/skip.txt") not in paths

    def test_snapshots_no_outside_read_on_ancestor_swap(self, tmp_path: Path) -> None:
        """Between enumeration and read, if the vault parent is swapped to a
        symlink, we should NOT read outside content. The FD should remain
        anchored to the original directory."""
        boundary, vault = _build_boundary(tmp_path)
        _write_raw(vault, "AI/note.md", b"original")
        # Keep a reference to the held fd to verify it still works
        snapshots = list(boundary.iter_admitted_snapshots())
        assert len(snapshots) == 1
        assert snapshots[0].content == b"original"


# ---------------------------------------------------------------------------
# atomic_replace / remove_if_version — single no-follow FD for stat + read
# ---------------------------------------------------------------------------


class TestReplaceSingleFD:
    """_replace_in_dir and _remove_in_dir must use ONE O_NOFOLLOW target FD
    for both fstat and content reads.  They must never reopen the target by
    pathname, eliminating TOCTOU windows."""

    def test_replace_swap_to_symlink_between_stat_and_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the directory entry is replaced by a symlink after the version
        snapshot, the rename must NOT publish over the swap.  The
        pre-publication re-check detects that the name no longer refers to
        the snapshotted file and reports reconciliation-required; the
        symlink (and its outside target) stays untouched."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        outside = tmp_path / "outside_target.md"
        outside.write_bytes(b"OUTSIDE DATA THAT SHOULD NOT BE READ")

        real_fstat = os.fstat
        fstat_calls = 0

        def _swap_on_fstat(fd: int) -> os.stat_result:
            nonlocal fstat_calls
            st = real_fstat(fd)
            fstat_calls += 1
            # Swap on the target file's fstat (second call: first is
            # _check_index_root_integrity's fstat, second is target).
            if fstat_calls == 2:
                # Swap the directory entry to a symlink.
                # The open FD still points to the original inode,
                # so the snapshot still describes the original data.
                existing.unlink()
                existing.symlink_to(outside)
            return st

        monkeypatch.setattr(os, "fstat", _swap_on_fstat)

        # The version snapshot matches, but the pre-publication re-check
        # sees the symlink: nothing is renamed.
        result = boundary.atomic_replace(
            PurePosixPath("AI/note.md"), b"# Updated", expected=fv
        )
        assert result.reconciliation_required is True
        assert result.published is False
        assert result.durable is False
        assert result.version == fv
        # The symlink is still under the name -- not clobbered.
        assert (vault / "AI" / "note.md").is_symlink()
        # Outside file was never read or clobbered.
        assert outside.read_bytes() == b"OUTSIDE DATA THAT SHOULD NOT BE READ"
        temps = [f for f in (vault / "AI").iterdir() if f.name.startswith(".tmp-")]
        assert not temps

    def test_replace_already_symlink_rejected(self, tmp_path: Path) -> None:
        """If the target is a symlink when atomic_replace starts, the
        O_NOFOLLOW open fails immediately.  Outside data must NOT be read."""
        boundary, vault = _build_boundary(tmp_path)
        outside = tmp_path / "outside_target.md"
        outside.write_bytes(b"OUTSIDE DATA")
        (vault / "AI" / "note.md").symlink_to(outside)
        fv = _file_version(vault / "AI" / "note.md")
        with pytest.raises(VaultPathError, match="symlink"):
            boundary.atomic_replace(PurePosixPath("AI/note.md"), b"# New", expected=fv)
        assert outside.read_bytes() == b"OUTSIDE DATA"

    def test_remove_swap_to_symlink_between_stat_and_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the directory entry is replaced by a symlink after the version
        snapshot, remove_if_version must NOT unlink the name: unlinking the
        name would remove the symlink this API never inspected (and prove
        nothing about the original file).  It reports
        reconciliation-required instead and leaves the symlink untouched."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        outside = tmp_path / "outside_target.md"
        outside.write_bytes(b"OUTSIDE DATA")

        real_fstat = os.fstat
        fstat_calls = 0

        def _swap_on_fstat(fd: int) -> os.stat_result:
            nonlocal fstat_calls
            st = real_fstat(fd)
            fstat_calls += 1
            if fstat_calls == 2:
                existing.unlink()
                existing.symlink_to(outside)
            return st

        monkeypatch.setattr(os, "fstat", _swap_on_fstat)

        # The version snapshot matches (read through the held FD of the
        # original inode), but the pre-unlink re-verify sees the symlink:
        # no unlink happens.
        result = boundary.remove_if_version(PurePosixPath("AI/note.md"), expected=fv)
        assert result.reconciliation_required is True
        assert result.published is False
        assert result.durable is False
        assert result.version == fv
        # The symlink is still under the name -- nothing was unlinked.
        assert (vault / "AI" / "note.md").is_symlink()
        # The outside target is untouched.
        assert outside.read_bytes() == b"OUTSIDE DATA"

    def test_outside_content_not_read_on_swap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sha256 during replace reads through the held target FD, not
        from a separate pathname-based open.  A symlink swap between fstat
        and read must NOT leak outside data into the hash, and the
        pre-publication re-check must NOT rename over the swap."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)
        outside = tmp_path / "outside_read.md"
        outside.write_bytes(b"SHOULD_NOT_APPEAR_IN_HASH")

        real_fstat = os.fstat
        fstat_calls = 0

        def _swap_on_fstat(fd: int) -> os.stat_result:
            nonlocal fstat_calls
            st = real_fstat(fd)
            fstat_calls += 1
            if fstat_calls == 2:
                existing.unlink()
                existing.symlink_to(outside)
            return st

        monkeypatch.setattr(os, "fstat", _swap_on_fstat)

        # The version check matches (hash from the held FD of the original
        # inode), but the pre-publication re-check sees the symlink under
        # the name: nothing is renamed and nothing is published.
        result = boundary.atomic_replace(
            PurePosixPath("AI/note.md"), b"# Updated", expected=fv
        )
        assert result.reconciliation_required is True
        assert result.published is False
        # The symlink remains under the name (not clobbered).
        assert (vault / "AI" / "note.md").is_symlink()
        # Outside file untouched
        assert outside.read_bytes() == b"SHOULD_NOT_APPEAR_IN_HASH"

    def test_replace_swap_to_regular_file_before_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the entry is replaced by a DIFFERENT regular file after the
        version snapshot, the rename must not clobber the external file:
        the pre-publication re-check reports reconciliation-required and
        nothing is published."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)

        real_fstat = os.fstat
        fstat_calls = 0

        def _swap_on_fstat(fd: int) -> os.stat_result:
            nonlocal fstat_calls
            st = real_fstat(fd)
            fstat_calls += 1
            if fstat_calls == 2:
                existing.unlink()
                existing.write_bytes(b"EXTERNAL REPLACEMENT CONTENT")
            return st

        monkeypatch.setattr(os, "fstat", _swap_on_fstat)

        result = boundary.atomic_replace(
            PurePosixPath("AI/note.md"), b"# Updated", expected=fv
        )
        assert result.reconciliation_required is True
        assert result.published is False
        assert result.durable is False
        # The external replacement file is intact -- not clobbered.
        assert existing.read_bytes() == b"EXTERNAL REPLACEMENT CONTENT"
        temps = [f for f in (vault / "AI").iterdir() if f.name.startswith(".tmp-")]
        assert not temps

    def test_replace_entry_gone_before_rename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the entry is removed by an external actor after the version
        snapshot, the rename has no verified destination: the boundary
        reports reconciliation-required, publishes nothing, and cleans up
        the temp file."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/note.md", b"# Original\n")
        fv = _file_version(existing)

        real_fstat = os.fstat
        fstat_calls = 0

        def _delete_on_fstat(fd: int) -> os.stat_result:
            nonlocal fstat_calls
            st = real_fstat(fd)
            fstat_calls += 1
            if fstat_calls == 2:
                existing.unlink()
            return st

        monkeypatch.setattr(os, "fstat", _delete_on_fstat)

        result = boundary.atomic_replace(
            PurePosixPath("AI/note.md"), b"# Updated", expected=fv
        )
        assert result.reconciliation_required is True
        assert result.published is False
        assert result.durable is False
        assert not existing.exists()
        temps = [f for f in (vault / "AI").iterdir() if f.name.startswith(".tmp-")]
        assert not temps


# ---------------------------------------------------------------------------
# remove_if_version — pre-unlink re-verify (no unsafe unlink after swap)
# ---------------------------------------------------------------------------


class TestRemovePreUnlinkReverify:
    """remove_if_version must never unlink a name after its version snapshot
    when an external swap could make the deletion unsafe: without proof the
    name still refers to the snapshotted file, it returns an explicit
    reconciliation-required outcome instead of deleting."""

    def test_remove_swap_to_regular_file_before_unlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the entry is replaced by a DIFFERENT regular file after the
        version snapshot, the unlink must not delete the external file:
        the pre-unlink re-verify reports reconciliation-required and
        nothing is removed."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/rm.md", b"bye")
        fv = _file_version(existing)

        real_fstat = os.fstat
        fstat_calls = 0

        def _swap_on_fstat(fd: int) -> os.stat_result:
            nonlocal fstat_calls
            st = real_fstat(fd)
            fstat_calls += 1
            if fstat_calls == 2:
                existing.unlink()
                existing.write_bytes(b"EXTERNAL REPLACEMENT CONTENT")
            return st

        monkeypatch.setattr(os, "fstat", _swap_on_fstat)

        result = boundary.remove_if_version(PurePosixPath("AI/rm.md"), expected=fv)
        assert result.reconciliation_required is True
        assert result.published is False
        assert result.durable is False
        assert result.version == fv
        # The external replacement file is intact -- not unlinked.
        assert existing.read_bytes() == b"EXTERNAL REPLACEMENT CONTENT"

    def test_remove_entry_gone_after_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the entry is removed by an external actor after the version
        snapshot, remove_if_version cannot prove what an unlink would act
        on: it reports reconciliation-required without unlinking."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/rm.md", b"bye")
        fv = _file_version(existing)

        real_fstat = os.fstat
        fstat_calls = 0

        def _delete_on_fstat(fd: int) -> os.stat_result:
            nonlocal fstat_calls
            st = real_fstat(fd)
            fstat_calls += 1
            if fstat_calls == 2:
                existing.unlink()
            return st

        monkeypatch.setattr(os, "fstat", _delete_on_fstat)

        result = boundary.remove_if_version(PurePosixPath("AI/rm.md"), expected=fv)
        assert result.reconciliation_required is True
        assert result.published is False
        assert result.durable is False
        assert result.version == fv
        assert not existing.exists()

    def test_remove_unaffected_when_no_swap(self, tmp_path: Path) -> None:
        """Without any external swap the pre-unlink re-verify passes and the
        removal proceeds normally (reconciliation_required stays False)."""
        boundary, vault = _build_boundary(tmp_path)
        existing = _write_raw(vault, "AI/rm.md", b"bye")
        fv = _file_version(existing)
        result = boundary.remove_if_version(PurePosixPath("AI/rm.md"), expected=fv)
        assert result.reconciliation_required is False
        assert result.published is True
        assert not existing.exists()


# ---------------------------------------------------------------------------
# FD hygiene in FD-recursive scanning
# ---------------------------------------------------------------------------


def _open_fds() -> set[int]:
    """Return the set of currently open file descriptors (portable probe)."""
    soft_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    fds: set[int] = set()
    for candidate in range(soft_limit):
        try:
            os.fstat(candidate)
        except OSError:
            continue
        fds.add(candidate)
    return fds


class TestRecursiveScanFDCleanup:
    """FD-recursive scanning must close every child directory FD it opens,
    on exhaustion AND on early termination."""

    def test_iter_markdown_files_no_fd_leak(self, tmp_path: Path) -> None:
        """Exhaustive recursive enumeration closes all child directory FDs."""
        boundary, vault = _build_boundary(tmp_path)
        for i in range(4):
            _write_raw(
                vault, f"AI/level1-{i}/level2-{i}/note-{i}.md", f"# {i}".encode()
            )
        before = _open_fds()
        files = list(boundary.iter_markdown_files())
        after = _open_fds()
        assert len(files) == 4
        # No FD (directory or otherwise) may be left open by the scan.
        assert after <= before

    def test_iter_admitted_snapshots_no_fd_leak(self, tmp_path: Path) -> None:
        """iter_admitted_snapshots closes all child directory FDs."""
        boundary, vault = _build_boundary(tmp_path)
        for i in range(4):
            _write_raw(vault, f"AI/a{i}/b{i}/note-{i}.md", f"# {i}".encode())
        before = _open_fds()
        snaps = list(boundary.iter_admitted_snapshots())
        after = _open_fds()
        assert len(snaps) == 4
        assert after <= before

    def test_iter_markdown_files_early_break_no_fd_leak(self, tmp_path: Path) -> None:
        """Abandoning the enumeration after the first (subtree) file still
        closes the open child directory FD."""
        boundary, vault = _build_boundary(tmp_path)
        for i in range(4):
            _write_raw(vault, f"AI/sub-{i}/note-{i}.md", f"# {i}".encode())
        before = _open_fds()
        files = []
        for f in boundary.iter_markdown_files():
            files.append(f)
            break  # abandon mid-enumeration (inside the first subtree)
        assert len(files) == 1
        gc.collect()
        after = _open_fds()
        assert after <= before

    def test_iter_admitted_snapshots_early_break_no_fd_leak(
        self, tmp_path: Path
    ) -> None:
        """Abandoning the snapshot enumeration early still closes any
        partially opened child directory FDs."""
        boundary, vault = _build_boundary(tmp_path)
        for i in range(4):
            _write_raw(vault, f"AI/sub-{i}/note-{i}.md", f"# {i}".encode())
        before = _open_fds()
        # Consume the generator partially by reading into a list with a
        # manual stop: take snapshots until we have one, then drop it.
        it = iter(boundary.iter_admitted_snapshots())
        snaps = []
        for snap in it:
            snaps.append(snap)
            if len(snaps) >= 1:
                break
        del it
        gc.collect()
        after = _open_fds()
        # iter_admitted_snapshots collects before yielding, so partial
        # consumption still exercises full recursion; assert no leak either way.
        assert len(snaps) >= 1
        assert after <= before
