"""FastAPI application for local status, vault write proposals, and activity."""

from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from importlib import metadata as importlib_metadata
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

import httpx
import jinja2
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import joinedload
from starlette.middleware.trustedhost import TrustedHostMiddleware

from harbor_ledger_memory.api.auth import (
    AuthContext,
    McpTokenMiddleware,
    TokenAuthError,
    authenticated,
    extract_bearer_header,
    hlm_internal_request,
    require_admin,
    require_any_write,
    require_writable,
    token_auth_error_handler,
)
from harbor_ledger_memory.api.mcp_server import build_mcp_server
from harbor_ledger_memory.api.routes.feedback import FeedbackRequest, FeedbackResponse
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import (
    MemoryWriteProposal,
    QueryTrace,
)
from harbor_ledger_memory.config import (
    FolderRule,
    Settings,
    ThemeSettings,
    config_payload,
    update_persistent_config,
)
from harbor_ledger_memory.domain.retrieval import QueryRequest, QueryResult
from harbor_ledger_memory.services.access import AccessPolicy
from harbor_ledger_memory.services.activity import (
    ActivityMessage,
    ActivityService,
)
from harbor_ledger_memory.services.adaptive import (
    AdaptiveService,
    FeedbackValidationError,
)
from harbor_ledger_memory.services.graph_projection import GraphHandleError, GraphProjectionService
from harbor_ledger_memory.services.memory import MemoryService
from harbor_ledger_memory.services.query import QueryService
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.services.status import (
    CatalogStatusService,
    token_status_payload,
    vault_is_read_only,
)
from harbor_ledger_memory.services.tokens import (
    DuplicateTokenNameError,
    InvalidTokenRequestError,
    TokenService,
)
from harbor_ledger_memory.services.ui_session import (
    UI_CSRF_COOKIE,
    UI_SESSION_COOKIE,
    UiSessionService,
)
from harbor_ledger_memory.services.vault_mutations import (
    STATUS_PENDING,
    VaultMutationService,
    VaultWriteDenied,
)
from harbor_ledger_memory.vault.boundary import VaultBoundary
from harbor_ledger_memory.watcher import VaultWatchService

logger = logging.getLogger(__name__)

RECENT_RESOLVED_WRITE_LIMIT = 100


class _HSTSMiddleware:
    """Add HSTS only to the HTTPS LAN deployment."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def send_with_hsts(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"strict-transport-security", b"max-age=31536000"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_hsts)


class _LanSourceMiddleware:
    """Reject UI/API requests from outside the configured LAN allowlist."""

    def __init__(self, app: Any, cidrs: tuple[str, ...]) -> None:
        self.app = app
        self.networks = tuple(ipaddress.ip_network(c, strict=False) for c in cidrs)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        protected = (
            path == "/"
            or path in {"/login", "/logout", "/status", "/query", "/settings"}
            or path.startswith("/api/v1/")
        )
        if scope.get("type") == "http" and protected and self.networks:
            client = scope.get("client")
            address = client[0] if client else ""
            try:
                allowed = any(
                    ipaddress.ip_address(address) in network
                    for network in self.networks
                )
            except ValueError:
                allowed = False
            if not allowed:
                response = JSONResponse(
                    status_code=403, content={"detail": "access denied"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _source_ip_allowed(request: Request, cidrs: tuple[str, ...]) -> bool:
    if not cidrs:
        return True
    address = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(address)
        return any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)
    except ValueError:
        return False


def _rate_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _login_allowed(application: FastAPI, request: Request) -> bool:
    entry = application.state.login_throttle.get(_rate_key(request))
    return entry is None or time.monotonic() >= entry[1]


def _login_failed(application: FastAPI, request: Request) -> None:
    key = _rate_key(request)
    count, _ = application.state.login_throttle.get(key, (0, 0.0))
    count += 1
    # Five failures are tolerated, then the delay grows exponentially.
    delay = 0.0 if count < 5 else min(900.0, 2.0 ** (count - 5))
    application.state.login_throttle[key] = (count, time.monotonic() + delay)


def _login_succeeded(application: FastAPI, request: Request) -> None:
    application.state.login_throttle.pop(_rate_key(request), None)


def _set_ui_cookies(
    response: Response,
    session: str,
    service: UiSessionService,
    secure: bool,
) -> None:
    response.set_cookie(
        UI_SESSION_COOKIE,
        session,
        httponly=True,
        samesite="strict",
        path="/",
        secure=secure,
    )
    response.set_cookie(
        UI_CSRF_COOKIE, service.csrf(session) or "", httponly=False,
        samesite="strict", path="/", secure=secure,
    )


class McpHealthResponse(BaseModel):
    """MCP reachability state: a locked /mcp must never be silent."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    active_tokens: int


class HealthResponse(BaseModel):
    """Stable response for process and routing health checks."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    read_only: bool
    mcp: McpHealthResponse


class StatusResponse(BaseModel):
    """Stable response for the disposable catalog status projection."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    read_only: bool
    write_policy: dict[str, object]
    vault_scope: str
    effective_read_scope: str
    indexed_notes: int
    scan_runs: int
    diagnostics: int
    broken_links: int
    ambiguous_links: int
    last_scan_status: str | None
    last_scan_completed_at: str | None


class CacheStatusResponse(BaseModel):
    """Response for the short-term cache status endpoint."""

    model_config = ConfigDict(extra="forbid")

    entry_count: int
    capacity: int
    ttl_days: int
    max_boost: float


class UpdateStatusResponse(BaseModel):
    """Response for the update status endpoint."""

    model_config = ConfigDict(extra="forbid")

    current_version: str
    latest_version: str
    update_available: bool


class TraceVisitResponse(BaseModel):
    """One activation node in a trace replay."""

    model_config = ConfigDict(extra="forbid")

    path: str
    activation_score: float
    hop: int
    via_path: str | None
    edge_type: str | None


class TraceSelectionResponse(BaseModel):
    """One context selection in a trace replay."""

    model_config = ConfigDict(extra="forbid")

    path: str
    rank: int
    retrieval_score: float
    activation_score: float


class TraceReplayResponse(BaseModel):
    """Response for the trace replay endpoint."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    query: str
    selected_paths: list[TraceSelectionResponse]
    activation_graph: list[TraceVisitResponse]
    created_at: str


class UpdateResponse(BaseModel):
    """Response for the update trigger endpoint."""

    model_config = ConfigDict(extra="forbid")

    success: bool
    message: str


class SettingsResponse(BaseModel):
    """Effective local configuration shown by the settings page/API."""

    model_config = ConfigDict(extra="forbid")

    vault_path: str
    index_root: str
    folder_rules: list[FolderRule]
    embedding_model: str | None
    effective_read_scope: str
    config_path: str
    folders: list[str]
    theme: ThemeSettings


class SettingsUpdateRequest(BaseModel):
    """Partial persistent settings update."""

    model_config = ConfigDict(extra="forbid")

    vault_path: str | None = None
    index_root: str | None = None
    folder_rules: list[FolderRule] | None = None
    embedding_model: str | None = None
    theme: ThemeSettings | None = None


class SettingsUpdateResponse(BaseModel):
    """Persistent settings update result."""

    model_config = ConfigDict(extra="forbid")

    saved: bool
    restart_required: bool
    message: str
    settings: SettingsResponse


class ActivityEventResponse(BaseModel):
    """One persisted operational activity event."""

    model_config = ConfigDict(extra="forbid")

    id: int
    event_type: str
    created_at: str
    payload: dict[str, object]


class ActivityHistoryResponse(BaseModel):
    """Recent activity events in chronological order."""

    model_config = ConfigDict(extra="forbid")

    events: list[ActivityEventResponse]


class ScanResponse(BaseModel):
    """Concise result of a triggered full scan."""

    model_config = ConfigDict(extra="forbid")

    files_indexed: int
    broken_links: int
    ambiguous_links: int
    diagnostics_count: int


class TokenCreateBody(BaseModel):
    """Request to create an API token with per-folder rules."""

    model_config = ConfigDict(extra="forbid")

    name: str
    rules: tuple[FolderRule, ...] = Field(default_factory=tuple)
    admin: bool = False


class WriteRequest(BaseModel):
    """Request to create a memory write proposal."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str = ""
    operation: Literal["write", "mkdir"] = "write"


class WriteProposalResponse(BaseModel):
    """Complete proposal record returned by the write endpoints."""

    model_config = ConfigDict(extra="forbid")

    id: int
    path: str
    content: str
    operation: str
    status: str
    rule_access: str
    requested_at: str
    resolved_at: str | None
    failure_reason: str | None
    affected_paths: list[str]
    created_paths: list[str]


class WritesListResponse(BaseModel):
    """Pending proposals and recent terminal records, newest first."""

    model_config = ConfigDict(extra="forbid")

    proposals: list[WriteProposalResponse]


class GraphNodeResponse(BaseModel):
    """One node in the structural graph snapshot."""

    model_config = ConfigDict(extra="forbid")

    path: str
    title: str
    isolated: bool
    kind: str = "file"


class GraphEdgeResponse(BaseModel):
    """One structural edge in the graph snapshot."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    target: str
    edge_type: str
    explicit: bool


class GraphSnapshotResponse(BaseModel):
    """Complete deterministic snapshot of the vault graph."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[GraphNodeResponse]
    edges: list[GraphEdgeResponse]
    generation: str


class GraphClusterResponse(BaseModel):
    """One bounded cluster. ``scope`` is the stable drill-down scope."""
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    kind: str
    type_counts: dict[str, int]
    member_count: int
    scope: str


class GraphClusterEdgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: str
    target: str
    weight: float


class GraphViewResponse(BaseModel):
    """Bounded graph view: level 0 broad, level 1 folder/type, level 2 leaves.

    Level 2 requires an authorized ``scope``; responses never include member
    paths, and ``generation`` hashes only the returned authorized view.
    """
    model_config = ConfigDict(extra="forbid")
    level: int
    scope: str | None
    clusters: list[GraphClusterResponse]
    edges: list[GraphClusterEdgeResponse]
    generation: str
    next_cursor: str | None = None


def create_app(
    settings: Settings,
    *,
    ui_session_service: UiSessionService | None = None,
    ui_origin: str | None = None,
) -> FastAPI:
    """Create the API without loading settings or starting a server.

    The only application data touched by the status endpoint is the catalog
    configured in ``settings.database_url``. Vault files are not opened.
    """

    activity_service = ActivityService(settings.database_url)
    token_service = TokenService(settings.database_url)
    ui_session_service = ui_session_service or UiSessionService()
    if settings.mcp.enabled:
        mcp_app, mcp_session_manager = build_mcp_server(
            settings, activity_service, token_service
        )
    else:
        mcp_app = None
        mcp_session_manager = None
    display_settings = settings
    graph_cursor_secret = secrets.token_bytes(32)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
        # The streamable-HTTP MCP session manager owns an anyio task group that
        # must live for the whole process; a mounted sub-app's own lifespan is
        # not started by the parent, so we drive it here. Enter it manually
        # (rather than ``async with``) so a startup error — e.g. a failed scan —
        # propagates as the original exception instead of being wrapped in an
        # anyio ExceptionGroup by the task-group context.
        manager = mcp_session_manager.run() if mcp_session_manager else None
        if manager:
            await manager.__aenter__()
        try:
            try:
                # Upgrade the catalog database to the latest schema before any writes.
                # Safe to call repeatedly — Alembic skips already-applied migrations.
                from harbor_ledger_memory.catalog.migrate import upgrade_to_head

                upgrade_to_head(settings.database_url)
                # Legacy scope-based tokens keep working: derive their per-folder
                # rules from the global config so read-sweeps apply to them too.
                token_service.backfill_legacy_tokens(settings.folder_rules)
                if settings.mcp.enabled and token_service.count_active() == 0:
                    logger.warning(
                        "MCP is enabled but has no active API tokens; /mcp "
                        "will return 401 until you create a token "
                        "(hlm token create)."
                    )

                scanner = ScanService.from_settings(settings)
                set_activity_service = getattr(scanner, "set_activity_service", None)
                is_real_scanner = callable(set_activity_service)
                if is_real_scanner:
                    set_activity_service(activity_service)
                scan_result = scanner.full_scan()
                if not is_real_scanner:
                    activity_service.record("scan", _scan_activity_payload(scan_result))
                watcher = VaultWatchService(scanner.boundary, scanner)
                watcher.start()
                application.state.vault_watcher = watcher
                application.state.activity_service = activity_service
            except Exception:
                logger.exception("Startup failed; shutting down server")
                raise
            try:
                yield
            finally:
                watcher.stop()
                token_service.close()
                activity_service.close()
        finally:
            # Pass no exception to the task-group shutdown so the original error,
            # if any, is the one the caller observes.
            if manager:
                await manager.__aexit__(None, None, None)

    application = FastAPI(
        title="Harbor Ledger Memory",
        description="Neural memory service for AI agents with local vault controls",
        version="2.0.1",
        lifespan=lifespan,
    )
    application.state.activity_service = activity_service
    application.state.token_service = token_service
    application.state.ui_session_service = ui_session_service
    application.state.ui_origin = ui_origin or _loopback_origin(
        settings.server_host, settings.server_port
    )
    application.add_exception_handler(TokenAuthError, token_auth_error_handler)
    application.state.frontend_enabled = settings.frontend.enabled
    application.state.api_enabled = settings.api.enabled
    application.state.frontend_mode = settings.frontend.mode
    application.state.secure_cookies = bool(ui_origin and ui_origin.startswith("https://"))
    application.state.login_throttle = {}  # type: ignore[attr-defined]

    if settings.frontend.mode == "lan":
        parsed_origin = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(
            application.state.ui_origin
        )
        application.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=[parsed_origin.hostname or ""],
        )
        application.add_middleware(
            _LanSourceMiddleware, cidrs=settings.network.allowed_cidrs
        )
        if application.state.secure_cookies:
            application.add_middleware(_HSTSMiddleware)

    def settings_response(policy: AccessPolicy | None = None) -> SettingsResponse:
        boundary = VaultBoundary(display_settings)
        payload = config_payload(display_settings)
        directories = [
            directory.as_posix()
            for directory in boundary.iter_directories()
            if not any(
                part.startswith(".") or part == "node_modules"
                for part in directory.parts
            )
        ]
        if policy is not None:
            payload["folder_rules"] = [
                rule.model_dump(mode="json")
                for rule in display_settings.folder_rules
                if policy.can_read(rule.path)
            ]
            directories = [d for d in directories if policy.can_read(d)]
        payload["folders"] = sorted(directories)
        return SettingsResponse.model_validate(payload)

    def _require_ui_page(request: Request) -> None:
        if settings.frontend.mode == "lan" and not ui_session_service.authenticate(
            request.cookies.get(UI_SESSION_COOKIE)
        ):
            raise HTTPException(status_code=303, headers={"location": "/login"})

    # The React app is an optional install. Keep the Python package usable with
    # only ``uv sync``; a Vite build is mounted when it is present in checkout.
    static_dir = Path(__file__).parent.parent / "static"
    application.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Streamable-HTTP MCP endpoint: tools over HTTP for MCP clients (opencode,
    # Claude, ...). Mounted at /mcp; the session manager runs in the lifespan.
    if mcp_app is not None:
        application.mount("/mcp", mcp_app, name="mcp")
        application.add_middleware(McpTokenMiddleware, token_service=token_service)

    # Resolve web/dist: prefer the packaged ``web_dist`` resource (installed
    # wheel), fall back to ``web/dist`` in the source checkout (development).
    _pkg_web_dist = Path(
        str(resources.files("harbor_ledger_memory").joinpath("web_dist"))
    )
    if _pkg_web_dist.is_dir():
        web_dist = _pkg_web_dist
    else:
        web_dist = Path(__file__).resolve().parents[4] / "web" / "dist"
    web_index = web_dist / "index.html"
    if web_dist.is_dir() and (web_dist / "assets").is_dir():
        application.mount(
            "/assets",
            StaticFiles(directory=str(web_dist / "assets")),
            name="web-assets",
        )

    # Templates
    template_dir = Path(__file__).parent.parent / "templates"
    jinja_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_dir)),
        autoescape=jinja2.select_autoescape(["html"]),
    )

    @application.get("/")
    def index(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        session = request.cookies.get(UI_SESSION_COOKIE)
        new_session = None
        if settings.frontend.mode == "lan" and not ui_session_service.authenticate(
            session
        ):
            return RedirectResponse("/login", status_code=303)
        if settings.frontend.enabled and not ui_session_service.authenticate(session):
            new_session = ui_session_service.new_session()
        if web_index.is_file():
            result = FileResponse(web_index, media_type="text/html")
        else:
            result = HTMLResponse(jinja_env.get_template("base.html").render())
        if new_session is not None:
            result.set_cookie(
                UI_SESSION_COOKIE,
                new_session,
                httponly=True,
                samesite="strict",
                path="/",
                secure=application.state.secure_cookies,
            )
            result.set_cookie(
                UI_CSRF_COOKIE,
                ui_session_service.csrf(new_session) or "",
                httponly=False, samesite="strict", path="/",
                secure=application.state.secure_cookies,
            )
        return result

    @application.get("/login")
    def login_page() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return HTMLResponse(
            '<!doctype html><title>Harbor Ledger Memory — Log in</title>'
            '<form method="post">'
            '<label>Password <input type="password" name="password" autofocus></label>'
            '<button type="submit">Log in</button></form>'
        )

    @application.post("/login")
    def login(request: Request, password: str = Form(...)) -> Response:  # pyright: ignore[reportUnusedFunction]
        if settings.frontend.mode != "lan":
            return RedirectResponse("/", status_code=303)
        if request.headers.get("origin") != application.state.ui_origin:
            raise HTTPException(status_code=403, detail="login unavailable")
        allowed = _source_ip_allowed(request, settings.network.allowed_cidrs)
        if not allowed:
            raise HTTPException(status_code=403, detail="login unavailable")
        if not _login_allowed(application, request):
            raise HTTPException(status_code=429, detail="try again later")
        verifier = settings.frontend.password_verifier
        valid = False
        if verifier:
            try:
                valid = PasswordHasher().verify(verifier, password)
            except (InvalidHashError, VerificationError, VerifyMismatchError):
                valid = False
        if not valid:
            _login_failed(application, request)
            raise HTTPException(status_code=401, detail="invalid credentials")
        _login_succeeded(application, request)
        old = request.cookies.get(UI_SESSION_COOKIE)
        ui_session_service.revoke(old)
        session = ui_session_service.new_session()
        result = RedirectResponse("/", status_code=303)
        _set_ui_cookies(
            result, session, ui_session_service, application.state.secure_cookies
        )
        return result

    @application.post("/logout")
    def logout(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        session = request.cookies.get(UI_SESSION_COOKIE)
        if (
            not ui_session_service.authenticate(session)
            or request.headers.get("origin") != application.state.ui_origin
            or request.headers.get("X-HLM-CSRF")
            != ui_session_service.csrf(session)
        ):
            raise HTTPException(status_code=403, detail="logout unavailable")
        ui_session_service.revoke(session)
        result = RedirectResponse("/login", status_code=303)
        result.delete_cookie(UI_SESSION_COOKIE, path="/")
        result.delete_cookie(UI_CSRF_COOKIE, path="/")
        return result

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:  # pyright: ignore[reportUnusedFunction]
        return HealthResponse(
            status="ok",
            read_only=vault_is_read_only(settings),
            mcp=McpHealthResponse(
                enabled=settings.mcp.enabled,
                active_tokens=token_service.count_active(),
            ),
        )

    @application.get("/api/v1/status", response_model=StatusResponse)
    def status(  # pyright: ignore[reportUnusedFunction]
        auth: AuthContext = Depends(authenticated),
    ) -> StatusResponse:
        boundary = VaultBoundary(settings)
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            catalog_status = CatalogStatusService(
                session, path_filter=boundary.is_admitted
            ).read()
        finally:
            session.close()
            engine.dispose()
        payload = token_status_payload(auth.policy, settings, catalog_status)
        return StatusResponse.model_validate(payload)

    @application.post(
        "/api/v1/scan",
        response_model=ScanResponse,
        dependencies=[Depends(require_any_write)],
    )
    def trigger_scan() -> ScanResponse:  # pyright: ignore[reportUnusedFunction]
        """Re-run a full vault scan, rebuilding the catalog projection."""
        scanner = ScanService.from_settings(settings)
        set_activity_service = getattr(scanner, "set_activity_service", None)
        if callable(set_activity_service):
            set_activity_service(activity_service)
        scan_result = scanner.full_scan()
        return ScanResponse(
            files_indexed=scan_result.files_indexed,
            broken_links=scan_result.broken_links,
            ambiguous_links=scan_result.ambiguous_links,
            diagnostics_count=len(scan_result.diagnostics),
        )

    @application.post("/api/v1/writes", response_model=WriteProposalResponse)
    def create_write(  # pyright: ignore[reportUnusedFunction]
        request: WriteRequest,
        auth: AuthContext = Depends(authenticated),
    ) -> WriteProposalResponse:
        """Create a write proposal, or auto-apply an allowed managed update.

        The caller's per-token access policy decides deny/propose/auto —
        the global ``settings.folder_rules`` are a draft template for the
        UI and never enforce.
        """
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            service = VaultMutationService.from_settings(
                session, settings, activity_service=activity_service
            )
            _require_mutation_paths_writable(
                auth, _mutation_affected_paths(service, request.path)
            )
            proposal = service.request(
                request.path,
                request.content,
                operation=request.operation,
                policy=auth.policy,
            )
            return _write_response(proposal)
        except VaultWriteDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        finally:
            session.close()
            engine.dispose()

    @application.get("/api/v1/writes", response_model=WritesListResponse)
    def list_writes(  # pyright: ignore[reportUnusedFunction]
        auth: AuthContext = Depends(authenticated),
    ) -> WritesListResponse:
        """Return pending proposals plus recent terminal records, newest first.

        Terminal records are those with a ``resolved_at``; the transient
        ``applying`` state is never listed.  Ordering is deterministic:
        newest ``requested_at`` first, tie-broken by newest id.  Proposals
        whose path the token cannot read are swept out.
        """
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            pending = (
                session.query(MemoryWriteProposal)
                .filter(MemoryWriteProposal.status == STATUS_PENDING)
                .all()
            )
            recent = (
                session.query(MemoryWriteProposal)
                .filter(MemoryWriteProposal.resolved_at.is_not(None))
                .order_by(MemoryWriteProposal.requested_at.desc())
                .limit(RECENT_RESOLVED_WRITE_LIMIT)
                .all()
            )
            proposals = sorted(
                [*pending, *recent],
                key=lambda proposal: (proposal.requested_at, proposal.id),
                reverse=True,
            )
            return WritesListResponse(
                proposals=[
                    _write_response(proposal)
                    for proposal in proposals
                    if auth.policy.can_read(proposal.path)
                ]
            )
        finally:
            session.close()
            engine.dispose()

    @application.post(
        "/api/v1/writes/{id}/approve",
        response_model=WriteProposalResponse,
    )
    def approve_write(  # pyright: ignore[reportUnusedFunction]
        id: int,
        auth: AuthContext = Depends(authenticated),
    ) -> WriteProposalResponse:
        """Approve a pending proposal and apply the vault write."""
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            existing = session.get(MemoryWriteProposal, id)
            if existing is None:
                raise HTTPException(status_code=404, detail=f"proposal {id} not found")
            service = VaultMutationService.from_settings(
                session, settings, activity_service=activity_service
            )
            _require_mutation_paths_writable(
                auth, _mutation_affected_paths(service, existing.path, existing.affected_paths)
            )
            proposal = service.approve(id, policy=auth.policy)
            return _write_response(proposal)
        except VaultWriteDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            session.close()
            engine.dispose()

    @application.post(
        "/api/v1/writes/{id}/reject",
        response_model=WriteProposalResponse,
    )
    def reject_write(  # pyright: ignore[reportUnusedFunction]
        id: int,
        auth: AuthContext = Depends(authenticated),
    ) -> WriteProposalResponse:
        """Reject a pending proposal without touching the vault."""
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            existing = session.get(MemoryWriteProposal, id)
            if existing is None:
                raise HTTPException(status_code=404, detail=f"proposal {id} not found")
            service = VaultMutationService.from_settings(
                session, settings, activity_service=activity_service
            )
            _require_mutation_paths_writable(
                auth, _mutation_affected_paths(service, existing.path, existing.affected_paths)
            )
            proposal = service.reject(id)
            return _write_response(proposal)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            session.close()
            engine.dispose()

    @application.get("/api/v1/tokens", dependencies=[Depends(require_admin)])
    def list_tokens() -> list[dict[str, object]]:  # pyright: ignore[reportUnusedFunction]
        return [
            {
                "name": record.name,
                "rules": [rule.model_dump(mode="json") for rule in record.rules],
                "admin": record.admin,
                "created_at": record.created_at.isoformat(),
            }
            for record in token_service.list()
            if record.active
        ]

    @application.post(
        "/api/v1/tokens",
        status_code=201,
        response_model=None,
        dependencies=[Depends(require_admin)],
    )
    def create_token(  # pyright: ignore[reportUnusedFunction]
        body: TokenCreateBody,
    ) -> dict[str, object] | JSONResponse:
        try:
            created = token_service.create(
                body.name, body.rules, admin=body.admin, activity=activity_service
            )
        except (DuplicateTokenNameError, InvalidTokenRequestError) as exc:
            status = 409 if isinstance(exc, DuplicateTokenNameError) else 400
            return JSONResponse(status_code=status, content={"detail": str(exc)})
        return {
            "token": created.plaintext,
            "name": created.name,
            "rules": [rule.model_dump(mode="json") for rule in created.rules],
            "admin": created.admin,
            "created_at": created.record.created_at.isoformat(),
        }

    @application.delete(
        "/api/v1/tokens/{name}",
        response_model=None,
        dependencies=[Depends(require_admin)],
    )
    def revoke_token(  # pyright: ignore[reportUnusedFunction]
        name: str,
    ) -> dict[str, object] | JSONResponse:
        record = token_service.revoke(name, activity=activity_service)
        if record is None:
            return JSONResponse(
                status_code=404, content={"detail": f"unknown token name: {name}"}
            )
        return {"name": name, "revoked": True}

    @application.get("/api/v1/graph", response_model=GraphSnapshotResponse)
    def graph_snapshot(  # pyright: ignore[reportUnusedFunction]
        auth: AuthContext = Depends(authenticated),
    ) -> GraphSnapshotResponse:
        """Return the structural graph snapshot, read-swept by the token."""
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            snapshot = GraphProjectionService(session).snapshot(
                path_filter=auth.policy.can_read
            )
        finally:
            session.close()
            engine.dispose()
        return GraphSnapshotResponse(
            nodes=[
                GraphNodeResponse(
                    path=node.path,
                    title=node.title,
                    isolated=node.isolated,
                    kind=node.kind,
                )
                for node in snapshot.nodes
            ],
            edges=[
                GraphEdgeResponse(
                    id=edge.id,
                    source=edge.source,
                    target=edge.target,
                    edge_type=edge.edge_type,
                    explicit=edge.explicit,
                )
                for edge in snapshot.edges
            ],
            generation=snapshot.generation,
        )

    @application.get("/api/v1/graph/view", response_model=GraphViewResponse)
    def graph_view(
        level: int = Query(0), scope: str | None = Query(None),
        page_size: int = Query(100), cursor: str | None = Query(None),
        auth: AuthContext = Depends(authenticated),
    ) -> GraphViewResponse:
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            try:
                view = GraphProjectionService(session).view(
                    level, scope=scope, path_filter=auth.policy.can_read,
                    policy=auth.policy,
                    policy_fingerprint=auth.policy.fingerprint(), cursor=cursor,
                    page_size=page_size, cursor_secret=graph_cursor_secret,
                )
            except GraphHandleError:
                raise HTTPException(status_code=400, detail="graph view handle invalid or expired; restart the view") from None
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            session.close()
            engine.dispose()
        return GraphViewResponse(
            level=view.level, scope=view.scope,
            clusters=[
                GraphClusterResponse(
                    id=cluster.id,
                    label=cluster.label,
                    kind=cluster.kind,
                    type_counts=cluster.type_counts,
                    member_count=cluster.member_count,
                    scope=cluster.scope,
                )
                for cluster in view.clusters
            ],
            edges=[
                GraphClusterEdgeResponse(
                    id=edge.id,
                    source=edge.source,
                    target=edge.target,
                    weight=edge.weight,
                )
                for edge in view.edges
            ],
            generation=view.generation,
            next_cursor=view.next_cursor,
        )

    @application.post("/api/v1/queries", response_model=QueryResult)
    def query(  # pyright: ignore[reportUnusedFunction]
        request: QueryRequest,
        auth: AuthContext = Depends(authenticated),
    ) -> QueryResult:
        """Execute a transparent memory query, read-swept by the token."""
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            service = QueryService(
                session,
                retrieval_settings=settings.retrieval,
                memory_settings=settings.memory,
                path_filter=VaultBoundary(settings).is_admitted,
                activity_service=activity_service,
            )
            result = service.query(request)
        finally:
            session.close()
            engine.dispose()
        kept = [
            memory
            for memory in result.selected_memories
            if auth.policy.can_read(memory.path)
        ]
        if len(kept) != len(result.selected_memories):
            result = result.model_copy(update={"selected_memories": tuple(kept)})
        return result

    @application.get(
        "/api/v1/activity",
        response_model=ActivityHistoryResponse,
    )
    @application.get(
        "/api/v1/activity/history",
        response_model=ActivityHistoryResponse,
    )
    def activity_history(  # pyright: ignore[reportUnusedFunction]
        limit: int = Query(100, ge=1, le=500),
        after_id: int | None = Query(None, ge=0),
        auth: AuthContext = Depends(authenticated),
    ) -> ActivityHistoryResponse:
        """Return persisted activity events, retaining only the last 30 days.

        Events whose path-bearing payload fields the token cannot read are
        swept out.
        """
        swept_events: list[ActivityEventResponse] = []
        for event in activity_service.history(limit=limit, after_id=after_id):
            payload = _sweep_activity_payload(
                auth.policy, event.event_type, event.payload
            )
            if payload is not None:
                swept_events.append(_activity_response(event, payload))
        return ActivityHistoryResponse(events=swept_events)

    @application.get("/api/v1/activity/stream")
    async def activity_stream(  # pyright: ignore[reportUnusedFunction]
        after_id: int | None = Query(None, ge=0),
        auth: AuthContext = Depends(authenticated),
    ) -> StreamingResponse:
        """Replay recent activity and then stream newly committed events.

        Unreadable events are swept before serialization, and
        ``token_created`` rules are swept to readable paths, so the stream
        leaks no path the token cannot read.
        """

        async def swept_stream() -> AsyncGenerator[str, None]:
            async for frame in activity_service.stream(after_id=after_id):
                if auth.ui_session and not ui_session_service.is_valid(auth.ui_session):
                    return
                swept = _swept_sse_frame(auth.policy, frame)
                if swept is not None:
                    yield swept

        return StreamingResponse(
            swept_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @application.get(
        "/api/v1/traces/{trace_id}",
        response_model=TraceReplayResponse,
    )
    def get_trace(  # pyright: ignore[reportUnusedFunction]
        trace_id: str,
        auth: AuthContext = Depends(authenticated),
    ) -> TraceReplayResponse:
        """Retrieve a stored query trace for replay.

        Selections and activation visits whose path (or the visit's
        ``via_path`` edge endpoint) the token cannot read are swept out, so
        the replay leaks no unreadable path.
        """
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            trace = (
                session.query(QueryTrace)
                .options(
                    joinedload(QueryTrace.visits),
                    joinedload(QueryTrace.selections),
                )
                .filter(QueryTrace.trace_uuid == trace_id)
                .first()
            )
            if trace is None:
                raise HTTPException(
                    status_code=404, detail=f"Trace {trace_id} not found"
                )

            boundary = VaultBoundary(settings)
            return TraceReplayResponse(
                trace_id=trace.trace_uuid,
                query=trace.query,
                selected_paths=[
                    TraceSelectionResponse(
                        path=sel.path,
                        rank=sel.rank,
                        retrieval_score=sel.retrieval_score,
                        activation_score=sel.activation_score,
                    )
                    for sel in trace.selections
                    if boundary.is_admitted(sel.path)
                    and auth.policy.can_read(sel.path)
                ],
                activation_graph=[
                    TraceVisitResponse(
                        path=visit.path,
                        activation_score=visit.activation_score,
                        hop=visit.hop,
                        via_path=visit.via_path,
                        edge_type=visit.edge_type,
                    )
                    for visit in trace.visits
                    if boundary.is_admitted(visit.path)
                    and auth.policy.can_read(visit.path)
                    and (
                        visit.via_path is None
                        or auth.policy.can_read(visit.via_path)
                    )
                ],
                created_at=trace.created_at,
            )
        finally:
            session.close()
            engine.dispose()

    @application.post(
        "/api/v1/feedback",
        dependencies=[Depends(require_any_write)],
    )
    def feedback(request: FeedbackRequest) -> FeedbackResponse:  # pyright: ignore[reportUnusedFunction]
        """Apply feedback to adjust adaptive edge weights."""
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            adaptive = AdaptiveService(session, settings.memory)
            try:
                adjustments = adaptive.apply_trace_feedback(
                    trace_uuid=request.trace_id,
                    relevant_paths=request.relevant_paths,
                    irrelevant_paths=request.irrelevant_paths,
                    path_filter=VaultBoundary(settings).is_admitted,
                )
            except FeedbackValidationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            session.commit()
            return FeedbackResponse(applied=True, adjustments_count=adjustments)
        finally:
            session.close()
            engine.dispose()

    @application.get(
        "/api/v1/cache-status",
        response_model=CacheStatusResponse,
        dependencies=[Depends(authenticated)],
    )
    def cache_status() -> CacheStatusResponse:  # pyright: ignore[reportUnusedFunction]
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            memory = MemoryService(session, settings.memory)
            candidates = memory.cache_candidates()
        finally:
            session.close()
            engine.dispose()
        return CacheStatusResponse(
            entry_count=len(candidates),
            capacity=settings.memory.short_term_capacity,
            ttl_days=settings.memory.short_term_ttl_days,
            max_boost=settings.memory.short_term_max_boost,
        )

    @application.get("/status")
    def status_page(request: Request) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        _require_ui_page(request)
        boundary = VaultBoundary(settings)
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            catalog_status = CatalogStatusService(
                session, path_filter=boundary.is_admitted
            ).read()
        finally:
            session.close()
            engine.dispose()

        memory_engine = create_database(settings.database_url)
        memory_session = CatalogSession(bind=memory_engine)
        try:
            memory = MemoryService(memory_session, settings.memory)
            candidates = memory.cache_candidates()
            cache_status = {
                "entry_count": len(candidates),
                "capacity": settings.memory.short_term_capacity,
                "ttl_days": settings.memory.short_term_ttl_days,
                "max_boost": settings.memory.short_term_max_boost,
            }
        except Exception:
            cache_status = {"error": "Unavailable"}
        finally:
            memory_session.close()
            memory_engine.dispose()

        return HTMLResponse(
            jinja_env.get_template("status.html").render(
                status=catalog_status,
                cache_status=cache_status,
                vault_scope=settings.index_root.as_posix(),
                effective_read_scope=settings.effective_read_scope,
            )
        )

    @application.get("/query")
    async def query_page(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        q: str = "",
        project: str = "",
        results: dict[str, object] | None = None,
    ) -> Response:
        _require_ui_page(request)
        return HTMLResponse(
            jinja_env.get_template("query.html").render(
                q=q, project=project, results=results
            )
        )

    @application.get("/settings")
    def settings_page(request: Request) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        _require_ui_page(request)
        return HTMLResponse(
            jinja_env.get_template("settings.html").render(
                settings=config_payload(settings)
            )
        )

    @application.get("/api/v1/settings", response_model=SettingsResponse)
    def read_settings(  # pyright: ignore[reportUnusedFunction]
        auth: AuthContext = Depends(authenticated),
    ) -> SettingsResponse:
        return settings_response(auth.policy)

    @application.put(
        "/api/v1/settings",
        response_model=SettingsUpdateResponse,
        dependencies=[Depends(require_admin)],
    )
    @application.post(
        "/api/v1/settings",
        response_model=SettingsUpdateResponse,
        dependencies=[Depends(require_admin)],
    )
    def update_settings(  # pyright: ignore[reportUnusedFunction]
        request: SettingsUpdateRequest,
    ) -> SettingsUpdateResponse:  # pyright: ignore[reportUnusedFunction]
        nonlocal display_settings
        updates = request.model_dump(exclude_none=True, mode="json")
        if not updates:
            raise HTTPException(status_code=400, detail="no settings supplied")
        update_persistent_config(updates)
        # Keep operational settings immutable until restart.  Only the settings
        # display is refreshed, so REST and MCP continue to share one policy.
        display_settings = Settings.model_validate(
            settings.model_dump(mode="json") | updates
        )
        activity_service.record(
            "config",
            {"changed_keys": sorted(updates), "restart_required": True},
        )
        return SettingsUpdateResponse(
            saved=True,
            restart_required=True,
            message="Settings saved. Restart the service to apply them.",
            settings=settings_response(),
        )

    @application.post("/query")
    async def query_submit(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        q: str = Form(...),
        project: str = Form(""),
        token: str = Form(""),
    ) -> Response:
        """Render the form's results swept to the caller's read policy.

        Browser forms cannot set headers, so the bearer token travels in the
        plain ``token`` form field; the Authorization header is a fallback for
        programmatic callers. A missing or invalid token is a 401 like the
        other REST surfaces — the unauthenticated internal bypass is gone.
        """
        _require_ui_page(request)
        candidate = token.strip() or extract_bearer_header(request) or ""
        if candidate and not application.state.api_enabled:
            raise TokenAuthError(
                403,
                {"detail": "REST API disabled", "api_enabled": False},
            )
        record = token_service.verify(candidate)
        internal_token = None
        if record is None and not candidate:
            auth = await authenticated(request)
            policy = auth.policy
            internal_token = hlm_internal_request.set(True)
        else:
            if record is None:
                raise TokenAuthError(
                    401, {"detail": "authentication required", "token_required": True}
                )
            policy = AccessPolicy(record.rules, record.admin)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/queries",
                json={"query": q, "active_project": project or None},
                headers={"Authorization": f"Bearer {candidate}"},
            )
        if internal_token is not None:
            hlm_internal_request.reset(internal_token)
        if resp.status_code != 200:
            return Response(
                status_code=resp.status_code,
                content=resp.content,
                media_type=resp.headers.get("content-type"),
            )
        results = resp.json()
        results["selected_memories"] = [
            _sweep_memory_reasons(policy, memory)
            for memory in results["selected_memories"]
            if policy.can_read(memory["path"])
        ]
        return HTMLResponse(
            jinja_env.get_template("query.html").render(
                q=q, project=project, results=results
            )
        )

    def _get_current_version() -> str:
        try:
            return importlib_metadata.version("harbor-ledger-memory")
        except importlib_metadata.PackageNotFoundError:
            return "unknown"

    def _check_update_available() -> tuple[bool, str]:
        """Return (update_available, latest_version)."""
        try:
            subprocess.run(
                ["git", "fetch"],
                check=True,
                capture_output=True,
                timeout=15,
            )
            local = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            remote = subprocess.run(
                ["git", "rev-parse", "origin/main"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            # Both empty means no remote tracking
            if not local or not remote:
                return False, _get_current_version()
            return local != remote, remote[:8] if remote else _get_current_version()
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            FileNotFoundError,
        ):
            return False, _get_current_version()

    @application.get(
        "/api/v1/update-status",
        response_model=UpdateStatusResponse,
        dependencies=[Depends(authenticated)],
    )
    def update_status() -> UpdateStatusResponse:  # pyright: ignore[reportUnusedFunction]
        current = _get_current_version()
        update_available, latest = _check_update_available()
        return UpdateStatusResponse(
            current_version=current,
            latest_version=latest,
            update_available=update_available,
        )

    @application.post(
        "/api/v1/update",
        response_model=UpdateResponse,
        dependencies=[Depends(require_admin)],
    )
    def trigger_update() -> UpdateResponse:  # pyright: ignore[reportUnusedFunction]
        try:
            subprocess.run(["git", "pull"], check=True, capture_output=True, timeout=30)
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--force-reinstall", "."],
                check=True,
                capture_output=True,
                timeout=60,
            )
            return UpdateResponse(success=True, message="Update applied successfully.")
        except subprocess.CalledProcessError as e:
            return UpdateResponse(
                success=False,
                message=f"Update failed: {e.stderr.decode() if e.stderr else str(e)}",
            )
        except subprocess.TimeoutExpired:
            return UpdateResponse(success=False, message="Update timed out.")
        except FileNotFoundError:
            return UpdateResponse(success=False, message="git not found in PATH.")

    return application


def _loopback_origin(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{display_host}:{port}"


__all__ = [
    "CacheStatusResponse",
    "ActivityEventResponse",
    "ActivityHistoryResponse",
    "GraphEdgeResponse",
    "GraphNodeResponse",
    "GraphSnapshotResponse",
    "HealthResponse",
    "McpHealthResponse",
    "QueryResult",
    "StatusResponse",
    "ScanResponse",
    "SettingsResponse",
    "SettingsUpdateRequest",
    "SettingsUpdateResponse",
    "TokenCreateBody",
    "TraceReplayResponse",
    "TraceSelectionResponse",
    "TraceVisitResponse",
    "UpdateResponse",
    "UpdateStatusResponse",
    "WriteProposalResponse",
    "WriteRequest",
    "WritesListResponse",
    "create_app",
]


def _activity_response(
    event: ActivityMessage, payload: Mapping[str, Any]
) -> ActivityEventResponse:
    envelope = event.as_dict()
    if payload is not event.payload:
        envelope["payload"] = dict(payload)
    return ActivityEventResponse.model_validate(envelope)


def _activity_payload_readable(
    policy: AccessPolicy, payload: Mapping[str, Any]
) -> bool:
    """True when every path-bearing payload field is readable by the token.

    Events expose vault paths in the top-level ``path`` field, the
    ``graph_refs`` and ``selected_paths`` lists, and the ``source``/``target``
    endpoints of each ``graph_path`` edge segment. An event is hidden from
    the caller when any of those paths is unreadable.
    """
    path = payload.get("path")
    if isinstance(path, str) and not policy.can_read(path):
        return False
    for field in ("graph_refs", "selected_paths"):
        refs = payload.get(field)
        if isinstance(refs, Sequence) and not isinstance(refs, str):
            for ref in cast(Sequence[object], refs):
                if isinstance(ref, str) and not policy.can_read(ref):
                    return False
    graph_path = payload.get("graph_path")
    if isinstance(graph_path, Sequence) and not isinstance(graph_path, str):
        for segment in cast(Sequence[object], graph_path):
            if not isinstance(segment, Mapping):
                continue
            endpoints = cast(Mapping[str, Any], segment)
            for endpoint in (endpoints.get("source"), endpoints.get("target")):
                if isinstance(endpoint, str) and not policy.can_read(endpoint):
                    return False
    return True


def _sweep_activity_payload(
    policy: AccessPolicy, event_type: str, payload: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Sweep one event payload to the token's readable paths."""
    if not _activity_payload_readable(policy, payload):
        return None
    if event_type != "token_created":
        return payload
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list):
        return payload
    swept_rules: list[Any] = []
    for rule in cast("list[Any]", raw_rules):
        if isinstance(rule, Mapping):
            path = cast(Mapping[str, Any], rule).get("path")
            if isinstance(path, str) and not policy.can_read(path):
                continue
        swept_rules.append(rule)
    if swept_rules == raw_rules:
        return payload
    return {**payload, "rules": swept_rules}


def _sweep_memory_reasons(
    policy: AccessPolicy, memory: dict[str, Any]
) -> dict[str, Any]:
    """Drop hop reasons whose activation source the token cannot read.

    The note itself passed the path sweep; only the ``via_path`` endpoint
    embedded in hop-based reasons can still leak an unreadable path.
    """
    raw_reasons = memory.get("reasons")
    if not isinstance(raw_reasons, list):
        return memory
    reasons = cast("list[Any]", raw_reasons)
    swept: list[Any] = [
        reason
        for reason in reasons
        if not isinstance(reason, str) or _hop_reason_readable(policy, reason)
    ]
    return {**memory, "reasons": swept}


def _hop_reason_readable(policy: AccessPolicy, reason: str) -> bool:
    """True when a reason names no unreadable activation source."""
    if reason.startswith("One hop from "):
        return policy.can_read(reason.removeprefix("One hop from "))
    if reason.startswith("Activated from "):
        via = reason.removeprefix("Activated from ").split(" at ", 1)[0]
        return policy.can_read(via)
    return True


def _swept_sse_frame(policy: AccessPolicy, frame: str) -> str | None:
    """Sweep one SSE frame to the token's readable paths."""
    lines = frame.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("data: "):
            continue
        data = json.loads(line.removeprefix("data: "))
        if not isinstance(data, Mapping):
            return frame
        envelope = cast(Mapping[str, Any], data)
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            return frame
        swept = _sweep_activity_payload(
            policy,
            str(envelope.get("event_type", "")),
            cast(Mapping[str, Any], payload),
        )
        if swept is None:
            return None
        if swept is payload:
            return frame
        rebuilt = cast("dict[str, Any]", data)
        rebuilt["payload"] = dict(swept)
        lines[index] = "data: " + json.dumps(
            rebuilt, ensure_ascii=False, sort_keys=True
        )
        return "\n".join(lines) + "\n"
    return frame


def _write_response(proposal: MemoryWriteProposal) -> WriteProposalResponse:
    return WriteProposalResponse(
        id=proposal.id,
        path=proposal.path,
        content=proposal.content,
        operation=proposal.operation,
        status=proposal.status,
        rule_access=proposal.rule_access,
        requested_at=proposal.requested_at,
        resolved_at=proposal.resolved_at,
        failure_reason=proposal.failure_reason,
        affected_paths=proposal.affected_paths,
        created_paths=proposal.created_paths,
    )


def _mutation_affected_paths(
    service: VaultMutationService,
    path: str,
    stored_paths: Sequence[str] = (),
) -> tuple[PurePosixPath, ...]:
    """Return current and persisted identities covered by a mutation."""
    identity = service._validate_path(path)
    paths = list(service._affected_paths(identity))
    for stored in stored_paths:
        candidate = PurePosixPath(stored)
        if candidate not in paths:
            paths.append(candidate)
    return tuple(paths)


def _require_mutation_paths_writable(
    auth: AuthContext, paths: Sequence[PurePosixPath]
) -> None:
    """Preflight every path a mutation may create or touch."""
    denied = False
    for path in paths:
        if not auth.policy.can_propose(path):
            denied = True
    if denied:
        # Missing parents must still be checked above, while the denial shown
        # to the caller remains the requested target path.
        require_writable(auth, paths[-1].as_posix())


def _scan_activity_payload(result: object) -> dict[str, object]:
    return {
        "files_indexed": int(getattr(result, "files_indexed", 0)),
        "broken_links": int(getattr(result, "broken_links", 0)),
        "ambiguous_links": int(getattr(result, "ambiguous_links", 0)),
        "diagnostics": len(getattr(result, "diagnostics", ())),
    }
