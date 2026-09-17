"""How fast the generated voice speaks, and whether each engine is asked in its own dialect.

The conversions are the kind of thing that is wrong in exactly one direction and stays wrong
quietly: an inverted ``length_scale`` still synthesizes, still returns valid audio, and is
simply faster when the user asked for slower. Nothing downstream notices — the note gets a
sound file either way — so these are pinned against the engines' documented parameters rather
than against what the code happens to do.
"""

from __future__ import annotations

import pytest

from omnia.core.providers.tts import speed as tts_speed


class TestReadingAPaceThatMightBeAnything:
    """``clamp`` is the only gate between a stored value and every engine's parameter."""

    def test_normal_is_the_natural_pace(self):
        assert tts_speed.clamp(1.0) == 1.0

    @pytest.mark.parametrize("value", [0.0, -1.0, -0.5])
    def test_zero_and_below_mean_no_request(self, value):
        # Zero is how a Smart Notes field says "inherit"; negatives can only be corruption.
        assert tts_speed.clamp(value) == tts_speed.NORMAL

    @pytest.mark.parametrize("value", ["fast", None, object()])
    def test_an_unreadable_value_does_not_fail_the_synthesis(self, value):
        """A bad stored value costs the natural pace, not the note's audio.

        Raising here would fail a whole batch over a cosmetic preference, and the pace a note
        would have had without this feature is exactly the pace it gets.
        """
        assert tts_speed.clamp(value) == tts_speed.NORMAL

    def test_it_confines_an_absurd_value(self):
        assert tts_speed.clamp(50.0) == tts_speed.MAX_SPEED
        assert tts_speed.clamp(0.01) == tts_speed.MIN_SPEED


class TestEachEnginesDialect:
    @pytest.mark.parametrize(
        "speed,expected",
        [(1.0, "+0%"), (1.25, "+25%"), (0.8, "-20%"), (0.5, "-50%"), (2.0, "+100%")],
    )
    def test_edge_wants_a_signed_percentage_delta(self, speed, expected):
        # Signed even at zero: the value this replaced was the literal '+0%'.
        assert tts_speed.as_percent_delta(speed) == expected

    @pytest.mark.parametrize("speed,expected", [(1.0, 1.0), (0.5, 2.0), (2.0, 0.5)])
    def test_piper_wants_the_inverse(self, speed, expected):
        """``length_scale`` scales DURATION, so slower is a BIGGER number.

        Getting this backwards is silent: piper synthesizes happily either way.
        """
        assert tts_speed.as_length_scale(speed) == expected

    def test_a_pace_that_asks_for_nothing_is_recognisable(self):
        # Providers use this to leave the request byte-identical to a pre-speed one.
        assert tts_speed.is_normal(1.0)
        assert tts_speed.is_normal(0.0)  # "inherit"
        assert not tts_speed.is_normal(0.8)


class TestWhatEachProviderActuallySends:
    """The conversion being right is worth nothing if the provider does not send it."""

    def test_edge_puts_the_rate_in_the_prosody(self):
        from omnia.core.providers.tts.edge_tts import EdgeProtocolSynthesizer

        ssml = EdgeProtocolSynthesizer._mkssml("hello", "en-US-AriaNeural", "-20%")

        assert "rate='-20%'" in ssml

    def test_edge_defaults_to_the_voices_own_pace(self):
        from omnia.core.providers.tts.edge_tts import EdgeProtocolSynthesizer

        assert "rate='+0%'" in EdgeProtocolSynthesizer._mkssml("hello", "v")

    def test_the_provider_converts_before_handing_it_to_the_transport(self):
        from omnia.core.providers.tts.edge_tts import EdgeTTS

        seen: dict = {}

        class _Transport:
            def synthesize(self, text, voice, rate="+0%"):
                seen["rate"] = rate
                return b"MP3"

        EdgeTTS(synthesizer=_Transport()).synthesize("hi", lang="en", speed=0.8)

        assert seen["rate"] == "-20%"

    def test_openai_sends_speed_only_when_asked(self):
        from omnia.core.providers.tts.openai_compatible import OpenAICompatibleTTS

        sent: list = []

        class _Http:
            def post_json_for_bytes(self, url, payload, headers=None, **kw):
                sent.append(payload)
                return b"MP3"

        provider = OpenAICompatibleTTS(
            api_key="k", base_url="https://x/v1", model="m", voice="v", http=_Http()
        )
        provider.synthesize("hi")
        provider.synthesize("hi", speed=0.8)

        assert "speed" not in sent[0], (
            "a local OpenAI-compatible server is not obliged to implement every field of the "
            "spec; sending one nobody asked for can fail every synthesis"
        )
        assert sent[1]["speed"] == 0.8

    def test_piper_is_handed_the_inverse(self):
        from omnia.core.providers.tts.piper import PiperTTS

        seen: dict = {}

        class _Runner:
            def ensure_ready(self):
                pass

            def run(self, text, model_path, *, length_scale=1.0):
                seen["length_scale"] = length_scale
                return b"WAV"

        PiperTTS(model="/tmp/x.onnx", runner=_Runner()).synthesize("hi", speed=0.5)

        assert seen["length_scale"] == 2.0

    def test_google_translate_asks_for_slow_only_below_the_threshold(self):
        from omnia.core.providers.tts.google_translate import GoogleTranslateTTS

        asked: list = []

        class _Http:
            def get_bytes(self, url, params=None, headers=None, **kw):
                asked.append(params or {})
                return b"MP3"

        provider = GoogleTranslateTTS(http=_Http())
        provider.synthesize("hi", lang="en")
        provider.synthesize("hi", lang="en", speed=0.6)

        assert "ttsspeed" not in asked[0]
        assert (
            asked[1]["ttsspeed"] == "0.24"
        ), "the tw-ob endpoint has no rate parameter, only the Translate UI's slow mode"
