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

from omnia.core import anki_compat
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.core.reviewer.web_injector import WebAsset
from omnia.plugins.display_interval.config import DisplayIntervalSettings
from omnia.plugins.display_interval.logic import format_interval

_EASE_GOOD = 3

# Styling is applied imperatively in JS (mirroring the reference) so it can switch on Anki's
# night-mode class at render time: white + shadow at night, red (#c62828) in day, bold, fixed
# bottom-right, and pointer-events:none so it never intercepts clicks.
_HIDE_JS = (
    "(function(){var d=document.getElementById('__TA_NEXT_IVL');"
    "if(d){d.style.display='none';}})();"
)


def _render_js(text: str) -> str:
    payload = json.dumps(text)
    return (
        "(function(){"
        "function night(){try{"
        "var s=(location&&location.hash?String(location.hash):'').toLowerCase();"
        "if(s.indexOf('night')>=0)return true;}catch(e){}"
        "try{var b=document.body;if(b&&(b.className||'').toLowerCase().indexOf('night')>=0)"
        "return true;}catch(e){}"
        "try{var de=document.documentElement;"
        "if(de&&(de.className||'').toLowerCase().indexOf('night')>=0)return true;}catch(e){}"
        "return false;}"
        "var el=document.getElementById('__TA_NEXT_IVL');"
        "if(!el){el=document.createElement('div');el.id='__TA_NEXT_IVL';"
        "el.style.position='fixed';el.style.right='14px';el.style.bottom='4px';"
        "el.style.zIndex='999999';el.style.fontSize='12px';el.style.fontWeight='800';"
        "el.style.pointerEvents='none';el.style.userSelect='none';"
        "el.style.whiteSpace='nowrap';document.body.appendChild(el);}"
        "if(night()){el.style.color='#ffffff';el.style.opacity='0.85';"
        "el.style.textShadow='0 1px 2px rgba(0,0,0,0.55)';}"
        "else{el.style.color='#c62828';el.style.opacity='0.90';el.style.textShadow='none';}"
        "el.textContent=" + payload + ";el.style.display='block';})();"
    )


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
