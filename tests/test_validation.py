from pathlib import Path

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import Note
from harbor_ledger_memory.config import Settings
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.services.validation import ValidationService
from harbor_ledger_memory.vault.boundary import VaultBoundary


def test_validation_reports_broken_links_and_orphans(tmp_path: Path) -> None:
    ai = tmp_path / "AI"
    (ai / "Knowledge").mkdir(parents=True)
    (ai / "INDEX.md").write_text("# Root\n[[missing]]\n", encoding="utf-8")
    (ai / "Knowledge" / "example.md").write_text(
        "---\n"
        "type: knowledge\nstatus: active\ntags: [memory]\n"
        "created: '2026-01-01'\nupdated: '2026-01-02'\n"
        "summary: Example\nparent: '[[AI/INDEX]]'\n---\n# Example\n",
        encoding="utf-8",
    )
    (ai / "orphan.md").write_text(
        "---\ntype: knowledge\nstatus: active\n---\n# Orphan\n", encoding="utf-8"
    )
    (ai / "SHORT-TERM.md").write_text("# Routes\n[[old-route]]\n", encoding="utf-8")
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    session = CatalogSession(bind=engine)
    try:
        ScanService(
            VaultBoundary(Settings(HLM_VAULT_PATH=tmp_path)), session
        ).full_scan()
        report = ValidationService(session).run()
        codes = {finding.code for finding in report.findings}
        assert {"link.broken", "note.orphan"} <= codes
        assert {
            "index.missing",
            "frontmatter.missing",
            "parent.index.missing",
            "short-term.stale",
        } <= codes
        missing_frontmatter = next(
            finding
            for finding in report.findings
            if finding.code == "frontmatter.missing"
        )
        assert "graph_color" in missing_frontmatter.evidence["missing"]
        assert all(
            finding.path is not None and finding.evidence is not None
            for finding in report.findings
        )
    finally:
        session.close()
        engine.dispose()


def test_validation_reports_ambiguous_links_unresolved_parents_and_missing_children(
    tmp_path: Path,
) -> None:
    ai = tmp_path / "AI"
    (ai / "One").mkdir(parents=True)
    (ai / "Two").mkdir()
    (ai / "INDEX.md").write_text(
        "# Root\n[[same]]\n[[missing-child]]\n", encoding="utf-8"
    )
    (ai / "One" / "same.md").write_text("# One\n", encoding="utf-8")
    (ai / "Two" / "same.md").write_text("# Two\n", encoding="utf-8")
    complete = (
        "---\n"
        "type: knowledge\nstatus: active\ntags: [memory]\n"
        "created: '2026-01-01'\nupdated: '2026-01-02'\n"
        "summary: Example\ngraph_color: '#123456'\n"
    )
    (ai / "unresolved.md").write_text(
        complete + "parent: '[[AI/missing-parent]]'\n---\n# Unresolved\n",
        encoding="utf-8",
    )
    (ai / "child.md").write_text(
        complete + "parent: '[[AI/INDEX]]'\n---\n# Child\n", encoding="utf-8"
    )

    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    session = CatalogSession(bind=engine)
    try:
        ScanService(
            VaultBoundary(Settings(HLM_VAULT_PATH=tmp_path)), session
        ).full_scan()
        report = ValidationService(session).run()
        codes = {finding.code for finding in report.findings}

        assert {
            "link.ambiguous",
            "index.link.missing",
            "parent.unresolved",
            "parent.index.missing",
        } <= codes
    finally:
        session.close()
        engine.dispose()


def test_validation_reports_duplicate_normalized_paths(tmp_path: Path) -> None:
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    session = CatalogSession(bind=engine)
    try:
        session.add_all(
            [
                Note(path="AI/Case.md", title="Case", content="one"),
                Note(path="AI/case.md", title="case", content="two"),
            ]
        )
        session.commit()

        report = ValidationService(session).run()
        finding = next(
            finding for finding in report.findings if finding.code == "path.duplicate"
        )

        assert finding.evidence["paths"] == ["AI/Case.md", "AI/case.md"]
    finally:
        session.close()
        engine.dispose()
