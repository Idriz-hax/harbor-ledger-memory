import hashlib
import struct
from pathlib import Path

import pytest
from sqlalchemy import select

from harbor_ledger_memory.catalog.database import (
    CatalogSession,
    create_database,
    resolve_database_url,
)
from harbor_ledger_memory.catalog.migrate import (
    upgrade_to_head as catalog_upgrade_to_head,
)
from harbor_ledger_memory.catalog.models import Link, Note, NoteEmbedding, ScanRun
from harbor_ledger_memory.config import MemorySettings, Settings
from harbor_ledger_memory.migrate import upgrade_to_head
from harbor_ledger_memory.services import embeddings as embeddings_module
from harbor_ledger_memory.services import scan as scan_module
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.vault.boundary import VaultBoundary

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"


def test_relative_database_url_is_stable_across_working_directories(
    tmp_path: Path,
) -> None:
    first = resolve_database_url("sqlite:///data/memory.db", base_dir=tmp_path)
    second = resolve_database_url("sqlite:///data/memory.db", base_dir=tmp_path)
    assert first == second == f"sqlite:///{tmp_path / 'data/memory.db'}"
    absolute = f"sqlite:///{tmp_path / 'explicit.db'}"
    assert resolve_database_url(absolute) == absolute


def test_migration_uses_the_same_resolved_relative_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    catalog_upgrade_to_head("sqlite:///data/memory.db")
    assert (tmp_path / "home/.config/harbor-ledger-memory/data/memory.db").exists()


def test_public_migration_entry_point_is_cwd_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    expected = tmp_path / "home/.config/harbor-ledger-memory/data/memory.db"
    create_database(f"sqlite:///{expected}").dispose()
    monkeypatch.chdir(first)
    upgrade_to_head("sqlite:///data/memory.db")
    monkeypatch.chdir(second)
    upgrade_to_head("sqlite:///data/memory.db")
    assert expected.exists()
    assert not (first / "data/memory.db").exists()
    assert not (second / "data/memory.db").exists()


class FakeEmbeddingService:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def encode_batch(self, texts: list[str]) -> list[tuple[float, ...]]:
        return [tuple([float(i)] * 4) for i, _ in enumerate(texts, 1)]

    def to_blob(self, vector: tuple[float, ...]) -> bytes:
        return struct.pack(f"<{len(vector)}f", *vector)


class FailingEmbeddingService:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    def encode_batch(self, texts: list[str]) -> list[tuple[float, ...]]:
        raise RuntimeError("encoder unavailable")


class ConstructionFailingEmbeddingService:
    def __init__(self, model_name: str) -> None:
        raise AssertionError(f"encoder should not be constructed: {model_name}")


class FewerVectorsEmbeddingService(FakeEmbeddingService):
    def encode_batch(self, texts: list[str]) -> list[tuple[float, ...]]:
        return super().encode_batch(texts)[:-1]


class ExtraVectorsEmbeddingService(FakeEmbeddingService):
    def encode_batch(self, texts: list[str]) -> list[tuple[float, ...]]:
        return super().encode_batch(texts) + [(99.0,) * 4]


def fixture_settings(tmp_path: Path, memory: MemorySettings) -> Settings:
    return Settings(
        vault_path=FIXTURE_VAULT,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        memory=memory,
    )


def make_scan_service(vault: Path) -> tuple[ScanService, CatalogSession]:
    engine = create_database(f"sqlite:///{vault / 'catalog.db'}")
    session = CatalogSession(bind=engine)
    return ScanService(
        VaultBoundary(Settings(vault_path=vault, index_root="AI")), session
    ), session


def test_full_scan_indexes_only_ai_tree(tmp_path: Path) -> None:
    ai = tmp_path / "AI"
    (ai / "Knowledge").mkdir(parents=True)
    (ai / "INDEX.md").write_text("# Root\n\n[[AI/Knowledge/example]]", encoding="utf-8")
    (ai / "Knowledge" / "example.md").write_text(
        "---\ntype: knowledge\nstatus: active\n---\n# Example\n", encoding="utf-8"
    )
    (tmp_path / "private.md").write_text("must not be indexed", encoding="utf-8")

    service, session = make_scan_service(tmp_path)
    try:
        result = service.full_scan()
        assert result.files_indexed == 2
        assert all(path.startswith("AI/") for path in result.indexed_paths)
        assert session.scalars(select(Note)).all()[0].path.startswith("AI/")
    finally:
        session.close()


def test_full_scan_persists_sha256_for_snapshot_bytes(tmp_path: Path) -> None:
    ai = tmp_path / "AI"
    ai.mkdir()
    content = b"---\ntype: knowledge\nstatus: active\n---\n# Hashed\n"
    (ai / "hashed.md").write_bytes(content)

    service, session = make_scan_service(tmp_path)
    try:
        service.full_scan()
        note = session.scalars(select(Note).where(Note.path == "AI/hashed.md")).one()

        assert note.content_hash == hashlib.sha256(content).hexdigest()
    finally:
        session.close()


def test_full_scan_uses_one_immutable_boundary_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    ai = tmp_path / "AI"
    ai.mkdir()
    note_path = ai / "snapshot.md"
    original = b"---\ntype: knowledge\nstatus: active\n---\n# Original\n"
    note_path.write_bytes(original)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"---\ntype: knowledge\nstatus: active\n---\n# Outside\n")

    service, session = make_scan_service(tmp_path)
    try:
        snapshot = next(service.boundary.iter_admitted_snapshots())
        note_path.unlink()
        note_path.symlink_to(outside)
        monkeypatch.setattr(
            service.boundary,
            "iter_admitted_snapshots",
            lambda: iter((snapshot,)),
        )

        service.full_scan()
        note = session.scalars(select(Note).where(Note.path == "AI/snapshot.md")).one()

        assert note.content == "# Original\n"
        assert note.content_hash == hashlib.sha256(original).hexdigest()
    finally:
        session.close()


def test_full_scan_is_idempotent_and_resolves_broken_and_ambiguous_links(
    tmp_path: Path,
) -> None:
    ai = tmp_path / "AI"
    (ai / "One").mkdir(parents=True)
    (ai / "Two").mkdir()
    (ai / "INDEX.md").write_text("# Root\n[[missing]]\n[[same]]\n", encoding="utf-8")
    (ai / "One" / "same.md").write_text("# One\n", encoding="utf-8")
    (ai / "Two" / "same.md").write_text("# Two\n", encoding="utf-8")

    service, session = make_scan_service(tmp_path)
    try:
        first = service.full_scan()
        first_links = [
            (link.raw, link.normalized_target, link.resolution_status)
            for link in session.scalars(select(Link)).all()
        ]
        second = service.full_scan()
        second_links = [
            (link.raw, link.normalized_target, link.resolution_status)
            for link in session.scalars(select(Link)).all()
        ]

        assert first.indexed_paths == second.indexed_paths
        assert first.broken_links == second.broken_links == 1
        assert first.ambiguous_links == second.ambiguous_links == 1
        assert first_links == second_links
        assert {status for _, _, status in first_links} == {"broken", "ambiguous"}
    finally:
        session.close()


def test_full_scan_persists_embeddings_when_configured(
    tmp_path: Path, monkeypatch, mock_embedding_runtime: None
) -> None:
    monkeypatch.setattr(scan_module, "EmbeddingService", FakeEmbeddingService)
    settings_with_model = fixture_settings(
        tmp_path, MemorySettings(embedding_model="test-model")
    )
    service = ScanService.from_settings(settings_with_model)
    try:
        service.full_scan()
        rows = service._session.scalars(select(NoteEmbedding)).all()

        expected_paths = ["AI/INDEX.md", "AI/Knowledge/example.md"]
        assert [row.note_path for row in rows] == expected_paths
        assert {row.model_name for row in rows} == {"test-model"}
    finally:
        service._session.close()


def test_full_scan_skips_embeddings_when_model_is_disabled(tmp_path: Path) -> None:
    settings_without_model = fixture_settings(tmp_path, MemorySettings())
    service = ScanService.from_settings(settings_without_model)
    try:
        service.full_scan()
        assert service._session.scalars(select(NoteEmbedding)).all() == []
    finally:
        service._session.close()


def test_full_scan_degrades_gracefully_when_embedding_runtime_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured model with a missing runtime skips embeddings, no exception."""
    embeddings_module.reset_runtime_warning_flag()
    monkeypatch.setattr(embeddings_module, "embedding_runtime_available", lambda: False)
    monkeypatch.setattr(
        scan_module, "EmbeddingService", ConstructionFailingEmbeddingService
    )
    settings = fixture_settings(tmp_path, MemorySettings(embedding_model="test-model"))
    service = ScanService.from_settings(settings)
    try:
        result = service.full_scan()

        # The scan still indexes notes; embeddings are skipped, encoder untouched.
        assert result.files_indexed > 0
        assert service._session.scalars(select(NoteEmbedding)).all() == []
    finally:
        service._session.close()


def test_full_scan_skips_encoder_when_no_inputs_are_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai = tmp_path / "AI"
    ai.mkdir()
    (ai / "brief.md").write_text("# Hi\n", encoding="utf-8")
    monkeypatch.setattr(
        scan_module, "EmbeddingService", ConstructionFailingEmbeddingService
    )
    settings = Settings(
        vault_path=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'catalog.db'}",
        memory=MemorySettings(embedding_model="test-model"),
    )
    service = ScanService.from_settings(settings)
    try:
        result = service.full_scan()

        assert result.files_indexed == 1
        assert service._session.scalars(select(NoteEmbedding)).all() == []
    finally:
        service._session.close()


def test_full_scan_rolls_back_notes_and_embeddings_when_encoding_fails(
    tmp_path: Path, monkeypatch, mock_embedding_runtime: None
) -> None:
    monkeypatch.setattr(scan_module, "EmbeddingService", FailingEmbeddingService)
    settings_with_model = fixture_settings(
        tmp_path, MemorySettings(embedding_model="test-model")
    )
    service = ScanService.from_settings(settings_with_model)
    try:
        with pytest.raises(RuntimeError, match="encoder unavailable"):
            service.full_scan()

        assert service._session.scalars(select(Note)).all() == []
        assert service._session.scalars(select(NoteEmbedding)).all() == []
    finally:
        service._session.close()


def test_full_scan_preserves_previous_catalog_when_rescan_encoding_fails(
    tmp_path: Path, monkeypatch, mock_embedding_runtime: None
) -> None:
    settings = fixture_settings(tmp_path, MemorySettings(embedding_model="test-model"))
    monkeypatch.setattr(scan_module, "EmbeddingService", FakeEmbeddingService)
    initial_service = ScanService.from_settings(settings)
    try:
        initial_service.full_scan()
    finally:
        initial_service._session.close()

    baseline_engine = create_database(settings.database_url)
    baseline_session = CatalogSession(bind=baseline_engine)
    try:
        original_notes = baseline_session.scalars(
            select(Note).order_by(Note.path)
        ).all()
        original_embeddings = baseline_session.scalars(
            select(NoteEmbedding).order_by(NoteEmbedding.note_path)
        ).all()
        original_scan = baseline_session.scalars(select(ScanRun)).one()
        original_note_values = [
            (note.path, note.content_hash, note.scan_timestamp)
            for note in original_notes
        ]
        original_embedding_values = [
            (
                embedding.note_path,
                embedding.embedding_blob,
                embedding.model_name,
                embedding.created_at,
            )
            for embedding in original_embeddings
        ]
        original_scan_values = (
            original_scan.id,
            original_scan.status,
            original_scan.completed_at,
            original_scan.notes_indexed,
        )
    finally:
        baseline_session.close()
        baseline_engine.dispose()

    monkeypatch.setattr(scan_module, "EmbeddingService", FailingEmbeddingService)
    failing_service = ScanService.from_settings(settings)
    try:
        with pytest.raises(RuntimeError, match="encoder unavailable"):
            failing_service.full_scan()
    finally:
        failing_service._session.close()

    fresh_engine = create_database(settings.database_url)
    fresh_session = CatalogSession(bind=fresh_engine)
    try:
        notes = fresh_session.scalars(select(Note).order_by(Note.path)).all()
        embeddings = fresh_session.scalars(
            select(NoteEmbedding).order_by(NoteEmbedding.note_path)
        ).all()
        scan = fresh_session.scalars(select(ScanRun)).one()

        assert [
            (note.path, note.content_hash, note.scan_timestamp) for note in notes
        ] == original_note_values
        assert [
            (
                embedding.note_path,
                embedding.embedding_blob,
                embedding.model_name,
                embedding.created_at,
            )
            for embedding in embeddings
        ] == original_embedding_values
        assert (
            scan.id,
            scan.status,
            scan.completed_at,
            scan.notes_indexed,
        ) == original_scan_values
    finally:
        fresh_session.close()
        fresh_engine.dispose()


@pytest.mark.parametrize(
    ("encoder", "actual_count"),
    [
        (FewerVectorsEmbeddingService, 1),
        (ExtraVectorsEmbeddingService, 3),
    ],
)
def test_full_scan_preserves_previous_embeddings_when_batch_cardinality_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_embedding_runtime: None,
    encoder: type[FakeEmbeddingService],
    actual_count: int,
) -> None:
    settings = fixture_settings(tmp_path, MemorySettings(embedding_model="test-model"))
    monkeypatch.setattr(scan_module, "EmbeddingService", FakeEmbeddingService)
    initial_service = ScanService.from_settings(settings)
    try:
        initial_service.full_scan()
    finally:
        initial_service._session.close()

    baseline_engine = create_database(settings.database_url)
    baseline_session = CatalogSession(bind=baseline_engine)
    try:
        original_embeddings = [
            (embedding.note_path, embedding.embedding_blob, embedding.model_name)
            for embedding in baseline_session.scalars(
                select(NoteEmbedding).order_by(NoteEmbedding.note_path)
            ).all()
        ]
    finally:
        baseline_session.close()
        baseline_engine.dispose()

    monkeypatch.setattr(scan_module, "EmbeddingService", encoder)
    failing_service = ScanService.from_settings(settings)
    try:
        with pytest.raises(
            RuntimeError,
            match=rf"returned {actual_count} vectors for 2 inputs",
        ):
            failing_service.full_scan()
    finally:
        failing_service._session.close()

    fresh_engine = create_database(settings.database_url)
    fresh_session = CatalogSession(bind=fresh_engine)
    try:
        embeddings = [
            (embedding.note_path, embedding.embedding_blob, embedding.model_name)
            for embedding in fresh_session.scalars(
                select(NoteEmbedding).order_by(NoteEmbedding.note_path)
            ).all()
        ]
        assert embeddings == original_embeddings
    finally:
        fresh_session.close()
        fresh_engine.dispose()
