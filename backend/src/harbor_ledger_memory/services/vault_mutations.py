"""Vault mutation policy engine with proposal lifecycle management."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.models import MemoryWriteProposal, Note
from harbor_ledger_memory.config import FolderAccess, Settings
from harbor_ledger_memory.services.access import AccessPolicy
from harbor_ledger_memory.services.activity import ActivityService, graph_refs
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.services.live_traversal import NullLiveTraversalPublisher, TraversalEvent
from harbor_ledger_memory.vault.boundary import (
    AdmittedFileSnapshot,
    DirectoryCreationError,
    VaultBoundary,
    VaultPathError,
    WriteResult,
)
from harbor_ledger_memory.vault.parser import parse_note_bytes

STATUS_PENDING = "pending"
STATUS_APPLYING = "applying"
STATUS_APPLIED = "applied"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUS_RECONCILIATION_REQUIRED = "reconciliation_required"

MANAGED_BY_FIELD = "managed_by"
MANAGED_BY_VALUE = "harbor-ledger-memory"


class VaultWriteDenied(Exception):
    """Raised when a write is blocked by the configured folder access rule."""


class VaultMutationService:
    """Enforce vault write policies and manage the proposal lifecycle.

    Manual approvals and automatic writes share a single apply path:

    1. revalidate the folder access rule,
    2. re-read the target and pin the :class:`FileVersion` to write
       against (auto-writes additionally pin a semantic
       ``validate_current`` callback for the boundary),
    3. durably record ``status="applying"`` *before* any filesystem
       mutation,
    4. perform the version-checked write via the boundary,
    5. finalize (full rescan published through this service's activity
       service + ``applied``) when the :class:`WriteResult` is durable,
       otherwise roll back using the published write's own version —
       escalating to ``reconciliation_required`` when the rollback fails
       or its durability is uncertain.

    Every terminal transition (``applied`` / ``rejected`` / ``failed`` /
    ``reconciliation_required``) persists its audit event with the state
    change through :meth:`_commit_and_audit`.  When the activity service
    shares the proposal session (the :meth:`from_settings` default) the
    audit event is staged in the same transaction as the state change, so
    a commit failure rolls both back and a terminal state never persists
    without its audit.  When the application injects its own activity
    service (on a separate session) the state commits first and the audit
    is then recorded — and published to that service's live subscribers —
    durably on its own.

    Access decisions come from the caller's per-token
    :class:`AccessPolicy` when one is supplied (the REST and MCP lanes);
    the global ``settings.folder_rules`` are a draft template for the UI
    and never enforce.  Direct in-process callers without a policy fall
    back to the boundary's folder rules evaluated live at call time.
    """

    def __init__(
        self,
        session: Session,
        boundary: VaultBoundary,
        activity_service: ActivityService,
        live_traversal: Any | None = None,
    ) -> None:
        self._session = session
        self.boundary = boundary
        self.activity_service = activity_service
        self.on_applying: Callable[[MemoryWriteProposal], None] | None = None
        self._live_traversal = live_traversal or NullLiveTraversalPublisher()
        self._traversal_trace: str | None = None
        self._traversal_sequence = 0

    @classmethod
    def from_settings(
        cls,
        session: Session,
        settings: Settings,
        activity_service: ActivityService | None = None,
        live_traversal: Any | None = None,
    ) -> VaultMutationService:
        """Construct a service from an existing session and application settings.

        ``activity_service`` defaults to one bound to ``session``; the API
        injects the application-owned service so write events reach its
        live subscribers.
        """
        boundary = VaultBoundary(settings)
        return cls(session, boundary, activity_service or ActivityService(session), live_traversal)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request(
        self,
        path: str,
        content: str,
        operation: Literal["write", "mkdir"] = "write",
        policy: AccessPolicy | None = None,
        creator_token_id: int | None = None,
    ) -> MemoryWriteProposal:
        """Request a vault write and return the proposal object.

        ``policy`` is the caller's per-token access policy and decides the
        effective access level; direct callers without one fall back to
        the boundary's folder rules.

        Raises :class:`VaultWriteDenied` when the path is denied or
        read-only. For ``auto-write`` access the write may be applied
        immediately (managed update) or queued pending (creates /
        unmanaged updates / conflicts).
        """
        identity = self._validate_path(path)
        self._start_traversal()
        affected = self._affected_paths(identity)
        access = self._require_writable_paths(policy, affected)
        proposal = self._build_proposal(identity, content, access, operation)
        proposal.creator_token_id = creator_token_id
        proposal.affected_paths = [path.as_posix() for path in affected]
        self._session.add(proposal)
        self._session.commit()
        self.activity_service.record(
            "vault.mutation.requested",
            {
                "proposal_id": proposal.id,
                "path": proposal.path,
                "operation": proposal.operation,
                "access": proposal.rule_access,
                "status": proposal.status,
                "affected_paths": proposal.affected_paths,
                "created_paths": proposal.created_paths,
                "graph_refs": graph_refs([proposal.path]),
            },
        )
        self._publish_traversal(proposal.path)
        # Auto-write: attempt immediate apply through the shared path.
        if access is FolderAccess.AUTO_WRITE and proposal.status == STATUS_PENDING:
            self._apply(proposal, origin="auto", policy=policy)
        return proposal

    def approve(
        self,
        proposal_id: int,
        policy: AccessPolicy | None = None,
    ) -> MemoryWriteProposal:
        """Approve a pending proposal: revalidate, snapshot, write, scan.

        ``policy`` is the approver's per-token access policy; the
        revalidation checks it against the stored path (direct callers
        without one fall back to the boundary's folder rules).
        """
        proposal = self._fetch_proposal(proposal_id)
        self._start_traversal()
        self._check_pending(proposal)
        self._validate_path(proposal.path)
        self._apply(proposal, origin="approve", policy=policy)
        return proposal

    def reject(self, proposal_id: int) -> MemoryWriteProposal:
        """Reject a pending proposal without writing to disk."""
        proposal = self._fetch_proposal(proposal_id)
        self._start_traversal()
        self._check_pending(proposal)
        proposal.status = STATUS_REJECTED
        proposal.resolved_at = _now()
        self._commit_and_audit(
            "vault.mutation.rejected",
            {
                "proposal_id": proposal.id,
                "path": proposal.path,
                "operation": proposal.operation,
                "affected_paths": proposal.affected_paths,
                "created_paths": proposal.created_paths,
                "graph_refs": graph_refs([proposal.path]),
            },
        )
        self._publish_traversal(proposal.path)
        return proposal

    # ------------------------------------------------------------------
    # Single apply path
    # ------------------------------------------------------------------

    def _apply(
        self,
        proposal: MemoryWriteProposal,
        *,
        origin: str,
        policy: AccessPolicy | None = None,
        traversal_trace: str | None = None,
    ) -> None:
        """Apply one proposal (origin: ``"approve"`` or ``"auto"``).

        ``policy`` is the caller's per-token access policy for the
        revalidation (direct callers without one fall back to the
        boundary's folder rules).

        Approvals are terminal: any rejection or failure resolves the
        proposal (``failed`` / ``reconciliation_required``) and rule
        rejections raise :class:`VaultWriteDenied`.  Auto-writes are
        best-effort: semantic rejections simply leave the proposal
        ``pending`` for a later retry.
        """
        identity = PurePosixPath(proposal.path)
        affected = self._merge_affected_paths(
            self._affected_paths(identity), self._stored_paths(proposal)
        )
        try:
            access = self._require_writable_paths(policy, affected)
        except VaultWriteDenied as exc:
            if origin == "approve":
                self._fail_proposal(proposal, str(exc), affected)
                raise
            return

        # (1) Revalidate the access rule.
        if origin == "approve":
            if access not in (
                FolderAccess.PROPOSE_WRITE,
                FolderAccess.AUTO_WRITE,
            ):
                label = "denied" if access is FolderAccess.DENY else "read-only"
                reason = f"path is now {label} by access rule: {identity.as_posix()}"
                self._fail_proposal(proposal, reason, affected)
                raise VaultWriteDenied(reason)
        else:
            if access is not FolderAccess.AUTO_WRITE:
                return  # Access no longer allows auto-writes — keep pending.
            if proposal.operation in ("create", "mkdir"):
                return  # Auto-writes never create files — keep pending.

        # (2) Re-read the target and pin the version to write against.
        snapshot: AdmittedFileSnapshot | None = None
        if proposal.operation == "mkdir":
            snapshot = None
        elif proposal.operation == "update":
            try:
                snapshot = self.boundary.read_file(identity)
            except VaultPathError:
                if origin == "approve":
                    self._fail_proposal(proposal, "version conflict: file no longer exists on disk", affected)
                return  # Auto: file gone — keep pending.

            if snapshot.version is None:
                if origin == "approve":
                    self._fail_proposal(proposal, "could not determine file version on disk", affected)
                return

            if proposal.expected_source_hash:
                current_hash = hashlib.sha256(snapshot.content).hexdigest()
                if current_hash != proposal.expected_source_hash:
                    if origin == "approve":
                        self._fail_proposal(
                            proposal,
                            "version conflict: file on disk changed since proposal was created",
                            affected,
                        )
                    return  # Auto: changed externally — keep pending.

        validate_current: Callable[[AdmittedFileSnapshot], bool] | None = None
        if origin == "auto":
            validate_current = self._auto_write_validator(
                identity, snapshot, policy
            )

        # (3) Durable applying state before any filesystem mutation.
        proposal.status = STATUS_APPLYING
        proposal.applying_at = _now()
        self._session.commit()
        if traversal_trace is not None:
            self._traversal_trace = traversal_trace
        self._publish_traversal(proposal.path)
        if self.on_applying is not None:
            self.on_applying(proposal)

        # (4) Version-checked write through the boundary.
        content_bytes = proposal.content.encode("utf-8")
        original_content = snapshot.content if snapshot is not None else None
        try:
            if proposal.operation == "mkdir":
                directory_result = self.boundary.atomic_mkdir(identity)
                proposal.created_paths = [
                    path.as_posix() for path in directory_result.created_paths
                ]
                if not directory_result.durable:
                    self._persist_reconciliation(
                        proposal,
                        f"directory creation durability is uncertain for {identity.as_posix()}",
                        affected,
                    )
                    return
                write_result = directory_result
            elif proposal.operation == "create":
                write_result = self.boundary.atomic_create(identity, content_bytes)
            else:
                assert snapshot is not None
                assert snapshot.version is not None
                write_result = self.boundary.atomic_replace(
                    identity,
                    content_bytes,
                    expected=snapshot.version,
                    validate_current=validate_current,
                )
        except VaultPathError as exc:
            # Boundary exceptions are always pre-publication: nothing on
            # disk changed, so there is nothing to roll back.
            if proposal.operation == "mkdir":
                if isinstance(exc, DirectoryCreationError):
                    proposal.created_paths = [path.as_posix() for path in exc.created_paths]
                self._persist_reconciliation(
                    proposal,
                    f"reconciliation-required: mkdir failed: {exc}",
                    affected,
                )
                return
            if origin == "auto" and self._is_semantic_rejection(exc):
                # Transient/semantic rejection: safe to retry later.
                proposal.status = STATUS_PENDING
                self._session.commit()
                return
            self._fail_proposal(proposal, str(exc), affected)
            return
        except Exception as exc:
            # Unknown failure: the write may or may not have published.
            if proposal.operation == "mkdir":
                self._persist_reconciliation(
                    proposal,
                    f"reconciliation-required: mkdir failed: {exc}",
                    affected,
                )
                return
            self._rollback_best_effort(proposal, identity, original_content, str(exc), affected)
            return

        # (5) Resolve from the explicit WriteResult.
        if not write_result.durable:
            primary_reason = (
                f"write published but durability is uncertain for "
                f"{identity.as_posix()} (post-publication fsync failed)"
            )
            self._rollback_and_resolve(
                proposal, identity, original_content, write_result, primary_reason, affected
            )
            return

        try:
            # Rescan through this service's activity fan-out so the post-
            # write scan event reaches the application's live subscribers
            # (an application-owned service on its own session, or the
            # shared session's service by default).
            scan_service = ScanService(
                self.boundary,
                self._session,
                embedding_model=None,
                activity_service=self.activity_service,
                live_traversal=self._live_traversal,
            )
            scan_service.full_scan()
        except Exception as exc:
            if proposal.operation == "mkdir":
                self._persist_reconciliation(
                    proposal,
                    f"reconciliation-required: {exc}",
                    affected,
                )
                return
            self._rollback_and_resolve(
                proposal, identity, original_content, write_result, str(exc), affected
            )
            return

        self._mark_applied(proposal, None if proposal.operation == "mkdir" else content_bytes, affected)

    # ------------------------------------------------------------------
    # Rollback / resolution
    # ------------------------------------------------------------------

    def _rollback_and_resolve(
        self,
        proposal: MemoryWriteProposal,
        identity: PurePosixPath,
        original_content: bytes | None,
        write_result: WriteResult,
        primary_reason: str,
        affected: tuple[PurePosixPath, ...],
    ) -> None:
        """Roll back a published write and resolve the proposal.

        The rollback is pinned to a fresh, coherent read of the target
        and only proceeds when the on-disk bytes are exactly what we
        published: the write's own :class:`FileVersion` was captured
        from the temp file before the rename, and the rename can bump
        the published entry's ctime, so it may no longer describe the
        on-disk entry.  The boundary's version check on the rollback
        write protects against concurrent modification between the read
        and the rollback.

        A durable rollback resolves the proposal as ``failed``; a
        failed, conflicted, or durability-uncertain rollback escalates
        it to ``reconciliation_required``.
        """
        assert write_result.version is not None
        content_bytes = proposal.content.encode("utf-8")
        rollback_error: str | None = None
        rollback_durable: bool | None = None
        try:
            try:
                current = self.boundary.read_file(identity)
            except VaultPathError:
                current = None
            if current is None:
                if proposal.operation == "create":
                    # The created file is already gone: the rollback goal
                    # state (absence) holds.
                    rollback_durable = True
                else:
                    rollback_error = "original file is missing on disk"
            elif current.content != content_bytes:
                rollback_error = "on-disk content no longer matches the published write"
            else:
                assert current.version is not None
                if proposal.operation == "create":
                    rollback_result = self.boundary.remove_if_version(
                        identity, expected=current.version
                    )
                else:
                    assert original_content is not None
                    rollback_result = self.boundary.atomic_replace(
                        identity,
                        original_content,
                        expected=current.version,
                    )
                rollback_durable = rollback_result.durable
        except Exception as exc:
            rollback_error = str(exc)
            rollback_durable = None

        if rollback_error is None and rollback_durable:
            self._fail_proposal(proposal, primary_reason, affected)
        else:
            detail = rollback_error or "rollback durability is uncertain"
            self._persist_reconciliation(
                proposal,
                f"reconciliation-required: {primary_reason}; rollback failed or conflicted: {detail}",
                affected,
            )

    def _rollback_best_effort(
        self,
        proposal: MemoryWriteProposal,
        identity: PurePosixPath,
        original_content: bytes | None,
        write_error: str,
        affected: tuple[PurePosixPath, ...],
    ) -> None:
        """Recover from a write exception whose publication state is unknown."""
        if original_content is None:
            # Create: there is no original to restore and the exception
            # may have been raised after publication, so the written
            # file's durability was never confirmed.  Persist
            # reconciliation_required -- never an ordinary failed.
            self._persist_reconciliation(
                proposal,
                f"reconciliation-required: {write_error}; create write raised without a confirmed durable result",
                affected,
            )
            return
        try:
            current = self.boundary.read_file(identity)
        except VaultPathError:
            current = None
        if current is not None and current.content == original_content:
            # The disk still holds the original — the write never published.
            self._fail_proposal(proposal, write_error, affected)
            return
        try:
            assert current is not None
            assert current.version is not None
            rollback_result = self.boundary.atomic_replace(
                identity,
                original_content,
                expected=current.version,
            )
        except Exception as exc:
            self._persist_reconciliation(
                proposal,
                f"reconciliation-required: {write_error}; rollback failed or conflicted: {exc}",
                affected,
            )
            return
        if rollback_result.durable:
            self._fail_proposal(proposal, write_error, affected)
        else:
            self._persist_reconciliation(
                proposal,
                f"reconciliation-required: {write_error}; rollback durability is uncertain",
                affected,
            )

    def _persist_reconciliation(
        self, proposal: MemoryWriteProposal, reason: str,
        affected: tuple[PurePosixPath, ...] | None = None,
    ) -> None:
        """Persist a reconciliation-required resolution and audit it.

        The terminal state and its audit event are committed together by
        :meth:`_commit_and_audit` (see its contract for the
        application-owned-service case).
        """
        proposal.status = STATUS_RECONCILIATION_REQUIRED
        proposal.resolved_at = _now()
        proposal.failure_reason = reason
        self._commit_and_audit(
            "vault.mutation.failed",
            {
                "proposal_id": proposal.id,
                "path": proposal.path,
                "operation": proposal.operation,
                "failure_reason": reason,
                "affected_paths": [p.as_posix() for p in (affected or self._stored_paths(proposal))],
                "created_paths": proposal.created_paths,
                "graph_refs": graph_refs([proposal.path]),
            },
        )

    # ------------------------------------------------------------------
    # Auto-write semantic validation
    # ------------------------------------------------------------------

    def _auto_write_validator(
        self,
        identity: PurePosixPath,
        snapshot: AdmittedFileSnapshot | None,
        policy: AccessPolicy | None = None,
    ) -> Callable[[AdmittedFileSnapshot], bool]:
        """Build the ``validate_current`` callback for auto-writes.

        Invoked by the boundary on the same coherent
        :class:`AdmittedFileSnapshot` that was version-checked, strictly
        before any write begins.  The decision is derived from that
        coherent snapshot only: the boundary verifies that the on-disk
        file matches the pinned :class:`FileVersion` (whose sha256 was
        computed from *snapshot*) before invoking this callback, so a
        matching content hash proves the coherent bytes equal
        ``snapshot.content`` and the managed-ownership verdict reuses
        the frontmatter parsed from that same snapshot -- no second
        read, no TOCTOU window.

        ``policy`` is the caller's per-token access policy for the
        auto-write check (direct callers without one fall back to the
        boundary's folder rules).

        Rejects (returns ``False``) when the access no longer allows
        auto-writes, the coherent snapshot's version no longer matches
        the pinned snapshot, or the file is not managed by this plugin.
        """
        expected_hash: str | None = None
        managed = False
        if snapshot is not None:
            expected_hash = hashlib.sha256(snapshot.content).hexdigest()
            try:
                parsed = parse_note_bytes(snapshot.content, identity)
                managed = (
                    parsed.frontmatter.extra.get(MANAGED_BY_FIELD) == MANAGED_BY_VALUE
                )
            except (ValueError, OSError):
                managed = False

        def validate_current(snapshot: AdmittedFileSnapshot) -> bool:
            try:
                if self._access_for(policy, identity) is not FolderAccess.AUTO_WRITE:
                    return False
                if (
                    snapshot.version is None
                    or expected_hash is None
                    or snapshot.version.sha256 != expected_hash
                ):
                    return False
                return managed
            except (VaultPathError, ValueError, OSError):
                return False

        return validate_current

    @staticmethod
    def _is_semantic_rejection(exc: VaultPathError) -> bool:
        """True for rejections that are safe to retry while pending."""
        message = str(exc)
        return (
            "version conflict" in message
            or "validate_current callback rejected" in message
            or "not found on disk" in message
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_path(self, path: str) -> PurePosixPath:
        """Validate vault-relative path; raise VaultWriteDenied on failure."""
        identity = PurePosixPath(path)
        try:
            # Use public API: access_for validates and resolves
            self.boundary.access_for(identity)
        except VaultPathError as exc:
            raise VaultWriteDenied(str(exc)) from exc
        return identity

    def _affected_paths(self, identity: PurePosixPath) -> tuple[PurePosixPath, ...]:
        return (*self.boundary.missing_parent_paths(identity), identity)

    @staticmethod
    def _merge_affected_paths(
        current: tuple[PurePosixPath, ...], stored: list[PurePosixPath]
    ) -> tuple[PurePosixPath, ...]:
        merged: list[PurePosixPath] = []
        for path in (*current, *stored):
            if path not in merged:
                merged.append(path)
        return tuple(merged)

    def _require_writable_paths(
        self,
        policy: AccessPolicy | None,
        paths: tuple[PurePosixPath, ...],
    ) -> FolderAccess:
        accesses = [(path, self._access_for(policy, path)) for path in paths]
        denied: tuple[PurePosixPath, FolderAccess] | None = None
        for path, access in accesses:
            if not access.is_writable:
                # Validate every affected path, but report the requested target
                # rather than exposing a missing parent as the denial.
                denied = denied or (path, access)
        if denied is not None:
            target = paths[-1]
            _, access = denied
            self._record_denied(target.as_posix(), access, paths)
            label = "read-only" if access is FolderAccess.READ else "denied"
            raise VaultWriteDenied(
                f"write denied: {target.as_posix()} is {label} by folder rule"
            )
        return accesses[-1][1]

    @staticmethod
    def _stored_paths(proposal: MemoryWriteProposal) -> list[PurePosixPath]:
        return [PurePosixPath(path) for path in proposal.affected_paths]

    def _access_for(
        self, policy: AccessPolicy | None, identity: PurePosixPath
    ) -> FolderAccess:
        """Resolve the effective access level for ``identity``.

        An explicit per-token policy decides; direct in-process callers
        without one fall back to the boundary's folder rules (the global
        draft template) evaluated live at call time.
        """
        if policy is not None:
            return policy.access_for(identity)
        return self.boundary.access_for(identity)

    def _build_proposal(
        self,
        identity: PurePosixPath,
        content: str,
        access: FolderAccess,
        requested_operation: Literal["write", "mkdir"],
    ) -> MemoryWriteProposal:
        """Create a MemoryWriteProposal based on path, content, and access rule."""
        if access is FolderAccess.DENY:
            self._record_denied(identity.as_posix(), access)
            raise VaultWriteDenied(
                f"write denied: {identity.as_posix()} is denied by folder rule"
            )
        if access is FolderAccess.READ:
            self._record_denied(identity.as_posix(), access)
            raise VaultWriteDenied(
                f"write denied: {identity.as_posix()} is read-only (read access)"
            )

        if requested_operation == "mkdir":
            return MemoryWriteProposal(
                path=identity.as_posix(), content=content, operation="mkdir",
                status=STATUS_PENDING, rule_access=access.value, requested_at=_now()
            )

        expected_source_hash: str | None = None
        try:
            snapshot = self.boundary.read_file(identity)
            expected_source_hash = hashlib.sha256(snapshot.content).hexdigest()
            operation = "update"
        except VaultPathError:
            # File not on disk — check catalog for previously indexed note
            existing_note = self._session.scalar(
                select(Note).where(Note.path == identity.as_posix())
            )
            operation = "update" if existing_note is not None else "create"

        return MemoryWriteProposal(
            path=identity.as_posix(),
            content=content,
            operation=operation,
            status=STATUS_PENDING,
            rule_access=access.value,
            requested_at=_now(),
            expected_source_hash=expected_source_hash,
        )

    def _record_denied(
        self, path: str, access: FolderAccess,
        affected: tuple[PurePosixPath, ...] | None = None,
    ) -> None:
        """Record an activity event for a denied write."""
        self.activity_service.record(
            "vault.mutation.requested",
            {
                "path": path,
                "access": access.value,
                "status": "denied",
                "affected_paths": [p.as_posix() for p in (affected or (PurePosixPath(path),))],
                "created_paths": [],
                "graph_refs": graph_refs([path]),
            },
        )

    def _start_traversal(self) -> None:
        self._traversal_trace = str(uuid4())
        self._traversal_sequence = 0

    def _publish_traversal(self, path: str) -> None:
        if self._traversal_trace is None:
            self._start_traversal()
        assert self._traversal_trace is not None
        self._traversal_sequence += 1
        self._live_traversal.publish(
            TraversalEvent(self._traversal_trace, self._traversal_sequence, "write", path)
        )

    def _fetch_proposal(self, proposal_id: int) -> MemoryWriteProposal:
        """Fetch a proposal by ID or raise."""
        proposal = self._session.get(MemoryWriteProposal, proposal_id)
        if proposal is None:
            raise ValueError(f"proposal {proposal_id} not found")
        return proposal

    def _check_pending(self, proposal: MemoryWriteProposal) -> None:
        """Ensure the proposal is still pending."""
        if proposal.status != STATUS_PENDING:
            raise ValueError(
                f"proposal {proposal.id} is already {proposal.status!r}, not pending"
            )

    def _commit_and_audit(self, event_type: str, payload: Mapping[str, object]) -> None:
        """Commit the pending state change and persist its audit event.

        Shared-session services (the :meth:`from_settings` default) stage
        the audit event inside the state change's transaction and commit
        both at once, so a failed commit leaves neither behind.  Services
        with an application-owned activity service on its own session
        commit the state first, then record the audit durably — and
        publish it to that service's live subscribers — on its own.
        """
        if self.activity_service.session is self._session:
            message = self.activity_service.stage(event_type, payload)
            self._session.commit()
            self.activity_service.publish(message)
        else:
            self._session.commit()
            self.activity_service.record(event_type, payload)

    def _fail_proposal(
        self,
        proposal: MemoryWriteProposal,
        reason: str,
        affected: tuple[PurePosixPath, ...] | None = None,
    ) -> None:
        """Mark a proposal failed and persist the audit with the state."""
        proposal.status = STATUS_FAILED
        proposal.resolved_at = _now()
        proposal.failure_reason = reason
        self._commit_and_audit(
            "vault.mutation.failed",
            {
                "proposal_id": proposal.id,
                "path": proposal.path,
                "operation": proposal.operation,
                "failure_reason": reason,
                "affected_paths": [p.as_posix() for p in (affected or self._stored_paths(proposal))],
                "created_paths": proposal.created_paths,
                "graph_refs": graph_refs([proposal.path]),
            },
        )
        self._publish_traversal(proposal.path)

    def _mark_applied(
        self,
        proposal: MemoryWriteProposal,
        content_bytes: bytes | None,
        affected: tuple[PurePosixPath, ...] | None = None,
    ) -> None:
        """Mark a proposal applied and persist the audit with the state."""
        proposal.status = STATUS_APPLIED
        proposal.applied_content_hash = (
            hashlib.sha256(content_bytes).hexdigest() if content_bytes is not None else None
        )
        proposal.resolved_at = _now()
        self._commit_and_audit(
            "vault.mutation.applied",
            {
                "proposal_id": proposal.id,
                "path": proposal.path,
                "operation": proposal.operation,
                "applied_content_hash": proposal.applied_content_hash,
                "affected_paths": [p.as_posix() for p in (affected or self._stored_paths(proposal))],
                "created_paths": proposal.created_paths,
                "graph_refs": graph_refs([proposal.path]),
            },
        )
        self._publish_traversal(proposal.path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["VaultMutationService", "VaultWriteDenied"]
