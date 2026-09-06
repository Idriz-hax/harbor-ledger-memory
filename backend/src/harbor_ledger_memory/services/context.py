"""Smallest-sufficient context package builder."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Final

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession
from harbor_ledger_memory.catalog.models import Note
from harbor_ledger_memory.domain.retrieval import (
    ActivatedNode,
    ContextMemory,
    SeedCandidate,
)

_TOKEN_RE: Final = re.compile(r"[^\W_]+(?:[''\-_][^\W_]+)*", re.UNICODE)
_CONTENT_WINDOW: Final = 1000
_GAP: Final = 200
_HIGH_RETRIEVAL_THRESHOLD: Final = 0.7


def _estimate_tokens(text: str) -> int:
    """Estimate token count as ceil(len / 4)."""
    return math.ceil(len(text) / 4) if text else 0


def _plain_tokens(value: str) -> list[str]:
    """Normalize a string to a list of plain text tokens."""
    return _TOKEN_RE.findall(value)


class ContextBuilder:
    """Build a smallest-sufficient context package from retrieval results.

    Combines seed candidates and activated nodes, ranks by combined score,
    creates excerpts from indexed content, and respects a token budget.
    """

    def __init__(self, session: Session | Engine) -> None:
        self._session = (
            CatalogSession(bind=session) if isinstance(session, Engine) else session
        )

    def build(
        self,
        query: str,
        seeds: Sequence[SeedCandidate],
        activated: Sequence[ActivatedNode],
        token_budget: int,
    ) -> tuple[ContextMemory, ...]:
        """Build a ranked context package from retrieval results.

        Args:
            query: The original query text (used for token matching in excerpts).
            seeds: Seed candidates from hybrid retrieval.
            activated: Nodes activated by graph traversal.
            token_budget: Maximum total estimated tokens for the context package.

        Returns:
            Ranked tuple of ContextMemory objects within the token budget.
        """
        query_tokens = {t.lower() for t in _plain_tokens(query)}

        # Build lookup maps
        seed_map: dict[str, SeedCandidate] = {s.path: s for s in seeds}
        activated_map: dict[str, ActivatedNode] = {}
        for node in activated:
            # Keep highest activation score per path
            existing = activated_map.get(node.path)
            if existing is None or node.activation_score > existing.activation_score:
                activated_map[node.path] = node

        # Collect all unique candidate paths
        all_paths = set(seed_map) | set(activated_map)
        if not all_paths:
            return ()

        # Bulk-load notes from catalog
        notes_stmt = select(Note).where(Note.path.in_(all_paths))
        note_map: dict[str, Note] = {
            note.path: note for note in self._session.scalars(notes_stmt)
        }

        # Score and rank candidates
        candidates = self._score_candidates(seed_map, activated_map)

        # Create excerpts within budget
        return self._select_with_budget(
            candidates, note_map, query_tokens, token_budget
        )

    def _score_candidates(
        self,
        seed_map: dict[str, SeedCandidate],
        activated_map: dict[str, ActivatedNode],
    ) -> list[_Candidate]:
        """Score and rank candidates by combined score.

        Returns list of _Candidate sorted by combined_score descending,
        then path ascending.
        """
        results: list[_Candidate] = []

        for path in set(seed_map) | set(activated_map):
            seed = seed_map.get(path)
            activated = activated_map.get(path)

            retrieval_score = seed.retrieval_score if seed else 0.0
            activation_score = activated.activation_score if activated else 0.0
            combined = max(retrieval_score, activation_score)

            # Build reasons tuple
            reasons: list[str] = []

            # High lexical similarity (only when retrieval score is high)
            if retrieval_score >= _HIGH_RETRIEVAL_THRESHOLD:
                reasons.append("High lexical similarity")

            # Hop-based reasons
            if activated is not None:
                if activated.hop == 1 and activated.via_path:
                    reasons.append(f"One hop from {activated.via_path}")
                elif activated.hop > 1 and activated.via_path:
                    reasons.append(
                        f"Activated from {activated.via_path} at {activated.hop} hops"
                    )

            # Include seed reasons
            if seed:
                reasons.extend(seed.reasons)

            results.append(
                _Candidate(
                    path=path,
                    retrieval_score=retrieval_score,
                    activation_score=activation_score,
                    combined=combined,
                    reasons=tuple(reasons),
                )
            )

        # Sort by combined score descending, then path ascending
        results.sort(key=lambda c: (-c.combined, c.path))
        return results

    def _create_excerpt(self, note: Note, query_tokens: set[str]) -> str:
        """Create an excerpt from indexed note data.

        Priority:
        1. Summary if it contains a normalized query token
        2. Content window nearest the first query token match
        3. First non-empty content window
        """
        # Priority 1: Summary with query token match
        if note.summary:
            summary_tokens = {t.lower() for t in _plain_tokens(note.summary)}
            if query_tokens & summary_tokens:
                return note.summary

        # Build content windows
        content = note.content or ""
        windows = self._content_windows(content)

        if not windows:
            return ""

        # Priority 2: Window nearest first token match
        first_match_pos = _first_token_match(content, query_tokens)
        if first_match_pos is not None:
            for start, text in windows:
                end = start + len(text)
                if start <= first_match_pos < end:
                    return text

        # Priority 3: First non-empty content window
        return windows[0][1]

    def _content_windows(self, content: str) -> list[tuple[int, str]]:
        """Split content into bounded windows.

        Returns list of (start_position, text) tuples.
        """
        windows: list[tuple[int, str]] = []
        pos = 0
        while pos < len(content):
            end = min(pos + _CONTENT_WINDOW, len(content))
            text = content[pos:end]
            if text:
                windows.append((pos, text))
            pos = end + _GAP
        return windows

    def _select_with_budget(
        self,
        candidates: list[_Candidate],
        note_map: dict[str, Note],
        query_tokens: set[str],
        token_budget: int,
    ) -> tuple[ContextMemory, ...]:
        """Select candidates within the token budget.

        Returns at least one candidate if it individually fits the budget.
        """
        selected: list[ContextMemory] = []
        total_tokens = 0
        first_fit = False

        for candidate in candidates:
            note = note_map.get(candidate.path)
            if note is None:
                continue

            excerpt = self._create_excerpt(note, query_tokens)
            tokens = _estimate_tokens(excerpt)

            # Check budget — skip if exceeds (unless no first fit yet)
            if total_tokens + tokens > token_budget:
                continue

            selected.append(
                ContextMemory(
                    path=candidate.path,
                    title=note.title,
                    summary=note.summary,
                    excerpt=excerpt,
                    retrieval_score=candidate.retrieval_score,
                    activation_score=candidate.activation_score,
                    reasons=candidate.reasons,
                    estimated_tokens=tokens,
                )
            )
            total_tokens += tokens
            if not first_fit:
                first_fit = True

        return tuple(selected)


class _Candidate:
    """Internal scoring candidate for context selection."""

    __slots__ = ("path", "retrieval_score", "activation_score", "combined", "reasons")

    def __init__(
        self,
        path: str,
        retrieval_score: float,
        activation_score: float,
        combined: float,
        reasons: tuple[str, ...],
    ) -> None:
        self.path = path
        self.retrieval_score = retrieval_score
        self.activation_score = activation_score
        self.combined = combined
        self.reasons = reasons


def _first_token_match(content: str, tokens: set[str]) -> int | None:
    """Return the position of the first occurrence of any token in content."""
    best: int | None = None
    for token in tokens:
        pos = content.find(token)
        if pos != -1:
            if best is None or pos < best:
                best = pos
    return best
