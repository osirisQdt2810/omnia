"""Display Interval feature: show the predicted next interval in the grading bar.

The label asks the shared ease pipeline (``ctx.ease.compute_ease(card, Good)``) so a
*synchronous* transformer like overdue_guard is reflected in the shown interval.
typed_accuracy is NOT reflected: its ease arrives over the async ``pycmd`` bridge after this
computation has already returned (a documented async limitation — see the module-level note in
typed_accuracy). Rendered into the reviewer's PERSISTENT bottom (grading) bar webview — the
Again/Hard/Good/Easy button area — as a fixed bottom-right, non-interactive label reading
``interval: <X>`` in the configured colour. The label ``<div>`` survives across cards (per-card
updates only touch an inner element of the bar), so it is driven directly off the reviewer
show-question / show-answer hooks rather than the card-webview injector.

**Template exposure** (``expose_to_templates``): the same pipeline preview is also handed to
the card template as ``window.omniaIntervals`` — ``{next_seconds, next_days, next_label,
current_days}`` — so template JS can branch on how well the card is known (e.g. read the
definition aloud while the answer is still shaky, the example once it is well learned).
Injected by PREPENDING a ``<script>`` through the ``card_will_show`` filter (answer side only),
which runs before any of the template's own scripts — deterministic, no polling handshake.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import omnia.gui.display_interval as _di_gui
from omnia.core import anki_compat
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
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


# Static, data-free snippets: hide the label (question side) and remove it from the bottom
# bar entirely (on_disable teardown). RENDER carries per-card data, so it is built per answer.
_HIDE_JS = _overlay_section("HIDE")
_REMOVE_JS = _overlay_section("REMOVE")


@register("display_interval")
class DisplayIntervalPlugin(FeaturePlugin):
    """Shows the predicted next interval in the reviewer's bottom grading bar."""

    name = "Display Interval"
    description = "Show the predicted next interval on the answer side."
    group = "Reviewing"
    order = 30
    config_model = DisplayIntervalSettings

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None

    def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        anki_compat.subscribe_hook("reviewer_did_show_question", self._on_question)
        anki_compat.subscribe_hook("reviewer_did_show_answer", self._on_answer)
        # Filter hook: prepend window.omniaIntervals into the answer HTML for templates.
        anki_compat.subscribe_hook("card_will_show", self._on_card_will_show)

    def on_disable(self, ctx: PluginContext) -> None:
        anki_compat.unsubscribe_hook("reviewer_did_show_question", self._on_question)
        anki_compat.unsubscribe_hook("reviewer_did_show_answer", self._on_answer)
        anki_compat.unsubscribe_hook("card_will_show", self._on_card_will_show)
        anki_compat.reviewer_bottom_eval(_REMOVE_JS)
        self._ctx = None

    def _on_question(self, *_args: Any) -> None:
        # Question side: hide the previous card's label until this answer's interval is known.
        anki_compat.reviewer_bottom_eval(_HIDE_JS)

    def _on_answer(self, card: Any, *_args: Any) -> None:
        # Fold a Good press through the pipeline as a non-destructive PREVIEW (apply=False) so
        # overdue_guard's (synchronous) adjustment shows WITHOUT consuming typed_accuracy's
        # staged ease. typed_accuracy stages async over pycmd — see the module docstring.
        # The hook passes the card first; tolerate any extra args Anki may add.
        if self._ctx is None:
            return
        effective = self._ctx.ease.compute_ease(card, _EASE_GOOD, apply=False)
        seconds = anki_compat.next_interval_seconds(card, effective)
        if seconds is None:
            return
        anki_compat.reviewer_bottom_eval(
            self._render_js(f"interval: {format_interval(seconds)}")
        )

    def _on_card_will_show(self, text: str, card: Any, kind: str) -> str:
        """Prepend ``window.omniaIntervals`` to the ANSWER html for template JS.

        Prepending (rather than eval'ing on show-answer) guarantees the value exists before
        any of the template's own inline scripts run — templates read it synchronously, no
        polling. Fallback contract: on ANY of — the flag off, no ease transformer active
        (overdue_guard / typed_accuracy disabled: the preview would be a raw Good, adding
        nothing over the current interval), or a compute failure — the html passes through
        untouched, ``window.omniaIntervals`` stays undefined, and the template's own
        fallback (e.g. ``{{info-Ivl:}}``, the current interval) applies.
        """
        if kind != "reviewAnswer" or self._ctx is None:
            return text
        if not getattr(self._ctx.settings, "expose_to_templates", True):
            return text
        if not self._ctx.ease.has_transformers():
            return text  # nothing shapes the ease -> let templates use the current interval
        try:
            # Same non-destructive pipeline preview as the grading-bar label (apply=False:
            # don't consume typed_accuracy's staged ease).
            effective = self._ctx.ease.compute_ease(card, _EASE_GOOD, apply=False)
            seconds = anki_compat.next_interval_seconds(card, effective)
        except Exception:
            return text  # never break card rendering over a preview
        if seconds is None:
            return text
        payload = {
            "next_seconds": int(seconds),
            "next_days": round(seconds / 86_400, 3),
            "next_label": format_interval(seconds),
            # card.ivl: days for review cards, 0 while (re)learning — document the caveat.
            "current_days": int(getattr(card, "ivl", 0) or 0),
        }
        script = (
            "<script>window.omniaIntervals = "
            + json.dumps(payload)
            + '; document.dispatchEvent(new CustomEvent("omnia:intervals"));</script>'
        )
        return script + text

    def _render_js(self, text: str) -> str:
        # The JS body lives in overlay.js; only the JSON-encoded label + configured colour are
        # injected here, so the dynamic part stays in Python while the markup lives on disk.
        return (
            _overlay_section("RENDER")
            .replace("__TEXT__", json.dumps(text))
            .replace("__COLOR__", json.dumps(self._ctx.settings.text_color))
        )
