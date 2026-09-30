"""The retired single self-hosted slot becomes a named endpoint, once per machine (ADR-022).

Keys used to show one built-in "Self-hosted / OpenAI-compatible" card beside the endpoints a
user adds by name. The card is gone. What was configured in it — ``[llm.openai_compatible]`` in
providers.toml and its key in ``.secrets/`` — moves into an endpoint called "Self-hosted" the
first time this build starts, and the old id stays a read-only alias: a Smart Notes field in the
SYNCED collection may pin it, and nothing here writes the collection.

A real ConfigRepository over a hand-written providers.toml throughout. The move is a file
rewrite, so what is on disk afterwards is the thing under test.
"""

from __future__ import annotations

import base64
import copy
import json

import pytest

from omnia.core.config.loader import (
    CollectionConfigLoader,
    ConfigLoader,
    read_toml,
    write_toml,
)
from omnia.core.config.models import LLMSettings, OpenAICompatibleLLMSettings
from omnia.core.config.repository import ConfigRepository
from omnia.core.config.secrets import SecretsStore
from omnia.core.providers import ProviderError, ProviderHub

_OLD_KEY_FILE = "llm.openai_compatible.api_key"
_NEW_KEY_FILE = "llm.custom%3ASelf-hosted.api_key"

#: The slot as a user of the old card left it: every field filled in, a setting this release has
#: never heard of, and a comment of their own.
_SLOT = """\
# the gpu box in the office
[llm]
provider = "openai_compatible"

[llm.openai_compatible]
base_url = "https://gpu.example/v1"
api_key = "secret:llm.openai_compatible.api_key"
text_model = "omnia-local"
image_model = "sdxl-turbo"
prompt_cache_control = true
future_knob = "kept"
"""

#: The same slot with no model named, so every model id is the slot's own default.
_SLOT_NAMING_NO_MODEL = _SLOT.replace('text_model = "omnia-local"\n', "").replace(
    'image_model = "sdxl-turbo"\n', ""
)

#: What the PREVIOUS release's template seeded into every install: a slot with nothing in it.
_OLD_TEMPLATE = """\
[llm]
provider = "gemini_vertex"

# Any other OpenAI-compatible endpoint (set base_url).
[llm.openai_compatible]
api_key = ""
base_url = ""
text_model = ""
image_model = ""
embedding_model = ""
"""


def _write(config_dir, text=_SLOT, key="tok-123"):
    """Lay down providers.toml and, unless ``key`` is None, the slot's stored key."""
    (config_dir / "providers.toml").write_text(text, encoding="utf-8")
    if key is not None:
        secrets = config_dir / ".secrets"
        secrets.mkdir(exist_ok=True)
        (secrets / _OLD_KEY_FILE).write_text(key, encoding="utf-8")


def _repo(config_dir):
    return ConfigRepository(ConfigLoader(config_dir))


def _raw(config_dir):
    return read_toml(config_dir / "providers.toml")


def _bytes(config_dir):
    return (config_dir / "providers.toml").read_bytes()


def _secret_files(config_dir):
    secrets = config_dir / ".secrets"
    return sorted(p.name for p in secrets.iterdir()) if secrets.exists() else []


def _built(settings, provider):
    """The provider the hub builds for ``provider``, unwrapped from its usage recorder."""
    return ProviderHub(llm_settings=settings).llm(provider=provider)._wrapped


def _requests(settings, provider):
    """Every HTTP call one text and one image generation through ``provider`` make."""
    from conftest import FakeHttpClient

    def answer(_method, url, _body, _headers):
        if url.endswith("/images/generations"):
            return {"data": [{"b64_json": base64.b64encode(b"png").decode()}]}
        return {"choices": [{"message": {"content": "ok"}}]}

    http = FakeHttpClient(responder=answer)
    built = ProviderHub(llm_settings=settings, http=http).llm(provider=provider)
    built.generate_text("Define {{Word}}", system="be brief")
    built.generate_image("a cat")
    return http.calls


class TestMovingTheSlot:
    def test_it_reports_where_the_slot_went(self, config_dir):
        _write(config_dir)

        assert _repo(config_dir).migrate_legacy_endpoint() == "Self-hosted"

    def test_everything_it_held_lands_in_the_endpoint(self, config_dir):
        _write(config_dir)

        _repo(config_dir).migrate_legacy_endpoint()
        endpoint = _raw(config_dir)["llm"]["custom"]["Self-hosted"]

        assert endpoint["base_url"] == "https://gpu.example/v1"
        assert endpoint["text_model"] == "omnia-local"
        assert endpoint["image_model"] == "sdxl-turbo"
        assert endpoint["prompt_cache_control"] is True
        # A key this release has never heard of is the user's, not ours to drop (ADR-010).
        assert endpoint["future_knob"] == "kept"

    def test_the_slot_remembers_where_it_went_and_the_endpoint_does_not(
        self, config_dir
    ):
        _write(config_dir)

        _repo(config_dir).migrate_legacy_endpoint()
        llm = _raw(config_dir)["llm"]

        assert llm["openai_compatible"]["moved_to"] == "Self-hosted"
        assert "moved_to" not in llm["custom"]["Self-hosted"]

    def test_a_model_the_slot_never_named_is_not_written_in(self, config_dir):
        """Only what the slot held is carried. Writing its defaults out would put
        `image_model = "gpt-image-1"`, a model no self-hosted server has, in the most visible
        endpoint there is — and the requests below are identical without it."""
        _write(config_dir, _SLOT_NAMING_NO_MODEL)

        _repo(config_dir).migrate_legacy_endpoint()

        written = _raw(config_dir)["llm"]["custom"]["Self-hosted"]
        assert not {"text_model", "image_model", "embedding_model"} & set(written)

    @pytest.mark.parametrize(
        "text",
        [_SLOT, _SLOT_NAMING_NO_MODEL],
        ids=["every model named", "every model left to default"],
    )
    def test_it_sends_exactly_the_requests_the_slot_sent(self, config_dir, text):
        """The round trip that matters: the same text and image request, byte for byte —
        address, key, model and every setting — before the move and after it."""
        _write(config_dir, text)
        repo = _repo(config_dir)
        before = _requests(repo.llm_settings(), "openai_compatible")

        repo.migrate_legacy_endpoint()

        assert _requests(repo.llm_settings(), "custom:Self-hosted") == before


class TestTheActiveProviderFollows:
    def test_the_retired_id_becomes_the_endpoints(self, config_dir):
        _write(config_dir)
        repo = _repo(config_dir)

        repo.migrate_legacy_endpoint()

        assert _raw(config_dir)["llm"]["provider"] == "custom:Self-hosted"
        assert repo.llm_settings().provider == "custom:Self-hosted"

    def test_another_active_provider_is_left_alone(self, config_dir):
        _write(
            config_dir,
            _SLOT.replace('provider = "openai_compatible"', 'provider = "gemini"'),
        )
        repo = _repo(config_dir)

        repo.migrate_legacy_endpoint()

        assert repo.llm_settings().provider == "gemini"
        assert repo.llm_settings().custom_providers() == ["custom:Self-hosted"]

    def test_an_older_build_writing_the_old_id_back_is_corrected(self, config_dir):
        """An older Omnia on this machine picks its old card again: the id comes back, and
        the next start points it at the endpoint again — without moving anything twice.
        """
        _write(config_dir)
        _repo(config_dir).migrate_legacy_endpoint()
        data = _raw(config_dir)
        data["llm"]["provider"] = "openai_compatible"
        write_toml(config_dir / "providers.toml", data)

        repo = _repo(config_dir)
        repo.migrate_legacy_endpoint()

        assert repo.llm_settings().provider == "custom:Self-hosted"
        assert repo.llm_settings().custom_providers() == ["custom:Self-hosted"]


class TestNothingToMove:
    """A slot with no address never worked, so there is nothing to carry over — and moving it
    anyway would put a phantom "Self-hosted" card in Keys on every install the old template
    seeded. Nothing is WRITTEN either, because tomli_w drops every comment in the file.
    """

    @pytest.mark.parametrize("base_url", ["", "   "])
    def test_a_slot_with_no_address_is_left_byte_for_byte(self, config_dir, base_url):
        # The slot is also the active provider: it must not be switched to anything else.
        _write(config_dir, _SLOT.replace("https://gpu.example/v1", base_url))
        before = _bytes(config_dir)
        repo = _repo(config_dir)

        assert repo.migrate_legacy_endpoint() == ""
        assert _bytes(config_dir) == before
        assert _secret_files(config_dir) == [_OLD_KEY_FILE]

    def test_what_the_old_template_seeded_is_left_byte_for_byte(self, config_dir):
        _write(config_dir, _OLD_TEMPLATE, key=None)
        before = _bytes(config_dir)

        assert _repo(config_dir).migrate_legacy_endpoint() == ""
        assert _bytes(config_dir) == before

    def test_a_fresh_install_writes_nothing(self, config_dir):
        repo = _repo(config_dir)  # seeds providers.toml from the shipped template
        before = _bytes(config_dir)

        assert repo.migrate_legacy_endpoint() == ""
        assert _bytes(config_dir) == before


class TestRunningItAgain:
    def test_the_next_start_changes_nothing(self, config_dir):
        _write(config_dir)
        _repo(config_dir).migrate_legacy_endpoint()
        after_the_move = _bytes(config_dir)

        assert _repo(config_dir).migrate_legacy_endpoint() == ""
        assert _bytes(config_dir) == after_the_move

    def test_an_endpoint_removed_after_the_move_stays_removed(self, config_dir):
        _write(config_dir)
        repo = _repo(config_dir)
        repo.migrate_legacy_endpoint()
        repo.remove_custom_provider("llm", "custom:Self-hosted")
        after_the_removal = _bytes(config_dir)

        repo = _repo(config_dir)

        assert repo.migrate_legacy_endpoint() == ""
        assert _bytes(config_dir) == after_the_removal
        assert repo.llm_settings().custom_providers() == []


class TestALabelAlreadyTaken:
    """An endpoint the user made is theirs: never edited, never merged into."""

    _THEIRS = '\n[llm.custom.self-hosted]\nbase_url = "http://theirs/v1"\n'

    def test_the_slot_takes_the_next_free_name(self, config_dir):
        # Case-insensitively taken: on APFS and NTFS the two would share one key file.
        _write(config_dir, _SLOT + self._THEIRS)

        assert _repo(config_dir).migrate_legacy_endpoint() == "Self-hosted 2"

    def test_theirs_is_left_exactly_as_it_was(self, config_dir):
        _write(config_dir, _SLOT + self._THEIRS)
        before = _raw(config_dir)["llm"]["custom"]["self-hosted"]

        _repo(config_dir).migrate_legacy_endpoint()

        assert _raw(config_dir)["llm"]["custom"]["self-hosted"] == before

    def test_the_count_goes_on_until_a_name_is_free(self, config_dir):
        two = '\n[llm.custom."Self-hosted 2"]\nbase_url = "http://two/v1"\n'
        _write(config_dir, _SLOT + self._THEIRS + two)

        assert _repo(config_dir).migrate_legacy_endpoint() == "Self-hosted 3"

    def test_the_active_provider_follows_to_the_name_it_got(self, config_dir):
        _write(config_dir, _SLOT + self._THEIRS)
        repo = _repo(config_dir)

        repo.migrate_legacy_endpoint()

        assert repo.llm_settings().provider == "custom:Self-hosted 2"


class TestTheKeyMoves:
    def test_one_file_named_for_its_owner_is_what_both_tables_name(self, config_dir):
        """Named for the endpoint, so removing it later shreds the key; named in the old
        table too, so an older build reading that table still finds a key behind it."""
        _write(config_dir)
        repo = _repo(config_dir)

        repo.migrate_legacy_endpoint()
        llm = _raw(config_dir)["llm"]

        assert _secret_files(config_dir) == [_NEW_KEY_FILE]
        assert llm["custom"]["Self-hosted"]["api_key"] == f"secret:{_NEW_KEY_FILE}"
        assert llm["openai_compatible"]["api_key"] == f"secret:{_NEW_KEY_FILE}"
        assert repo.llm_settings().subsection("custom:Self-hosted").api_key == "tok-123"

    def test_an_inline_key_leaves_no_plaintext_behind(self, config_dir):
        _write(
            config_dir,
            _SLOT.replace("secret:llm.openai_compatible.api_key", "sk-inline-123"),
            key=None,
        )
        repo = _repo(config_dir)

        repo.migrate_legacy_endpoint()

        text = (config_dir / "providers.toml").read_text(encoding="utf-8")
        assert "sk-inline-123" not in text
        assert repo.llm_settings().subsection("custom:Self-hosted").api_key == (
            "sk-inline-123"
        )

    @pytest.mark.parametrize(
        "ref",
        ["secret:nothing-behind-this", "secret-file:llm.openai_compatible.json"],
        ids=["dangling", "a file reference"],
    )
    def test_a_reference_with_no_key_behind_it_is_copied_verbatim(
        self, config_dir, ref
    ):
        _write(
            config_dir,
            _SLOT.replace("secret:llm.openai_compatible.api_key", ref),
            key=None,
        )

        _repo(config_dir).migrate_legacy_endpoint()
        llm = _raw(config_dir)["llm"]

        assert llm["custom"]["Self-hosted"]["api_key"] == ref
        assert llm["openai_compatible"]["api_key"] == ref
        assert _secret_files(config_dir) == []

    def test_removing_the_endpoint_later_shreds_the_only_copy(self, config_dir):
        _write(config_dir)
        repo = _repo(config_dir)
        repo.migrate_legacy_endpoint()

        repo.remove_custom_provider("llm", "custom:Self-hosted")

        assert _secret_files(config_dir) == []

    def test_a_key_file_another_table_still_names_is_kept(self, config_dir):
        """Only a key nothing points at any more is forgotten. The file names are the owners'
        own, so this takes a hand-edited providers.toml — which is exactly where a user who
        reused one key for two providers would have put it."""
        _write(
            config_dir,
            _SLOT
            + '\n[llm.openrouter]\napi_key = "secret:llm.openai_compatible.api_key"\n',
        )
        repo = _repo(config_dir)

        repo.migrate_legacy_endpoint()

        assert _OLD_KEY_FILE in _secret_files(config_dir)
        assert repo.llm_settings().openrouter.api_key == "tok-123"


class TestTheRetiredIdResolves:
    """A Smart Notes field pinned to `openai_compatible` lives in the synced collection, which
    nothing here writes — so the id has to go on meaning something."""

    def _moved(self, config_dir):
        _write(config_dir)
        repo = _repo(config_dir)
        repo.migrate_legacy_endpoint()
        # Edited after the move, so a value read through the old id can only have come from
        # the endpoint.
        repo.set_provider_fields(
            "llm", "custom:Self-hosted", [("base_url", "text", "http://moved/v1")]
        )
        return repo

    def test_it_reads_the_endpoints_values(self, config_dir):
        repo = self._moved(config_dir)

        assert repo.llm_settings().subsection("openai_compatible").base_url == (
            "http://moved/v1"
        )

    def test_it_builds_the_endpoint(self, config_dir):
        repo = self._moved(config_dir)

        built = _built(repo.llm_settings(), "openai_compatible")

        assert (built._base_url, built._api_key) == ("http://moved/v1", "tok-123")

    def test_a_removed_endpoint_says_so_rather_than_asking_for_a_key(self, config_dir):
        """Not "requires an api_key", which sends the user looking for a key they have."""
        repo = self._moved(config_dir)
        repo.remove_custom_provider("llm", "custom:Self-hosted")
        hub = ProviderHub(llm_settings=repo.llm_settings())

        with pytest.raises(ProviderError) as caught:
            hub.llm(provider="openai_compatible")

        assert "custom:Self-hosted" in str(caught.value)
        assert "api_key" not in str(caught.value)

    def test_an_unmoved_slot_with_an_address_still_builds(self):
        llm = LLMSettings.parse_obj(
            {"openai_compatible": {"base_url": "http://x/v1", "api_key": "k"}}
        )

        assert _built(llm, "openai_compatible")._base_url == "http://x/v1"

    @pytest.mark.parametrize(
        "pinned", ["openai_compatible", ""], ids=["a field's pin", "the default model"]
    )
    def test_an_unmoved_slot_with_no_address_says_where_it_went(self, pinned):
        from omnia.core.providers import _RETIRED_SLOT

        hub = ProviderHub(llm_settings=LLMSettings(provider="openai_compatible"))

        with pytest.raises(ProviderError) as caught:
            hub.llm(provider=pinned)

        assert str(caught.value) == _RETIRED_SLOT


class _Col:
    """A collection whose config is a dict, and which records every write."""

    def __init__(self, conf):
        self.conf = conf
        self.writes = []

    def get_config(self, key, default=None):
        return copy.deepcopy(self.conf.get(key, default))

    def set_config(self, key, value):
        self.writes.append(key)
        self.conf[key] = value


class TestAnOlderBuildStillReads:
    """The fleet is not upgraded at once, and the collection syncs between builds."""

    def test_the_synced_collection_is_never_written(self, config_dir):
        pinned = {
            "note_types": [
                {
                    "note_type": "Vocab",
                    "base_field": "Word",
                    "fields": [
                        {
                            "field": "Meaning",
                            "enabled": True,
                            "type": "text",
                            "provider": "openai_compatible",
                            "model": "omnia-local",
                        }
                    ],
                }
            ]
        }
        col = _Col(
            {
                "omnia:smart_notes": pinned,
                "omnia:config:omnia": {"plugins": {"smart_notes": {"enabled": True}}},
            }
        )
        before = json.dumps(col.conf)
        _write(config_dir)
        repo = ConfigRepository(
            CollectionConfigLoader(config_dir, col_provider=lambda: col)
        )

        assert repo.migrate_legacy_endpoint() == "Self-hosted"  # it did run
        assert col.writes == []
        assert json.dumps(col.conf) == before

    def test_the_moved_slot_still_reads_as_the_old_type(self, config_dir):
        """What a build from before this one parses the slot as: its URL and a key that
        resolves, with the extra `moved_to` carried along rather than refused."""
        _write(config_dir)
        _repo(config_dir).migrate_legacy_endpoint()

        old = OpenAICompatibleLLMSettings.parse_obj(
            _raw(config_dir)["llm"]["openai_compatible"]
        )

        assert old.base_url == "https://gpu.example/v1"
        assert SecretsStore(config_dir / ".secrets").resolve(old.api_key) == "tok-123"

    def test_the_endpoint_reads_as_the_type_the_previous_release_gave_it(
        self, config_dir
    ):
        _write(config_dir)
        _repo(config_dir).migrate_legacy_endpoint()

        endpoint = OpenAICompatibleLLMSettings.parse_obj(
            _raw(config_dir)["llm"]["custom"]["Self-hosted"]
        )

        assert endpoint.base_url == "https://gpu.example/v1"
        assert endpoint.text_model == "omnia-local"
