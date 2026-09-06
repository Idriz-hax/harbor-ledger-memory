"""Tests for deterministic hybrid seed retrieval."""

import struct
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import Note, NoteEmbedding
from harbor_ledger_memory.config import MemorySettings
from harbor_ledger_memory.domain.retrieval import QuerySettings
from harbor_ledger_memory.services import embeddings as embeddings_module
from harbor_ledger_memory.services import retrieval as retrieval_module
from harbor_ledger_memory.services.retrieval import HybridRetrievalService

_SETTINGS = QuerySettings()


def _blob_for(vector: tuple[float, ...]) -> bytes:
    """Serialize a test embedding vector."""
    return struct.pack(f"<{len(vector)}f", *vector)


class FakeEmbeddingService:
    """Deterministic embedding service for retrieval tests."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def encode(self, text: str) -> tuple[float, ...]:
        return (1.0, 0.0)

    def from_blob(self, blob: bytes) -> tuple[float, ...]:
        return struct.unpack(f"<{len(blob) // 4}f", blob)

    def to_blob(self, vector: tuple[float, ...]) -> bytes:
        return _blob_for(vector)


def _make_session(tmp_path: Path) -> tuple[Path, Session]:
    """Create a test database and session pre-populated with fixtures."""
    db_path = tmp_path / "retrieval.db"
    engine = create_database(f"sqlite:///{db_path}")
    session = CatalogSession(bind=engine)
    return db_path, session


def _seed_notes(session: Session, notes: list[dict[str, str]]) -> None:
    """Insert notes into the catalog and commit."""
    for kwargs in notes:
        note = Note(**kwargs)  # type: ignore[arg-type]
        note.content_hash = kwargs.get("content_hash", "h")  # type: ignore[assignment]
        session.add(note)
    session.commit()


# -- Fixture tests -----------------------------------------------------------


class TestExactPathDominance:
    """Exact path token match should dominate all other signals."""

    def test_exact_path_match_scores_highest(self, tmp_path: Path) -> None:
        """A note whose path exactly matches a query token gets +1.00."""
        _, session = _make_session(tmp_path)
        _seed_notes(
            session,
            [
                {
                    "path": "AI/Memory/ai-memory.md",
                    "title": "Something else",
                    "content": "completely different topic",
                    "summary": "different",
                    "frontmatter_json": '{"tags": ["ai-memory"]}',
                },
                {
                    "path": "Z/other/caching.md",
                    "title": "Cache",
                    "content": "about ai memory systems",
                    "summary": "memory",
                },
            ],
        )
        service = HybridRetrievalService(session, _SETTINGS)
        candidates = service.seeds("ai-memory")
        assert len(candidates) == 2
        # Exact path match + tag (AI/Memory/ai-memory.md) should beat FTS-only
        assert candidates[0].path == "AI/Memory/ai-memory.md"
        assert "Exact path match" in candidates[0].reasons


class TestTagOnlyRetrieval:
    """Notes matching only by tags should still be retrieved."""

    def test_tag_match_without_fts_match(self, tmp_path: Path) -> None:
        """A note with a matching tag but no FTS content match should appear."""
        _, session = _make_session(tmp_path)
        _seed_notes(
            session,
            [
                {
                    "path": "AI/Projects/design.md",
                    "title": "Design doc",
                    "content": "architecture and patterns",
                    "summary": "design patterns",
                    "frontmatter_json": '{"tags": ["ai-memory", "design"]}',
                },
            ],
        )
        service = HybridRetrievalService(session, _SETTINGS)
        candidates = service.seeds("ai-memory")
        # Should find note by tag even though FTS won't match "ai-memory"
        assert len(candidates) >= 1
        tag_candidate = [c for c in candidates if c.path == "AI/Projects/design.md"]
        assert len(tag_candidate) == 1
        assert any("Tag match" in r for r in tag_candidate[0].reasons)


class TestShortTermCache:
    """Short-term cache paths should participate in retrieval."""

    def test_cache_path_is_candidate_without_lexical_match(
        self, tmp_path: Path
    ) -> None:
        """A cached path is returned even when it has no query match."""
        _, session = _make_session(tmp_path)
        _seed_notes(
            session,
            [
                {
                    "path": "AI/cache.md",
                    "title": "Cached",
                    "content": "unrelated",
                }
            ],
        )
        result = HybridRetrievalService(session, _SETTINGS).seeds(
            "no match", recent_paths={"AI/cache.md": 0.25}
        )
        assert result[0].reasons == ("Short-term cache (+0.25)",)

    def test_cache_boost_composes_with_lexical_score(self, tmp_path: Path) -> None:
        """A cached lexical hit retains both signals in its score and reasons."""
        _, session = _make_session(tmp_path)
        _seed_notes(
            session,
            [
                {
                    "path": "AI/cache.md",
                    "title": "Cached",
                    "content": "memory",
                }
            ],
        )

        result = HybridRetrievalService(session, _SETTINGS).seeds(
            "memory", recent_paths={"AI/cache.md": 0.25}
        )

        assert result[0].retrieval_score == 0.75
        assert result[0].reasons == (
            "Full-text match",
            "Short-term cache (+0.25)",
        )

    def test_cache_tie_is_broken_by_path_ascending(self, tmp_path: Path) -> None:
        """Equal cache-only candidates have deterministic path ordering."""
        _, session = _make_session(tmp_path)
        _seed_notes(
            session,
            [
                {"path": "AI/z-cache.md", "title": "Z", "content": "unrelated"},
                {"path": "AI/a-cache.md", "title": "A", "content": "unrelated"},
            ],
        )

        result = HybridRetrievalService(session, _SETTINGS).seeds(
            "no match",
            recent_paths={"AI/z-cache.md": 0.25, "AI/a-cache.md": 0.25},
        )

        assert [candidate.path for candidate in result] == [
            "AI/a-cache.md",
            "AI/z-cache.md",
        ]


class TestProjectBoost:
    """Notes within the active project path should get a boost."""

    def test_active_project_path_match(self, tmp_path: Path) -> None:
        """Notes under the active project directory get +0.35."""
        _, session = _make_session(tmp_path)
        _seed_notes(
            session,
            [
                {
                    "path": "projects/my-project/planning.md",
                    "title": "Planning",
                    "content": "planning and tasks for the sprint",
                    "summary": "sprint planning tasks",
                },
                {
                    "path": "AI/General/random-notes.md",
                    "title": "Random",
                    "content": "tasks are important for productivity",
                    "summary": "random thoughts",
                },
            ],
        )
        service = HybridRetrievalService(session, _SETTINGS)
        candidates = service.seeds("tasks", active_project="my-project")
        # Project-matched note should rank higher (project boost + summary vs FTS only)
        assert candidates[0].path == "projects/my-project/planning.md"
        assert "Active project match" in candidates[0].reasons


class TestDeterministicTies:
    """Equal scores must be broken deterministically by path."""

    def test_tie_broken_by_path_ascending(self, tmp_path: Path) -> None:
        """When two notes have identical scores, path ascending wins."""
        _, session = _make_session(tmp_path)
        _seed_notes(
            session,
            [
                {
                    "path": "Z-notes/zeta.md",
                    "title": "Same Content",
                    "content": "identical query matching text",
                    "summary": "same summary",
                },
                {
                    "path": "A-notes/alpha.md",
                    "title": "Same Content",
                    "content": "identical query matching text",
                    "summary": "same summary",
                },
            ],
        )
        service = HybridRetrievalService(session, _SETTINGS)
        candidates = service.seeds("identical query matching")
        assert len(candidates) == 2
        assert candidates[0].path == "A-notes/alpha.md"
        assert candidates[1].path == "Z-notes/zeta.md"


class TestEmptyAndNoMatch:
    """Empty queries and no-match queries should return empty results."""

    def test_empty_query_returns_empty(self, tmp_path: Path) -> None:
        """Empty string query returns no candidates."""
        _, session = _make_session(tmp_path)
        _seed_notes(
            session,
            [
                {
                    "path": "AI/note.md",
                    "title": "Note",
                    "content": "some content",
                    "summary": "summary",
                },
            ],
        )
        service = HybridRetrievalService(session, _SETTINGS)
        assert service.seeds("") == ()
        assert service.seeds("   ") == ()

    def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        """Query with no matches returns empty tuple."""
        _, session = _make_session(tmp_path)
        _seed_notes(
            session,
            [
                {
                    "path": "AI/note.md",
                    "title": "Note",
                    "content": "completely unrelated",
                    "summary": "nothing relevant",
                },
            ],
        )
        service = HybridRetrievalService(session, _SETTINGS)
        candidates = service.seeds("xyzzy-unique-nonexistent")
        # No FTS match, no tag match, no path match -> empty
        assert candidates == ()


class TestMaxSeedNodesLimit:
    """Results should be limited to max_seed_nodes."""

    def test_respects_max_seed_nodes(self, tmp_path: Path) -> None:
        """Only max_seed_nodes candidates should be returned."""
        _, session = _make_session(tmp_path)
        notes = [
            {
                "path": f"AI/note-{i}.md",
                "title": f"Note {i}",
                "content": f"common search term note {i}",
                "summary": f"summary {i}",
            }
            for i in range(15)
        ]
        _seed_notes(session, notes)
        service = HybridRetrievalService(session, _SETTINGS)
        candidates = service.seeds("common search term")
        # Default max_seed_nodes is 10
        assert len(candidates) <= 10


class TestScoreClamping:
    """Scores should be clamped to 1.0 maximum."""

    def test_score_clamped_to_one(self, tmp_path: Path) -> None:
        """A note matching all signals should still have score <= 1.0."""
        _, session = _make_session(tmp_path)
        _seed_notes(
            session,
            [
                {
                    "path": "AI/nlp/nlp.md",
                    "title": "nlp",
                    "content": "natural language processing nlp nlp nlp",
                    "summary": "nlp techniques",
                    "frontmatter_json": '{"tags": ["nlp"]}',
                },
            ],
        )
        service = HybridRetrievalService(session, _SETTINGS)
        candidates = service.seeds(
            "nlp",
            active_project="nlp",
            recent_paths={"AI/nlp/nlp.md": 0.25},
        )
        assert len(candidates) == 1
        assert candidates[0].retrieval_score == 1.0


class TestReasons:
    """Each candidate should have human-readable reasons."""

    def test_reasons_reflect_active_signals(self, tmp_path: Path) -> None:
        """Reasons should correspond to the signals that contributed."""
        _, session = _make_session(tmp_path)
        _seed_notes(
            session,
            [
                {
                    "path": "AI/Knowledge/ml-models.md",
                    "title": "ML Models",
                    "content": "machine learning models and training",
                    "summary": "ml training guide",
                    "frontmatter_json": '{"tags": ["ml", "models"]}',
                },
            ],
        )
        service = HybridRetrievalService(session, _SETTINGS)
        candidates = service.seeds("ml models")
        assert len(candidates) == 1
        # Should have at least FTS and title/tag reasons
        assert len(candidates[0].reasons) >= 1


class TestSemanticSimilarity:
    """Semantic similarity should boost non-lexical candidates."""

    def test_semantic_only_note_is_a_seed_when_embeddings_are_enabled(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_embedding_runtime: None,
    ) -> None:
        """Global semantic candidates are included for the configured model."""
        _, session = _make_session(tmp_path)
        session.add(
            Note(
                path="AI/Projects/semantic.md",
                title="Concept",
                content="none",
                content_hash="h",
            )
        )
        session.add(
            NoteEmbedding(
                note_path="AI/Projects/semantic.md",
                embedding_blob=_blob_for((1.0, 0.0)),
                model_name="test-model",
                created_at="2026-08-09T00:00:00+00:00",
            )
        )
        session.add(
            Note(
                path="AI/Projects/other-model.md",
                title="Different",
                content="none",
                content_hash="h",
            )
        )
        session.add(
            NoteEmbedding(
                note_path="AI/Projects/other-model.md",
                embedding_blob=_blob_for((1.0, 0.0)),
                model_name="other-model",
                created_at="2026-08-09T00:00:00+00:00",
            )
        )
        session.commit()

        monkeypatch.setattr(embeddings_module, "EmbeddingService", FakeEmbeddingService)
        monkeypatch.setattr(
            retrieval_module, "EmbeddingService", FakeEmbeddingService, raising=False
        )
        seeds = HybridRetrievalService(
            session,
            memory_settings=MemorySettings(HLM_EMBEDDING_MODEL="test-model"),
        ).seeds("unrelated words")

        semantic_seed = next(
            seed for seed in seeds if seed.path.endswith("semantic.md")
        )
        assert semantic_seed.reasons[-1] == "Semantic similarity (1.00)"
        assert not any(seed.path.endswith("other-model.md") for seed in seeds)

    def test_active_project_is_a_boost_not_a_semantic_filter(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_embedding_runtime: None,
    ) -> None:
        """Semantic candidates outside the active project remain eligible."""
        _, session = _make_session(tmp_path)
        for path in (
            "AI/Projects/in-project.md",
            "AI/General/global.md",
        ):
            session.add(
                Note(
                    path=path,
                    title="Concept",
                    content="none",
                    content_hash="h",
                )
            )
            session.add(
                NoteEmbedding(
                    note_path=path,
                    embedding_blob=_blob_for((1.0, 0.0)),
                    model_name="test-model",
                    created_at="2026-08-09T00:00:00+00:00",
                )
            )
        session.commit()

        monkeypatch.setattr(embeddings_module, "EmbeddingService", FakeEmbeddingService)
        monkeypatch.setattr(
            retrieval_module, "EmbeddingService", FakeEmbeddingService, raising=False
        )
        seeds = HybridRetrievalService(
            session,
            memory_settings=MemorySettings(HLM_EMBEDDING_MODEL="test-model"),
        ).seeds("unrelated words", active_project="AI/Projects")

        assert [seed.path for seed in seeds] == [
            "AI/Projects/in-project.md",
            "AI/General/global.md",
        ]
        assert "Active project match" in seeds[0].reasons
        assert "Active project match" not in seeds[1].reasons

    def test_semantic_similarity_boosts_non_lexical_candidates(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mock_embedding_runtime: None,
    ) -> None:
        """Semantic relevance boosts notes without exact lexical match."""
        monkeypatch.setattr(embeddings_module, "EmbeddingService", FakeEmbeddingService)
        monkeypatch.setattr(
            retrieval_module, "EmbeddingService", FakeEmbeddingService, raising=False
        )

        _, session = _make_session(tmp_path)

        # Create a note whose content is semantically related to the query
        # but does NOT contain the exact query term "management" for FTS
        # (only has "manage" which FTS won't match against "management")
        note_path = "AI/Knowledge/strategy.md"
        note_content = (
            "We need to manage information better and organize our collective "
            "knowledge for the team. This involves building systems that help "
            "people find what they need quickly."
        )
        session.add(
            Note(
                path=note_path,
                title="Strategy",
                content=note_content,
                content_hash="h",
                frontmatter_json='{"tags": ["management"]}',
            )
        )
        session.commit()

        # Embed the note
        service = FakeEmbeddingService("all-MiniLM-L6-v2")
        text = f"Strategy {note_content}"
        vector = service.encode(text)
        session.add(
            NoteEmbedding(
                note_path=note_path,
                embedding_blob=service.to_blob(vector),
                model_name="all-MiniLM-L6-v2",
                created_at="2026-08-09T12:00:00",
            )
        )
        session.commit()

        # Query for semantically related but lexically different terms
        memory = MemorySettings(HLM_EMBEDDING_MODEL="all-MiniLM-L6-v2")
        retrieval = HybridRetrievalService(
            session,
            memory_settings=memory,
        )
        seeds = retrieval.seeds("knowledge management")

        # The note should appear because it has "knowledge" and "manage" in content
        # (FTS will match "knowledge AND management" since both terms exist)
        # Semantic similarity should boost the score
        paths = [s.path for s in seeds]
        assert note_path in paths

        # Find the candidate and verify semantic similarity is in the reasons
        candidate = next(s for s in seeds if s.path == note_path)
        assert any("Semantic similarity" in r for r in candidate.reasons)

    def test_seeds_degrade_without_semantic_signal_when_runtime_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured model with a missing runtime still returns lexical hits."""
        embeddings_module.reset_runtime_warning_flag()
        monkeypatch.setattr(
            embeddings_module, "embedding_runtime_available", lambda: False
        )

        class _ConstructionFailingEmbeddingService:
            def __init__(self, model_name: str) -> None:
                raise AssertionError(f"encoder should not be constructed: {model_name}")

        monkeypatch.setattr(
            retrieval_module,
            "EmbeddingService",
            _ConstructionFailingEmbeddingService,
            raising=False,
        )

        _, session = _make_session(tmp_path)
        session.add(
            Note(
                path="AI/Knowledge/memory.md",
                title="Memory",
                content="notes about memory systems",
                content_hash="h",
                summary="memory",
            )
        )
        session.add(
            NoteEmbedding(
                note_path="AI/Knowledge/memory.md",
                embedding_blob=_blob_for((1.0, 0.0)),
                model_name="test-model",
                created_at="2026-08-09T00:00:00+00:00",
            )
        )
        session.commit()

        seeds = HybridRetrievalService(
            session, memory_settings=MemorySettings(HLM_EMBEDDING_MODEL="test-model")
        ).seeds("memory")

        # The lexical hit is returned, but without any semantic contribution.
        assert len(seeds) >= 1
        memory_seed = next(s for s in seeds if s.path == "AI/Knowledge/memory.md")
        assert "Full-text match" in memory_seed.reasons
        assert not any("Semantic similarity" in r for r in memory_seed.reasons)
