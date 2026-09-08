"""Durable, policy-independent graph facts built during a catalog scan."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import PurePosixPath

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.models import (
    GraphEdgeFact,
    GraphNodeFact,
    GraphProjectionVersion,
    GraphSnapshotHandle,
)
from harbor_ledger_memory.graph.builder import GraphBuilder

MAX_PINNED_SNAPSHOT_HANDLES = 8


def materialize_graph(session: Session, *, created_at: str | None = None) -> str:
    """Build and atomically activate a complete graph projection.

    The caller owns the surrounding transaction. Any exception leaves the
    caller's transaction rollback path responsible for preserving the prior
    active version.
    """
    graph = GraphBuilder(session).build()
    node_rows = []
    for path in sorted(graph.nodes):
        data = graph.nodes[path]
        node_rows.append({
            "path": path,
            "parent_folder": PurePosixPath(path).parent.as_posix(),
            "top_folder": PurePosixPath(path).parts[0] if PurePosixPath(path).parts else ".",
            "kind": str(data.get("kind", "file")),
            "title": str(data.get("title", "")),
        })
    edge_weights: dict[tuple[str, str, str], float] = {}
    for source, target, data in sorted(
        graph.edges(data=True), key=lambda item: (item[0], item[1], str(item[2].get("edge_type", "")))
    ):
        key = (source, target, str(data.get("edge_type", "")))
        edge_weights[key] = edge_weights.get(key, 0.0) + float(data.get("weight", 1.0))
    edge_rows = [
        {"source": source, "target": target, "edge_type": edge_type, "weight": weight}
        for (source, target, edge_type), weight in sorted(edge_weights.items())
    ]
    payload = json.dumps({"nodes": node_rows, "edges": edge_rows}, sort_keys=True, separators=(",", ":"))
    stamp = created_at or datetime.now(UTC).isoformat()
    version_id = hashlib.sha256(f"{stamp}\n{payload}".encode()).hexdigest()

    session.execute(update(GraphProjectionVersion).where(GraphProjectionVersion.active.is_(True)).values(active=False))
    session.add(GraphProjectionVersion(id=version_id, created_at=stamp, active=True))
    session.flush()
    for row in node_rows:
        fact_id = hashlib.sha256(f"node\t{row['path']}".encode()).hexdigest()
        session.add(GraphNodeFact(id=fact_id, version_id=version_id, **row))
    for row in edge_rows:
        fact_id = hashlib.sha256(
            f"edge\t{row['source']}\t{row['target']}\t{row['edge_type']}".encode()
        ).hexdigest()
        session.add(GraphEdgeFact(id=fact_id, version_id=version_id, **row))
    session.flush()

    now = datetime.now(UTC).isoformat()
    session.execute(delete(GraphSnapshotHandle).where(GraphSnapshotHandle.expires_at <= now))
    pinned = session.scalars(
        select(GraphSnapshotHandle)
        .where(GraphSnapshotHandle.expires_at > now)
        .order_by(GraphSnapshotHandle.expires_at.desc(), GraphSnapshotHandle.handle.asc())
        .limit(MAX_PINNED_SNAPSHOT_HANDLES)
    ).all()
    keep_handles = {handle.handle for handle in pinned}
    if keep_handles:
        session.execute(delete(GraphSnapshotHandle).where(~GraphSnapshotHandle.handle.in_(keep_handles)))
    else:
        session.execute(delete(GraphSnapshotHandle))
    protected = {handle.version_id for handle in pinned}
    inactive = session.scalars(
        select(GraphProjectionVersion)
        .where(GraphProjectionVersion.active.is_(False))
        .order_by(GraphProjectionVersion.created_at.desc(), GraphProjectionVersion.id.desc())
    ).all()
    retained = {inactive[0].id} if inactive else set()
    retained.update(protected)
    for old in inactive:
        if old.id not in retained:
            session.delete(old)
    session.flush()
    return version_id


__all__ = ["materialize_graph"]
