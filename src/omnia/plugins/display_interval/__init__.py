"""Display Interval feature: show the predicted next interval in the grading bar.

The label asks the shared ease pipeline (``ctx.ease.compute_ease(card, Good)``) so every
transformer is reflected in the shown interval — overdue_guard synchronously, typed_accuracy on
a second pass. typed_accuracy's grade arrives over the async ``pycmd`` bridge AFTER show-answer
has already computed and drawn the label, so the first draw cannot see it; the plugin publishes
:data:`INTERVAL_LABEL_SERVICE` while it is enabled and typed_accuracy asks for a redraw once it
has staged its ease. Without that the label said "1mo" while the card was about to be graded
Hard — the number was a correct interval for the ease it was computed with, and a wrong answer
to the only question the reader is asking it.

The capability is published by NAME (``core.services``) rather than imported, so neither plugin
knows the other exists and a disabled display_interval simply stops being asked. Rendered into the reviewer's PERSISTENT bottom (grading) bar webview — the
Again/Hard/Good/Easy button area — as a fixed bottom-right, non-interactive label reading
``interval: <X>`` in the configured colour. The label ``<div>`` survives across cards (per-card
updates only touch an inner element of the bar), so it is driven directly off the reviewer
show-question / show-answer hooks rather than the card-webview injector.

**Template exposure** (``expose_to_templates``): the same pipeline preview is also handed to
the card template as ``window.omniaIntervals`` — ``{next_seconds, next_days, next_label,
current_days, state}`` — so template JS can branch on how well the card is known (e.g. read the
definition aloud while the answer is still shaky, the example once it is well learned).
Injected by PREPENDING a ``<script>`` through the ``card_will_show`` filter (answer side only),
which runs before any of the template's own scripts — deterministic, no polling handshake.

It is published TWICE on a card whose ease a late grader CHANGES. The prepend cannot carry a
measurement that has not been taken yet, so the first value is the ungraded Good; when
typed_accuracy stages a different one, the global is set again and ``omnia:intervals`` fires
with the corrected payload as its ``detail``. A template that must be right in both cases reads
what is already there AND listens:

.. code-block:: javascript

    const decide = () => { /* branch on window.omniaIntervals */ };
    if (window.omniaIntervals) decide();                   // the prepend already ran
    document.addEventListener("omnia:intervals", decide);  // and the correction, if one comes

Listening ALONE is not enough: the prepend's script runs before the template's own, so its
event has already fired by the time a listener could be registered, and a ``CustomEvent`` is
not sticky. The correction is suppressed when the payload is unchanged — a correctly typed
answer stages the same Good the preview already showed — so a listener that plays audio does
not play it twice on the common case.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import omnia.gui.display_interval as _di_gui
from omnia.core import anki_compat, services
from omnia.core.logging import get_logger
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.gui.assets import read_asset
from omnia.plugins.display_interval.config import DisplayIntervalSettings
from omnia.plugins.display_interval.logic import format_interval

logger = get_logger()

_EASE_GOOD = 3

#: Published while this plugin is enabled: an object with ``refresh()``, for a plugin whose
#: ease lands AFTER show-answer (typed_accuracy) to ask for the label to be drawn again.
INTERVAL_LABEL_SERVICE = "display_interval.interval_label"

# card.type -> template-facing state name (Anki: 0=new 1=learning 2=review 3=relearning).
_CARD_STATES = {0: "new", 1: "learning", 2: "review", 3: "relearning"}


class _IntervalLabel:
    """What :data:`INTERVAL_LABEL_SERVICE` hands out: one verb, not the plugin.

    A consumer gets the capability it needs and no way to reach into the rest of the feature —
    and, because the object is withdrawn at ``on_disable``, a stale handle cannot outlive the
    switch the way an import would.
    """

    def __init__(self, refresh: Any) -> None:
        self._refresh = refresh

    def refresh(self) -> None:
        """Draw the interval label again for the card currently on screen."""
        self._refresh()


def _intervals_js(payload: dict) -> str:
    """The one statement pair that publishes the template preview and announces it.

    Shared by the prepend and the redraw so the two can never drift into announcing different
    shapes for the same thing.
    """
    encoded = json.dumps(payload)
    return (
        f"window.omniaIntervals = {encoded}; "
        'document.dispatchEvent(new CustomEvent("omnia:intervals", '
        f"{{detail: {encoded}}}));"
    )


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
        # The template payload already on screen, by card id. A redraw that would publish the
        # same thing publishes nothing: the event is how a template knows something CHANGED,
        # and a correctly typed answer stages the very Good the preview already showed — so
        # firing anyway made a listener that plays audio play it twice, on the common case.
        self._published: dict[Any, dict] = {}

    def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        anki_compat.subscribe_hook("reviewer_did_show_question", self._on_question)
        anki_compat.subscribe_hook("reviewer_did_show_answer", self._on_answer)
        # Filter hook: prepend window.omniaIntervals into the answer HTML for templates.
        anki_compat.subscribe_hook("card_will_show", self._on_card_will_show)
        services.provide(INTERVAL_LABEL_SERVICE, _IntervalLabel(self._refresh))

    def on_disable(self, ctx: PluginContext) -> None:
        services.revoke(INTERVAL_LABEL_SERVICE)
        anki_compat.unsubscribe_hook("reviewer_did_show_question", self._on_question)
        anki_compat.unsubscribe_hook("reviewer_did_show_answer", self._on_answer)
        anki_compat.unsubscribe_hook("card_will_show", self._on_card_will_show)
        anki_compat.reviewer_bottom_eval(_REMOVE_JS)
        self._ctx = None

    def _on_question(self, *_args: Any) -> None:
        # Question side: hide the previous card's label until this answer's interval is known,
        # and forget what the last card was told — a new card publishes from scratch.
        self._published = {}
        anki_compat.reviewer_bottom_eval(_HIDE_JS)

    def _on_answer(self, card: Any, *_args: Any) -> None:
        # The hook passes the card first; tolerate any extra args Anki may add.
        self._draw(card)

    def _draw(self, card: Any) -> None:
        """Compute the label for ``card`` and push it into the bottom bar.

        Folds a Good press through the pipeline as a non-destructive PREVIEW (apply=False) so
        overdue_guard's adjustment — and typed_accuracy's staged ease, once there is one —
        show WITHOUT consuming it before the card is actually answered.
        """
        if self._ctx is None or card is None:
            return
        effective = self._ctx.ease.compute_ease(card, _EASE_GOOD, apply=False)
        seconds = anki_compat.next_interval_seconds(card, effective)
        if seconds is None:
            return
        anki_compat.reviewer_bottom_eval(
            self._render_js(f"interval: {format_interval(seconds)}")
        )

    def _refresh(self) -> None:
        """Say the interval again for the card on screen — the published capability.

        BOTH consumers, because both were computed before the late grade existed: the label in
        the grading bar, and ``window.omniaIntervals`` in the card webview. The template value
        cannot be right at prepend time — the measurement does not exist yet — but it can be
        corrected and announced, and ``omnia:intervals`` is the event the contract already
        fires for exactly that.

        Answer side only. A transformer that stages late has nothing to say about a question
        the user has not turned over yet, and drawing there would undo the hide the question
        hook just performed.
        """
        try:
            if anki_compat.reviewer_side() != "answer":
                return
            card = anki_compat.current_card()
            self._draw(card)
            self._push_intervals(card)
        except Exception:  # a redraw is never worth taking the reviewer down for
            logger.exception("display_interval: could not refresh the interval label")

    def _push_intervals(self, card: Any) -> None:
        """Re-publish ``window.omniaIntervals`` into the card webview — only if it changed.

        Same payload and same event as the prepend, so a template reads the global the way it
        did on the first fire and gets the value that reflects the grade the card is about to
        receive. Silent when the value is identical, because the event means "this changed"
        and firing it for an unchanged value is a duplicate the listener cannot tell apart.
        """
        payload = self._payload(card)
        if payload is None or payload == self._published.get(getattr(card, "id", None)):
            return
        self._remember(card, payload)
        anki_compat.reviewer_eval(_intervals_js(payload))

    def _remember(self, card: Any, payload: Optional[dict]) -> None:
        """Record what the card on screen has been told (one card at a time)."""
        cid = getattr(card, "id", None)
        self._published = {cid: payload} if payload is not None else {}

    def _payload(self, card: Any) -> Optional[dict]:
        """The template-facing preview for ``card``, or None when there is nothing to say.

        None on ANY of — the flag off, no ease transformer active (the preview would be a raw
        Good, adding nothing over the current interval), or a compute failure — which is what
        leaves ``window.omniaIntervals`` undefined and the template's own fallback (e.g.
        ``{{info-Ivl:}}``) in charge.
        """
        if self._ctx is None or card is None:
            return None
        if not getattr(self._ctx.settings, "expose_to_templates", True):
            return None
        if not self._ctx.ease.has_transformers():
            return None
        try:
            # Same non-destructive pipeline preview as the grading-bar label (apply=False:
            # don't consume typed_accuracy's staged ease).
            effective = self._ctx.ease.compute_ease(card, _EASE_GOOD, apply=False)
            seconds = anki_compat.next_interval_seconds(card, effective)
        except Exception:
            return None  # never break card rendering over a preview
        if seconds is None:
            return None
        return {
            "next_seconds": int(seconds),
            "next_days": round(seconds / 86_400, 3),
            "next_label": format_interval(seconds),
            # card.ivl is the last SCHEDULED day-interval. Caveat it does NOT capture: a
            # (re)learning card still carries a positive day count (a lapsed card keeps its
            # post-lapse days, e.g. 1) — the "10m" learning step is never in ivl. Templates
            # that mean "just forgotten" must branch on `state`, not on a small interval.
            "current_days": int(getattr(card, "ivl", 0) or 0),
            # 0=new 1=learning 2=review 3=relearning -> a template can put a just-lapsed
            # card back on its "still shaky" branch regardless of the interval numbers.
            "state": _CARD_STATES.get(int(getattr(card, "type", 2) or 0), "review"),
        }

    def _on_card_will_show(self, text: str, card: Any, kind: str) -> str:
        """Prepend ``window.omniaIntervals`` to the ANSWER html for template JS.

        Prepending (rather than eval'ing on show-answer) guarantees the value exists before
        any of the template's own inline scripts run — templates read it synchronously, no
        polling. What it CANNOT contain is a grade that has not been measured yet:
        typed_accuracy reports after the answer is on screen, so a template reading this
        synchronously sees the ungraded Good. The ``omnia:intervals`` event fires again with
        the corrected value (see :meth:`_refresh`); a template that must be right about a
        mistyped answer has to listen for it rather than read once.
        """
        if kind != "reviewAnswer":
            return text
        payload = self._payload(card)
        if payload is None:
            return text
        self._remember(card, payload)
        return f"<script>{_intervals_js(payload)}</script>" + text

    def _render_js(self, text: str) -> str:
        # The JS body lives in overlay.js; only the JSON-encoded label + configured colour are
        # injected here, so the dynamic part stays in Python while the markup lives on disk.
        return (
            _overlay_section("RENDER")
            .replace("__TEXT__", json.dumps(text))
            .replace("__COLOR__", json.dumps(self._ctx.settings.text_color))
        )
