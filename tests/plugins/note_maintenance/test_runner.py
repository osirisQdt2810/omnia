"""Tests for the note-maintenance runner: ordering, composition, skipping (pure logic)."""

from __future__ import annotations

from omnia.plugins.note_maintenance.base import (
    MaintenanceTask,
    NoteView,
    TaskConfigBase,
)
from omnia.plugins.note_maintenance.runner import (
    ChangePlan,
    FieldChange,
    MaintenanceRunner,
    NoteChange,
)


class _AppendConfig(TaskConfigBase):
    """Config for the test task: what to append, to which field."""

    field: str = "Word"
    suffix: str = "!"


class _AppendTask(MaintenanceTask):
    """Appends ``suffix`` to ``field`` — enough to observe order and composition."""

    task_id = "append"
    config_model = _AppendConfig

    def process(self, note: NoteView) -> dict[str, str]:
        return {self.config.field: note.field(self.config.field) + self.config.suffix}


class _NoopTask(MaintenanceTask):
    """Always returns "nothing to do"."""

    task_id = "noop"

    def process(self, note: NoteView) -> dict[str, str]:
        return {}


def _append(suffix: str, *, order: int = 100, enable: bool = True, field: str = "Word"):
    return _AppendTask(
        _AppendConfig(suffix=suffix, order=order, enable=enable, field=field)
    )


def _note(**fields: str) -> NoteView:
    return NoteView(note_id=1, note_type="Vocab", fields=dict(fields))


class TestMaintenanceRunner:
    def test_tasks_run_in_order(self):
        runner = MaintenanceRunner([_append("b", order=20), _append("a", order=10)])
        plan = runner.plan([_note(Word="x")])
        assert plan.notes[0].updates() == {"Word": "xab"}

    def test_ties_keep_the_given_order(self):
        runner = MaintenanceRunner([_append("a", order=5), _append("b", order=5)])
        assert runner.plan([_note(Word="x")]).notes[0].updates() == {"Word": "xab"}

    def test_two_tasks_on_one_field_compose(self):
        # The second task sees the first task's output, not the stored value.
        runner = MaintenanceRunner([_append("-one", order=1), _append("-two", order=2)])
        plan = runner.plan([_note(Word="w")])
        assert plan.notes[0].fields == (
            FieldChange(field="Word", before="w", after="w-one-two"),
        )

    def test_disabled_tasks_are_skipped(self):
        runner = MaintenanceRunner(
            [_append("a", order=1), _append("b", order=2, enable=False)]
        )
        assert [task.config.suffix for task in runner.active_tasks] == ["a"]
        assert runner.plan([_note(Word="x")]).notes[0].updates() == {"Word": "xa"}

    def test_task_returning_nothing_produces_no_entry(self):
        runner = MaintenanceRunner([_NoopTask()])
        plan = runner.plan([_note(Word="x")])
        assert plan.is_empty and plan.notes == ()

    def test_field_restored_to_its_original_value_is_not_a_change(self):
        class _RestoreTask(_AppendTask):
            def process(self, note: NoteView) -> dict[str, str]:
                return {"Word": note.field("Word")[:-1]}

        runner = MaintenanceRunner(
            [_append("!", order=1), _RestoreTask(_AppendConfig(order=2))]
        )
        assert runner.plan([_note(Word="x")]).is_empty

    def test_a_task_may_write_a_field_the_note_does_not_have_yet(self):
        runner = MaintenanceRunner([_append("!", field="Word")])
        plan = runner.plan([_note(Word="a"), _note(Other="b")])
        # The second note has no Word field, so the task creates one -> both notes change.
        assert plan.note_count == 2
        assert plan.notes[1].updates() == {"Word": "!"}

    def test_plan_counts_notes_and_fields(self):
        runner = MaintenanceRunner(
            [_append("!", field="Word"), _append("?", field="Meaning")]
        )
        plan = runner.plan([_note(Word="a", Meaning="b"), _note(Word="c", Meaning="d")])
        assert (plan.note_count, plan.field_count, plan.is_empty) == (2, 4, False)

    def test_the_source_note_is_never_mutated(self):
        note = _note(Word="x")
        MaintenanceRunner([_append("!")]).plan([note])
        assert note.fields == {"Word": "x"}

    def test_an_empty_plan_is_empty(self):
        assert ChangePlan().is_empty
        assert list(ChangePlan((NoteChange(1, ()),))) == [NoteChange(1, ())]
