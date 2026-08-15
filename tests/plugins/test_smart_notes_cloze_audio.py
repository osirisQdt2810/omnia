"""Tests for the ``cloze_audio`` tool (smart-notes tools phase 3).

The tool has exactly one rule — **the answer is never spoken** — and the tests are organised
around the ways that could be broken:

* :class:`TestNeverSpeaksTheAnswer` is the safety net. A fake voice encodes the text it was
  asked to say straight into its samples, so a test can decode the produced clip and assert the
  answer is not in ANY of it. It also proves the failure paths hard-fail instead of falling
  through to a tool that would read the sentence out — and that a chain pairing this tool with
  another that could speak the field is refused before anything runs, in either order.
* :class:`TestSpanFinding` covers where the hole goes: an already-clozed source, one that needs
  the word located, several holes, and the markup traps.
* :class:`TestSplice` is the frame math at 22050 Hz mono — the measured-duration invariant,
  silence vs beep, the fades, and the guards that refuse mismatched or non-PCM audio.

No network, no Anki, no real codec: the MP3 side runs against a fake sidecar.
"""

from __future__ import annotations

import logging
import struct
import wave

import pytest

from omnia.core.audio.wav import SAMPLE_WIDTH, WavClip
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.tts.registry import tts_providers_with_ext
from omnia.plugins.smart_notes.config import (
    CompiledToolSpec,
    SmartNotesFieldConfig,
    SmartNotesFieldRule,
    default_tool_chain,
)
from omnia.plugins.smart_notes.engine import compile_field_rule
from omnia.plugins.smart_notes.engine.generators import ResolvedVoice
from omnia.plugins.smart_notes.engine.rules import rule_prerequisites
from omnia.plugins.smart_notes.engine.tools import (
    ClozeAudioTool,
    ClozeMaskPlanner,
    GenerationPipeline,
    MaskedAudioBuilder,
    MaskedSpeech,
    Produced,
    SidecarCodec,
    ToolContext,
    ToolError,
    ToolRequest,
    WavCodec,
    tools_catalog,
)
from omnia.plugins.smart_notes.engine.tools.pipeline import ToolAttempt

_RATE = 22050
_CHANNELS = 1

# One "syllable" of audio per character, so a clip's length is a readable function of its text.
# 100 ms at 22050 Hz — comfortably longer than the 10 ms splice fade, so a character's middle
# sample is never touched by a ramp and stays readable.
_FRAMES_PER_CHAR = _RATE // 10


def _wav_bytes(samples: list[int], *, rate: int = _RATE, channels: int = 1) -> bytes:
    """Serialise raw samples as a real ``.wav`` file (so the tool parses a genuine header)."""
    clip = WavClip(
        channels, SAMPLE_WIDTH, rate, struct.pack(f"<{len(samples)}h", *samples)
    )
    return clip.to_bytes()


class _FakeWavTTS:
    """A WAV voice whose SAMPLES encode the text it spoke, so a leak is detectable.

    Each character becomes ``_FRAMES_PER_CHAR`` frames holding that character's code point.
    Deterministic length (``len(text) * _FRAMES_PER_CHAR`` frames) makes the frame math
    checkable, and ``spoken`` records every request so a test can assert what was NOT asked for.
    """

    audio_ext = "wav"

    def __init__(self, *, rate: int = _RATE, channels: int = 1) -> None:
        self.rate = rate
        self.channels = channels
        self.spoken: list[str] = []

    def synthesize(self, text, *, lang=None, voice=None):
        self.spoken.append(text)
        samples: list[int] = []
        for char in text:
            samples.extend([ord(char)] * _FRAMES_PER_CHAR * self.channels)
        return _wav_bytes(samples, rate=self.rate, channels=self.channels)


class _FakeMp3TTS(_FakeWavTTS):
    """The same voice, declaring MP3 — the format the stdlib cannot open."""

    audio_ext = "mp3"


class _ExplodingTTS(_FakeWavTTS):
    """A voice whose provider is down."""

    def synthesize(self, text, *, lang=None, voice=None):
        raise ProviderError("HTTP 401")


class _FakeSidecar:
    """A stand-in codec: 'MP3' here is just the WAV bytes wrapped in a marker."""

    _PREFIX = b"MP3<"
    _SUFFIX = b">"

    def __init__(self, *, installed: bool = True) -> None:
        self._installed = installed

    def is_installed(self) -> bool:
        return self._installed

    def decode(self, data: bytes) -> bytes:
        self._require()
        if data.startswith(self._PREFIX):
            return data[len(self._PREFIX) : -len(self._SUFFIX)]
        return data

    def encode(self, wav: bytes) -> bytes:
        self._require()
        return self._PREFIX + wav + self._SUFFIX

    def _require(self) -> None:
        if not self._installed:
            raise ProviderError(
                "Audio codec (MP3 voices, PyAV) isn't installed — enable it in Smart "
                "Notes → Options → Advanced (native runtimes)."
            )


class _FakeLLM:
    """The paid path cloze_audio must never take (only the ``ai`` fall-through tool may)."""

    def generate_text(self, prompt, **kwargs):
        return f"gen:{prompt}"


class _Hub:
    """A ProviderHub-shaped stub pinned to one TTS provider; counts every LLM build."""

    def __init__(self, tts) -> None:
        self._tts = tts
        self.llm_calls = 0

    def tts(self, *, provider: str = ""):
        return self._tts

    def resolve_auto_voice(self, lang: str):
        return "fake", "fake-voice"

    def llm(self, **kwargs):
        self.llm_calls += 1
        return _FakeLLM()


class _NoDetector:
    """Language detection is off in these tests (the rule pins a voice or a language)."""

    def detect(self, providers, text):
        return None


def _ctx(tts=None, *, audio=None) -> ToolContext:
    """A tool context whose codec runtime is NOT installed unless a test says otherwise.

    That is the state a fresh install is in, and the tool must work fully in it (with a WAV
    voice), so it is the right default for every test that does not care.
    """
    return ToolContext(
        providers=_Hub(tts if tts is not None else _FakeWavTTS()),
        detector=_NoDetector(),
        logger=logging.getLogger("omnia.test"),
        audio=audio if audio is not None else _FakeSidecar(installed=False),
    )


def _rule(**kwargs) -> SmartNotesFieldRule:
    """A compiled-looking tts rule carrying the cloze_audio chain."""
    params = kwargs.pop("params", {})
    base = {
        "target_field": "Sentence (audio)",
        "base_field": "Word",
        "kind": "tts",
        "voice": "fake-voice",
        "tools": (CompiledToolSpec(name="cloze_audio", params=params),),
    }
    base.update(kwargs)
    return SmartNotesFieldRule(**base)


def _run(fields: dict[str, str], *, tts=None, audio=None, **kwargs):
    """Run the tool once and return its outcome."""
    rule = _rule(**kwargs)
    request = ToolRequest(
        rule=rule,
        fields=fields,
        params=ClozeAudioTool.parse_params(rule.tools[0].params),
    )
    return ClozeAudioTool().run(request, _ctx(tts, audio=audio))


def _produced_clip(fields: dict[str, str], *, tts=None, **kwargs) -> WavClip:
    """Run the tool and parse the WAV it produced (fails the test if it did not produce)."""
    outcome = _run(fields, tts=tts, **kwargs)
    assert isinstance(outcome, Produced), outcome
    assert outcome.result.kind == "tts"
    assert outcome.result.ext == "wav"
    return WavClip.from_bytes(outcome.result.data or b"")


def _decoded_text(clip: WavClip) -> str:
    """Read back the characters the fake voice encoded into ``clip``'s samples.

    Each character occupies ``_FRAMES_PER_CHAR`` frames holding its code point; the MIDDLE
    frame of each is read because the splice fades ramp a block's edges. A silent block (a
    masked answer) contributes nothing, which is exactly what the leak tests assert.
    """
    letters: list[str] = []
    samples = clip.samples()
    for frame in range(0, clip.frame_count, _FRAMES_PER_CHAR):
        middle = (frame + _FRAMES_PER_CHAR // 2) * clip.nchannels
        value = samples[middle]
        if value:
            letters.append(chr(value))
    return "".join(letters)


class TestToolContract:
    """The registry-facing declarations the picker and pipeline read."""

    def test_is_a_deterministic_tts_tool(self):
        assert ClozeAudioTool.name == "cloze_audio"
        assert ClozeAudioTool.kinds == frozenset({"tts"})
        # TTS calls, but never an LLM: the words come from the note.
        assert ClozeAudioTool.deterministic is True

    def test_it_is_deterministic_and_still_uses_the_rows_voice(self):
        # The two flags answer different questions, and this tool is why they are separate: it
        # invents no text (no LLM tokens) yet synthesizes speech, so the row's Provider/Voice
        # cells apply to it and must not be faded away.
        assert ClozeAudioTool.deterministic is True
        assert ClozeAudioTool.uses_provider is True
        entry = {item["name"]: item for item in tools_catalog(_ctx())}["cloze_audio"]
        assert entry["uses_provider"] is True

    def test_it_requires_its_source_field_but_not_its_word_field(self):
        # A required param becomes a HARD prerequisite, and a blank hard prerequisite BLOCKS
        # the field. `source_field` decides what is read at all, so refusing a blank one in the
        # picker is right. `word_field` is read ONLY when the source carries no {{cN::…}}
        # marker — requiring it would block generation on every note where it happens to be
        # empty, including the natural chain where the source already has markers and the param
        # is never looked at.
        #
        # Leaving it optional risks nothing: a blank or wrong word_field cannot make this tool
        # speak the answer (see the test below) — it makes the tool RAISE.
        assert ClozeAudioTool.required_params == frozenset({"source_field"})
        entry = {item["name"]: item for item in tools_catalog(_ctx())}["cloze_audio"]
        assert entry["required_params"] == ["source_field"]

    def test_a_blank_word_field_fails_the_tool_rather_than_speaking_the_answer(self):
        # The reason word_field can safely stay optional. No marker in the source and no word
        # to find => `plan` returns None and `run` raises; nothing is synthesized.
        with pytest.raises(ToolError, match="found nothing to hide"):
            _run(
                {"Word": "", "Sentence": "The cat sat down."},
                params={"source_field": "Sentence", "word_field": "Word"},
            )

    def test_is_in_the_catalog_with_its_params(self):
        entry = {item["name"]: item for item in tools_catalog(_ctx())}["cloze_audio"]
        assert entry["kinds"] == ["tts"]
        properties = entry["params_schema"]["properties"]
        assert set(properties) == {
            "source_field",
            "word_field",
            "mode",
            "beep_hz",
            "beep_gain_db",
            "strategy",
        }
        assert properties["mode"]["enum"] == ["silence", "beep"]
        assert properties["strategy"]["enum"] == ["auto", "segments", "sidecar"]

    def test_availability_advises_about_the_codec_without_claiming_to_be_unusable(self):
        # It must NAME what an MP3 voice needs…
        reason = ClozeAudioTool.availability(_ctx(audio=_FakeSidecar(installed=False)))
        assert reason is not None
        assert "piper" in reason and "edge_tts" in reason and "Advanced" in reason
        # …and say nothing once the runtime is there.
        assert ClozeAudioTool.availability(_ctx(audio=_FakeSidecar())) is None

    def test_the_provider_names_in_the_advice_come_from_the_registry(self):
        # Hard-coded lists go stale: the first copy of this message said "viet-tts" for a
        # provider registered as "viettts", and named "openai" without its family.
        reason = ClozeAudioTool.availability(_ctx(audio=_FakeSidecar(installed=False)))
        for name in tts_providers_with_ext("wav") + tts_providers_with_ext("mp3"):
            assert name in reason

    def test_a_wav_only_machine_can_still_enable_the_tool(self):
        # The zero-setup path: a piper voice installs itself on first use, so the picker must
        # not be able to read this as "unavailable" and grey the tool out (the reason it renders
        # the advice without gating). A tool cannot see which voice the ROW will resolve to.
        entry = {
            item["name"]: item
            for item in tools_catalog(_ctx(audio=_FakeSidecar(installed=False)))
        }["cloze_audio"]
        assert entry["kinds"] == ["tts"]  # the only real gate is the field's type
        assert "work as-is" in entry["unavailable_reason"]

    def test_named_fields_become_dependency_edges(self):
        rule = _rule(params={"source_field": "Sentence", "word_field": "Headword"})
        prereqs = dict(rule_prerequisites(rule))
        assert prereqs["Sentence"] == "hard"
        assert prereqs["Headword"] == "hard"

    def test_unnamed_fields_add_no_edges(self):
        assert ClozeAudioTool.referenced_fields({}) == []


class TestSpanFinding:
    """Where the hole goes — and the trap of stripping the markup before looking."""

    def test_an_already_clozed_source_masks_exactly_its_markers(self):
        plan = ClozeMaskPlanner("").plan("The cat {{c1::sat}} on the mat.")
        assert plan.segments == ("The cat", "on the mat.")
        assert plan.hidden == ("sat",)

    def test_a_hint_is_not_spoken_as_part_of_the_answer(self):
        plan = ClozeMaskPlanner("").plan("She {{c1::survived::s______d}} it.")
        assert plan.hidden == ("survived",)
        assert plan.segments == ("She", "it.")

    def test_a_source_without_markers_locates_the_word(self):
        plan = ClozeMaskPlanner("survive").plan("She survived the winter.")
        assert plan.hidden == ("survived",)
        assert plan.segments == ("She", "the winter.")

    def test_several_markers_all_become_holes(self):
        plan = ClozeMaskPlanner("").plan("{{c1::A}} then {{c2::B}} then C.")
        assert plan.hidden == ("A", "B")
        assert plan.segments == ("", "then", "then C.")

    def test_markers_win_over_the_word(self):
        # The text card decides what the audio card hides; otherwise the pair drift apart.
        plan = ClozeMaskPlanner("cat").plan("The cat {{c1::sat}} down.")
        assert plan.hidden == ("sat",)

    def test_markup_around_a_hole_is_not_spoken(self):
        plan = ClozeMaskPlanner("").plan(
            "<b>The cat</b> {{c1::sat}} [sound:x.mp3] down."
        )
        assert plan.hidden == ("sat",)
        assert plan.segments == ("The cat", "down.")

    def test_no_marker_and_no_match_plans_nothing(self):
        assert ClozeMaskPlanner("dog").plan("The cat sat.") is None

    def test_no_marker_and_no_word_plans_nothing(self):
        # Nothing says what to hide, so there is nothing this tool can safely speak.
        assert ClozeMaskPlanner("").plan("The cat sat.") is None

    def test_a_mismatched_shape_is_rejected(self):
        # The alternating shape is what keeps a spoken segment and a hidden answer from ever
        # being the same string; a value that breaks it must not exist at all.
        with pytest.raises(ValueError, match="one more segment"):
            MaskedSpeech(("The cat",), ("sat", "down"))

    def test_an_empty_marker_hides_nothing(self):
        assert ClozeMaskPlanner("").plan("The cat {{c1::}} sat.") is None

    def test_detection_text_omits_the_answer(self):
        plan = ClozeMaskPlanner("").plan("The cat {{c1::sat}} on the mat.")
        # The detector sends this to an LLM; the answer must not go with it.
        assert "sat" not in plan.detection_text
        assert plan.detection_text == "The cat on the mat."


class TestNeverSpeaksTheAnswer:
    """The rule the whole tool exists for, asserted on the produced audio itself."""

    def test_the_answer_is_absent_from_the_produced_audio(self):
        voice = _FakeWavTTS()
        ctx = _ctx(voice)
        rule = _rule(params={"source_field": "Sentence"})
        outcome = ClozeAudioTool().run(
            ToolRequest(
                rule=rule,
                fields={"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
                params=ClozeAudioTool.parse_params(rule.tools[0].params),
            ),
            ctx,
        )
        clip = WavClip.from_bytes(outcome.result.data)

        # The synthesized-and-discarded measurement call is the ONLY place the answer appears.
        assert voice.spoken == ["The cat", "sat", "down."]
        assert "sat" not in _decoded_text(clip)
        assert _decoded_text(clip) == "The catdown."
        assert ctx.providers.llm_calls == 0  # deterministic: no LLM spend, ever

    def test_the_masked_region_is_silence(self):
        clip = _produced_clip(
            {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
            params={"source_field": "Sentence"},
        )
        start = len("The cat") * _FRAMES_PER_CHAR
        gap = clip.samples()[start : start + len("sat") * _FRAMES_PER_CHAR]
        assert set(gap) == {0}

    def test_a_missing_span_fails_the_field_instead_of_speaking_it(self):
        with pytest.raises(ToolError, match="nothing to hide"):
            _run(
                {"Word": "dog", "Sentence": "The cat sat down."},
                params={"source_field": "Sentence"},
            )

    def test_an_mp3_voice_without_the_codec_fails_with_the_install_hint(self):
        with pytest.raises(ToolError) as excinfo:
            _run(
                {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
                tts=_FakeMp3TTS(),
                audio=_FakeSidecar(installed=False),
                params={"source_field": "Sentence"},
            )
        message = str(excinfo.value)
        assert "Advanced" in message
        # Not a NotApplicable: a chain must not be able to fall past this.
        assert isinstance(excinfo.value, ToolError)

    def test_a_provider_failure_is_terminal_too(self):
        with pytest.raises(ToolError, match="HTTP 401"):
            _run(
                {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
                tts=_ExplodingTTS(),
                params={"source_field": "Sentence"},
            )

    def test_an_unconfigured_voice_is_terminal_too(self):
        # Voice resolution lives in shared code and raises an ordinary ProviderError; without
        # a guard around the WHOLE produce phase that would fall through and speak the answer.
        class _NoVoices(_Hub):
            def resolve_auto_voice(self, lang: str):
                raise ProviderError("No Auto-detect voice set for language 'en'")

        rule = _rule(voice="", params={"source_field": "Sentence"})
        ctx = ToolContext(
            providers=_NoVoices(_FakeWavTTS()),
            detector=_NoDetector(),
            logger=logging.getLogger("omnia.test"),
        )
        request = ToolRequest(
            rule=rule,
            fields={"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
            params=ClozeAudioTool.parse_params(rule.tools[0].params),
        )
        with pytest.raises(ToolError, match="Auto-detect voice"):
            ClozeAudioTool().run(request, ctx)

    def test_an_unexpected_bug_still_raises_rather_than_declines(self, monkeypatch):
        # Even a plain programming error must not become "let the next tool speak it".
        import omnia.plugins.smart_notes.engine.tools.cloze_audio as module

        def _boom(*args, **kwargs):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(module.ResolvedVoice, "for_rule", _boom)
        with pytest.raises(ToolError, match="could not mask the answer"):
            _run(
                {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
                params={"source_field": "Sentence"},
            )

    def test_first_in_the_chain_it_produces_and_ai_never_runs(self):
        # The correct configuration, and the one the tool exists for: it masks, it produces,
        # and the chain stops there — `ai` is never reached, so nothing can speak the answer.
        rule = _rule(
            prompt="{{Sentence}}",
            tools=(
                CompiledToolSpec(
                    name="cloze_audio", params={"source_field": "Sentence"}
                ),
                CompiledToolSpec(name="ai"),
            ),
        )
        ctx = _ctx(_FakeWavTTS())

        result = GenerationPipeline(ctx).run(
            rule, {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."}
        )

        assert result.produced is not None
        assert result.produced.tool == "cloze_audio"
        assert [(a.tool, a.status) for a in result.attempts] == [
            ("cloze_audio", "produced")
        ]

    @pytest.mark.parametrize(
        ("order", "fields"),
        [
            # `ai` ordered FIRST simply wins — it never even reaches this tool.
            (
                ("ai", "cloze_audio"),
                {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
            ),
            # `cloze_audio` ordered first but UNABLE to mask (no marker, and its word is not in
            # the sentence): it raises, and the chain falls through to `ai` like any other
            # failure.
            (
                ("cloze_audio", "ai"),
                {"Word": "absent", "Sentence": "The cat sat down."},
            ),
        ],
    )
    def test_a_tts_tool_after_it_speaks_the_answer_and_that_is_the_configured_rule(
        self, order, fields
    ):
        """The documented COST of "run in order, fall through on failure" — pinned, not hidden.

        An earlier design refused this pairing outright (the tool declared itself exclusive,
        and a failure halted the chain). That was reversed by an explicit project decision: a
        chain runs in the configured order and every failure moves to the next tool, with no
        special cases. The consequence is real and belongs in the suite rather than in a
        comment — putting any tts tool after ``cloze_audio`` on a field whose answer must stay
        hidden WILL have that tool speak it, because
        :func:`omnia.core.lang.text.strip_markup` unwraps ``{{c1::sat}}`` to ``sat``.

        What this does NOT weaken is the tool's own guarantee: in both orderings below,
        ``cloze_audio`` itself never speaks the answer — it is the SIBLING that does.
        """
        rule = _rule(
            prompt="{{Sentence}}",  # what `ai` speaks: the answer, unwrapped
            tools=tuple(
                CompiledToolSpec(
                    name=name,
                    params=(
                        {"source_field": "Sentence", "word_field": "Word"}
                        if name == "cloze_audio"
                        else {}
                    ),
                )
                for name in order
            ),
        )
        ctx = _ctx(_FakeWavTTS())

        result = GenerationPipeline(ctx).run(rule, fields)

        assert result.produced is not None
        assert result.produced.tool == "ai"  # `ai` filled the field, answer and all
        assert result.attempts[-1] == ToolAttempt("ai", "produced", "")

    @pytest.mark.parametrize(
        "params",
        [
            # A value a newer release could give a KNOWN key (ADR-010) or a hand-edited blob…
            {"source_field": "Sentence", "beep_hz": "auto"},
            # …and the one the picker itself can write: Number("1e999") is Infinity, which
            # JSON.stringify serialises as null.
            {"source_field": "Sentence", "beep_gain_db": None},
        ],
    )
    def test_unreadable_params_fail_the_field_terminally(self, params):
        # parse_params runs INSIDE the pipeline's attempt guard but BEFORE run(), so the base
        # "one failed attempt, carry on" would walk straight past the tool's whole purpose on
        # any chain that still had somewhere to fall to.
        rule = _rule(tools=(CompiledToolSpec(name="cloze_audio", params=params),))
        voice = _FakeWavTTS()

        result = GenerationPipeline(_ctx(voice)).run(
            rule, {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."}
        )

        assert result.produced is None
        assert [(a.tool, a.status) for a in result.attempts] == [
            ("cloze_audio", "error")
        ]
        assert isinstance(result.attempts[0].error, ToolError)
        assert voice.spoken == []
        assert result.errored is True

    def test_a_recoverable_tool_still_falls_through(self):
        # The halt must be surgical: an ordinary error keeps the old fall-through behaviour.
        rule = _rule(
            kind="text",
            tools=(
                CompiledToolSpec(name="cloze_audio"),  # wrong kind for a text field
                CompiledToolSpec(name="ai"),
            ),
        )
        result = GenerationPipeline(_ctx()).run(rule, {"Word": "cat"})
        assert [a.status for a in result.attempts] == ["wrong_kind", "produced"]

    def test_no_chain_this_build_writes_itself_speaks_after_cloze_audio(self):
        # Graft #1's other half. A chain reaches config from exactly two places: the user's
        # picker (which now warns) and default_tool_chain(). The default must never be the
        # leaking order, because a device WITHOUT cloze_audio degrades that entry to
        # "unknown tool" and hands the sentence to the next voice — the one residual this
        # feature cannot close from here.
        assert [spec.name for spec in default_tool_chain()] == ["ai"]
        compiled = compile_field_rule(
            SmartNotesFieldConfig(field="Sentence (audio)", enabled=True, type="tts"),
            "Word",
        )
        assert [spec.name for spec in compiled.tools] == ["ai"]

    @pytest.mark.parametrize(
        "sentence", ["", "   ", "&nbsp;", "<br>", "[sound:x.mp3]", "<b></b>"]
    )
    def test_a_source_with_nothing_speakable_is_terminal_not_a_decline(self, sentence):
        # "There is no sentence here, so there is no answer to leak" reasons about the wrong
        # field: the next tool speaks the RULE's refs, not this tool's source_field. Every
        # value here is everyday Anki field content and NON-blank, so the hard-prerequisite
        # gate lets the field through — and the answer sitting in Hint gets read out the
        # moment this tool declines. It cannot know what a later tool would say, so it never
        # declines at all.
        with pytest.raises(ToolError, match="nothing to speak"):
            _run(
                {
                    "Word": "cat",
                    "Sentence": sentence,
                    "Hint": "The cat {{c1::sat}} down.",
                },
                prompt="{{Sentence}} {{Hint}}",
                params={"source_field": "Sentence"},
            )

    def test_it_never_declines_even_with_no_params_at_all(self):
        # The reported repro: no explicit params, so source_field defaults to the prompt's
        # first ref ("Sentence") while `ai` would speak both refs.
        with pytest.raises(ToolError):
            _run(
                {"Word": "cat", "Sentence": "&nbsp;", "Hint": "The cat sat down."},
                prompt="{{Sentence}} {{Hint}}",
            )


class TestSplice:
    """Frame math at 22050 Hz mono, the format the bundled piper voice emits."""

    def _clip(self, **kwargs) -> WavClip:
        return _produced_clip(
            {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
            params={"source_field": "Sentence", **kwargs},
        )

    def test_the_gap_is_the_measured_length_of_the_word(self):
        clip = self._clip()
        spoken = len("The cat") + len("down.")
        assert clip.frame_count == (spoken + len("sat")) * _FRAMES_PER_CHAR
        assert clip.params == (_CHANNELS, SAMPLE_WIDTH, _RATE)

    def test_a_longer_word_makes_a_longer_gap(self):
        short = self._clip()
        long_clip = _produced_clip(
            {"Word": "x", "Sentence": "The cat {{c1::hesitated}} down."},
            params={"source_field": "Sentence"},
        )
        assert (
            long_clip.frame_count - short.frame_count
            == (len("hesitated") - len("sat")) * _FRAMES_PER_CHAR
        )

    def test_beep_mode_fills_the_gap_with_a_tone(self):
        clip = self._clip(mode="beep")
        start = len("The cat") * _FRAMES_PER_CHAR
        gap = clip.samples()[start : start + len("sat") * _FRAMES_PER_CHAR]
        assert set(gap) != {0}
        # -3 dB of full scale, so it never clips and never deafens.
        assert max(abs(sample) for sample in gap) <= int(32767 * 10 ** (-3.0 / 20))

    def test_the_beep_is_quieter_when_configured_so(self):
        loud = self._clip(mode="beep", beep_gain_db=-3.0)
        quiet = self._clip(mode="beep", beep_gain_db=-30.0)
        peak = lambda clip: max(abs(s) for s in clip.samples())  # noqa: E731
        assert peak(quiet) < peak(loud)

    def test_splice_joins_are_faded(self):
        clip = self._clip()
        samples = clip.samples()
        # The fade ramps the first sample of each spoken segment to zero; without it the step
        # from silence to a full-amplitude sample clicks.
        assert samples[0] == 0
        assert samples[-1] == 0

    def test_a_leading_hole_produces_no_empty_synthesis_call(self):
        voice = _FakeWavTTS()
        clip = _produced_clip(
            {"Word": "x", "Sentence": "{{c1::Cats}} purr."},
            tts=voice,
            params={"source_field": "Sentence"},
        )
        assert voice.spoken == ["Cats", "purr."]  # the empty prefix is never sent
        assert clip.frame_count == (len("Cats") + len("purr.")) * _FRAMES_PER_CHAR

    def test_a_frame_rate_mismatch_between_segments_is_refused(self):
        class _Drifting(_FakeWavTTS):
            def synthesize(self, text, *, lang=None, voice=None):
                # A provider that changes rate mid-sentence would splice chipmunk speech in.
                self.rate = 24000 if self.spoken else _RATE
                return super().synthesize(text, lang=lang, voice=voice)

        with pytest.raises(ToolError, match="two different formats"):
            _run(
                {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
                tts=_Drifting(),
                params={"source_field": "Sentence"},
            )

    def test_non_pcm_audio_from_a_wav_provider_is_refused(self):
        class _Lying(_FakeWavTTS):
            def synthesize(self, text, *, lang=None, voice=None):
                return b"not a wav at all"

        with pytest.raises(ToolError, match="cannot cut"):
            _run(
                {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
                tts=_Lying(),
                params={"source_field": "Sentence"},
            )

    def test_eight_bit_audio_is_refused(self):
        class _EightBit(_FakeWavTTS):
            def synthesize(self, text, *, lang=None, voice=None):
                import io

                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(1)
                    writer.setframerate(_RATE)
                    writer.writeframes(b"\x40" * 100)
                return buffer.getvalue()

        with pytest.raises(ToolError, match="16-bit"):
            _run(
                {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
                tts=_EightBit(),
                params={"source_field": "Sentence"},
            )

    def test_a_sentence_with_nothing_in_it_produces_no_audio(self):
        # MaskedSpeech's shape permits one empty segment and no hidden word; ClozeMaskPlanner
        # never builds one, but the builder takes a MaskedSpeech from anywhere and must fail
        # rather than write an empty media file into the note.
        builder = MaskedAudioBuilder(
            ResolvedVoice(_FakeWavTTS(), None, "fake-voice"), WavCodec()
        )

        with pytest.raises(ToolError, match="produced no audio"):
            builder.build(MaskedSpeech(("",), ()))

    def test_stereo_audio_splices_when_every_segment_agrees(self):
        clip = _produced_clip(
            {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
            tts=_FakeWavTTS(channels=2),
            params={"source_field": "Sentence"},
        )
        assert clip.nchannels == 2
        assert clip.frame_count == len("The catsatdown.") * _FRAMES_PER_CHAR


class TestCodecSelection:
    """``strategy`` picks how the bytes are cut; ``auto`` follows the voice's format."""

    def test_auto_uses_the_stdlib_for_a_wav_voice(self):
        assert isinstance(
            ClozeAudioTool._codec("auto", "wav", _FakeSidecar()), WavCodec
        )

    def test_auto_uses_the_sidecar_for_an_mp3_voice(self):
        assert isinstance(
            ClozeAudioTool._codec("auto", "mp3", _FakeSidecar()), SidecarCodec
        )

    def test_segments_forces_the_stdlib_even_for_an_mp3_voice(self):
        # The escape hatch for a user who wants no runtime installed: the splice is attempted
        # with the stdlib and fails loudly if the bytes are not PCM (never silently).
        assert isinstance(
            ClozeAudioTool._codec("segments", "mp3", _FakeSidecar()), WavCodec
        )

    def test_an_unknown_strategy_degrades_to_auto(self):
        # ADR-010: a value a newer release added must not break this one.
        assert isinstance(
            ClozeAudioTool._codec("alignment", "wav", _FakeSidecar()), WavCodec
        )

    def test_the_codec_drives_the_contexts_runtime_not_a_process_wide_one(self):
        # The sidecar is injected through the context (DIP), so a test — and a second Anki
        # profile — never depends on the process-wide runtime manager.
        sidecar = _FakeSidecar()
        codec = ClozeAudioTool._codec("sidecar", "wav", sidecar)
        assert codec._sidecar is sidecar

    def test_audio_the_runtime_cannot_decode_is_refused(self):
        # The runtime answered, but not with PCM (a codec version that returns something else,
        # a truncated pipe). Cutting is impossible, so it fails like every other unmaskable
        # case rather than handing the sentence on.
        class _JunkSidecar(_FakeSidecar):
            def decode(self, data: bytes) -> bytes:
                return b"not a wav at all"

        with pytest.raises(ToolError, match="the audio runtime returned"):
            _run(
                {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
                tts=_FakeMp3TTS(),
                audio=_JunkSidecar(),
                params={"source_field": "Sentence"},
            )

    def test_an_mp3_voice_with_the_codec_installed_produces_an_mp3(self):
        voice = _FakeMp3TTS()
        outcome = _run(
            {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
            tts=voice,
            audio=_FakeSidecar(),
            params={"source_field": "Sentence"},
        )
        assert isinstance(outcome, Produced), outcome
        assert outcome.result.ext == "mp3"
        # The fake codec wraps the spliced WAV, so the splice can still be inspected.
        assert outcome.result.data.startswith(b"MP3<")
        clip = WavClip.from_bytes(outcome.result.data[len(b"MP3<") : -1])
        assert _decoded_text(clip) == "The catdown."
        assert clip.frame_count == len("The catsatdown.") * _FRAMES_PER_CHAR


class TestFieldDefaults:
    """Which fields are read when the params are blank."""

    def test_the_source_defaults_to_the_rules_first_prompt_ref(self):
        clip = _produced_clip(
            {"Word": "sat", "Sentence": "The cat {{c1::sat}} down."},
            prompt="{{Sentence}}",
        )
        assert _decoded_text(clip) == "The catdown."

    def test_the_word_defaults_to_the_base_field(self):
        clip = _produced_clip(
            {"Word": "survive", "Sentence": "She survived it."},
            params={"source_field": "Sentence"},
        )
        assert _decoded_text(clip) == "Sheit."

    def test_field_names_match_case_insensitively(self):
        clip = _produced_clip(
            {"Word": "sat", "sentence": "The cat {{c1::sat}} down."},
            params={"source_field": "Sentence"},
        )
        assert _decoded_text(clip) == "The catdown."


class TestMarkupSplittingAWordStillHidesIt:
    """Every occurrence is hidden, including one an inline tag cuts in half.

    The shared matcher DISCARDS a hit that reads across a tag — right for the text ``cloze``
    tool, which edits the original markup and would otherwise wrap half a word, and fatal here:
    the occurrence would simply go unmasked and ``strip_markup`` re-joins the pieces before the
    voice reads them. Refusing the sentence would be safe but would lose the feature on ordinary
    deck content (bold, pasted colour spans, soft hyphens), so the word path matches on the text
    that will actually be SPOKEN, where the tag has already vanished. See ADR-011.
    """

    @pytest.mark.parametrize(
        "sentence",
        [
            "She survived the winter. She sur<b>vived</b> again.",
            "She survived. She surviv<b>ed</b> again.",
            'She survived. <span style="color:red">sur</span>vived again.',
            "She survived. She sur&shy;vived.",
            "She survived. She <b><i>survived</i></b> again.",
        ],
    )
    def test_both_occurrences_are_hidden(self, sentence):
        plan = ClozeMaskPlanner("survive").plan(sentence)

        assert plan is not None, "the sentence must produce, not refuse"
        assert len(plan.hidden) == 2
        assert not any("surviv" in segment.lower() for segment in plan.segments)

    def test_the_marker_path_still_reads_the_raw_value(self):
        # Markers must be located BEFORE stripping: strip_markup unwraps a cloze to its answer,
        # which would destroy the very positions the marker path needs.
        plan = ClozeMaskPlanner("").plan("The cat {{c1::sat}} down.")

        assert plan is not None
        assert list(plan.hidden) == ["sat"]
        assert not any("sat" in segment for segment in plan.segments)
