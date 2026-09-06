import os
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import pytest

from harbor_ledger_memory.config import FolderAccess, FolderRule, Settings
from harbor_ledger_memory.vault.boundary import VaultBoundary, VaultPathError


@pytest.mark.parametrize(
    "candidate", ["../outside.md", "/tmp/outside.md", "AI-evil/x.md", "AI/%2e%2e/x.md"]
)
def test_boundary_rejects_escape_candidates(
    boundary: VaultBoundary, candidate: str
) -> None:
    with pytest.raises(VaultPathError):
        boundary.resolve_ai_path(PurePosixPath(candidate))


def test_boundary_rejects_symlink_escape(
    boundary: VaultBoundary, tmp_path: Path
) -> None:
    (boundary.ai_root / "escape").symlink_to(tmp_path, target_is_directory=True)

    assert list(boundary.iter_markdown_files()) == []


def test_boundary_rejects_file_symlink_aliases_and_checks_resolved_suffix(
    boundary: VaultBoundary,
) -> None:
    admitted = boundary.ai_root / "admitted.md"
    admitted.write_text("inside", encoding="utf-8")
    text_target = boundary.ai_root / "target.txt"
    text_target.write_text("not markdown", encoding="utf-8")
    (boundary.ai_root / "alias.md").symlink_to(admitted)
    (boundary.ai_root / "text-alias.md").symlink_to(text_target)

    assert list(boundary.iter_markdown_files()) == [admitted]


def test_boundary_ai_root_property_is_read_only(
    boundary: VaultBoundary, tmp_path: Path
) -> None:
    with pytest.raises(AttributeError):
        boundary.ai_root = tmp_path / "replacement"  # type: ignore[misc]


def test_boundary_fails_closed_before_traversing_replaced_root(
    boundary: VaultBoundary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.md").write_text("outside", encoding="utf-8")
    original_root = boundary.ai_root
    original_root.rename(tmp_path / "AI-original")
    original_root.symlink_to(outside, target_is_directory=True)

    def unexpected_walk(*args: object, **kwargs: object) -> object:
        pytest.fail("replaced AI root must be rejected before os.walk")

    monkeypatch.setattr(os, "walk", unexpected_walk)

    assert list(boundary.iter_markdown_files()) == []


def test_boundary_rejects_deeply_encoded_traversal(boundary: VaultBoundary) -> None:
    candidate = "../outside.md"
    for _ in range(9):
        candidate = quote(candidate, safe="")

    with pytest.raises(VaultPathError):
        boundary.resolve_ai_path(PurePosixPath(candidate))


def test_boundary_enumerates_only_markdown_inside_ai_root(
    boundary: VaultBoundary, tmp_path: Path
) -> None:
    note = boundary.ai_root / "nested" / "note.md"
    note.parent.mkdir()
    note.write_text("inside", encoding="utf-8")
    (boundary.ai_root / "nested" / "not-markdown.txt").write_text(
        "ignored", encoding="utf-8"
    )
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")

    assert list(boundary.iter_markdown_files()) == [note]


def test_boundary_resolves_admitted_paths(boundary: VaultBoundary) -> None:
    assert boundary.resolve_ai_path(PurePosixPath("nested/note.md")) == (
        boundary.ai_root / "nested" / "note.md"
    )


def test_boundary_defaults_to_full_vault(tmp_path: Path) -> None:
    (tmp_path / "root.md").write_text("root", encoding="utf-8")
    nested = tmp_path / "Notes" / "nested.md"
    nested.parent.mkdir()
    nested.write_text("nested", encoding="utf-8")

    boundary = VaultBoundary(Settings(vault_path=tmp_path))

    assert [
        path.relative_to(tmp_path).as_posix() for path in boundary.iter_markdown_files()
    ] == [
        "root.md",
        "Notes/nested.md",
    ]


def test_boundary_narrows_root_and_excludes_denied_subtree(tmp_path: Path) -> None:
    (tmp_path / "Notes" / "Private").mkdir(parents=True)
    (tmp_path / "Notes" / "Private" / "secret.md").write_text("secret")
    (tmp_path / "Notes" / "Public").mkdir()
    (tmp_path / "Notes" / "Public" / "note.md").write_text("public")
    (tmp_path / "Other.md").write_text("outside")
    settings = Settings(
        vault_path=tmp_path,
        index_root="Notes",
        folder_rules=(FolderRule(path="Notes/Private", access=FolderAccess.DENY),),
    )

    boundary = VaultBoundary(settings)

    assert [path.name for path in boundary.iter_markdown_files()] == ["note.md"]
    assert boundary.is_readable("Notes/Public/note.md")
    assert not boundary.is_readable("Notes/Private/secret.md")
    with pytest.raises(VaultPathError):
        boundary.resolve_vault_path(PurePosixPath("Other.md"))
