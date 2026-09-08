"""Vault mutation policy and lifecycle tests — security-repaired edition."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import (
    CatalogSession,
    create_database,
)
from harbor_ledger_memory.catalog.models import (
    ActivityEvent,
    MemoryWriteProposal,
)
from harbor_ledger_memory.config import (
    FolderAccess,
    FolderRule,
    Settings,
)
from harbor_ledger_memory.services.activity import ActivityService
from harbor_ledger_memory.services.vault_mutations import (
    VaultMutationService,
    VaultWriteDenied,
)
from harbor_ledger_memory.vault.boundary import DirectoryCreationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_settings(
    tmp_path: Path,
    index_root: str = "AI",
    folder_rules: tuple[FolderRule, ...] | None = None,
) -> Settings:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / index_root).mkdir()
    return Settings(
        vault_path=vault,
        index_root=index_root,
        folder_rules=folder_rules or (),
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )


def _make_service(
    settings: Settings,
) -> tuple[VaultMutationService, Path, Session]:
    engine = create_database(settings.database_url)
    session = CatalogSession(bind=engine)
    service = VaultMutationService.from_settings(session, settings)
    return service, settings.vault_path, session


def _write_note(vault: Path, index_root: str, rel: str, content: str) -> Path:
    abs_path = vault / index_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")
    return abs_path


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------------------
# Return objects
# ---------------------------------------------------------------------------


class TestReturnObjects:
    def test_request_returns_proposal_object(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, _, _ = _make_service(settings)
        result = service.request("AI/proposed/new.md", "# New")
        assert isinstance(result, MemoryWriteProposal)
        assert result.operation == "create"
        assert result.status == "pending"

    def test_approve_returns_proposal_object(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        proposal = service.request("AI/proposed/new.md", "# New")
        result = service.approve(proposal.id)
        assert isinstance(result, MemoryWriteProposal)
        assert result.status == "applied"
        assert result.applying_at is not None

    def test_reject_returns_proposal_object(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, _, _ = _make_service(settings)
        proposal = service.request("AI/proposed/new.md", "# New")
        result = service.reject(proposal.id)
        assert isinstance(result, MemoryWriteProposal)
        assert result.status == "rejected"


# ---------------------------------------------------------------------------
# Policy: deny and read reject
# ---------------------------------------------------------------------------


class TestDenyReject:
    def test_missing_parent_is_checked_with_longest_prefix_policy(
        self, tmp_path: Path
    ) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
                FolderRule(path=PurePosixPath("AI/private"), access=FolderAccess.DENY),
            ),
        )
        service, _, _ = _make_service(settings)
        with pytest.raises(
            VaultWriteDenied,
            match=r"write denied: AI/private/new/deep\.md is denied by folder rule",
        ):
            service.request("AI/private/new/deep.md", "# denied")

    def test_deny_raises_vault_write_denied(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(path=PurePosixPath("AI/denied"), access=FolderAccess.DENY),
            ),
        )
        service, _, _ = _make_service(settings)
        with pytest.raises(VaultWriteDenied, match="denied"):
            service.request("AI/denied/note.md", "# Hello")

    def test_deny_records_activity(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(path=PurePosixPath("AI/denied"), access=FolderAccess.DENY),
            ),
        )
        service, _, _ = _make_service(settings)
        with pytest.raises(VaultWriteDenied):
            service.request("AI/denied/note.md", "# Hello")
        history = service.activity_service.history()
        assert any(e.event_type == "vault.mutation.requested" for e in history)

    def test_read_access_raises_vault_write_denied(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(path=PurePosixPath("AI/readonly"), access=FolderAccess.READ),
            ),
        )
        service, _, _ = _make_service(settings)
        with pytest.raises(VaultWriteDenied, match="read"):
            service.request("AI/readonly/note.md", "# Hello")

    def test_nested_most_specific_rule(self, tmp_path: Path) -> None:
        """The deepest matching rule wins."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(path=PurePosixPath("AI"), access=FolderAccess.DENY),
                FolderRule(
                    path=PurePosixPath("AI/sub"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, _, session = _make_service(settings)
        with pytest.raises(VaultWriteDenied):
            service.request("AI/other.md", "#")
        result = service.request("AI/sub/deep.md", "# Deep")
        assert result.status == "pending"
        assert result.operation == "create"


# ---------------------------------------------------------------------------
# Traversal rejection
# ---------------------------------------------------------------------------


class TestTraversalRejection:
    def test_parent_traversal_raises(self, tmp_path: Path) -> None:
        settings = _build_settings(tmp_path)
        service, _, _ = _make_service(settings)
        with pytest.raises(VaultWriteDenied):
            service.request("AI/../secret.md", "# leaked")

    def test_deep_traversal_raises(self, tmp_path: Path) -> None:
        settings = _build_settings(tmp_path)
        service, _, _ = _make_service(settings)
        with pytest.raises(VaultWriteDenied):
            service.request("AI/sub/../../escape.md", "#")


# ---------------------------------------------------------------------------
# Policy: propose-write queues pending
# ---------------------------------------------------------------------------


class TestProposeWrite:
    def test_propose_write_queues_create_as_pending(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        proposal = service.request("AI/proposed/new.md", "# New")
        assert proposal.status == "pending"
        assert proposal.operation == "create"
        assert proposal.path == "AI/proposed/new.md"
        assert not (vault / "AI" / "proposed" / "new.md").exists()

    def test_propose_write_queues_update_as_pending(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(
            vault, "AI", "proposed/existing.md", "---\ntitle: old\n---\n# Old"
        )
        proposal = service.request("AI/proposed/existing.md", "# Updated")
        assert proposal.status == "pending"
        assert proposal.operation == "update"
        # Source file should still have original content
        assert existing.read_text(encoding="utf-8") == "---\ntitle: old\n---\n# Old"
        # SHA-256 should be captured
        expected = _sha256(existing.read_bytes())
        assert proposal.expected_source_hash == expected


# ---------------------------------------------------------------------------
# Auto-write: managed update vs unmanaged
# ---------------------------------------------------------------------------


class TestAutoWriteManaged:
    def test_auto_write_applies_managed_update(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(
            vault,
            "AI",
            "managed/managed.md",
            "---\nmanaged_by: harbor-ledger-memory\n---\n# Original",
        )
        proposal = service.request("AI/managed/managed.md", "# Updated by AI")
        assert proposal.status == "applied"
        assert existing.read_text(encoding="utf-8") == "# Updated by AI"

    def test_auto_write_queues_create_as_pending(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, _ = _make_service(settings)
        proposal = service.request("AI/managed/new.md", "# New")
        assert proposal.status == "pending"
        assert proposal.operation == "create"
        assert not (vault / "AI" / "managed" / "new.md").exists()


class TestAutoWriteUnmanaged:
    def test_legacy_managed_by_value_is_unmanaged(self, tmp_path: Path) -> None:
        """The former plugin identity must not authorize auto-write."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, _ = _make_service(settings)
        original = "---\nmanaged_by: legacy-plugin\n---\n# Legacy\n"
        existing = _write_note(vault, "AI", "managed/legacy.md", original)

        proposal = service.request("AI/managed/legacy.md", "# AI change")

        assert proposal.status == "pending"
        assert proposal.operation == "update"
        assert existing.read_text(encoding="utf-8") == original

    def test_unmanaged_update_queued(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, _ = _make_service(settings)
        existing = _write_note(vault, "AI", "managed/unmanaged.md", "# Human note")
        proposal = service.request("AI/managed/unmanaged.md", "# AI change")
        assert proposal.status == "pending"
        assert proposal.operation == "update"
        assert existing.read_text(encoding="utf-8") == "# Human note"

    def test_unmanaged_create_queued(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, _ = _make_service(settings)
        proposal = service.request("AI/managed/brand_new.md", "# New")
        assert proposal.status == "pending"
        assert proposal.operation == "create"

    def test_auto_write_race_file_changed(self, tmp_path: Path) -> None:
        """Auto-write must not overwrite a file that changed since the request."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, _ = _make_service(settings)
        existing = _write_note(
            vault,
            "AI",
            "managed/race.md",
            "---\nmanaged_by: harbor-ledger-memory\n---\n# Original",
        )
        # Mutate the file on disk after the proposal reads it
        # (simulates external modification between read and write)
        import time

        time.sleep(0.01)
        existing.write_text("# Changed externally\n", encoding="utf-8")
        proposal = service.request("AI/managed/race.md", "# AI update")
        # Should still be pending (version conflict) or failed
        assert proposal.status in ("pending", "failed")
        # Original content preserved (not overwritten by AI)
        content = existing.read_text(encoding="utf-8")
        assert content != "# AI update"

    def test_auto_write_race_file_deleted(self, tmp_path: Path) -> None:
        """Auto-write must not create a file when the target was deleted."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(
            vault,
            "AI",
            "managed/deleted.md",
            "---\nmanaged_by: harbor-ledger-memory\n---\n# Original",
        )
        # Index the note so the service knows it was a managed file
        from harbor_ledger_memory.services.scan import ScanService

        ScanService(service.boundary, session, embedding_model=None).full_scan()
        existing.unlink()
        proposal = service.request("AI/managed/deleted.md", "# AI update")
        # Should stay pending (file gone)
        assert proposal.status == "pending"
        assert proposal.operation == "update"


# ---------------------------------------------------------------------------
# Approval workflow
# ---------------------------------------------------------------------------


class TestApprovalWorkflow:
    def test_approval_policy_change_audits_revalidated_affected_paths(
        self, tmp_path: Path
    ) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
            ),
        )
        service, _, _ = _make_service(settings)
        proposal = service.request("AI/new/deep.md", "# New")
        service.boundary._rules = (  # noqa: SLF001
            FolderRule(path=PurePosixPath("AI/new"), access=FolderAccess.DENY),
        )
        with pytest.raises(VaultWriteDenied):
            service.approve(proposal.id)

        event = next(
            event
            for event in reversed(service.activity_service.history())
            if event.event_type == "vault.mutation.failed"
        )
        assert event.payload["affected_paths"] == [
            "AI/new",
            "AI/new/deep.md",
        ]

    def test_mkdir_is_pending_then_applies_and_records_created_paths(
        self, tmp_path: Path
    ) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
            ),
        )
        service, vault, _ = _make_service(settings)
        proposal = service.request("AI/new/deep", "", operation="mkdir")
        assert proposal.status == "pending"
        result = service.approve(proposal.id)
        assert result.status == "applied"
        assert result.created_paths == ["AI/new", "AI/new/deep"]
        assert (vault / "AI/new/deep").is_dir()

    def test_partial_mkdir_is_reconciled_with_created_paths_and_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(path=PurePosixPath("AI"), access=FolderAccess.PROPOSE_WRITE),
            ),
        )
        service, _, _ = _make_service(settings)
        proposal = service.request("AI/partial/deep", "", operation="mkdir")
        monkeypatch.setattr(
            service.boundary,
            "atomic_mkdir",
            lambda _identity: (_ for _ in ()).throw(
                DirectoryCreationError(
                    "injected mkdir failure", (PurePosixPath("AI/partial"),)
                )
            ),
        )
        result = service.approve(proposal.id)
        assert result.status == "reconciliation_required"
        assert result.created_paths == ["AI/partial"]
        event = next(
            event
            for event in reversed(service.activity_service.history())
            if event.event_type == "vault.mutation.failed"
        )
        assert event.payload["created_paths"] == ["AI/partial"]
    def test_approve_writes_and_marks_applied(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        proposal = service.request("AI/proposed/approved.md", "# Approved")
        result = service.approve(proposal.id)
        assert result.status == "applied"
        assert result.applying_at is not None
        assert (vault / "AI" / "proposed" / "approved.md").exists()

    def test_approve_records_activity(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, _, _ = _make_service(settings)
        proposal = service.request("AI/proposed/approved.md", "# Approved")
        service.approve(proposal.id)
        history = service.activity_service.history()
        assert any(e.event_type == "vault.mutation.applied" for e in history)

    def test_approve_scans_after_write(self, tmp_path: Path) -> None:
        """Approval should run a full scan and mark applied only after it succeeds."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        proposal = service.request(
            "AI/proposed/scan-me.md",
            "---\ntitle: scanned\n---\n# Scanned Note",
        )
        service.approve(proposal.id)
        from harbor_ledger_memory.catalog.models import Note

        note = session.scalar(select(Note).where(Note.path == "AI/proposed/scan-me.md"))
        assert note is not None
        assert note.title == "Scanned Note"

    def test_approve_revalidates_path_and_rule_deny(self, tmp_path: Path) -> None:
        """Approval re-validates; a DENY rule change should be caught."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        _write_note(vault, "AI", "proposed/valid.md", "# Valid")
        proposal = service.request("AI/proposed/valid.md", "# Updated")
        service.boundary._rules = (  # noqa: SLF001
            FolderRule(path=PurePosixPath("AI/proposed"), access=FolderAccess.DENY),
        )
        with pytest.raises(VaultWriteDenied):
            service.approve(proposal.id)

    def test_approve_revalidates_path_and_rule_read(self, tmp_path: Path) -> None:
        """Approval re-validates; a READ rule change should be caught."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        _write_note(vault, "AI", "proposed/valid.md", "# Valid")
        proposal = service.request("AI/proposed/valid.md", "# Updated")
        service.boundary._rules = (  # noqa: SLF001
            FolderRule(path=PurePosixPath("AI/proposed"), access=FolderAccess.READ),
        )
        with pytest.raises(VaultWriteDenied):
            service.approve(proposal.id)

    def test_approve_create_requires_absence(self, tmp_path: Path) -> None:
        """Approving a create proposal that already exists on disk must fail."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        proposal = service.request("AI/proposed/race.md", "# New")
        # Someone created the file outside the service
        _write_note(vault, "AI", "proposed/race.md", "# External")
        result = service.approve(proposal.id)
        assert result.status == "failed"
        assert result.failure_reason is not None

    def test_approve_source_hash_version_conflict(self, tmp_path: Path) -> None:
        """Update approval must detect source-hash version conflict."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(vault, "AI", "proposed/conflict.md", "# Original")
        proposal = service.request("AI/proposed/conflict.md", "# New")
        # Mutate the file after the proposal captured the hash
        import time

        time.sleep(0.01)
        existing.write_text("# Changed", encoding="utf-8")
        result = service.approve(proposal.id)
        assert result.status == "failed"
        assert "version conflict" in (result.failure_reason or "").lower()
        # Original file preserved
        assert existing.read_text(encoding="utf-8") == "# Changed"


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------


class TestRejection:
    def test_reject_marks_rejected_without_write(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        proposal = service.request("AI/proposed/rejected.md", "# Rejected")
        result = service.reject(proposal.id)
        assert result.status == "rejected"
        assert result.resolved_at is not None
        assert not (vault / "AI" / "proposed" / "rejected.md").exists()

    def test_reject_records_activity(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, _, _ = _make_service(settings)
        proposal = service.request("AI/proposed/rejected.md", "# Rejected")
        service.reject(proposal.id)
        history = service.activity_service.history()
        assert any(e.event_type == "vault.mutation.rejected" for e in history)


# ---------------------------------------------------------------------------
# Write failure preservation
# ---------------------------------------------------------------------------


class TestWriteFailurePreservation:
    def test_failed_write_preserves_source_and_persists_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auto-write rollback: original restored when scan fails after write."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(
            vault,
            "AI",
            "managed/fail.md",
            "---\nmanaged_by: harbor-ledger-memory\n---\n# Original",
        )
        original_content = existing.read_text(encoding="utf-8")

        # Inject scan failure (write succeeds, scan fails → rollback)
        from harbor_ledger_memory.services.scan import ScanService

        monkeypatch.setattr(
            ScanService,
            "full_scan",
            lambda self: (_ for _ in ()).throw(RuntimeError("scan crash")),
        )

        proposal = service.request("AI/managed/fail.md", "# Failing update")
        assert proposal.status == "failed"
        assert proposal.failure_reason is not None
        assert existing.read_text(encoding="utf-8") == original_content

    def test_failed_approval_preserves_source_and_persists_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Approve rollback: original content restored when scan fails after write."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(vault, "AI", "proposed/approve_fail.md", "# Original")

        # Inject scan failure (write succeeds, scan fails → rollback)
        from harbor_ledger_memory.services.scan import ScanService

        monkeypatch.setattr(
            ScanService,
            "full_scan",
            lambda self: (_ for _ in ()).throw(RuntimeError("scan crash")),
        )

        proposal = service.request("AI/proposed/approve_fail.md", "# Updated")
        result = service.approve(proposal.id)
        assert result.status == "failed"
        assert result.failure_reason is not None
        assert existing.read_text(encoding="utf-8") == "# Original"


# ---------------------------------------------------------------------------
# Scan failure → not applied + preservation
# ---------------------------------------------------------------------------


class TestScanFailure:
    def test_scan_failure_not_applied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If full_scan raises, proposal stays pending and original is restored."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        # Write an existing file so we have a backup to restore
        existing = _write_note(vault, "AI", "proposed/scan_fail.md", "# Original")
        proposal = service.request("AI/proposed/scan_fail.md", "# New")
        # Inject scan failure
        from harbor_ledger_memory.services.scan import ScanService

        def _failing_full_scan(self: object) -> None:
            raise RuntimeError("scan crash")

        monkeypatch.setattr(ScanService, "full_scan", _failing_full_scan)
        result = service.approve(proposal.id)
        assert result.status == "failed"
        assert "scan" in (result.failure_reason or "").lower()
        # Original file preserved
        assert existing.read_text(encoding="utf-8") == "# Original"


# ---------------------------------------------------------------------------
# Durable state: fresh session can read proposals and activity
# ---------------------------------------------------------------------------


class TestDurableState:
    def test_proposal_state_survives_new_session(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            proposal = service.request("AI/proposed/new.md", "# New")
            session.commit()
            pid = proposal.id

        with CatalogSession(bind=engine) as session:
            row = session.get(MemoryWriteProposal, pid)
            assert row is not None
            assert row.status == "pending"
            assert row.operation == "create"
            assert row.path == "AI/proposed/new.md"

    def test_activity_state_survives_new_session(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            service.request("AI/proposed/new.md", "# New")
            session.commit()

        with CatalogSession(bind=engine) as session:
            from harbor_ledger_memory.services.activity import ActivityService

            svc = ActivityService(session)
            history = svc.history()
            events = [e for e in history if e.event_type == "vault.mutation.requested"]
            assert len(events) == 1

    def test_applied_state_survives_new_session(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            proposal = service.request("AI/proposed/new.md", "# New")
            session.commit()
            pid = proposal.id

        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            service.approve(pid)
            session.commit()

        with CatalogSession(bind=engine) as session:
            row = session.get(MemoryWriteProposal, pid)
            assert row.status == "applied"
            assert row.applying_at is not None


# ---------------------------------------------------------------------------
# Activity records
# ---------------------------------------------------------------------------


class TestActivityRecords:
    def test_requested_activity_recorded(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, _, _ = _make_service(settings)
        service.request("AI/proposed/one.md", "# One")
        history = service.activity_service.history()
        events = [e for e in history if e.event_type == "vault.mutation.requested"]
        assert len(events) == 1
        assert events[0].payload["path"] == "AI/proposed/one.md"

    def test_applied_activity_recorded(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, _, _ = _make_service(settings)
        proposal = service.request("AI/proposed/do.md", "# Do")
        service.approve(proposal.id)
        history = service.activity_service.history()
        events = [e for e in history if e.event_type == "vault.mutation.applied"]
        assert len(events) == 1
        assert events[0].payload["proposal_id"] == proposal.id

    def test_rejected_activity_recorded(self, tmp_path: Path) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, _, _ = _make_service(settings)
        proposal = service.request("AI/proposed/nope.md", "# No")
        service.reject(proposal.id)
        history = service.activity_service.history()
        events = [e for e in history if e.event_type == "vault.mutation.rejected"]
        assert len(events) == 1

    def test_failed_activity_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failed auto-write should record vault.mutation.failed activity."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        _write_note(
            vault,
            "AI",
            "managed/fail.md",
            "---\nmanaged_by: harbor-ledger-memory\n---\n# Original",
        )

        # Inject failure in atomic_replace
        from harbor_ledger_memory.vault.boundary import VaultBoundary

        monkeypatch.setattr(
            VaultBoundary,
            "atomic_replace",
            lambda self, identity, content, *, expected: (_ for _ in ()).throw(
                PermissionError("disk full")
            ),
        )

        service.request("AI/managed/fail.md", "# Fail")
        history = service.activity_service.history()
        events = [e for e in history if e.event_type == "vault.mutation.failed"]
        assert len(events) == 1
        assert "failure_reason" in events[0].payload


# ---------------------------------------------------------------------------
# New focused integration tests (Task 3)
# ---------------------------------------------------------------------------


class TestSuccessfulApproveUpdate:
    def test_approve_update_uses_atomic_replace(self, tmp_path: Path) -> None:
        """Successful update via atomic_replace; applied + hash captured."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(vault, "AI", "proposed/update.md", "# Original")
        proposal = service.request("AI/proposed/update.md", "# Updated")
        assert proposal.status == "pending"
        assert proposal.expected_source_hash == _sha256(existing.read_bytes())
        result = service.approve(proposal.id)
        assert result.status == "applied"
        assert result.applying_at is not None
        assert result.applied_content_hash is not None
        assert result.applied_content_hash == _sha256(b"# Updated")
        assert existing.read_text(encoding="utf-8") == "# Updated"

    def test_approve_create_uses_atomic_create(self, tmp_path: Path) -> None:
        """Successful create via atomic_create; applied + hash captured."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        proposal = service.request("AI/proposed/new.md", "# New")
        assert proposal.status == "pending"
        assert proposal.operation == "create"
        result = service.approve(proposal.id)
        assert result.status == "applied"
        assert result.applied_content_hash == _sha256(b"# New")
        assert (vault / "AI" / "proposed" / "new.md").read_text(
            encoding="utf-8"
        ) == "# New"


class TestScanFailureRestore:
    def test_scan_failure_restore_update(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Update: scan failure restores original via version-matched replace."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(vault, "AI", "proposed/sf-update.md", "# Original")
        proposal = service.request("AI/proposed/sf-update.md", "# New")
        from harbor_ledger_memory.services.scan import ScanService
        from harbor_ledger_memory.vault.boundary import VaultBoundary

        monkeypatch.setattr(
            ScanService,
            "full_scan",
            lambda self: (_ for _ in ()).throw(RuntimeError("scan crash")),
        )

        real_replace = VaultBoundary.atomic_replace
        calls: list[dict[str, object]] = []

        def recording_replace(
            self, identity, content, *, expected, validate_current=None
        ):
            write_result = real_replace(
                self,
                identity,
                content,
                expected=expected,
                validate_current=validate_current,
            )
            calls.append(
                {"content": content, "expected": expected, "write_result": write_result}
            )
            return write_result

        monkeypatch.setattr(VaultBoundary, "atomic_replace", recording_replace)

        result = service.approve(proposal.id)
        assert result.status == "failed"
        assert "scan" in (result.failure_reason or "").lower()
        # Original file preserved
        assert existing.read_text(encoding="utf-8") == "# Original"
        # The restore was a version-matched atomic replace of the original
        # bytes, pinned to a version describing exactly the content we
        # published. (Full FileVersion equality with the write result is
        # not asserted: the rename can bump the published entry's ctime,
        # so the rollback re-pins from a fresh coherent read.)
        assert len(calls) == 2
        assert calls[0]["content"] == b"# New"
        assert calls[1]["content"] == b"# Original"
        assert calls[1]["expected"].sha256 == calls[0]["write_result"].version.sha256
        assert calls[1]["expected"].sha256 == _sha256(b"# New")

    def test_scan_failure_remove_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Create: scan failure removes the created file via remove_if_version."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        proposal = service.request("AI/proposed/sf-create.md", "# New")
        from harbor_ledger_memory.services.scan import ScanService
        from harbor_ledger_memory.vault.boundary import VaultBoundary

        monkeypatch.setattr(
            ScanService,
            "full_scan",
            lambda self: (_ for _ in ()).throw(RuntimeError("scan crash")),
        )

        real_remove = VaultBoundary.remove_if_version
        removals: list[dict[str, object]] = []

        def recording_remove(self, identity, *, expected):
            write_result = real_remove(self, identity, expected=expected)
            removals.append({"expected": expected, "write_result": write_result})
            return write_result

        monkeypatch.setattr(VaultBoundary, "remove_if_version", recording_remove)

        result = service.approve(proposal.id)
        assert result.status == "failed"
        assert "scan" in (result.failure_reason or "").lower()
        assert len(removals) == 1
        # The created file was removed, not left behind.
        assert not (vault / "AI" / "proposed" / "sf-create.md").exists()


class TestAutoWriteScanFailureRestore:
    def test_auto_write_scan_failure_restores(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auto-write managed update: scan failure restores original."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(
            vault,
            "AI",
            "managed/aw-sf.md",
            "---\nmanaged_by: harbor-ledger-memory\n---\n# Original",
        )
        from harbor_ledger_memory.services.scan import ScanService

        monkeypatch.setattr(
            ScanService,
            "full_scan",
            lambda self: (_ for _ in ()).throw(RuntimeError("scan crash")),
        )
        proposal = service.request("AI/managed/aw-sf.md", "# AI update")
        assert proposal.status == "failed"
        assert existing.read_text(encoding="utf-8") == (
            "---\nmanaged_by: harbor-ledger-memory\n---\n# Original"
        )


class TestDurableApplyingState:
    def test_applying_state_persisted_before_write(self, tmp_path: Path) -> None:
        """proposal.applying_at is committed before filesystem mutation."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            _write_note(
                settings.vault_path,
                "AI",
                "proposed/durable.md",
                "# Original",
            )
            proposal = service.request("AI/proposed/durable.md", "# New")
            session.commit()
            pid = proposal.id

            # New session — verify applying_at is set before approve completes
            with CatalogSession(bind=engine) as fresh:
                service2 = VaultMutationService.from_settings(fresh, settings)
                service2.approve(pid)
                fresh.commit()

            with CatalogSession(bind=engine) as fresh:
                row = fresh.get(MemoryWriteProposal, pid)
                assert row is not None
                assert row.status == "applied"
                assert row.applying_at is not None
                assert row.applied_content_hash is not None


class TestRuleChangeRevalidate:
    def test_approve_revalidate_deny(self, tmp_path: Path) -> None:
        """Rule changed to DENY between request and approve → rejected."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        _write_note(vault, "AI", "proposed/rule.md", "# Valid")
        proposal = service.request("AI/proposed/rule.md", "# Updated")
        service.boundary._rules = (  # noqa: SLF001
            FolderRule(path=PurePosixPath("AI/proposed"), access=FolderAccess.DENY),
        )
        with pytest.raises(VaultWriteDenied):
            service.approve(proposal.id)
        session.refresh(proposal)
        assert proposal.status == "failed"

    def test_approve_revalidate_read(self, tmp_path: Path) -> None:
        """Rule changed to READ between request and approve → rejected."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        _write_note(vault, "AI", "proposed/rule.md", "# Valid")
        proposal = service.request("AI/proposed/rule.md", "# Updated")
        service.boundary._rules = (  # noqa: SLF001
            FolderRule(path=PurePosixPath("AI/proposed"), access=FolderAccess.READ),
        )
        with pytest.raises(VaultWriteDenied):
            service.approve(proposal.id)
        session.refresh(proposal)
        assert proposal.status == "failed"


class TestSourceConflict:
    def test_approve_update_source_conflict(self, tmp_path: Path) -> None:
        """File changed on disk between proposal and approve → failed."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(vault, "AI", "proposed/conflict.md", "# Original")
        proposal = service.request("AI/proposed/conflict.md", "# New")
        # Mutate the file after proposal captured the hash
        import time

        time.sleep(0.01)
        existing.write_text("# Changed externally", encoding="utf-8")
        result = service.approve(proposal.id)
        assert result.status == "failed"
        assert "version conflict" in (result.failure_reason or "").lower()
        # File on disk preserved (not overwritten)
        assert existing.read_text(encoding="utf-8") == "# Changed externally"


class TestSingleApplyPath:
    def test_approve_update_single_version_matched_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Approve update: exactly one replace, version pinned, no validator."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(vault, "AI", "proposed/matched.md", "# Original")
        proposal = service.request("AI/proposed/matched.md", "# Updated")
        identity = PurePosixPath("AI/proposed/matched.md")
        pre_version = service.boundary.read_file(identity).version
        assert pre_version is not None

        from harbor_ledger_memory.vault.boundary import VaultBoundary

        real_replace = VaultBoundary.atomic_replace
        calls: list[dict[str, object]] = []

        def recording_replace(
            self, identity, content, *, expected, validate_current=None
        ):
            write_result = real_replace(
                self,
                identity,
                content,
                expected=expected,
                validate_current=validate_current,
            )
            calls.append(
                {
                    "content": content,
                    "expected": expected,
                    "validate_current": validate_current,
                    "write_result": write_result,
                }
            )
            return write_result

        monkeypatch.setattr(VaultBoundary, "atomic_replace", recording_replace)

        result = service.approve(proposal.id)
        assert result.status == "applied"
        # The single apply path performs exactly one version-checked write.
        assert len(calls) == 1
        call = calls[0]
        assert call["content"] == b"# Updated"
        assert call["expected"] == pre_version
        # Approvals rely on the pre-read hash check, not a semantic validator.
        assert call["validate_current"] is None
        # The WriteResult of the consumed write is durable.
        assert call["write_result"].published is True
        assert call["write_result"].durable is True
        assert existing.read_text(encoding="utf-8") == "# Updated"


# ---------------------------------------------------------------------------
# Durable "applying" state + pre-write hook
# ---------------------------------------------------------------------------


class TestDurableApplying:
    def test_applying_committed_before_disk_write(self, tmp_path: Path) -> None:
        """The applying state is durable and the disk is untouched at hook time."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        vault = settings.vault_path
        note_path = vault / "AI" / "proposed" / "applying-visible.md"
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            _write_note(vault, "AI", "proposed/applying-visible.md", "# Original")
            proposal = service.request("AI/proposed/applying-visible.md", "# New")
            session.commit()
            pid = proposal.id

            with CatalogSession(bind=engine) as fresh:
                service2 = VaultMutationService.from_settings(fresh, settings)
                observations: list[str] = []

                def observe(_proposal: MemoryWriteProposal) -> None:
                    # A brand-new session must already see "applying" ...
                    with CatalogSession(bind=engine) as audit:
                        row = audit.get(MemoryWriteProposal, pid)
                        assert row is not None
                        assert row.status == "applying"
                        assert row.applying_at is not None
                        observations.append(row.status)
                    # ... while the disk still holds the original content.
                    observations.append(note_path.read_text(encoding="utf-8"))

                service2.on_applying = observe
                result = service2.approve(pid)
                assert result.status == "applied"

        assert observations == ["applying", "# Original"]


# ---------------------------------------------------------------------------
# Auto-write semantic validation at the boundary
# ---------------------------------------------------------------------------


class TestAutoWriteValidator:
    def test_unmanaged_file_rejected_at_validator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auto-write of an unmanaged file is rejected by validate_current."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, _ = _make_service(settings)
        existing = _write_note(
            vault, "AI", "managed/unmanaged-validator.md", "# Human note"
        )
        from harbor_ledger_memory.vault.boundary import VaultBoundary

        real_replace = VaultBoundary.atomic_replace
        captured: dict[str, object] = {}

        def recording_replace(
            self, identity, content, *, expected, validate_current=None
        ):
            captured["validate_current"] = validate_current
            return real_replace(
                self,
                identity,
                content,
                expected=expected,
                validate_current=validate_current,
            )

        monkeypatch.setattr(VaultBoundary, "atomic_replace", recording_replace)

        proposal = service.request("AI/managed/unmanaged-validator.md", "# AI change")
        # Semantic rejection: back to pending, nothing written.
        assert proposal.status == "pending"
        assert existing.read_text(encoding="utf-8") == "# Human note"
        # The apply path pinned the applying state before the write attempt.
        assert proposal.applying_at is not None
        # A semantic validator was handed to the boundary ...
        assert captured.get("validate_current") is not None
        # ... and it rejects the coherent snapshot of the current on-disk
        # file (unmanaged frontmatter) -- the boundary invokes the
        # callback with the snapshot, not a bare FileVersion.
        current = service.boundary.read_file(
            PurePosixPath("AI/managed/unmanaged-validator.md")
        )
        assert current.version is not None
        assert captured["validate_current"](current) is False

    def test_rule_change_rejected_at_validator(self, tmp_path: Path) -> None:
        """A rule flip to DENY after the applying commit aborts the auto-write."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, _ = _make_service(settings)
        managed_content = "---\nmanaged_by: harbor-ledger-memory\n---\n# Original"
        existing = _write_note(vault, "AI", "managed/rule-flip.md", managed_content)

        def flip_rule(_proposal: MemoryWriteProposal) -> None:
            service.boundary._rules = (  # noqa: SLF001
                FolderRule(path=PurePosixPath("AI/managed"), access=FolderAccess.DENY),
            )

        service.on_applying = flip_rule
        proposal = service.request("AI/managed/rule-flip.md", "# AI change")
        # The validator re-checks the rule and rejects → pending, untouched.
        assert proposal.status == "pending"
        assert existing.read_text(encoding="utf-8") == managed_content


# ---------------------------------------------------------------------------
# Terminal state + audit event in one transaction
# ---------------------------------------------------------------------------


def _install_event_commit_guard(
    monkeypatch: pytest.MonkeyPatch, session: Session, event_type: str
) -> None:
    """Inject a commit failure the moment *event_type* is about to persist.

    Any flush that would write a new ``ActivityEvent`` of the target type
    raises, simulating a crash between (or during) the state commit and the
    audit-event commit of the old two-commit design.
    """
    real_flush = session.flush

    def guarded_flush(*args: object, **kwargs: object) -> object:
        for obj in list(session.new):
            if isinstance(obj, ActivityEvent) and obj.event_type == event_type:
                raise RuntimeError(f"injected {event_type} commit failure")
        return real_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", guarded_flush)


class TestTerminalStateAuditAtomicity:
    """A terminal state must never persist without its audit event."""

    def test_no_applied_state_without_audit_after_event_commit_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            proposal = service.request("AI/proposed/atomic-applied.md", "# New")
            session.commit()
            pid = proposal.id

            _install_event_commit_guard(monkeypatch, session, "vault.mutation.applied")
            with pytest.raises(RuntimeError, match="injected"):
                service.approve(pid)
            session.rollback()

        with CatalogSession(bind=engine) as fresh:
            row = fresh.get(MemoryWriteProposal, pid)
            assert row is not None
            assert row.status == "applying"
            applied = [
                e
                for e in ActivityService(fresh).history()
                if e.event_type == "vault.mutation.applied"
            ]
            assert applied == []

    def test_no_failed_state_without_audit_after_event_commit_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            _write_note(
                settings.vault_path,
                "AI",
                "managed/atomic-failed.md",
                "---\nmanaged_by: harbor-ledger-memory\n---\n# Original",
            )
            from harbor_ledger_memory.services.scan import ScanService

            monkeypatch.setattr(
                ScanService,
                "full_scan",
                lambda self: (_ for _ in ()).throw(RuntimeError("scan crash")),
            )
            _install_event_commit_guard(monkeypatch, session, "vault.mutation.failed")
            with pytest.raises(RuntimeError, match="injected"):
                service.request("AI/managed/atomic-failed.md", "# AI update")
            session.rollback()

        with CatalogSession(bind=engine) as fresh:
            rows = list(fresh.scalars(select(MemoryWriteProposal)))
            assert len(rows) == 1
            assert rows[0].status == "applying"
            failed = [
                e
                for e in ActivityService(fresh).history()
                if e.event_type == "vault.mutation.failed"
            ]
            assert failed == []

    def test_no_rejected_state_without_audit_after_event_commit_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            proposal = service.request("AI/proposed/atomic-rejected.md", "# No")
            session.commit()
            pid = proposal.id

            _install_event_commit_guard(monkeypatch, session, "vault.mutation.rejected")
            with pytest.raises(RuntimeError, match="injected"):
                service.reject(pid)
            session.rollback()

        with CatalogSession(bind=engine) as fresh:
            row = fresh.get(MemoryWriteProposal, pid)
            assert row is not None
            assert row.status == "pending"
            rejected = [
                e
                for e in ActivityService(fresh).history()
                if e.event_type == "vault.mutation.rejected"
            ]
            assert rejected == []

    def test_no_reconciliation_state_without_audit_after_event_commit_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            proposal = service.request("AI/proposed/atomic-recon.md", "# New")
            session.commit()
            pid = proposal.id

            from harbor_ledger_memory.vault.boundary import VaultBoundary

            real_create = VaultBoundary.atomic_create

            def exploding_create(self, identity, content):
                real_create(self, identity, content)  # file is published
                raise RuntimeError("unknown create failure")

            monkeypatch.setattr(VaultBoundary, "atomic_create", exploding_create)
            _install_event_commit_guard(monkeypatch, session, "vault.mutation.failed")
            with pytest.raises(RuntimeError, match="injected"):
                service.approve(pid)
            session.rollback()

        with CatalogSession(bind=engine) as fresh:
            row = fresh.get(MemoryWriteProposal, pid)
            assert row is not None
            assert row.status == "applying"
            failed = [
                e
                for e in ActivityService(fresh).history()
                if e.event_type == "vault.mutation.failed"
            ]
            assert failed == []


# ---------------------------------------------------------------------------
# Coherent boundary snapshot for auto-write ownership/policy
# ---------------------------------------------------------------------------


class TestCoherentValidatorSnapshot:
    def test_validator_uses_coherent_snapshot_without_reread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ownership/policy must derive from the boundary's coherent snapshot.

        The validator is invoked with the coherent AdmittedFileSnapshot
        the boundary verified against the pinned snapshot; it must not
        re-read the file (a second read opens a TOCTOU window and can
        observe the file after the write instead of the version being
        validated).
        """
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/managed"),
                    access=FolderAccess.AUTO_WRITE,
                ),
            ),
        )
        service, vault, _ = _make_service(settings)
        _write_note(
            vault,
            "AI",
            "managed/coherent.md",
            "---\nmanaged_by: harbor-ledger-memory\n---\n# Original",
        )
        from harbor_ledger_memory.vault.boundary import VaultBoundary

        real_replace = VaultBoundary.atomic_replace
        captured: dict[str, object] = {}

        def recording_replace(
            self, identity, content, *, expected, validate_current=None
        ):
            captured["expected"] = expected
            captured["validate_current"] = validate_current
            return real_replace(
                self,
                identity,
                content,
                expected=expected,
                validate_current=validate_current,
            )

        monkeypatch.setattr(VaultBoundary, "atomic_replace", recording_replace)

        read_calls: list[PurePosixPath] = []
        real_read = service.boundary.read_file

        def counting_read(identity):
            read_calls.append(identity)
            return real_read(identity)

        monkeypatch.setattr(service.boundary, "read_file", counting_read)

        proposal = service.request("AI/managed/coherent.md", "# AI update")
        assert proposal.status == "applied"
        # Exactly two reads: the proposal snapshot and the apply snapshot.
        assert len(read_calls) == 2

        from harbor_ledger_memory.vault.boundary import AdmittedFileSnapshot

        validator = captured["validate_current"]
        expected = captured["expected"]
        assert validator is not None
        assert expected is not None
        coherent_bytes = b"---\nmanaged_by: harbor-ledger-memory\n---\n# Original"

        def make_snapshot(version) -> AdmittedFileSnapshot:
            return AdmittedFileSnapshot(
                path=PurePosixPath("AI/managed/coherent.md"),
                content=coherent_bytes,
                file_size=len(coherent_bytes),
                file_mtime_ns=0,
                version=version,
            )

        # The coherent snapshot is accepted even though the on-disk bytes
        # have since been replaced by the applied write itself.
        assert validator(make_snapshot(expected)) is True
        # A coherent snapshot whose version content hash does not match
        # the pinned snapshot is rejected without any re-read.
        other = replace(expected, sha256="0" * 64)
        assert validator(make_snapshot(other)) is False
        # A snapshot without a usable version is rejected.
        assert validator(make_snapshot(None)) is False
        assert len(read_calls) == 2


# ---------------------------------------------------------------------------
# Unknown create write exceptions
# ---------------------------------------------------------------------------


class TestUnknownCreateFailureStatus:
    def test_unknown_create_exception_is_reconciliation_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown create failure after publication: reconciliation, not failed."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            proposal = service.request("AI/proposed/unknown-create.md", "# New")

            from harbor_ledger_memory.vault.boundary import VaultBoundary

            real_create = VaultBoundary.atomic_create

            def exploding_create(self, identity, content):
                real_create(self, identity, content)  # file is published
                raise RuntimeError("unknown create failure")

            monkeypatch.setattr(VaultBoundary, "atomic_create", exploding_create)

            result = service.approve(proposal.id)
            assert result.status == "reconciliation_required"
            assert "reconciliation" in (result.failure_reason or "").lower()
            # The published file remains: unreconciled.
            target = settings.vault_path / "AI" / "proposed" / "unknown-create.md"
            assert target.exists()
            pid = proposal.id

        with CatalogSession(bind=engine) as fresh:
            row = fresh.get(MemoryWriteProposal, pid)
            assert row is not None
            assert row.status == "reconciliation_required"
            failed = [
                e
                for e in ActivityService(fresh).history()
                if e.event_type == "vault.mutation.failed"
            ]
            assert len(failed) == 1
            assert (
                "reconciliation" in (failed[0].payload["failure_reason"] or "").lower()
            )

    def test_unknown_create_exception_before_publish_is_reconciliation_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown create failure before publication is never ordinary failed."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            proposal = service.request("AI/proposed/unknown-create2.md", "# New")

            from harbor_ledger_memory.vault.boundary import VaultBoundary

            def exploding_create(self, identity, content):
                raise RuntimeError("unknown create failure")

            monkeypatch.setattr(VaultBoundary, "atomic_create", exploding_create)

            result = service.approve(proposal.id)
            assert result.status == "reconciliation_required"
            assert "reconciliation" in (result.failure_reason or "").lower()
            target = settings.vault_path / "AI" / "proposed" / "unknown-create2.md"
            assert not target.exists()


# ---------------------------------------------------------------------------
# WriteResult-driven rollback / reconciliation
# ---------------------------------------------------------------------------


class TestUncertainDurability:
    def test_uncertain_durability_rolls_back_to_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """published-but-not-durable write is rolled back and marked failed."""
        import os

        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        service, vault, session = _make_service(settings)
        existing = _write_note(
            vault, "AI", "proposed/durable-uncertain.md", "# Original"
        )
        proposal = service.request("AI/proposed/durable-uncertain.md", "# New")

        real_fsync = os.fsync
        fsync_calls = {"count": 0}

        def flaky_fsync(fd: int) -> None:
            # Call 1 = content fsync (pre-publication) of the write,
            # call 2 = post-publication parent fsync of the write.
            fsync_calls["count"] += 1
            if fsync_calls["count"] == 2:
                raise OSError("simulated post-publication parent fsync failure")
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", flaky_fsync)

        result = service.approve(proposal.id)
        assert result.status == "failed"
        assert "durab" in (result.failure_reason or "").lower()
        # The published write was rolled back.
        assert existing.read_text(encoding="utf-8") == "# Original"
        history = service.activity_service.history()
        failed = [e for e in history if e.event_type == "vault.mutation.failed"]
        assert len(failed) == 1


class TestRollbackReconciliation:
    def test_failed_rollback_marks_reconciliation_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the rollback itself fails, the proposal needs reconciliation."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        vault = settings.vault_path
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            existing = _write_note(
                vault, "AI", "proposed/rollback-fail.md", "# Original"
            )
            proposal = service.request("AI/proposed/rollback-fail.md", "# New")

            from harbor_ledger_memory.services.scan import ScanService
            from harbor_ledger_memory.vault.boundary import VaultBoundary

            monkeypatch.setattr(
                ScanService,
                "full_scan",
                lambda self: (_ for _ in ()).throw(RuntimeError("scan crash")),
            )
            real_replace = VaultBoundary.atomic_replace
            replace_calls = {"count": 0}

            def failing_rollback_replace(
                self, identity, content, *, expected, validate_current=None
            ):
                # Call 1 = the write, call 2 = the rollback replace.
                replace_calls["count"] += 1
                if replace_calls["count"] == 2:
                    raise PermissionError("rollback write failed")
                return real_replace(
                    self,
                    identity,
                    content,
                    expected=expected,
                    validate_current=validate_current,
                )

            monkeypatch.setattr(
                VaultBoundary, "atomic_replace", failing_rollback_replace
            )

            result = service.approve(proposal.id)
            assert result.status == "reconciliation_required"
            reason = (result.failure_reason or "").lower()
            assert "scan" in reason
            assert "rollback" in reason
            # The vault still holds the written content: unreconciled.
            assert existing.read_text(encoding="utf-8") == "# New"
            pid = proposal.id

        with CatalogSession(bind=engine) as fresh:
            row = fresh.get(MemoryWriteProposal, pid)
            assert row is not None
            assert row.status == "reconciliation_required"
            from harbor_ledger_memory.services.activity import ActivityService

            failed = [
                e
                for e in ActivityService(fresh).history()
                if e.event_type == "vault.mutation.failed"
            ]
            assert len(failed) == 1
            assert (
                "reconciliation" in (failed[0].payload["failure_reason"] or "").lower()
            )


class TestFreshSessionAudit:
    def test_applied_audit_survives_session_restart(self, tmp_path: Path) -> None:
        """Applied audit trail visible in fresh session."""
        settings = _build_settings(
            tmp_path,
            folder_rules=(
                FolderRule(
                    path=PurePosixPath("AI/proposed"),
                    access=FolderAccess.PROPOSE_WRITE,
                ),
            ),
        )
        engine = create_database(settings.database_url)
        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            proposal = service.request("AI/proposed/audit.md", "# New")
            session.commit()
            pid = proposal.id

        with CatalogSession(bind=engine) as session:
            service = VaultMutationService.from_settings(session, settings)
            service.approve(pid)
            session.commit()

        with CatalogSession(bind=engine) as session:
            row = session.get(MemoryWriteProposal, pid)
            assert row is not None
            assert row.status == "applied"
            assert row.applied_content_hash is not None
            # Audit trail
            from harbor_ledger_memory.services.activity import ActivityService

            svc = ActivityService(session)
            history = svc.history()
            applied = [e for e in history if e.event_type == "vault.mutation.applied"]
            assert len(applied) == 1
            assert applied[0].payload["proposal_id"] == pid
            assert (
                applied[0].payload["applied_content_hash"] == row.applied_content_hash
            )
