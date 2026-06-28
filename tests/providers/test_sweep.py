"""Provider sweep: EVERY registered LLM + TTS config builds, runs, and yields non-empty output.

Goal: no provider config has a syntax/wiring gap, and each produces output. HTTP providers
are driven through a single routing :class:`FakeHttpClient` (real provider code + factories
run; only the socket is faked). Non-HTTP providers (edge_tts websocket, piper subprocess)
have their transport injected as a fake. Real-credential checks live in the ``integration``
tests and skip unless creds are present.
"""

from __future__ import annotations

import base64
import os

import pytest
from conftest import FakeHttpClient

from omnia.core.providers import (
    available_llm_providers,
    available_tts_providers,
    create_llm_provider,
    create_tts_provider,
)
from omnia.core.providers.llm.factory import _BUILDERS as LLM_BUILDERS
from omnia.core.providers.tts.edge_tts import EdgeTTS
from omnia.core.providers.tts.factory import _BUILDERS as TTS_BUILDERS
from omnia.core.providers.tts.piper import PiperTTS

_MP3_B64 = base64.b64encode(b"MP3").decode()


def _route(method, url, body, headers):
    if "chat/completions" in url:
        return {"choices": [{"message": {"content": "TEXT"}}]}
    if "images/generations" in url:
        return {"data": [{"b64_json": base64.b64encode(b"IMG").decode()}]}
    if "audio/speech" in url:
        return b"AUDIO"
    if "texttospeech.googleapis.com" in url:
        return {"audioContent": _MP3_B64}
    if "generativelanguage" in url or "aiplatform" in url:
        return {"candidates": [{"content": {"parts": [{"text": "TEXT"}]}}]}
    if "translate_tts" in url:
        return b"MP3"
    if "oauth2.googleapis.com" in url or url.endswith("/token"):
        return {"access_token": "tok", "expires_in": 3600}
    raise AssertionError(f"unexpected URL in sweep: {url}")


@pytest.fixture
def routed_http():
    return FakeHttpClient(responder=_route)


# --- LLM: every provider builds + generates text --------------------------------------
_LLM_CONFIGS = {
    "openai": {"provider": "openai", "api_key": "k"},
    "openrouter": {"provider": "openrouter", "api_key": "k"},
    "openai_compatible": {"provider": "openai_compatible", "api_key": "k"},
    "gemini": {"provider": "gemini", "api_key": "k"},
    "gemini_vertex": {"provider": "gemini_vertex", "project": "p", "access_token": "t"},
}


class TestLLMSweep:
    def test_every_llm_provider_is_swept(self):
        assert set(_LLM_CONFIGS) == set(LLM_BUILDERS) == set(available_llm_providers())

    @pytest.mark.parametrize("name", sorted(_LLM_CONFIGS))
    def test_llm_provider_builds_and_generates(self, name, routed_http):
        provider = create_llm_provider(_LLM_CONFIGS[name], http=routed_http)
        out = provider.generate_text("hi", system="be brief", max_tokens=8)
        assert out == "TEXT"


# --- TTS: every provider builds; output is non-empty ----------------------------------
# Minimal config to construct each provider without error (no I/O at construction).
_TTS_BUILD_CONFIGS = {
    "google_translate": {"provider": "google_translate"},
    "openai": {"provider": "openai", "api_key": "k"},
    "openrouter": {"provider": "openrouter", "api_key": "k"},
    "openai_compatible": {"provider": "openai_compatible", "api_key": "k"},
    "google_cloud": {"provider": "google_cloud", "access_token": "t"},
    "edge_tts": {"provider": "edge_tts"},
    "piper": {"provider": "piper", "model": "voice.onnx"},
}
# The subset whose transport is HTTP (driven through the routing fake).
_TTS_HTTP = (
    "google_translate",
    "openai",
    "openrouter",
    "openai_compatible",
    "google_cloud",
)


class TestTTSSweep:
    def test_every_tts_provider_is_swept(self):
        assert (
            set(_TTS_BUILD_CONFIGS)
            == set(TTS_BUILDERS)
            == set(available_tts_providers())
        )

    @pytest.mark.parametrize("name", sorted(_TTS_BUILD_CONFIGS))
    def test_tts_provider_builds(self, name):
        # Construction must not raise or do I/O for any provider (covers syntax/wiring).
        from omnia.core.providers.tts.base import TTSProvider

        provider = create_tts_provider(_TTS_BUILD_CONFIGS[name])
        assert isinstance(provider, TTSProvider)

    @pytest.mark.parametrize("name", _TTS_HTTP)
    def test_http_tts_synthesizes(self, name, routed_http):
        provider = create_tts_provider(_TTS_BUILD_CONFIGS[name], http=routed_http)
        audio = provider.synthesize("hello world", lang="en")
        assert isinstance(audio, bytes) and len(audio) > 0

    def test_edge_tts_synthesizes_with_injected_transport(self):
        class _FakeSynth:
            def synthesize(self, text, voice):
                return b"EDGE-MP3"

        audio = EdgeTTS(synthesizer=_FakeSynth()).synthesize("hi", lang="vi")
        assert audio == b"EDGE-MP3"

    def test_piper_synthesizes_with_injected_runner(self):
        class _FakeRunner:
            def run(self, text, model_path):
                assert model_path == "voice.onnx"
                return b"RIFFwav"

        audio = PiperTTS(model="voice.onnx", runner=_FakeRunner()).synthesize("hi")
        assert audio == b"RIFFwav"


# --------------------------------------------------------------------------------------
# Integration: real providers. Skipped unless creds/env present (CI stays green/offline).
# --------------------------------------------------------------------------------------
class TestRealProviderIntegration:
    @pytest.mark.integration
    def test_integration_gemini_vertex_real(self):
        project = os.environ.get("OMNIA_IT_VERTEX_PROJECT")
        if not project:
            pytest.skip(
                "set OMNIA_IT_VERTEX_PROJECT (+ creds) to run the Vertex integration"
                " test"
            )
        config = {
            "provider": "gemini_vertex",
            "project": project,
            "location": os.environ.get("OMNIA_IT_VERTEX_LOCATION", "global"),
            "model": os.environ.get("OMNIA_IT_VERTEX_MODEL", "gemini-2.5-flash"),
            "credentials_path": os.environ.get("OMNIA_IT_VERTEX_CREDS"),
            "use_gcloud": os.environ.get("OMNIA_IT_GCLOUD") == "1",
        }
        out = create_llm_provider(config).generate_text(
            "Reply with the single word: pong"
        )
        assert isinstance(out, str) and out.strip()

    @pytest.mark.integration
    def test_integration_google_translate_tts_real(self):
        if os.environ.get("OMNIA_IT_NETWORK") != "1":
            pytest.skip(
                "set OMNIA_IT_NETWORK=1 to run the free gTTS integration test"
                " (hits network)"
            )
        audio = create_tts_provider(
            {"provider": "google_translate", "lang": "en"}
        ).synthesize("hello")
        assert audio[:3] == b"ID3" or len(audio) > 100  # mp3-ish
