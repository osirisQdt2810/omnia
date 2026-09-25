"""`no-undef` over the assembled page bundles.

There are ~4k lines of JavaScript behind the settings and Smart Notes dialogs, and until this
they had no linter at all. The bug that prompted it: a refactor removed a `const` and left one
of its two readers behind, so the gen-order animation threw a `ReferenceError` inside the
timeout callback — before the line that reschedules it. The animation stopped dead on the
first field for every user, Play stayed disabled, and nothing in the suite noticed, because
reading an undeclared identifier is perfectly valid syntax.

The check runs over the ASSEMBLED bundle, not the pieces. The Smart Notes page is eleven files
concatenated into one IIFE in a fixed order, so linting them separately reports every
cross-piece reference as undefined — hundreds of them, all fine. Assembled the way the page
assembles it, the only undefined names left are the page's real globals, listed below.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

#: Names the host supplies rather than the bundle: Anki's webview bridge, and the note-type
#: list the page bakes in. Anything else undefined is a bug, which is the point.
PAGE_GLOBALS = ("pycmd", "SN_TYPES")

#: Browser globals the bundles legitimately use. Not `eslint-config-*`'s browser preset: this
#: is a QtWebEngine view, the list is short, and an explicit one says which host APIs the page
#: is allowed to assume.
BROWSER_GLOBALS = [
    "window",
    "document",
    "console",
    "navigator",
    "location",
    "alert",
    "confirm",
    "setTimeout",
    "clearTimeout",
    "setInterval",
    "clearInterval",
    "requestAnimationFrame",
    "getComputedStyle",
    "MutationObserver",
    "Event",
    "URL",
    "Blob",
    "FileReader",
    "DOMParser",
    "XMLHttpRequest",
    "fetch",
    "Image",
]


def _bundles() -> dict[str, str]:
    """Each page's JavaScript, assembled exactly as the page builder assembles it."""
    import omnia.gui.settings_html as settings_html
    import omnia.gui.smart_notes.html as sn_html
    from omnia.gui.assets import read_asset, read_assets

    return {
        "smart_notes.js": read_assets(
            sn_html.__file__, "web", names=sn_html._PAGE_JS_PARTS
        ),
        "settings.js": read_asset(settings_html.__file__, "web", "settings.js"),
    }


def _lint(tmp_path, bundles: dict[str, str]) -> list[dict]:
    """Run eslint's `no-undef` over `bundles`; return its messages."""
    globals_map = {name: "readonly" for name in (*BROWSER_GLOBALS, *PAGE_GLOBALS)}
    (tmp_path / "eslint.config.mjs").write_text(
        "export default [{files: ['**/*.js'], languageOptions: "
        "{ecmaVersion: 2020, sourceType: 'script', globals: "
        + json.dumps(globals_map)
        + "}, rules: {'no-undef': 'error'}}];",
        encoding="utf-8",
    )
    for name, source in bundles.items():
        # Explicit: Windows writes in the locale codec by default and the bundles are full of
        # arrows and check marks, so the check died on cp1252 before eslint saw a byte — which
        # is what the Windows leg reported the first time this ran.
        (tmp_path / name).write_text(source, encoding="utf-8")
    try:
        result = _run(tmp_path, bundles)
    except OSError as exc:
        # On Windows `npx` is a `.cmd`, which CreateProcess will not launch directly. Rather
        # than shelling out (and quoting paths by hand), the check runs where it can: the
        # Linux leg of the matrix is the gate, and this is JavaScript — nothing about the
        # result is platform-specific.
        pytest.skip(f"cannot launch npx here: {exc}")
    if not result.stdout.strip():
        pytest.skip(f"eslint unavailable (no network for npx?): {result.stderr[:300]}")
    return [
        {"file": os.path.basename(entry["filePath"]), **message}
        for entry in json.loads(result.stdout)
        for message in entry["messages"]
    ]


def _run(tmp_path, bundles: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            # Resolved, not the bare name: on Windows `npx` is a `.cmd` shim and subprocess
            # does not go through the shell to find it.
            shutil.which("npx") or "npx",
            "--yes",
            "eslint@9",
            "--no-config-lookup",
            "-c",
            "eslint.config.mjs",
            "-f",
            "json",
            *bundles,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )


@pytest.mark.skipif(
    shutil.which("npx") is None,
    reason="needs node/npx; CI runners all ship it, a contributor's box may not",
)
class TestNothingReadsAnIdentifierThatWasNeverDeclared:
    def test_the_bundles_are_clean(self, tmp_path):
        messages = _lint(tmp_path, _bundles())

        assert not messages, "\n".join(
            f"{m['file']}:{m['line']} {m['message']}" for m in messages
        )

    def test_the_check_is_actually_running(self, tmp_path):
        """A lint that silently lints nothing is worse than no lint.

        It is also the failure mode this file is one step away from: assembled wrongly, or
        pointed at the pieces, eslint reports either everything or nothing.
        """
        bundles = _bundles()
        bundles[
            "smart_notes.js"
        ] += "\nfunction sanity() { return aNameNobodyDeclared; }\n"

        messages = _lint(tmp_path, bundles)

        assert [m["ruleId"] for m in messages] == ["no-undef"]
        assert "aNameNobodyDeclared" in messages[0]["message"]

    def test_a_cross_piece_reference_is_not_reported(self, tmp_path):
        """The pieces are one IIFE, so `send` in 11-transfer resolves to `01-bridge`'s.

        Linting them separately would report hundreds of those. This asserts the assembly is
        what gets linted, which is the whole reason the check is usable at all.
        """
        messages = _lint(tmp_path, _bundles())

        assert not [m for m in messages if "'send'" in m["message"]]
