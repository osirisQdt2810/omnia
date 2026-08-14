"""The ``cloze_audio`` tool: speak a sentence with the answer replaced by silence or a beep.

A listening-cloze card is a sentence you hear with a hole in it. Plain TTS cannot make one:
:func:`omnia.core.text.strip_markup` unwraps ``{{c1::survive}}`` to ``survive`` before the text
reaches a provider, so pointing a TTS field at a cloze field produces audio that **reads the
answer out loud**. This tool exists to close that hole, and its whole design follows from one
rule:

    **The answer is never spoken. Ever.**

Three consequences, each visible in the code below:

1. **This tool never DECLINES — it either masks or fails.** Other tools that cannot do their
   job return :class:`~omnia.plugins.smart_notes.engine.tools.base.NotApplicable`, which is a
   silent, expected fall-through. Here there is no such thing: every way of not producing
   raises :class:`~omnia.plugins.smart_notes.engine.tools.base.ToolError`, including the
   failure that happens before the tool even runs (params it cannot parse — see
   :meth:`ClozeAudioTool.parse_params`), so the trace always names what went wrong. There is
   deliberately no "safe decline" — not even for an empty source. The reasoning that there is
   "no answer to leak" when THIS tool's ``source_field`` holds nothing speakable is local and
   wrong: a later tool speaks the RULE's prompt refs, so a prompt of ``{{Sentence}} {{Hint}}``
   with an unspeakable ``Sentence`` (``&nbsp;``, ``<br>``, ``[sound:x.mp3]`` — all non-blank,
   so the dependency gate lets the field through) and the answer sitting in ``Hint`` leaks
   exactly as loudly.

   **What a failure does NOT do is stop the chain.** By project rule a chain runs in the
   configured order and every failure falls through to the next tool. So a field configured
   ``[cloze_audio, ai]`` whose ``cloze_audio`` fails WILL be spoken by ``ai`` — with the answer
   audible. That is the cost of the rule, and it is a configuration decision: put nothing after
   ``cloze_audio`` on a field whose answer must stay hidden. The tool's own guarantee is
   narrower and absolute — *it* never speaks the answer.
2. **The spans are found in the RAW value.** Stripping the markup first would unwrap the very
   cloze markers that say what to hide. A source that already carries ``{{cN::…}}`` (the
   natural chain: a "Definition (cloze)" text field feeding a "Definition (cloze audio)" sound
   field) masks exactly those; otherwise the word is located with
   :class:`~omnia.plugins.smart_notes.engine.tools.cloze.ClozeRewriter`, the same matcher the
   ``cloze`` tool uses, so the text card and its audio hide the same words. (Inflected forms
   are always matched — the option that used to make that configurable was removed, because
   missing an inflection here means speaking the answer.)
3. **The hidden word is measured, not estimated.** It is synthesized once and the mask is built
   to its exact frame count, so the gap lasts as long as the word would have — the invariant a
   listening cloze is built on. A guessed "~90 ms per character" gap gives the answer's length
   away and desynchronises the sentence.

Splicing is 16-bit PCM surgery (:mod:`omnia.core.audio.wav`), which covers the WAV providers
(piper, viet-tts) with nothing to install. Cloud voices return MP3, which the stdlib cannot
open, so those go through the ``audio`` native runtime
(:class:`~omnia.core.audio.sidecar.AudioSidecar`) — and while that runtime is not installed the
field fails with an actionable message rather than falling through to a voice that would read
the answer.

Pure logic — no ``aqt``/``anki`` imports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Optional

from pydantic import BaseModel, Field, ValidationError

from omnia.core.audio.sidecar import AudioSidecar
from omnia.core.audio.wav import WavClip, WavFormatError
from omnia.core.config.base import PersistedModel
from omnia.core.providers.errors import ProviderError
from omnia.core.providers.tts.registry import tts_providers_with_ext
from omnia.core.text import CLOZE_RE, strip_markup
from omnia.plugins.smart_notes.engine.generators import GenerationResult, ResolvedVoice
from omnia.plugins.smart_notes.engine.tools.base import (
    Produced,
    Tool,
    ToolError,
    ToolOutcome,
)
from omnia.plugins.smart_notes.engine.tools.cloze import (
    ClozeRewriter,
    default_source_field,
    default_word_field,
    field_value,
)
from omnia.plugins.smart_notes.engine.tools.registry import register_tool

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from omnia.plugins.smart_notes.engine.tools.base import ToolContext, ToolRequest

#: ``mode`` values: replace the answer with nothing, or with a tone.
MODE_SILENCE = "silence"
MODE_BEEP = "beep"

#: ``strategy`` values: pick the codec by the provider's format, or force one.
STRATEGY_AUTO = "auto"
STRATEGY_SEGMENTS = "segments"
STRATEGY_SIDECAR = "sidecar"

#: Length of the linear ramp applied at every splice join. Cutting speech at an arbitrary
#: sample leaves a step in the waveform, which plays back as a click.
_FADE_MS = 10.0


def _voices_named(ext: str) -> str:
    """Return the TTS providers that return ``ext``, as a comma-separated list for a message.

    Derived from the registry rather than written out, because three user-facing messages name
    these lists and a hand-kept copy is stale the moment a provider is added or renamed — the
    first copy already said "viet-tts" for a provider registered as ``viettts``.
    """
    return ", ".join(tts_providers_with_ext(ext))


class ClozeAudioParams(PersistedModel):
    """The ``cloze_audio`` tool's per-field options.

    A :class:`~omnia.core.config.base.PersistedModel` for the same reason
    :class:`~omnia.plugins.smart_notes.engine.tools.cloze.ClozeParams` is (ADR-010): these live
    in the field row's persisted chain, which syncs to devices on other Omnia releases, and an
    option a newer release added must not turn this tool into an error attempt on an older one.
    """

    source_field: str = Field(
        "",
        description=(
            "Field holding the sentence to speak. Blank = the field's first prompt "
            "reference, else the note type's base field."
        ),
    )
    word_field: str = Field(
        "",
        description=(
            "Field holding the word to hide, used only when the sentence carries no "
            "{{c1::…}} markers. Blank = the note type's base field."
        ),
    )
    mode: str = Field(
        MODE_SILENCE,
        description="Replace the hidden word with silence, or with a beep.",
        # Not a Literal: an unrecognised value must stay loadable on an older release (ADR-010)
        # and simply falls back to silence, while the picker still renders a dropdown.
        enum=[MODE_SILENCE, MODE_BEEP],
    )
    beep_hz: int = Field(1000, description="Beep frequency in Hz (mode = beep).")
    beep_gain_db: float = Field(
        -3.0,
        description=(
            "Beep loudness relative to full scale, in dB (negative is quieter)."
        ),
    )
    strategy: str = Field(
        STRATEGY_AUTO,
        description=(
            "How to cut the audio: auto picks by the voice's format, segments is the "
            "built-in WAV splice, sidecar uses the installed audio codec."
        ),
        enum=[STRATEGY_AUTO, STRATEGY_SEGMENTS, STRATEGY_SIDECAR],
    )


@dataclass(frozen=True)
class MaskedSpeech:
    """A sentence split into the parts that get spoken and the answers that must not be.

    ``segments`` always has exactly one more entry than ``hidden``: the runs of text before,
    between and after the hidden words (either end may be empty when the sentence starts or
    ends on one). Keeping them apart in ONE value object is what makes the leak impossible to
    reintroduce by accident — nothing downstream ever holds a string containing both.
    """

    segments: tuple[str, ...]
    hidden: tuple[str, ...]

    def __post_init__(self) -> None:
        """Enforce the alternating shape.

        Raises:
            ValueError: If ``segments`` is not exactly one longer than ``hidden``.
        """
        if len(self.segments) != len(self.hidden) + 1:
            raise ValueError(
                f"a masked sentence needs one more segment than hidden words, got "
                f"{len(self.segments)} and {len(self.hidden)}"
            )

    @property
    def detection_text(self) -> str:
        """The spoken parts only, for language detection — deliberately NOT the whole sentence.

        Language detection sends this text to an LLM, and the tool's entire purpose is that the
        answer never leaves the note; the surrounding words identify the language perfectly
        well, so the hidden ones are simply never joined in.
        """
        return " ".join(segment for segment in self.segments if segment).strip()


class ClozeMaskPlanner:
    """Works out which parts of a field value must not be spoken.

    Owns one decision with two sources, in priority order: the ``{{cN::…}}`` markers the value
    already carries, else the occurrences of a headword. Constructed per run from the word, so
    the (expensive) word-form derivation happens once.
    """

    def __init__(self, word: str) -> None:
        """Build a planner for ``word``.

        Args:
            word: The headword to hide when the value carries no cloze markers (may be blank,
                in which case only markers can be masked).
        """
        self._word = word

    def plan(self, value: str) -> Optional[MaskedSpeech]:
        """Split ``value`` into spoken segments and hidden words, or None when nothing matched.

        Args:
            value: The RAW field value — markup and cloze markers included. Never pass a
                stripped copy: :func:`omnia.core.text.strip_markup` unwraps a cloze to its
                answer, which destroys the very positions this needs.

        Returns:
            The split sentence, or ``None`` when there is nothing in it to hide (the caller
            turns that into a hard failure — see the module docstring).
        """
        marked = [(match.start(), match.end()) for match in CLOZE_RE.finditer(value)]
        if marked:
            # MARKER PATH — offsets into the RAW value, because strip_markup unwraps a cloze to
            # its answer and would destroy the very positions this needs.
            return self._split(value, marked, spoken=self._spoken)
        if not self._word:
            return None
        # WORD PATH — matched on the text that will actually be SPOKEN, where markup has already
        # vanished. This is what lets an occurrence a tag splits still be hidden: the shared
        # matcher deliberately DISCARDS a hit reading across a tag (right for the text `cloze`
        # tool, which must edit the original markup and would otherwise wrap half a word), and
        # here that would leave "She sur<b>vived</b>" unmasked and then speak it, because
        # strip_markup re-joins the pieces. This tool never edits markup, so it can simply look
        # at the stripped text, where the word is whole. See ADR-011.
        speech = self._spoken(value)
        spans = [
            (start, end)
            for start, end, _ in ClozeRewriter(self._word).occurrences(speech)
        ]
        return self._split(speech, spans, spoken=lambda fragment: fragment.strip())

    @staticmethod
    def _split(
        text: str,
        spans: list[tuple[int, int]],
        *,
        spoken: Callable[[str], str],
    ) -> Optional[MaskedSpeech]:
        """Cut ``text`` at ``spans`` into spoken segments and hidden answers."""
        segments: list[str] = []
        hidden: list[str] = []
        cursor = 0
        for start, end in spans:
            answer = spoken(text[start:end])
            if not answer:
                continue  # a degenerate "{{c1::}}" hides nothing; leave the text as it is
            segments.append(spoken(text[cursor:start]))
            hidden.append(answer)
            cursor = end
        if not hidden:
            return None
        segments.append(spoken(text[cursor:]))
        return MaskedSpeech(tuple(segments), tuple(hidden))

    @staticmethod
    def _spoken(fragment: str) -> str:
        """Return one slice of the raw value as the words a voice would read from it."""
        return strip_markup(fragment, keep_line_breaks=False).strip()


class SpeechCodec(ABC):
    """Turns a TTS provider's audio bytes into PCM, and the spliced PCM back into a file.

    The splice itself is format-agnostic, so this is the only thing that differs between a WAV
    voice and an MP3 one: one implementation is the stdlib, the other is the ``av`` sidecar.
    """

    #: Extension of the bytes :meth:`encode` returns (what the media file is named with).
    ext: ClassVar[str]

    @abstractmethod
    def decode(self, data: bytes) -> WavClip:
        """Return ``data`` as a 16-bit PCM clip.

        Raises:
            ToolError: If the bytes cannot be turned into PCM. It raises rather than declines because the
                alternative is a later tool speaking the answer.
        """

    @abstractmethod
    def encode(self, clip: WavClip) -> bytes:
        """Return ``clip`` as the bytes of a playable media file.

        Raises:
            ToolError: If the clip cannot be written.
        """


class WavCodec(SpeechCodec):
    """The zero-install codec: the provider already speaks 16-bit PCM (piper, viet-tts)."""

    ext: ClassVar[str] = "wav"

    def decode(self, data: bytes) -> WavClip:
        try:
            return WavClip.from_bytes(data)
        except WavFormatError as exc:
            raise ToolError(
                f"cloze_audio cannot cut this voice's audio ({exc}). Use a WAV voice "
                f"({_voices_named('wav')}), or install the audio runtime in Smart Notes → "
                "Options → Advanced to handle compressed voices."
            ) from exc

    def encode(self, clip: WavClip) -> bytes:
        return clip.to_bytes()


class SidecarCodec(SpeechCodec):
    """The managed-runtime codec: PyAV decodes/encodes out of process (ADR-005).

    Required for every cloud voice, all of which return MP3. Until the runtime is installed the
    field FAILS with the install hint — it must never quietly become plain TTS.
    """

    ext: ClassVar[str] = "mp3"

    def __init__(self, sidecar: AudioSidecar) -> None:
        """Initialise the codec.

        Args:
            sidecar: The runtime wrapper to drive (the tool passes its context's).
        """
        self._sidecar = sidecar

    def decode(self, data: bytes) -> WavClip:
        wav = self._run(lambda: self._sidecar.decode(data), "decode")
        try:
            return WavClip.from_bytes(wav)
        except WavFormatError as exc:
            raise ToolError(
                f"the audio runtime returned audio cloze_audio cannot cut ({exc})."
            ) from exc

    def encode(self, clip: WavClip) -> bytes:
        return self._run(lambda: self._sidecar.encode(clip.to_bytes()), "encode")

    @staticmethod
    def _run(call: Callable[[], bytes], what: str) -> bytes:
        """Run one sidecar call, turning any provider-level failure into a ``ToolError``."""
        try:
            return call()
        except ProviderError as exc:
            raise ToolError(
                f"cloze_audio needs the audio runtime to {what} this voice's audio "
                f"({_voices_named('mp3')} return MP3, which Anki's Python cannot open): {exc}"
            ) from exc


class MaskedAudioBuilder:
    """Synthesizes a masked sentence: speak each segment, hide each answer, splice the lot.

    Holds the resolved voice and the mask options for one field's run. Every piece of audio it
    joins comes from the SAME resolved voice, and it verifies that claim against each returned
    header rather than trusting it — a mismatch in channels, sample width or rate would splice
    chipmunk speech into the card.
    """

    def __init__(
        self,
        voice: ResolvedVoice,
        codec: SpeechCodec,
        *,
        mode: str = MODE_SILENCE,
        beep_hz: int = 1000,
        beep_gain_db: float = -3.0,
    ) -> None:
        """Initialise the builder.

        Args:
            voice: The resolved provider/voice every segment is synthesized with.
            codec: How the provider's bytes become PCM and back.
            mode: :data:`MODE_BEEP` to replace the answer with a tone; anything else is silence.
            beep_hz: Beep frequency.
            beep_gain_db: Beep attenuation in dB relative to full scale.
        """
        self._voice = voice
        self._codec = codec
        self._mode = mode
        self._beep_hz = beep_hz
        self._beep_gain_db = beep_gain_db

    def build(self, speech: MaskedSpeech) -> bytes:
        """Return the masked sentence as a playable media file.

        Args:
            speech: The split sentence to voice.

        Returns:
            The bytes of a file in the codec's format.

        Raises:
            ToolError: If a segment cannot be synthesized, the pieces disagree about
                their stream parameters, or nothing at all could be produced.
        """
        pieces: list[WavClip] = []
        for index, segment in enumerate(speech.segments):
            if segment:
                pieces.append(self._speak(segment).fade_edges(_FADE_MS))
            if index < len(speech.hidden):
                # Measure the answer by synthesizing it, then throw the audio away and keep
                # only its length: the gap must last exactly as long as the word would have.
                pieces.append(self._mask(self._speak(speech.hidden[index])))
        if not pieces:
            raise ToolError(
                "cloze_audio produced no audio for this sentence (the voice returned nothing)."
            )
        self._verify_params(pieces)
        return self._codec.encode(WavClip.concat(pieces))

    def _speak(self, text: str) -> WavClip:
        """Synthesize one fragment and decode it to PCM."""
        try:
            data = self._voice.synthesize(text)
        except ProviderError as exc:
            # Raised, never a silent decline: this tool does not hand a cloze field on quietly.
            raise ToolError(f"cloze_audio could not synthesize a part: {exc}") from exc
        return self._codec.decode(data)

    def _mask(self, measured: WavClip) -> WavClip:
        """Return the replacement for one hidden word: same length, no words in it."""
        if self._mode == MODE_BEEP:
            return WavClip.sine_beep(
                measured.duration_ms,
                self._beep_hz,
                like=measured,
                gain_db=self._beep_gain_db,
            ).fade_edges(_FADE_MS)
        return WavClip.silence(measured.duration_ms, like=measured)

    @staticmethod
    def _verify_params(pieces: list[WavClip]) -> None:
        """Fail loudly when the pieces do not share ``(channels, width, rate)``.

        :meth:`WavClip.concat` refuses a mismatch too, but only as "clip 3 has parameters …",
        which says nothing about the voice that caused it. Checking here names the real problem
        — one provider answered two calls in two formats — so the user can pin a voice.

        Raises:
            ToolError: On the first piece whose parameters differ from the first one's.
        """
        head = pieces[0].params
        for piece in pieces[1:]:
            if piece.params != head:
                raise ToolError(
                    f"cloze_audio got audio in two different formats from one voice "
                    f"({head} and {piece.params}), which cannot be spliced."
                )


@register_tool("cloze_audio")
class ClozeAudioTool(Tool):
    """Speaks a sentence with the answer replaced by silence or a beep — never aloud.

    One class-level rule carries that guarantee, and it is absolute: **it never returns**
    :class:`~omnia.plugins.smart_notes.engine.tools.base.NotApplicable`. Declining hands the
    field to a tool that speaks whatever the RULE's prompt refs hold — not this tool's
    ``source_field`` — so no local check can prove a decline is harmless. Every inability to
    produce raises :class:`~omnia.plugins.smart_notes.engine.tools.base.ToolError` instead.

    What that guarantee does NOT cover is a LATER tool: a failure falls through like any other
    (see the module docstring), so a chain that puts another tts tool after this one will have
    that tool speak the answer. Configuring the chain is the user's call, not this class's.
    """

    name: ClassVar[str] = "cloze_audio"
    label: ClassVar[str] = "Cloze audio"
    description: ClassVar[str] = (
        "Speak a sentence with the cloze answer replaced by silence or a beep. It fails "
        "rather than speak the answer — so put no other tts tool after it on this field."
    )
    kinds: ClassVar[frozenset[str]] = frozenset({"tts"})
    # It calls TTS, never an LLM: the text is the note's own, so there is nothing to think up.
    deterministic: ClassVar[bool] = True
    # ...but it DOES synthesize, with the row's voice — it speaks the sentence and the word it
    # masks. Deterministic and provider-using at once, which is why they are two flags.
    uses_provider: ClassVar[bool] = True
    # `source_field` only, deliberately — NOT `word_field`, which `cloze` does require.
    #
    # A required param becomes a HARD prerequisite (`tool_referenced_fields` →
    # `rule_prerequisites`), and a blank hard prerequisite BLOCKS the field. `word_field` is
    # read only when the source carries no ``{{cN::…}}`` marker, so requiring it would block
    # generation on every note where it happens to be empty — including the natural chain,
    # where the source already has markers and the param is never looked at.
    #
    # Nothing is risked by leaving it optional: a blank or wrong `word_field` cannot make this
    # tool speak the answer. With no marker and no match, `plan` returns None and `run` raises
    # — a failed field, not a spoken one. `source_field` is different: it decides what is read
    # at all, so a wrong guess there is worth refusing in the picker.
    required_params: ClassVar[frozenset[str]] = frozenset({"source_field"})
    params_model: ClassVar[Optional[type[BaseModel]]] = ClozeAudioParams

    @classmethod
    def referenced_fields(cls, params: Mapping[str, object]) -> list[str]:
        """Return the fields the params NAME, so they become real dependency edges.

        Only the explicitly configured names: the defaults resolve to the rule's own prompt
        refs (already prerequisites) or to the note type's base field (always present).
        """
        names = [
            str(params.get("source_field", "") or "").strip(),
            str(params.get("word_field", "") or "").strip(),
        ]
        return [name for name in names if name]

    @classmethod
    def availability(cls, ctx: ToolContext) -> str | None:
        """Name what a voice on THIS machine might still need — advice, never a gate.

        The tool is fully usable with nothing installed: a WAV voice (the bundled piper one
        included) splices with the stdlib. Only an MP3 voice needs the codec runtime, and which
        voice a FIELD uses is a per-row setting this classmethod cannot see — so a global "no"
        would be wrong in both directions. The picker renders this next to the tool without
        disabling it (see :meth:`Tool.availability`).
        """
        if ctx.audio.is_installed():
            return None
        return (
            f"WAV voices ({_voices_named('wav')}) work as-is; MP3 voices "
            f"({_voices_named('mp3')}) need the audio runtime — install it in "
            "Options → Advanced"
        )

    @classmethod
    def parse_params(cls, params: Mapping[str, object]) -> dict[str, object]:
        """Validate the stored params, raising rather than letting the default swallow it.

        The pipeline calls this INSIDE the attempt guard but BEFORE :meth:`run`, so the base
        implementation's "an unparsable params dict is one failed attempt, carry on" would walk
        straight past this tool's whole reason to exist: ``[cloze_audio, ai]`` with a
        ``beep_hz`` of ``"auto"`` — a value a newer release could give the key (ADR-010), or a
        hand-edited blob, or the picker's own ``Number("1e999")`` → ``Infinity`` → ``null`` —
        would fall through to ``ai``, which speaks the sentence with the answer in it.

        Raises:
            ToolError: If the params do not satisfy :class:`ClozeAudioParams`.
        """
        try:
            return super().parse_params(params)
        except ValidationError as exc:
            raise ToolError(
                f"cloze_audio cannot read its own settings ({exc}), so it cannot know how to "
                "hide the answer, so it produced nothing. Fix the tool's options on this field — and "
                "note that any tts tool AFTER it in the chain will now speak this field."
            ) from exc

    def run(self, request: ToolRequest, ctx: ToolContext) -> ToolOutcome:
        """Speak this rule's sentence with its answer masked, or fail the field.

        It never declines. EVERY failure — a source with nothing speakable in it, no cloze span
        and no word match, an unconfigured voice, audio that cannot be cut, the missing codec
        runtime — raises
        :class:`~omnia.plugins.smart_notes.engine.tools.base.ToolError` rather than declining,
        so the trace always names what went wrong. It does NOT stop the chain: by project rule
        a failure falls through to the next tool, so a tts tool ordered after this one will
        speak the field, answer included. This tool's guarantee is narrower and absolute —
        *it* never speaks the answer.

        Raises:
            ToolError: When the answer cannot be masked.
        """
        rule = request.rule
        params = request.params
        source_field = str(params.get("source_field", "") or "").strip() or (
            default_source_field(rule)
        )
        word_field = str(params.get("word_field", "") or "").strip() or (
            default_word_field(rule)
        )
        source = field_value(request.fields, source_field)
        if not strip_markup(source).strip():
            # NOT a decline. "There is no sentence here, so there is nothing to leak" reasons
            # about the wrong field: the next tool speaks the RULE's prompt refs, and a source
            # holding only "&nbsp;" while a Hint ref holds "The cat {{c1::sat}} down." is both
            # unspeakable HERE and a leak THERE.
            raise ToolError(
                "cloze_audio has nothing to speak in "
                f"{source_field or 'the source field'}, so it produced nothing. It never speaks a "
                "field it cannot mask; a tts tool after it in the chain would speak this "
                "field's other sources, answers included."
            )
        word = strip_markup(
            field_value(request.fields, word_field), keep_line_breaks=False
        ).strip()
        speech = ClozeMaskPlanner(word).plan(source)
        if speech is None:
            raise ToolError(
                f"cloze_audio found nothing to hide in {source_field!r}: it carries no "
                f"{{{{c1::…}}}} marker and {word or 'the word field'} does not occur in it. "
                "It produced nothing rather than speak the sentence with the answer in it; a tts "
                "tool after it in the chain would speak it."
            )
        # PAST THIS LINE THERE IS AN ANSWER TO PROTECT, so every remaining failure RAISES —
        # including ones raised by code this tool merely calls (an unconfigured Auto-detect
        # voice, say, which is an ordinary ProviderError and would otherwise fall through to a
        # tool that speaks the sentence). Guarding the phase rather than each call is what makes
        # the guarantee hold for code added here later.
        try:
            return self._produce(speech, request, ctx)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(
                f"cloze_audio could not mask the answer in {source_field!r} ({exc}), so it produced "
                "nothing. A tts tool after it in the chain would speak this field."
            ) from exc

    def _produce(
        self, speech: MaskedSpeech, request: ToolRequest, ctx: ToolContext
    ) -> ToolOutcome:
        """Resolve the voice, pick the codec, and build the masked clip."""
        params = request.params
        voice = ResolvedVoice.for_rule(
            ctx.providers, ctx.detector, request.rule, speech.detection_text
        )
        codec = self._codec(
            str(params.get("strategy", "") or ""), voice.audio_ext, ctx.audio
        )
        data = MaskedAudioBuilder(
            voice,
            codec,
            mode=str(params.get("mode", MODE_SILENCE) or MODE_SILENCE),
            beep_hz=int(params.get("beep_hz", 1000) or 1000),
            beep_gain_db=float(params.get("beep_gain_db", -3.0)),
        ).build(speech)
        return Produced(GenerationResult("tts", data=data, ext=codec.ext))

    @staticmethod
    def _codec(strategy: str, audio_ext: str, audio: AudioSidecar) -> SpeechCodec:
        """Pick the codec for a voice that returns ``audio_ext``.

        ``auto`` (the default, and what an unrecognised value degrades to per ADR-010) uses the
        stdlib for a WAV voice and the sidecar for anything else; the explicit values let a user
        force one — ``segments`` to keep a run install-free, ``sidecar`` to route WAV through
        PyAV as well.

        Args:
            strategy: The field's ``strategy`` param.
            audio_ext: The format the resolved voice returns.
            audio: The context's codec runtime, handed to the sidecar codec.
        """
        if strategy == STRATEGY_SEGMENTS:
            return WavCodec()
        if strategy == STRATEGY_SIDECAR:
            return SidecarCodec(audio)
        return WavCodec() if audio_ext == WavCodec.ext else SidecarCodec(audio)
