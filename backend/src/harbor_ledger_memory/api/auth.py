"""Token authentication: REST dependency, contextvars, MCP middleware helpers.

Task 2: REST is always locked — zero active tokens no longer means open mode.
A valid bearer token resolves to an :class:`AuthContext` carrying the token's
per-folder :class:`AccessPolicy`; routes check that policy for write paths and
sweep read responses to what the policy can read.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import PurePosixPath

from fastapi import Depends, HTTPException, Request, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from harbor_ledger_memory.config import FolderAccess, FolderRule
from harbor_ledger_memory.services.access import AccessPolicy
from harbor_ledger_memory.services.tokens import TokenRecord, TokenService
from harbor_ledger_memory.services.ui_session import (
    UI_SESSION_COOKIE,
    UiSessionService,
)

#: Set while the app dispatches to itself (the HTML ``POST /query`` form
#: handler calling ``POST /api/v1/queries`` via ``httpx.ASGITransport``).
hlm_internal_request: ContextVar[bool] = ContextVar(
    "hlm_internal_request", default=False
)

#: The resolved MCP token record for the in-flight ``/mcp`` request; set by
#: :class:`McpTokenMiddleware` and read by per-tool scope guards.
hlm_mcp_token: ContextVar[TokenRecord | None] = ContextVar(
    "hlm_mcp_token", default=None
)

#: Flat 401 body for locked ``/mcp`` requests, with a hint that names the
#: remediation instead of leaving the client to guess why it is disabled.
MCP_401_PAYLOAD: dict[str, object] = {
    "detail": "authentication required",
    "token_required": True,
    "hint": (
        "No active API token matches this bearer token. Create one with "
        "`hlm token create` and set it as the MCP client's Authorization "
        "bearer."
    ),
}

# The in-process internal bypass (HTML form handler) is treated as full access;
# it never represents a remote caller. The ``.`` rule grants every path the
# highest access level (a bare admin flag no longer implies folder access).
_FULL_ACCESS = AccessPolicy(
    rules=(FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE),),
    admin=True,
)


class TokenAuthError(HTTPException):
    """401/403 with a flat JSON body (e.g. ``token_required``) that the
    UI keys off. Subclasses ``HTTPException`` so it composes with the
    existing exception-handling stack."""

    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        super().__init__(status_code=status_code, detail=dict(payload))


@dataclass(frozen=True)
class AuthContext:
    """Resolved caller identity for one request.

    ``record`` is the active token (``None`` for the internal bypass) and
    ``policy`` is its effective folder-access policy.
    """

    record: TokenRecord | None
    policy: AccessPolicy
    ui_session: str | None = None


async def token_auth_error_handler(request: Request, exc: Exception) -> Response:
    """Render :class:`TokenAuthError` with the flat body it carries."""
    assert isinstance(exc, TokenAuthError)
    return Response(
        status_code=exc.status_code,
        content=json.dumps(exc.detail),
        media_type="application/json",
    )


def _unauthenticated() -> TokenAuthError:
    return TokenAuthError(
        401, {"detail": "authentication required", "token_required": True}
    )


def _path_forbidden(path: str) -> TokenAuthError:
    return TokenAuthError(403, {"detail": f"path '{path}' not writable by this token"})


def _no_write_forbidden() -> TokenAuthError:
    return TokenAuthError(403, {"detail": "token has no write access"})


def _admin_forbidden() -> TokenAuthError:
    return TokenAuthError(403, {"detail": "token missing admin access"})


def extract_bearer_header(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        if token:
            return token
    return None


def _extract_token(request: Request) -> str | None:
    """Extract bearer credentials from the Authorization header only."""
    return extract_bearer_header(request)


def _is_rest_api(request: Request) -> bool:
    return request.url.path.startswith("/api/v1/")


def _cookie_session(request: Request) -> bool:
    service: UiSessionService = request.app.state.ui_session_service
    return (
        _is_rest_api(request) or request.url.path == "/query"
    ) and service.authenticate(request.cookies.get(UI_SESSION_COOKIE))


def _cookie_csrf_allowed(request: Request) -> bool:
    service: UiSessionService = request.app.state.ui_session_service
    session = request.cookies.get(UI_SESSION_COOKIE)
    expected = service.csrf(session)
    supplied = request.headers.get("X-HLM-CSRF")
    return expected is not None and supplied is not None and supplied == expected


def _cookie_origin_allowed(request: Request) -> bool:
    return request.headers.get("origin") == request.app.state.ui_origin


async def authenticated(request: Request) -> AuthContext:
    """Resolve the calling token and its policy (always locked).

    The in-process internal bypass resolves to full access. Otherwise a valid
    bearer token is required — zero active tokens no longer means open mode, so
    an absent or invalid token is a 401.
    """
    if hlm_internal_request.get():
        return AuthContext(None, _FULL_ACCESS)
    service: TokenService = request.app.state.token_service
    bearer = _extract_token(request)
    if _is_rest_api(request) and not request.app.state.api_enabled and bearer:
        raise TokenAuthError(
            403,
            {"detail": "REST API disabled", "api_enabled": False},
        )
    record = service.verify(bearer or "")
    if record is None:
        if bearer is None and _cookie_session(request):
            if (
                request.method not in {"GET", "HEAD", "OPTIONS"}
                and (
                    not _cookie_origin_allowed(request)
                    or not _cookie_csrf_allowed(request)
                )
            ):
                raise _unauthenticated()
            return AuthContext(
                None,
                UiSessionService.policy(),
                request.cookies.get(UI_SESSION_COOKIE),
            )
        raise _unauthenticated()
    return AuthContext(record, AccessPolicy(record.rules, record.admin))


def require_admin(auth: AuthContext = Depends(authenticated)) -> AuthContext:
    """Guard admin-only routes (token management, settings, update)."""
    if not auth.policy.is_admin:
        raise _admin_forbidden()
    return auth


def require_any_write(auth: AuthContext = Depends(authenticated)) -> AuthContext:
    """Guard routes that mutate the catalog (scan, feedback)."""
    if not auth.policy.has_any_write():
        raise _no_write_forbidden()
    return auth


def require_writable(auth: AuthContext, path: str) -> None:
    """Plain path check for a write/resolve target (not a FastAPI dependency).

    Raises :class:`TokenAuthError` (403 path body) when the policy cannot
    propose to ``path``.
    """
    if not auth.policy.can_propose(path):
        raise _path_forbidden(path)


def require_auto_writable(auth: AuthContext, path: str) -> None:
    """Plain path check for an auto-write target (not a FastAPI dependency).

    Raises :class:`TokenAuthError` (403 path body) when the policy cannot
    auto-write ``path`` (used when the global boundary auto-applies the write).
    """
    if not auth.policy.can_auto_write(path):
        raise _path_forbidden(path)


def bearer_from_scope(scope: Scope) -> str | None:
    """Read the bearer token from raw ASGI headers (mounted-sub-app requests)."""
    for key, value in scope.get("headers", []):
        if key == b"authorization":
            text = value.decode("latin-1")
            if text.lower().startswith("bearer "):
                token = text[7:].strip()
                if token:
                    return token
            return None
    return None


async def send_json(
    scope: Scope, receive: Receive, send: Send, status: int, payload: dict[str, object]
) -> None:
    """Write a complete JSON response at the ASGI level (no routing)."""
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode("ascii")],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class McpTokenMiddleware:
    """Bearer auth for the mounted ``/mcp`` sub-app (always locked).

    Parent dependencies never run for mounted routes, so enforcement lives
    here. Zero active tokens no longer opens the sub-app.
    """

    def __init__(self, app: ASGIApp, token_service: TokenService) -> None:
        self._app = app
        self._token_service = token_service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            token = bearer_from_scope(scope)
            record = self._token_service.verify(token) if token is not None else None
            if record is None:
                await send_json(scope, receive, send, 401, dict(MCP_401_PAYLOAD))
                return
            token_var = hlm_mcp_token.set(record)
            try:
                await self._app(scope, receive, send)
            finally:
                # The contextvar is in-flight request state; never leak it
                # past this request, even if the mounted app raises.
                hlm_mcp_token.reset(token_var)
            return
        await self._app(scope, receive, send)
