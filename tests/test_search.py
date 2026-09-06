from pathlib import Path

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import Note
from harbor_ledger_memory.services.search import SearchService


def test_search_returns_small_safe_hits(tmp_path: Path) -> None:
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    session = CatalogSession(bind=engine)
    try:
        session.add(
            Note(
                path="AI/Knowledge/cache.md",
                title="Caching",
                content="cache invalidation is important",
                summary="Cache notes",
                content_hash="hash",
            )
        )
        session.commit()
        hits = SearchService(session).search("cache")
        assert len(hits) == 1
        assert hits[0].path == "AI/Knowledge/cache.md"
        assert hits[0].title == "Caching"
        assert hits[0].summary == "Cache notes"
        assert hits[0].snippet
        assert isinstance(hits[0].score, float)
    finally:
        session.close()
        engine.dispose()


def test_search_escapes_fts_operators(tmp_path: Path) -> None:
    engine = create_database(f"sqlite:///{tmp_path / 'catalog.db'}")
    session = CatalogSession(bind=engine)
    try:
        session.add(
            Note(
                path="AI/note.md",
                title="Note",
                content="ordinary text",
                content_hash="hash",
            )
        )
        session.commit()
        assert SearchService(session).search('" OR *') == []
        assert SearchService(session).search("   * ? ") == []
    finally:
        session.close()
        engine.dispose()
