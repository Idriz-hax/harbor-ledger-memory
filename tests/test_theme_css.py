"""The shared theme must scope .brand to the sidebar in media queries.

A bare ``.brand`` rule inside an @media block hides the login page's brand
mark and name on narrow screens, so only the ``.sidebar .brand`` form is
allowed there. (The base .brand styling rules are intentional.)
"""

from __future__ import annotations

import re
from pathlib import Path

STYLE = (
    Path(__file__).resolve().parents[1]
    / "backend" / "src" / "harbor_ledger_memory" / "static" / "style.css"
)


def test_media_queries_scope_brand_to_sidebar() -> None:
    css = STYLE.read_text()
    media = "".join(
        block for block in re.split(r"(?=@media)", css) if block.startswith("@media")
    )
    assert ".brand" not in media.replace(".sidebar .brand", "")
