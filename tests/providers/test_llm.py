"""LLM provider tests — fake and real, in one file.

Layers (top to bottom):
1. ``TestLLMFactoryErrors`` — pure error-path checks (sentinel inputs, no config needed).
2. ``TestGeminiImageGeneration`` — Gemini image generation over a mocked response (offline).
3. ``TestLLMProviderWiring`` — request wiring built from the REAL configured model/credentials
   with a FakeHttpClient injected (offline, no quota → UNMARKED); asserts the actual config is
   wired in, not fabricated values. Skips a provider that isn't configured.
4. ``LLMProviderContract`` + ``TestFakeLLMContract`` — the structural contract (fake context):
   a provider is an ``LLMProvider`` and returns non-empty text / optional kwargs / image.
5. ``RealLLMBehaviorMixin`` — the behavior battery: the REAL output must SATISFY the prompt
   (factual, arithmetic, yes/no, item-count, conditional, JSON, translation) by partial match.
6. ONE real suite PER provider (``Test<Provider>RealLLM``, all ``@llm``) — the contract + the
   behavior battery against that provider, auto-skipping when it has no credentials configured.

Real paths read the actual config (config/ + the gitignored user_files override), auto-skip a
provider without credentials, and ``xfail`` on a quota/token/transient limit.
"""

from __future__ import annotations

import base64
import json
import re

import pytest
from conftest import (
    FakeHttpClient,
    FakeLLMProvider,
    call_or_xfail,
    is_provider_limit_error,
    llm_provider_params,
    real_llm_provider_for_or_skip,
    real_llm_subsection_or_skip,
)

from omnia.core.providers import (
    ProviderError,
    available_llm_providers,
    create_llm_provider,
)
from omnia.core.providers.llm.base import LLMProvider


# --- helpers ---------------------------------------------------------------------------
def _norm(text: str) -> str:
    """Lowercased, stripped — for tolerant 'contains the required info' matching."""
    return text.strip().lower()


def _strip_code_fences(text: str) -> str:
    """Remove a ```lang ... ``` wrapper some models add around JSON/code, if present."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s.strip())
    return s.strip()


def _wiring_route(_method, url, _body, _headers):
    """Canned responder for OFFLINE wiring tests across all provider shapes (no network)."""
    if "oauth2.googleapis.com" in url or url.endswith("/token"):
        return {"access_token": "wiring-token", "expires_in": 3600}
    if "chat/completions" in url:
        return {"choices": [{"message": {"content": "WIRED"}}]}
    if "aiplatform" in url or "generativelanguage" in url:
        return {"candidates": [{"content": {"parts": [{"text": "WIRED"}]}}]}
    raise AssertionError(f"unexpected wiring URL: {url}")


def _request_blob(calls) -> str:
    """Non-token request URLs+payloads joined — for 'is X wired into the request' checks."""
    return "\n".join(
        f"{url} {json.dumps(body or {})}"
        for _m, url, body, _h in calls
        if "oauth2.googleapis.com" not in url
    )


# --- 1. error paths (offline, no config) -----------------------------------------------
class TestLLMFactoryErrors:
    """Pure error-path checks that need no provider config (sentinel inputs only)."""

    def test_unknown_provider_raises(self):
        with pytest.raises(ProviderError):
            create_llm_provider({"provider": "nope"})

    def test_openai_compatible_empty_key_raises(self):
        # The empty string is THE case under test (a blank/misconfigured key), not a placeholder.
        with pytest.raises(ProviderError):
            create_llm_provider({"provider": "openai", "api_key": ""})

    def test_bad_response_shape_raises(self):
        # A 200 with the wrong JSON shape must raise, not return garbage. The key here is a
        # throwaway because no real call is made — only the response-parsing path is exercised.
        http = FakeHttpClient(json={"unexpected": 1})
        provider = create_llm_provider(
            {"provider": "openai", "api_key": "unused-no-call"}, http=http
        )
        with pytest.raises(ProviderError):
            provider.generate_text("hi")

    def test_lists_known_providers(self):
        assert {"openai_compatible", "gemini", "gemini_vertex"} <= set(
            available_llm_providers()
        )

    def test_vertex_derives_project_from_credentials_json(self):
        # No explicit `project` in config: the factory reads `project_id` from the SA JSON.
        provider = create_llm_provider(
            {
                "provider": "gemini_vertex",
                "credentials_json": {"project_id": "derived-proj"},
            }
        )
        assert provider._project == "derived-proj"

    def test_vertex_explicit_project_wins_over_json(self):
        provider = create_llm_provider(
            {
                "provider": "gemini_vertex",
                "project": "explicit-proj",
                "credentials_json": {"project_id": "derived-proj"},
            }
        )
        assert provider._project == "explicit-proj"

    def test_vertex_no_project_anywhere_raises(self):
        with pytest.raises(ProviderError):
            create_llm_provider({"provider": "gemini_vertex"})


# --- 1b. Gemini image generation (offline, mocked HTTP) --------------------------------
class TestGeminiImageGeneration:
    """``GeminiProvider.generate_image`` over a mocked ``:generateContent`` response.

    The Gemini image model returns the picture as an inline base64 ``inlineData`` part; these
    assert the provider targets the configured image model and decodes that data — never
    hitting a real API.
    """

    def test_generate_image_decodes_inline_data_and_targets_image_model(self):
        png = b"\x89PNG\r\n\x1a\n-fake-bytes"
        http = FakeHttpClient(
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "here is your image"},
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(png).decode(),
                                    }
                                },
                            ]
                        }
                    }
                ]
            }
        )
        provider = create_llm_provider(
            {
                "provider": "gemini",
                "api_key": "k",
                "model": "gemini-2.0-flash",
                "image_model": "gemini-2.5-flash-image",
            },
            http=http,
        )
        data = provider.generate_image("a red apple")
        # The inline base64 part was decoded to the raw image bytes.
        assert data == png
        _method, url, body, _headers = http.calls[-1]
        # The request targeted the configured IMAGE model, not the text model.
        assert "gemini-2.5-flash-image:generateContent" in url
        # And it asked for the IMAGE modality (vio-ai's wire shape) — without this the model
        # returns text only and emits no picture.
        assert body["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
        # The prompt rides the same user-part envelope as a text request.
        assert body["contents"][0]["parts"][0]["text"] == "a red apple"

    def test_generate_image_without_image_model_raises(self):
        provider = create_llm_provider(
            {"provider": "gemini", "api_key": "k", "model": "gemini-2.0-flash"}
        )
        with pytest.raises(ProviderError):
            provider.generate_image("a red apple")

    def test_generate_image_rejects_a_response_with_no_inline_data(self):
        http = FakeHttpClient(
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "no picture"}]},
                        "finishReason": "STOP",
                    }
                ]
            }
        )
        provider = create_llm_provider(
            {"provider": "gemini", "api_key": "k", "image_model": "img"}, http=http
        )
        with pytest.raises(ProviderError):
            provider.generate_image("a red apple")


# --- 2. wiring, built from the REAL config (fake transport, offline) -------------------
class TestLLMProviderWiring:
    """Per-provider request WIRING, built from the REAL configured model/credentials but with a
    FakeHttpClient injected — so it makes NO network call (offline, no quota → intentionally
    UNMARKED) yet asserts against the actual config, not fabricated values. Skips an
    unconfigured provider.
    """

    @pytest.mark.parametrize("provider", llm_provider_params())
    def test_wires_configured_model_into_request(self, provider):
        http = FakeHttpClient(responder=_wiring_route)
        llm = real_llm_provider_for_or_skip(provider, http=http)
        sub = real_llm_subsection_or_skip(provider)
        out = llm.generate_text("ping", max_tokens=16)
        assert out == "WIRED"  # the response shape was parsed
        blob = _request_blob(http.calls)
        assert (
            sub.text_model in blob
        ), f"configured model {sub.text_model!r} not wired into request: {blob[:200]}"

    @pytest.mark.parametrize("provider", llm_provider_params())
    def test_auth_in_header_never_in_url(self, provider):
        http = FakeHttpClient(responder=_wiring_route)
        llm = real_llm_provider_for_or_skip(provider, http=http)
        sub = real_llm_subsection_or_skip(provider)
        llm.generate_text("ping", max_tokens=16)
        model_calls = [c for c in http.calls if "oauth2.googleapis.com" not in c[1]]
        _m, url, _b, headers = model_calls[-1]
        # Authenticated via a header (Bearer token or x-goog-api-key); never leak into the URL.
        assert any(k.lower() in ("authorization", "x-goog-api-key") for k in headers)
        secret = getattr(
            sub, "api_key", ""
        )  # vertex uses a minted token, not a config key
        if secret:
            assert secret not in url


# --- 3. structural contract (fake + real) ----------------------------------------------
class LLMProviderContract:
    """Abstract contract: the shared assertions an :class:`LLMProvider` must satisfy.

    Concrete subclasses supply the CONTEXT by overriding the ``provider`` fixture. Not named
    ``Test*`` so pytest does not collect it directly.
    """

    @pytest.fixture
    def provider(self) -> LLMProvider:
        raise NotImplementedError

    def test_is_an_llm_provider(self, provider):
        assert isinstance(provider, LLMProvider)
        assert isinstance(provider.name, str) and provider.name

    def test_generate_text_returns_nonempty_string(self, provider):
        # Generous budget: reasoning models (gemini-2.5-*) spend tokens on thoughts first.
        out = call_or_xfail(
            provider.generate_text,
            "Reply with exactly the single word: pong",
            system="Be terse.",
            max_tokens=256,
        )
        assert isinstance(out, str) and out.strip()

    def test_generate_text_accepts_optional_kwargs(self, provider):
        # The whole documented kwarg surface must be accepted: no system message, no token cap.
        out = call_or_xfail(provider.generate_text, "Say hi.", max_tokens=None)
        assert isinstance(out, str) and out.strip()

    def test_generate_image_if_supported(self, provider):
        try:
            data = provider.generate_image("a single red apple on a white background")
        except ProviderError as exc:
            if is_provider_limit_error(exc):
                pytest.xfail(f"image-gen limit: {str(exc)[:160]}")
            pytest.skip(f"{provider.name} image gen unavailable: {str(exc)[:120]}")
        assert isinstance(data, (bytes, bytearray)) and len(data) > 0


class TestFakeLLMContract(LLMProviderContract):
    """The contract against a canned provider — always runs, no quota."""

    @pytest.fixture
    def provider(self) -> LLMProvider:
        return FakeLLMProvider(text="pong")


# --- 4. real behavior: the output must SATISFY the prompt ------------------------------
class RealLLMBehaviorMixin:
    """The behavior battery: a real provider's output must actually SATISFY the prompt (the
    right fact / count / condition by partial-semantic match), not merely be non-empty. Mixed
    into each per-provider suite below; ``xfail`` on a quota/token limit via ``call_or_xfail``.
    """

    def test_answers_a_factual_question(self, provider):
        out = call_or_xfail(
            provider.generate_text,
            "What is the capital city of Japan? Answer with only the city name.",
            max_tokens=256,
        )
        assert "tokyo" in _norm(out), f"expected Tokyo, got: {out!r}"

    def test_does_simple_arithmetic(self, provider):
        out = call_or_xfail(
            provider.generate_text,
            "Compute 6 multiplied by 7. Reply with only the resulting number.",
            max_tokens=256,
        )
        assert "42" in re.sub(r"[^0-9]", "", out), f"expected 42, got: {out!r}"

    def test_follows_yes_no_with_the_correct_answer(self, provider):
        out = call_or_xfail(
            provider.generate_text,
            "Is the planet Earth flat? Answer with only the word yes or no.",
            max_tokens=256,
        )
        n = _norm(out)
        assert "no" in n and "yes" not in n, f"expected no, got: {out!r}"

    def test_respects_an_exact_item_count(self, provider):
        # The user's example: a count constraint must be honored — 4 items would be wrong.
        out = call_or_xfail(
            provider.generate_text,
            "List exactly three fruits as a comma-separated list. Output only the list.",
            max_tokens=256,
        )
        items = [p.strip() for p in _strip_code_fences(out).split(",") if p.strip()]
        assert len(items) == 3, f"expected exactly 3 items, got {len(items)}: {out!r}"

    def test_follows_a_conditional_instruction(self, provider):
        out = call_or_xfail(
            provider.generate_text,
            "If 10 is greater than 5 reply with the single word ALPHA, otherwise reply BETA. "
            "Reply with only that one word.",
            max_tokens=256,
        )
        n = _norm(out)
        assert "alpha" in n and "beta" not in n, f"expected ALPHA, got: {out!r}"

    def test_returns_the_requested_json(self, provider):
        out = call_or_xfail(
            provider.generate_text,
            'Reply with ONLY this JSON object and nothing else: {"sum": N} '
            "where N is 2 plus 3.",
            max_tokens=256,
        )
        data = json.loads(_strip_code_fences(out))
        assert data.get("sum") == 5, f"expected sum=5, got: {out!r}"

    def test_translates_to_vietnamese(self, provider):
        out = call_or_xfail(
            provider.generate_text,
            "Translate the English word 'water' into Vietnamese. "
            "Output only the translation.",
            max_tokens=256,
        )
        # 'nước' — accept with/without the diacritic so the match is tolerant.
        n = _norm(out)
        assert "nư" in n or "nuoc" in n, f"expected 'nước', got: {out!r}"


# --- 5. ONE real suite PER LLM provider ------------------------------------------------
# Each provider gets its OWN class (so a failure is attributed to that provider by name, and
# the suite is run "for all providers" as the user asked) combining the structural contract +
# the behavior battery. Every class is marked ``llm`` (real quota) and auto-skips when that
# provider has no credentials configured — so a credentialed env runs them for real while an
# uncredentialed one reports a clear per-provider skip (never a fake pass).
@pytest.mark.llm
class _RealLLMProviderSuite(LLMProviderContract, RealLLMBehaviorMixin):
    """Shared body for the per-provider real suites. ``PROVIDER`` is set by each subclass; the
    leading underscore keeps pytest from collecting this base directly.
    """

    PROVIDER: str = ""

    @pytest.fixture
    def provider(self) -> LLMProvider:
        return real_llm_provider_for_or_skip(self.PROVIDER)


class TestGeminiVertexRealLLM(_RealLLMProviderSuite):
    PROVIDER = "gemini_vertex"


class TestGeminiRealLLM(_RealLLMProviderSuite):
    PROVIDER = "gemini"


class TestOpenAIRealLLM(_RealLLMProviderSuite):
    PROVIDER = "openai"


class TestOpenRouterRealLLM(_RealLLMProviderSuite):
    PROVIDER = "openrouter"


class TestOpenAICompatibleRealLLM(_RealLLMProviderSuite):
    PROVIDER = "openai_compatible"
