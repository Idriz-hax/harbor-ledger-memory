"""Rebuildable SQLite persistence for parsed vault projections."""

from .database import (
    CatalogSession,
    create_database,
    persist_parsed_note,
    rebuild_catalog,
    search_fts,
)
from .models import ActivityEvent, Base, Diagnostic, Link, Note, ScanRun

__all__ = [
    "Base",
    "ActivityEvent",
    "CatalogSession",
    "Diagnostic",
    "Link",
    "Note",
    "ScanRun",
    "create_database",
    "persist_parsed_note",
    "rebuild_catalog",
    "search_fts",
]
