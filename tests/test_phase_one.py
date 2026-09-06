"""End-to-end Phase 1 verification using only an isolated fixture vault."""

from pathlib import Path

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.config import Settings
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.services.validation import ValidationService
from harbor_ledger_memory.vault.boundary import VaultBoundary


def test_phase_one_workflow_uses_only_the_ai_projection(tmp_path: Path) -> None:
    ai = tmp_path / "AI"
    (ai / "Knowledge").mkdir(parents=True)
    (ai / "INDEX.md").write_text("# Root\n[[AI/Knowledge/example]]\n", encoding="utf-8")
    (ai / "Knowledge" / "example.md").write_text(
        "---\ntype: knowledge\nstatus: active\n---\n# Example\n",
        encoding="utf-8",
    )
    sentinel = tmp_path / "do-not-index.md"
    sentinel.write_text("outside AI", encoding="utf-8")
    settings = Settings(
        HLM_VAULT_PATH=tmp_path,
        index_root="AI",
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
    )

    engine = create_database(settings.database_url)
    session = CatalogSession(bind=engine)
    try:
        scanner = ScanService(VaultBoundary(settings), session)
        result = scanner.full_scan()
        report = ValidationService(session).run()

        assert result.files_indexed > 0
        assert report is not None
        assert all(path.startswith("AI/") for path in result.indexed_paths)
        assert sentinel.exists()
    finally:
        session.close()
        engine.dispose()
