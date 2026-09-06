import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from harbor_ledger_memory.domain.models import Frontmatter, Heading
from harbor_ledger_memory.vault.parser import parse_note

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "vault"


def test_parse_fixture_note_frontmatter_headings_and_links() -> None:
    path = FIXTURE_ROOT / "AI" / "Knowledge" / "example.md"

    result = parse_note(path, PurePosixPath("AI/Knowledge/example.md"))

    assert result.path == PurePosixPath("AI/Knowledge/example.md")
    assert result.frontmatter.type == "knowledge"
    assert result.frontmatter.status == "active"
    assert result.frontmatter.tags == ("memory", "example")
    assert result.frontmatter.extra == {"custom_field": "preserved"}
    assert result.headings == (
        Heading(level=1, text="Example note", line=14),
        Heading(level=2, text="Details", line=16),
    )
    assert [link.target for link in result.wikilinks] == [
        "AI/INDEX",
        "../INDEX",
        "AI/INDEX",
    ]
    assert result.wikilinks[-1].block_id == "example-block"
    assert result.diagnostics == ()


def test_parse_note_preserves_missing_optional_frontmatter_defaults(
    tmp_path: Path,
) -> None:
    note_path = tmp_path / "minimal.md"
    note_path.write_text("---\ntype: context\n---\n# Minimal\n", encoding="utf-8")

    result = parse_note(note_path, PurePosixPath("AI/minimal.md"))

    assert result.frontmatter == Frontmatter(type="context")
    assert result.frontmatter.tags == ()
    assert result.frontmatter.status is None
    assert result.frontmatter.parent is None


def test_parser_reports_malformed_frontmatter(tmp_path: Path) -> None:
    note_path = tmp_path / "bad.md"
    note_path.write_text(
        "---\ntype: [broken\n---\n# Still readable\n", encoding="utf-8"
    )

    result = parse_note(note_path, PurePosixPath("AI/bad.md"))

    assert any(issue.code == "frontmatter.invalid" for issue in result.diagnostics)
    assert result.frontmatter == Frontmatter()
    assert result.headings == (Heading(level=1, text="Still readable", line=4),)


def test_parser_reports_unclosed_frontmatter_without_guessing(tmp_path: Path) -> None:
    note_path = tmp_path / "unclosed.md"
    note_path.write_text("---\ntype: knowledge\n# body\n", encoding="utf-8")

    result = parse_note(note_path, PurePosixPath("AI/unclosed.md"))

    assert any(issue.code == "frontmatter.invalid" for issue in result.diagnostics)
    assert result.frontmatter == Frontmatter()
    assert result.content == "---\ntype: knowledge\n# body\n"
    assert result.headings == ()
    assert result.wikilinks == ()


def test_parser_reports_duplicate_yaml_keys_without_overwriting(tmp_path: Path) -> None:
    note_path = tmp_path / "duplicate.md"
    note_path.write_text(
        "---\ntype: first\ntype: second\ncustom: preserved\n---\n# Body\n",
        encoding="utf-8",
    )

    result = parse_note(note_path, PurePosixPath("AI/duplicate.md"))

    assert result.frontmatter.type == "first"
    assert result.frontmatter.extra == {"custom": "preserved"}
    assert any(
        issue.code == "frontmatter.duplicate"
        and issue.line == 3
        and "type" in issue.message
        for issue in result.diagnostics
    )


def test_parser_preserves_valid_fields_and_extras_with_field_diagnostics(
    tmp_path: Path,
) -> None:
    note_path = tmp_path / "partially-invalid.md"
    note_path.write_text(
        "---\n"
        "type: knowledge\n"
        "status: [invalid]\n"
        "tags: invalid\n"
        "summary: preserved summary\n"
        "custom: preserved extra\n"
        "---\n"
        "# Body\n",
        encoding="utf-8",
    )

    result = parse_note(note_path, PurePosixPath("AI/partially-invalid.md"))

    assert result.frontmatter.type == "knowledge"
    assert result.frontmatter.summary == "preserved summary"
    assert result.frontmatter.extra == {"custom": "preserved extra"}
    assert result.frontmatter.tags == ()
    assert (
        len(
            [
                issue
                for issue in result.diagnostics
                if issue.code == "frontmatter.field.invalid"
            ]
        )
        == 2
    )
    assert all(issue.line in {3, 4} for issue in result.diagnostics)


def test_parser_ignores_headings_and_links_inside_fenced_code(tmp_path: Path) -> None:
    note_path = tmp_path / "fenced.md"
    note_path.write_text(
        "# Before\n"
        "```markdown\n"
        "# Not a heading\n"
        "[[not-a-link]]\n"
        "```\n"
        "## After\n"
        "[[real-link]]\n",
        encoding="utf-8",
    )

    result = parse_note(note_path, PurePosixPath("AI/fenced.md"))

    assert result.headings == (
        Heading(level=1, text="Before", line=1),
        Heading(level=2, text="After", line=6),
    )
    assert [link.target for link in result.wikilinks] == ["real-link"]


def test_parser_ignores_indented_and_list_nested_fences_and_resumes(
    tmp_path: Path,
) -> None:
    note_path = tmp_path / "nested-fences.md"
    note_path.write_text(
        "    ```markdown\n"
        "    # Hidden indented heading\n"
        "    [[hidden-indented-link]]\n"
        "    ```\n"
        "# Visible heading\n"
        "[[visible-link]]\n"
        "-   ```markdown\n"
        "    # Hidden list heading\n"
        "    [[hidden-list-link]]\n"
        "    ```\n"
        "## Visible after list\n"
        "[[visible-after-list]]\n",
        encoding="utf-8",
    )

    result = parse_note(note_path, PurePosixPath("AI/nested-fences.md"))

    assert result.headings == (
        Heading(level=1, text="Visible heading", line=5),
        Heading(level=2, text="Visible after list", line=11),
    )
    assert [link.target for link in result.wikilinks] == [
        "visible-link",
        "visible-after-list",
    ]


def test_parser_keeps_literal_blockquote_fence_inside_top_level_fence(
    tmp_path: Path,
) -> None:
    note_path = tmp_path / "literal-blockquote-fence.md"
    note_path.write_text(
        "```markdown\n"
        "> ```\n"
        "# Hidden heading\n"
        "[[hidden-link]]\n"
        "```\n"
        "# Visible heading\n"
        "[[visible-link]]\n",
        encoding="utf-8",
    )

    result = parse_note(note_path, PurePosixPath("AI/literal-blockquote-fence.md"))

    assert result.headings == (Heading(level=1, text="Visible heading", line=6),)
    assert [link.target for link in result.wikilinks] == ["visible-link"]


@pytest.mark.parametrize(
    ("opener", "literal_fence", "allowed_closer", "hidden_prefix"),
    [
        ("```markdown", "    ```", "```", ""),
        ("- ```markdown", "        ```", "    ```", "    "),
        ("> ```markdown", ">     ```", "> ```", "> "),
    ],
)
def test_parser_rejects_over_indented_literal_fences_in_each_container(
    tmp_path: Path,
    opener: str,
    literal_fence: str,
    allowed_closer: str,
    hidden_prefix: str,
) -> None:
    note_path = tmp_path / "over-indented-fences.md"
    note_path.write_text(
        f"{opener}\n"
        f"{literal_fence}\n"
        f"{hidden_prefix}# Hidden heading\n"
        f"{hidden_prefix}[[hidden-link]]\n"
        f"{allowed_closer}\n"
        "# Visible heading\n"
        "[[visible-link]]\n",
        encoding="utf-8",
    )

    result = parse_note(note_path, PurePosixPath("AI/over-indented-fences.md"))

    assert result.headings == (Heading(level=1, text="Visible heading", line=6),)
    assert [link.target for link in result.wikilinks] == ["visible-link"]


@pytest.mark.parametrize(
    ("opener", "continuation_indent", "closer"),
    [
        ("- ```markdown", "    ", "    ```"),
        ("> - ```markdown", "        ", ">        ```"),
    ],
)
def test_parser_closes_nested_list_blockquote_fences_after_deep_continuation(
    tmp_path: Path, opener: str, continuation_indent: str, closer: str
) -> None:
    note_path = tmp_path / "nested-list-blockquote-fence.md"
    note_path.write_text(
        f"{opener}\n"
        "> ```\n"
        f"{continuation_indent}# Hidden heading\n"
        f"{continuation_indent}[[hidden-link]]\n"
        f"{closer}\n"
        "# Visible heading\n"
        "[[visible-link]]\n",
        encoding="utf-8",
    )

    result = parse_note(note_path, PurePosixPath("AI/nested-list-blockquote-fence.md"))

    assert result.headings == (Heading(level=1, text="Visible heading", line=6),)
    assert [link.target for link in result.wikilinks] == ["visible-link"]


@pytest.mark.parametrize(
    "frontmatter",
    [
        "? [unhashable, key]\n: value\n",
        "recursive: &recursive\n  self: *recursive\n",
    ],
)
def test_parser_reports_invalid_frontmatter_without_raising(
    tmp_path: Path, frontmatter: str
) -> None:
    note_path = tmp_path / "unsafe-structure.md"
    note_path.write_text(
        f"---\n{frontmatter}type: knowledge\n---\n# Body\n", encoding="utf-8"
    )

    result = parse_note(note_path, PurePosixPath("AI/unsafe-structure.md"))

    assert any(issue.code == "frontmatter.invalid" for issue in result.diagnostics)


def test_parser_diagnostics_are_stable_across_hash_seeds(tmp_path: Path) -> None:
    note_path = tmp_path / "diagnostics.md"
    note_path.write_text(
        "---\n"
        "type: [invalid]\n"
        "status: [invalid]\n"
        "tags: invalid\n"
        "created: [invalid]\n"
        "updated: [invalid]\n"
        "summary: [invalid]\n"
        "parent: [invalid]\n"
        "graph_color: [invalid]\n"
        "---\n",
        encoding="utf-8",
    )
    script = """
import json
import sys
from pathlib import Path, PurePosixPath

from harbor_ledger_memory.vault.parser import parse_note

result = parse_note(Path(sys.argv[1]), PurePosixPath("AI/diagnostics.md"))
print(
    json.dumps(
        [(issue.code, issue.message, issue.line) for issue in result.diagnostics]
    )
)
"""
    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = str(Path(__file__).parents[1] / "backend" / "src")

    outputs = []
    for seed in ("1", "2", "3", "4", "5"):
        completed = subprocess.run(
            [sys.executable, "-c", script, str(note_path)],
            check=True,
            capture_output=True,
            env={**process_env, "PYTHONHASHSEED": seed},
            text=True,
        )
        outputs.append(json.loads(completed.stdout))

    assert all(output == outputs[0] for output in outputs)


def test_parser_models_are_container_immutable(tmp_path: Path) -> None:
    note_path = tmp_path / "immutable.md"
    note_path.write_text(
        "---\ntags:\n  - one\ncustom:\n  nested:\n    - value\n---\n# Body\n",
        encoding="utf-8",
    )

    result = parse_note(note_path, PurePosixPath("AI/immutable.md"))

    with pytest.raises(AttributeError):
        result.frontmatter.tags.append("two")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        result.frontmatter.extra["other"] = "value"
    with pytest.raises(TypeError):
        result.frontmatter.extra["custom"]["nested"] = ("changed",)
    with pytest.raises(AttributeError):
        result.headings.append(Heading(level=2, text="Nope", line=2))  # type: ignore[attr-defined]


def test_parser_diagnoses_empty_wikilink_fragments_with_source_lines(
    tmp_path: Path,
) -> None:
    note_path = tmp_path / "empty-fragments.md"
    note_path.write_text("[[note|]]\n[[note#]]\n[[note#^]]\n", encoding="utf-8")

    result = parse_note(note_path, PurePosixPath("AI/empty-fragments.md"))

    invalid_links = [
        issue for issue in result.diagnostics if issue.code == "wikilink.invalid"
    ]
    assert [issue.line for issue in invalid_links] == [1, 2, 3]
    assert [link.alias for link in result.wikilinks] == ["", None, None]
    assert [link.heading for link in result.wikilinks] == [None, "", None]
    assert [link.block_id for link in result.wikilinks] == [None, None, ""]
