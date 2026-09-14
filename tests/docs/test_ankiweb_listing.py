"""The AnkiWeb listing describes the add-on people actually install.

It is the one page a stranger reads before deciding whether to install anything, and it is also
the file furthest from the code — nothing imports it, so it goes stale silently. This add-on has
already shipped a listing describing a feature that did not exist, caught in review rather than
by anything here.

What is checked is only what can be checked mechanically: that every plugin has a row, and that
no row survives a plugin being removed. What a row *says* is still a human's job — but a feature
that ships with no mention at all, or a mention of one that is gone, is now a failing test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

LISTING = Path(__file__).resolve().parents[2] / "docs" / "ankiweb.md"


@pytest.fixture(scope="module")
def listing() -> str:
    return LISTING.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def plugins() -> dict:
    """Every registered plugin, by id. Imported the way Anki imports them."""
    import omnia.plugins  # noqa: F401 - the import is what runs every @register
    from omnia.core.registry import FEATURE_REGISTRY

    return dict(FEATURE_REGISTRY)


def _rows(listing: str) -> dict[str, str]:
    """The plugin table, as ``{bolded name: what it says}``."""
    rows = {}
    for line in listing.splitlines():
        match = re.match(r"^\|\s*\*\*(.+?)\*\*\s*\|\s*(.+?)\s*\|$", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


class TestEveryPluginIsListed:
    def test_the_table_was_found_at_all(self, listing):
        # Guards the parser, not the content: a reformatted table that stopped matching would
        # make every test below pass vacuously, which is the failure mode of this whole file.
        assert len(_rows(listing)) >= 5

    def test_each_registered_plugin_has_a_row(self, listing, plugins):
        named = _rows(listing)
        missing = [
            plugin.name
            for plugin in (cls() for cls in plugins.values())
            if plugin.name not in named
        ]

        assert not missing, (
            f"shipped but not described on AnkiWeb: {missing}. Someone installs the add-on "
            f"and finds a feature the listing never mentioned."
        )

    def test_no_row_describes_something_that_is_gone(self, listing, plugins):
        real = {plugin.name for plugin in (cls() for cls in plugins.values())}
        # The header row's "Plugin | What it does" is not bolded, so it never reaches here.
        ghosts = [name for name in _rows(listing) if name not in real]

        assert not ghosts, (
            f"described on AnkiWeb but not in the add-on: {ghosts}. This listing has already "
            f"shipped a feature that did not exist."
        )

    def test_every_row_actually_says_something(self, listing):
        empty = [name for name, text in _rows(listing).items() if len(text) < 30]

        assert not empty, f"rows with nothing useful in them: {empty}"
