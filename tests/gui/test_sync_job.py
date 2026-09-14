"""Tests for one pull in flight: what it protects, and what it reports while it runs.

The user can put the window away and keep studying, so a pull outlives the window that started it
— which is the feature, and which is why these rules live on an object rather than in a dialog.

Driven without Qt: ``anki_compat.run_in_background`` is replaced by something that runs the work
inline, so the ordering is the real ordering and nothing is timing-dependent.
"""

from __future__ import annotations

import os

import pytest
from aqt_stubs import install_gui_stubs

install_gui_stubs()

from omnia.core.sync.package import PackageOffer, PackageRequest  # noqa: E402
from omnia.core.sync.progress import DONE, FAILED  # noqa: E402
from omnia.gui.sync import job as job_module  # noqa: E402


class _Client:
    """A source that hands over a package without a network."""

    def __init__(
        self,
        payload: bytes = b"apkg",
        *,
        fail: Exception | None = None,
        config: dict | None = None,
        note_types: tuple = (),
    ) -> None:
        self.payload = payload
        self.fail = fail
        self.config = config or {}
        self.note_types = note_types
        self.asked: list[PackageRequest] = []

    def request_package(self, request: PackageRequest) -> PackageOffer:
        self.asked.append(request)
        if self.fail is not None:
            raise self.fail
        return PackageOffer(
            id="pkg",
            bytes=len(self.payload),
            cards=3,
            notes=2,
            config=self.config,
            note_types=self.note_types,
        )

    def download_package(self, offer, destination, on_progress=None):
        with open(destination, "wb") as handle:
            for index in range(0, len(self.payload), 2):
                chunk = self.payload[index : index + 2]
                handle.write(chunk)
                if on_progress:
                    on_progress(min(len(self.payload), index + len(chunk)))
        return len(self.payload)


def _request() -> PackageRequest:
    return PackageRequest(decks=("Japanese",), note_types=("Basic",))


@pytest.fixture
def inline(monkeypatch):
    """Run the background half inline, so ordering is real and nothing waits on a clock."""
    calls: dict[str, object] = {}

    def run_in_background(op, *, on_success, on_failure=None, label=None, parent=None):
        calls["label"] = label
        try:
            value = op()
        except Exception as exc:
            if on_failure is None:
                raise
            on_failure(exc)
            return
        on_success(value)

    monkeypatch.setattr("omnia.core.anki_compat.run_in_background", run_in_background)
    yield calls
    job_module.forget()


@pytest.fixture
def applied(monkeypatch):
    """Record what the import step did, without touching a collection."""
    seen: dict[str, object] = {"backups": 0, "packages": []}

    from omnia.gui.sync.apply import ApplyResult

    def _make_result():
        result = ApplyResult()
        result.notes_added = 3
        return result

    def backup_first(reason=""):
        seen["backups"] = int(seen["backups"]) + 1
        seen["backup_reason"] = reason
        return True

    def apply_package(path, policy=""):
        seen["packages"].append((path, os.path.exists(path)))
        seen["policy"] = policy
        return _make_result()

    def apply_note_types(definitions):
        seen["note_types"] = tuple(definitions or ())
        return tuple(str(d.get("name", "")) for d in definitions or ())

    # The real ApplyResult, so its `summary` is what the tests read back — a stub with a fixed
    # summary would have hidden the missing clause this covers.

    def apply_config(repo, sections):
        seen["config"] = dict(sections or {})
        return tuple(sorted(sections or {}))

    monkeypatch.setattr("omnia.gui.sync.apply.backup_first", backup_first)
    monkeypatch.setattr("omnia.gui.sync.apply.apply_package", apply_package)
    monkeypatch.setattr("omnia.gui.sync.apply.apply_note_types", apply_note_types)
    monkeypatch.setattr("omnia.gui.sync.apply.apply_config", apply_config)
    return seen


class TestAPullThatWorks:
    def test_it_ends_done_with_a_sentence_about_what_arrived(self, inline, applied):
        job = job_module.start_pull(_Client(b"abcdefgh"), _request(), repo=None)

        assert job.snapshot().phase == DONE
        assert job.result == "3 new notes"

    def test_it_reports_progress_while_the_bytes_arrive(self, inline, applied):
        seen: list[int] = []
        client = _Client(b"a" * 20)
        job_module.start_pull(
            client, _request(), repo=None, on_change=lambda: seen.append(1)
        )

        # One callback per chunk plus the phase changes: a bar that only moves at the end is a
        # bar that jumps.
        assert len(seen) > 5

    def test_it_never_asks_for_a_modal_progress_window(self, inline, applied):
        # A label turns run_in_background into QueryOp.with_progress, which is Anki's
        # APPLICATION-MODAL window. With one up the user cannot close the picker, answer a card,
        # or open the settings dialog — so the Sync button filling up behind its label could only
        # ever be seen after the copy it was reporting had already finished. The whole point of
        # this job is that it runs while the user carries on.
        job_module.start_pull(_Client(), _request(), repo=None, machine="mac-mini")

        assert inline.get("label") is None


class TestWhatItProtects:
    def test_a_backup_is_taken_before_anything_is_written(self, inline, applied):
        job_module.start_pull(_Client(), _request(), repo=None, machine="mac-mini")

        assert applied["backups"] == 1
        assert "mac-mini" in str(applied["backup_reason"])

    def test_the_downloaded_file_is_gone_afterwards(self, inline, applied):
        job_module.start_pull(_Client(b"abcd"), _request(), repo=None)

        path, existed_during_import = applied["packages"][0]
        assert existed_during_import, "the import was handed a file that was not there"
        assert not os.path.exists(path), "the package was left on disk"

    def test_a_second_pull_is_refused_while_one_is_running(self, monkeypatch):
        # Two imports into one collection at once is a way to lose data that no backup makes
        # obvious afterwards. Refused with a sentence, never queued.
        started: list[object] = []
        monkeypatch.setattr(
            "omnia.core.anki_compat.run_in_background",
            lambda op, **kw: started.append(op),  # never completes
        )
        try:
            job_module.start_pull(_Client(), _request(), repo=None)

            with pytest.raises(job_module.PullRefusedError, match="already running"):
                job_module.start_pull(_Client(), _request(), repo=None)
        finally:
            job_module.forget()

    def test_a_finished_pull_does_not_block_the_next_one(self, inline, applied):
        job_module.start_pull(_Client(), _request(), repo=None)

        job_module.start_pull(_Client(), _request(), repo=None)  # must not raise


class TestWhenItGoesWrong:
    def test_a_source_that_refuses_ends_failed_with_its_sentence(self, inline, applied):
        from omnia.core.sync import SyncError

        job = job_module.start_pull(
            _Client(
                fail=SyncError("That access code does not open the other machine.")
            ),
            _request(),
            repo=None,
        )

        assert job.snapshot().phase == FAILED
        assert "access code" in job.result

    def test_nothing_is_imported_when_the_transfer_fails(self, inline, applied):
        from omnia.core.sync import SyncError

        job_module.start_pull(_Client(fail=SyncError("gone")), _request(), repo=None)

        assert applied["packages"] == []
        assert applied["backups"] == 0

    def test_an_unexpected_error_does_not_leak_a_traceback_at_the_user(
        self, inline, applied
    ):
        job = job_module.start_pull(
            _Client(fail=RuntimeError("something internal")), _request(), repo=None
        )

        assert "see the Omnia log" in job.result
        assert "RuntimeError" not in job.result

    def test_a_failed_import_says_nothing_was_changed(
        self, inline, applied, monkeypatch
    ):
        # The one thing the user needs to know, or they go looking for a half-imported deck.
        monkeypatch.setattr(
            "omnia.gui.sync.apply.apply_package",
            lambda path: (_ for _ in ()).throw(
                RuntimeError("the collection is locked")
            ),
        )

        job = job_module.start_pull(_Client(), _request(), repo=None)

        assert job.snapshot().phase == FAILED
        assert "Nothing was changed" in job.result

    def test_a_failed_import_still_cleans_the_file_up(
        self, inline, applied, monkeypatch
    ):
        paths: list[str] = []
        monkeypatch.setattr(
            "omnia.gui.sync.apply.apply_package",
            lambda path, policy="": paths.append(path)
            or (_ for _ in ()).throw(RuntimeError("no")),
        )

        job_module.start_pull(_Client(), _request(), repo=None)

        assert paths and not os.path.exists(paths[0])


class TestTheDuplicatePolicy:
    def test_it_reaches_the_import(self, inline, applied):
        job_module.start_pull(_Client(), _request(), repo=None, policy="override")

        assert applied["policy"] == "override"

    def test_none_chosen_leaves_the_import_to_its_own_default(self, inline, applied):
        # Which is KEEP. The safe one is safe in both places, so a missing choice cannot become
        # permission to overwrite by passing through an extra layer.
        job_module.start_pull(_Client(), _request(), repo=None)

        assert applied["policy"] == ""


class TestWhatArrivesBesideThePackage:
    """Settings and note type definitions cannot ride in an ``.apkg``.

    A review found all of this selected in the picker, announced in the confirmation, sent over
    the wire — and then dropped on the floor, because nothing on the target ever read it.
    """

    def test_settings_reach_the_config(self, inline, applied):
        values = {"note_types": [{"note_type": "Basic"}]}

        job = job_module.start_pull(
            _Client(config={"smart_notes": values}), _request(), repo="the-repo"
        )

        assert applied["config"] == {"smart_notes": values}
        assert job.snapshot().phase == DONE

    def test_note_type_definitions_are_created(self, inline, applied):
        definitions = ({"name": "Brand New", "id": 99},)

        job_module.start_pull(_Client(note_types=definitions), _request(), repo=None)

        assert applied["note_types"] == definitions

    def test_they_are_applied_AFTER_the_package(self, inline, applied):
        # So a note type that DID come with notes is already here and is left alone rather than
        # created a second time.
        order: list[str] = []
        applied["packages"] = _Recorder(order, "package")

        job_module.start_pull(
            _Client(note_types=({"name": "N"},)), _request(), repo=None
        )

        assert order and order[0] == "package"

    def test_a_definition_only_pull_says_what_it_did(self, inline, applied):
        # It used to report "nothing new — this machine already had it all", which is the
        # sentence the picker's strip AND the Sync button tooltip both show: the user was told
        # the thing that had just worked did not happen.
        job = job_module.start_pull(
            _Client(b"", note_types=({"name": "Brand New"},)), _request(), repo=None
        )

        assert "note type" in job.result
        assert "nothing" not in job.result

    def test_a_pull_with_no_package_at_all_still_applies_them(self, inline, applied):
        # Note types or settings alone: there is nothing to download, and inventing an empty file
        # to fetch would mean a progress bar for a transfer of zero bytes.
        client = _Client(b"", config={"auto_flip": {"delay": 3}})

        job = job_module.start_pull(client, _request(), repo=None)

        assert applied["packages"] == [], "a package was imported when none was sent"
        assert applied["config"] == {"auto_flip": {"delay": 3}}
        assert job.snapshot().phase == DONE


class _Recorder(list):
    """A list that notes when it is appended to, for checking the order of two steps."""

    def __init__(self, order: list, label: str) -> None:
        super().__init__()
        self._order = order
        self._label = label

    def append(self, item) -> None:
        self._order.append(self._label)
        super().append(item)


class TestFindingItAgain:
    def test_the_running_pull_can_be_found_without_the_window_that_started_it(
        self, monkeypatch
    ):
        # This is the feature: close the picker, keep studying, and the Sync button still knows.
        monkeypatch.setattr(
            "omnia.core.anki_compat.run_in_background", lambda op, **kw: None
        )
        try:
            job = job_module.start_pull(_Client(), _request(), repo=None)

            assert job_module.current() is job
        finally:
            job_module.forget()

    def test_a_finished_pull_is_kept_so_its_outcome_can_be_read(self, inline, applied):
        # The whole point is that the user walked away. Dropping the job the instant it succeeded
        # would mean the one moment they were not looking is the one moment it could be seen.
        job = job_module.start_pull(_Client(), _request(), repo=None)

        assert job_module.current() is job
        assert job_module.current().result == "3 new notes"

    def test_the_next_pull_replaces_the_finished_one(self, inline, applied):
        job_module.start_pull(_Client(), _request(), repo=None)

        second = job_module.start_pull(_Client(), _request(), repo=None)

        assert job_module.current() is second

    def test_a_failed_pull_is_kept_so_its_reason_can_still_be_read(
        self, inline, applied
    ):
        from omnia.core.sync import SyncError

        job_module.start_pull(_Client(fail=SyncError("nope")), _request(), repo=None)

        # Not dropped: the button has to be able to say why, and the user may not have been
        # looking at the window when it happened.
        assert job_module.current() is not None
        assert job_module.current().result == "nope"

    def test_closing_the_profile_forgets_everything(self, inline, applied):
        from omnia.core.sync import SyncError

        job_module.start_pull(_Client(fail=SyncError("nope")), _request(), repo=None)

        job_module.forget()

        assert job_module.current() is None
