"""Where a batch reports, and therefore whether Anki is usable while it runs.

The generation itself was always off the Qt main thread. What froze Anki was the PROGRESS: Anki's
dialog is ``ApplicationModal``, so while it is up the reviewer accepts no input. Choosing a
different surface is the whole of the fix, which is why these tests are about the choice and the
plumbing rather than about generation.
"""

from __future__ import annotations

from omnia.core.progress import JobTracker
from omnia.plugins.smart_notes.integration.progress import (
    BackgroundBar,
    ModalDialog,
    ProgressSurface,
    Silent,
    surface_for,
)


class TestChoosingTheSurface:
    def test_background_mode_gets_the_background_bar(self):
        assert isinstance(
            surface_for(background=True, tracker=JobTracker()), BackgroundBar
        )

    def test_the_dialog_is_what_background_mode_off_means(self):
        assert isinstance(
            surface_for(background=False, tracker=JobTracker()), ModalDialog
        )

    def test_background_mode_with_nowhere_to_report_falls_back_to_the_dialog(self):
        # Silently dropping the progress of a batch the user STARTED would leave them unable to
        # tell it apart from nothing having happened.
        assert isinstance(surface_for(background=True, tracker=None), ModalDialog)


class TestTheBackgroundBar:
    def test_it_writes_the_tracker_a_reader_polls(self):
        tracker = JobTracker("Generating")
        bar = BackgroundBar(tracker)

        bar.start(8)
        bar.publish(3, 8)

        assert tracker.snapshot().summary() == "Generating 3 of 8"

    def test_finishing_leaves_the_last_frame_up(self):
        tracker = JobTracker()
        bar = BackgroundBar(tracker)
        bar.start(2)
        bar.publish(2, 2)

        bar.finish()

        assert (tracker.snapshot().done, tracker.snapshot().active) == (2, False)

    def test_a_cancel_asked_for_by_a_reader_reaches_the_batch(self):
        # The Stop button on the card writes the tracker; the driver reads it between cohorts.
        tracker = JobTracker()
        bar = BackgroundBar(tracker)
        bar.start(5)

        tracker.cancel()

        assert bar.cancelled() is True

    def test_it_never_touches_the_qt_main_thread(self, monkeypatch):
        # The reason it can publish on every commit where the dialog has to coalesce — and the
        # reason a long batch costs the main thread nothing at all.
        import omnia.core.anki_compat as anki_compat

        def boom(_work):
            raise AssertionError("the background bar marshalled to the main thread")

        monkeypatch.setattr(anki_compat, "run_on_main", boom)
        bar = BackgroundBar(JobTracker())

        bar.start(3)
        bar.publish(1, 3)
        bar.finish()


class TestSilence:
    def test_it_says_nothing_and_never_cancels(self):
        quiet = Silent()

        quiet.start(4)
        quiet.publish(2, 4)
        quiet.finish()

        assert quiet.cancelled() is False


class TestTheBaseIsTheSilentOne:
    def test_a_surface_that_implements_nothing_still_works(self):
        # Deliberately not abstract: a batch must not fail because the thing drawing it does
        # not care about one of these.
        surface = ProgressSurface()

        surface.start(1)
        surface.publish(1, 1)
        surface.finish()

        assert surface.cancelled() is False


class TestThePluginPublishesItsProgress:
    """A reader finds the tracker by NAME, so nothing drawing it imports this plugin."""

    def _plugin_and_ctx(self):
        """A plugin and the minimum context its enable/disable path actually reads.

        Built here rather than imported from another test module: `tests` is not a package, so
        a cross-module import only resolves when the repo root happens to be on sys.path —
        true under `python -m pytest`, false under the bare `pytest` that CI runs.
        """
        import logging
        import tempfile
        from pathlib import Path

        from omnia.core.plugin import AddonPaths, PluginContext
        from omnia.core.providers import ProviderHub
        from omnia.core.reviewer.ease_pipeline import EasePipeline
        from omnia.core.reviewer.web_injector import WebInjector
        from omnia.plugins.smart_notes import SmartNotesPlugin
        from omnia.plugins.smart_notes.config import SmartNotesSettings

        tmp = Path(tempfile.mkdtemp())
        return SmartNotesPlugin(), PluginContext(
            plugin_id="smart_notes",
            settings=SmartNotesSettings(),
            log=logging.getLogger("omnia.test"),
            ease=EasePipeline(),
            web=WebInjector(),
            providers=ProviderHub(),
            paths=AddonPaths(tmp, tmp, tmp),
            config=None,  # not read by the enable/disable path exercised here
            reload_self=lambda: None,
        )

    def test_enable_publishes_and_disable_withdraws(self):
        from omnia.core import services
        from omnia.core.progress import progress_service

        name = progress_service("smart_notes")
        plugin, ctx = self._plugin_and_ctx()
        try:
            plugin.on_enable(ctx)
            assert isinstance(services.lookup(name), JobTracker)

            plugin.on_disable(ctx)

            assert services.lookup(name) is None
        finally:
            services.revoke(name)

    def test_the_same_tracker_survives_a_reload(self):
        """Because a batch outlives the plugin object that started it.

        ``PluginManager.reload`` rebuilds the plugin — saving a setting is enough — and a
        tracker owned by the instance would be replaced mid-batch by an empty one, so a running
        generation would appear to have stopped.
        """
        from omnia.core import services
        from omnia.core.progress import progress_service

        name = progress_service("smart_notes")
        plugin, ctx = self._plugin_and_ctx()
        try:
            plugin.on_enable(ctx)
            first = services.lookup(name)
            plugin.on_disable(ctx)

            rebuilt, ctx2 = self._plugin_and_ctx()
            rebuilt.on_enable(ctx2)

            assert services.lookup(name) is first
        finally:
            services.revoke(name)
