from pathlib import Path

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.config import Settings
from harbor_ledger_memory.graph.builder import GraphBuilder
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.services.status import GraphService
from harbor_ledger_memory.vault.boundary import VaultBoundary


def test_graph_marks_explicit_wikilink(tmp_path: Path) -> None:
    ai = tmp_path / "AI"
    (ai / "Knowledge").mkdir(parents=True)
    (ai / "INDEX.md").write_text(
        "---\ntype: root\nstatus: active\n---\n[[AI/Knowledge/INDEX]]\n",
        encoding="utf-8",
    )
    (ai / "Knowledge" / "INDEX.md").write_text(
        "---\ntype: index\nstatus: active\n---\n# Knowledge\n",
        encoding="utf-8",
    )
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    session = CatalogSession(bind=engine)
    try:
        ScanService(
            VaultBoundary(Settings(HLM_VAULT_PATH=tmp_path)), session
        ).full_scan()
        graph = GraphBuilder(session).build()
        edge = graph.get_edge_data("AI/INDEX.md", "AI/Knowledge/INDEX.md")
        assert edge is not None
        assert any(
            values["edge_type"] == "links_to" and values["explicit"] is True
            for values in edge.values()
        )
        assert graph.nodes["AI/INDEX.md"]["node_type"] == "Root"
        assert graph.nodes["AI/Knowledge/INDEX.md"]["node_type"] == "Index"
    finally:
        session.close()
        engine.dispose()


def test_graph_rebuild_is_equivalent_after_note_change(tmp_path: Path) -> None:
    ai = tmp_path / "AI"
    (ai / "Knowledge").mkdir(parents=True)
    (ai / "INDEX.md").write_text("# Root\n[[AI/Knowledge/example]]\n", encoding="utf-8")
    note = ai / "Knowledge" / "example.md"
    note.write_text(
        "---\ntype: knowledge\nstatus: active\n---\n# One\n", encoding="utf-8"
    )
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    session = CatalogSession(bind=engine)
    try:
        service = ScanService(VaultBoundary(Settings(HLM_VAULT_PATH=tmp_path)), session)
        service.full_scan()
        note.write_text(
            "---\ntype: knowledge\nstatus: active\n---\n# Two\n", encoding="utf-8"
        )
        service.full_scan()
        actual = set(GraphBuilder(session).build().edges(keys=True))

        clean_engine = create_database(f"sqlite:///{tmp_path / 'clean.db'}")
        clean_session = CatalogSession(bind=clean_engine)
        try:
            ScanService(
                VaultBoundary(Settings(HLM_VAULT_PATH=tmp_path)), clean_session
            ).full_scan()
            expected = set(GraphBuilder(clean_session).build().edges(keys=True))
        finally:
            clean_session.close()
            clean_engine.dispose()
        assert actual == expected
    finally:
        session.close()
        engine.dispose()


def test_graph_service_returns_deterministic_neighbours(tmp_path: Path) -> None:
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "INDEX.md").write_text("# Root\n", encoding="utf-8")
    (ai / "child.md").write_text("# Child\n", encoding="utf-8")
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    session = CatalogSession(bind=engine)
    try:
        ScanService(
            VaultBoundary(Settings(HLM_VAULT_PATH=tmp_path)), session
        ).full_scan()
        neighbours = GraphService(session).neighbours("AI/INDEX.md")
        assert neighbours[0].path == "AI/child.md"
        assert neighbours[0].edge_type == "contains"
    finally:
        session.close()
        engine.dispose()
