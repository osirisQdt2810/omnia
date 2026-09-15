"""The two guards around the settings panel's save: refuse bad values, still open on bad ones.

A feature section is validated LAZILY — `update_section` writes whatever it is handed. That is
fine for every other caller, and not fine for a panel a person types into, because the value
that fails is written, and then:

* `PluginManager._activate` catches the `ValidationError` and reports a failed enable, so the
  save looks like it worked while the feature quietly stops running; and
* `feature_settings` raises for the WHOLE section from then on, so the panel that would let the
  value be corrected cannot be opened — the button goes dead with nothing said.

The way back was hand-editing TOML. So: check before writing, and open anyway when the stored
section is already unreadable (which a save from an older build, or sync, can also cause).

The dialog is exercised through a subclass that sets the fields Qt would — there is no display
here to build a real one on, and the logic under test never touches Qt.
"""

from __future__ import annotations

import pytest

import omnia.plugins  # noqa: F401 — registers plugins so config_model resolves


@pytest.fixture
def repo(config_repo):
    """A writable repository over a throwaway config tree (conftest's, renamed for reading)."""
    return config_repo


class _Manager:
    """Just enough PluginManager for the two methods under test."""

    def __init__(self, repo, plugin):
        self._repo = repo
        self._plugin = plugin
        self.reloaded: list[str] = []

    @property
    def config(self):
        return self._repo

    def plugins(self):
        return [self._plugin]

    def reload(self, plugin_id: str) -> None:
        self.reloaded.append(plugin_id)


def _dialog(manager):
    """A SettingsDialog with its fields set directly — no Qt, no webview."""
    from aqt_stubs import install_gui_stubs

    install_gui_stubs()
    from omnia.gui.settings_dialog import SettingsDialog

    class _Dialog(SettingsDialog):
        def __init__(self) -> None:  # deliberately NOT SettingsDialog's
            self._manager = manager
            self._category_names = ["Reviewing", "AI"]
            self.pushed: list[str] = []

        def _push_card_state(self, plugin_id: str) -> None:
            self.pushed.append(plugin_id)

    return _Dialog()


def _plugin(plugin_id: str):
    """The registered plugin instance for ``plugin_id``."""
    from omnia.core.registry import get_registered

    return get_registered()[plugin_id]()


class TestASaveTheModelWouldRefuse:
    """`word_lookup.port` is bounded 1024-65535 and drawn as a number box, so 80 is typeable."""

    def test_it_is_refused(self, repo):
        dialog = _dialog(_Manager(repo, _plugin("word_lookup")))

        answer = dialog._on_save_config({"id": "word_lookup", "values": {"port": 80}})

        assert "error" in answer

    def test_the_message_names_the_field(self, repo):
        dialog = _dialog(_Manager(repo, _plugin("word_lookup")))

        answer = dialog._on_save_config({"id": "word_lookup", "values": {"port": 80}})

        assert "port" in answer["error"]

    def test_nothing_is_written(self, repo):
        # The whole point. A refusal that still persists the value leaves the panel
        # unopenable, which is the failure this guard exists to prevent.
        dialog = _dialog(_Manager(repo, _plugin("word_lookup")))
        before = repo.raw_section("word_lookup")

        dialog._on_save_config({"id": "word_lookup", "values": {"port": 80}})

        assert repo.raw_section("word_lookup") == before

    def test_the_panel_can_still_be_opened_afterwards(self, repo):
        # The refusal has to leave the section READABLE, or it has merely postponed the bug.
        plugin = _plugin("word_lookup")
        dialog = _dialog(_Manager(repo, plugin))

        dialog._on_save_config({"id": "word_lookup", "values": {"port": 80}})

        assert dialog._panel_payload(plugin)["fields"]

    def test_the_feature_is_not_reloaded(self, repo):
        # Nothing changed, so re-applying would only risk tearing a working feature down.
        manager = _Manager(repo, _plugin("word_lookup"))
        dialog = _dialog(manager)

        dialog._on_save_config({"id": "word_lookup", "values": {"port": 80}})

        assert manager.reloaded == []


class TestASaveTheModelAccepts:
    def test_it_is_written_and_re_applied(self, repo):
        manager = _Manager(repo, _plugin("word_lookup"))
        dialog = _dialog(manager)

        answer = dialog._on_save_config({"id": "word_lookup", "values": {"port": 8080}})

        assert answer == {}
        assert repo.raw_section("word_lookup")["port"] == 8080
        assert manager.reloaded == ["word_lookup"]


class TestASectionThatIsAlreadyUnreadable:
    """Not everything invalid came from this panel — an older build or a sync can leave one."""

    def test_the_panel_still_opens(self, repo):
        plugin = _plugin("overdue_guard")
        repo.update_section("overdue_guard", {"min_days": "soon"})
        dialog = _dialog(_Manager(repo, plugin))

        payload = dialog._panel_payload(plugin)

        assert payload["fields"], "the panel refused to open, so there is no way back"

    def test_the_unreadable_field_falls_back_to_something_the_control_can_hold(
        self, repo
    ):
        # A number box cannot show "soon". It shows the default, which is the one value that is
        # both representable and safe — and nothing is written until the reader presses Save.
        plugin = _plugin("overdue_guard")
        repo.update_section("overdue_guard", {"min_days": "soon"})
        dialog = _dialog(_Manager(repo, plugin))

        payload = dialog._panel_payload(plugin)
        shown = {field["key"]: field["value"] for field in payload["fields"]}

        assert isinstance(shown["min_days"], int)

    def test_the_readable_fields_beside_it_keep_their_stored_values(self, repo):
        # One bad key must not reset the whole section to defaults — the reader would save the
        # reset back over settings they never touched.
        plugin = _plugin("overdue_guard")
        repo.update_section("overdue_guard", {"min_days": "soon", "ratio": 0.55})
        dialog = _dialog(_Manager(repo, plugin))

        payload = dialog._panel_payload(plugin)
        shown = {field["key"]: field["value"] for field in payload["fields"]}

        assert shown["ratio"] == 0.55

    def test_and_the_correction_saves(self, repo):
        plugin = _plugin("overdue_guard")
        repo.update_section("overdue_guard", {"min_days": "soon"})
        dialog = _dialog(_Manager(repo, plugin))

        answer = dialog._on_save_config(
            {"id": "overdue_guard", "values": {"min_days": 7}}
        )

        assert answer == {}
        assert repo.feature_settings("overdue_guard").min_days == 7
