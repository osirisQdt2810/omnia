"""Tests for this machine's sync identity: where it is kept, and what it refuses to publish.

One property carries the rest: these settings must never travel. ``features.toml`` rides the
collection and therefore AnkiWeb, so a sharing switch kept there would turn sharing on over on
the other machine too, and a key kept there would hand two machines one identity — which is the
whole of the access control.
"""

from __future__ import annotations

import pytest

from omnia.core.config.loader import CollectionConfigLoader, TomlConfigLoader
from omnia.core.config.repository import ConfigRepository
from omnia.core.sync import (
    DEFAULT_PORT,
    MachineIdentity,
    MachineSettings,
    machine_id,
    parse_pairing_code,
)


class _Repo:
    """A repository stand-in that records which SECTION was written, and remembers it."""

    def __init__(self) -> None:
        self.sections: dict[str, dict] = {}

    def raw_section(self, section: str) -> dict:
        return dict(self.sections.get(section, {}))

    def update_section(self, section: str, values: dict) -> None:
        self.sections.setdefault(section, {}).update(values)


class _FakeCol:
    """A stand-in collection exposing ``get_config``/``set_config`` over a plain dict."""

    def __init__(self) -> None:
        self.conf: dict[str, object] = {}

    def get_config(self, key, default=None):
        return self.conf.get(key, default)

    def set_config(self, key, value):
        self.conf[key] = value


@pytest.fixture(params=["toml", "collection"])
def repo(request, tmp_path) -> ConfigRepository:
    """A real repository on EACH backend.

    Parameterised rather than asserted on class constants, because a constant assertion is
    exactly what let a whole backend ship broken: ``machine.toml`` was added to the collection
    loader's merge order and not to the file loader's ``LIVE_FILES``, the test checked the
    former, and on the file backend every write landed on disk and was never read back — so the
    key was re-minted on every call and every ID was dead before it could be typed.
    """
    if request.param == "toml":
        return ConfigRepository(TomlConfigLoader(tmp_path))
    return ConfigRepository(
        CollectionConfigLoader(tmp_path, col_provider=lambda: _FakeCol())
    )


class TestWhereItIsKept:
    def test_the_sync_section_is_written_to_disk_not_to_the_collection(self):
        # The real routing decision, asked of the real repository: `features.toml` is a
        # collection domain, so a `sync` section there would sync — and both machines would
        # share one identity and one switch.
        assert ConfigRepository._file_for("sync") == "machine.toml"
        assert ConfigRepository._file_for("auto_flip") == "features.toml"

    def test_machine_toml_is_never_a_collection_domain(self):
        assert "machine.toml" not in CollectionConfigLoader._DB_FILES


class TestItSurvivesTheRoundTripOnEveryBackend:
    """Written, then read back — through a REAL repository, on both config backends."""

    def test_the_key_is_minted_once_and_read_back(self, repo):
        first = MachineSettings(repo).identity().key

        # A second MachineSettings over the same repository: this is the panel being reopened,
        # which is where a key that is not read back gets re-minted and kills every ID already
        # copied to the other machine.
        assert MachineSettings(repo).identity().key == first

    def test_the_sharing_switch_is_read_back(self, repo):
        MachineSettings(repo).set_sharing(True)

        assert MachineSettings(repo).identity().sharing is True

    def test_the_port_is_read_back(self, repo):
        MachineSettings(repo).set_port(9000)

        assert MachineSettings(repo).identity().port == 9000

    def test_regenerating_is_read_back_as_the_new_key(self, repo):
        before = MachineSettings(repo).identity().key

        after = MachineSettings(repo).regenerate().key

        assert after != before
        assert MachineSettings(repo).identity().key == after


class TestTheKey:
    def test_it_is_minted_on_first_use_and_then_kept(self):
        # Minted on READ rather than at install: a profile that never opens the panel has no
        # business holding a credential.
        repo = _Repo()
        settings = MachineSettings(repo)

        first = settings.identity().key
        second = settings.identity().key

        assert first and first == second
        assert repo.sections["sync"]["key"] == first

    def test_regenerating_replaces_it(self):
        # This is the revoke, and the only one there is.
        settings = MachineSettings(_Repo())
        before = settings.identity().key

        after = settings.regenerate().key

        assert after != before

    def test_an_unreadable_config_still_yields_an_identity(self):
        class _Broken(_Repo):
            def raw_section(self, section: str) -> dict:
                raise RuntimeError("the config file is a directory")

        # The panel has to render "not ready" rather than fail to open.
        assert MachineSettings(_Broken()).identity().key


class TestTheSwitchAndThePort:
    def test_sharing_is_off_in_a_fresh_profile(self):
        # ADR-020 condition 1: nothing offers to be pulled from until somebody says so.
        assert MachineSettings(_Repo()).identity().sharing is False

    def test_the_switch_is_remembered(self):
        settings = MachineSettings(_Repo())
        settings.set_sharing(True)

        assert settings.identity().sharing is True

    def test_the_port_defaults_and_is_remembered(self):
        settings = MachineSettings(_Repo())
        assert settings.identity().port == DEFAULT_PORT

        settings.set_port(9000)

        assert settings.identity().port == 9000

    def test_a_nonsense_port_falls_back_rather_than_breaking_sharing(self):
        repo = _Repo()
        repo.sections["sync"] = {"key": "a" * 32, "port": "nope"}

        assert MachineSettings(repo).identity().port == DEFAULT_PORT

    def test_a_port_out_of_range_falls_back_too(self):
        repo = _Repo()
        repo.sections["sync"] = {"key": "a" * 32, "port": 70000}

        assert MachineSettings(repo).identity().port == DEFAULT_PORT


class TestTheIdThisMachineShows:
    def _identity(self) -> MachineIdentity:
        return MachineIdentity(key="a" * 32, port=8767, sharing=True)

    def test_it_carries_the_best_address_and_the_key(self):
        code = machine_id(self._identity(), addresses=["192.168.1.40", "100.71.161.7"])

        address = parse_pairing_code(code)
        assert address.host == "100.71.161.7"  # the mesh address outranks the LAN one
        assert address.port == 8767
        assert address.token == "a" * 32

    def test_a_machine_with_no_dialable_address_shows_no_id(self):
        # A real answer, not a failure: an ID minted from loopback would never connect, and the
        # panel says so instead of handing out something that cannot work.
        assert machine_id(self._identity(), addresses=["127.0.0.1"]) == ""
