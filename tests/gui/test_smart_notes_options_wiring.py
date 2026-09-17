"""Every global Smart Notes option is reachable, round-trips, and is named once.

Both failures this pins were made while adding `overwrite_scope`: the key was written into the
options payload twice (a literal duplicate that Python silently collapses, so the second wins and
nothing complains), and an option is useless if the page cannot draw it or send it back.
"""

from __future__ import annotations

import pathlib
import re

SRC = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "omnia"
    / "gui"
    / "smart_notes"
)


def _text(*parts: str) -> str:
    return (SRC.joinpath(*parts)).read_text(encoding="utf-8")


class TestNoOptionIsNamedTwice:
    def test_the_options_payload_has_no_duplicate_key(self):
        """A repeated dict key is legal Python and silently keeps the last one.

        Harmless here and not elsewhere: the same mistake in the SAVE map would drop whichever
        value was written first, which is how a setting appears to not persist.
        """
        source = _text("dialogs", "controllers", "config.py")
        block = source[source.index("def _options_payload") :]
        block = block[: block.index("\n    def ", 1)]
        keys = re.findall(r'^\s+"([a-z_]+)":', block, re.M)

        assert len(keys) == len(set(keys)), sorted(k for k in keys if keys.count(k) > 1)

    def test_the_save_map_has_no_duplicate_key(self):
        source = _text("dialogs", "controllers", "config.py")
        block = source[source.index("settings.copy(") :]
        block = block[: block.index("\n        )")]
        keys = re.findall(r'^\s+"([a-z_]+)":', block, re.M)

        assert len(keys) == len(set(keys)), sorted(k for k in keys if keys.count(k) > 1)


class TestEveryOptionSurvivesTheRoundTrip:
    """Python sends it, the page shows it, the page sends it back, Python stores it."""

    def _options(self) -> list[str]:
        source = _text("dialogs", "controllers", "config.py")
        block = source[source.index("def _options_payload") :]
        block = block[: block.index("\n    def ", 1)]
        # Only the plain scalar options; the integration map and status are built elsewhere.
        skip = {"auto_generate_integrations", "integration_status", "integrations"}
        return [
            k
            for k in re.findall(r'^\s+"([a-z_]+)": settings\.', block, re.M)
            if k not in skip
        ]

    def test_there_are_options_to_check(self):
        # A regex that silently matched nothing would make every test below vacuous.
        assert len(self._options()) >= 5

    def test_the_page_reads_each_one(self):
        handlers = _text("web", "05-handlers.js")

        for name in self._options():
            assert f"opts.{name}" in handlers, f"the page never reads {name}"

    def test_the_page_sends_each_one_back(self):
        """Two spellings, both legitimate — some options go into the object literal and some
        are assigned onto it afterwards (the ones whose control may be absent)."""
        handlers = _text("web", "05-handlers.js")

        for name in self._options():
            sent = f"{name}:" in handlers or f"opts.{name} =" in handlers
            assert sent, f"the page never sends {name} back"

    def test_python_stores_each_one(self):
        source = _text("dialogs", "controllers", "config.py")
        saved = source[source.index("settings.copy(") :]

        for name in self._options():
            assert f'"{name}"' in saved, f"{name} is shown but never saved"


class TestTheOverwriteScopeControl:
    def test_the_page_offers_exactly_the_scopes_the_model_accepts(self):
        """A value the picker can produce that the settings model rejects is an unopenable
        panel — the failure #92 was about, one dialog along."""
        from omnia.plugins.smart_notes.provenance import SCOPES

        page = _text("web", "page.html")
        block = page[page.index('id="sn-opt-overwrite-scope"') :]
        block = block[: block.index("</select>")]
        offered = re.findall(r'value="([a-z_]+)"', block)

        assert sorted(offered) == sorted(SCOPES)

    def test_the_default_shown_is_the_one_that_changes_nothing(self):
        # A config from an older Omnia carries no scope; landing on whichever option happens to
        # be first would change what Overwrite does without being asked.
        handlers = _text("web", "05-handlers.js")

        assert 'opts.overwrite_scope || "always"' in handlers
