"""Per-token folder access policy evaluation."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath

from harbor_ledger_memory.config import FolderAccess, FolderRule


class AccessPolicy:
    """Evaluate vault-relative folder access for one token."""

    def __init__(
        self,
        rules: Sequence[FolderRule],
        admin: bool = False,
    ) -> None:
        self._rules = tuple(rules)
        self._admin = admin

    @property
    def rules(self) -> tuple[FolderRule, ...]:
        return self._rules

    @property
    def admin(self) -> bool:
        return self._admin

    @property
    def is_admin(self) -> bool:
        return self._admin

    def access_for(self, path: str | PurePosixPath) -> FolderAccess:
        """Return the deepest matching rule, defaulting to read-only."""
        target = PurePosixPath(path)
        best: FolderRule | None = None
        best_depth = -1
        for rule in self._rules:
            depth = _matching_depth(rule.path, target)
            if depth is not None and depth > best_depth:
                best = rule
                best_depth = depth
        if best is None:
            return FolderAccess.READ
        return best.access

    def can_read(self, path: str | PurePosixPath) -> bool:
        return self.access_for(path).is_readable

    def can_propose(self, path: str | PurePosixPath) -> bool:
        return self.access_for(path).is_writable

    def can_auto_write(self, path: str | PurePosixPath) -> bool:
        return self.access_for(path).is_auto_writable

    def has_any_write(self) -> bool:
        return any(rule.access.is_writable for rule in self._rules)

    def readable_paths(
        self, paths: Sequence[str | PurePosixPath]
    ) -> list[str | PurePosixPath]:
        return [path for path in paths if self.can_read(path)]


def _normalized_parts(path: PurePosixPath) -> tuple[str, ...]:
    parts = path.parts
    if not parts or parts == (".",):
        return ()
    return parts


def _matching_depth(rule_path: PurePosixPath, target: PurePosixPath) -> int | None:
    rule_parts = _normalized_parts(rule_path)
    target_parts = _normalized_parts(target)
    if not rule_parts:
        return 0
    if len(target_parts) < len(rule_parts):
        return None
    if target_parts[: len(rule_parts)] != rule_parts:
        return None
    return len(rule_parts)
