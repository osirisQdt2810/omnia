"""The ``cloze_audio`` tool: speak a sentence with the answer replaced by silence or a beep.

A listening-cloze card is a sentence you hear with a hole in it. Plain TTS cannot make one:
:func:`omnia.core.text.strip_markup` unwraps ``{{c1::survive}}`` to ``survive`` before the text
reaches a provider, so pointing a TTS field at a cloze field produces audio that **reads the
answer out loud**. This tool exists to close that hole, and its whole design follows from one
rule:

    **The answer is never spoken. Ever.**

Three consequences, each visible in the code below:

1. **Failure is terminal, not a fall-through.** Every other tool that cannot do its job hands
   the field to the next one; here that would hand a cloze sentence to plain TTS, which speaks
   the answer. So an unmaskable field raises
   :class:`~omnia.plugins.smart_notes.engine.tools.base.TerminalToolError` and the chain STOPS
   — a ``[cloze_audio, ai]`` chain fails the field instead of ruining the card. The one thing
   that still declines softly is an EMPTY source: there is no sentence, so there is no answer
   to leak.
2. **The spans are found in the RAW value.** Stripping the markup first would unwrap the very
   cloze markers that say what to hide. A source that already carries ``{{cN::…}}`` (the
   natural chain: a "Definition (cloze)" text field feeding a "Definition (cloze audio)" sound
   field) masks exactly those; otherwise the word is located with
   :class:`~omnia.plugins.smart_notes.engine.tools.cloze.ClozeRewriter`, the same matcher the
   ``cloze`` tool uses, so both tools agree about what counts as the word.
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

from pydantic import BaseModel, Field

from omnia.core.audio.sidecar import AudioSidecar
from omnia.core.audio.wav import WavClip, WavFormatError
from omnia.core.config.base import PersistedModel
from omnia.core.providers.errors import ProviderError
from omnia.core.text import CLOZE_RE, strip_markup
from omnia.plugins.smart_notes.engine.generators import GenerationResult, ResolvedVoice
from omnia.plugins.smart_notes.engine.tools.base import (
    NotApplicable,
    Produced,
    TerminalToolError,
    Tool,
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

#: The formats each codec understands, for the picker's availability line.
_WAV_PROVIDERS = "piper, viet-tts"
_MP3_PROVIDERS = "edge_tts, google_translate, google_cloud, openai"


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

    def __init__(self, word: str, *, match_word_forms: bool = True) -> None:
        """Build a planner for ``word``.

        Args:
            word: The headword to hide when the value carries no cloze markers (may be blank,
                in which case only markers can be masked).
            match_word_forms: Also hide inflected forms of the word.
        """
        self._word = word
        self._match_word_forms = match_word_forms

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
        segments: list[str] = []
        hidden: list[str] = []
        cursor = 0
        for start, end in self._spans(value):
            answer = self._spoken(value[start:end])
            if not answer:
                continue  # a degenerate "{{c1::}}" hides nothing; leave the text as it is
            segments.append(self._spoken(value[cursor:start]))
            hidden.append(answer)
            cursor = end
        if not hidden:
            return None
        segments.append(self._spoken(value[cursor:]))
        return MaskedSpeech(tuple(segments), tuple(hidden))

    def _spans(self, value: str) -> list[tuple[int, int]]:
        """Return the ``(start, end)`` offsets to hide, in document order.

        Existing cloze markers win: a field fed by the ``cloze`` tool already says exactly what
        the card hides, and honouring it keeps the text and audio cards in step. Only when
        there are none does the headword get located — with
        :class:`~omnia.plugins.smart_notes.engine.tools.cloze.ClozeRewriter`, so both tools
        share one matcher (markup projection, two-way de-inflection, the function-word filter).
        """
        marked = [(match.start(), match.end()) for match in CLOZE_RE.finditer(value)]
        if marked:
            return marked
        if not self._word:
            return []
        rewriter = ClozeRewriter(
            self._word, match_word_forms=self._match_word_forms, separate_cards=False
        )
        return [(start, end) for start, end, _ in rewriter.occurrences(value)]

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
            TerminalToolError: If the bytes cannot be turned into PCM. Terminal because the
                alternative is a later tool speaking the answer.
        """

    @abstractmethod
    def encode(self, clip: WavClip) -> bytes:
        """Return ``clip`` as the bytes of a playable media file.

        Raises:
            TerminalToolError: If the clip cannot be written.
        """


class WavCodec(SpeechCodec):
    """The zero-install codec: the provider already speaks 16-bit PCM (piper, viet-tts)."""

    ext: ClassVar[str] = "wav"

    def decode(self, data: bytes) -> WavClip:
        try:
            return WavClip.from_bytes(data)
        except WavFormatError as exc:
            raise TerminalToolError(
                f"cloze_audio cannot cut this voice's audio ({exc}). Use a WAV voice "
                f"({_WAV_PROVIDERS}), or install the audio runtime in Smart Notes → "
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

    def __init__(self, sidecar: Optional[AudioSidecar] = None) -> None:
        """Initialise the codec.

        Args:
            sidecar: The runtime wrapper to drive (injected in tests; defaults to a real one).
        """
        self._sidecar = sidecar or AudioSidecar()

    def decode(self, data: bytes) -> WavClip:
        wav = self._run(lambda: self._sidecar.decode(data), "decode")
        try:
            return WavClip.from_bytes(wav)
        except WavFormatError as exc:
            raise TerminalToolError(
                f"the audio runtime returned audio cloze_audio cannot cut ({exc})."
            ) from exc

    def encode(self, clip: WavClip) -> bytes:
        return self._run(lambda: self._sidecar.encode(clip.to_bytes()), "encode")

    @staticmethod
    def _run(call: Callable[[], bytes], what: str) -> bytes:
        """Run one sidecar call, turning any provider-level failure into a terminal one."""
        try:
            return call()
        except ProviderError as exc:
            raise TerminalToolError(
                f"cloze_audio needs the audio runtime to {what} this voice's audio "
                f"({_MP3_PROVIDERS} return MP3, which Anki's Python cannot open): {exc}"
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
            TerminalToolError: If a segment cannot be synthesized, the pieces disagree about
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
            raise TerminalToolError(
                "cloze_audio produced no audio for this sentence (the voice returned nothing)."
            )
        self._verify_params(pieces)
        return self._codec.encode(WavClip.concat(pieces))

    def _speak(self, text: str) -> WavClip:
        """Synthesize one fragment and decode it to PCM."""
        try:
            data = self._voice.synthesize(text)
        except ProviderError as exc:
            # Terminal, not a fall-through: the next tool would speak the whole sentence.
            raise TerminalToolError(
                f"cloze_audio could not synthesize a part: {exc}"
            ) from exc
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
            TerminalToolError: On the first piece whose parameters differ from the first one's.
        """
        head = pieces[0].params
        for piece in pieces[1:]:
            if piece.params != head:
                raise TerminalToolError(
                    f"cloze_audio got audio in two different formats from one voice "
                    f"({head} and {piece.params}), which cannot be spliced."
                )


@register_tool("cloze_audio")
class ClozeAudioTool(Tool):
    """Speaks a sentence with the answer replaced by silence or a beep — never aloud."""

    name: ClassVar[str] = "cloze_audio"
    label: ClassVar[str] = "Cloze audio"
    description: ClassVar[str] = (
        "Speak a sentence with the cloze answer replaced by silence or a beep. Fails "
        "(never falls through) when it cannot hide the answer, so no tool can read it out."
    )
    kinds: ClassVar[frozenset[str]] = frozenset({"tts"})
    # It calls TTS, never an LLM: the text is the note's own, so there is nothing to think up.
    deterministic: ClassVar[bool] = True
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
        """Say which voices this tool can serve on THIS machine right now.

        Never None while the codec runtime is missing, even though the tool works perfectly
        with a WAV voice: the picker's job is to warn before a chain is saved, and "it depends
        which voice the field resolves to" is the honest answer.
        """
        if AudioSidecar().is_installed():
            return None
        return (
            f"works with WAV voices ({_WAV_PROVIDERS}) now; MP3 voices "
            f"({_MP3_PROVIDERS}) need the audio runtime — install it in Options → Advanced"
        )

    def run(self, request: ToolRequest, ctx: ToolContext) -> ToolOutcome:
        """Speak this rule's sentence with its answer masked.

        Declines only when there is nothing to speak. Once there IS a sentence with something
        to hide in it, EVERY failure — no cloze span and no word match, an unconfigured voice,
        audio that cannot be cut, the missing codec runtime — raises
        :class:`~omnia.plugins.smart_notes.engine.tools.base.TerminalToolError`, so the chain
        stops instead of handing the sentence to a tool that would read the answer aloud.

        Raises:
            TerminalToolError: When the answer cannot be masked.
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
            # No sentence at all, so there is no answer to leak: this is the one safe decline.
            return NotApplicable(
                f"nothing to speak — {source_field or 'the source field'} is empty"
            )
        word = strip_markup(
            field_value(request.fields, word_field), keep_line_breaks=False
        ).strip()
        speech = ClozeMaskPlanner(word).plan(source)
        if speech is None:
            raise TerminalToolError(
                f"cloze_audio found nothing to hide in {source_field!r}: it carries no "
                f"{{{{c1::…}}}} marker and {word or 'the word field'} does not occur in it. "
                "Refusing to speak the sentence, which would give the answer away."
            )
        # PAST THIS LINE THERE IS AN ANSWER TO PROTECT, so every remaining failure is terminal —
        # including ones raised by code this tool merely calls (an unconfigured Auto-detect
        # voice, say, which is an ordinary ProviderError and would otherwise fall through to a
        # tool that speaks the sentence). Guarding the phase rather than each call is what makes
        # the guarantee hold for code added here later.
        try:
            return self._produce(speech, request, ctx)
        except TerminalToolError:
            raise
        except Exception as exc:
            raise TerminalToolError(
                f"cloze_audio could not mask the answer in {source_field!r} ({exc}). "
                "Refusing to fall through to a tool that would speak it."
            ) from exc

    def _produce(
        self, speech: MaskedSpeech, request: ToolRequest, ctx: ToolContext
    ) -> ToolOutcome:
        """Resolve the voice, pick the codec, and build the masked clip."""
        params = request.params
        voice = ResolvedVoice.for_rule(
            ctx.providers, ctx.detector, request.rule, speech.detection_text
        )
        codec = self._codec(str(params.get("strategy", "") or ""), voice.audio_ext)
        data = MaskedAudioBuilder(
            voice,
            codec,
            mode=str(params.get("mode", MODE_SILENCE) or MODE_SILENCE),
            beep_hz=int(params.get("beep_hz", 1000) or 1000),
            beep_gain_db=float(params.get("beep_gain_db", -3.0)),
        ).build(speech)
        return Produced(GenerationResult("tts", data=data, ext=codec.ext))

    @staticmethod
    def _codec(strategy: str, audio_ext: str) -> SpeechCodec:
        """Pick the codec for a voice that returns ``audio_ext``.

        ``auto`` (the default, and what an unrecognised value degrades to per ADR-010) uses the
        stdlib for a WAV voice and the sidecar for anything else; the explicit values let a user
        force one — ``segments`` to keep a run install-free, ``sidecar`` to route WAV through
        PyAV as well.
        """
        if strategy == STRATEGY_SEGMENTS:
            return WavCodec()
        if strategy == STRATEGY_SIDECAR:
            return SidecarCodec()
        return WavCodec() if audio_ext == WavCodec.ext else SidecarCodec()
