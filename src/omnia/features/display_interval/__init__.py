"""Display Interval feature: show the predicted next interval on the answer side.

The overlay asks the shared ease pipeline (``ctx.ease.compute_ease(card, Good)``) so a
*synchronous* transformer like overdue_guard is reflected in the shown interval.
typed_accuracy is NOT reflected: its ease arrives over the async ``pycmd`` bridge after this
computation has already returned. Rendered as per-card dynamic JS via the web injector.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from omnia.core import anki_compat
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.core.reviewer.web_injector import WebAsset
from omnia.features.display_interval.logic import format_interval

_EASE_GOOD = 3

_CSS = (
    "#omnia-next-ivl{position:fixed;bottom:8px;right:10px;padding:2px 9px;"
    "border-radius:6px;background:rgba(0,0,0,.55);color:#fff;font-size:12px;"
    "z-index:9999;font-family:sans-serif;}"
)
_HIDE_JS = (
    "(function(){var d=document.getElementById('omnia-next-ivl');"
    "if(d){d.style.display='none';}})();"
)


def _render_js(text: str) -> str:
    payload = json.dumps(text)
    return (
        "(function(){var d=document.getElementById('omnia-next-ivl');"
        "if(!d){d=document.createElement('div');d.id='omnia-next-ivl';"
        "document.body.appendChild(d);}d.textContent=" + payload + ";"
        "d.style.display='block';})();"
    )


@register("display_interval")
class DisplayIntervalPlugin(FeaturePlugin):
    """Shows the predicted next interval in a corner overlay on the answer side."""

    name = "Display Interval"
    description = "Show the predicted next interval on the answer side."
    order = 30

    def on_enable(self, ctx: PluginContext) -> None:
        ctx.web.add_asset(self.id, WebAsset(css=_CSS))
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
        return _render_js(f"next: {format_interval(seconds)}")
