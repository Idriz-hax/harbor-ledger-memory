"""Parse the supported, read-only Markdown/Obsidian subset."""

import re
from collections.abc import Callable, Hashable, Iterable, Mapping
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

import yaml

from harbor_ledger_memory.domain.models import (
    Frontmatter,
    FrozenDict,
    Heading,
    ParseDiagnostic,
    ParsedNote,
)
from harbor_ledger_memory.vault.links import (
    iter_non_fenced_lines,
    parse_wikilinks_with_diagnostics,
)

_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})(?:[ \t]+|$)(.*?)\s*$")
_FRONTMATTER_DELIMITER = re.compile(r"^---[ \t]*(?:\r?\n)?$")
_STANDARD_FIELDS = frozenset(
    {
        "type",
        "status",
        "tags",
        "created",
        "updated",
        "summary",
        "parent",
        "graph_color",
    }
)
_STANDARD_FIELD_ORDER = (
    "type",
    "status",
    "tags",
    "created",
    "updated",
    "summary",
    "parent",
    "graph_color",
)


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader variant that retains the first value for duplicate keys."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.duplicate_keys: list[tuple[str, int]] = []
        self.field_lines: dict[str, int] = {}
        self._mapping_depth = 0

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Hashable, Any]:
        if not isinstance(node, yaml.MappingNode):
            return super().construct_mapping(node, deep=deep)

        self.flatten_mapping(node)
        mapping: dict[Hashable, Any] = {}
        construct_object = cast(
            Callable[[Any, bool], Any], getattr(self, "construct_object")
        )
        self._mapping_depth += 1
        try:
            node_values = cast(list[tuple[Any, Any]], node.value)
            for key_node, value_node in node_values:
                key = cast(Hashable, construct_object(key_node, deep))
                key_line = cast(int, key_node.start_mark.line) + 2
                if self._mapping_depth == 1 and isinstance(key, str):
                    self.field_lines.setdefault(key, key_line)
                if key in mapping:
                    if isinstance(key, str):
                        self.duplicate_keys.append((key, key_line))
                    construct_object(value_node, deep)
                    continue
                value = construct_object(value_node, deep)
                mapping[key] = value
        finally:
            self._mapping_depth -= 1
        return mapping


def parse_note(path: Path, vault_relative_path: PurePosixPath) -> ParsedNote:
    """Read and parse an admitted Markdown path.

    ``vault_relative_path`` is the only path identity retained in the parsed
    result. The caller is responsible for admitting ``path`` through the
    ``VaultBoundary`` before invoking this function.
    """

    diagnostics: list[ParseDiagnostic] = []
    try:
        raw = path.read_bytes()
    except (OSError, UnicodeError) as exc:
        diagnostics.append(
            ParseDiagnostic(code="file.read", message=f"could not read note: {exc}")
        )
        return ParsedNote(
            path=vault_relative_path,
            content="",
            diagnostics=tuple(diagnostics),
        )

    return parse_note_bytes(raw, vault_relative_path)


def parse_note_bytes(raw: bytes, vault_relative_path: PurePosixPath) -> ParsedNote:
    """Parse one immutable UTF-8 byte snapshot without reading the filesystem."""
    diagnostics: list[ParseDiagnostic] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        return ParsedNote(
            path=vault_relative_path,
            content="",
            diagnostics=(
                ParseDiagnostic(
                    code="file.read", message=f"could not read note: {exc}"
                ),
            ),
        )

    frontmatter, content, frontmatter_diagnostics, body_parseable = _parse_frontmatter(
        text
    )
    diagnostics.extend(frontmatter_diagnostics)
    if body_parseable:
        body_start_line = _body_start_line(text, content)
        headings = _parse_headings(content, body_start_line)
        wikilinks, wikilink_diagnostics = parse_wikilinks_with_diagnostics(
            content, body_start_line
        )
        diagnostics.extend(wikilink_diagnostics)
    else:
        headings = []
        wikilinks = []
    return ParsedNote(
        path=vault_relative_path,
        content=content,
        frontmatter=frontmatter,
        headings=tuple(headings),
        wikilinks=tuple(wikilinks),
        diagnostics=tuple(diagnostics),
    )


def _parse_frontmatter(
    text: str,
) -> tuple[Frontmatter, str, list[ParseDiagnostic], bool]:
    lines = text.splitlines(keepends=True)
    if not lines or not _FRONTMATTER_DELIMITER.match(lines[0]):
        return Frontmatter(), text, [], True

    closing_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if _FRONTMATTER_DELIMITER.match(line)
        ),
        None,
    )
    if closing_index is None:
        return (
            Frontmatter(),
            text,
            [
                ParseDiagnostic(
                    code="frontmatter.invalid",
                    message="frontmatter opening delimiter has no closing delimiter",
                    line=1,
                )
            ],
            False,
        )

    raw_frontmatter = "".join(lines[1:closing_index])
    content = "".join(lines[closing_index + 1 :])
    loader = _UniqueKeyLoader(raw_frontmatter)
    try:
        loaded = loader.get_single_data()
    except yaml.YAMLError as exc:
        return (
            Frontmatter(),
            content,
            [
                ParseDiagnostic(
                    code="frontmatter.invalid",
                    message=f"frontmatter is not valid YAML: {exc}",
                    line=_yaml_error_line(exc),
                )
            ],
            True,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        return (
            Frontmatter(),
            content,
            [
                ParseDiagnostic(
                    code="frontmatter.invalid",
                    message=(
                        "frontmatter contains an unsupported or recursive value: "
                        f"{type(exc).__name__}"
                    ),
                    line=_first_frontmatter_line(raw_frontmatter),
                )
            ],
            True,
        )
    finally:
        duplicate_keys = list(loader.duplicate_keys)
        field_lines = dict(loader.field_lines)
        dispose = cast(Callable[[], None], getattr(loader, "dispose"))
        dispose()

    diagnostics = [
        ParseDiagnostic(
            code="frontmatter.duplicate",
            message=f"duplicate YAML key {key!r}; first value retained",
            line=line,
        )
        for key, line in duplicate_keys
    ]

    if loaded is None:
        return Frontmatter(), content, diagnostics, True
    if not isinstance(loaded, Mapping):
        diagnostics.append(
            ParseDiagnostic(
                code="frontmatter.invalid",
                message="frontmatter must be a mapping",
                line=_first_frontmatter_line(raw_frontmatter),
            )
        )
        return Frontmatter(), content, diagnostics, True

    loaded_mapping = cast(Mapping[Any, Any], loaded)
    if _contains_recursive_value(loaded_mapping):
        diagnostics.append(
            ParseDiagnostic(
                code="frontmatter.invalid",
                message="frontmatter contains a recursive YAML alias",
                line=_first_frontmatter_line(raw_frontmatter),
            )
        )
        return Frontmatter(), content, diagnostics, True

    values: dict[str, Any] = {}
    for key, value in loaded_mapping.items():
        if isinstance(key, str):
            values[key] = value
        else:
            diagnostics.append(
                ParseDiagnostic(
                    code="frontmatter.invalid",
                    message="frontmatter keys must be strings",
                    line=_first_frontmatter_line(raw_frontmatter),
                )
            )

    tags_value: Any = values.get("tags", [])
    if tags_value is None:
        tags: tuple[str, ...] = ()
    elif isinstance(tags_value, list):
        tag_values = cast(list[Any], tags_value)
        if all(isinstance(tag, str) for tag in tag_values):
            tags = tuple(cast(list[str], tag_values))
        else:
            tags = ()
            diagnostics.append(
                ParseDiagnostic(
                    code="frontmatter.field.invalid",
                    message="frontmatter field 'tags' must be a list of strings",
                    line=field_lines.get(
                        "tags", _first_frontmatter_line(raw_frontmatter)
                    ),
                )
            )
    else:
        tags = ()
        diagnostics.append(
            ParseDiagnostic(
                code="frontmatter.field.invalid",
                message="frontmatter field 'tags' must be a list of strings",
                line=field_lines.get("tags", _first_frontmatter_line(raw_frontmatter)),
            )
        )

    standard: dict[str, str | None] = {}
    for field in _STANDARD_FIELD_ORDER:
        if field == "tags":
            continue
        if field not in values:
            continue
        try:
            standard[field] = _source_string(values[field])
        except (TypeError, ValueError) as exc:
            diagnostics.append(
                ParseDiagnostic(
                    code="frontmatter.field.invalid",
                    message=f"frontmatter field {field!r} is invalid: {exc}",
                    line=field_lines.get(
                        field, _first_frontmatter_line(raw_frontmatter)
                    ),
                )
            )

    try:
        frontmatter = Frontmatter(
            **standard,
            tags=tags,
            extra=FrozenDict(
                {
                    key: value
                    for key, value in values.items()
                    if key not in _STANDARD_FIELDS
                }
            ),
        )
    except (RecursionError, TypeError, ValueError) as exc:
        diagnostics.append(
            ParseDiagnostic(
                code="frontmatter.invalid",
                message=(
                    "frontmatter contains an unsupported or recursive value: "
                    f"{type(exc).__name__}"
                ),
                line=_first_frontmatter_line(raw_frontmatter),
            )
        )
        return Frontmatter(), content, diagnostics, True
    return frontmatter, content, diagnostics, True


def _yaml_error_line(exc: yaml.YAMLError) -> int:
    mark = getattr(exc, "problem_mark", None)
    if mark is not None and mark.line is not None:
        return mark.line + 2
    return 2


def _first_frontmatter_line(raw_frontmatter: str) -> int:
    return 2 if raw_frontmatter else 1


def _contains_recursive_value(value: Any, active: set[int] | None = None) -> bool:
    if active is None:
        active = set()
    if not isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return False

    container = cast(Any, value)
    marker = id(container)
    if marker in active:
        return True
    active.add(marker)
    try:
        if isinstance(container, Mapping):
            mapping = cast(Mapping[Any, Any], container)
            return any(
                _contains_recursive_value(key, active)
                or _contains_recursive_value(item, active)
                for key, item in mapping.items()
            )
        values = cast(Iterable[Any], container)
        return any(_contains_recursive_value(item, active) for item in values)
    finally:
        active.remove(marker)


def _source_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"expected a string, got {type(value).__name__}")


def _body_start_line(text: str, content: str) -> int:
    if content == text:
        return 1
    prefix_length = len(text) - len(content)
    return text[:prefix_length].count("\n") + 1


def _parse_headings(content: str, start_line: int) -> list[Heading]:
    headings: list[Heading] = []
    for line_number, line in iter_non_fenced_lines(content, start_line):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        heading_text = match.group(2).strip()
        heading_text = re.sub(r"[ \t]+#+[ \t]*$", "", heading_text).rstrip()
        headings.append(
            Heading(
                level=len(match.group(1)),
                text=heading_text,
                line=line_number,
            )
        )
    return headings
