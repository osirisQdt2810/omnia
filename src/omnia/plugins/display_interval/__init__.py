"""Display Interval feature: show the predicted next interval on the answer side.

The overlay asks the shared ease pipeline (``ctx.ease.compute_ease(card, Good)``) so a
*synchronous* transformer like overdue_guard is reflected in the shown interval.
typed_accuracy is NOT reflected: its ease arrives over the async ``pycmd`` bridge after this
computation has already returned (a documented async limitation — see the module-level note in
typed_accuracy). Rendered as per-card dynamic JS via the web injector, styled to match the
reference's ``display_interval.js``: a fixed bottom-right, non-interactive, night-mode-aware
label reading ``interval: <X>``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import omnia.gui.display_interval as _di_gui
from omnia.core import anki_compat
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.core.reviewer.web_injector import WebAsset
from omnia.gui.assets import read_asset
from omnia.plugins.display_interval.config import DisplayIntervalSettings
from omnia.plugins.display_interval.logic import format_interval

_EASE_GOOD = 3


def _overlay_section(name: str) -> str:
    """Return the body of the ``// ===<name>===`` section of ``overlay.js``, trimmed."""
    text = read_asset(_di_gui.__file__, "web", "overlay.js")
    for block in text.split("// ===")[1:]:
        header, _, body = block.partition("\n")
        if header.strip() == f"{name}===":
            return body.strip()
    raise KeyError(f"overlay.js section not found: {name}")


# Static "hide the label" snippet, shown on the question side.
_HIDE_JS = _overlay_section("HIDE")


def _render_js(text: str) -> str:
    # The JS body lives in overlay.js; only the JSON-encoded label is injected here, so the
    # dynamic part stays in Python while the markup lives on disk.
    return _overlay_section("RENDER").replace("__TEXT__", json.dumps(text))


@register("display_interval")
class DisplayIntervalPlugin(FeaturePlugin):
    """Shows the predicted next interval in a corner overlay on the answer side."""

    name = "Display Interval"
    description = "Show the predicted next interval on the answer side."
    group = "Reviewing"
    order = 30
    config_model = DisplayIntervalSettings

    def on_enable(self, ctx: PluginContext) -> None:
        ctx.web.add_asset(self.id, WebAsset())
        ctx.web.add_dynamic(
            self.id,
            on_question=lambda _card: _HIDE_JS,
            on_answer=lambda card: self._overlay_js(ctx, card),
        )

    def on_disable(self, ctx: PluginContext) -> None:
        ctx.web.remove(self.id)

    @staticmethod
    def _overlay_js(ctx: PluginContext, card: Any) -> Optional[str]:
        # Fold a Good press through the pipeline so overdue_guard's (synchronous) adjustment
        # shows. typed_accuracy is async (pycmd) and not yet staged here — see module docstring.
        effective = ctx.ease.compute_ease(card, _EASE_GOOD)
        seconds = anki_compat.next_interval_seconds(card, effective)
        if seconds is None:
            return None
        return _render_js(f"interval: {format_interval(seconds)}")
