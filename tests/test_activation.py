"""Tests for bounded spreading activation."""

from __future__ import annotations

import networkx as nx
import pytest

from harbor_ledger_memory.domain.retrieval import ActivatedNode, SeedCandidate
from harbor_ledger_memory.graph.activation import spread_activation


def _make_graph() -> nx.MultiDiGraph:
    """Build a small deterministic test graph."""
    g: nx.MultiDiGraph = nx.MultiDiGraph()
    # Nodes
    for path in ("A.md", "B.md", "C.md", "D.md", "E.md", "F.md"):
        g.add_node(path)

    # A --links_to--> B (weight 1.0)
    g.add_edge("A.md", "B.md", edge_type="links_to", weight=1.0)
    # A --contains--> C (weight 0.5)
    g.add_edge("A.md", "C.md", edge_type="contains", weight=0.5)
    # A --parent_of--> D (weight 1.0)
    g.add_edge("A.md", "D.md", edge_type="parent_of", weight=1.0)
    # B --links_to--> E (weight 1.0)
    g.add_edge("B.md", "E.md", edge_type="links_to", weight=1.0)
    # D --contains--> F (weight 0.5)
    g.add_edge("D.md", "F.md", edge_type="contains", weight=0.5)
    return g


@pytest.fixture
def graph() -> nx.MultiDiGraph:
    return _make_graph()


EDGE_TYPES = frozenset({"links_to", "contains", "parent_of"})


@pytest.fixture
def all_edge_types() -> frozenset[str]:
    return EDGE_TYPES


class TestOneHopDecay:
    """Activation decays by one hop from a seed."""

    def test_seed_appears_at_hop_zero(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=3,
            max_nodes=100,
            decay=0.5,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        paths = {node.path for node in result}
        assert "A.md" in paths
        seed_node = next(n for n in result if n.path == "A.md")
        assert seed_node.hop == 0
        assert seed_node.activation_score == pytest.approx(1.0)

    def test_one_hop_decay_applied(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=0.5,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        b = next(n for n in result if n.path == "B.md")
        # 1.0 * 1.0 (edge weight) * 0.5 (decay) = 0.5
        assert b.activation_score == pytest.approx(0.5)
        assert b.hop == 1
        assert b.via_path == "A.md"
        assert b.edge_type == "links_to"


class TestEdgeWeightMultiplication:
    """Edge weights affect propagation scores."""

    def test_contains_edge_halves_score(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        c = next(n for n in result if n.path == "C.md")
        # 1.0 * 0.5 (contains edge weight) * 1.0 (decay) = 0.5
        assert c.activation_score == pytest.approx(0.5)
        assert c.edge_type == "contains"

    def test_links_to_full_weight(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        b = next(n for n in result if n.path == "B.md")
        # 1.0 * 1.0 (links_to edge weight) * 1.0 (decay) = 1.0
        assert b.activation_score == pytest.approx(1.0)


class TestBidirectionalReachability:
    """Edges are traversed in both directions."""

    def test_reverse_links_to_reaches_source(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="B.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=0.5,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        a = next(n for n in result if n.path == "A.md")
        # B has links_to incoming from A, so A is reachable in reverse
        assert a.hop == 1
        assert a.via_path == "B.md"
        assert a.edge_type == "links_to"

    def test_reverse_parent_of_reaches_child(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="D.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        # A --parent_of--> D, so from D we can reach A via reverse parent_of
        a = next(n for n in result if n.path == "A.md")
        assert a.hop == 1
        assert a.edge_type == "parent_of"

    def test_reverse_contains_reaches_child(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="F.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        # D --contains--> F, so from F we can reach D via reverse contains
        d = next(n for n in result if n.path == "D.md")
        assert d.hop == 1
        assert d.edge_type == "contains"


class TestExcludedEdgeTypes:
    """Only allowed edge types are traversed."""

    def test_excluded_edge_type_not_traversed(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=frozenset({"links_to"}),
        )
        paths = {node.path for node in result}
        assert "A.md" in paths
        assert "B.md" in paths  # links_to is allowed
        assert "C.md" not in paths  # contains is excluded
        assert "D.md" not in paths  # parent_of is excluded

    def test_empty_edge_types_only_seeds(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=frozenset(),
        )
        assert len(result) == 1
        assert result[0].path == "A.md"


class TestThresholdCutoff:
    """Nodes below minimum_activation are not returned."""

    def test_below_threshold_excluded(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=3,
            max_nodes=100,
            decay=0.5,
            minimum_activation=0.6,
            edge_types=EDGE_TYPES,
        )
        paths = {node.path for node in result}
        # A = 1.0 (>= 0.6), B = 0.5 (< 0.6), C = 0.25 (< 0.6), D = 0.5 (< 0.6)
        assert "A.md" in paths
        assert "B.md" not in paths
        assert "C.md" not in paths
        assert "D.md" not in paths

    def test_two_hop_below_threshold(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=2,
            max_nodes=100,
            decay=0.8,
            minimum_activation=0.5,
            edge_types=EDGE_TYPES,
        )
        # A=1.0, B=0.8, C=0.4, D=0.8, E=0.64, F=0.32
        paths = {node.path for node in result}
        assert "A.md" in paths
        assert "B.md" in paths  # 0.8 >= 0.5
        assert "D.md" in paths  # 0.8 >= 0.5
        assert "C.md" not in paths  # 0.4 < 0.5
        assert "E.md" in paths  # 0.64 >= 0.5
        assert "F.md" not in paths  # 0.32 < 0.5


class TestMaxHopCutoff:
    """Propagation stops after max_hops."""

    def test_two_hop_reachable_within_limit(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=2,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        paths = {node.path for node in result}
        assert "E.md" in paths  # A->B->E is 2 hops

    def test_two_hop_excluded_by_max_hops_1(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        paths = {node.path for node in result}
        assert "E.md" not in paths  # A->B->E is 2 hops, max_hops=1
        assert "F.md" not in paths  # A->D->F is 2 hops, max_hops=1


class TestMaxNodeCutoff:
    """Activation stops when max_nodes is reached."""

    def test_max_nodes_limits_results(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=3,
            max_nodes=3,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        # Should return at most 3 nodes (seed + highest activation neighbors)
        assert len(result) <= 3

    def test_max_nodes_prefers_higher_score(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=2,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        # A=1.0, B=1.0, D=1.0, C=0.5 — should get A + highest scored nodes
        assert len(result) <= 2
        scores = {n.path: n.activation_score for n in result}
        # All returned nodes should have score >= 0.5
        for score in scores.values():
            assert score >= 0.5 - 1e-9


class TestStableOrdering:
    """Results are deterministically ordered by score desc, path asc."""

    def test_ordered_by_score_descending(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        scores = [n.activation_score for n in result]
        assert scores == sorted(scores, reverse=True)

    def test_tie_broken_by_path_ascending(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        # A=1.0, B=1.0, D=1.0, C=0.5 — ties broken by path ascending
        top_paths = [n.path for n in result if n.activation_score == pytest.approx(1.0)]
        assert top_paths == sorted(top_paths)

    def test_multiple_seeds_ordered(self) -> None:
        g: nx.MultiDiGraph = nx.MultiDiGraph()
        g.add_node("X.md")
        g.add_node("Y.md")
        g.add_node("Z.md")
        g.add_edge("X.md", "Y.md", edge_type="links_to", weight=1.0)
        g.add_edge("Z.md", "Y.md", edge_type="links_to", weight=1.0)
        seeds = [
            SeedCandidate(path="X.md", retrieval_score=0.9),
            SeedCandidate(path="Z.md", retrieval_score=1.0),
        ]
        result = spread_activation(
            g,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=0.5,
            minimum_activation=0.0,
            edge_types=frozenset({"links_to"}),
        )
        scores = [n.activation_score for n in result]
        assert scores == sorted(scores, reverse=True)


class TestHighestActivationKept:
    """When a node is reached multiple times, only the highest score is kept."""

    def test_duplicate_path_keeps_highest(self, graph: nx.MultiDiGraph) -> None:
        # Add a second path from A to B via a different edge
        graph.add_edge("A.md", "B.md", edge_type="contains", weight=0.3)
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        b_nodes = [n for n in result if n.path == "B.md"]
        assert len(b_nodes) == 1
        # Should keep links_to path (weight 1.0) over contains path (weight 0.3)
        assert b_nodes[0].activation_score == pytest.approx(1.0)
        assert b_nodes[0].edge_type == "links_to"


class TestSelectedEdgeProvenance:
    """Activated nodes carry the actually selected structural edge.

    The provenance (structural source/target, traversal direction, and the
    projection edge's key) is the edge the traversal *actually used*, chosen
    inside ``spread_activation`` after any adaptive weight deltas are
    applied — not a guess reconstructed from the raw projection afterwards.
    """

    def test_reciprocal_same_type_edges_carry_selected_provenance(self) -> None:
        g: nx.MultiDiGraph = nx.MultiDiGraph()
        g.add_node("a.md")
        g.add_node("b.md")
        g.add_edge("a.md", "b.md", edge_type="links_to", weight=0.5)
        reverse_key = g.add_edge("b.md", "a.md", edge_type="links_to", weight=1.0)

        seeds = [SeedCandidate(path="a.md", retrieval_score=1.0)]
        result = spread_activation(
            g,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=0.5,
            minimum_activation=0.0,
            edge_types=frozenset({"links_to"}),
        )
        b = next(n for n in result if n.path == "b.md")
        # The heavier reciprocal edge wins: reverse traversal of b -> a
        # (1.0 * 1.0 * decay 0.5).
        assert b.activation_score == pytest.approx(0.5)
        assert b.via_path == "a.md"
        assert b.edge_type == "links_to"
        assert b.edge_source == "b.md"
        assert b.edge_target == "a.md"
        assert b.traversal_direction == "reverse"
        assert b.edge_key == reverse_key

    def test_reciprocal_edges_with_adaptive_delta_retain_selected_provenance(
        self,
    ) -> None:
        """Adaptive deltas rescale scores but never rewrite the winner.

        A delta on the ``a -> b`` traversal pair scales *both* reciprocal
        routes by the same factor, so the heavier ``b -> a`` edge still wins
        — and the node must carry that edge's provenance together with the
        post-delta score.
        """
        g: nx.MultiDiGraph = nx.MultiDiGraph()
        g.add_node("a.md")
        g.add_node("b.md")
        g.add_edge("a.md", "b.md", edge_type="links_to", weight=0.5)
        reverse_key = g.add_edge("b.md", "a.md", edge_type="links_to", weight=1.0)

        seeds = [SeedCandidate(path="a.md", retrieval_score=1.0)]
        result = spread_activation(
            g,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=frozenset({"links_to"}),
            adaptive_deltas={"a.md": {"b.md": 0.5}},
        )
        b = next(n for n in result if n.path == "b.md")
        # Winning route b -> a: 1.0 * (1.0 + 0.5 delta) * 1.0 decay = 1.5.
        assert b.activation_score == pytest.approx(1.5)
        assert b.edge_source == "b.md"
        assert b.edge_target == "a.md"
        assert b.traversal_direction == "reverse"
        assert b.edge_key == reverse_key

    def test_parallel_same_type_edges_carry_winner_edge_key(self) -> None:
        g: nx.MultiDiGraph = nx.MultiDiGraph()
        g.add_node("a.md")
        g.add_node("b.md")
        light_key = g.add_edge("a.md", "b.md", edge_type="links_to", weight=0.5)
        heavy_key = g.add_edge("a.md", "b.md", edge_type="links_to", weight=1.0)

        seeds = [SeedCandidate(path="a.md", retrieval_score=1.0)]
        result = spread_activation(
            g,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=1.0,
            minimum_activation=0.0,
            edge_types=frozenset({"links_to"}),
        )
        b = next(n for n in result if n.path == "b.md")
        assert b.activation_score == pytest.approx(1.0)
        assert b.edge_source == "a.md"
        assert b.edge_target == "b.md"
        assert b.traversal_direction == "forward"
        assert b.edge_key == heavy_key
        assert b.edge_key != light_key


class TestMultiHopDecay:
    """Activation compounds decay across hops."""

    def test_two_hop_decay_compounds(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=2,
            max_nodes=100,
            decay=0.5,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        e = next(n for n in result if n.path == "E.md")
        # A->B (1.0*1.0*0.5=0.5), B->E (0.5*1.0*0.5=0.25)
        assert e.activation_score == pytest.approx(0.25)
        assert e.hop == 2

    def test_flat_query_delta_only_applies_from_seed_hop(self) -> None:
        graph: nx.MultiDiGraph = nx.MultiDiGraph()
        graph.add_edge("A.md", "B.md", edge_type="links_to", weight=1.0)
        graph.add_edge("B.md", "T.md", edge_type="links_to", weight=1.0)
        graph.add_edge("T.md", "U.md", edge_type="links_to", weight=1.0)
        graph.add_node("S.md")
        seeds = [
            SeedCandidate(path="A.md", retrieval_score=1.0),
            SeedCandidate(path="S.md", retrieval_score=0.8),
        ]

        kwargs = {
            "max_hops": 3,
            "max_nodes": 100,
            "decay": 1.0,
            "minimum_activation": 0.0,
            "edge_types": frozenset({"links_to"}),
        }
        baseline = spread_activation(graph, seeds, **kwargs)
        adjusted = spread_activation(
            graph, seeds, adaptive_deltas={"T.md": 0.5}, **kwargs
        )
        baseline_scores = {node.path: node.activation_score for node in baseline}
        adjusted_scores = {node.path: node.activation_score for node in adjusted}
        assert adjusted_scores["T.md"] == baseline_scores["T.md"]
        assert adjusted_scores["U.md"] == baseline_scores["U.md"]

    def test_two_hop_via_contains(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=2,
            max_nodes=100,
            decay=0.5,
            minimum_activation=0.0,
            edge_types=EDGE_TYPES,
        )
        f = next(n for n in result if n.path == "F.md")
        # A->D (1.0*1.0*0.5=0.5), D->F (0.5*0.5*0.5=0.125)
        assert f.activation_score == pytest.approx(0.125)
        assert f.hop == 2


class TestEmptyInputs:
    """Edge cases with empty inputs."""

    def test_no_seeds_returns_empty(self, graph: nx.MultiDiGraph) -> None:
        result = spread_activation(
            graph,
            [],
            max_hops=3,
            max_nodes=100,
            decay=0.5,
            minimum_activation=0.0,
            edge_types=frozenset({"links_to"}),
        )
        assert result == ()

    def test_isolated_seed_returns_only_itself(self) -> None:
        g: nx.MultiDiGraph = nx.MultiDiGraph()
        g.add_node("lonely.md")
        seeds = [SeedCandidate(path="lonely.md", retrieval_score=0.8)]
        result = spread_activation(
            g,
            seeds,
            max_hops=3,
            max_nodes=100,
            decay=0.5,
            minimum_activation=0.0,
            edge_types=frozenset({"links_to"}),
        )
        assert len(result) == 1
        assert result[0].path == "lonely.md"
        assert result[0].activation_score == pytest.approx(0.8)


class TestReturnTuple:
    """Return type is a tuple of ActivatedNode."""

    def test_returns_tuple(self, graph: nx.MultiDiGraph) -> None:
        seeds = [SeedCandidate(path="A.md", retrieval_score=1.0)]
        result = spread_activation(
            graph,
            seeds,
            max_hops=1,
            max_nodes=100,
            decay=0.5,
            minimum_activation=0.0,
            edge_types=frozenset({"links_to"}),
        )
        assert isinstance(result, tuple)
        assert all(isinstance(node, ActivatedNode) for node in result)
