"""Tests for the shared OpenAI-family base URLs (``core/providers/openai_family.py``).

This table used to exist twice, byte for byte, in ``llm/openai_compatible.py`` and
``tts/openai_compatible.py``, and only the TTS copy was pinned by a test. That is the shape of
bug the duplication invites: change OpenRouter's base URL where you happen to be reading, ship,
and the *other* kind keeps posting to the old host with a valid key — so it surfaces as a
puzzling HTTP error rather than as a configuration problem.

So the assertions here are deliberately about BOTH kinds resolving through the one table, not
just about the table's contents.
"""

from __future__ import annotations

import pytest

from omnia.core.providers.llm.registry import create_llm_provider
from omnia.core.providers.openai_family import (
    OPENAI_FAMILY_BASE_URLS,
    openai_family_base_url,
)
from omnia.core.providers.tts.registry import create_tts_provider

#: The vendor endpoints as they stand. A hand-written literal on purpose: a test that derives
#: this from the module under test would pass even if every URL changed at once.
#:
#: ``openai_compatible`` is NOT here, and its absence is the point — see
#: :class:`TestAServerYouRunYourselfHasNoDefault`.
EXPECTED = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


class TestTheTable:
    def test_the_endpoints_are_what_we_think_they_are(self):
        assert OPENAI_FAMILY_BASE_URLS == EXPECTED

    def test_an_explicit_base_url_always_wins(self):
        out = openai_family_base_url(
            {"provider": "openrouter", "base_url": "http://localhost:1234/v1"}
        )

        assert out == "http://localhost:1234/v1"

    def test_an_empty_base_url_falls_back_rather_than_being_used(self):
        # `config.get("base_url") or ...` was the original behaviour; an empty string in a config
        # file must not become the base URL, or every request goes to a relative path.
        assert openai_family_base_url({"provider": "openrouter", "base_url": ""}) == (
            EXPECTED["openrouter"]
        )

    def test_a_config_with_no_provider_key_resolves_to_nothing(self):
        # ProviderHub._llm_config returns {} when settings are None, so this path is reachable.
        # It used to resolve to OpenAI's endpoint, which is a vendor nobody named.
        assert openai_family_base_url({}) == ""

    def test_an_unknown_name_gets_no_url_rather_than_openai(self):
        assert openai_family_base_url({"provider": "not_a_provider"}) == ""


class TestBothKindsResolveThroughIt:
    """The point of lifting the table: one edit changes both kinds, and a test proves it.

    Asserted through ``create_*_provider`` rather than by calling the helper twice, because what
    actually matters is that the built provider ends up pointing at the right host — one class
    serves all three names on each side, so the name is the only thing that distinguishes them.
    """

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_llm(self, name: str):
        provider = create_llm_provider({"provider": name, "api_key": "k"})

        assert provider._base_url == EXPECTED[name]

    @pytest.mark.parametrize("name", sorted(EXPECTED))
    def test_tts(self, name: str):
        provider = create_tts_provider({"provider": name, "api_key": "k"})

        assert provider._base_url == EXPECTED[name]

    @pytest.mark.parametrize("kind", ["llm", "tts"])
    def test_the_self_hosted_name_still_BUILDS_with_no_url(self, kind: str):
        """Construction stays total — the metadata sweep builds every provider from an empty
        config, and a provider that raises in its constructor cannot be inspected at all.
        """
        make = create_llm_provider if kind == "llm" else create_tts_provider
        provider = make({"provider": "openai_compatible", "api_key": "k"})

        assert provider._base_url == ""


class TestAServerYouRunYourselfHasNoDefault:
    """``openai_compatible`` used to fall back to OpenAI's own endpoint.

    The reasoning was that the reference implementation is the only sane guess. But the name
    means "some other server", so a user who picks it and leaves Base URL blank has not asked
    for OpenAI — and guessing sends their prompts, and the key they typed, to a third party
    they never named. Silently, and successfully, which is worse than a failure.
    """

    def test_the_table_no_longer_answers_for_it(self):
        assert "openai_compatible" not in OPENAI_FAMILY_BASE_URLS

    def test_an_explicit_url_is_still_used(self):
        out = openai_family_base_url(
            {"provider": "openai_compatible", "base_url": "http://127.0.0.1:8721/v1"}
        )

        assert out == "http://127.0.0.1:8721/v1"

    def test_calling_without_one_raises_where_it_can_be_fixed(self):
        from omnia.core.providers.errors import ProviderError
        from omnia.core.providers.openai_family import require_base_url

        with pytest.raises(ProviderError) as caught:
            require_base_url("")

        assert "Base URL" in str(caught.value)
        assert "Usage & keys" in str(
            caught.value
        ), "the message must say where to fix it"

    def test_a_configured_url_passes_through(self):
        from omnia.core.providers.openai_family import require_base_url

        assert require_base_url("http://x/v1") == "http://x/v1"


class TestTheLlmDefaultProvider:
    """The LLM fallback moved from a literal inside the factory into a constructor argument.

    TTS's equivalent was already pinned; LLM's was not, so a typo in
    ``ProviderRegistry("LLM", default=...)`` would have been caught by nothing.
    """

    def test_a_config_naming_no_provider_builds_the_openai_compatible_one(self):
        provider = create_llm_provider({"api_key": "k"})

        assert type(provider).__name__ == "OpenAICompatibleProvider"
        # No URL, because no vendor was named — the complaint comes at the call.
        assert provider._base_url == ""
