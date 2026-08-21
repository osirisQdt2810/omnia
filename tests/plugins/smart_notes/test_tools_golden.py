"""Golden behaviour lock for the smart-notes tool pipeline (tools phase 1).

Every assertion in this file was written against — and verified green on — the PRE-refactor
:class:`GenerationService` (one :class:`Generator` strategy per kind, dispatched from a dict),
then re-run UNCHANGED against the ``GenerationPipeline``. It uses only the public service API
(:meth:`GenerationService.generate` / :meth:`GenerationService.generate_note`) plus
deterministic fakes, so it is the byte-for-byte contract that a field with no configured tools
— i.e. every config that exists today, which compiles to the single legacy ``"ai"`` tool —
still generates exactly what it generated before the seam landed: same text (through the same
Markdown conversion), same bytes, same extension, same chaining, same skip/block/failure
bookkeeping, same run order, and the same error message on a provider failure.
"""

from __future__ import annotations

import pytest
from conftest import FakeLLMProvider, FakeTTSProvider

from omnia.core.providers import ProviderError
from omnia.plugins.smart_notes.config import (
    SmartNotesFieldConfig,
    SmartNotesFieldRule,
    SmartNotesNoteTypeConfig,
)
from omnia.plugins.smart_notes.engine import GenerationService


class _MarkdownLLM(FakeLLMProvider):
    """Emits Markdown + a newline so the golden text locks the Markdown→HTML conversion."""

    def generate_text(self, prompt, *, system=None, temperature=0.7, max_tokens=None):
        return f"**{prompt}**\nsecond line"

    def generate_image(self, prompt, *, size="1024x1024"):
        return f"IMG<{prompt}>".encode()


class _EchoLLM(FakeLLMProvider):
    """Echoes the prompt (so chaining is visible) and explodes on a ``boom`` prompt."""

    def generate_text(self, prompt, *, system=None, temperature=0.7, max_tokens=None):
        if prompt.startswith("boom"):
            raise ProviderError(f"provider exploded on {prompt!r}")
        return f"gen:{prompt}"

    def generate_image(self, prompt, *, size="1024x1024"):
        return f"IMG<{prompt}>".encode()


class _EchoTTS(FakeTTSProvider):
    """Encodes the spoken text + resolved language/voice into the audio bytes."""

    def synthesize(self, text, *, lang=None, voice=None):
        return f"AUDIO<{text}|{lang}|{voice}>".encode()


class _StubHub:
    """A minimal ProviderHub-shaped stub (llm / tts / resolve_auto_voice)."""

    def __init__(self, *, llm=None, tts=None, auto_voices=None):
        self._llm = llm
        self._tts = tts
        self._auto_voices = auto_voices or {}

    def llm(self, *, model: str = "", image_model: str = "", provider: str = ""):
        return self._llm

    def tts(self, *, provider: str = ""):
        return self._tts

    def resolve_auto_voice(self, lang: str, *, reason: str = ""):
        if lang not in self._auto_voices:
            raise ProviderError(f"No Auto-detect voice set for language {lang!r}")
        return self._auto_voices[lang]


def _hub(*, llm=None, tts=None):
    return _StubHub(
        llm=llm or _EchoLLM(),
        tts=tts or _EchoTTS(),
        auto_voices={"en": ("fake", "en-voice")},
    )


def _config(fields):
    return SmartNotesNoteTypeConfig(
        note_type="Basic",
        base_field="Word",
        fields=[SmartNotesFieldConfig(field=name, **kw) for name, kw in fields],
    )


class TestGenerateGolden:
    """One rule at a time: the exact :class:`GenerationResult` the legacy path produced."""

    def test_text_rule_output_is_unchanged(self):
        service = GenerationService(_hub(llm=_MarkdownLLM()))
        rule = SmartNotesFieldRule(
            kind="text", prompt="define {{Word}}", target_field="Def"
        )
        result = service.generate(rule, {"Word": "cat"})
        assert result.kind == "text"
        assert result.text == "<strong>define cat</strong><br>second line"
        assert result.data is None
        assert result.ext == ""

    def test_image_rule_output_is_unchanged(self):
        service = GenerationService(_hub())
        rule = SmartNotesFieldRule(
            kind="image", prompt="draw {{Word}}", target_field="Pic"
        )
        result = service.generate(rule, {"Word": "cat"})
        assert result.kind == "image"
        assert result.data == b"IMG<draw cat>"
        assert result.ext == "png"
        assert result.text is None

    def test_tts_rule_with_pinned_voice_is_unchanged(self):
        service = GenerationService(_hub())
        rule = SmartNotesFieldRule(
            kind="tts", prompt="say {{Word}}", target_field="Audio", voice="pinned"
        )
        result = service.generate(rule, {"Word": "cat"})
        assert result.kind == "tts"
        # A pinned voice speaks only the prompt's refs, with lang=None.
        assert result.data == b"AUDIO<cat|None|pinned>"
        assert result.ext == "mp3"

    def test_tts_rule_on_auto_detect_is_unchanged(self):
        service = GenerationService(_hub())
        rule = SmartNotesFieldRule(
            kind="tts", source_field="Word", target_field="Audio", language="en"
        )
        result = service.generate(rule, {"Word": "cat"})
        assert result.data == b"AUDIO<cat|en|en-voice>"
        assert result.ext == "mp3"

    def test_provider_failure_surfaces_the_provider_message(self):
        service = GenerationService(_hub())
        rule = SmartNotesFieldRule(
            kind="text", prompt="boom {{Word}}", target_field="Def"
        )
        with pytest.raises(ProviderError) as excinfo:
            service.generate(rule, {"Word": "cat"})
        assert str(excinfo.value) == "provider exploded on 'boom cat'"


class TestGenerateNoteGolden:
    """A whole note: run order, chaining, media handling, skip/block/failure bookkeeping."""

    def test_full_note_shape_is_unchanged(self):
        service = GenerationService(_hub())
        config = _config(
            [
                ("Word", dict(enabled=True, type="text", prompt="ignored (base)")),
                ("Usage", dict(enabled=True, type="text", prompt="use {{Def}}")),
                ("Def", dict(enabled=True, type="text", prompt="define {{Word}}")),
                ("Pic", dict(enabled=True, type="image", prompt="draw {{Word}}")),
                (
                    "Audio",
                    dict(
                        enabled=True, type="tts", prompt="say {{Word}}", language="en"
                    ),
                ),
                ("Filled", dict(enabled=True, type="text", prompt="fill {{Word}}")),
                ("Boom", dict(enabled=True, type="text", prompt="boom {{Word}}")),
                ("Dependent", dict(enabled=True, type="text", prompt="need {{Boom}}")),
                ("Off", dict(enabled=False, type="text", prompt="never {{Word}}")),
            ]
        )
        fields = {
            "Word": "cat",
            "Usage": "",
            "Def": "",
            "Pic": "",
            "Audio": "",
            "Filled": "already here",
            "Boom": "",
            "Dependent": "",
            "Off": "",
        }
        results, blocked, failed = service.generate_note(config, fields)

        # Run order: config order, with Def pulled ahead of the Usage that references it.
        assert [rule.target_field for rule, _ in results] == [
            "Def",
            "Usage",
            "Pic",
            "Audio",
        ]
        produced = {rule.target_field: result for rule, result in results}
        assert produced["Def"].text == "gen:define cat"
        # Text chains: Usage's prompt saw Def's freshly generated value.
        assert produced["Usage"].text == "gen:use gen:define cat"
        assert produced["Pic"].data == b"IMG<draw cat>"
        assert produced["Pic"].ext == "png"
        assert produced["Audio"].data == b"AUDIO<cat|en|en-voice>"
        assert produced["Audio"].ext == "mp3"

        # Boom's provider raised → isolated as a failure carrying the provider's message.
        assert [item.field for item in failed] == ["Boom"]
        assert failed[0].error == "provider exploded on 'boom cat'"
        # …and its hard dependent blocks transitively; the note is not aborted.
        assert [(item.target_field, item.missing) for item in blocked] == [
            ("Dependent", ["Boom"])
        ]
        # The caller's field map is never mutated.
        assert fields["Def"] == ""

    def test_media_results_do_not_chain_into_a_downstream_prompt(self):
        service = GenerationService(_hub())
        config = _config(
            [
                (
                    "Audio",
                    dict(
                        enabled=True, type="tts", prompt="say {{Word}}", language="en"
                    ),
                ),
                ("Caption", dict(enabled=True, type="text", prompt="about {{Audio}}")),
            ]
        )
        results, blocked, failed = service.generate_note(
            config, {"Word": "cat", "Audio": "", "Caption": ""}
        )
        assert blocked == []
        assert failed == []
        # Audio produced, so Caption is NOT blocked (``produced`` satisfies the hard gate) — but
        # the embed ref is never chained into the working map, so Caption's only source reads
        # blank and the skip predicate drops it silently: no result, no block, no failure.
        assert [rule.target_field for rule, _ in results] == ["Audio"]

    def test_force_overwrite_regenerates_a_filled_target(self):
        service = GenerationService(_hub())
        config = _config(
            [("Def", dict(enabled=True, type="text", prompt="d {{Word}}"))]
        )
        results, _blocked, _failed = service.generate_note(
            config, {"Word": "cat", "Def": "already here"}, force_overwrite=True
        )
        assert [rule.target_field for rule, _ in results] == ["Def"]
        assert results[0][1].text == "gen:d cat"

    def test_blank_sources_skip_unless_allowed(self):
        service = GenerationService(_hub())
        config = _config(
            [("Def", dict(enabled=True, type="text", prompt="d {{Word}}"))]
        )
        results, blocked, failed = service.generate_note(
            config, {"Word": "", "Def": ""}, allow_empty_fields=True
        )
        # allow_empty_fields wins over the blank-source skip, but the hard block gate still
        # runs first: a blank {{Word}} blocks the field.
        assert results == []
        assert [item.target_field for item in blocked] == ["Def"]
        assert failed == []
