"""Bounded spreading activation over the project graph."""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Sequence
from typing import Any, Final, cast

import networkx as nx

from harbor_ledger_memory.domain.retrieval import ActivatedNode, SeedCandidate

_SUPPORTED_EDGE_TYPES: Final = frozenset({"links_to", "contains", "parent_of"})


def spread_activation(
    graph: nx.MultiDiGraph[str, dict[str, Any], dict[str, Any]],
    seeds: Sequence[SeedCandidate],
    *,
    max_hops: int,
    max_nodes: int,
    decay: float,
    minimum_activation: float,
    edge_types: frozenset[str],
    adaptive_deltas: dict[str, dict[str, float]] | dict[str, float] | None = None,
) -> tuple[ActivatedNode, ...]:
    """Propagate activation from *seeds* through *graph* with bounded decay.

    Edges in ``edge_types`` are traversed in both directions.  The highest
    activation score for each path is kept.  Propagation stops before
    enqueueing a node whose score falls below *minimum_activation* or when
    the hard limits *max_nodes* / *max_hops* are reached.

    Each result carries the provenance of the edge the traversal actually
    used to reach it — the structural ``edge_source`` → ``edge_target``
    orientation, the ``traversal_direction``, and the projection edge's
    ``edge_key``.  The winner is selected here, after any adaptive weight
    deltas are applied, so the provenance is intrinsic to the result and
    never needs to be reconstructed from the projection afterwards.

    Returns results ordered by activation score (descending), then path
    (ascending) for stable ordering.
    """
    if not seeds:
        return ()

    allowed = edge_types & _SUPPORTED_EDGE_TYPES
    # Query feedback uses a flat target -> delta map.  Pair-specific
    # adaptive callers use a nested source -> target -> delta map.
    flat_deltas: dict[str, float] | None = None
    nested_deltas: dict[str, dict[str, float]] | None = None
    if adaptive_deltas:
        if all(isinstance(value, float) for value in adaptive_deltas.values()):
            flat_deltas = cast(dict[str, float], adaptive_deltas)
        else:
            nested_deltas = cast(dict[str, dict[str, float]], adaptive_deltas)

    # best_score[path] = highest activation seen so far
    best_score: dict[str, float] = {}
    # Results accumulator — we may exceed max_nodes during BFS,
    # but only the top max_nodes survive at the end.
    results: list[ActivatedNode] = []
    count = 0

    # Min-heap: (-score, path) so we process highest-score first.
    # Entries: (-activation_score, path, hop, via_path, edge_type,
    #          edge_source, edge_target, traversal_direction, edge_key)
    # Scores are unique per path (pushes are strictly improving), so heap
    # comparisons never run past the (score, path) prefix.
    queue: list[
        tuple[
            float,
            str,
            int,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            int | str | None,
        ]
    ] = []

    for seed in seeds:
        score = seed.retrieval_score
        if score > best_score.get(seed.path, -1.0):
            best_score[seed.path] = score
            heapq.heappush(
                queue,
                (-score, seed.path, 0, None, None, None, None, None, None),
            )

    while queue and count < max_nodes:
        (
            neg_score,
            path,
            hop,
            via_path,
            edge_type,
            edge_source,
            edge_target,
            traversal_direction,
            edge_key,
        ) = heapq.heappop(queue)
        current_score = -neg_score

        # Skip if we already found a better route to this node
        if current_score < best_score.get(path, -1.0):
            continue

        count += 1
        results.append(
            ActivatedNode(
                path=path,
                activation_score=current_score,
                hop=hop,
                via_path=via_path,
                edge_type=edge_type,
                edge_source=edge_source,
                edge_target=edge_target,
                traversal_direction=traversal_direction,
                edge_key=edge_key,
            )
        )

        # Propagate to neighbours (if we haven't hit hop limit)
        if hop >= max_hops:
            continue

        next_hop = hop + 1

        for neighbour in _neighbours(graph, path, allowed):
            (
                n_path,
                n_edge_type,
                n_weight,
                n_source,
                n_target,
                n_direction,
                n_key,
            ) = neighbour
            # Flat query deltas are target feedback and apply only while
            # leaving a seed. Nested maps remain source -> target deltas and
            # retain their traversal behavior at every hop.
            if flat_deltas is not None and hop == 0:
                delta = flat_deltas.get(n_path, 0.0)
                n_weight *= 1.0 + delta
            elif nested_deltas is not None and path in nested_deltas:
                delta = nested_deltas[path].get(n_path, 0.0)
                n_weight *= 1.0 + delta
            next_score = current_score * n_weight * decay

            if next_score < minimum_activation:
                continue
            if next_score <= best_score.get(n_path, -1.0):
                continue

            best_score[n_path] = next_score
            heapq.heappush(
                queue,
                (
                    -next_score,
                    n_path,
                    next_hop,
                    path,
                    n_edge_type,
                    n_source,
                    n_target,
                    n_direction,
                    n_key,
                ),
            )

    # Sort: score descending, path ascending
    results.sort(key=lambda n: (-n.activation_score, n.path))
    return tuple(results)


def _neighbours(
    graph: nx.MultiDiGraph[str, dict[str, Any], dict[str, Any]],
    path: str,
    allowed_edge_types: frozenset[str],
) -> list[tuple[str, str, float, str, str, str, int | str]]:
    """Return per-edge neighbours reachable from *path* via allowed edge types.

    Traverses both directions.  Each item describes one directed projection
    edge and is ``(neighbour_path, edge_type, weight, source, target,
    direction, key)``: ``source``/``target`` are the edge's structural
    endpoints as stored in the projection, ``direction`` records how the
    traversal moves across it ("forward" for outgoing edges, "reverse" for
    incoming ones), and ``key`` is the multigraph edge key.
    """
    neighbours: list[tuple[str, str, float, str, str, str, int | str]] = []

    if path not in graph:
        return neighbours

    # Outgoing edges: path -> neighbour (traversed forward)
    out_edges = cast(
        Iterable[tuple[str, str, int | str, dict[str, Any]]],
        graph.out_edges(path, keys=True, data=True),
    )
    for _, target, key, key_data in out_edges:
        etype = key_data.get("edge_type", "")
        if etype not in allowed_edge_types:
            continue
        weight = key_data.get("weight", 1.0)
        neighbours.append((target, etype, weight, path, target, "forward", key))

    # Incoming edges: neighbour -> path (traversed in reverse)
    in_edges = cast(
        Iterable[tuple[str, str, int | str, dict[str, Any]]],
        graph.in_edges(path, keys=True, data=True),
    )
    for source, _, key, key_data in in_edges:
        etype = key_data.get("edge_type", "")
        if etype not in allowed_edge_types:
            continue
        weight = key_data.get("weight", 1.0)
        neighbours.append((source, etype, weight, source, path, "reverse", key))

    return neighbours
