"""Tests for context builder — smallest-sufficient context package."""

from __future__ import annotations

import math
from pathlib import Path

from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import Note
from harbor_ledger_memory.domain.retrieval import (
    ActivatedNode,
    SeedCandidate,
)
from harbor_ledger_memory.services.context import ContextBuilder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTENT_LONG = " ".join(["word"] * 300)  # ~1200 chars of content


def _create_session(tmp_path: Path) -> tuple[Path, Session]:
    """Create an in-memory test database and session."""
    db_path = tmp_path / "context.db"
    engine = create_database(f"sqlite:///{db_path}")
    session = CatalogSession(bind=engine)
    return db_path, session


def _insert_note(
    session: Session,
    path: str,
    title: str = "Title",
    content: str = "",
    summary: str | None = None,
) -> None:
    """Insert a note into the catalog."""
    session.add(
        Note(
            path=path,
            title=title,
            content=content,
            summary=summary,
            frontmatter_json="{}",
            content_hash="h",
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# Excerpt tests — summary preference
# ---------------------------------------------------------------------------


class TestSummaryPreference:
    """Summary should be used as excerpt when it contains a normalized query token."""

    def test_summary_used_when_contains_query_token(self, tmp_path: Path) -> None:
        """If the summary contains a query token, use it as the excerpt."""
        _, session = _create_session(tmp_path)
        _insert_note(
            session,
            path="AI/note.md",
            title="Memory",
            content="completely unrelated long content text here",
            summary="memory systems are important",
        )

        builder = ContextBuilder(session)
        results = builder.build(
            query="memory",
            seeds=(SeedCandidate(path="AI/note.md", retrieval_score=0.9),),
            activated=(),
            token_budget=10_000,
        )

        assert len(results) == 1
        assert results[0].excerpt == "memory systems are important"

    def test_summary_not_used_when_no_query_token(self, tmp_path: Path) -> None:
        """Summary without a matching query token should not be used."""
        _, session = _create_session(tmp_path)
        _insert_note(
            session,
            path="AI/note.md",
            title="Note",
            content="this is about memory and recall",
            summary="something else entirely",
        )

        builder = ContextBuilder(session)
        results = builder.build(
            query="memory",
            seeds=(SeedCandidate(path="AI/note.md", retrieval_score=0.5),),
            activated=(),
            token_budget=10_000,
        )

        assert len(results) == 1
        # Excerpt should come from content, not summary
        assert results[0].excerpt != "something else entirely"
        assert "memory" in results[0].excerpt


# ---------------------------------------------------------------------------
# Excerpt tests — nearest-match content window
# ---------------------------------------------------------------------------


class TestNearestMatchExcerpt:
    """Content window nearest the first token match should be selected."""

    def test_nearest_content_window_selected(self, tmp_path: Path) -> None:
        """When summary doesn't match, pick the content window nearest
        the first occurrence of a query token.
        """
        _, session = _create_session(tmp_path)
        # Token "memory" appears far into the content
        content = "intro text here " * 100 + "memory is key " + "trailing text " * 50
        _insert_note(
            session,
            path="AI/note.md",
            title="Note",
            content=content,
            summary="no matching token here",
        )

        builder = ContextBuilder(session)
        results = builder.build(
            query="memory",
            seeds=(SeedCandidate(path="AI/note.md", retrieval_score=0.5),),
            activated=(),
            token_budget=10_000,
        )

        assert len(results) == 1
        # The excerpt should contain "memory"
        assert "memory" in results[0].excerpt

    def test_fallback_to_first_content_window(self, tmp_path: Path) -> None:
        """When no token matches in content, use the first non-empty window."""
        _, session = _create_session(tmp_path)
        _insert_note(
            session,
            path="AI/note.md",
            title="Note",
            content="nothing relevant in this paragraph of text",
            summary="also nothing relevant",
        )

        builder = ContextBuilder(session)
        results = builder.build(
            query="xyzzy",
            seeds=(SeedCandidate(path="AI/note.md", retrieval_score=0.3),),
            activated=(),
            token_budget=10_000,
        )

        assert len(results) == 1
        # Should get first content window
        assert results[0].excerpt == "nothing relevant in this paragraph of text"


# ---------------------------------------------------------------------------
# Ranking tests
# ---------------------------------------------------------------------------


class TestDeterministicRanking:
    """Candidates ranked by combined score descending, path ascending."""

    def test_higher_combined_score_ranks_first(self, tmp_path: Path) -> None:
        """A candidate with a higher combined score comes first."""
        _, session = _create_session(tmp_path)
        _insert_note(session, "AI/high.md", content="high score note")
        _insert_note(session, "AI/low.md", content="low score note")

        builder = ContextBuilder(session)
        results = builder.build(
            query="note",
            seeds=(
                SeedCandidate(path="AI/high.md", retrieval_score=0.9),
                SeedCandidate(path="AI/low.md", retrieval_score=0.3),
            ),
            activated=(),
            token_budget=10_000,
        )

        assert len(results) == 2
        assert results[0].path == "AI/high.md"
        assert results[1].path == "AI/low.md"

    def test_tie_broken_by_path_ascending(self, tmp_path: Path) -> None:
        """Equal combined scores should be broken by path ascending."""
        _, session = _create_session(tmp_path)
        _insert_note(session, "Z/zeta.md", content="content")
        _insert_note(session, "A/alpha.md", content="content")

        builder = ContextBuilder(session)
        results = builder.build(
            query="content",
            seeds=(
                SeedCandidate(path="Z/zeta.md", retrieval_score=0.5),
                SeedCandidate(path="A/alpha.md", retrieval_score=0.5),
            ),
            activated=(),
            token_budget=10_000,
        )

        assert len(results) == 2
        assert results[0].path == "A/alpha.md"
        assert results[1].path == "Z/zeta.md"


# ---------------------------------------------------------------------------
# Budget tests
# ---------------------------------------------------------------------------


class TestBudgetExclusion:
    """Candidates excluded when total estimated tokens exceed budget."""

    def test_candidate_excluded_when_exceeds_budget(self, tmp_path: Path) -> None:
        """Only candidates that fit within the token budget are returned."""
        _, session = _create_session(tmp_path)
        _insert_note(session, "AI/big.md", content=_CONTENT_LONG)
        _insert_note(session, "AI/small.md", content="short")

        builder = ContextBuilder(session)
        # Budget large enough for "small" but not both
        results = builder.build(
            query="note",
            seeds=(
                SeedCandidate(path="AI/big.md", retrieval_score=0.8),
                SeedCandidate(path="AI/small.md", retrieval_score=0.7),
            ),
            activated=(),
            token_budget=100,
        )

        # "small" fits (ceil(5/4) = 2 tokens), "big" (~1200 chars = ~300 tokens) doesn't
        # Total with just big = ~300 > 100, so big gets excluded after being first
        # Actually: big is first by score, ceil(~1200/4) = ~300 > 100, excluded
        # small is second, ceil(5/4) = 2 <= 100, included
        assert all(r.path != "AI/big.md" for r in results)
        assert len(results) >= 1
        assert results[0].path == "AI/small.md"

    def test_one_large_note_rejected(self, tmp_path: Path) -> None:
        """If the single highest candidate doesn't fit the budget, return empty."""
        _, session = _create_session(tmp_path)
        _insert_note(session, "AI/huge.md", content=_CONTENT_LONG)

        builder = ContextBuilder(session)
        results = builder.build(
            query="note",
            seeds=(SeedCandidate(path="AI/huge.md", retrieval_score=1.0),),
            activated=(),
            token_budget=10,
        )

        # ~1200 chars / 4 = ~300 tokens >> 10 budget
        assert results == ()


# ---------------------------------------------------------------------------
# Reason composition tests
# ---------------------------------------------------------------------------


class TestReasonComposition:
    """Reasons stored only when the relevant measured condition exists."""

    def test_high_lexical_similarity_reason(self, tmp_path: Path) -> None:
        """High lexical similarity reason appears when retrieval score is high."""
        _, session = _create_session(tmp_path)
        _insert_note(session, "AI/note.md", content="relevant content")

        builder = ContextBuilder(session)
        results = builder.build(
            query="note",
            seeds=(
                SeedCandidate(
                    path="AI/note.md",
                    retrieval_score=0.8,
                    reasons=("Full-text match",),
                ),
            ),
            activated=(),
            token_budget=10_000,
        )

        assert len(results) == 1
        assert "High lexical similarity" in results[0].reasons

    def test_one_hop_reason(self, tmp_path: Path) -> None:
        """One hop reason appears when a node is one hop from a seed."""
        _, session = _create_session(tmp_path)
        _insert_note(session, "AI/note.md", content="content")
        _insert_note(session, "AI/linked.md", content="linked content")

        builder = ContextBuilder(session)
        results = builder.build(
            query="note",
            seeds=(SeedCandidate(path="AI/note.md", retrieval_score=0.5),),
            activated=(
                ActivatedNode(
                    path="AI/linked.md",
                    activation_score=0.4,
                    hop=1,
                    via_path="AI/note.md",
                    edge_type="links_to",
                ),
            ),
            token_budget=10_000,
        )

        # Find the activated note
        linked = [r for r in results if r.path == "AI/linked.md"]
        assert len(linked) == 1
        assert "One hop from AI/note.md" in linked[0].reasons

    def test_seed_reasons_included(self, tmp_path: Path) -> None:
        """Reasons from seed candidates are passed through."""
        _, session = _create_session(tmp_path)
        _insert_note(session, "AI/note.md", content="content")

        builder = ContextBuilder(session)
        results = builder.build(
            query="note",
            seeds=(
                SeedCandidate(
                    path="AI/note.md",
                    retrieval_score=0.6,
                    reasons=("Full-text match", "Title match"),
                ),
            ),
            activated=(),
            token_budget=10_000,
        )

        assert len(results) == 1
        assert "Full-text match" in results[0].reasons
        assert "Title match" in results[0].reasons

    def test_no_high_lexical_similarity_for_low_score(self, tmp_path: Path) -> None:
        """High lexical similarity should not appear for low retrieval scores."""
        _, session = _create_session(tmp_path)
        _insert_note(session, "AI/note.md", content="content")

        builder = ContextBuilder(session)
        results = builder.build(
            query="note",
            seeds=(
                SeedCandidate(
                    path="AI/note.md",
                    retrieval_score=0.2,
                    reasons=(),
                ),
            ),
            activated=(),
            token_budget=10_000,
        )

        assert len(results) == 1
        assert "High lexical similarity" not in results[0].reasons


# ---------------------------------------------------------------------------
# Token estimation test
# ---------------------------------------------------------------------------


class TestTokenEstimation:
    """Tokens estimated as ceil(len(excerpt) / 4)."""

    def test_token_estimation_formula(self, tmp_path: Path) -> None:
        """estimated_tokens equals ceil(len(excerpt) / 4)."""
        _, session = _create_session(tmp_path)
        content = "x" * 100
        _insert_note(session, "AI/note.md", content=content, summary="summary text")

        builder = ContextBuilder(session)
        results = builder.build(
            query="x",
            seeds=(SeedCandidate(path="AI/note.md", retrieval_score=0.5),),
            activated=(),
            token_budget=10_000,
        )

        assert len(results) == 1
        expected = math.ceil(len(results[0].excerpt) / 4)
        assert results[0].estimated_tokens == expected
