"""TTS provider tests — fake and real, in one file.

Layers:
1. ``TestSplitText`` / ``TestTTSFactory`` — pure logic + error paths (no config needed).
2. ``TestTTSProviderWiring`` — request wiring built from the REAL config with a FakeHttpClient
   injected (offline, no quota → UNMARKED); asserts real configured values + auth, not
   fabricated keys. Skips an unconfigured / non-HTTP provider.
3. ``TTSProviderContract`` (+ Fake/Real subclasses) — the structural contract.
4. ``TestRealTTSBehavior`` — the REAL output must be VALID audio for the provider's format
   (magic bytes per ``audio_ext`` + a real, non-trivial size), not just non-empty.

Real paths read the actual config, auto-skip without creds/tools, and ``xfail`` on a
quota/rate/transient limit. Keyed/cloud providers carry ``@pytest.mark.tts`` per param;
keyless/offline ones (google_translate, edge_tts, piper) are UNMARKED and run by default.
"""

from __future__ import annotations

import base64

import pytest
from conftest import (
    FakeHttpClient,
    FakeTTSProvider,
    assert_valid_audio,
    call_or_xfail,
    real_tts_provider_for_or_skip,
    real_tts_subsection_or_skip,
    tts_provider_params,
)

from omnia.core.providers import (
    ProviderError,
    available_tts_providers_requiring_api,
    create_tts_provider,
)
from omnia.core.providers.tts.base import TTSProvider
from omnia.core.providers.tts.google_translate import split_text

# TTS providers that talk HTTP (so a FakeHttpClient can stand in for the network). edge_tts
# (websocket) and piper (subprocess) have no HTTP wiring and are excluded from wiring tests.
_HTTP_TTS = (
    "google_translate",
    "openai",
    "openrouter",
    "openai_compatible",
    "google_cloud",
)


def _tts_wiring_route(_method, url, _body, _headers):
    """Canned responder for OFFLINE TTS wiring across HTTP provider shapes (no network)."""
    if "oauth2.googleapis.com" in url or url.endswith("/token"):
        return {"access_token": "wiring-token", "expires_in": 3600}
    if "texttospeech.googleapis.com" in url:
        return {"audioContent": base64.b64encode(b"AUDIO").decode()}
    return b"AUDIO"  # gtts GET / openai POST-for-bytes


# --- 1. pure logic + error paths -------------------------------------------------------
class TestSplitText:
    def test_split_text_empty(self):
        assert split_text("") == []
        assert split_text("   ") == []

    def test_split_text_groups_words_under_limit(self):
        chunks = split_text("a b c d e", max_chars=3)
        assert all(len(c) <= 3 for c in chunks)
        assert chunks == ["a b", "c d", "e"]

    def test_split_text_hard_splits_long_word(self):
        assert split_text("abcdefgh", max_chars=3) == ["abc", "def", "gh"]


class TestTTSFactory:
    def test_factory_default_is_free_provider(self):
        assert create_tts_provider({}).name == "google_translate"

    def test_factory_unknown_raises(self):
        with pytest.raises(ProviderError):
            create_tts_provider({"provider": "nope"})

    def test_openai_tts_empty_key_raises(self):
        # The empty string is THE case under test (blank/misconfigured key), not a placeholder.
        with pytest.raises(ProviderError):
            create_tts_provider({"provider": "openai", "api_key": ""})


# --- 2. wiring, built from the REAL config (fake transport, offline) -------------------
class TestTTSProviderWiring:
    """Per-provider HTTP wiring built from the REAL config with a FakeHttpClient injected — no
    network call (offline → UNMARKED), asserting against the actual config (no fabricated
    keys). Skips a provider that isn't configured.
    """

    @pytest.mark.parametrize("provider", _HTTP_TTS)
    def test_makes_request_with_correct_auth(self, provider):
        http = FakeHttpClient(responder=_tts_wiring_route)
        tts = real_tts_provider_for_or_skip(provider, http=http)
        sub = real_tts_subsection_or_skip(provider)
        audio = tts.synthesize("hello world", lang="en")
        assert audio  # response parsed to bytes
        model_calls = [c for c in http.calls if "oauth2.googleapis.com" not in c[1]]
        assert model_calls, "no transport request was made"
        _m, url, _b, headers = model_calls[-1]
        if provider in available_tts_providers_requiring_api():
            # keyed/cloud: authenticates via a header, and the secret never leaks into the URL.
            assert any(
                k.lower() in ("authorization", "x-goog-api-key") for k in headers
            )
            secret = getattr(sub, "api_key", "")
            if secret:
                assert secret not in url


# --- 3. structural contract (fake + real) ----------------------------------------------
class TTSProviderContract:
    """Abstract contract: the shared assertions a :class:`TTSProvider` must satisfy.

    Concrete subclasses supply the CONTEXT via the ``tts`` fixture. Not named ``Test*`` so
    pytest does not collect it directly.
    """

    @pytest.fixture
    def tts(self) -> TTSProvider:
        raise NotImplementedError

    def test_is_a_tts_provider(self, tts):
        assert isinstance(tts, TTSProvider)
        assert isinstance(tts.name, str) and tts.name

    def test_synthesize_returns_nonempty_audio(self, tts):
        audio = call_or_xfail(tts.synthesize, "hello world", lang="en")
        assert isinstance(audio, (bytes, bytearray)) and len(audio) > 0


class TestFakeTTSContract(TTSProviderContract):
    """The contract against a canned provider — always runs, no quota."""

    @pytest.fixture
    def tts(self) -> TTSProvider:
        return FakeTTSProvider()


class TestRealTTSContract(TTSProviderContract):
    """The contract against EACH real provider (per-provider marks; skips when unavailable)."""

    @pytest.fixture(params=tts_provider_params())
    def tts(self, request) -> TTSProvider:
        return real_tts_provider_for_or_skip(request.param)


# --- 4. real behavior: the output must be VALID audio ----------------------------------
class TestRealTTSBehavior:
    """Each real provider's output must be VALID audio in its declared format (magic bytes +
    a real, non-trivial size), not merely non-empty. Per-provider marks; keyless providers run
    by default, keyed/cloud carry ``@tts``; ``xfail`` on a rate/transient limit.
    """

    @pytest.mark.parametrize("provider", tts_provider_params())
    def test_synthesizes_valid_audio(self, provider):
        tts = real_tts_provider_for_or_skip(provider)
        audio = call_or_xfail(
            tts.synthesize, "Hello, this is a real speech test.", lang="en"
        )
        assert_valid_audio(audio, tts.audio_ext)
