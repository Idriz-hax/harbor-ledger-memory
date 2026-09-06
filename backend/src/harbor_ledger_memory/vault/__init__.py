"""Access boundaries for configured vault subtrees."""

from .boundary import AdmittedFileSnapshot, VaultBoundary, VaultPathError

__all__ = ["AdmittedFileSnapshot", "VaultBoundary", "VaultPathError"]
