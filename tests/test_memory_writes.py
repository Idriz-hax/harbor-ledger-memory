"""Persistence tests for MemoryWriteProposal."""

from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import MemoryWriteProposal


def test_pending_proposal_persists_and_returns_fields(tmp_path: Path) -> None:
    engine: Engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    path = "AI/Knowledge/test.md"
    content = "# Hello\nWorld"
    now = datetime.now(UTC).isoformat()

    with CatalogSession(bind=engine) as session:
        proposal = MemoryWriteProposal(
            path=path,
            content=content,
            operation="overwrite",
            rule_access="system",
            requested_at=now,
        )
        session.add(proposal)
        session.commit()
        session.refresh(proposal)

        assert proposal.id is not None
        assert proposal.path == path
        assert proposal.content == content
        assert proposal.operation == "overwrite"
        assert proposal.status == "pending"
        assert proposal.rule_access == "system"
        assert proposal.requested_at == now
        assert proposal.resolved_at is None
        assert proposal.failure_reason is None

    with CatalogSession(bind=engine) as session:
        result = session.scalars(select(MemoryWriteProposal)).one()
        assert result.id == proposal.id
        assert result.path == path
        assert result.status == "pending"

    engine.dispose()


def test_update_proposal_with_source_hash_and_applying_state(
    tmp_path: Path,
) -> None:
    engine: Engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    path = "AI/Knowledge/test.md"
    content = "# Updated\nContent"
    source_hash = "abc123def456"
    now = datetime.now(UTC).isoformat()

    with CatalogSession(bind=engine) as session:
        proposal = MemoryWriteProposal(
            path=path,
            content=content,
            operation="update",
            rule_access="system",
            requested_at=now,
            expected_source_hash=source_hash,
        )
        session.add(proposal)
        session.commit()
        session.refresh(proposal)

        assert proposal.id is not None
        assert proposal.status == "pending"
        assert proposal.expected_source_hash == source_hash
        assert proposal.applying_at is None

        # Transition to applying state
        proposal.status = "applying"
        proposal.applying_at = now
        session.commit()
        session.refresh(proposal)

        assert proposal.status == "applying"
        assert proposal.applying_at == now
        assert proposal.expected_source_hash == source_hash

    with CatalogSession(bind=engine) as session:
        result = session.scalars(select(MemoryWriteProposal)).one()
        assert result.status == "applying"
        assert result.expected_source_hash == source_hash
        assert result.applying_at == now

    engine.dispose()


def test_applied_proposal_stores_applied_content_hash(
    tmp_path: Path,
) -> None:
    engine: Engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    now = datetime.now(UTC).isoformat()

    with CatalogSession(bind=engine) as session:
        proposal = MemoryWriteProposal(
            path="AI/test.md",
            content="# Applied",
            operation="overwrite",
            rule_access="system",
            requested_at=now,
        )
        session.add(proposal)
        session.commit()
        session.refresh(proposal)

        assert proposal.applied_content_hash is None

        # Simulate successful application
        proposal.status = "applied"
        proposal.applied_content_hash = "a1b2c3d4e5f6"
        proposal.resolved_at = now
        session.commit()
        session.refresh(proposal)

        assert proposal.status == "applied"
        assert proposal.applied_content_hash == "a1b2c3d4e5f6"
        assert proposal.resolved_at == now

    with CatalogSession(bind=engine) as session:
        result = session.scalars(select(MemoryWriteProposal)).one()
        assert result.status == "applied"
        assert result.applied_content_hash == "a1b2c3d4e5f6"

    engine.dispose()


HEAD_REVISION = "0012_token_rules"

ACTIVITY_METADATA_COLUMNS = (
    "operation_id",
    "run_id",
    "agent_id",
    "parent_id",
    "graph_refs_json",
)

PROPOSAL_LIFECYCLE_COLUMNS = (
    "expected_source_hash",
    "applying_at",
    "applied_content_hash",
)


def _alembic_config(database_path: Path) -> Config:
    config = Config(str(Path(__file__).parent.parent / "backend" / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def _column_map(engine: Engine, table: str):
    return {col["name"]: col for col in inspect(engine).get_columns(table)}


def _assert_merged_head_schema(engine: Engine) -> None:
    """Assert the full merged schema every upgrade path must converge to."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert {"activity_events", "memory_write_proposals", "activation_visits"} <= tables

    activity = _column_map(engine, "activity_events")
    for name in ("id", "event_type", "created_at", "payload_json"):
        assert name in activity, f"activity_events missing column {name}"
    for name in ACTIVITY_METADATA_COLUMNS:
        assert name in activity, f"activity_events missing column {name}"
        assert activity[name]["nullable"] is True

    proposals = _column_map(engine, "memory_write_proposals")
    for name in (
        "id",
        "path",
        "content",
        "operation",
        "status",
        "rule_access",
        "requested_at",
        "resolved_at",
        "failure_reason",
    ):
        assert name in proposals, f"memory_write_proposals missing column {name}"
    for name in PROPOSAL_LIFECYCLE_COLUMNS:
        assert name in proposals, f"memory_write_proposals missing column {name}"
        assert proposals[name]["nullable"] is True

    visits = _column_map(engine, "activation_visits")
    for name in ("edge_source", "edge_target"):
        assert name in visits, f"activation_visits missing column {name}"
        assert visits[name]["nullable"] is True

    tokens = _column_map(engine, "api_tokens")
    assert "rules" in tokens, "api_tokens missing rules column"
    assert tokens["rules"]["nullable"] is True
    assert "admin" in tokens, "api_tokens missing admin column"
    assert tokens["admin"]["nullable"] is False

    indexes = {
        idx["name"]: idx["column_names"]
        for idx in insp.get_indexes("memory_write_proposals")
    }
    assert "ix_memory_write_proposals_status" in indexes
    assert indexes["ix_memory_write_proposals_status"] == ["status"]
    assert "ix_memory_write_proposals_path" in indexes
    assert indexes["ix_memory_write_proposals_path"] == ["path"]

    with engine.connect() as conn:
        revision = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert revision == HEAD_REVISION


def test_fresh_0006_upgrades_to_merge_head(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh0006.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "0006_activity_events")
    command.upgrade(config, HEAD_REVISION)

    engine: Engine = create_engine(f"sqlite:///{database_path}")
    try:
        _assert_merged_head_schema(engine)
    finally:
        engine.dispose()


def test_graph_head_upgrades_to_merge_head(tmp_path: Path) -> None:
    database_path = tmp_path / "graphhead.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "0007_activity_graph_metadata")

    engine: Engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO activity_events "
                    "(event_type, created_at, payload_json, operation_id, run_id, "
                    "agent_id, parent_id, graph_refs_json) "
                    "VALUES ('scan', '2026-08-19T00:00:00+00:00', '{}', "
                    "'op-1', 'run-1', 'agent-1', 'parent-1', '[\"AI/a.md\"]')"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, HEAD_REVISION)

    engine2 = create_engine(f"sqlite:///{database_path}")
    try:
        _assert_merged_head_schema(engine2)
        with engine2.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT operation_id, run_id, agent_id, parent_id, "
                    "graph_refs_json FROM activity_events"
                )
            ).one()
        assert row == ("op-1", "run-1", "agent-1", "parent-1", '["AI/a.md"]')
    finally:
        engine2.dispose()


def test_proposal_head_upgrades_to_merge_head(tmp_path: Path) -> None:
    database_path = tmp_path / "proposalhead.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "0007_memory_write_proposals")

    engine: Engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO memory_write_proposals "
                    "(path, content, operation, rule_access, requested_at) "
                    "VALUES ('AI/n.md', '# n', 'overwrite', 'system', "
                    "'2026-08-19T00:00:00+00:00')"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, HEAD_REVISION)

    engine2 = create_engine(f"sqlite:///{database_path}")
    try:
        _assert_merged_head_schema(engine2)
        with engine2.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT path, content, status, expected_source_hash, "
                    "applying_at, applied_content_hash FROM memory_write_proposals"
                )
            ).one()
        assert row == ("AI/n.md", "# n", "pending", None, None, None)
    finally:
        engine2.dispose()


def test_activation_head_upgrades_to_merge_head(tmp_path: Path) -> None:
    database_path = tmp_path / "activationhead.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "0008_activation_visit_edges")

    engine: Engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO query_traces "
                    "(trace_uuid, created_at, query, retrieval_settings) "
                    "VALUES ('trace-1', '2026-08-19T00:00:00+00:00', 'hello', "
                    "'{}')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO activation_visits "
                    "(trace_id, path, activation_score, hop, edge_source, "
                    "edge_target) VALUES (1, 'AI/a.md', 0.9, 0, 'AI/a.md', "
                    "'AI/b.md')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO activity_events "
                    "(event_type, created_at, payload_json) "
                    "VALUES ('query', '2026-08-19T00:01:00+00:00', '{}')"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, HEAD_REVISION)

    engine2 = create_engine(f"sqlite:///{database_path}")
    try:
        _assert_merged_head_schema(engine2)
        with engine2.connect() as conn:
            visit = conn.execute(
                text("SELECT path, edge_source, edge_target FROM activation_visits")
            ).one()
            activity = conn.execute(
                text(
                    "SELECT event_type, payload_json, operation_id, run_id, "
                    "agent_id, parent_id, graph_refs_json FROM activity_events"
                )
            ).one()
        assert visit == ("AI/a.md", "AI/a.md", "AI/b.md")
        assert activity == ("query", "{}", None, None, None, None, None)
    finally:
        engine2.dispose()


def test_merge_upgrade_retains_branch_data(tmp_path: Path) -> None:
    # Graph-branch database: activity metadata values survive the merge.
    graph_db = tmp_path / "retain_graph.db"
    graph_config = _alembic_config(graph_db)
    command.upgrade(graph_config, "0007_activity_graph_metadata")

    engine: Engine = create_engine(f"sqlite:///{graph_db}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO activity_events "
                    "(event_type, created_at, payload_json, operation_id, run_id, "
                    "agent_id, parent_id, graph_refs_json) "
                    "VALUES ('vault.created', '2026-08-19T00:00:00+00:00', "
                    "'{}', 'op-9', 'run-9', 'agent-9', 'parent-9', "
                    "'[\"AI/w.md\"]')"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(graph_config, HEAD_REVISION)

    engine2 = create_engine(f"sqlite:///{graph_db}")
    try:
        with engine2.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT event_type, operation_id, run_id, agent_id, "
                    "parent_id, graph_refs_json FROM activity_events"
                )
            ).one()
        assert row == (
            "vault.created",
            "op-9",
            "run-9",
            "agent-9",
            "parent-9",
            '["AI/w.md"]',
        )
    finally:
        engine2.dispose()

    # Writes-branch database: visits, activity, and proposal rows survive.
    writes_db = tmp_path / "retain_writes.db"
    writes_config = _alembic_config(writes_db)
    command.upgrade(writes_config, "0008_activation_visit_edges")

    engine3 = create_engine(f"sqlite:///{writes_db}")
    try:
        with engine3.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO query_traces "
                    "(trace_uuid, created_at, query, retrieval_settings) "
                    "VALUES ('trace-2', '2026-08-19T00:00:00+00:00', 'world', "
                    "'{}')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO activation_visits "
                    "(trace_id, path, activation_score, hop, edge_source, "
                    "edge_target) VALUES (1, 'AI/b.md', 0.5, 1, 'AI/a.md', "
                    "'AI/b.md')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO activity_events "
                    "(event_type, created_at, payload_json) "
                    "VALUES ('scan', '2026-08-19T00:02:00+00:00', '{}')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO memory_write_proposals "
                    "(path, content, operation, rule_access, requested_at, "
                    "resolved_at, failure_reason) "
                    "VALUES ('AI/p.md', '# p', 'update', 'agent', "
                    "'2026-08-19T00:03:00+00:00', "
                    "'2026-08-19T00:04:00+00:00', 'boom')"
                )
            )
    finally:
        engine3.dispose()

    command.upgrade(writes_config, HEAD_REVISION)

    engine4 = create_engine(f"sqlite:///{writes_db}")
    try:
        with engine4.connect() as conn:
            visit = conn.execute(
                text(
                    "SELECT path, hop, edge_source, edge_target FROM activation_visits"
                )
            ).one()
            activity = conn.execute(
                text("SELECT event_type, payload_json FROM activity_events")
            ).one()
            proposal = conn.execute(
                text(
                    "SELECT path, content, operation, rule_access, status, "
                    "requested_at, resolved_at, failure_reason, "
                    "expected_source_hash, applying_at, applied_content_hash "
                    "FROM memory_write_proposals"
                )
            ).one()
        assert visit == ("AI/b.md", 1, "AI/a.md", "AI/b.md")
        assert activity == ("scan", "{}")
        assert proposal == (
            "AI/p.md",
            "# p",
            "update",
            "agent",
            "pending",
            "2026-08-19T00:03:00+00:00",
            "2026-08-19T00:04:00+00:00",
            "boom",
            None,
            None,
            None,
        )
    finally:
        engine4.dispose()


def test_merge_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "merge.db"
    config = _alembic_config(database_path)

    command.upgrade(config, HEAD_REVISION)

    engine: Engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO activity_events "
                    "(event_type, created_at, payload_json, operation_id) "
                    "VALUES ('scan', '2026-08-19T00:00:00+00:00', '{}', "
                    "'op-1')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO memory_write_proposals "
                    "(path, content, operation, rule_access, requested_at, "
                    "expected_source_hash) "
                    "VALUES ('AI/n.md', '# n', 'overwrite', 'system', "
                    "'2026-08-19T00:00:00+00:00', 'hash-1')"
                )
            )
    finally:
        engine.dispose()

    # Downgrade the merge: additive lifecycle columns are removed, both
    # branch schemas stay intact.
    command.downgrade(config, "0008_activation_visit_edges")

    engine2 = create_engine(f"sqlite:///{database_path}")
    try:
        proposals = _column_map(engine2, "memory_write_proposals")
        assert (set(PROPOSAL_LIFECYCLE_COLUMNS) & set(proposals)) == set(), (
            "lifecycle columns must be dropped at merge downgrade"
        )
        assert "path" in proposals and "content" in proposals
        activity = _column_map(engine2, "activity_events")
        assert set(ACTIVITY_METADATA_COLUMNS) <= set(activity)
        visits = _column_map(engine2, "activation_visits")
        assert {"edge_source", "edge_target"} <= set(visits)
        with engine2.connect() as conn:
            row = conn.execute(
                text("SELECT event_type, operation_id FROM activity_events")
            ).one()
        assert row == ("scan", "op-1")
    finally:
        engine2.dispose()

    # Downgrade 0008_activation_visit_edges: edge endpoints are removed.
    command.downgrade(config, "0007_memory_write_proposals")

    engine3 = create_engine(f"sqlite:///{database_path}")
    try:
        visits = _column_map(engine3, "activation_visits")
        assert not {"edge_source", "edge_target"} & set(visits)
        assert "memory_write_proposals" in set(inspect(engine3).get_table_names())
    finally:
        engine3.dispose()

    # Downgrade to the branchpoint: both branches collapse to 0006, dropping
    # the proposals table and the graph-branch metadata columns together.
    command.downgrade(config, "0006_activity_events")

    engine4 = create_engine(f"sqlite:///{database_path}")
    try:
        tables = set(inspect(engine4).get_table_names())
        assert "memory_write_proposals" not in tables
        assert "activity_events" in tables
        activity = _column_map(engine4, "activity_events")
        assert not set(ACTIVITY_METADATA_COLUMNS) & set(activity)
        with engine4.connect() as conn:
            row = conn.execute(
                text("SELECT event_type, payload_json FROM activity_events")
            ).one()
        assert row == ("scan", "{}")
    finally:
        engine4.dispose()

    # Re-upgrade restores the merged schema; data that outlived its columns
    # survives, dropped columns come back empty.
    command.upgrade(config, HEAD_REVISION)

    engine5 = create_engine(f"sqlite:///{database_path}")
    try:
        _assert_merged_head_schema(engine5)
        with engine5.connect() as conn:
            activity = conn.execute(
                text(
                    "SELECT event_type, payload_json, operation_id FROM activity_events"
                )
            ).one()
            proposal_count = conn.execute(
                text("SELECT COUNT(*) FROM memory_write_proposals")
            ).scalar_one()
        assert activity == ("scan", "{}", None)
        assert proposal_count == 0
    finally:
        engine5.dispose()


def test_api_tokens_0011_upgrades_to_0012(tmp_path: Path) -> None:
    database_path = tmp_path / "tokens0011.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "0011_token_name_reuse")

    engine: Engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(name, token_hash, scopes, created_at, revoked_at) "
                    "VALUES ('reader', 'hash-1', '[\"read\"]', "
                    "'2026-08-31T00:00:00+00:00', NULL)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, HEAD_REVISION)

    engine2 = create_engine(f"sqlite:///{database_path}")
    try:
        with engine2.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT name, token_hash, scopes, created_at, revoked_at, "
                    "rules, admin FROM api_tokens"
                )
            ).one()
        assert row == (
            "reader",
            "hash-1",
            '["read"]',
            "2026-08-31T00:00:00+00:00",
            None,
            None,
            0,
        )
        with engine2.connect() as conn:
            revision = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == HEAD_REVISION
    finally:
        engine2.dispose()


def test_api_tokens_0010_upgrades_to_head(tmp_path: Path) -> None:
    database_path = tmp_path / "tokens0010.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "0010_api_tokens")

    engine: Engine = create_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO api_tokens "
                    "(name, token_hash, scopes, created_at, revoked_at) "
                    "VALUES ('reader', 'hash-1', '[\"read\"]', "
                    "'2026-08-31T00:00:00+00:00', NULL)"
                )
            )
    finally:
        engine.dispose()

    command.upgrade(config, HEAD_REVISION)

    engine2 = create_engine(f"sqlite:///{database_path}")
    try:
        with engine2.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT name, token_hash, scopes, created_at, revoked_at "
                    "FROM api_tokens"
                )
            ).one()
        assert row == (
            "reader",
            "hash-1",
            '["read"]',
            "2026-08-31T00:00:00+00:00",
            None,
        )
        insp = inspect(engine2)
        index_names = {idx["name"] for idx in insp.get_indexes("api_tokens")}
        assert "uq_api_tokens_active_name" in index_names
        with engine2.connect() as conn:
            revision = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert revision == HEAD_REVISION
    finally:
        engine2.dispose()
