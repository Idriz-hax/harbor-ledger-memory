"""Shared pytest configuration for the project tests."""

import tempfile
import uuid
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.catalog.database import resolve_database_url
from harbor_ledger_memory.config import (
    ApiSettings,
    FolderAccess,
    FolderRule,
    Settings,
)
from harbor_ledger_memory.vault.boundary import VaultBoundary

#: Where relative database URLs (e.g. the default ``data/memory.db``) resolve.
_LIVE_CONFIG_ROOT = Path.home() / ".config" / "harbor-ledger-memory"


@pytest.fixture
def mock_embedding_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from harbor_ledger_memory.services import embeddings as embeddings_module

    monkeypatch.setattr(
        embeddings_module,
        "embedding_runtime_available",
        lambda: True,
    )


@pytest.fixture
def boundary(tmp_path: Path) -> VaultBoundary:
    """Provide a boundary rooted in an isolated test vault."""
    (tmp_path / "AI").mkdir()
    return VaultBoundary(Settings(vault_path=tmp_path, index_root=PurePosixPath("AI")))


def _isolated_db_url(settings: Settings) -> str:
    """A database URL that never touches the live config store.

    Explicit isolated URLs (e.g. a ``tmp_path`` SQLite file the test seeds
    directly) are kept so the app and the test's own seeding share one
    database. Only URLs that resolve into the live config directory are
    redirected to an isolated per-call file — under the test's vault when
    available, else a fresh temp directory.
    """
    database = make_url(resolve_database_url(settings.database_url)).database
    if database is not None and database != ":memory:":
        path = Path(database).expanduser()
        if path.is_absolute() and path.is_relative_to(_LIVE_CONFIG_ROOT.resolve()):
            vault = Path(settings.vault_path)
            base = vault if vault.is_dir() else Path(tempfile.mkdtemp())
            return f"sqlite:///{base / 'test-memory.db'}"
    return settings.database_url


def authed_client(settings: Settings) -> TestClient:
    """Build an authenticated :class:`TestClient` for a ``Settings`` object.

    REST is always locked (Task 2), so functional tests need a caller. This
    creates an admin token via the app's token service and returns a
    ``TestClient`` whose default headers carry it. The returned client can be
    used directly or as a context manager (``with``). The token name is
    unique per call so tests that build two clients against the same database
    (e.g. to compare before/after state) never hit a duplicate-name error.

    The app's ``database_url`` is redirected away from the live config
    database so token creation never accumulates in (or locks) it.
    """
    settings = settings.model_copy(
        update={
            "api": ApiSettings(enabled=True),
            "database_url": _isolated_db_url(settings),
        }
    )
    app = create_app(settings)
    name = f"test-admin-{uuid.uuid4().hex}"
    token = app.state.token_service.create(
        name,
        rules=[FolderRule(path=PurePosixPath("."), access=FolderAccess.AUTO_WRITE)],
        admin=True,
    ).plaintext
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})
