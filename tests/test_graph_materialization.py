from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import (
    GraphEdgeFact,
    GraphNodeFact,
    GraphProjectionVersion,
    GraphSnapshotHandle,
)
from harbor_ledger_memory.config import Settings
from harbor_ledger_memory.services import graph_materialization
from harbor_ledger_memory.services.access import AccessPolicy
from harbor_ledger_memory.services.graph_projection import GraphProjectionService
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.vault.boundary import VaultBoundary


def _service(tmp_path: Path) -> tuple[ScanService, CatalogSession, object]:
    (tmp_path / "AI").mkdir()
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    session = CatalogSession(bind=engine)
    settings = Settings(
        vault_path=tmp_path, database_url=f"sqlite:///{tmp_path / 'catalog.db'}"
    )
    return ScanService(VaultBoundary(settings), session), session, engine


def test_materialized_facts_are_deterministic_and_weighted(tmp_path: Path) -> None:
    service, session, engine = _service(tmp_path)
    try:
        (tmp_path / "AI" / "a.md").write_text("# A\n[[b]]\n[[b]]\n", encoding="utf-8")
        (tmp_path / "AI" / "b.md").write_text("# B\n", encoding="utf-8")
        service.full_scan()
        version = session.scalar(
            select(GraphProjectionVersion).where(
                GraphProjectionVersion.active.is_(True)
            )
        )
        assert version is not None
        nodes = session.scalars(
            select(GraphNodeFact).where(GraphNodeFact.version_id == version.id)
        ).all()
        edges = session.scalars(
            select(GraphEdgeFact).where(GraphEdgeFact.version_id == version.id)
        ).all()
        assert [node.path for node in nodes] == sorted(node.path for node in nodes)
        assert any(
            edge.edge_type == "links_to" and edge.weight == 2.0 for edge in edges
        )
    finally:
        session.close()
        engine.dispose()


def test_materialization_swaps_and_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, session, engine = _service(tmp_path)
    try:
        note = tmp_path / "AI" / "note.md"
        note.write_text("# One\n", encoding="utf-8")
        service.full_scan()
        first = session.scalar(
            select(GraphProjectionVersion).where(
                GraphProjectionVersion.active.is_(True)
            )
        )
        assert first is not None
        note.write_text("# Two\n", encoding="utf-8")
        service.full_scan()
        versions = session.scalars(
            select(GraphProjectionVersion).order_by(GraphProjectionVersion.created_at)
        ).all()
        assert len(versions) == 2
        assert sum(version.active for version in versions) == 1

        def fail(*args: object, **kwargs: object) -> str:
            raise RuntimeError("materialization failed")

        monkeypatch.setattr(graph_materialization, "materialize_graph", fail)
        with pytest.raises(RuntimeError, match="materialization failed"):
            service.full_scan()
        active = session.scalar(
            select(GraphProjectionVersion).where(
                GraphProjectionVersion.active.is_(True)
            )
        )
        assert active is not None and active.id == versions[-1].id
    finally:
        session.close()
        engine.dispose()


def test_snapshot_handle_creation_cleans_expired_and_caps_capacity(
    tmp_path: Path,
) -> None:
    service, session, engine = _service(tmp_path)
    try:
        for index in range(10):
            folder = tmp_path / "AI" / f"folder-{index}"
            folder.mkdir()
            (folder / "note.md").write_text(f"# {index}\n", encoding="utf-8")
        service.full_scan()
        session.add(
            GraphSnapshotHandle(
                handle="expired",
                version_id=session.scalar(
                    select(GraphProjectionVersion).where(
                        GraphProjectionVersion.active.is_(True)
                    )
                ).id,
                policy_fingerprint="public",
                scope_fingerprint="scope",
                level=0,
                offset=1,
                expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            )
        )
        session.commit()
        policy = AccessPolicy(())
        cursor = None
        for _ in range(12):
            view = GraphProjectionService(session).view(
                1,
                page_size=1,
                cursor=cursor,
                policy=policy,
                policy_fingerprint=policy.fingerprint(),
            )
            cursor = view.next_cursor
            if cursor is None:
                break
        handles = session.scalars(select(GraphSnapshotHandle)).all()
        assert len(handles) <= 8
        assert all(handle.handle != "expired" for handle in handles)
    finally:
        session.close()
        engine.dispose()


def test_edge_heavy_level_two_pages_without_scope(tmp_path: Path) -> None:
    service, session, engine = _service(tmp_path)
    try:
        names = [f"note-{index:02d}" for index in range(26)]
        for name in names:
            links = "\n".join(f"[[{target}]]" for target in names if target != name)
            (tmp_path / "AI" / f"{name}.md").write_text(
                f"# {name}\n{links}\n", encoding="utf-8"
            )
        service.full_scan()

        policy = AccessPolicy(())
        first = GraphProjectionService(session).view(
            2, page_size=500, policy=policy, policy_fingerprint=policy.fingerprint()
        )
        assert len(first.clusters) < 500
        assert len(first.edges) == 500
        assert first.next_cursor is not None

        second = GraphProjectionService(session).view(
            2,
            page_size=500,
            cursor=first.next_cursor,
            policy=policy,
            policy_fingerprint=policy.fingerprint(),
        )
        assert second.scope is None
        assert second.edges
    finally:
        session.close()
        engine.dispose()
