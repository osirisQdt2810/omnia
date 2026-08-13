"""Tests for the task registry, the config -> tasks bridge, and the plugin shell."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

import pytest

try:  # stdlib on Python 3.11+; the vendored tomli covers 3.10 (same fallback as loader.py)
    import tomllib  # noqa: TID251 - guarded here, which is what the ban asks for
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib  # type: ignore[no-redef]

from omnia.core.plugin import AddonPaths, PluginContext
from omnia.core.providers import ProviderHub
from omnia.core.reviewer.ease_pipeline import EasePipeline
from omnia.core.reviewer.web_injector import WebInjector
from omnia.plugins.note_maintenance import NoteMaintenancePlugin
from omnia.plugins.note_maintenance import registry as task_registry
from omnia.plugins.note_maintenance.base import (
    MaintenanceTask,
    NoteView,
    TaskConfigBase,
)
from omnia.plugins.note_maintenance.config import NoteMaintenanceSettings
from omnia.plugins.note_maintenance.runner import MaintenanceRunner

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


class _DemoConfig(TaskConfigBase):
    suffix: str = "!"


class _DemoTask(MaintenanceTask):
    config_model = _DemoConfig

    def process(self, note: NoteView) -> dict[str, str]:
        return {}


def _context(settings: NoteMaintenanceSettings) -> PluginContext:
    """A real PluginContext carrying ``settings`` (the seams are not exercised here)."""
    user_files = Path(tempfile.mkdtemp())
    return PluginContext(
        plugin_id="note_maintenance",
        settings=settings,
        log=logging.getLogger("omnia.test"),
        ease=EasePipeline(),
        web=WebInjector(),
        providers=ProviderHub(),
        paths=AddonPaths(user_files, user_files, user_files),
        config=None,  # this plugin reads config only through ctx.settings
        reload_self=lambda: None,
    )


def _bundled_task_configs() -> dict[str, Any]:
    """The ``tasks`` namespace exactly as the shipped ``features.example.toml`` declares it."""
    with (_CONFIG_DIR / "features.example.toml").open("rb") as handle:
        return tomllib.load(handle)["note_maintenance"]["tasks"]


@pytest.fixture
def clean_registry():
    """Isolate TASK_REGISTRY so a test's demo tasks don't leak into the real one."""
    snapshot = dict(task_registry.TASK_REGISTRY)
    task_registry.TASK_REGISTRY.clear()
    yield task_registry.TASK_REGISTRY
    task_registry.TASK_REGISTRY.clear()
    task_registry.TASK_REGISTRY.update(snapshot)


class TestRegisterTask:
    def test_registers_and_stamps_the_task_id(self, clean_registry):
        cls = task_registry.register_task("demo")(_DemoTask)
        assert cls.task_id == "demo"
        assert task_registry.registered_tasks() == {"demo": _DemoTask}

    def test_rejects_an_empty_task_id(self, clean_registry):
        with pytest.raises(ValueError):
            task_registry.register_task("")

    def test_rejects_a_duplicate_task_id(self, clean_registry):
        task_registry.register_task("demo")(_DemoTask)

        class _Other(_DemoTask):
            pass

        with pytest.raises(ValueError):
            task_registry.register_task("demo")(_Other)

    def test_registering_the_same_class_again_is_a_no_op(self, clean_registry):
        task_registry.register_task("demo")(_DemoTask)
        task_registry.register_task("demo")(_DemoTask)
        assert task_registry.registered_tasks() == {"demo": _DemoTask}


class TestBuildTasks:
    def test_uses_model_defaults_when_a_task_has_no_config(self, clean_registry):
        task_registry.register_task("demo")(_DemoTask)
        (task,) = task_registry.build_tasks({})
        assert (task.is_enabled, task.order, task.config.suffix) == (True, 100, "!")

    def test_applies_the_tasks_own_config(self, clean_registry):
        task_registry.register_task("demo")(_DemoTask)
        (task,) = task_registry.build_tasks(
            {"demo": {"enable": False, "order": 5, "suffix": "?"}}
        )
        assert (task.is_enabled, task.order, task.config.suffix) == (False, 5, "?")

    def test_ignores_an_entry_for_an_unknown_task(self, clean_registry):
        task_registry.register_task("demo")(_DemoTask)
        assert len(task_registry.build_tasks({"gone": {"enable": True}})) == 1


class TestUnknownTaskOptionRoundTrip:
    """ADR-010 one level down: an option key a NEWER Omnia added, inside a task's section.

    The mixed-version case the ADR is about — the newer device wrote the key, this one loads
    and rewrites the same blob. A task's own model is the layer that used to REJECT it (which
    cost the whole section its settings); it now has to carry the key through load AND back
    out, or the older device deletes the newer one's option on the next sync.
    """

    def test_the_task_still_builds(self, clean_registry):
        task_registry.register_task("demo")(_DemoTask)

        (task,) = task_registry.build_tasks(
            {"demo": {"suffix": "?", "brand_new_option": True}}
        )

        assert (task.is_enabled, task.config.suffix) == (True, "?")

    def test_the_unknown_key_survives_the_round_trip(self, clean_registry):
        task_registry.register_task("demo")(_DemoTask)

        (task,) = task_registry.build_tasks(
            {"demo": {"from_a_newer_omnia": {"nested": 1}}}
        )

        # ``.dict()`` is what a save writes back, so this is the whole promise.
        assert task.config.dict()["from_a_newer_omnia"] == {"nested": 1}


class TestBuildTasksTolerateBadConfig:
    """Both callers are Qt slots: a config this version can't parse must never raise there."""

    def test_an_invalid_option_falls_back_to_that_tasks_defaults(self, clean_registry):
        task_registry.register_task("demo")(_DemoTask)

        (task,) = task_registry.build_tasks({"demo": {"order": "whenever"}})

        assert (task.is_enabled, task.order, task.config.suffix) == (True, 100, "!")

    def test_the_fallback_keeps_a_task_the_user_switched_off(self, clean_registry):
        # The switches are not options: reverting ``enable`` would silently run a task over the
        # user's notes because ONE of its options could not be read.
        task_registry.register_task("demo")(_DemoTask)

        (task,) = task_registry.build_tasks(
            {"demo": {"enable": False, "order": 5, "suffix": ["not", "a", "string"]}}
        )

        assert (task.is_enabled, task.order) == (False, 5)
        assert task.config.suffix == "!"  # only the unreadable option reverts

    def test_the_fallback_keeps_every_option_it_can_read(self, clean_registry):
        # Not just the switches: an option that reverts is an option the settings panel then
        # SAVES as the default, so the user's own text would be destroyed by a bad neighbour.
        class _Pair(_DemoTask):
            class _Config(_DemoConfig):
                find: str = ""

            config_model = _Config

        task_registry.register_task("pair")(_Pair)

        (task,) = task_registry.build_tasks(
            {"pair": {"order": "whenever", "find": "PROMO", "suffix": "?"}}
        )

        assert (task.config.find, task.config.suffix) == ("PROMO", "?")
        assert task.order == 100  # only the unreadable one reverts

    def test_a_model_that_rejects_the_readable_subset_falls_back(self, clean_registry):
        # Readable field by field does not mean parseable together, and this is a Qt slot.
        from pydantic import root_validator

        class _Paired(_DemoTask):
            class _Config(_DemoConfig):
                other: str = "x"

                @root_validator()
                def _both_or_neither(cls, values):  # noqa: N805
                    if values.get("suffix") == "?" and values.get("other") != "y":
                        raise ValueError("suffix '?' needs other 'y'")
                    return values

            config_model = _Config

        task_registry.register_task("paired")(_Paired)

        (task,) = task_registry.build_tasks(
            {"paired": {"suffix": "?", "other": ["not", "a", "string"]}}
        )

        assert (task.config.suffix, task.config.other) == ("!", "x")

    def test_a_task_section_that_is_not_a_table_falls_back(self, clean_registry):
        # The settings panel hands over the RAW section, so an entry can be any shape at all.
        task_registry.register_task("demo")(_DemoTask)

        (task,) = task_registry.build_tasks({"demo": "a string"})

        assert (task.is_enabled, task.order, task.config.suffix) == (True, 100, "!")

    def test_the_fallback_keeps_a_task_the_user_switched_on(self, clean_registry):
        class _OffByDefault(_DemoTask):
            class _Config(_DemoConfig):
                enable: bool = False

            config_model = _Config

        task_registry.register_task("off_by_default")(_OffByDefault)

        (task,) = task_registry.build_tasks(
            {"off_by_default": {"enable": True, "suffix": {"not": "a string"}}}
        )

        assert task.is_enabled

    def test_an_unreadable_switch_falls_back_on_its_own(self, clean_registry):
        # A garbage ``order`` must not take a perfectly good ``enable`` down with it.
        task_registry.register_task("demo")(_DemoTask)

        (task,) = task_registry.build_tasks(
            {"demo": {"enable": False, "order": "whenever"}}
        )

        assert (task.is_enabled, task.order) == (False, 100)

    def test_only_the_broken_task_loses_its_settings(self, clean_registry):
        task_registry.register_task("demo")(_DemoTask)

        class _Other(_DemoTask):
            config_model = _DemoConfig

        task_registry.register_task("other")(_Other)

        demo, other = task_registry.build_tasks(
            {"demo": {"order": "whenever"}, "other": {"order": 5, "suffix": "?"}}
        )

        assert (demo.order, demo.config.suffix) == (100, "!")
        assert (other.order, other.config.suffix) == (5, "?")


class TestNoteMaintenancePlugin:
    def test_every_bundled_task_is_registered(self):
        assert set(task_registry.registered_tasks()) == {
            "strip_ipa",
            "extract_audio_file_name",
            "reformat_synonyms",
            "fill_first_example",
            "replace_text_all_fields",
        }

    def test_build_runner_uses_the_configured_tasks(self):
        plugin = NoteMaintenancePlugin()
        runner = plugin.build_runner()
        # No settings (plugin not enabled) -> every task at its MODEL defaults: in the shipped
        # order, and without replace_text_all_fields, which ships off.
        assert [task.task_id for task in runner.active_tasks] == [
            "reformat_synonyms",
            "strip_ipa",
            "extract_audio_file_name",
            "fill_first_example",
        ]

    def test_enable_applies_the_settings_and_disable_restores_the_defaults(self):
        plugin = NoteMaintenancePlugin()
        ctx = _context(NoteMaintenanceSettings(tasks={"strip_ipa": {"enable": False}}))
        default_active = len(plugin.build_runner().active_tasks)

        plugin.on_enable(ctx)
        assert len(plugin.build_runner().active_tasks) == default_active - 1

        plugin.on_disable(ctx)
        assert len(plugin.build_runner().active_tasks) == default_active


class TestBundledDefaults:
    """What the add-on SHIPS: the defaults in the models, and a run that settles in one pass."""

    def test_every_model_default_matches_the_bundled_toml(self):
        # The defaults live in each task's config MODEL; features.example.toml only mirrors
        # them. The collection config backend (ADR-006) never reads that file — it starts
        # fresh from the models — so the two layers must not drift apart.
        for task_id, values in _bundled_task_configs().items():
            config = task_registry.registered_tasks()[task_id]().config
            for option, expected in values.items():
                assert getattr(config, option) == expected, (task_id, option)

    def test_the_models_alone_ship_the_same_run_as_the_toml(self):
        from_models = MaintenanceRunner(task_registry.build_tasks({})).active_tasks
        from_toml = MaintenanceRunner(
            task_registry.build_tasks(_bundled_task_configs())
        ).active_tasks
        assert [task.task_id for task in from_models] == [
            task.task_id for task in from_toml
        ]

    def test_one_pass_reaches_a_fixed_point(self):
        runner = MaintenanceRunner(task_registry.build_tasks(_bundled_task_configs()))
        note = NoteView(
            note_id=1,
            note_type="Vocab",
            fields={
                "Synonyms": "modest, meek (ˈmɒdɪst, miːk)",
                "SynonymsNoIPA": "",
                "Dictionary Definition Audio": "[sound:modest_def.mp3]",
                "Dictionary Definition AudioNoTag": "",
                "First Example Audio": "[sound:modest_ex.mp3]",
                "First Example AudioNoTag": "",
                "Clozed First Example": "He was {{c1::modest}} about it.",
                "First Example": "",
            },
        )

        plan = runner.plan([note])
        assert plan.note_count == 1
        settled = note.with_updates(
            {field.field: field.after for field in plan.notes[0].fields}
        )
        # Everything the shipped tasks can do is done: a second run finds nothing left.
        assert runner.plan([settled]).is_empty
        assert settled.field("Synonyms") == "modest (ˈmɒdɪst), meek (miːk)"
        assert settled.field("SynonymsNoIPA") == "modest, meek"


class TestNonTableTaskEntryKeepsTheFeatureAlive:
    """A task entry that is not a table must not stop the plugin from enabling.

    ``PluginManager._activate`` builds the plugin context inside a try/except, so a
    ``ValidationError`` from ``feature_settings`` is swallowed into "the feature never enables"
    — the user loses the Browser entry with only a log line. ADR-010 keeps unknown *keys*
    working; this pins the same tolerance for an unexpected *shape*.
    """

    def test_settings_parse_and_the_entry_survives(self):
        from omnia.plugins.note_maintenance.config import NoteMaintenanceSettings

        settings = NoteMaintenanceSettings.parse_obj(
            {"tasks": {"strip_ipa": {"enable": False}, "weird": "a string"}}
        )
        assert settings.tasks["weird"] == "a string"
        assert settings.tasks["strip_ipa"] == {"enable": False}
        # and it round-trips, so an older build cannot strip it back out
        assert (
            NoteMaintenanceSettings.parse_obj(settings.dict()).tasks["weird"]
            == "a string"
        )

    def test_a_non_table_entry_reads_as_no_stored_options(self):
        from omnia.plugins.note_maintenance.registry import build_tasks

        tasks = build_tasks({"strip_ipa": "a string"})
        assert [t.task_id for t in tasks]  # the registry still yields runnable tasks
