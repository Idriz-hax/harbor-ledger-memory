"""Deterministic parsing of Obsidian wikilink syntax."""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from harbor_ledger_memory.domain.models import ParseDiagnostic, Wikilink

_WIKILINK_RE = re.compile(r"\[\[([^\[\]\r\n]+?)\]\]")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_RE = re.compile(r"^(`{3,}|~{3,})[ \t]*$")
_BLOCKQUOTE_PREFIX_RE = re.compile(r"^[ \t]*>[ \t]?")
_LIST_PREFIX_RE = re.compile(r"^[ \t]*(?:[*+-]|\d+[.)])[ \t]+")
_MAX_FENCE_INDENT = 3


@dataclass(frozen=True)
class _FenceState:
    character: str
    length: int
    containers: tuple[str, ...]
    opener_indent: int
    container_prefix_length: int
    container_leading_indent: int


def parse_wikilinks(text: str) -> list[Wikilink]:
    """Extract complete wikilinks without resolving their targets.

    The parser deliberately leaves relative paths and aliases untouched. A
    fragment beginning with ``^`` is a block ID; other fragments are headings.
    Incomplete bracket pairs are ignored rather than interpreted heuristically.
    """

    links, _ = parse_wikilinks_with_diagnostics(text)
    return links


def parse_wikilinks_with_diagnostics(
    text: str, start_line: int = 1
) -> tuple[list[Wikilink], list[ParseDiagnostic]]:
    """Extract wikilinks and diagnose empty aliases or fragments.

    ``start_line`` is the one-based source line of ``text``. Fenced code is
    skipped so examples in Markdown code blocks are not treated as links.
    """

    links: list[Wikilink] = []
    diagnostics: list[ParseDiagnostic] = []
    for line_number, line in iter_non_fenced_lines(text, start_line):
        for match in _WIKILINK_RE.finditer(line):
            raw = match.group(0)
            link_text = match.group(1)
            target_and_fragment, separator, alias = link_text.partition("|")
            if not separator:
                alias = None
            elif not alias:
                diagnostics.append(
                    ParseDiagnostic(
                        code="wikilink.invalid",
                        message="wikilink alias is empty",
                        line=line_number,
                    )
                )

            target, fragment_separator, fragment = target_and_fragment.partition("#")
            heading: str | None = None
            block_id: str | None = None
            if fragment_separator:
                if fragment.startswith("^"):
                    block_id = fragment[1:]
                    if not block_id:
                        diagnostics.append(
                            ParseDiagnostic(
                                code="wikilink.invalid",
                                message="wikilink block ID fragment is empty",
                                line=line_number,
                            )
                        )
                else:
                    heading = fragment
                    if not heading:
                        diagnostics.append(
                            ParseDiagnostic(
                                code="wikilink.invalid",
                                message="wikilink heading fragment is empty",
                                line=line_number,
                            )
                        )

            links.append(
                Wikilink(
                    raw=raw,
                    target=target,
                    alias=alias,
                    heading=heading,
                    block_id=block_id,
                )
            )
    return links, diagnostics


def iter_non_fenced_lines(text: str, start_line: int = 1) -> Iterator[tuple[int, str]]:
    fence_state: _FenceState | None = None
    for offset, raw_line in enumerate(text.splitlines(), start=0):
        line = raw_line
        (
            fence_line,
            containers,
            prefix_length,
            container_leading_indent,
            continuation_indent,
        ) = _fence_line_context(line)
        if fence_state is not None:
            closing_match = _FENCE_CLOSE_RE.match(fence_line)
            if (
                closing_match is not None
                and closing_match.group(1)[0] == fence_state.character
                and len(closing_match.group(1)) >= fence_state.length
                and _is_valid_closer(
                    containers,
                    prefix_length,
                    container_leading_indent,
                    continuation_indent,
                    fence_state,
                )
            ):
                fence_state = None
            continue
        fence_match = _FENCE_RE.match(fence_line)
        if fence_match is not None:
            fence_state = _FenceState(
                character=fence_match.group(1)[0],
                length=len(fence_match.group(1)),
                containers=containers,
                opener_indent=continuation_indent,
                container_prefix_length=prefix_length,
                container_leading_indent=container_leading_indent,
            )
            continue
        yield start_line + offset, line


def _fence_line_context(
    line: str,
) -> tuple[str, tuple[str, ...], int, int, int]:
    """Normalize a possible fence and retain its Markdown containers."""

    remainder = line
    containers: list[str] = []
    prefix_length = 0
    leading_indent = 0
    while True:
        whitespace_length = len(remainder) - len(remainder.lstrip(" \t"))
        blockquote_match = _BLOCKQUOTE_PREFIX_RE.match(remainder)
        if blockquote_match is not None:
            containers.append("blockquote")
            prefix_length += blockquote_match.end()
            if len(containers) == 1:
                leading_indent = whitespace_length
            remainder = remainder[blockquote_match.end() :]
            continue
        list_match = _LIST_PREFIX_RE.match(remainder)
        if list_match is not None:
            containers.append("list")
            prefix_length += list_match.end()
            if len(containers) == 1:
                leading_indent = whitespace_length
            remainder = remainder[list_match.end() :]
            continue
        continuation_indent = len(remainder) - len(remainder.lstrip(" \t"))
        return (
            remainder.lstrip(" \t"),
            tuple(containers),
            prefix_length,
            leading_indent,
            continuation_indent,
        )


def _is_valid_closer(
    containers: tuple[str, ...],
    prefix_length: int,
    leading_indent: int,
    continuation_indent: int,
    state: _FenceState,
) -> bool:
    if (
        containers == state.containers
        and leading_indent == state.container_leading_indent
    ):
        return continuation_indent <= max(_MAX_FENCE_INDENT, state.opener_indent)
    if "list" not in state.containers:
        return False

    expected_containers = _without_one_list(state.containers)
    closer_indent = prefix_length + continuation_indent
    return containers == expected_containers and (
        leading_indent == state.container_leading_indent
        and state.container_prefix_length
        <= closer_indent
        <= state.opener_indent + (2 * state.container_prefix_length) + _MAX_FENCE_INDENT
    )


def _without_one_list(containers: tuple[str, ...]) -> tuple[str, ...]:
    """Return a mixed container stack with its list continuation omitted."""

    omitted = False
    remaining: list[str] = []
    for container in containers:
        if container == "list" and not omitted:
            omitted = True
            continue
        remaining.append(container)
    return tuple(remaining)
