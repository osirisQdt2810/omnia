"""The counted, pollable progress a background job publishes instead of a modal dialog."""

from __future__ import annotations

import threading

from omnia.core.progress import JobProgress, JobTracker, progress_service


class TestTheServiceName:
    def test_it_is_built_from_the_plugin_id(self):
        # A convention, not a registration API: the settings page asks for this name for every
        # card it draws, and a plugin with nothing to report simply never provides it.
        assert progress_service("smart_notes") == "smart_notes.progress"


class TestWhatAReaderSees:
    def test_an_unknown_total_has_no_percentage(self):
        # None rather than 0.0: a bar pinned at zero reads as stuck, so the caller shows an
        # indeterminate state instead of a lie.
        assert JobProgress(done=3, total=0).percent is None

    def test_a_known_total_is_a_percentage(self):
        assert JobProgress(done=3, total=12).percent == 25.0

    def test_it_cannot_exceed_a_hundred(self):
        # A job that advances past its announced total is a bug, not a reason to draw a bar
        # wider than its track.
        assert JobProgress(done=15, total=12).percent == 100.0

    def test_the_summary_counts_rather_than_only_shading(self):
        # "142 of 300" is the answer a person wants; a percentage alone hides whether the
        # remaining 30% is three notes or three hundred.
        progress = JobProgress(done=142, total=300, label="Generating", active=True)

        assert progress.summary() == "Generating 142 of 300"

    def test_an_idle_tracker_says_nothing(self):
        assert JobProgress().summary() == ""

    def test_a_stop_that_has_been_asked_for_says_so(self):
        # Distinct from finished: the job runs until it reaches a point where stopping is safe.
        progress = JobProgress(done=5, total=9, active=True, cancelled=True)

        assert progress.summary() == "Stopping… (5 of 9)"


class TestTheTracker:
    def test_a_finished_run_keeps_its_counts(self):
        # So the last frame drawn is the complete one. Zeroing here leaves a reader who polls
        # every 500ms showing an empty bar as the reward for finishing.
        tracker = JobTracker("Generating")
        tracker.start(4)
        tracker.advance(4)
        tracker.finish()

        done = tracker.snapshot()
        assert (done.done, done.total, done.active) == (4, 4, False)

    def test_starting_again_discards_the_last_run(self):
        tracker = JobTracker()
        tracker.start(4)
        tracker.advance(4)
        tracker.finish()

        tracker.start(10)

        assert tracker.snapshot().done == 0

    def test_a_new_run_clears_a_cancel_asked_for_in_the_last_one(self):
        # Otherwise the next batch stops on the previous batch's Cancel.
        tracker = JobTracker()
        tracker.start(4)
        tracker.cancel()
        tracker.finish()

        tracker.start(4)

        assert tracker.cancelled is False

    def test_advancing_by_nothing_changes_nothing(self):
        tracker = JobTracker()
        tracker.start(3)

        tracker.advance(0)
        tracker.advance(-2)

        assert tracker.snapshot().done == 0

    def test_the_job_can_see_a_cancel_it_was_asked_for(self):
        tracker = JobTracker()
        tracker.start(3)

        tracker.cancel()

        assert tracker.cancelled is True

    def test_a_snapshot_does_not_change_under_the_reader(self):
        # Handed out by value because the writer is on another thread — a live view would let a
        # bar draw `done` from after a `total` it does not match.
        tracker = JobTracker()
        tracker.start(2)
        taken = tracker.snapshot()

        tracker.advance(2)

        assert taken.done == 0

    def test_the_count_survives_many_threads_advancing_at_once(self):
        # The driver thread is the only writer today, and `advance` is still the one place a
        # lost update would silently under-report a batch that did finish.
        tracker = JobTracker()
        tracker.start(400)
        workers = [
            threading.Thread(target=lambda: [tracker.advance(1) for _ in range(100)])
            for _ in range(4)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        assert tracker.snapshot().done == 400


class TestRecordingAnAbsoluteCount:
    """For a caller that already keeps its own running total, as the batch driver does."""

    def test_it_sets_the_count(self):
        tracker = JobTracker()
        tracker.start(10)

        tracker.record(4)

        assert tracker.snapshot().done == 4

    def test_it_never_walks_backwards(self):
        # Two publishes racing on the way to the reader must not un-draw the bar.
        tracker = JobTracker()
        tracker.start(10)
        tracker.record(7)

        tracker.record(4)

        assert tracker.snapshot().done == 7
