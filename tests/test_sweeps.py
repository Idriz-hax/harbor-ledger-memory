"""Per-token read sweeps on the remaining surfaces.

Task 2 sweeps every read response to what the caller's token can read.
The status, graph, and query endpoints are covered in ``test_token_auth.py``
and the MCP tools in ``test_mcp_tokens.py``; this file pins the sweeps on
the surfaces that were still leaking paths: the write proposal list, trace
replay (including the ``via_path`` edge endpoint), activity history, the
SSE activity stream, and the HTML query form.

Every test compares a ``reader`` token (denies the ``Secret`` folder)
against an ``admin`` token (reads and writes everything) over the same
vault and database, so a passing sweep is proven by the control seeing
the secret paths.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.config import (
    ApiSettings,
    FolderAccess,
    FolderRule,
    Settings,
)
from harbor_ledger_memory.services.tokens import TokenService

_QUERY = "memory systems"
_SECRET_PREFIX = "Secret/"
_VISIBLE_WRITE = "AI/writes-visible.md"
_HIDDEN_WRITE = "Secret/writes-hidden.md"


def _make_app(tmp_path: Path) -> tuple[FastAPI, str]:
    """Build an app whose vault exposes AI (readable) and Secret (denied).

    ``vault_path=`` (not the ``HLM_VAULT_PATH`` env alias used by
    test_api.py): accepted identically at runtime via
    ``populate_by_name=True`` and keeps this file pyright-clean.
    """
    rules = (
        FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),
        FolderRule(path=PurePosixPath("Secret"), access=FolderAccess.PROPOSE_WRITE),
    )
    for rule in rules:
        (tmp_path / rule.path.as_posix()).mkdir(parents=True, exist_ok=True)
    settings = Settings(
        vault_path=tmp_path,
        folder_rules=rules,
        database_url=f"sqlite:///{tmp_path / 'sweeps.db'}",
    )
    settings = settings.model_copy(update={"api": ApiSettings(enabled=True)})
    return create_app(settings), f"sqlite:///{tmp_path / 'sweeps.db'}"


def _headers(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


def _seed_vault_files(tmp_path: Path) -> None:
    """Four notes: two query-matching, two reachable only by activation.

    ``bridge`` and ``deep`` carry no query tokens and no matching tags, so
    they can only appear in results through the links out of
    ``Secret/hidden`` — which makes them deterministic carriers of
    ``via_path == "Secret/hidden.md"`` in the trace activation graph.
    """
    (tmp_path / "AI" / "Knowledge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Secret").mkdir(parents=True, exist_ok=True)
    (tmp_path / "AI" / "Knowledge" / "visible.md").write_text(
        "---\ntype: knowledge\ntags: [memory]\nstatus: active\n---\n"
        "# Visible\nMemory systems and retrieval fundamentals.\n"
        "[[Secret/hidden]]\n",
        encoding="utf-8",
    )
    (tmp_path / "Secret" / "hidden.md").write_text(
        "---\ntype: knowledge\ntags: [memory]\nstatus: active\n---\n"
        "# Hidden\nMemory systems and retrieval private notes.\n"
        "[[AI/bridge]]\n[[Secret/deep]]\n",
        encoding="utf-8",
    )
    (tmp_path / "AI" / "bridge.md").write_text(
        "---\ntype: knowledge\nstatus: active\n---\n"
        "# Bridge\nActivation bridge note.\n",
        encoding="utf-8",
    )
    (tmp_path / "Secret" / "deep.md").write_text(
        "---\ntype: knowledge\nstatus: active\n---\n# Deep\nDeep private note.\n",
        encoding="utf-8",
    )


def _make_tokens(db: str) -> tuple[str, str]:
    """Create (admin, reader) plaintext tokens over one database."""
    service = TokenService(db)
    _, admin = service.create(
        "admin",
        rules=[FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE)],
        admin=True,
    )
    _, reader = service.create(
        "reader",
        rules=[FolderRule(path=PurePosixPath("Secret"), access=FolderAccess.NONE)],
    )
    return admin, reader


def _request_writes(client: TestClient, admin: str) -> None:
    """Queue one create proposal in each folder as the admin token."""
    for path in (_VISIBLE_WRITE, _HIDDEN_WRITE):
        created = client.post(
            "/api/v1/writes",
            json={"path": path, "content": "# Write proposal\n"},
            headers=_headers(admin),
        )
        assert created.status_code == 200


def test_writes_list_sweeps_unreadable_proposals(tmp_path: Path) -> None:
    """The proposal list hides paths the token cannot read."""
    app, db = _make_app(tmp_path)
    admin, reader = _make_tokens(db)
    with TestClient(app) as client:
        _request_writes(client, admin)

        full = client.get("/api/v1/writes", headers=_headers(admin)).json()
        assert {proposal["path"] for proposal in full["proposals"]} == {
            _VISIBLE_WRITE,
            _HIDDEN_WRITE,
        }

        swept = client.get("/api/v1/writes", headers=_headers(reader)).json()
        assert [proposal["path"] for proposal in swept["proposals"]] == [_VISIBLE_WRITE]


def test_trace_replay_sweeps_unreadable_paths_and_edges(tmp_path: Path) -> None:
    """Trace replay hides unreadable selections and edge endpoints."""
    app, db = _make_app(tmp_path)
    _seed_vault_files(tmp_path)
    admin, reader = _make_tokens(db)
    with TestClient(app) as client:
        query = client.post(
            "/api/v1/queries",
            json={"query": _QUERY},
            headers=_headers(admin),
        )
        assert query.status_code == 200
        trace_id = query.json()["trace_id"]

        full = client.get(f"/api/v1/traces/{trace_id}", headers=_headers(admin)).json()
        full_selected = {selection["path"] for selection in full["selected_paths"]}
        assert "Secret/hidden.md" in full_selected
        # Sanity: hidden drives activation of both neighbours, so the
        # via_path edge rule has something to sweep on each side.
        via_hidden = {
            visit["path"]
            for visit in full["activation_graph"]
            if visit["via_path"] == "Secret/hidden.md"
        }
        assert {"AI/bridge.md", "Secret/deep.md"} <= via_hidden

        swept = client.get(
            f"/api/v1/traces/{trace_id}", headers=_headers(reader)
        ).json()
        assert swept["trace_id"] == trace_id

        def readable(path: str | None) -> bool:
            return path is None or not path.startswith(_SECRET_PREFIX)

        assert swept["selected_paths"] == [
            selection
            for selection in full["selected_paths"]
            if readable(selection["path"])
        ]
        assert swept["activation_graph"] == [
            visit
            for visit in full["activation_graph"]
            if readable(visit["path"]) and readable(visit["via_path"])
        ]
        # The sweep removed entries from both lists, so the reader's view
        # is narrower by construction, not by an empty vault.
        assert len(swept["selected_paths"]) < len(full["selected_paths"])
        assert len(swept["activation_graph"]) < len(full["activation_graph"])


def test_activity_history_sweeps_unreadable_events(tmp_path: Path) -> None:
    """Activity history hides events whose payload paths are unreadable."""
    app, db = _make_app(tmp_path)
    _seed_vault_files(tmp_path)
    admin, reader = _make_tokens(db)
    with TestClient(app) as client:
        _request_writes(client, admin)
        query = client.post(
            "/api/v1/queries",
            json={"query": _QUERY},
            headers=_headers(admin),
        )
        assert query.status_code == 200

        full = client.get("/api/v1/activity", headers=_headers(admin)).json()
        # Control: the trail references Secret paths (scan graph_refs, the
        # Secret proposal, the query's selections), so a reader that sees
        # none of them is being swept, not served an empty vault.
        assert any("Secret/" in json.dumps(event) for event in full["events"])

        swept = client.get("/api/v1/activity", headers=_headers(reader)).json()
        assert swept["events"], "readable events must survive the sweep"
        for event in swept["events"]:
            assert "Secret/" not in json.dumps(event)
        # The readable write is still audited for the reader.
        assert any(
            event["event_type"] == "vault.mutation.requested"
            and event["payload"].get("path") == _VISIBLE_WRITE
            for event in swept["events"]
        )


def test_activity_history_sweeps_token_created_rules(tmp_path: Path) -> None:
    """token_created events hide rules on paths the observer cannot read.

    A readable token must not learn another token's hidden-folder grants:
    the event itself stays visible (its name is not a path), but rules on
    unreadable folders disappear from the served payload.
    """
    app, db = _make_app(tmp_path)
    admin, reader = _make_tokens(db)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/tokens",
            json={
                "name": "swept",
                "rules": [
                    {"path": "AI", "access": "read"},
                    {"path": "Secret", "access": "propose-write"},
                ],
            },
            headers=_headers(admin),
        )
        assert created.status_code == 201

        full = client.get("/api/v1/activity", headers=_headers(admin)).json()
        admin_created = [
            event for event in full["events"] if event["event_type"] == "token_created"
        ]
        assert len(admin_created) == 1
        # Control: the admin history carries both rules, so the reader's
        # sweep has something to remove.
        assert admin_created[0]["payload"]["rules"] == [
            {"path": "AI", "access": "read"},
            {"path": "Secret", "access": "propose-write"},
        ]

        swept = client.get("/api/v1/activity", headers=_headers(reader)).json()
        reader_created = [
            event for event in swept["events"] if event["event_type"] == "token_created"
        ]
        assert len(reader_created) == 1, "the event stays visible to the reader"
        # The rule on the reader's hidden folder disappears; the readable
        # one survives.
        assert reader_created[0]["payload"]["rules"] == [
            {"path": "AI", "access": "read"}
        ]
        assert "Secret" not in json.dumps(reader_created)


def test_sse_stream_sweeps_unreadable_events(tmp_path: Path) -> None:
    """The SSE stream sweeps both the history replay and live frames.

    The stream is driven directly as a background task on the client
    portal, mirroring ``test_api.py``: TestClient's transport runs the
    ASGI app to completion, so an infinite SSE body cannot stream
    incrementally through it.
    """
    app, db = _make_app(tmp_path)
    _seed_vault_files(tmp_path)
    admin, reader = _make_tokens(db)

    payloads: list[dict[str, Any]] = []
    visible_requested = threading.Event()

    def handle_frame(body: bytes) -> None:
        for line in body.decode("utf-8").splitlines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line.removeprefix("data: "))
            payloads.append(payload)
            if (
                payload.get("event_type") == "vault.mutation.requested"
                and payload.get("payload", {}).get("path") == _VISIBLE_WRITE
            ):
                visible_requested.set()

    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/v1/activity/stream",
        "raw_path": b"/api/v1/activity/stream",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {reader}".encode("ascii"))],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "extensions": {"http.response.debug": {}},
        "state": {},
    }

    client = TestClient(app, headers=_headers(admin))
    with client:
        _request_writes(client, admin)
        portal = client.portal
        assert portal is not None
        disconnect = portal.call(asyncio.Event)

        async def run_stream() -> None:
            async def receive() -> dict[str, str]:
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    assert message["status"] == 200
                elif message["type"] == "http.response.body":
                    handle_frame(bytes(message.get("body", b"")))

            await app(scope, receive, send)

        stream_future = portal.start_task_soon(run_stream)
        try:
            assert visible_requested.wait(10), (
                "SSE stream did not deliver the readable proposal event"
            )
        finally:
            # Always stop the stream: on a failed assertion the task would
            # otherwise await ``disconnect`` forever and block portal
            # shutdown when the client exits.
            portal.call(disconnect.set)
            try:
                stream_future.result(timeout=10)
            except Exception:
                # The test already failed; a stream-task error must not
                # mask the original assertion.
                pass

    assert any(
        payload.get("event_type") == "vault.mutation.requested"
        and payload.get("payload", {}).get("path") == _VISIBLE_WRITE
        for payload in payloads
    )
    for payload in payloads:
        assert "Secret/" not in json.dumps(payload)


def test_query_form_sweeps_unreadable_results(tmp_path: Path) -> None:
    """The HTML query form renders only the caller's readable memories."""
    app, db = _make_app(tmp_path)
    _seed_vault_files(tmp_path)
    admin, reader = _make_tokens(db)
    with TestClient(app) as client:
        admin_form = client.post(
            "/query",
            data={"q": _QUERY, "project": "", "token": admin},
        )
        assert admin_form.status_code == 200
        assert "AI/Knowledge/visible.md" in admin_form.text
        assert "Secret/hidden.md" in admin_form.text

        reader_form = client.post(
            "/query",
            data={"q": _QUERY, "project": "", "token": reader},
        )
        assert reader_form.status_code == 200
        assert "AI/Knowledge/visible.md" in reader_form.text
        # The visible note's excerpt still shows the [[Secret/hidden]]
        # wikilink (no ".md"), so assert the rendered memory path, not a
        # bare folder prefix.
        assert "Secret/hidden.md" not in reader_form.text


def test_query_form_renders_token_input(tmp_path: Path) -> None:
    """The rendered query form carries the token field POST /query needs.

    The form authenticates through the plain ``token`` form field, so the
    template must render that input — without it, ordinary browser
    submissions always 401.
    """
    app, _ = _make_app(tmp_path)
    with TestClient(app) as client:
        form = client.get("/query")
        assert form.status_code == 200
        assert 'name="token"' in form.text
