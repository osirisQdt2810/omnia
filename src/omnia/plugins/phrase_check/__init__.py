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

from dataclasses import replace
from typing import Any, Optional

from omnia.core import services
from omnia.core.logging import get_logger
from omnia.core.plugin import ConfigField, FeaturePlugin, PluginContext
from omnia.core.registry import register
from omnia.plugins.phrase_check.cache import STORE_FILENAME, CorrectionCache, file_store
from omnia.plugins.phrase_check.config import PhraseCheckSettings
from omnia.plugins.phrase_check.correction import MODES, WRITTEN, Correction
from omnia.plugins.phrase_check.prompt import language_name
from omnia.plugins.phrase_check.service import PhraseChecker, PhraseCheckError

logger = get_logger("phrase_check")

#: The name the core service registry publishes this under. A clipper reaches it through
#: ``word_lookup``'s socket, and ``word_lookup`` finds it here by NAME rather than by importing
#: this module — plugins never import each other (ADR-019).
CHECK_SERVICE = "phrase_check.check"


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

    def config_schema(self, repo: Any = None) -> list[ConfigField]:
        """The declared fields, with ``model`` turned into a list of what can actually be picked.

        A model id is not free text: it has to be one the ACTIVE provider serves, and typing one
        it does not is a failed check with a provider error nobody can act on. The list is not
        declarable on the settings model either — it depends on which provider is configured
        right now, which is why it is filled here, per open, rather than baked into a
        ``Literal`` the way ``default_mode`` is.

        The empty option leads and means "whatever Omnia is set to", which is the answer for
        almost everybody; pinning one is for spending less (a correction is short and frequent)
        or more (when the answers are not good enough) than the rest of the add-on does.
        """
        fields = super().config_schema(repo)
        models = self._available_models(repo)
        if not models:
            # Nothing to offer — no provider configured, or one whose catalogue we do not carry.
            # It stays a TEXT box, because a dropdown holding only "Omnia's default" is a
            # control that cannot be used, and it would take away the one thing that still
            # works here: typing the id yourself.
            return fields
        choices = ("", *models)
        return [
            (
                replace(field, kind="choice", choices=choices)
                if field.key == "model"
                else field
            )
            for field in fields
        ]

    def _available_models(self, repo: Any = None) -> tuple[str, ...]:
        """The text models the configured provider serves, or none when that cannot be read.

        The repo comes from the CALLER when there is one, and only falls back to the activation
        context. Reading it off ``on_enable`` alone meant the dropdown appeared only while the
        feature was switched on: tick it off, press Configure, and the model field was the
        free-text box this replaced — with nothing on screen saying why.
        """
        config = repo if repo is not None else getattr(self._ctx, "config", None)
        if config is None:
            return ()
        try:
            from omnia.core.providers.catalog import text_models

            provider = str(config.llm_settings().provider or "")
            return tuple(text_models(provider))
        except Exception:
            # No settings, an unknown provider, a config that will not load. A free-text box
            # beats a dropdown with one empty row in it, and `_choices_with` keeps whatever is
            # already stored either way.
            logger.debug("phrase_check: could not list models", exc_info=True)
            return ()

    def on_enable(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        # Published by NAME through the core registry rather than imported (ADR-019): word_lookup
        # serves the clippers and calls this, and two plugins that import each other cannot be
        # switched off independently.
        services.provide(CHECK_SERVICE, self._check)
        logger.info("phrase_check: ready")

    def on_disable(self, _ctx: PluginContext) -> None:
        services.revoke(CHECK_SERVICE)
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
        return as_payload(correction)

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


def _mode(requested: str, settings: PhraseCheckSettings) -> str:
    """The register to use: what the panel asked for, else the configured default."""
    if requested in MODES:
        return requested
    configured = str(getattr(settings, "default_mode", WRITTEN) or WRITTEN)
    return configured if configured in MODES else WRITTEN


def as_payload(correction: Correction) -> dict[str, Any]:
    """A correction as the clippers render it.

    The highlighted runs are computed HERE and sent, rather than sending both sentences and
    letting each clipper diff them: two implementations of "which words changed" is two answers
    to a question with one right one, and the web and desktop panels would slowly disagree.
    """
    return {
        "original": correction.original,
        "rewritten": correction.rewritten,
        "mode": correction.mode,
        "already_good": correction.already_good,
        "changed": correction.changed,
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
