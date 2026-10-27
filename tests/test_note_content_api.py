from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from harbor_ledger_memory.api.app import MAX_NOTE_BYTES, create_app
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import Note
from harbor_ledger_memory.config import (
    ApiSettings,
    FolderAccess,
    FolderRule,
    McpSettings,
    Settings,
)
from harbor_ledger_memory.services.scan import ScanService


def test_note_content_read_is_bounded_authenticated_and_canonical(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    (vault / "AI").mkdir(parents=True)
    content = "# Café\nwith spaces\n"
    (vault / "AI" / "Café Notes.md").write_text(content, encoding="utf-8")
    settings = Settings(
        vault_path=vault,
        index_root=PurePosixPath("AI"),
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        api=ApiSettings(enabled=True),
        mcp=McpSettings(enabled=True),
    )
    app = create_app(settings)
    token = app.state.token_service.create(
        "reader",
        rules=(FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),),
    ).plaintext
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/notes/content?path=AI%2FCaf%C3%A9%20Notes.md",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "path": "AI/Café Notes.md",
            "content": content,
            "revision": hashlib.sha256(content.encode()).hexdigest(),
        }
        assert response.headers["cache-control"] == "no-store"
        for layers in (2, 3, 4):
            nested = "AI/note.md"
            for _ in range(layers):
                nested = quote(nested, safe="")
            assert (
                client.get(
                    f"/api/v1/notes/content?path={nested}",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 422
            )
        assert (
            client.get(
                "/api/v1/notes/content?path=AI%2Fmissing.md",
                headers={"Authorization": f"Bearer {token}"},
            ).status_code
            == 404
        )
        missing = client.get(
            "/api/v1/notes/content?path=AI%2Fmissing.md",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert missing.headers["cache-control"] == "no-store"
        assert (
            client.get(
                "/api/v1/notes/content?path=AI%2Fmissing.txt",
                headers={"Authorization": f"Bearer {token}"},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/v1/notes/content?path=AI%2FCaf%252F%C3%A9.md",
                headers={"Authorization": f"Bearer {token}"},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/v1/notes/content?path=AI%2FCaf%C3%A9%20Notes.md"
            ).status_code
            == 401
        )
        missing_auth = client.get("/api/v1/notes/content?path=AI%2Fmissing.md")
        assert missing_auth.status_code == 401
        assert missing_auth.headers["cache-control"] == "no-store"


def test_denied_and_missing_notes_are_indistinguishable_and_not_cached(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    (vault / "AI" / "Secret").mkdir(parents=True)
    (vault / "AI" / "Secret" / "private.md").write_text("private")
    settings = Settings(
        vault_path=vault,
        index_root=PurePosixPath("AI"),
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        api=ApiSettings(enabled=True),
        mcp=McpSettings(enabled=True),
    )
    app = create_app(settings)
    reader_rules = (FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),)
    denied_token = app.state.token_service.create(
        "reader-denied",
        rules=(
            *reader_rules,
            FolderRule(path=PurePosixPath("AI/Secret"), access=FolderAccess.NONE),
        ),
    ).plaintext
    allowed_token = app.state.token_service.create(
        "reader-allowed", rules=reader_rules
    ).plaintext
    with TestClient(app) as client:
        missing = client.get(
            "/api/v1/notes/content?path=AI%2Fabsent.md",
            headers={"Authorization": f"Bearer {allowed_token}"},
        )
        denied = client.get(
            "/api/v1/notes/content?path=AI%2FSecret%2Fprivate.md",
            headers={"Authorization": f"Bearer {denied_token}"},
        )
        assert (missing.status_code, missing.json()) == (
            denied.status_code,
            denied.json(),
        )
        assert missing.status_code == 404
        assert missing.headers["cache-control"] == "no-store"
        assert denied.headers["cache-control"] == "no-store"


def test_invalid_utf8_and_oversized_notes_return_errors(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "AI").mkdir(parents=True)
    (vault / "AI" / "invalid.md").write_bytes(b"\xff")
    (vault / "AI" / "large.md").write_bytes(b"x" * (MAX_NOTE_BYTES + 1))
    settings = Settings(
        vault_path=vault,
        index_root=PurePosixPath("AI"),
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        api=ApiSettings(enabled=True),
        mcp=McpSettings(enabled=True),
    )
    app = create_app(settings)
    token = app.state.token_service.create(
        "reader",
        rules=(FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),),
    ).plaintext
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        invalid = client.get(
            "/api/v1/notes/content?path=AI%2Finvalid.md", headers=headers
        )
        oversized = client.get(
            "/api/v1/notes/content?path=AI%2Flarge.md", headers=headers
        )
        assert invalid.status_code == 422
        assert oversized.status_code == 413
        assert invalid.headers["cache-control"] == "no-store"
        assert oversized.headers["cache-control"] == "no-store"


def test_note_content_works_without_startup_scan(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    (vault / "AI").mkdir(parents=True)
    content = "# Not cataloged\n"
    (vault / "AI" / "disk only.md").write_text(content, encoding="utf-8")
    settings = Settings(
        vault_path=vault,
        index_root=PurePosixPath("AI"),
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        api=ApiSettings(enabled=True),
        mcp=McpSettings(enabled=True),
    )
    app = create_app(settings)
    token = app.state.token_service.create(
        "reader",
        rules=(FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),),
    ).plaintext

    def no_scan(_self: ScanService) -> None:
        return None

    monkeypatch.setattr(ScanService, "full_scan", no_scan)
    with TestClient(app) as client:
        engine = create_database(settings.database_url)
        session = CatalogSession(bind=engine)
        assert session.query(Note).count() == 0
        response = client.get(
            "/api/v1/notes/content?path=AI%2Fdisk%20only.md",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["content"] == content
        assert session.query(Note).count() == 0
        session.close()
        engine.dispose()


def test_internal_io_error_is_not_reported_as_missing_note(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    (vault / "AI").mkdir(parents=True)
    (vault / "AI" / "note.md").write_text("# Note", encoding="utf-8")
    settings = Settings(
        vault_path=vault,
        index_root=PurePosixPath("AI"),
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        api=ApiSettings(enabled=True),
        mcp=McpSettings(enabled=True),
    )
    app = create_app(settings)
    token = app.state.token_service.create(
        "reader",
        rules=(FolderRule(path=PurePosixPath("AI"), access=FolderAccess.READ),),
    ).plaintext
    from harbor_ledger_memory.vault.boundary import VaultBoundary

    def fail_read(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated I/O failure")

    monkeypatch.setattr(VaultBoundary, "read_file", fail_read)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/notes/content?path=AI%2Fnote.md",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 500
