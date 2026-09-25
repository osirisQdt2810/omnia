"""The user's own OpenAI-compatible endpoints: many at once, added and removed at runtime.

A self-hosted server and a colleague's differ by a URL, not by a protocol — so these are
CONFIGURED INSTANCES of one provider rather than provider types of their own. That is what lets
any number exist with no class, no registration and no code change, and it is the property
every test here is really about.
"""

from __future__ import annotations

import pytest

from omnia.core.config.models import (
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
    def repo(self, config_dir):
        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository

        return ConfigRepository(ConfigLoader(config_dir))

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


class TestSecretFilenamesSurviveEveryPlatform:
    """A custom endpoint's id contains a colon, which Windows forbids in a filename.

    Left unsanitised the write simply fails there and the credential is never stored — on the
    one platform where nobody would think to look. Found by the CI matrix's Windows leg rather
    than by reading the code.
    """

    def _name(self, provider):
        from omnia.core.config.repository import ConfigRepository

        return ConfigRepository._secret_name("llm", provider, "api_key")

    def test_a_colon_never_reaches_the_filesystem(self):
        assert ":" not in self._name("custom:mine")

    @pytest.mark.parametrize("char", list(':*?"<>|/\\'))
    def test_no_reserved_character_survives(self, char):
        assert char not in self._name(f"custom:na{char}me")

    def test_a_shipped_provider_keeps_its_existing_filename(self):
        """The substitution must not rename anything that already works — an existing secret
        whose file moved would read as a credential that vanished."""
        assert self._name("gemini") == "llm.gemini.api_key"
        assert self._name("gemini_vertex") == "llm.gemini_vertex.api_key"

    def test_two_endpoints_still_get_different_files(self):
        assert self._name("custom:a") != self._name("custom:b")


class TestACustomEndpointsKeyIsActuallyResolved:
    """A stored credential is written as a `secret:` reference and must be swapped for its
    value before a provider sees it.

    `_resolve_secrets` walked only the direct `BaseModel` attributes of `[llm]`, and the user's
    endpoints live in a `dict`. So the provider authenticated with the literal string
    `secret:llm.custom-mine.api_key` and every request was rejected — with a 401 from the
    server, which points at the key being wrong rather than at it never having been read.
    """

    @pytest.fixture
    def repo(self, config_dir):
        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository

        return ConfigRepository(ConfigLoader(config_dir))

    def test_the_stored_key_comes_back_as_its_value(self, repo):
        repo.add_custom_provider("llm", "mine")
        repo.set_provider_fields(
            "llm", "custom:mine", [("api_key", "secret", "sk-xyz")]
        )

        assert repo.llm_settings().subsection("custom:mine").api_key == "sk-xyz"

    def test_it_is_not_the_reference_string(self, repo):
        repo.add_custom_provider("llm", "mine")
        repo.set_provider_fields(
            "llm", "custom:mine", [("api_key", "secret", "sk-xyz")]
        )

        assert (
            not repo.llm_settings()
            .subsection("custom:mine")
            .api_key.startswith("secret:")
        )

    def test_each_endpoint_gets_its_own_key(self, repo):
        for label, key in (("a", "sk-a"), ("b", "sk-b")):
            repo.add_custom_provider("llm", label)
            repo.set_provider_fields(
                "llm", f"custom:{label}", [("api_key", "secret", key)]
            )
        llm = repo.llm_settings()

        assert llm.subsection("custom:a").api_key == "sk-a"
        assert llm.subsection("custom:b").api_key == "sk-b"

    def test_a_plain_field_is_left_alone(self, repo):
        repo.add_custom_provider("llm", "mine")
        repo.set_provider_fields(
            "llm", "custom:mine", [("base_url", "text", "http://x/v1")]
        )

        assert repo.llm_settings().subsection("custom:mine").base_url == "http://x/v1"

    def test_a_shipped_provider_still_resolves(self, repo):
        # The rewrite must not lose the case it already handled.
        repo.set_provider_fields("llm", "openrouter", [("api_key", "secret", "sk-1")])

        assert repo.llm_settings().openrouter.api_key == "sk-1"


class TestChoosingADefaultModelForACustomEndpoint:
    """The Account picker writes through `set_active_llm`, which wrote flat.

    A custom endpoint lives at `[llm.custom.<label>]`, so a flat write produced a SECOND table
    — `[llm."custom:mine"]` — that nothing reads. The picker accepted the choice, the file
    changed, and the setting had no effect: the worst shape a settings bug can take, because
    everything looks like it worked.
    """

    @pytest.fixture
    def repo(self, config_dir):
        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository

        return ConfigRepository(ConfigLoader(config_dir))

    def test_the_chosen_text_model_is_read_back(self, repo):
        repo.add_custom_provider("llm", "mine")

        repo.set_active_llm("custom:mine", text_model="llama-3.1")

        assert repo.llm_settings().subsection("custom:mine").text_model == "llama-3.1"

    def test_the_chosen_image_model_is_read_back(self, repo):
        repo.add_custom_provider("llm", "mine")

        repo.set_active_llm("custom:mine", image_model="my-sdxl")

        assert repo.llm_settings().subsection("custom:mine").image_model == "my-sdxl"

    def test_it_becomes_the_active_provider(self, repo):
        repo.add_custom_provider("llm", "mine")

        repo.set_active_llm("custom:mine", text_model="m")

        assert repo.llm_settings().provider == "custom:mine"

    def test_it_does_not_leave_a_second_table_nobody_reads(self, repo, tmp_path):
        repo.add_custom_provider("llm", "mine")

        repo.set_active_llm("custom:mine", text_model="m")
        written = (tmp_path / "providers.toml").read_text(encoding="utf-8")

        assert '[llm."custom:mine"]' not in written
        assert "[llm.custom.mine]" in written

    def test_it_does_not_disturb_the_endpoints_other_fields(self, repo):
        repo.add_custom_provider("llm", "mine")
        repo.set_provider_fields(
            "llm", "custom:mine", [("base_url", "text", "http://x/v1")]
        )

        repo.set_active_llm("custom:mine", text_model="m")

        assert repo.llm_settings().subsection("custom:mine").base_url == "http://x/v1"

    def test_a_shipped_provider_still_writes_to_its_own_table(self, repo):
        repo.set_active_llm("openrouter", text_model="openai/gpt-4o-mini")

        assert repo.llm_settings().openrouter.text_model == "openai/gpt-4o-mini"


class TestRemovingTheActiveEndpoint:
    """Deleting the endpoint the domain points at must not leave it pointing there.

    `active()` then returns None and the hub falls back to a bare `openai_compatible` with no
    base URL and no key, so every generation — and language-detect, ✨ Auto-prompt and
    ✦ Improve with it — fails with "requires an api_key". That names a credential, when what
    the user did was delete an endpoint. A shipped provider at least still reaches the clear
    "Unknown provider"; the `custom:` branch rewrites the name first and loses even that.
    """

    @pytest.fixture
    def repo(self, config_dir):
        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository

        repo = ConfigRepository(ConfigLoader(config_dir))
        repo.add_custom_provider("llm", "gpu")
        repo.set_active_llm("custom:gpu", text_model="llama-3.1")
        return repo

    def test_the_domain_stops_naming_it(self, repo):
        repo.remove_custom_provider("llm", "custom:gpu")

        assert repo.llm_settings().provider != "custom:gpu"

    def test_what_is_left_is_a_provider_that_resolves(self, repo):
        """Not merely "not the deleted one" — the fallback has to be usable."""
        from omnia.core.config.models import LLMSettings

        repo.remove_custom_provider("llm", "custom:gpu")

        assert repo.llm_settings().provider == LLMSettings().provider

    def test_removing_an_inactive_one_leaves_the_active_alone(self, repo):
        repo.add_custom_provider("llm", "spare")

        repo.remove_custom_provider("llm", "custom:spare")

        assert repo.llm_settings().provider == "custom:gpu"

    def test_a_removed_endpoint_cannot_be_written_back_into_existence(self, repo):
        """The Account panel holds a picker built when the dialog opened, so it still offers an
        endpoint that has just been deleted. Choosing a model for it used to write `text_model`
        to a section created on the spot — and a card reappeared in Keys for an endpoint with
        no URL, no key, and a credential already shredded. Removal has to stay removed.
        """
        repo.remove_custom_provider("llm", "custom:gpu")

        with pytest.raises(ValueError):
            repo.set_active_llm("custom:gpu", text_model="llama-3.1")

        assert not repo.llm_settings().custom_providers()

    def test_a_shipped_provider_is_still_written_on_demand(self, repo):
        """Its name is compiled in, so an absent section only means nothing was written yet."""
        repo.set_active_llm("gemini", text_model="gemini-2.5-flash")

        assert repo.llm_settings().subsection("gemini").text_model == "gemini-2.5-flash"


class TestOneSecretFilePerEndpoint:
    """Two labels must not share one credential file.

    Every character Windows forbids was mapped to ``-``, which is not injective: ``a:b`` and
    ``a-b`` landed on the same name, so adding the second endpoint overwrote the first one's
    key — silently, and with no way to tell which endpoint the surviving key belonged to.
    """

    def _name(self, provider):
        from omnia.core.config.repository import ConfigRepository

        return ConfigRepository._secret_name("llm", provider, "api_key")

    def test_two_labels_one_character_apart_do_not_collide(self):
        assert self._name("custom:a:b") != self._name("custom:a-b")

    def test_a_shipped_providers_filename_is_untouched(self):
        """It contains none of these characters, so existing secrets keep resolving."""
        assert self._name("gemini") == "llm.gemini.api_key"

    def test_nothing_windows_forbids_survives(self):
        from omnia.core.config.repository import ConfigRepository

        name = self._name('custom:a*b?c"d<e>f|g/h\\i')

        assert not set(name) & set(ConfigRepository._UNSAFE_IN_FILENAMES)

    def test_an_encoded_name_cannot_be_spelt_literally(self):
        """`%` itself is escaped first, or a literal "%3A" and an encoded ":" would collide."""
        assert self._name("custom:a%3Ab") != self._name("custom:a:b")


class TestEveryWriterNestsACustomEndpoint:
    """`_provider_table` exists so there is ONE place that knows a custom endpoint nests.

    A flat write produces a second table, ``[llm."custom:mine"]``, that nothing reads: the
    setting is accepted, the file changes, and nothing happens.
    """

    @pytest.fixture
    def repo(self, config_dir):
        from omnia.core.config.loader import ConfigLoader
        from omnia.core.config.repository import ConfigRepository

        repo = ConfigRepository(ConfigLoader(config_dir))
        repo.add_custom_provider("llm", "mine")
        return repo

    def test_a_raw_field_write_lands_where_it_is_read_from(self, repo):
        repo._write_provider_field(
            "llm", "custom:mine", "base_url", "http://127.0.0.1:8721/v1"
        )

        assert (
            repo.llm_settings().subsection("custom:mine").base_url
            == "http://127.0.0.1:8721/v1"
        )

    def test_it_leaves_no_second_table(self, repo):
        repo._write_provider_field("llm", "custom:mine", "base_url", "http://x/v1")

        assert "custom:mine" not in repo._loader.read_file("providers.toml")["llm"]
