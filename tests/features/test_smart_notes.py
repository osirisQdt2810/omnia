"""Tests for smart_notes (prompt interpolation + the provider-backed GenerationService).

This file also holds the real-provider feature tests: smart_notes' GenerationService driven
by EACH real provider, marked ``llm``/``tts`` and auto-skipping without credentials.
"""

from __future__ import annotations

import base64
import types

import pytest
from conftest import (
    FakeHttpClient,
    FakeLLMProvider,
    FakeTTSProvider,
    assert_valid_audio,
    call_or_xfail,
    is_provider_limit_error,
    llm_provider_params,
    real_llm_provider_for_or_skip,
    real_tts_provider_for_or_skip,
    tts_provider_params,
)

from omnia.core.config.models import (
    LLMSettings,
    OpenAICompatibleLLMSettings,
    SmartNotesFieldRule,
    TTSSettings,
)
from omnia.core.providers import ProviderError, ProviderHub
from omnia.features.smart_notes.logic import (
    GenerationService,
    extract_field_refs,
    interpolate,
)

# ---------------------------------------------------------------------------
# Mocked / offline tests
# ---------------------------------------------------------------------------


class TestPromptInterpolation:
    def test_extract_field_refs_skips_cloze(self):
        assert extract_field_refs("Define {{Word}} using {{Hint}}") == ["Word", "Hint"]
        assert extract_field_refs("{{c1::hidden}} {{Word}}") == ["Word"]

    def test_interpolate_substitutes_and_skips_cloze(self):
        out = interpolate("{{c1::x}} define {{Word}}", {"Word": "cat"})
        assert out == "{{c1::x}} define cat"
        assert interpolate("{{Missing}}!", {}) == "!"


def _route(method, url, body, headers):
    if "chat/completions" in url:
        return {"choices": [{"message": {"content": "a feline"}}]}
    if "images/generations" in url:
        return {"data": [{"b64_json": base64.b64encode(b"PNG").decode()}]}
    if "translate_tts" in url:
        return b"MP3"
    raise AssertionError(url)


def _hub():
    return ProviderHub(
        LLMSettings(provider="openai", openai=OpenAICompatibleLLMSettings(api_key="k")),
        TTSSettings(provider="google_translate"),  # lang defaults to "en"
        http=FakeHttpClient(responder=_route),
    )


class TestGenerationService:
    def test_generate_text_interpolates_prompt(self):
        service = GenerationService(_hub())
        rule = SmartNotesFieldRule(
            kind="text", prompt="Define {{Word}}", target_field="Definition"
        )
        result = service.generate(rule, {"Word": "cat"})
        assert result.kind == "text"
        assert result.text == "a feline"

    def test_generate_image_returns_bytes(self):
        service = GenerationService(_hub())
        rule = SmartNotesFieldRule(
            kind="image", prompt="a {{Word}}", target_field="Pic"
        )
        result = service.generate(rule, {"Word": "cat"})
        assert result.kind == "image"
        assert result.data == b"PNG"
        assert result.ext == "png"

    def test_generate_tts_uses_source_field(self):
        service = GenerationService(_hub())
        rule = SmartNotesFieldRule(
            kind="tts", source_field="Word", target_field="Audio"
        )
        result = service.generate(rule, {"Word": "cat"})
        assert result.kind == "tts"
        assert result.data == b"MP3"
        assert result.ext == "mp3"


class TestSmartNotesPlugin:
    def test_disable_unsubscribes_browser_hook(self, gui_hooks):
        import types

        from omnia.core.config.models import SmartNotesSettings
        from omnia.features.smart_notes import SmartNotesPlugin

        ctx = types.SimpleNamespace(settings=SmartNotesSettings(), providers=_hub())
        plugin = SmartNotesPlugin()
        plugin.on_enable(ctx)
        assert gui_hooks.browser_will_show_context_menu.count() == 1
        plugin.on_disable(ctx)
        assert gui_hooks.browser_will_show_context_menu.count() == 0


# ---------------------------------------------------------------------------
# GenerationService contract — the SAME functional assertions, fake + real backends.
#
# The provider sweep (``tests/providers/test_llm.py`` / ``test_tts.py``) proves each provider
# works in isolation; these prove the *feature that uses them* works against its providers. The
# context is a ProviderHub-shaped ``hub`` (exposing ``.llm()`` / ``.tts()``) supplied by a
# fixture each concrete subclass overrides. Because LLM-gen and TTS-gen parametrize over
# different provider sets, there are TWO bases:
#
# * :class:`_SmartNotesLLMGenContract`  → text + (capability-gated) image rules.
# * :class:`_SmartNotesTTSGenContract`  → tts rules.
#
# Each base has a Fake subclass (canned providers — always runs, free) and a Real subclass
# whose ``hub`` fixture is PARAMETRIZED over every provider. Policy mirrors the provider sweep:
# skip without creds, ``xfail`` on quota/token/transient.
# ---------------------------------------------------------------------------


def _stub_hub(*, llm=None, tts=None):
    """A minimal ProviderHub-shaped stub exposing just ``llm()`` / ``tts()``."""
    return types.SimpleNamespace(llm=lambda: llm, tts=lambda: tts)


class _SmartNotesLLMGenContract:
    """Shared assertions for LLM-backed smart_notes rules; subclasses supply ``hub``."""

    @pytest.fixture
    def hub(self):
        raise NotImplementedError

    def test_text_rule_generates(self, hub):
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="text",
            prompt="Define {{Word}} in one short sentence.",
            target_field="Def",
        )
        result = call_or_xfail(service.generate, rule, {"Word": "cat"})
        assert result.kind == "text"
        assert isinstance(result.text, str) and result.text.strip()

    def test_image_rule_generates_if_supported(self, hub):
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="image", prompt="a single red apple", target_field="Pic"
        )
        try:
            result = service.generate(rule, {})
        except ProviderError as exc:
            if is_provider_limit_error(exc):
                pytest.xfail(f"image-gen limit: {str(exc)[:160]}")
            pytest.skip(f"image gen unavailable: {str(exc)[:120]}")
        assert result.kind == "image"
        assert isinstance(result.data, (bytes, bytearray)) and result.data


class _SmartNotesTTSGenContract:
    """Shared assertions for TTS-backed smart_notes rules; subclasses supply ``hub``."""

    @pytest.fixture
    def hub(self):
        raise NotImplementedError

    def test_tts_rule_synthesizes(self, hub):
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="tts", source_field="Word", target_field="Audio"
        )
        result = call_or_xfail(service.generate, rule, {"Word": "hello world"})
        assert result.kind == "tts"
        assert isinstance(result.data, (bytes, bytearray)) and result.data
        assert result.ext  # provider declared an audio extension


class TestSmartNotesLLMGenFake(_SmartNotesLLMGenContract):
    """LLM-backed rules against canned providers — always runs, no quota."""

    @pytest.fixture
    def hub(self):
        return _stub_hub(llm=FakeLLMProvider())


@pytest.mark.llm
class TestSmartNotesLLMGenReal(_SmartNotesLLMGenContract):
    """LLM-backed rules against EACH real provider (skips per provider without creds)."""

    @pytest.fixture(params=llm_provider_params())
    def hub(self, request):
        return _stub_hub(llm=real_llm_provider_for_or_skip(request.param))

    def test_generated_definition_matches_the_word(self, hub):
        # Beyond non-empty: the generated definition must actually be ABOUT the word.
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="text",
            prompt="Define the word '{{Word}}' in one short sentence.",
            target_field="Def",
        )
        result = call_or_xfail(service.generate, rule, {"Word": "cat"})
        text = result.text.lower()
        assert any(
            kw in text for kw in ("animal", "feline", "mammal", "pet", "cat")
        ), f"definition of 'cat' had no relevant content: {result.text!r}"


class TestSmartNotesTTSGenFake(_SmartNotesTTSGenContract):
    """TTS-backed rules against a canned provider — always runs, no quota."""

    @pytest.fixture
    def hub(self):
        return _stub_hub(tts=FakeTTSProvider())


class TestSmartNotesTTSGenReal(_SmartNotesTTSGenContract):
    """TTS-backed rules against EACH real provider (per-provider marks; skips when unavailable)."""

    @pytest.fixture(params=tts_provider_params())
    def hub(self, request):
        return _stub_hub(tts=real_tts_provider_for_or_skip(request.param))

    def test_generated_audio_is_valid(self, hub):
        # Beyond non-empty: the field gets REAL audio in the provider's declared format.
        service = GenerationService(hub)
        rule = SmartNotesFieldRule(
            kind="tts", source_field="Word", target_field="Audio"
        )
        result = call_or_xfail(
            service.generate, rule, {"Word": "Hello, this is a real speech test."}
        )
        assert_valid_audio(result.data, result.ext)
