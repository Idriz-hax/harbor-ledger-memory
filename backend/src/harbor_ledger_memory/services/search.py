"""Safe plain-text search over the catalog-owned SQLite FTS projection."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession

_MAX_QUERY_LENGTH = 512
_MAX_LIMIT = 100
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’_-][^\W_]+)*", re.UNICODE)


@dataclass(frozen=True)
class SearchHit:
    """The intentionally small, non-ORM search response."""

    path: str
    title: str
    summary: str | None
    snippet: str
    score: float


class SearchService:
    """Search FTS5 with user input treated as literal tokens."""

    def __init__(
        self,
        session: Session | Engine,
        *,
        path_filter: Callable[[str], bool] | None = None,
    ) -> None:
        self._session = (
            CatalogSession(bind=session) if isinstance(session, Engine) else session
        )
        self._path_filter = path_filter

    def search(self, query: str, limit: int = 20) -> list[SearchHit]:
        if not query.strip() or limit <= 0:
            return []
        tokens = _plain_tokens(query[:_MAX_QUERY_LENGTH])
        if not tokens:
            return []
        fts_query = " AND ".join(_quote_fts_token(token) for token in tokens)
        safe_limit = min(limit, _MAX_LIMIT)
        statement = text(
            """
            SELECT notes.path, notes.title, notes.summary,
                   snippet(note_fts, 2, '', '', ' … ', 12) AS snippet,
                   bm25(note_fts) AS score
            FROM notes
            JOIN note_fts ON note_fts.rowid = notes.id
            WHERE note_fts MATCH :query
            ORDER BY score, notes.path
            LIMIT :limit
            """
        )
        rows = self._session.execute(
            statement, {"query": fts_query, "limit": safe_limit}
        ).mappings()
        hits = [
            SearchHit(
                path=str(row["path"]),
                title=str(row["title"]),
                summary=None if row["summary"] is None else str(row["summary"]),
                snippet=str(row["snippet"] or ""),
                score=float(row["score"]),
            )
            for row in rows
        ]
        if self._path_filter is not None:
            hits = [hit for hit in hits if self._path_filter(hit.path)]
        return hits


def _plain_tokens(value: str) -> list[str]:
    return _TOKEN_RE.findall(value)


def _quote_fts_token(token: str) -> str:
    return f'"{token.replace(chr(34), chr(34) * 2)}"'


__all__ = ["SearchHit", "SearchService", "plain_tokens", "quote_fts_token"]


plain_tokens = _plain_tokens
"""Normalize a string to a list of plain text tokens (public alias)."""

quote_fts_token = _quote_fts_token
"""Quote a single token for safe FTS5 embedding (public alias)."""
