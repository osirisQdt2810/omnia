"""The user's own OpenAI-compatible endpoints: many at once, added and removed at runtime.

A self-hosted server and a colleague's differ by a URL, not by a protocol — so these are
CONFIGURED INSTANCES of one provider rather than provider types of their own. That is what lets
any number exist with no class, no registration and no code change, and it is the property
every test here is really about.
"""

from __future__ import annotations

import pytest

from omnia.core.config.models import (
    CUSTOM_PREFIX,
    LLMSettings,
    custom_provider_label,
    custom_provider_name,
)


class TestNamingOne:
    def test_a_label_becomes_a_prefixed_id(self):
        assert custom_provider_name("gpu-nha") == "custom:gpu-nha"

    def test_the_label_comes_back_out(self):
        assert custom_provider_label("custom:gpu-nha") == "gpu-nha"

    @pytest.mark.parametrize("provider", ["gemini", "openrouter", "", "customish"])
    def test_a_shipped_provider_is_not_a_custom_one(self, provider):
        assert custom_provider_label(provider) == ""

    def test_a_label_can_never_shadow_a_shipped_provider(self):
        """Somebody naming theirs "gemini" gets their server, and the real one is untouched.

        The prefix is what makes that true without validating labels against a list that would
        have to be kept in step with every provider ever added.
        """
        llm = LLMSettings.parse_obj(
            {"custom": {"gemini": {"base_url": "http://mine/v1"}}}
        )

        assert llm.subsection("custom:gemini").base_url == "http://mine/v1"
        # The shipped one is untouched, and is still its own settings type rather than having
        # been replaced by an OpenAI-compatible section wearing its name.
        shipped = llm.subsection("gemini")
        assert type(shipped).__name__ == "GeminiLLMSettings"
        assert not hasattr(shipped, "base_url")


class TestResolvingOne:
    def _llm(self, **custom):
        return LLMSettings.parse_obj({"custom": custom})

    def test_a_configured_endpoint_resolves(self):
        llm = self._llm(mine={"base_url": "http://x/v1", "text_model": "omnia-local"})

        assert llm.subsection("custom:mine").text_model == "omnia-local"

    def test_an_unknown_endpoint_is_none_rather_than_an_error(self):
        """The factory raises its own clear message later; config load must not brick.

        Same rule the shipped providers already follow for an unknown `provider` name.
        """
        assert self._llm().subsection("custom:never-made") is None

    def test_the_active_provider_may_be_a_custom_one(self):
        llm = LLMSettings.parse_obj(
            {"provider": "custom:mine", "custom": {"mine": {"base_url": "http://x/v1"}}}
        )

        assert llm.active().base_url == "http://x/v1"

    def test_they_are_listed_in_the_order_added(self):
        llm = self._llm(first={}, second={}, third={})

        assert llm.custom_providers() == [
            "custom:first",
            "custom:second",
            "custom:third",
        ]

    def test_none_configured_is_an_empty_list_not_a_missing_key(self):
        assert LLMSettings().custom_providers() == []


class TestBuildingOne:
    """The hub must ask the registry for the class that speaks the protocol, while the NAME
    only selects whose URL and key to speak it with."""

    def _hub(self, **custom):
        from omnia.core.providers import ProviderHub

        return ProviderHub(LLMSettings.parse_obj({"custom": custom}))

    def test_it_builds_as_an_openai_compatible_provider(self):
        hub = self._hub(mine={"base_url": "http://x/v1", "text_model": "m"})

        assert hub._llm_config("custom:mine")["provider"] == "openai_compatible"

    def test_it_carries_that_endpoints_own_config(self):
        hub = self._hub(
            a={"base_url": "http://a/v1", "text_model": "model-a"},
            b={"base_url": "http://b/v1", "text_model": "model-b"},
        )

        assert hub._llm_config("custom:a")["base_url"] == "http://a/v1"
        assert hub._llm_config("custom:b")["model"] == "model-b"

    def test_two_endpoints_do_not_share_a_configuration(self):
        # The whole point of "many": editing one must not reach the other.
        hub = self._hub(a={"base_url": "http://a/v1"}, b={"base_url": "http://b/v1"})

        assert (
            hub._llm_config("custom:a")["base_url"]
            != hub._llm_config("custom:b")["base_url"]
        )

    def test_a_shipped_provider_still_resolves_to_itself(self):
        hub = self._hub(mine={"base_url": "http://x/v1"})

        assert hub._llm_config("gemini")["provider"] == "gemini"


class TestOfferingThem:
    def test_they_are_appended_after_the_shipped_ones(self):
        """Shipped first because that is what a fresh profile has; custom in the order added,
        so the picker does not reshuffle when one is edited."""
        from omnia.core.providers.catalog import providers_with_custom
        from omnia.core.providers.llm import LLM_PROVIDERS

        offered = providers_with_custom(["custom:a", "custom:b"])

        assert offered[: len(LLM_PROVIDERS)] == list(LLM_PROVIDERS)
        assert offered[len(LLM_PROVIDERS) :] == ["custom:a", "custom:b"]

    def test_none_configured_leaves_the_list_alone(self):
        from omnia.core.providers.catalog import providers_with_custom
        from omnia.core.providers.llm import LLM_PROVIDERS

        assert providers_with_custom(None) == list(LLM_PROVIDERS)

    def test_each_gets_a_model_entry_of_its_own(self):
        """Without one the picker finds nothing for it — not even the configured model."""
        from omnia.core.providers.catalog import catalog_payload

        payload = catalog_payload(
            None, {"custom:a": "model-a"}, None, ["custom:a", "custom:b"]
        )

        assert payload["text_models"]["custom:a"] == ["model-a"]
        assert payload["text_models"]["custom:b"] == []


class TestAddingAndRemovingOne:
    """The runtime half: a new endpoint appears in the config, and a removed one takes its
    stored credential with it."""

    @pytest.fixture
    def repo(self, tmp_path):
        import shutil
        from pathlib import Path

        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository

        src = Path(__file__).resolve().parents[2] / "src" / "omnia" / "config"
        for template in src.glob("*.example.toml"):
            shutil.copy(template, tmp_path / template.name)
        return ConfigRepository(ConfigLoader(tmp_path))

    def test_adding_one_returns_its_provider_id(self, repo):
        assert repo.add_custom_provider("llm", "gpu-nha") == "custom:gpu-nha"

    def test_it_is_configured_immediately(self, repo):
        """Immediately, not after a reload: the card the page draws next comes from here."""
        repo.add_custom_provider("llm", "gpu-nha")

        assert repo.llm_settings().custom_providers() == ["custom:gpu-nha"]

    def test_several_coexist(self, repo):
        # The whole point of the feature.
        for label in ("gpu-nha", "ollama", "ban-A"):
            repo.add_custom_provider("llm", label)

        assert repo.llm_settings().custom_providers() == [
            "custom:gpu-nha",
            "custom:ollama",
            "custom:ban-A",
        ]

    def test_they_keep_separate_settings(self, repo):
        repo.add_custom_provider("llm", "a")
        repo.add_custom_provider("llm", "b")

        repo.set_provider_fields(
            "llm", "custom:a", [("base_url", "text", "http://a/v1")]
        )
        repo.set_provider_fields(
            "llm", "custom:b", [("base_url", "text", "http://b/v1")]
        )
        llm = repo.llm_settings()

        assert llm.subsection("custom:a").base_url == "http://a/v1"
        assert llm.subsection("custom:b").base_url == "http://b/v1"

    def test_a_duplicate_name_is_refused_rather_than_merged(self, repo):
        """Two endpoints sharing a name would be one endpoint, and the second Save would
        silently overwrite the first."""
        repo.add_custom_provider("llm", "mine")

        with pytest.raises(ValueError, match="already"):
            repo.add_custom_provider("llm", "mine")

    @pytest.mark.parametrize("label", ["", "   "])
    def test_a_blank_name_is_refused(self, repo, label):
        with pytest.raises(ValueError):
            repo.add_custom_provider("llm", label)

    def test_removing_one_leaves_the_others(self, repo):
        for label in ("a", "b", "c"):
            repo.add_custom_provider("llm", label)

        repo.remove_custom_provider("llm", "custom:b")

        assert repo.llm_settings().custom_providers() == ["custom:a", "custom:c"]

    def test_removing_one_forgets_its_secret(self, repo, tmp_path):
        """A credential outliving every reference to it is the kind of leftover nobody goes
        back and cleans up."""
        repo.add_custom_provider("llm", "mine")
        repo.set_provider_fields(
            "llm", "custom:mine", [("api_key", "secret", "sk-xyz")]
        )
        secrets = list((tmp_path / ".secrets").glob("*api_key*"))
        assert secrets, "the fixture did not actually store a secret"

        repo.remove_custom_provider("llm", "custom:mine")

        assert not [p for p in secrets if p.exists()]

    def test_a_shipped_provider_cannot_be_removed(self, repo):
        """Removing `gemini` would mean removing support for it, not removing a setting."""
        with pytest.raises(ValueError):
            repo.remove_custom_provider("llm", "gemini")

    def test_a_shipped_provider_still_writes_to_its_own_section(self, repo):
        # The routing must not capture everything just because it learned about nesting.
        repo.set_provider_fields("llm", "openrouter", [("api_key", "secret", "sk-1")])

        assert repo.llm_settings().openrouter.api_key == "sk-1"
