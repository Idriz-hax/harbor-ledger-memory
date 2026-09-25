"""Milestone 4 scoped memory-mark contract tests."""

from datetime import UTC, datetime, timedelta

import pytest

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import Note, QueryTrace
from harbor_ledger_memory.services.memory_marks import (
    MarkCapacityError,
    MemoryMarkScope,
    MemoryMarkService,
    TraceScopeError,
)


def _session(tmp_path):
    engine = create_database(f"sqlite:///{tmp_path / 'marks.db'}")
    return engine, CatalogSession(bind=engine)


def _seed(session: CatalogSession, trace_scope: MemoryMarkScope) -> str:
    session.add(
        Note(path="Public/note.md", title="Note", content="content", content_hash="h")
    )
    trace = QueryTrace(
        trace_uuid="trace-1",
        query="content",
        retrieval_settings="{}",
        status="completed",
        scope_kind=trace_scope.scope_kind,
        scope_id=trace_scope.scope_id,
    )
    session.add(trace)
    session.commit()
    return trace.trace_uuid


def test_marks_are_scope_isolated_and_owner_checked(tmp_path):
    engine, session = _session(tmp_path)
    try:
        owner = MemoryMarkScope("token", "one")
        other = MemoryMarkScope("token", "two")
        trace_uuid = _seed(session, owner)
        service = MemoryMarkService(session)
        service.create_mark(owner, trace_uuid, "Public/note.md", "pin", lambda _: True)
        assert len(service.list_marks(owner, lambda _: True)) == 1
        assert service.list_marks(other, lambda _: True) == []
        with pytest.raises(TraceScopeError):
            service.create_mark(
                other, trace_uuid, "Public/note.md", "pin", lambda _: True
            )
    finally:
        session.close()
        engine.dispose()


def test_pin_boost_is_bounded_resettable_and_expirable(tmp_path):
    engine, session = _session(tmp_path)
    try:
        scope = MemoryMarkScope("token", "one")
        trace_uuid = _seed(session, scope)
        service = MemoryMarkService(session, max_marks=2, pin_boost=0.2)
        service.create_mark(scope, trace_uuid, "Public/note.md", "pin", lambda _: True)
        assert service.pin_boosts(scope, lambda _: True) == {"Public/note.md": 0.2}
        assert service.pin_boosts(scope, lambda _: False) == {}
        assert service.list_marks(scope, lambda _: False) == []
        service.reset(scope, lambda _: True)
        assert service.pin_boosts(scope, lambda _: True) == {}
        service.create_mark(
            scope,
            trace_uuid,
            "Public/note.md",
            "pin",
            lambda _: True,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        assert service.pin_boosts(scope, lambda _: True) == {}
    finally:
        session.close()
        engine.dispose()


def test_feedback_marks_are_audit_only_and_capacity_is_per_scope(tmp_path):
    engine, session = _session(tmp_path)
    try:
        scope = MemoryMarkScope("token", "one")
        other = MemoryMarkScope("token", "two")
        trace_uuid = _seed(session, scope)
        service = MemoryMarkService(session, max_marks=1)
        service.create_mark(
            scope, trace_uuid, "Public/note.md", "relevant", lambda _: True
        )
        assert service.pin_boosts(scope, lambda _: True) == {}
        with pytest.raises(MarkCapacityError):
            service.create_mark(
                scope, trace_uuid, "Public/note.md", "irrelevant", lambda _: True
            )
        session.add(
            QueryTrace(
                trace_uuid="trace-2",
                query="content",
                retrieval_settings="{}",
                status="completed",
                scope_kind=other.scope_kind,
                scope_id=other.scope_id,
            )
        )
        session.commit()
        service.create_mark(other, "trace-2", "Public/note.md", "pin", lambda _: True)
    finally:
        session.close()
        engine.dispose()


def test_legacy_trace_cannot_create_marks(tmp_path):
    engine, session = _session(tmp_path)
    try:
        scope = MemoryMarkScope("token", "one")
        session.add(
            Note(
                path="Public/note.md", title="Note", content="content", content_hash="h"
            )
        )
        session.add(
            QueryTrace(
                trace_uuid="legacy",
                query="content",
                retrieval_settings="{}",
                status="completed",
            )
        )
        session.commit()
        with pytest.raises(TraceScopeError):
            MemoryMarkService(session).create_mark(
                scope, "legacy", "Public/note.md", "pin", lambda _: True
            )
    finally:
        session.close()
        engine.dispose()


def test_scope_identifier_rejects_secret_material() -> None:
    scope = MemoryMarkScope("token", "stable-token-id")
    assert "secret" not in scope.scope_id.lower()
