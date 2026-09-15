"""Tests for the plugin itself: the wire contract, the register fallback, and the seam.

The endpoint tests inject a fake checker, so none of this glue was exercised by them — which is
how :func:`as_payload` came to be the one function in the feature with no test at all despite
being the thing both clippers render.

``as_payload`` in particular is worth pinning: the PR's own argument for computing the highlight
runs here rather than in each clipper is that the two panels must not be able to disagree. That
guarantee is only worth anything if the runs are checked to join back to exactly the rewrite.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from pydantic import ValidationError

from omnia.core import services
from omnia.core.config.schema import schema_from_model
from omnia.plugins.phrase_check import (
    CHECK_SERVICE,
    PhraseCheckPlugin,
    _fixes_shown,
    _mode,
    as_payload,
)
from omnia.plugins.phrase_check.config import PhraseCheckSettings
from omnia.plugins.phrase_check.correction import (
    DEFAULT_FIXES_SHOWN,
    MAX_FIXES_SHOWN,
    MODES,
    SPOKEN,
    WRITTEN,
    parse,
)


def _correction(payload: Optional[dict[str, Any]] = None, original: str = ""):
    return parse(
        payload
        or {
            "rewritten": "I went to the shop to buy milk.",
            "fixes": [
                {
                    "before": "have went",
                    "after": "went",
                    "why": "A finished action at a stated time takes the simple past.",
                    "kind": "grammar",
                },
                {"before": "for buy", "after": "to buy", "why": "Purpose takes “to”."},
            ],
        },
        original=original or "I have went to the shop for buy milk.",
        mode=WRITTEN,
    )


class TestTheWireContract:
    def test_the_runs_join_back_to_exactly_the_rewrite(self):
        # The whole justification for computing them here. If this can drift, the web panel and
        # the desktop panel will eventually show two different sentences for one answer.
        payload = as_payload(_correction())

        assert (
            "".join(text for text, _new in payload["highlight"]) == payload["rewritten"]
        )

    def test_the_runs_survive_a_rewrite_that_changed_nothing(self):
        payload = as_payload(
            _correction(
                {"rewritten": "I went to the shop.", "fixes": []},
                original="I went to the shop.",
            )
        )

        assert (
            "".join(text for text, _new in payload["highlight"]) == payload["rewritten"]
        )
        assert payload["already_good"] is True
        assert not any(
            new for _text, new in payload["highlight"]
        ), "it marked an unchanged word"

    def test_every_key_a_clipper_reads_is_present(self):
        payload = as_payload(_correction())

        assert set(payload) == {
            "original",
            "rewritten",
            "mode",
            "already_good",
            "changed",
            "fixes",
            "highlight",
            "shown",
        }

    def test_a_fix_carries_everything_its_card_renders(self):
        fix = as_payload(_correction())["fixes"][0]

        assert set(fix) == {"before", "after", "why", "kind", "is_deletion"}
        assert fix["before"] == "have went"
        assert fix["after"] == "went"
        assert fix["is_deletion"] is False

    def test_it_is_json_shaped_all_the_way_down(self):
        # It goes out through json.dumps on the HTTP thread; a dataclass or a tuple surviving
        # into it is a 500 at the moment a user presses the button.
        import json

        payload = as_payload(_correction())

        assert json.loads(json.dumps(payload))["rewritten"] == payload["rewritten"]

    def test_a_deletion_is_marked_as_one(self):
        payload = as_payload(
            _correction(
                {
                    "rewritten": "I went to the shop.",
                    "fixes": [
                        {
                            "before": "very quickly",
                            "after": "",
                            "why": "It adds nothing.",
                        }
                    ],
                },
                original="I went very quickly to the shop.",
            )
        )

        assert payload["fixes"][0]["is_deletion"] is True


class TestWhichRegister:
    def test_what_the_panel_asked_for_wins(self):
        assert _mode(SPOKEN, PhraseCheckSettings(default_mode=WRITTEN)) == SPOKEN

    def test_no_request_falls_back_to_the_setting(self):
        # The clipper sends '' deliberately when the user has not chosen: the configured default
        # lives HERE, and a clipper guessing would quietly override a setting.
        assert _mode("", PhraseCheckSettings(default_mode=SPOKEN)) == SPOKEN

    def test_a_register_nobody_recognises_falls_back_rather_than_failing(self):
        assert _mode("shouted", PhraseCheckSettings(default_mode=SPOKEN)) == SPOKEN

    def test_the_model_refuses_a_register_that_does_not_exist(self):
        # The fix that matters. `default_mode` was a bare `str` carrying a v2-only choices hint
        # that Pydantic 1.10 swallows without error, so the settings dialog rendered a free-text
        # box: a user could type "speech", have it save without complaint, and get every
        # correction judged in the wrong register with nothing on screen saying so.
        with pytest.raises(ValidationError):
            PhraseCheckSettings(default_mode="prose")

    def test_the_settings_choices_are_the_registers_the_code_knows(self):
        # The annotation has to spell the values out (mypy rejects names inside a Literal), so
        # this is what stops it drifting from `correction.MODES`, which everything else uses.
        fields = {field.key: field for field in schema_from_model(PhraseCheckSettings)}

        assert set(fields["default_mode"].choices) == set(MODES)

    def test_the_settings_form_offers_a_dropdown_rather_than_a_text_box(self):
        # Asserted on the GENERATED schema, not on the annotation: the annotation is only half
        # the mechanism, and it was the other half (how the form derives a choice field) that
        # made the v2 spelling fail silently.
        fields = {field.key: field for field in schema_from_model(PhraseCheckSettings)}

        assert fields["default_mode"].kind == "choice"
        assert set(fields["default_mode"].choices) == {SPOKEN, WRITTEN}

    def test_a_settings_object_that_is_not_one_still_produces_a_usable_register(self):
        # `_mode` reads the value with getattr, so it must survive whatever it is handed —
        # including a model built by `construct()`, which skips validation.
        class NotReallySettings:
            default_mode = "prose"

        assert _mode("", NotReallySettings()) == WRITTEN


class TestTheServiceSeam:
    """word_lookup finds the checker by NAME (ADR-019) — so the name has to actually appear."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        services.revoke(CHECK_SERVICE)
        yield
        services.revoke(CHECK_SERVICE)

    def test_enabling_publishes_it_and_disabling_takes_it_away(self):
        plugin = PhraseCheckPlugin()
        assert services.lookup(CHECK_SERVICE) is None

        plugin.on_enable(object())
        assert callable(
            services.lookup(CHECK_SERVICE)
        ), "word_lookup would answer 503 for a feature that is switched on"

        plugin.on_disable(object())
        assert (
            services.lookup(CHECK_SERVICE) is None
        ), "a disabled plugin kept serving the clippers"

    def test_disabling_drops_the_context_so_nothing_can_check_after_it(self):
        from omnia.plugins.phrase_check.service import PhraseCheckError

        plugin = PhraseCheckPlugin()
        plugin.on_enable(object())
        check = services.lookup(CHECK_SERVICE)
        plugin.on_disable(object())

        # The callable a caller grabbed BEFORE the switch went off. It must refuse rather than
        # reach through a context that is gone.
        with pytest.raises(PhraseCheckError):
            check("I have went.", "", False)

    def test_the_plugin_is_off_by_default_like_every_other(self):
        assert PhraseCheckPlugin.always_on is False


class TestHowManyFixesAPanelLists:
    """The limit is a display setting, and it must not become a limit on the correction.

    The panel lists a few; the rewrite fixes everything; a saved card keeps every fix. Truncating
    the list before it is stored would lose exactly the fixes nobody had room for — the ones
    worth coming back to, which is what a card is for.
    """

    def test_the_limit_travels_with_the_answer(self):
        payload = as_payload(_correction(), shown=3)

        assert payload["shown"] == 3

    def test_every_fix_travels_whatever_the_limit_says(self):
        payload = as_payload(_correction(), shown=1)

        assert (
            len(payload["fixes"]) == 2
        ), "the answer was truncated before it was stored"

    def test_the_rewrite_is_untouched_by_the_limit(self):
        # It fixes everything that was found; the limit decides what is SHOWN, never what is
        # corrected. A rewrite trimmed to match the visible list would be a sentence that is
        # still wrong in the ways nobody had room to explain.
        full = as_payload(_correction(), shown=99)
        clipped = as_payload(_correction(), shown=1)

        assert clipped["rewritten"] == full["rewritten"]
        assert clipped["highlight"] == full["highlight"]

    def test_a_nonsense_limit_still_produces_a_usable_one(self):
        assert as_payload(_correction(), shown=0)["shown"] == 1
        assert as_payload(_correction(), shown=-4)["shown"] == 1

    def test_it_defaults_rather_than_requiring_a_caller_to_know(self):
        from omnia.plugins.phrase_check.correction import DEFAULT_FIXES_SHOWN

        assert as_payload(_correction())["shown"] == DEFAULT_FIXES_SHOWN


class TestReadingTheLimitFromSettings:
    def test_it_comes_off_the_settings(self):
        assert _fixes_shown(PhraseCheckSettings(fixes_shown=3)) == 3

    def test_settings_stored_without_it_fall_back(self):
        # extra="allow", so a config written before the field exists loads fine — and a
        # correction that failed on the first check after an upgrade would be a worse bug.
        settings = PhraseCheckSettings()
        del settings.__dict__["fixes_shown"]

        assert _fixes_shown(settings) == DEFAULT_FIXES_SHOWN

    def test_something_that_is_not_a_number_falls_back(self):
        class NotReally:
            fixes_shown = "lots"

        assert _fixes_shown(NotReally()) == DEFAULT_FIXES_SHOWN

    def test_it_is_clamped_at_both_ends(self):
        class Silly:
            fixes_shown = 0

        class Sillier:
            fixes_shown = 10_000

        assert _fixes_shown(Silly()) == 1
        assert _fixes_shown(Sillier()) == MAX_FIXES_SHOWN

    def test_the_model_itself_refuses_an_out_of_range_value(self):
        # The form is a slider between the two bounds, but a hand-edited config is not.
        with pytest.raises(ValidationError):
            PhraseCheckSettings(fixes_shown=0)
        with pytest.raises(ValidationError):
            PhraseCheckSettings(fixes_shown=MAX_FIXES_SHOWN + 1)
