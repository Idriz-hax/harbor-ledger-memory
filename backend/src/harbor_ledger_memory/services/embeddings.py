"""Local sentence-transformer embedding service for semantic retrieval."""

from __future__ import annotations

import logging
import math
import struct
from importlib import import_module
from typing import Any, cast

logger = logging.getLogger(__name__)

_MODEL_CACHE: dict[str, Any] = {}
_EMBEDDABLE_MAX = 2000
_MIN_TEXT_LENGTH = 10


def embeddable_text(
    title: str,
    summary: str | None,
    content: str | None,
) -> str | None:
    """Concatenate title, summary, and first 2000 chars of content.

    Returns None if the resulting text is shorter than the minimum length.
    """
    parts = [title]
    if summary:
        parts.append(summary)
    if content:
        parts.append(content[:_EMBEDDABLE_MAX])
    text = " ".join(parts).strip()
    if len(text) < _MIN_TEXT_LENGTH:
        return None
    return text


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def embedding_runtime_available() -> bool:
    """Return True if the optional ``sentence-transformers`` runtime is importable."""
    from importlib.util import find_spec

    return find_spec("sentence_transformers") is not None


_runtime_warning_emitted = False


def effective_embedding_model(model_name: str | None) -> str | None:
    """Return the embedding model to use, or ``None`` when embeddings are off.

    When a model is configured but the optional ``sentence-transformers`` runtime
    is missing, degrade to ``None`` (embeddings disabled) and emit a one-time,
    actionable warning instead of raising. This keeps the server bootable and
    queries functional without the semantic signal.
    """
    global _runtime_warning_emitted
    if not model_name:
        return None
    if embedding_runtime_available():
        return model_name
    if not _runtime_warning_emitted:
        _runtime_warning_emitted = True
        logger.warning(
            "Embedding model %r is configured but the optional "
            "'sentence-transformers' runtime is not installed; embeddings are "
            "disabled for this process. To enable semantic search install the "
            "extra, e.g.: uv tool install --with sentence-transformers <wheel> "
            "or: uv pip install -e '.[embeddings]'.",
            model_name,
        )
    return None


def reset_runtime_warning_flag() -> None:
    """Test helper: allow the one-time warning to fire again."""
    global _runtime_warning_emitted
    _runtime_warning_emitted = False


class EmbeddingService:
    """Encode text into embedding vectors using a local model."""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name

    def _load_model(self) -> Any:
        if self._model_name not in _MODEL_CACHE:
            sentence_transformers = cast(Any, import_module("sentence_transformers"))
            _MODEL_CACHE[self._model_name] = sentence_transformers.SentenceTransformer(
                self._model_name
            )
        return _MODEL_CACHE[self._model_name]

    def encode(self, text: str) -> tuple[float, ...]:
        """Encode a text string into a 384-dimensional vector."""
        model = self._load_model()
        vector = model.encode(text, convert_to_numpy=True)
        return tuple(float(v) for v in vector.flatten())

    def encode_batch(self, texts: list[str]) -> list[tuple[float, ...]]:
        """Encode multiple texts efficiently."""
        model = self._load_model()
        vectors = model.encode(texts, convert_to_numpy=True)
        return [tuple(float(v) for v in vec.flatten()) for vec in vectors]

    def to_blob(self, vector: tuple[float, ...]) -> bytes:
        """Serialize a vector to a SQLite BLOB."""
        return struct.pack(f"<{len(vector)}f", *vector)

    def from_blob(self, blob: bytes) -> tuple[float, ...]:
        """Deserialize a BLOB back to a vector."""
        count = len(blob) // 4
        return struct.unpack(f"<{count}f", blob)


__all__ = [
    "EmbeddingService",
    "cosine_similarity",
    "embeddable_text",
    "effective_embedding_model",
    "embedding_runtime_available",
]
