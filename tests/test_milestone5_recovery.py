from pathlib import Path, PurePosixPath

from sqlalchemy import create_engine, inspect, text

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.config import Settings
from harbor_ledger_memory.migrate import upgrade_to_head
from harbor_ledger_memory.services.scan import (
    ScanResult,
    ScanService,
    projection_identity,
    visible_scan_paths,
)
from harbor_ledger_memory.services.vault_mutations import proposal_diff
from harbor_ledger_memory.vault.boundary import VaultBoundary


def test_scan_projection_identity_is_deterministic() -> None:
    result = ScanResult(
        files_indexed=2,
        broken_links=0,
        diagnostics=(),
        indexed_paths=("b.md", "a.md"),
        content_hashes={"b.md": "2", "a.md": "1"},
        links={"b.md": ("a.md",)},
    )
    assert projection_identity(result) == projection_identity(result)
    def can_read(path: str) -> bool:
        return path.startswith("Public/")

    assert visible_scan_paths(
        ("Public/a.md", "Private/b.md"), can_read
    ) == ("Public/a.md",)


def test_proposal_diff_is_unified_and_safe() -> None:
    assert "-old" in proposal_diff("note.md", "old\n", "new\n")
    assert "+new" in proposal_diff("note.md", "old\n", "new\n")


def test_untracked_old_proposal_table_gets_base_diff_before_stamp(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE memory_write_proposals ("
                "id INTEGER PRIMARY KEY, path VARCHAR(1024) NOT NULL, "
                "content TEXT NOT NULL, operation VARCHAR(16) NOT NULL, "
                "status VARCHAR(16) NOT NULL, rule_access VARCHAR(32) NOT NULL, "
                "requested_at VARCHAR(128) NOT NULL, expected_source_hash VARCHAR(64), "
                "resolved_at VARCHAR(128), failure_reason TEXT, "
                "creator_token_id INTEGER, "
                "applying_at VARCHAR(128), applied_content_hash VARCHAR(64), "
                "affected_paths_json TEXT NOT NULL, created_paths_json TEXT NOT NULL)"
            )
        )
    engine.dispose()
    upgrade_to_head(database_url)
    upgrade_to_head(database_url)
    engine = create_engine(database_url)
    try:
        assert "base_diff" in {
            column["name"]
            for column in inspect(engine).get_columns("memory_write_proposals")
        }
    finally:
        engine.dispose()


def test_rebuild_is_equivalent_and_does_not_modify_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "AI").mkdir(parents=True)
    note = vault / "AI" / "note.md"
    note.write_bytes(b"# Note\n\n[[AI/other]]\n")
    other = vault / "AI" / "other.md"
    other.write_bytes(b"# Other\n")
    before = {path: path.read_bytes() for path in (note, other)}
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    session = CatalogSession(bind=engine)
    try:
        service = ScanService(
            VaultBoundary(Settings(vault_path=vault, index_root=PurePosixPath("AI"))),
            session,
        )
        first = service.full_scan()
        second = service.full_scan()
        assert projection_identity(first) == projection_identity(second)
        assert {path: path.read_bytes() for path in (note, other)} == before
    finally:
        session.close()
        engine.dispose()
