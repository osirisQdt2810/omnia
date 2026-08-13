"""Tests for the task registry, the config -> tasks bridge, and the plugin shell."""

from __future__ import annotations

import pytest

from omnia.plugins.note_maintenance import NoteMaintenancePlugin
from omnia.plugins.note_maintenance import registry as task_registry
from omnia.plugins.note_maintenance.base import (
    MaintenanceTask,
    NoteView,
    TaskConfigBase,
)


class _DemoConfig(TaskConfigBase):
    suffix: str = "!"


class _DemoTask(MaintenanceTask):
    config_model = _DemoConfig

    def process(self, note: NoteView) -> dict[str, str]:
        return {}


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
        assert task_registry.get_task("demo") is _DemoTask
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

    def test_disable_drops_the_context(self):
        plugin = NoteMaintenancePlugin()
        plugin.on_enable(None)  # the shell only stores the context; nothing is hooked
        plugin.on_disable(None)
        assert plugin._ctx is None
