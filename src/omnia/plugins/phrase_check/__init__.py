"""Phrase Check: what is wrong with this sentence, and what a fluent speaker would write.

The clippers already answer "is this word in my collection?". This answers the question that comes
just before it — the user is reading or writing something and is not sure the sentence is right.

What comes back is a LIST of small fixes, each with its own reason behind a button, and then the
whole phrase rewritten with the changes marked. That shape is the feature: "your sentence should
be X" teaches nothing, and one paragraph explaining six unrelated problems is read by nobody.

Two registers, because the same sentence is wrong in different ways depending on whether it is
being said or written — "I ain't got none" is a mistake in an essay and ordinary in conversation,
and a corrector with one standard is wrong half the time with total confidence.

Answers are remembered, because the panel is transient by design: click away and it is gone, and
without a cache coming back costs another request, another wait, and can come back DIFFERENT,
which reads as the tool being unreliable rather than as sampling.

**This plugin serves the clippers and has no reviewer surface of its own.** Everything it does is
reached from a selection in a browser or on the desktop, through the same loopback service
``word_lookup`` already runs — one socket, one place a clipper has to be told about.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional

from omnia.core import services
from omnia.core.logging import get_logger
from omnia.core.plugin import FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.plugins.phrase_check import library
from omnia.plugins.phrase_check.cache import STORE_FILENAME, CorrectionCache, file_store
from omnia.plugins.phrase_check.config import PhraseCheckSettings
from omnia.plugins.phrase_check.correction import (
    DEFAULT_FIXES_SHOWN,
    MAX_FIXES_SHOWN,
    MODES,
    WRITTEN,
    Correction,
)
from omnia.plugins.phrase_check.prompt import language_name
from omnia.plugins.phrase_check.service import PhraseChecker, PhraseCheckError

logger = get_logger("phrase_check")

#: The name the core service registry publishes this under. A clipper reaches it through
#: ``word_lookup``'s socket, and ``word_lookup`` finds it here by NAME rather than by importing
#: this module — plugins never import each other (ADR-019).
CHECK_SERVICE = "phrase_check.check"

#: Saving one as a note. A second name rather than a flag on the first: they are different
#: operations with different failure modes — one spends money and touches nothing, the other
#: spends nothing and writes to the collection — and a clipper that can do one is not thereby
#: entitled to do the other.
SAVE_SERVICE = "phrase_check.save"


@register("phrase_check")
class PhraseCheckPlugin(FeaturePlugin):
    """Corrects a phrase a clipper sent, and explains every change."""

    name = "Phrase Check"
    description = "Check a phrase for grammar, word choice and how natural it sounds."
    group = "AI"
    tooltip = (
        "Select a phrase in a browser or on the desktop and press Correct.\n"
        "\n"
        "• Every change is its own card with its own reason behind an Explanation button — "
        "six corrections in one paragraph is an essay nobody reads.\n"
        "• Two registers: spoken and written. The same sentence is wrong in different ways "
        "depending on which, so the panel asks.\n"
        "• It flags sentences that are grammatically perfect and that no fluent speaker "
        "would actually say, which is the part a learner most needs.\n"
        "• Answers are remembered, so coming back to a phrase costs nothing.\n"
        "• Uses the LLM provider Omnia is configured with. No card is changed and nothing is "
        "written to your collection — it only reads what you selected."
    )
    order = 60
    config_model = PhraseCheckSettings

    def __init__(self) -> None:
        self._ctx: Optional[PluginContext] = None

    def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        # Published by NAME through the core registry rather than imported (ADR-019): word_lookup
        # serves the clippers and calls this, and two plugins that import each other cannot be
        # switched off independently.
        services.provide(CHECK_SERVICE, self._check)
        services.provide(SAVE_SERVICE, self._save)
        logger.info("phrase_check: ready")

    def on_disable(self, _ctx: PluginContext) -> None:
        services.revoke(CHECK_SERVICE)
        services.revoke(SAVE_SERVICE)
        self._ctx = None

    # --- what a clipper reaches ------------------------------------------------------------
    def _check(
        self, text: str, mode: str = "", refresh: bool = False
    ) -> dict[str, Any]:
        """Correct ``text`` and return it JSON-ready.

        The shape a clipper renders, built here rather than in the caller: the panel, the
        Explanation buttons and the bolded rewrite all read the same thing, and two of them
        deriving it separately is how they come to disagree.

        Raises:
            PhraseCheckError: With a sentence the clipper shows as-is.
        """
        settings = self._settings()
        checker = self._checker(settings)
        correction = checker.check(
            text, mode=_mode(mode, settings), refresh=bool(refresh)
        )
        return as_payload(correction, shown=_fixes_shown(settings))

    def _save(
        self,
        text: str,
        mode: str = "",
        col: Any = None,
        on_main: Optional[Callable[[Callable[[], Any]], Any]] = None,
    ) -> dict[str, Any]:
        """Save the correction for ``text`` as a note, and say where it went.

        Two phases on two threads, and the split is the point.

        **Resolving** the correction happens on the calling (worker) thread, because it is
        usually a cache hit but NOT always — a changed model or language is a guaranteed miss,
        and so is an entry that aged out — and a miss is a synchronous call to an LLM. Doing
        that on the Qt main thread froze Anki for the length of it, and then `call_on_main`'s
        five-second patience expired and answered "nothing was saved" while the main thread went
        on to add the note regardless, so a second press added a duplicate.

        **Writing** happens through ``on_main``, because ``col`` may not be touched from a
        worker at all.

        The correction is looked up again rather than taken from the caller: the alternative is
        letting a clipper post whatever it likes into the collection, which is a different
        feature with a different risk.

        Args:
            text: The phrase, as it was checked.
            mode: The register it was checked in.
            col: The collection, when the caller has one. ``None`` reads it inside the write,
                on the thread that is about to use it — which is also what stops a stale handle
                surviving a profile switch.
            on_main: Marshals a callable onto the Qt main thread and waits. ``None`` writes
                inline, which is right headless and in tests.

        Raises:
            PhraseCheckError: With a sentence the clipper shows as-is.
        """
        settings = self._settings()
        checker = self._checker(settings)
        # Worker thread. May call the model.
        correction = checker.check(text, mode=_mode(mode, settings), refresh=False)

        def write() -> Any:
            target = col
            if target is None:
                from omnia.core import anki_compat

                target = anki_compat.main_window().col
            return library.save_correction(
                target,
                correction,
                deck=str(getattr(settings, "save_deck", library.DEFAULT_DECK) or ""),
                note_type=str(getattr(settings, "save_note_type", "") or ""),
            )

        try:
            saved = on_main(write) if on_main is not None else write()
        except library.SaveError as exc:
            raise PhraseCheckError(str(exc)) from exc
        return {
            "note_id": saved.note_id,
            "deck": saved.deck,
            "note_type": saved.note_type,
            "renamed": saved.renamed,
            "summary": saved.summary(),
        }

    def _checker(self, settings: PhraseCheckSettings) -> PhraseChecker:
        ctx = self._ctx
        if ctx is None:
            raise PhraseCheckError("Phrase Check is not running.")
        # A file under user_files/, NOT the config: a check runs on the HTTP worker thread and
        # col.db must not be written from one, the config blob is also rewritten by the settings
        # dialog, and the feature domain SYNCS -- five hundred cached corrections have no
        # business riding to AnkiWeb. See cache.py.
        read, write = file_store(ctx.paths.user_files_dir / STORE_FILENAME)
        return PhraseChecker(
            ctx.providers,
            CorrectionCache(read, write),
            language=language_name(getattr(settings, "language", "")),
            model=str(getattr(settings, "model", "") or ""),
        )

    def _settings(self) -> PhraseCheckSettings:
        """Read settings PER REQUEST, so changing the language takes effect on the next check."""
        ctx = self._ctx
        if ctx is None:
            raise PhraseCheckError("Phrase Check is not running.")
        try:
            current = ctx.config.feature_settings(self.id)
        except Exception:
            logger.exception("phrase_check: could not read the settings")
            current = None
        return (
            current
            if isinstance(current, PhraseCheckSettings)
            else PhraseCheckSettings()
        )


def _fixes_shown(settings: PhraseCheckSettings) -> int:
    """How many fixes the panel should list, read defensively.

    ``getattr`` and a clamp, because this is read per request from a settings object that may
    have been stored by a build without the field (``PersistedModel`` is ``extra="allow"``), or
    hand-edited to something that is not a number. A panel showing five is a far better answer
    than a correction that fails.
    """
    try:
        value = int(getattr(settings, "fixes_shown", DEFAULT_FIXES_SHOWN))
    except (TypeError, ValueError):
        return DEFAULT_FIXES_SHOWN
    return max(1, min(value, MAX_FIXES_SHOWN))


def _mode(requested: str, settings: PhraseCheckSettings) -> str:
    """The register to use: what the panel asked for, else the configured default."""
    if requested in MODES:
        return requested
    configured = str(getattr(settings, "default_mode", WRITTEN) or WRITTEN)
    return configured if configured in MODES else WRITTEN


def as_payload(
    correction: Correction, *, shown: int = DEFAULT_FIXES_SHOWN
) -> dict[str, Any]:
    """A correction as the clippers render it.

    The highlighted runs are computed HERE and sent, rather than sending both sentences and
    letting each clipper diff them: two implementations of "which words changed" is two answers
    to a question with one right one, and the web and desktop panels would slowly disagree.

    Every fix travels, and ``shown`` says how many of them a PANEL should list. The list is not
    truncated here on purpose: the same answer is what a saved card is built from, and a card is
    for coming back to — cutting the tail off before it is stored would lose the fixes nobody
    had room for, which are exactly the ones worth a second look.

    Args:
        correction: What the model said.
        shown: How many fixes the panel should list, most important first.
    """
    return {
        "original": correction.original,
        "rewritten": correction.rewritten,
        "mode": correction.mode,
        "already_good": correction.already_good,
        "changed": correction.changed,
        # How many of `fixes` a panel should list. A clipper slices; nothing is dropped here.
        "shown": max(1, int(shown)),
        "fixes": [
            {
                "before": fix.before,
                "after": fix.after,
                "why": fix.why,
                "kind": fix.kind,
                "is_deletion": fix.is_deletion,
            }
            for fix in correction.fixes
        ],
        # ``[[text, is_new], …]`` — joined in order it is exactly ``rewritten``, so a panel that
        # renders the runs cannot show a sentence nobody wrote.
        "highlight": [[text, is_new] for text, is_new in correction.highlighted()],
    }
