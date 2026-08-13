"""Tests for the task registry, the config -> tasks bridge, and the plugin shell."""

from __future__ import annotations

import logging
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import pytest

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

    def test_rejects_an_unknown_option(self, clean_registry):
        task_registry.register_task("demo")(_DemoTask)
        with pytest.raises(
            ValueError
        ):  # pydantic ValidationError subclasses ValueError
            task_registry.build_tasks({"demo": {"nope": 1}})


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
        # No settings (plugin not enabled) -> every task at its defaults, all enabled.
        assert len(runner.active_tasks) == len(task_registry.registered_tasks())

    def test_enable_applies_the_settings_and_disable_restores_the_defaults(self):
        plugin = NoteMaintenancePlugin()
        ctx = _context(NoteMaintenanceSettings(tasks={"strip_ipa": {"enable": False}}))
        total = len(task_registry.registered_tasks())

        plugin.on_enable(ctx)
        assert len(plugin.build_runner().active_tasks) == total - 1

        plugin.on_disable(ctx)
        assert len(plugin.build_runner().active_tasks) == total


class TestBundledDefaults:
    """The ordering the add-on SHIPS has to settle in a single pass over a note."""

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
        settled = note.with_updates(plan.notes[0].updates())
        # Everything the shipped tasks can do is done: a second run finds nothing left.
        assert runner.plan([settled]).is_empty
        assert settled.field("Synonyms") == "modest (ˈmɒdɪst), meek (miːk)"
        assert settled.field("SynonymsNoIPA") == "modest, meek"
