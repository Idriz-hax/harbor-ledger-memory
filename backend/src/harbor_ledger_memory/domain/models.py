"""Immutable models produced by the Markdown parser."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FrozenDict(Mapping[Any, Any]):
    """A recursively-freezable mapping used for unknown metadata."""

    __slots__ = ("_data",)
    _data: Mapping[Any, Any]

    def __init__(self, values: Mapping[Any, Any] | None = None) -> None:
        source = values or {}
        object.__setattr__(
            self,
            "_data",
            MappingProxyType(
                {key: _freeze_value(value) for key, value in source.items()}
            ),
        )

    def __getitem__(self, key: Any) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return repr(dict(self._data))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")


def _freeze_value(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        mapping = cast(Mapping[Any, Any], value)
        return FrozenDict({key: _freeze_value(item) for key, item in mapping.items()})
    if isinstance(value, (list, tuple)):
        sequence = cast(list[Any] | tuple[Any, ...], value)
        return tuple(_freeze_value(item) for item in sequence)
    if isinstance(value, (set, frozenset)):
        members = cast(set[Any] | frozenset[Any], value)
        return frozenset(_freeze_value(item) for item in members)
    return value


class _ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)


class Wikilink(_ImmutableModel):
    """A wikilink with its identity and optional display fragments intact."""

    raw: str
    target: str
    alias: str | None = None
    heading: str | None = None
    block_id: str | None = None


class Frontmatter(_ImmutableModel):
    """The supported frontmatter fields and unrecognised source metadata."""

    type: str | None = None
    status: str | None = None
    tags: tuple[str, ...] = Field(default_factory=tuple)
    created: str | None = None
    updated: str | None = None
    summary: str | None = None
    parent: str | None = None
    graph_color: str | None = None
    extra: FrozenDict = Field(default_factory=FrozenDict)

    @field_validator("extra", mode="before")
    @classmethod
    def _freeze_extra(cls, value: Any) -> FrozenDict:
        if isinstance(value, FrozenDict):
            return value
        if isinstance(value, Mapping):
            frozen = _freeze_value(value)
            if isinstance(frozen, FrozenDict):
                return frozen
        return FrozenDict()


class Heading(_ImmutableModel):
    """An ATX Markdown heading and its one-based source line."""

    level: int
    text: str
    line: int


class ParseDiagnostic(_ImmutableModel):
    """A typed, non-fatal parser diagnostic."""

    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "error"
    line: int | None = None


class ParsedNote(_ImmutableModel):
    """A parsed note identified by its vault-relative POSIX path."""

    path: PurePosixPath
    content: str
    frontmatter: Frontmatter = Field(default_factory=Frontmatter)
    headings: tuple[Heading, ...] = Field(default_factory=tuple)
    wikilinks: tuple[Wikilink, ...] = Field(default_factory=tuple)
    diagnostics: tuple[ParseDiagnostic, ...] = Field(default_factory=tuple)

    @property
    def body(self) -> str:
        """Return the Markdown body, excluding a leading frontmatter block."""
        return self.content
