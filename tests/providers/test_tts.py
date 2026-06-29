"""TTS provider tests — fake and real, in one file.

Layers:
1. ``TestSplitText`` / ``TestTTSFactory`` — pure logic + error paths (no config needed).
2. ``TestTTSProviderWiring`` — request wiring built from the REAL config with a FakeHttpClient
   injected (offline, no quota → UNMARKED); asserts real configured values + auth, not
   fabricated keys. Skips an unconfigured / non-HTTP provider.
   ``TestGoogleCloudLanguageCode`` — offline regression that a voice's own language drives the
   request's ``languageCode`` (the playground HTTP-400 bug).
3. ``TTSProviderContract`` + ``TestFakeTTSContract`` — the structural contract (fake context).
4. ONE real suite PER provider (``Test<Provider>RealTTS``) — the contract + a VALID-audio check
   (magic bytes per ``audio_ext`` + a real, non-trivial size), run against that provider built
   from the real config. Keyless ones (google_translate, edge_tts, piper) run by default; the
   keyed/cloud ones carry ``@pytest.mark.tts``.

Real paths read the actual config, auto-skip with a clear reason when they can't run (no creds,
no package, no native runner), and ``xfail`` on a quota/rate/transient limit — never a fake pass.
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
)

from omnia.core.providers import (
    ProviderError,
    available_tts_providers_requiring_api,
    create_tts_provider,
)
from omnia.core.providers.token_source import StaticTokenSource
from omnia.core.providers.tts.base import TTSProvider, TTSVoice
from omnia.core.providers.tts.edge_tts import EdgeTTS
from omnia.core.providers.tts.google_cloud import GoogleCloudTTS
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


class TestEdgeListVoices:
    """EdgeTTS.list_voices: curated seed offline; refresh parses the manifest + merges (no net)."""

    _FAKE_MANIFEST = [
        {
            "ShortName": "ja-JP-NanamiNeural",
            "Locale": "ja-JP",
            "FriendlyName": "Microsoft Nanami Online (Natural) - Japanese (Japan)",
            "Gender": "Female",
        },
        {
            "ShortName": "zh-CN-NewVoiceNeural",  # a voice NOT in the curated seed
            "Locale": "zh-CN",
            "Gender": "Male",
        },
        {"Locale": "en-US"},  # no ShortName → skipped
    ]

    def _http(self):
        import json

        return FakeHttpClient(data=json.dumps(self._FAKE_MANIFEST).encode("utf-8"))

    def test_no_refresh_returns_curated_seed_without_network(self):
        http = self._http()
        voices = EdgeTTS.list_voices(http)
        # Offline path: the curated seed, no HTTP call.
        assert all(isinstance(v, TTSVoice) for v in voices)
        assert any(v.voice == "vi-VN-HoaiMyNeural" for v in voices)
        assert http.calls == []

    def test_refresh_parses_and_merges_over_the_seed(self):
        http = self._http()
        voices = EdgeTTS.list_voices(http, refresh=True)
        by_id = {v.voice: v for v in voices}
        # The fetched voice (not in the seed) is added, normalized to a TTSVoice.
        new = by_id["zh-CN-NewVoiceNeural"]
        assert (new.provider, new.lang_code, new.language, new.gender) == (
            "edge_tts",
            "zh",
            "zh-CN",
            "Male",
        )
        # The fetched ja voice overrides the seed's ja entry (FriendlyName → name).
        assert by_id["ja-JP-NanamiNeural"].name.startswith("Microsoft Nanami")
        # Seed-only voices (no fetched counterpart) are kept.
        assert "vi-VN-HoaiMyNeural" in by_id

    def test_refresh_hits_the_manifest_url(self):
        http = self._http()
        EdgeTTS.list_voices(http, refresh=True)
        _, url, _params, _headers = http.calls[0]
        assert "voices/list" in url and "trustedclienttoken=" in url

    def test_refresh_non_array_response_raises(self):
        http = FakeHttpClient(data=b'{"not": "an array"}')
        with pytest.raises(ProviderError):
            EdgeTTS.list_voices(http, refresh=True)


class TestAggregatedVoices:
    """The tts-package aggregation is the single voice source (no concrete-provider import)."""

    def test_aggregated_voices_groups_by_provider_tag(self):
        from omnia.core.providers import tts

        voices = tts.aggregated_voices()
        # Curated seeds from each provider, keyed by the voice's own provider tag. piper ships a
        # bundled Vietnamese voice, so it now contributes one too.
        assert voices["edge_tts"] and voices["openai"] and voices["viettts"]
        assert voices["piper"] and any(v.lang_code == "vi" for v in voices["piper"])
        assert all(isinstance(v, TTSVoice) for v in voices["edge_tts"])
        # google_translate is language-only — it contributes no named voices.
        assert "google_translate" not in voices

    def test_voices_for_reads_the_aggregation(self):
        from omnia.core.providers import tts

        assert tts.voices_for("google_translate") == []
        assert any(v.voice == "vi-VN-HoaiMyNeural" for v in tts.voices_for("edge_tts"))

    def test_refresh_voices_is_provider_agnostic(self):
        # Provider-agnostic: refresh_voices fetches via the FAKE http for whichever providers
        # can fetch (edge_tts), curated for the rest — no concrete-provider import here.
        import json

        from omnia.core.providers import tts

        manifest = [
            {"ShortName": "de-DE-NewNeural", "Locale": "de-DE", "Gender": "Male"}
        ]
        http = FakeHttpClient(data=json.dumps(manifest).encode("utf-8"))
        refreshed = tts.refresh_voices(http)
        # edge_tts got the fetched voice merged in; openai stayed on its curated seed.
        assert any(v.voice == "de-DE-NewNeural" for v in refreshed["edge_tts"])
        assert refreshed["openai"] == tts.voices_for("openai")


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


class _FakeNativeManager:
    """Fake NativeRuntimeManager: scripts ensure_running / run_in_venv for the migrated TTS.

    No venv, pip, subprocess, or socket — records calls and returns canned results so the
    managed viet-tts (server) and piper (cli) paths are exercised offline.
    """

    def __init__(
        self,
        *,
        host_port: tuple[str, int] | None = None,
        installed: bool = True,
        wav: bytes = b"",
    ) -> None:
        self._host_port = host_port
        self._installed = installed
        self._wav = wav
        self.ensure_running_specs: list = []
        self.run_in_venv_calls: list = []

    def ensure_running(self, spec):
        self.ensure_running_specs.append(spec)
        if not self._installed:
            raise ProviderError(
                f"{spec.label} isn't installed — enable it in Advanced."
            )
        assert self._host_port is not None, "test did not script host_port"
        return self._host_port

    def run_in_venv(self, spec, extra_argv, *, input=None):
        self.run_in_venv_calls.append((spec, list(extra_argv), input))
        if not self._installed:
            raise ProviderError(
                f"{spec.label} isn't installed — enable it in Advanced."
            )
        # piper writes WAV to the `-f <output>` path; mimic that so the runner reads it back.
        out_path = extra_argv[extra_argv.index("-f") + 1]
        from pathlib import Path as _Path

        _Path(out_path).write_bytes(self._wav)
        return 0


class TestVietTTSManagedServer:
    """viet-tts now runs in the add-on-managed sidecar venv (ADR-005): synthesize() asks the
    manager to start/reuse the local server and points the request at the returned host:port.
    """

    def test_synthesize_ensures_running_and_targets_returned_host_port(self):
        from omnia.core.providers.tts.viettts import SPEC, VietTTS

        manager = _FakeNativeManager(host_port=("127.0.0.1", 54321))
        http = FakeHttpClient(responder=lambda method, url, body, headers: b"RIFFwav")
        provider = VietTTS(http=http, manager=manager)

        audio = provider.synthesize("xin chào", lang="vi")

        assert audio == b"RIFFwav"
        assert manager.ensure_running_specs == [SPEC]  # asked the manager to start it
        # the OpenAI-compatible request hit the manager-provided host:port
        url = http.calls[-1][1]
        assert url.startswith("http://127.0.0.1:54321/v1/audio/speech")

    def test_not_installed_propagates_provider_error(self):
        from omnia.core.providers.tts.viettts import VietTTS

        manager = _FakeNativeManager(installed=False)
        provider = VietTTS(manager=manager)
        with pytest.raises(ProviderError, match="isn't installed"):
            provider.synthesize("xin chào", lang="vi")


class TestSidecarPiperRunner:
    """piper's default runner runs the CLI in the managed venv: text on stdin, WAV out to a
    temp `-f` file that the runner reads back as bytes.
    """

    def _model_file(self, tmp_path):
        # The runner checks the model file exists before invoking the CLI; provide a real one.
        model = tmp_path / "voice.onnx"
        model.write_bytes(b"onnx")
        return str(model)

    def test_returns_wav_bytes_from_managed_cli(self, tmp_path):
        from omnia.core.providers.tts.piper import SidecarPiperRunner

        model_path = self._model_file(tmp_path)
        manager = _FakeNativeManager(wav=b"RIFF....WAVE-bytes")
        runner = SidecarPiperRunner(manager=manager)

        audio = runner.run("hello", model_path)

        assert audio == b"RIFF....WAVE-bytes"
        spec, argv, stdin = manager.run_in_venv_calls[-1]
        assert spec.name == "piper"
        assert "-m" in argv and model_path in argv  # resolved model passed through
        assert stdin == b"hello"  # text fed on stdin

    def test_not_installed_propagates_provider_error(self, tmp_path):
        from omnia.core.providers.tts.piper import SidecarPiperRunner

        manager = _FakeNativeManager(installed=False)
        runner = SidecarPiperRunner(manager=manager)
        with pytest.raises(ProviderError, match="isn't installed"):
            runner.run("hello", self._model_file(tmp_path))


class TestGoogleCloudLanguageCode:
    """Regression for the playground HTTP 400: a Google Cloud voice name encodes its OWN
    language ("vi-VN-Neural2-A" → "vi-VN"); the request's ``languageCode`` MUST match it, or
    Google rejects it ("Requested language code 'en-US' doesn't match the voice…"). The voice
    wins over a mismatched configured/detected ``lang``. Offline (fake transport) so it runs in
    the default bucket and guards the fix forever.
    """

    def _synth_voice_params(self, voice, *, lang=None, language_code="", cfg_lang="en"):
        http = FakeHttpClient(responder=_tts_wiring_route)
        provider = GoogleCloudTTS(
            token_source=StaticTokenSource("tok"),
            lang=cfg_lang,
            voice=voice,
            language_code=language_code,
            http=http,
        )
        provider.synthesize("xin chào", lang=lang)
        call = [c for c in http.calls if "texttospeech" in c[1]][-1]
        return call[2]["voice"]  # the request body's voice params

    def test_language_code_is_derived_from_a_configured_voice(self):
        # The exact playground scenario: vi-VN voice, lang defaults to "en" → must send vi-VN.
        params = self._synth_voice_params("vi-VN-Neural2-A", lang="en")
        assert params["languageCode"] == "vi-VN"
        assert params["name"] == "vi-VN-Neural2-A"

    def test_voice_language_wins_over_explicit_language_code(self):
        # An explicit language_code that disagrees with the voice would 400 at Google — the
        # voice's own language must override it.
        params = self._synth_voice_params(
            "vi-VN-Wavenet-B", lang="en", language_code="en-US"
        )
        assert params["languageCode"] == "vi-VN"

    def test_falls_back_to_lang_when_no_voice(self):
        # With no voice pinned, the requested/configured language is used (mapped to BCP-47).
        params = self._synth_voice_params("", lang="vi")
        assert params["languageCode"] == "vi-VN"
        assert "name" not in params


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


# --- 4. ONE real suite PER TTS provider ------------------------------------------------
# Each provider gets its OWN class (a failure is attributed to that provider by name, and the
# whole TTS layer is exercised "for all providers" as the user asked): the structural contract
# + a VALID-audio assertion (magic bytes + a real, non-trivial size — not merely non-empty).
# Keyless/offline providers (google_translate, edge_tts, piper) run in the DEFAULT bucket; the
# keyed/cloud ones carry ``@pytest.mark.tts``. Every class auto-skips (with a clear reason) when
# it can't run here — no creds, no package, no native runner — never a fake pass.
class _RealTTSProviderSuite(TTSProviderContract):
    """Shared body for the per-provider real suites. ``PROVIDER`` is set by each subclass; the
    leading underscore keeps pytest from collecting this base directly.
    """

    PROVIDER: str = ""

    @pytest.fixture
    def tts(self) -> TTSProvider:
        return real_tts_provider_for_or_skip(self.PROVIDER)

    def test_synthesizes_valid_audio(self, tts):
        audio = call_or_xfail(
            tts.synthesize, "Hello, this is a real speech test.", lang="en"
        )
        assert_valid_audio(audio, tts.audio_ext)


# Keyless / free — run by default (no API, no key); these are the "không có llm" path.
class TestGoogleTranslateRealTTS(_RealTTSProviderSuite):
    PROVIDER = "google_translate"


class TestEdgeRealTTS(_RealTTSProviderSuite):
    PROVIDER = "edge_tts"


class TestVietTTSRealTTS(_RealTTSProviderSuite):
    # Local open-source server (no cloud key); auto-skips unless a viet-tts server is running.
    PROVIDER = "viettts"


class TestPiperRealTTS(_RealTTSProviderSuite):
    PROVIDER = "piper"


# Keyed / cloud — marked ``tts`` (network + creds); these are the "có llm/credentials" path.
@pytest.mark.tts
class TestGoogleCloudRealTTS(_RealTTSProviderSuite):
    PROVIDER = "google_cloud"


@pytest.mark.tts
class TestOpenAIRealTTS(_RealTTSProviderSuite):
    PROVIDER = "openai"


@pytest.mark.tts
class TestOpenRouterRealTTS(_RealTTSProviderSuite):
    PROVIDER = "openrouter"


@pytest.mark.tts
class TestOpenAICompatibleRealTTS(_RealTTSProviderSuite):
    PROVIDER = "openai_compatible"
