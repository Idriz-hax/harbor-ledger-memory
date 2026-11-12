"""Per-token folder access policy evaluation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import PurePosixPath

from sqlalchemy import and_, false, func, not_, or_, true
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from harbor_ledger_memory.config import FolderAccess, FolderRule


class AccessPolicy:
    """Evaluate vault-relative folder access for one token."""

    def __init__(
        self,
        rules: Sequence[FolderRule],
        admin: bool = False,
        *,
        agent_principal: str | None = None,
        agent_private_enabled: bool = False,
        agent_private_root: str | PurePosixPath = "AgentPrivate",
    ) -> None:
        self._rules = tuple(rules)
        self._admin = admin
        self._agent_principal = agent_principal
        self._agent_private_enabled = agent_private_enabled
        self._agent_private_root = PurePosixPath(agent_private_root)

    @property
    def rules(self) -> tuple[FolderRule, ...]:
        return self._rules

    @property
    def admin(self) -> bool:
        return self._admin

    @property
    def is_admin(self) -> bool:
        return self._admin

    @property
    def agent_principal(self) -> str | None:
        return self._agent_principal

    @property
    def agent_private_enabled(self) -> bool:
        return self._agent_private_enabled

    @property
    def agent_private_root(self) -> PurePosixPath:
        return self._agent_private_root

    def access_for(self, path: str | PurePosixPath) -> FolderAccess:
        """Return the deepest matching rule, defaulting to read-only."""
        target = PurePosixPath(path)
        if (
            target.is_absolute()
            or ".." in target.parts
            or "\\" in str(path)
            or "\x00" in str(path)
        ):
            return FolderAccess.NONE
        if self._is_agent_private(target):
            if not self._agent_private_enabled:
                return FolderAccess.NONE
            root_depth = len(self._agent_private_root.parts)
            if (
                len(target.parts) <= root_depth
                or
                self._agent_principal is None
                or target.parts[root_depth] != self._agent_principal
            ):
                return FolderAccess.NONE
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

    def fingerprint(self) -> str:
        """Stable policy binding for opaque, short-lived read handles."""
        payload = [(rule.path.as_posix(), rule.access.value) for rule in self._rules]
        payload.sort()
        binding: object = [
            self._admin,
            payload,
            self._agent_private_root.as_posix(),
            self._agent_private_enabled,
            self._agent_principal if self._agent_private_enabled else None,
        ]
        return hashlib.sha256(
            json.dumps(binding, separators=(",", ":")).encode()
        ).hexdigest()

    def _is_agent_private(self, path: PurePosixPath) -> bool:
        root = self._agent_private_root.parts
        return len(path.parts) >= len(root) and tuple(
            part.casefold() for part in path.parts[: len(root)]
        ) == tuple(part.casefold() for part in root)

    def readable_clause(
        self, column: ColumnElement[str] | InstrumentedAttribute[str]
    ) -> ColumnElement[bool]:
        """Return a parameterized SQL predicate equivalent to deepest-prefix reads."""
        predicates: list[ColumnElement[bool]] = []
        for rule in self._rules:
            prefix = rule.path.as_posix().rstrip("/")
            match = (
                true()
                if prefix == "."
                else or_(
                    column == prefix, column.like(_like_prefix(prefix), escape="\\")
                )
            )
            deeper: list[ColumnElement[bool]] = []
            for other in self._rules:
                if (
                    len(other.path.parts) > len(rule.path.parts)
                    and _matching_depth(rule.path, other.path) is not None
                ):
                    oprefix = other.path.as_posix().rstrip("/")
                    deeper.append(
                        true()
                        if oprefix == "."
                        else or_(
                            column == oprefix,
                            column.like(_like_prefix(oprefix), escape="\\"),
                        )
                    )
            if rule.access.is_readable:
                predicates.append(and_(match, not_(or_(*deeper))) if deeper else match)
        # Unruled paths default to read access; exclude all explicitly denied
        # prefixes. SQL parameters are generated by SQLAlchemy for every value.
        denied: list[ColumnElement[bool]] = []
        for rule in self._rules:
            if not rule.access.is_readable:
                prefix = rule.path.as_posix().rstrip("/")
                denied.append(
                    true()
                    if prefix == "."
                    else or_(
                        column == prefix, column.like(_like_prefix(prefix), escape="\\")
                    )
                )
        predicates.append(not_(or_(*denied)) if denied else true())
        clause = or_(*predicates)
        private = _case_insensitive_prefix(column, self._agent_private_root.as_posix())
        if private is not None:
            owned_path = self._agent_private_root / (self._agent_principal or "")
            owned = (
                _case_sensitive_prefix(column, owned_path.as_posix())
                if self._agent_private_enabled and self._agent_principal is not None
                else false()
            )
            clause = and_(clause, or_(not_(private), owned))
        return clause


def _case_sensitive_prefix(
    column: ColumnElement[str], prefix: str
) -> ColumnElement[bool]:
    """Match an exact path or slash-delimited descendant without SQLite LIKE folding."""
    return or_(
        column.collate("BINARY") == prefix,
        func.substr(column.collate("BINARY"), 1, len(prefix) + 1)
        == f"{prefix}/",
    )


def _case_insensitive_prefix(
    column: ColumnElement[str] | InstrumentedAttribute[str], prefix: str
) -> ColumnElement[bool]:
    lowered = func.lower(column)
    value = prefix.lower()
    return or_(
        lowered.collate("BINARY") == value,
        func.substr(lowered.collate("BINARY"), 1, len(value) + 1) == f"{value}/",
    )


def _like_prefix(prefix: str) -> str:
    """Escape literal folder names before adding a SQL LIKE descendant suffix."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "/%"


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
