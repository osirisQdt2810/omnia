"""A field's Provider picker keeps a pin the list does not offer.

It used to drop such a pin to "(inherit)" on sight. That looked like tidying and was data loss:
the page posts what the picker shows, so the next save of ANY row on the note type rewrote the
field to the default provider. A pin on an endpoint removed since, or on the retired
``openai_compatible`` slot (ADR-022), is exactly the kind of value this build no longer lists.

The shipped functions, run in node over a DOM just big enough — the pattern of the page-half
tests in ``test_endpoint_models.py``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[2] / "src/omnia/gui/smart_notes/web"

#: What the text kind's picker offers in these tests.
_OFFERED = ["gemini", "openrouter", "custom:gpu"]


def _function(source: str, name: str) -> str:
    """The top-level page function ``name``, cut out of its piece of the page IIFE."""
    found = re.search(rf"  function {name}\(.*?\n  \}}", source, re.S)
    assert found, f"{name} moved"
    return found.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
class TestAPinTheListDoesNotOffer:
    def _run(self, preset: str) -> dict:
        bridge = (_WEB / "01-bridge.js").read_text(encoding="utf-8")
        render = (_WEB / "03-render.js").read_text(encoding="utf-8")
        shipped = "\n".join(
            [
                _function(bridge, "opt"),
                _function(render, "fillCellSelect"),
                _function(render, "rebuildProvider"),
            ]
        )
        script = f"""
function node(tag) {{
  return {{tag, children: [], className: "", textContent: "", value: "", selected: false,
    innerHTML: "", appendChild(c) {{ this.children.push(c); }}, addEventListener() {{}}}};
}}
const document = {{createElement: node}};
const cell = node("td");
const tr = {{dataset: {{}},
  querySelector(s) {{ return s === ".sn-provider-cell" ? cell : null; }}}};
function providerNames(kind) {{ return {json.dumps(_OFFERED)}; }}
const rebuilt = [];
function rebuildModel(row, kind, provider) {{ rebuilt.push(provider); }}
function rebuildVoice() {{}}
{shipped}
rebuildProvider(tr, "text", {json.dumps(preset)});
const options = cell.children[0].children;
console.log(JSON.stringify({{
  labels: options.map((o) => o.textContent),
  selected: options.filter((o) => o.selected).map((o) => [o.value, o.textContent]),
  model: rebuilt,
}}));
"""
        out = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_it_stays_selected_and_says_it_was_saved(self):
        got = self._run("openai_compatible")

        assert got["selected"] == [["openai_compatible", "openai_compatible (saved)"]]

    def test_the_model_picker_is_built_for_it(self):
        """Not for "(inherit)": the Model picker belongs to the provider the row really has."""
        assert self._run("openai_compatible")["model"] == ["openai_compatible"]

    def test_no_pin_is_still_inherit(self):
        got = self._run("")

        assert got["selected"] == [["", "(inherit)"]]
        assert got["model"] == [""]

    def test_a_listed_pin_gets_no_saved_copy(self):
        got = self._run("custom:gpu")

        assert got["selected"] == [["custom:gpu", "custom:gpu"]]
        assert got["labels"] == ["(inherit)", *_OFFERED]
