"""MCP server exposing the memory index as tools over streamable HTTP.

The server is built per :func:`create_app` call so its tools close over the
active :class:`~harbor_ledger_memory.config.Settings` and the shared
:class:`~harbor_ledger_memory.services.activity.ActivityService`.  The
returned ASGI app is mounted at ``/mcp`` and its session manager is driven by
the application lifespan so the streamable-HTTP session task group is alive
for the process lifetime.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import PurePosixPath

from harbor_ledger_memory.api.auth import hlm_internal_request, hlm_mcp_token
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import MemoryWriteProposal
from harbor_ledger_memory.config import (
    FolderAccess,
    FolderRule,
    Settings,
    config_payload,
)
from harbor_ledger_memory.domain.retrieval import QueryRequest
from harbor_ledger_memory.graph.builder import GraphBuilder
from harbor_ledger_memory.services.access import AccessPolicy
from harbor_ledger_memory.services.activity import ActivityService
from harbor_ledger_memory.services.adaptive import (
    AdaptiveService,
    FeedbackValidationError,
)
from harbor_ledger_memory.services.query import QueryService
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.services.status import (
    CatalogStatusService,
    GraphService,
    effective_read_scope_for_policy,
    token_status_payload,
)
from harbor_ledger_memory.services.tokens import TokenService
from harbor_ledger_memory.services.vault_mutations import (
    VaultMutationService,
    VaultWriteDenied,
)
from harbor_ledger_memory.vault.boundary import VaultBoundary, VaultPathError

__all__ = ["build_mcp_server"]


# Full access for the internal in-process drive (no HTTP middleware ran, so
# no token record exists). Mirrors ``_FULL_ACCESS`` in ``api.auth``. The
# ``.`` rule grants every path the highest access level (a bare admin flag
# no longer implies folder access).
_FULL_ACCESS = AccessPolicy(
    rules=(FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE),),
    admin=True,
)


def build_mcp_server(
    settings: Settings,
    activity_service: ActivityService,
    token_service: TokenService | None = None,
):
    """Build the MCP server and its streamable-HTTP transport.

    Returns a ``(asgi_app, session_manager)`` tuple.  The caller must mount
    ``asgi_app`` on the application and run ``session_manager.run()`` inside
    the application lifespan — the streamable-HTTP session manager owns a task
    group that must stay alive for the whole process, and a mounted sub-app's
    own lifespan is not started by the parent.
    """
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    def _policy() -> AccessPolicy:
        """Resolve the AccessPolicy for the current tool call.

        Internal in-process drives (no HTTP middleware ran) get full access;
        otherwise the middleware-stamped token record defines the policy. A
        call with neither is a tool error — over HTTP the middleware has
        already answered 401, so this guards direct ``call_tool`` drives.
        """
        if hlm_internal_request.get():
            return _FULL_ACCESS
        record = hlm_mcp_token.get()
        if record is None:
            raise ToolError("authentication required: provide a valid API token")
        return AccessPolicy(record.rules, record.admin)

    mcp = MCPServer(
        "harbor-ledger-memory",
        instructions=(
            "Harbor Ledger Memory is a neural memory service for AI agents. "
            "Use query to retrieve relevant "
            "context for a task, status to inspect catalog health, "
            "settings_snapshot to see the active configuration, "
            "neighbours to explore the link graph, and scan to rebuild the "
            "index after vault changes. Retain the trace_id from query when "
            "using feedback to submit selected admitted paths as relevant or "
            "irrelevant."
        ),
    )

    def _open_session():
        """Open a catalog session for a single tool call (mirrors the REST routes)."""
        engine = create_database(settings.database_url)
        return engine, CatalogSession(bind=engine)

    @mcp.tool()
    def query(  # pyright: ignore[reportUnusedFunction]
        text: str,
        active_project: str | None = None,
    ) -> str:
        """Retrieve relevant vault context for a natural-language query.

        Args:
            text: The question or topic to retrieve context for.
            active_project: Optional project path used to bias retrieval.

        Returns:
            JSON with the selected memories (path, title, excerpt, retrieval and
            activation scores, reasons), excluded nodes, and the total estimated
            token count for the selection. Selected memories are limited to
            paths this token can read.
        """
        policy = _policy()
        engine, session = _open_session()
        try:
            service = QueryService(
                session,
                retrieval_settings=settings.retrieval,
                memory_settings=settings.memory,
                path_filter=VaultBoundary(settings).is_admitted,
                activity_service=activity_service,
            )
            result = service.query(
                QueryRequest(query=text, active_project=active_project)
            )
            kept = tuple(
                memory
                for memory in result.selected_memories
                if policy.can_read(memory.path)
            )
            if len(kept) != len(result.selected_memories):
                result = result.model_copy(update={"selected_memories": kept})
            return result.model_dump_json(indent=2)
        finally:
            session.close()
            engine.dispose()

    @mcp.tool()
    def feedback(  # pyright: ignore[reportUnusedFunction]
        trace_id: str,
        relevant_paths: list[str] | None = None,
        irrelevant_paths: list[str] | None = None,
    ) -> str:
        """Apply feedback to paths selected by a previous query trace."""
        policy = _policy()
        if not policy.has_any_write():
            raise ToolError("token has no write access")
        engine, session = _open_session()
        try:
            adaptive = AdaptiveService(session, settings.memory)
            try:
                adjustments = adaptive.apply_trace_feedback(
                    trace_uuid=trace_id,
                    relevant_paths=relevant_paths,
                    irrelevant_paths=irrelevant_paths,
                    path_filter=VaultBoundary(settings).is_admitted,
                )
            except FeedbackValidationError as exc:
                raise ToolError(str(exc)) from exc
            session.commit()
            return json.dumps(
                {"applied": True, "adjustments_count": adjustments}, indent=2
            )
        finally:
            session.close()
            engine.dispose()

    @mcp.tool()
    def status() -> str:  # pyright: ignore[reportUnusedFunction]
        """Inspect catalog health and the effective vault scope.

        Returns:
            JSON with whether the vault is read-only, the index root, indexed
            note count, scan runs, diagnostics, broken and ambiguous link
            counts, and the last scan status and timestamp. The write policy
            only lists folder rules this token can read.
        """
        policy = _policy()
        engine, session = _open_session()
        try:
            catalog = CatalogStatusService(
                session, path_filter=VaultBoundary(settings).is_admitted
            ).read()
            return json.dumps(
                token_status_payload(policy, settings, catalog),
                indent=2,
                default=str,
            )
        finally:
            session.close()
            engine.dispose()

    @mcp.tool()
    def settings_snapshot() -> str:  # pyright: ignore[reportUnusedFunction]
        """Return the active configuration as JSON.

        Returns:
            JSON with the index root, folder rules, database URL, retrieval and
            memory settings, and the effective read scope. Folder rules are
            limited to paths this token can read.
        """
        policy = _policy()
        payload = config_payload(settings)
        payload["folder_rules"] = [
            rule
            for rule in payload["folder_rules"]
            if policy.can_read(rule["path"])
        ]
        payload["effective_read_scope"] = effective_read_scope_for_policy(
            policy, settings.index_root.as_posix()
        )
        return json.dumps(payload, indent=2, default=str)

    @mcp.tool()
    def neighbours(path: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Explore the link graph around a single vault note.

        Args:
            path: Vault-relative note path.

        Returns:
            JSON list of adjacent notes with edge type, direction, node type,
            and confidence. Empty list when the path is not indexed or not
            readable by this token.
        """
        policy = _policy()
        if not policy.can_read(path):
            return "[]"
        engine, session = _open_session()
        try:
            boundary = VaultBoundary(settings)
            candidate = PurePosixPath(path)
            try:
                boundary.resolve_vault_path(candidate)
            except (ValueError, VaultPathError) as exc:
                raise ValueError(
                    "node path must be an admitted vault-relative path"
                ) from exc
            graph = GraphBuilder(session, path_filter=boundary.is_admitted).build()
            result = GraphService(graph).neighbours(candidate.as_posix())
            kept = [
                asdict(item) for item in result if policy.can_read(item.path)
            ]
            return json.dumps(kept, indent=2, default=str)
        finally:
            session.close()
            engine.dispose()

    @mcp.tool()
    def scan() -> str:  # pyright: ignore[reportUnusedFunction]
        """Rebuild the catalog with a full vault scan.

        Returns:
            JSON with the number of files indexed and broken/ambiguous link
            counts.
        """
        policy = _policy()
        if not policy.has_any_write():
            raise ToolError("token has no write access")
        scanner = ScanService.from_settings(settings)
        set_activity = getattr(scanner, "set_activity_service", None)
        if callable(set_activity):
            set_activity(activity_service)
        result = scanner.full_scan()
        return json.dumps(
            {
                "files_indexed": result.files_indexed,
                "broken_links": result.broken_links,
                "ambiguous_links": result.ambiguous_links,
            },
            indent=2,
            default=str,
        )

    def _mutation_call(
        operation: str,
        path: str | None = None,
        content: str | None = None,
        proposal_id: int | None = None,
        write_operation: str = "write",
    ) -> str:
        policy = _policy()
        if operation == "request":
            assert path is not None
            preflight_engine, preflight_session = _open_session()
            try:
                preflight_service = VaultMutationService.from_settings(
                    preflight_session, settings, activity_service=activity_service
                )
                for affected in _mutation_affected_paths(preflight_service, path):
                    if not policy.can_propose(affected.as_posix()):
                        raise ToolError(
                            f"path '{affected.as_posix()}' not writable by this token"
                        )
            finally:
                preflight_session.close()
                preflight_engine.dispose()
        elif proposal_id is not None:
            engine, session = _open_session()
            try:
                existing = session.get(MemoryWriteProposal, proposal_id)
                if existing is None:
                    raise ToolError(f"proposal {proposal_id} not found")
                service = VaultMutationService.from_settings(
                    session, settings, activity_service=activity_service
                )
                for affected in _mutation_affected_paths(
                    service, existing.path, existing.affected_paths
                ):
                    if not policy.can_propose(affected.as_posix()):
                        raise ToolError(
                            f"path '{affected.as_posix()}' not writable by this token"
                        )
            finally:
                session.close()
                engine.dispose()
        engine, session = _open_session()
        try:
            service = VaultMutationService.from_settings(
                session, settings, activity_service=activity_service
            )
            if operation == "request":
                assert path is not None and content is not None
                proposal = service.request(
                    path, content, operation=write_operation, policy=policy
                )
            elif operation == "approve":
                assert proposal_id is not None
                proposal = service.approve(proposal_id, policy=policy)
            else:
                assert proposal_id is not None
                proposal = service.reject(proposal_id)
            return json.dumps(_write_payload(proposal), indent=2, default=str)
        except VaultWriteDenied as exc:
            raise ToolError(str(exc)) from exc
        finally:
            session.close()
            engine.dispose()

    @mcp.tool()
    def propose_write(path: str, content: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Propose a policy-checked vault write."""
        return _mutation_call(
            "request", path=path, content=content, write_operation="write"
        )

    @mcp.tool()
    def propose_folder(path: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Propose a policy-checked vault folder creation."""
        return _mutation_call(
            "request", path=path, content="", write_operation="mkdir"
        )

    @mcp.tool()
    def approve_proposal(proposal_id: int) -> str:  # pyright: ignore[reportUnusedFunction]
        """Approve and apply a pending policy-checked write proposal."""
        return _mutation_call("approve", proposal_id=proposal_id)

    @mcp.tool()
    def reject_proposal(proposal_id: int) -> str:  # pyright: ignore[reportUnusedFunction]
        """Reject a pending write proposal without touching the vault."""
        return _mutation_call("reject", proposal_id=proposal_id)

    asgi_app = mcp.streamable_http_app(streamable_http_path="/")
    # Keep the server available on the mounted app for embedding hosts that
    # need to drive tools directly (the HTTP transport remains the public API).
    asgi_app.state.mcp_server = mcp
    # The public manager owns the task group driven by the application lifespan.
    return asgi_app, mcp.session_manager


def _write_payload(proposal: MemoryWriteProposal) -> dict[str, object]:
    return {
        "id": proposal.id,
        "path": proposal.path,
        "content": proposal.content,
        "operation": proposal.operation,
        "status": proposal.status,
        "rule_access": proposal.rule_access,
        "requested_at": proposal.requested_at,
        "resolved_at": proposal.resolved_at,
        "failure_reason": proposal.failure_reason,
        "affected_paths": proposal.affected_paths,
        "created_paths": proposal.created_paths,
    }


def _mutation_affected_paths(
    service: VaultMutationService,
    path: str,
    stored_paths: list[str] | None = None,
) -> tuple[PurePosixPath, ...]:
    """Return current and persisted identities covered by a mutation."""
    identity = service._validate_path(path)
    paths = list(service._affected_paths(identity))
    for stored in stored_paths or []:
        candidate = PurePosixPath(stored)
        if candidate not in paths:
            paths.append(candidate)
    return tuple(paths)
