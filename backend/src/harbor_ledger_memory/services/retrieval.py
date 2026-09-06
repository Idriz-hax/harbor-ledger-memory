"""Deterministic hybrid seed retrieval over the catalog."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession
from harbor_ledger_memory.catalog.models import Note, NoteEmbedding
from harbor_ledger_memory.config import MemorySettings
from harbor_ledger_memory.domain.retrieval import QuerySettings, SeedCandidate
from harbor_ledger_memory.services.embeddings import (
    EmbeddingService,
    cosine_similarity,
    effective_embedding_model,
)
from harbor_ledger_memory.services.search import plain_tokens, quote_fts_token

_MAX_QUERY_LENGTH = 512
_FTS_CONTRIB = 0.50
_PATH_TOKEN_WEIGHT = 1.00
_TITLE_TOKEN_WEIGHT = 0.80
_TAG_WEIGHT = 0.65
_SUMMARY_TOKEN_WEIGHT = 0.55
_PROJECT_WEIGHT = 0.35
_EMBEDDING_WEIGHT = 0.40


def _normalize_fts_scores(scores: list[float]) -> dict[int, float]:
    """Normalize bm25 scores (negative floats) to [0, 1] per result index."""
    if not scores:
        return {}
    worst = min(scores)
    best = max(scores)
    if best == worst:
        return {i: 1.0 for i in range(len(scores))}
    span = best - worst
    return {i: (s - worst) / span for i, s in enumerate(scores)}


def _tokens_for_string(value: str) -> set[str]:
    """Return lowercased tokens from a string."""
    tokens: list[str] = plain_tokens(value)  # type: ignore[assignment]
    return {t.lower() for t in tokens}


def _note_tags(note: Note) -> list[str]:
    """Extract tag list from frontmatter JSON."""
    try:
        fm = json.loads(note.frontmatter_json or "{}")
        tags = fm.get("tags", [])
        if isinstance(tags, list):
            tag_items = cast(list[object], tags)
            return [str(t).lower() for t in tag_items]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


class HybridRetrievalService:
    """Retrieve seed candidates using deterministic hybrid scoring.

    Combines FTS5 full-text search with catalog metadata signals to produce
    ranked, reproducible seed candidates for downstream activation.
    """

    def __init__(
        self,
        session: Session | Engine,
        settings: QuerySettings | None = None,
        memory_settings: MemorySettings | None = None,
        path_filter: Callable[[str], bool] | None = None,
    ) -> None:
        self._session = (
            CatalogSession(bind=session) if isinstance(session, Engine) else session
        )
        self._settings = settings or QuerySettings()
        self._memory_settings = memory_settings or MemorySettings()
        self._path_filter = path_filter

    def seeds(
        self,
        query: str,
        active_project: str | None = None,
        recent_paths: dict[str, float] | None = None,
    ) -> tuple[SeedCandidate, ...]:
        """Return ranked seed candidates for a query string.

        Never passes raw user input to FTS — tokens are normalised first.
        Queries FTS once, then enriches with metadata from the catalog.
        Does not read source files or call the parser.
        """
        tokens = plain_tokens(query[:_MAX_QUERY_LENGTH])
        if not tokens:
            return ()

        token_set = {t.lower() for t in tokens}
        fts_query = " AND ".join(quote_fts_token(t) for t in tokens)

        # --- Phase 1: FTS search (with generous limit to capture all candidates) ---
        from sqlalchemy import text as sa_text

        fts_statement = sa_text(
            """
            SELECT notes.path, bm25(note_fts) AS score
            FROM notes
            JOIN note_fts ON note_fts.rowid = notes.id
            WHERE note_fts MATCH :query
            ORDER BY score, notes.path
            LIMIT 500
            """
        )
        fts_rows = self._session.execute(fts_statement, {"query": fts_query}).mappings()
        fts_results: list[tuple[str, float]] = [
            (str(row["path"]), float(row["score"]))
            for row in fts_rows
            if self._path_filter is None or self._path_filter(str(row["path"]))
        ]
        fts_paths = {path for path, _ in fts_results}
        fts_score_map: dict[str, float] = {}
        for idx, (path, raw_score) in enumerate(fts_results):
            fts_score_map[path] = raw_score

        # Normalize FTS scores to [0, 1]
        raw_scores = [score for _, score in fts_results]
        norm_fts = _normalize_fts_scores(raw_scores)
        # Build normalized FTS score per path
        norm_fts_map: dict[str, float] = {}
        for idx, (path, _) in enumerate(fts_results):
            norm_fts_map[path] = norm_fts.get(idx, 0.0)

        # --- Phase 2: Collect tag-match candidates outside FTS results ---
        # We need to find notes whose tags match query tokens but that weren't
        # returned by FTS. Load all notes for tag scanning (bounded by catalog size).
        all_notes_stmt = select(Note.path, Note.frontmatter_json)
        all_notes = list(self._session.execute(all_notes_stmt))
        tag_match_paths: set[str] = set()
        for row in all_notes:
            path = str(row[0])
            if self._path_filter is not None and not self._path_filter(path):
                continue
            if path in fts_paths:
                continue
            tags = _note_tags(Note(path=path, frontmatter_json=str(row[1])))
            if token_set & {t.lower() for t in tags}:
                tag_match_paths.add(path)

        # --- Phase 3: Bulk-load all candidate notes from catalog ---
        candidate_paths = (
            fts_paths
            | tag_match_paths
            | {
                path
                for path in (recent_paths or ())
                if self._path_filter is None or self._path_filter(path)
            }
        )
        # --- Embedding lookup (optional semantic signal) ---
        embedding_map: dict[str, tuple[float, ...]] = {}
        query_vector: tuple[float, ...] | None = None
        emb_model = effective_embedding_model(self._memory_settings.embedding_model)
        if emb_model:
            embedding_svc = EmbeddingService(emb_model)
            query_vector = embedding_svc.encode(query[:_MAX_QUERY_LENGTH])

            emb_rows = self._session.execute(
                select(NoteEmbedding.note_path, NoteEmbedding.embedding_blob).where(
                    NoteEmbedding.model_name == emb_model
                )
            ).all()
            embedding_map = {
                row[0]: embedding_svc.from_blob(row[1])
                for row in emb_rows
                if self._path_filter is None or self._path_filter(row[0])
            }
            candidate_paths.update(embedding_map)

        if not candidate_paths:
            return ()

        notes_stmt = select(Note).where(Note.path.in_(candidate_paths))
        candidate_notes = {
            note.path: note
            for note in self._session.scalars(notes_stmt)
            if self._path_filter is None or self._path_filter(note.path)
        }

        # --- Phase 4: Score each candidate ---
        candidates: list[tuple[str, float, list[str]]] = []

        for path, note in candidate_notes.items():
            score = 0.0
            reasons: list[str] = []

            # FTS contribution
            if path in norm_fts_map:
                fts_contribution = _FTS_CONTRIB * norm_fts_map[path]
                if fts_contribution > 0:
                    score += fts_contribution
                    reasons.append("Full-text match")

            # Exact path token match
            path_segments: set[str] = set()
            for part in path.replace(".md", "").split("/"):
                for seg in plain_tokens(part):
                    path_segments.add(seg.lower())
            if token_set & path_segments:
                score += _PATH_TOKEN_WEIGHT
                reasons.append("Exact path match")

            # Title/filename token match
            title_tokens = _tokens_for_string(note.title or "")
            if title_tokens & token_set:
                score += _TITLE_TOKEN_WEIGHT
                reasons.append("Title match")

            # Tag match
            tags = _note_tags(note)
            matching_tags = token_set & set(tags)
            if matching_tags:
                score += _TAG_WEIGHT
                for tag in sorted(matching_tags):
                    reasons.append(f"Tag match: {tag}")

            # Summary token match
            if note.summary:
                summary_tokens = _tokens_for_string(note.summary)
                if summary_tokens & token_set:
                    score += _SUMMARY_TOKEN_WEIGHT
                    reasons.append("Summary match")

            # Active project match
            if active_project and active_project in path:
                score += _PROJECT_WEIGHT
                reasons.append("Active project match")

            # Short-term cache boost
            if recent_paths and path in recent_paths:
                boost = recent_paths[path]
                score += boost
                reasons.append(f"Short-term cache (+{boost:.2f})")

            # Semantic similarity
            if query_vector is not None and path in embedding_map:
                similarity = cosine_similarity(query_vector, embedding_map[path])
                if similarity > 0.0:
                    score += _EMBEDDING_WEIGHT * similarity
                    reasons.append(f"Semantic similarity ({similarity:.2f})")

            # Clamp to 1.0
            score = min(score, 1.0)

            if score > 0:
                candidates.append((path, score, reasons))

        # --- Phase 5: Sort and truncate ---
        candidates.sort(key=lambda c: (-c[1], c[0]))
        max_nodes = self._settings.max_seed_nodes
        candidates = candidates[:max_nodes]

        return tuple(
            SeedCandidate(
                path=path,
                retrieval_score=round(score, 4),
                reasons=tuple(reasons),
            )
            for path, score, reasons in candidates
        )
