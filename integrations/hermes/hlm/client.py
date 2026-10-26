"""Small REST-only client for Harbor Ledger Memory."""

from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class HLMError(RuntimeError):
    """An HLM request failed or returned an invalid response."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> None:
        return None


class HLMClient:
    MAX_CONTENT_BYTES = 64 * 1024
    MAX_PATH_BYTES = 4 * 1024
    MAX_REQUEST_BYTES = 512 * 1024
    MAX_RESPONSE_BYTES = 8 * 1024 * 1024

    def __init__(self, base_url: str, token: str, timeout: float = 10) -> None:
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("HLM_BASE_URL must be an http(s) base URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("HLM_BASE_URL must not include user information")
        if parsed.query or parsed.fragment:
            raise ValueError("HLM_BASE_URL must not include query or fragment")
        if parsed.scheme == "http" and not self._is_loopback(parsed.hostname):
            raise ValueError("HLM_BASE_URL must use HTTPS for non-loopback hosts")
        self.base_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self._token = token
        self.timeout = timeout

    @staticmethod
    def _is_loopback(host: str) -> bool:
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        data = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        if data is not None and len(data) > self.MAX_REQUEST_BYTES:
            raise HLMError("HLM request exceeds 512KiB")
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with build_opener(_NoRedirect()).open(
                request, timeout=self.timeout
            ) as response:
                body = response.read(self.MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            if isinstance(exc, HTTPError):
                detail = f"HTTP {exc.code}"
            elif isinstance(exc, URLError):
                detail = "network error"
            else:
                detail = "request failed"
            raise HLMError(f"HLM {method} {path} failed: {detail}") from None
        if len(body) > self.MAX_RESPONSE_BYTES:
            raise HLMError(f"HLM {method} {path} response exceeds 8MiB")
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HLMError(f"HLM {method} {path} returned invalid JSON") from None

    def query(self, query: str) -> Any:
        result = self._request("POST", "/api/v1/queries", {"query": query})
        selected = result.get("selected_memories") if isinstance(result, dict) else None
        if not isinstance(selected, list) or any(
            not isinstance(item, dict)
            or any(
                not isinstance(item.get(key), str)
                for key in ("path", "title", "excerpt")
            )
            for item in selected
        ):
            raise HLMError("HLM query response has an invalid shape")
        return result

    def get_note(self, path: str) -> Any:
        result = self._request(
            "GET", "/api/v1/notes/content?" + urlencode({"path": path})
        )
        if not isinstance(result, dict) or any(
            not isinstance(result.get(key), str)
            for key in ("path", "content", "revision")
        ):
            raise HLMError("HLM note response has an invalid shape")
        return result

    def propose_write(self, path: str, content: str) -> Any:
        self._check_write_fields(path, content)
        result = self._request(
            "POST",
            "/api/v1/agent/writes",
            {"path": path, "content": content, "operation": "write"},
        )
        self._validate_proposal(result)
        return result

    def propose_folder(self, path: str) -> Any:
        self._check_write_fields(path, "")
        result = self._request(
            "POST",
            "/api/v1/agent/writes",
            {"path": path, "content": "", "operation": "mkdir"},
        )
        self._validate_proposal(result)
        return result

    @classmethod
    def _check_write_fields(cls, path: str, content: str) -> None:
        if len(path.encode("utf-8")) > cls.MAX_PATH_BYTES:
            raise HLMError("HLM proposal path exceeds 4KiB")
        if len(content.encode("utf-8")) > cls.MAX_CONTENT_BYTES:
            raise HLMError("HLM proposal content exceeds 64KiB")

    def pending_proposals(self, limit: int = 20, before_id: int | None = None) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if before_id is not None:
            params["before_id"] = before_id
        result = self._request(
            "GET", "/api/v1/agent/writes/pending?" + urlencode(params)
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("proposals"), list
        ):
            raise HLMError("HLM pending proposals response has an invalid shape")
        if any(not HLMClient._is_proposal(item) for item in result["proposals"]):
            raise HLMError("HLM pending proposals response has an invalid shape")
        cursor = result.get("next_before_id")
        if cursor is not None and (type(cursor) is not int or cursor < 1):
            raise HLMError("HLM pending proposals response has an invalid shape")
        return result

    @staticmethod
    def _validate_proposal(result: Any) -> None:
        if not HLMClient._is_proposal(result):
            raise HLMError("HLM proposal response has an invalid shape")

    @staticmethod
    def _is_proposal(result: Any) -> bool:
        return (
            isinstance(result, dict)
            and type(result.get("id")) is int
            and result["id"] > 0
            and result.get("operation") in {"create", "update", "mkdir"}
            and all(
                isinstance(result.get(key), str)
                for key in ("path", "status", "requested_at")
            )
        )
