"""Endpoint tests for GET /api/v1/graph — structural graph snapshot."""

from pathlib import Path

from conftest import authed_client
from harbor_ledger_memory.config import Settings


def _settings(tmp_path: Path) -> Settings:
    """Build settings for a temporary vault."""
    db_path = tmp_path / "catalog.db"
    return Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{db_path}",
    )


def test_graph_endpoint_returns_empty_snapshot(tmp_path: Path) -> None:
    """Empty vault returns nodes=[], edges=[], generation present."""
    (tmp_path / "AI").mkdir()
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp = client.get("/api/v1/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data
        assert "generation" in data
        assert data["nodes"] == []
        assert data["edges"] == []
        assert isinstance(data["generation"], str)


def test_graph_includes_isolated_notes(tmp_path: Path) -> None:
    """Notes with no links appear with isolated=true."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "orphan.md").write_text("# Orphan note\nNo links here.", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp = client.get("/api/v1/graph")
        assert resp.status_code == 200
        data = resp.json()
        paths = {node["path"] for node in data["nodes"]}
        assert "AI/orphan.md" in paths
        orphan = next(n for n in data["nodes"] if n["path"] == "AI/orphan.md")
        assert orphan["isolated"] is True
        assert isinstance(orphan["title"], str)


def test_graph_excludes_isolated_flag_for_connected_notes(tmp_path: Path) -> None:
    """Notes that have edges should have isolated=false."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "parent.md").write_text("# Parent\n[[child]]", encoding="utf-8")
    (ai / "child.md").write_text("# Child", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp = client.get("/api/v1/graph")
        assert resp.status_code == 200
        data = resp.json()
        parent = next(n for n in data["nodes"] if n["path"] == "AI/parent.md")
        child = next(n for n in data["nodes"] if n["path"] == "AI/child.md")
        assert parent["isolated"] is False
        assert child["isolated"] is False


def test_graph_contains_wikilink_edges(tmp_path: Path) -> None:
    """Resolved wikilinks produce links_to edges with explicit=true."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "a.md").write_text("# A\n[[b]]", encoding="utf-8")
    (ai / "b.md").write_text("# B", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp = client.get("/api/v1/graph")
        assert resp.status_code == 200
        data = resp.json()
        edges = data["edges"]
        link_edge = [
            e
            for e in edges
            if e["source"] == "AI/a.md"
            and e["target"] == "AI/b.md"
            and e["edge_type"] == "links_to"
        ]
        assert len(link_edge) == 1
        assert link_edge[0]["explicit"] is True
        assert isinstance(link_edge[0]["id"], str)


def test_graph_contains_parent_edges(tmp_path: Path) -> None:
    """Frontmatter parent field produces parent_of edges."""
    ai = tmp_path / "AI"
    (ai / "Knowledge").mkdir(parents=True)
    (ai / "Knowledge" / "INDEX.md").write_text(
        "---\ntype: index\nstatus: active\n---\n# Knowledge Index\n",
        encoding="utf-8",
    )
    (ai / "Knowledge" / "concept.md").write_text(
        '---\nparent: "[[AI/Knowledge/INDEX]]"\n---\n# Concept\n',
        encoding="utf-8",
    )
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp = client.get("/api/v1/graph")
        assert resp.status_code == 200
        data = resp.json()
        parent_edges = [e for e in data["edges"] if e["edge_type"] == "parent_of"]
        assert len(parent_edges) == 1
        pe = parent_edges[0]
        assert pe["source"] == "AI/Knowledge/INDEX.md"
        assert pe["target"] == "AI/Knowledge/concept.md"
        assert pe["explicit"] is True


def test_graph_contains_containment_edges(tmp_path: Path) -> None:
    """index.md files produce contains edges for siblings and sub-folders."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "INDEX.md").write_text("# Root\n", encoding="utf-8")
    (ai / "child.md").write_text("# Child\n", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp = client.get("/api/v1/graph")
        assert resp.status_code == 200
        data = resp.json()
        contains_edges = [
            e
            for e in data["edges"]
            if e["edge_type"] == "contains"
            and e["source"] == "AI/INDEX.md"
            and e["target"] == "AI/child.md"
        ]
        assert len(contains_edges) == 1
        assert contains_edges[0]["explicit"] is False


def test_graph_connects_descendants_to_the_nearest_folder_index(tmp_path: Path) -> None:
    """Folder structure creates local index hubs without an ancestor-wide mesh."""
    ai = tmp_path / "AI"
    guides = ai / "Guides"
    deep = guides / "Deep"
    deep.mkdir(parents=True)
    (ai / "INDEX.md").write_text("# Vault index\n", encoding="utf-8")
    (guides / "INDEX.md").write_text("# Guides index\n", encoding="utf-8")
    (guides / "intro.md").write_text("# Intro\n", encoding="utf-8")
    (deep / "detail.md").write_text("# Detail\n", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        data = client.get("/api/v1/graph").json()

    contains = {
        (edge["source"], edge["target"])
        for edge in data["edges"]
        if edge["edge_type"] == "contains"
    }
    assert ("AI/Guides/INDEX.md", "AI/Guides/intro.md") in contains
    assert ("AI/Guides/INDEX.md", "AI/Guides/Deep/detail.md") in contains
    # The nested index joins the root; descendants do not also join the root.
    assert ("AI/INDEX.md", "AI/Guides/INDEX.md") in contains
    assert ("AI/INDEX.md", "AI/Guides/Deep/detail.md") not in contains


def test_graph_deterministic_ordering(tmp_path: Path) -> None:
    """Nodes and edges are returned in deterministic order."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "z.md").write_text("# Z\n", encoding="utf-8")
    (ai / "a.md").write_text("# A\n", encoding="utf-8")
    (ai / "m.md").write_text("# M\n", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp1 = client.get("/api/v1/graph")
        resp2 = client.get("/api/v1/graph")
        assert resp1.json() == resp2.json()

        data = resp1.json()
        node_paths = [n["path"] for n in data["nodes"]]
        assert node_paths == sorted(node_paths)

        edge_keys = [(e["source"], e["target"], e["edge_type"]) for e in data["edges"]]
        assert edge_keys == sorted(edge_keys)


def test_graph_deterministic_ordering_with_multiple_edges(tmp_path: Path) -> None:
    """Multiple edges between and around nodes are deterministically ordered."""
    ai = tmp_path / "AI"
    ai.mkdir()
    # index.md creates contains edges to all children
    # a.md links to b.md and c.md
    # b.md links to c.md
    (ai / "INDEX.md").write_text("# Root\n", encoding="utf-8")
    (ai / "a.md").write_text("# A\n[[b]]\n[[c]]", encoding="utf-8")
    (ai / "b.md").write_text("# B\n[[c]]", encoding="utf-8")
    (ai / "c.md").write_text("# C\n", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp1 = client.get("/api/v1/graph")
        resp2 = client.get("/api/v1/graph")
        assert resp1.json() == resp2.json()

        data = resp1.json()
        node_paths = [n["path"] for n in data["nodes"]]
        assert node_paths == sorted(node_paths)

        edge_keys = [(e["source"], e["target"], e["edge_type"]) for e in data["edges"]]
        assert edge_keys == sorted(edge_keys)

        # Should have: 3 contains edges (INDEX->a, INDEX->b, INDEX->c)
        # + 3 links_to edges (a->b, a->c, b->c)
        assert len(data["edges"]) == 6
        link_edges = [e for e in data["edges"] if e["edge_type"] == "links_to"]
        contains_edges = [e for e in data["edges"] if e["edge_type"] == "contains"]
        assert len(link_edges) == 3
        assert len(contains_edges) == 3


def test_graph_edge_ids_are_stable(tmp_path: Path) -> None:
    """Edge IDs are deterministic strings derived from source/target/type."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "x.md").write_text("# X\n[[y]]", encoding="utf-8")
    (ai / "y.md").write_text("# Y\n", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp1 = client.get("/api/v1/graph")
        resp2 = client.get("/api/v1/graph")
        edges1 = resp1.json()["edges"]
        edges2 = resp2.json()["edges"]
        for e1, e2 in zip(edges1, edges2):
            assert e1["id"] == e2["id"]
            assert len(e1["id"]) > 0


def test_graph_generation_field_present(tmp_path: Path) -> None:
    """generation reflects catalog scan timestamp or is non-empty."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "note.md").write_text("# Note\n", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp = client.get("/api/v1/graph")
        data = resp.json()
        assert data["generation"] != ""


def test_graph_ignores_broken_links(tmp_path: Path) -> None:
    """Broken (unresolved) links do not produce edges."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "a.md").write_text("# A\n[[nonexistent]]", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp = client.get("/api/v1/graph")
        data = resp.json()
        # Note should appear (as isolated), but no edges
        assert any(n["path"] == "AI/a.md" for n in data["nodes"])
        assert len(data["edges"]) == 0


def test_graph_mixed_connected_and_isolated(tmp_path: Path) -> None:
    """A vault with both connected and isolated notes."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "connected1.md").write_text("# C1\n[[connected2]]", encoding="utf-8")
    (ai / "connected2.md").write_text("# C2\n", encoding="utf-8")
    (ai / "isolated.md").write_text("# Isolated\n", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        resp = client.get("/api/v1/graph")
        data = resp.json()
        nodes_by_path = {n["path"]: n for n in data["nodes"]}

        # All three notes present
        assert len(nodes_by_path) == 3

        # Connected notes
        assert nodes_by_path["AI/connected1.md"]["isolated"] is False
        assert nodes_by_path["AI/connected2.md"]["isolated"] is False

        # Isolated note
        assert nodes_by_path["AI/isolated.md"]["isolated"] is True

        # One links_to edge
        link_edges = [e for e in data["edges"] if e["edge_type"] == "links_to"]
        assert len(link_edges) == 1
        assert link_edges[0]["source"] == "AI/connected1.md"
        assert link_edges[0]["target"] == "AI/connected2.md"


def test_graph_generation_changes_when_title_changes(tmp_path: Path) -> None:
    """generation changes when a node's H1 title changes."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "note.md").write_text("# Original Title\n", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        gen1 = client.get("/api/v1/graph").json()["generation"]

    # Change the H1 title
    (ai / "note.md").write_text("# Changed Title\n", encoding="utf-8")

    with authed_client(settings) as client:
        gen2 = client.get("/api/v1/graph").json()["generation"]

    assert gen1 != gen2


def test_graph_generation_changes_when_explicit_content_changes(tmp_path: Path) -> None:
    """generation changes when wikilinks are added (explicit edges)."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "a.md").write_text("# A\n", encoding="utf-8")
    (ai / "b.md").write_text("# B\n", encoding="utf-8")
    settings = _settings(tmp_path)

    with authed_client(settings) as client:
        gen1 = client.get("/api/v1/graph").json()["generation"]

    # Add a wikilink to create an explicit edge
    (ai / "a.md").write_text("# A\n[[b]]\n", encoding="utf-8")

    with authed_client(settings) as client:
        gen2 = client.get("/api/v1/graph").json()["generation"]

    assert gen1 != gen2


def test_graph_only_structural_edge_types(tmp_path: Path) -> None:
    """Only structural edge types (links_to, parent_of, contains) are emitted."""
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "INDEX.md").write_text("# Root\n", encoding="utf-8")
    (ai / "a.md").write_text('# A\nparent: "[[b]]"\n', encoding="utf-8")
    (ai / "b.md").write_text("# B\n[[a]]", encoding="utf-8")
    settings = _settings(tmp_path)

    ALLOWED_EDGE_TYPES = {"links_to", "parent_of", "contains"}

    with authed_client(settings) as client:
        resp = client.get("/api/v1/graph")
        data = resp.json()

        edge_types = {e["edge_type"] for e in data["edges"]}
        assert edge_types.issubset(ALLOWED_EDGE_TYPES), (
            f"Unexpected edge types: {edge_types - ALLOWED_EDGE_TYPES}"
        )
