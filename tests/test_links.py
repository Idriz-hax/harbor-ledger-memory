from harbor_ledger_memory.domain.models import Wikilink
from harbor_ledger_memory.vault.links import parse_wikilinks


def test_parse_wikilink_with_alias_and_heading() -> None:
    assert parse_wikilinks("[[AI/Knowledge/a#Part|Readable]]") == [
        Wikilink(
            raw="[[AI/Knowledge/a#Part|Readable]]",
            target="AI/Knowledge/a",
            alias="Readable",
            heading="Part",
            block_id=None,
        )
    ]


def test_parse_supported_wikilink_forms_without_resolving_targets() -> None:
    assert parse_wikilinks("[[file]] [[folder/file]] [[folder/file|Alias]]") == [
        Wikilink(raw="[[file]]", target="file"),
        Wikilink(raw="[[folder/file]]", target="folder/file"),
        Wikilink(
            raw="[[folder/file|Alias]]",
            target="folder/file",
            alias="Alias",
        ),
    ]
    assert parse_wikilinks("[[../INDEX]]") == [
        Wikilink(raw="[[../INDEX]]", target="../INDEX")
    ]


def test_parse_heading_and_block_fragments_separately() -> None:
    assert parse_wikilinks("[[note#Details]] [[note#^block-id]]") == [
        Wikilink(
            raw="[[note#Details]]",
            target="note",
            heading="Details",
        ),
        Wikilink(
            raw="[[note#^block-id]]",
            target="note",
            block_id="block-id",
        ),
    ]


def test_missing_closing_brackets_are_not_guessed_as_links() -> None:
    assert parse_wikilinks("before [[not-closed and [[also-not-closed]]") == [
        Wikilink(
            raw="[[also-not-closed]]",
            target="also-not-closed",
        )
    ]
