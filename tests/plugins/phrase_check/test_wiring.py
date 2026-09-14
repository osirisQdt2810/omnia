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

from omnia.core import services
from omnia.plugins.phrase_check import (
    CHECK_SERVICE,
    PhraseCheckPlugin,
    _mode,
    as_payload,
)
from omnia.plugins.phrase_check.config import PhraseCheckSettings
from omnia.plugins.phrase_check.correction import SPOKEN, WRITTEN, parse


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

    def test_a_setting_nobody_recognises_still_produces_a_usable_register(self):
        # The settings model is extra="allow" and the value is a free string, so this is
        # reachable from a hand-edited config as well as from an older build.
        assert _mode("", PhraseCheckSettings(default_mode="prose")) == WRITTEN


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
