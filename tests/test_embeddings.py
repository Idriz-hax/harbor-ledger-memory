"""Tests for the local sentence-transformer embedding service."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Generator
from contextlib import contextmanager

import pytest


class _FakeEncodedVector:
    """Small stand-in for the NumPy arrays returned by a model."""

    def __init__(self, values: list[float]) -> None:
        self._values = values

    def flatten(self) -> list[float]:
        return self._values


class _FakeSentenceTransformer:
    """Deterministic model double that never loads a local or remote model."""

    def encode(
        self,
        texts: str | list[str],
        *,
        convert_to_numpy: bool,
    ) -> _FakeEncodedVector | list[_FakeEncodedVector]:
        del convert_to_numpy
        if isinstance(texts, str):
            return self._vector_for(texts)
        return [self._vector_for(text) for text in texts]

    @staticmethod
    def _vector_for(text: str) -> _FakeEncodedVector:
        first_value = float(sum(map(ord, text)))
        return _FakeEncodedVector([first_value] + [0.0] * 383)


def _fake_load_model(_service: object) -> _FakeSentenceTransformer:
    return _FakeSentenceTransformer()


@pytest.fixture
def fake_sentence_transformer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch EmbeddingService so encoding tests remain model-independent."""
    from harbor_ledger_memory.services import embeddings as embeddings_module

    monkeypatch.setattr(
        embeddings_module.EmbeddingService,
        "_load_model",
        _fake_load_model,
    )


class TestEmbeddableText:
    """Tests for embeddable_text helper."""

    def test_concatenates_title_summary_content(self) -> None:
        from harbor_ledger_memory.services.embeddings import embeddable_text

        text = embeddable_text(
            title="Caching Guide",
            summary="How to implement caching",
            content="Long content " * 100,
        )
        assert text is not None
        assert "Caching Guide" in text
        assert "How to implement caching" in text
        assert len(text) <= 2200  # title + summary + 2000 chars max

    def test_returns_none_for_short_text(self) -> None:
        from harbor_ledger_memory.services.embeddings import embeddable_text

        text = embeddable_text(
            title="Hi",
            summary=None,
            content=None,
        )
        assert text is None

    def test_truncates_long_content(self) -> None:
        from harbor_ledger_memory.services.embeddings import embeddable_text

        text = embeddable_text(
            title="Title",
            summary=None,
            content="x" * 5000,
        )
        assert text is not None
        # "Title " (6 chars) + first 2000 x's = 2006 chars
        assert len(text) == 2006

    def test_handles_none_summary_and_content(self) -> None:
        from harbor_ledger_memory.services.embeddings import embeddable_text

        text = embeddable_text(
            title="A sufficiently long title on its own",
            summary=None,
            content=None,
        )
        assert text == "A sufficiently long title on its own"

    def test_handles_empty_summary_and_content(self) -> None:
        from harbor_ledger_memory.services.embeddings import embeddable_text

        text = embeddable_text(
            title="A sufficiently long title on its own",
            summary="",
            content="",
        )
        assert text == "A sufficiently long title on its own"


class TestCosineSimilarity:
    """Tests for cosine_similarity function."""

    def test_identical_vectors(self) -> None:
        from harbor_ledger_memory.services.embeddings import cosine_similarity

        vec: tuple[float, ...] = tuple([0.1] * 384)
        assert cosine_similarity(vec, vec) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        from harbor_ledger_memory.services.embeddings import cosine_similarity

        vec_a: tuple[float, ...] = tuple([1.0] + [0.0] * 383)
        vec_b: tuple[float, ...] = tuple([0.0] * 1 + [1.0] + [0.0] * 382)
        assert cosine_similarity(vec_a, vec_b) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        from harbor_ledger_memory.services.embeddings import cosine_similarity

        vec_a: tuple[float, ...] = (1.0, 0.0)
        vec_b: tuple[float, ...] = (-1.0, 0.0)
        assert cosine_similarity(vec_a, vec_b) == pytest.approx(-1.0)

    def test_zero_vector(self) -> None:
        from harbor_ledger_memory.services.embeddings import cosine_similarity

        vec_a: tuple[float, ...] = (0.0, 0.0)
        vec_b: tuple[float, ...] = (1.0, 2.0)
        assert cosine_similarity(vec_a, vec_b) == 0.0


class TestEmbeddingServiceBlob:
    """Tests for EmbeddingService serialization (no model load needed)."""

    def test_to_blob_and_from_blob_round_trip(self) -> None:
        from harbor_ledger_memory.services.embeddings import EmbeddingService

        service = EmbeddingService("all-MiniLM-L6-v2")
        vector: tuple[float, ...] = tuple([0.1 * (i % 10) for i in range(384)])
        blob = service.to_blob(vector)
        restored = service.from_blob(blob)
        # float32 round-trip introduces tiny precision loss
        for orig, got in zip(vector, restored):
            assert got == pytest.approx(orig, rel=1e-5)

    def test_blob_length_matches_vector_dimension(self) -> None:
        from harbor_ledger_memory.services.embeddings import EmbeddingService

        service = EmbeddingService("all-MiniLM-L6-v2")
        vector: tuple[float, ...] = tuple([0.5] * 384)
        blob = service.to_blob(vector)
        assert len(blob) == 384 * 4  # 4 bytes per float32


def test_concurrent_first_use_constructs_model_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from harbor_ledger_memory.services import embeddings as module

    monkeypatch.setattr(module, "_MODEL_CACHE", {})
    monkeypatch.setattr(module, "_MODEL_AVAILABLE", {})
    calls = 0
    lock = threading.Lock()

    class Runtime:
        def __init__(self, _name: str) -> None:
            nonlocal calls
            with lock:
                calls += 1

    monkeypatch.setattr(module, "import_module", lambda _name: type("ST", (), {"SentenceTransformer": Runtime}))
    with ThreadPoolExecutor(max_workers=8) as pool:
        models = list(pool.map(lambda _: module.EmbeddingService("single-flight")._load_model(), range(16)))
    assert calls == 1
    assert all(model is models[0] for model in models)


def test_effective_embedding_model_probe_is_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harbor_ledger_memory.services import embeddings as module

    monkeypatch.setattr(module, "_MODEL_CACHE", {})
    monkeypatch.setattr(module, "_MODEL_AVAILABLE", {})
    monkeypatch.setattr(module, "embedding_runtime_available", lambda: True)
    rendezvous = threading.Barrier(2)
    calls = 0
    calls_lock = threading.Lock()

    def load(_service: object) -> _FakeSentenceTransformer:
        nonlocal calls
        with calls_lock:
            calls += 1
        class Model:
            def encode(self, *_args: object, **_kwargs: object) -> _FakeEncodedVector:
                return _FakeEncodedVector([1.0] * 384)

        return Model()

    monkeypatch.setattr(module.EmbeddingService, "_load_model", load)
    with ThreadPoolExecutor(max_workers=2) as pool:
        def probe(_index: int) -> str | None:
            rendezvous.wait(timeout=2)
            return module.effective_embedding_model("single-flight-probe")

        results = list(pool.map(probe, range(2)))
    assert results == ["single-flight-probe", "single-flight-probe"]
    assert calls == 1


def test_concurrent_encode_calls_are_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    from harbor_ledger_memory.services import embeddings as module

    active = 0
    maximum_active = 0
    guard = threading.Lock()
    entered = threading.Event()

    class ConcurrentCheckModel(_FakeSentenceTransformer):
        def encode(self, texts, *, convert_to_numpy):  # type: ignore[no-untyped-def]
            nonlocal active, maximum_active
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
            entered.set()
            threading.Event().wait(0.02)
            with guard:
                active -= 1
            return super().encode(texts, convert_to_numpy=convert_to_numpy)

    monkeypatch.setattr(module.EmbeddingService, "_load_model", lambda _self: ConcurrentCheckModel())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda text: module.EmbeddingService("serialize").encode(text), ["one", "two"]))
    assert len(results) == 2
    assert maximum_active == 1


class TestEmbeddingServiceEncode:
    """Tests for EmbeddingService encoding using a deterministic fake model."""

    @pytest.mark.parametrize(
        "model_name",
        ["intfloat/e5-small-v2", "intfloat/multilingual-e5-small"],
    )
    def test_e5_prefixes_documents_and_queries(
        self, monkeypatch: pytest.MonkeyPatch, model_name: str
    ) -> None:
        from harbor_ledger_memory.services.embeddings import EmbeddingService

        received: list[str | list[str]] = []

        class RecordingModel(_FakeSentenceTransformer):
            def encode(self, texts, *, convert_to_numpy):  # type: ignore[no-untyped-def]
                received.append(texts)
                return super().encode(texts, convert_to_numpy=convert_to_numpy)

        monkeypatch.setattr(
            EmbeddingService, "_load_model", lambda _self: RecordingModel()
        )
        service = EmbeddingService(model_name)
        service.encode_batch(["first note", "second note"])
        service.encode("a search query")

        assert received == [
            ["passage: first note", "passage: second note"],
            "query: a search query",
        ]

    @pytest.mark.parametrize("model_name", ["all-MiniLM-L6-v2", "custom/e5-like-model"])
    def test_non_e5_text_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, model_name: str
    ) -> None:
        from harbor_ledger_memory.services.embeddings import EmbeddingService

        received: list[str | list[str]] = []

        class RecordingModel(_FakeSentenceTransformer):
            def encode(self, texts, *, convert_to_numpy):  # type: ignore[no-untyped-def]
                received.append(texts)
                return super().encode(texts, convert_to_numpy=convert_to_numpy)

        monkeypatch.setattr(
            EmbeddingService, "_load_model", lambda _self: RecordingModel()
        )
        service = EmbeddingService(model_name)
        service.encode_batch(["first note"])
        service.encode("a search query")

        assert received == [["first note"], "a search query"]

    def test_encode_returns_384_dimension_vector(
        self, fake_sentence_transformer: None
    ) -> None:
        from harbor_ledger_memory.services.embeddings import EmbeddingService

        service = EmbeddingService("all-MiniLM-L6-v2")
        vector = service.encode("caching is important for performance")
        assert len(vector) == 384
        assert all(isinstance(v, float) for v in vector)

    def test_encode_same_text_gives_same_vector(
        self, fake_sentence_transformer: None
    ) -> None:
        from harbor_ledger_memory.services.embeddings import EmbeddingService

        service = EmbeddingService("all-MiniLM-L6-v2")
        vector_a = service.encode("hello world")
        vector_b = service.encode("hello world")
        assert vector_a == vector_b

    def test_encode_batch_matches_individual(
        self, fake_sentence_transformer: None
    ) -> None:
        from harbor_ledger_memory.services.embeddings import EmbeddingService

        service = EmbeddingService("all-MiniLM-L6-v2")
        texts = ["hello", "world"]
        batch_vectors = service.encode_batch(texts)
        assert len(batch_vectors) == 2
        for bv, text in zip(batch_vectors, texts):
            iv = service.encode(text)
            # Batch and individual encoding can differ slightly due to
            # numerical precision in the model; compare with tolerance.
            for b_val, i_val in zip(bv, iv):
                assert b_val == pytest.approx(i_val, rel=1e-4)


@contextmanager
def _captured_embedding_logs(
    level: int = logging.WARNING,
) -> Generator[list[logging.LogRecord], None, None]:
    """Yield the records the embeddings logger emits during the block.

    ``caplog`` alone is unreliable here: earlier tests and third-party imports
    can leave ``logger.disabled`` set (or otherwise mutate global logging
    state), which makes ``Logger.handle`` drop every record before any handler
    sees it. This attaches a dedicated handler to the embeddings logger and
    re-enables it for the duration of the block, then restores the prior state.
    """
    logger = logging.getLogger("harbor_ledger_memory.services.embeddings")
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=level)
    previous_disabled = logger.disabled
    previous_level = logger.level
    logger.addHandler(handler)
    logger.disabled = False
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.disabled = previous_disabled
        logger.setLevel(previous_level)


class TestEffectiveEmbeddingModel:
    """Tests for graceful degradation when the optional runtime is missing."""

    @pytest.fixture(autouse=True)
    def _reset_warning_flag(self) -> None:
        from harbor_ledger_memory.services import embeddings as embeddings_module

        embeddings_module.reset_runtime_warning_flag()

    def test_none_model_returns_none_without_warning(self) -> None:
        from harbor_ledger_memory.services import embeddings as embeddings_module

        with _captured_embedding_logs() as records:
            result = embeddings_module.effective_embedding_model(None)

        assert result is None
        assert [r for r in records if r.levelno == logging.WARNING] == []

    def test_runtime_available_returns_model_without_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harbor_ledger_memory.services import embeddings as embeddings_module

        monkeypatch.setattr(
            embeddings_module, "embedding_runtime_available", lambda: True
        )
        monkeypatch.setattr(embeddings_module.EmbeddingService, "_load_model", _fake_load_model)
        with _captured_embedding_logs() as records:
            result = embeddings_module.effective_embedding_model("m")

        assert result == "m"
        assert [r for r in records if r.levelno == logging.WARNING] == []

    @pytest.mark.parametrize("failure_stage", ["load", "encode"])
    def test_broken_model_is_disabled_once(self, monkeypatch: pytest.MonkeyPatch, failure_stage: str) -> None:
        from harbor_ledger_memory.services import embeddings as embeddings_module

        monkeypatch.setattr(embeddings_module, "embedding_runtime_available", lambda: True)
        attempts = []

        def load(_service):
            attempts.append(1)
            if failure_stage == "load":
                raise RuntimeError("model unavailable")

            class BrokenModel:
                def encode(self, *_args, **_kwargs):
                    raise RuntimeError("encoder unavailable")

            return BrokenModel()

        monkeypatch.setattr(embeddings_module.EmbeddingService, "_load_model", load)
        with _captured_embedding_logs() as records:
            assert embeddings_module.effective_embedding_model(f"broken-{failure_stage}") is None
            assert embeddings_module.effective_embedding_model(f"broken-{failure_stage}") is None
        assert len(attempts) == 1
        assert len([r for r in records if r.levelno == logging.WARNING]) == 1

    def test_runtime_missing_returns_none_and_warns_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harbor_ledger_memory.services import embeddings as embeddings_module

        monkeypatch.setattr(
            embeddings_module, "embedding_runtime_available", lambda: False
        )
        with _captured_embedding_logs() as records:
            first = embeddings_module.effective_embedding_model("m")
            second = embeddings_module.effective_embedding_model("m")

        assert first is None
        assert second is None
        warnings = [r for r in records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "sentence-transformers" in warnings[0].getMessage()

    def test_reset_flag_allows_warning_to_fire_again(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harbor_ledger_memory.services import embeddings as embeddings_module

        monkeypatch.setattr(
            embeddings_module, "embedding_runtime_available", lambda: False
        )
        with _captured_embedding_logs() as records:
            embeddings_module.effective_embedding_model("m")
            embeddings_module.reset_runtime_warning_flag()
            embeddings_module.effective_embedding_model("m")

        warnings = [r for r in records if r.levelno == logging.WARNING]
        assert len(warnings) == 2

    def test_embedding_runtime_available_returns_bool(self) -> None:
        from harbor_ledger_memory.services import embeddings as embeddings_module

        assert isinstance(embeddings_module.embedding_runtime_available(), bool)
