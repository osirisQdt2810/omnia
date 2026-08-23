"""Omnia's real-Anki smoke harness — the one place the add-on runs against real aqt/anki/Qt.

``tests/conftest.py`` deliberately STUBS ``aqt`` and ``anki`` so the pytest suite runs headless.
That makes a renamed ``gui_hooks`` signature, a PyQt6 API change, a moved dialog module, or an
import that only fails under Anki's own interpreter invisible to every one of those tests and
visible only here. Moving these checks into pytest would stub away the very thing they exist to
exercise, so this file is named ``run_smoke.py``: pytest collects only ``test_*.py``, so it lives
under ``tests/`` without being collected.

With all seven feature plugins enabled against a throwaway collection holding one forged overdue
card, it asserts behaviour rather than absence-of-exception:

* every plugin activates, and disabling them all leaves no trace — no ease transformer, no
  reviewer asset, no Tools-menu action, and a grade that passes straight through;
* the JS that actually reached the reviewer webview carries each injecting plugin's payload, on
  the side that plugin claims (and not on the other one);
* a real press of an answer button, routed through the live ``Reviewer._answerCard`` patch, comes
  out as the ease the rules say — typed_accuracy substitutes its staged grade, overdue_guard then
  caps it, in that order;
* each of the seven plugins has a step of its own, driving its real entry point (bridge message,
  menu hook, planner, loopback endpoint, dialog op);
* every dialog the settings screen's Configure button can open is constructed, discovered from
  the manager rather than a hard-coded list, plus the two dialogs reached from Anki's own menus.

It is NOT exhaustive over ``gui_hooks`` and does not claim to be: it fires the hooks its
assertions need. It is hermetic — no credentials, no network, no writes outside a temp directory.

Each step is isolated: a failure prints its traceback and the run continues. The exit code is
non-zero if any step failed.

Run with Anki's bundled interpreter:
    QT_QPA_PLATFORM=offscreen "<AnkiProgramFiles>/.venv/bin/python" tests/smoke/run_smoke.py
"""

from __future__ import annotations

import shutil
import socket
import sys
import tempfile
import time
import traceback
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
# ``import omnia`` must resolve to the SOURCE package, and its third-party deps come ONLY from the
# repo-root vendor tree: Anki's bundled Python has no pip packages, and ``src/omnia`` has no
# ``vendor/`` sibling until install_addon.py assembles one. Without the second line the very first
# plugin import dies on ``No module named 'pydantic'``. Mirrors tests/conftest.py.
sys.path.insert(0, str(_REPO / "src"))
sys.path.append(str(_REPO / "vendor" / "universal"))
# ``scripts/`` is on sys.path automatically for a script that LIVES there; from here it is not.
sys.path.insert(0, str(_REPO / "scripts"))

from common import enable_utf8_output  # noqa: E402

# Step labels carry non-ASCII, and Windows sizes stdout to the ANSI codepage — arm the console
# before the first print, or the run does all its work and then dies reporting it.
enable_utf8_output()

import aqt  # noqa: E402
from anki.collection import Collection  # noqa: E402
from aqt import gui_hooks  # noqa: E402
from aqt.qt import QApplication, QMainWindow, QMenu  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from omnia.core.config import ConfigLoader, ConfigRepository  # noqa: E402
from omnia.core.manager import PluginManager  # noqa: E402
from omnia.core.plugin import AddonPaths  # noqa: E402
from omnia.core.reviewer.ease_pipeline import EasePipeline  # noqa: E402
from omnia.core.reviewer.web_injector import WebInjector, build_message  # noqa: E402
from omnia.gui.config_form import PluginConfigDialog  # noqa: E402

# Anki card states (``card.type``): 0=new 1=learning 2=review 3=relearning.
_CARD_TYPE_REVIEW = 2
# The forged card's scheduling: a 10-day interval reviewed 8 days past due. 8/10 = 0.80 meets
# overdue_guard's default ratio (0.8) and clears its default minimum (2 days) with room to spare,
# so the rule fires on the numbers rather than on a boundary that a clock tick could flip.
_FORGED_IVL_DAYS = 10
_FORGED_LATE_DAYS = 8

_SECONDS_PER_DAY = 86_400

# Ease buttons, named so the grading assertions read as the rules they check.
_AGAIN, _HARD, _GOOD, _EASY = 1, 2, 3, 4

#: Per-side proof that a plugin's payload reached the reviewer CARD webview, keyed by the marker
#: its own asset carries. Only the two plugins that register a ``WebAsset`` appear: the others
#: reach the reviewer by different routes (display_interval writes the bottom bar; overdue_guard
#: and typed_accuracy grade through the ease pipeline) or do not touch it at all. Listing only
#: what is genuinely injected is the point — claiming more is what made the old docstring wrong.
_REVIEWER_JS_MARKERS: dict[str, dict[str, str]] = {
    "question": {"auto_flip": 'plugin: "auto_flip"'},
    "answer": {
        "auto_flip": 'plugin: "auto_flip"',
        "typed_accuracy": 'plugin: "typed_accuracy"',
    },
}


def _free_port() -> int:
    """Reserve an unused loopback port and return it.

    word_lookup's default port is 8766 — which is exactly the port the developer's OWN Anki is
    already serving while they run this. ``LookupService.start`` reports that failure rather than
    raising, and the plugin still lands in the active set, so an endpoint assertion on the default
    port fails for a reason that has nothing to do with Omnia.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SmokeRunner:
    """Runs labelled steps in isolation and owns the process exit code."""

    def __init__(self) -> None:
        self._failures: list[str] = []
        self._passed = 0

    def step(self, label: str, check: Callable[[], None]) -> None:
        """Run ``check``, reporting OK/FAIL.

        Catching ``Exception`` is what makes a bare ``assert`` a first-class failure mechanism:
        a behavioural step needs no machinery beyond the statement it wants to be true.
        """
        try:
            check()
        except Exception:
            self._failures.append(label)
            print(f"FAIL {label}")
            traceback.print_exc()
            print("-" * 72)
        else:
            self._passed += 1
            print(f"OK   {label}")

    def report(self) -> int:
        """Print the verdict and return the exit code (non-zero if any step failed)."""
        print("=" * 72)
        if self._failures:
            print(f"{len(self._failures)} FAILED step(s): {self._failures}")
            return 1
        print(f"ALL UI SMOKE STEPS PASSED ({self._passed} steps)")
        return 0


class RecordingWeb:
    """Stand-in for an ``AnkiWebView``: records every ``eval`` instead of running it.

    The recorded JS is the assertion surface. "Which plugin's payload actually reached which
    webview" is what a real Anki run would show and what no stubbed test can see — the previous
    harness collected exactly this and threw it away, printing only how many evals it had counted.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.evals: list[str] = []

    def eval(self, js: str) -> None:
        self.evals.append(js)

    def clear(self) -> None:
        """Drop everything recorded so far, so the next step asserts on a clean slate."""
        self.evals.clear()

    def joined(self) -> str:
        """Everything recorded since the last :meth:`clear`, as one string to search."""
        return "\n".join(self.evals)


class AnkiStandIn(QMainWindow):
    """Anki's ``mw``, standing in with real Qt over a real throwaway collection.

    A ``QMainWindow`` and not a ``SimpleNamespace``, because ``anki_compat`` builds
    ``QAction(label, mw)`` and PyQt6 rejects a non-``QObject`` parent. That single ``TypeError``
    stopped auto_flip from ever enabling in the previous harness: the manager's plugin-isolation
    boundary swallowed it, ``set_enabled`` returned a False nobody read, and the run reported all
    green with six of the seven features on and one leaking a web asset past teardown.
    """

    def __init__(self, col: Any, note: Any, card: Any, deck_id: int) -> None:
        super().__init__()
        self.col = col
        self.note = note
        self.card = card
        self.deck_id = deck_id
        self.card_web = RecordingWeb("reviewer card")
        self.bottom_web = RecordingWeb("reviewer bottom bar")
        self.web = RecordingWeb("main")
        self.reviewer = SimpleNamespace(
            card=card,
            state="answer",
            web=self.card_web,
            # display_interval writes its label into the PERSISTENT grading bar — a different
            # webview. Without ``bottom``, ``anki_compat.reviewer_bottom_eval`` silently no-ops
            # and the whole feature produces nothing observable.
            bottom=SimpleNamespace(web=self.bottom_web),
            # ``EasePipeline._answer_button_count`` reads reviewer.mw.col.sched.answerButtons();
            # without this it falls back to 4 and the clamp is never really exercised.
            mw=self,
            _showAnswer=lambda: None,
        )
        self.progress = SimpleNamespace(
            timer=lambda ms, cb, repeat: SimpleNamespace(stop=lambda: None),
            start=lambda **k: None,
            update=lambda **k: None,
            finish=lambda: None,
            want_cancel=lambda: False,
        )
        self.taskman = SimpleNamespace(
            # A no-op background runner is what keeps this harness OFFLINE: an op that would call
            # a provider is built and handed over, then never executed. ``run_on_main`` runs
            # inline so word_lookup's main-thread marshalling resolves for the HTTP worker.
            run_in_background=lambda *a, **k: None,
            run_on_main=lambda cb: cb(),
        )
        self.form = SimpleNamespace(menuTools=QMenu(self))

    # Anki's real ``mw`` counts in-flight background ops around every ``QueryOp``; the counters
    # are bookkeeping for its progress UI, so a stand-in only has to accept the calls — the op
    # itself still dies quietly in the no-op ``taskman`` above.
    def _increase_background_ops(self) -> None:
        return

    def _decrease_background_ops(self) -> None:
        return

    @classmethod
    def build(cls, workdir: Path) -> AnkiStandIn:
        """Create the collection, its single (overdue) card, and point ``aqt.mw`` at the result."""
        col = Collection(str(workdir / "collection.anki2"))
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = "front", "back"
        deck_id = col.decks.id("Default")
        col.add_note(note, deck_id)
        card = col.get_card(col.find_cards("")[0])
        cls._forge_overdue(col, card)
        standin = cls(col, note, col.get_card(card.id), deck_id)
        aqt.mw = standin  # what makes anki_compat.main_window() resolve
        return standin

    @staticmethod
    def _forge_overdue(col: Any, card: Any) -> None:
        """Turn the freshly added NEW card into a review card that is well past due.

        overdue_guard measures lateness from the card's last review, and
        ``anki_compat.card_last_review_ms`` reads the newest ``revlog`` row before falling back to
        ``card.mod``. Backdating ``mod`` does not work — ``col.update_card`` restamps it to now —
        so the review is written into ``revlog``, whose row id IS the review time in epoch
        milliseconds. Without this the collection's only card is NEW (``ivl`` 0),
        ``OverdueRule.is_overdue`` returns False for every input, and every overdue_guard
        assertion would pass while proving nothing.
        """
        card.type = _CARD_TYPE_REVIEW
        card.queue = _CARD_TYPE_REVIEW
        card.ivl = _FORGED_IVL_DAYS
        card.due = col.sched.today - _FORGED_LATE_DAYS
        card.reps = 5
        card.factor = 2500
        col.update_card(card)
        elapsed_days = _FORGED_IVL_DAYS + _FORGED_LATE_DAYS
        reviewed_ms = int((time.time() - elapsed_days * _SECONDS_PER_DAY) * 1000)
        # revlog columns: id(=review time ms), cid, usn, ease, ivl, lastIvl, factor, time, type.
        col.db.execute(
            "insert or replace into revlog values (?,?,?,?,?,?,?,?,?)",
            reviewed_ms,
            card.id,
            -1,
            _GOOD,
            _FORGED_IVL_DAYS,
            5,
            2500,
            8000,
            0,
        )


class OmniaSmoke:
    """Omnia wired up against the stand-in: the config, the two seams, and the manager.

    Owns the ``EasePipeline`` / ``WebInjector`` explicitly instead of letting the manager build
    its own, so the steps assert through the seams' public API rather than reaching into the
    manager's privates. The drive helpers below are the vocabulary the checks are written in:
    show a side, press a button, send a bridge message.
    """

    def __init__(self, anki: AnkiStandIn, workdir: Path) -> None:
        self.anki = anki
        self.repo = self._build_config(workdir)
        self.ease = EasePipeline()
        self.web = WebInjector()
        addon_dir = _REPO / "src" / "omnia"
        self.manager = PluginManager(
            self.repo,
            AddonPaths(addon_dir, addon_dir / "web", workdir),
            ease=self.ease,
            web=self.web,
        )
        self.graded: list[int] = []
        self._install_grade_recorder()

    @staticmethod
    def _build_config(workdir: Path) -> ConfigRepository:
        """A ConfigRepository over an isolated config dir seeded from the TRACKED templates.

        Deliberately hermetic: only ``*.example.toml`` is copied, so the run never reads the
        developer's live ``config/providers.toml`` and can never write to it. This harness
        exercises wiring, not credentials, and no step may start billing an API key.

        The two writes make otherwise environment-dependent steps deterministic — see
        :func:`_free_port` and :func:`_check_overdue_guard_caps_a_grade`.
        """
        config_dir = workdir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        for template in (_REPO / "config").glob("*.example.toml"):
            shutil.copy(template, config_dir / template.name)
        repo = ConfigRepository(ConfigLoader(config_dir))
        repo.update_section("overdue_guard", {"force_again_after_days": 0})
        repo.update_section("word_lookup", {"port": _free_port()})
        return repo

    def _install_grade_recorder(self) -> None:
        """Replace ``Reviewer._answerCard`` with a recorder BEFORE the pipeline wraps it.

        The ease pipeline is a one-time monkeypatch of the ``Reviewer._answerCard`` CLASS
        attribute, not a ``gui_hooks`` subscription: firing ``reviewer_will_answer_card`` can
        never reach it (auto_flip is that hook's only subscriber in the whole add-on, and it
        returns the ease unchanged). Anki's real ``_answerCard`` cannot run against a stand-in
        reviewer — it wants the scheduler, the undo stack and ``_answeredIds`` — so the recorder
        becomes the pipeline's ``orig``. Everything the wrap does then runs for real (the card
        lookup, the answer-button clamp, the fold), and the ease coming out the far end is a value
        a step can assert on.
        """
        from aqt.reviewer import Reviewer

        graded = self.graded
        Reviewer._answerCard = lambda reviewer, ease: graded.append(ease)

    # --- drive helpers ----------------------------------------------------------------
    def plugin_ids(self) -> list[str]:
        """Every registered plugin id, in the settings screen's display order."""
        return [plugin.id for plugin in self.manager.plugins()]

    def active_ids(self) -> set[str]:
        """The ids the manager currently reports as active."""
        return {pid for pid in self.plugin_ids() if self.manager.is_active(pid)}

    def plugin(self, plugin_id: str) -> Any:
        """The live plugin instance for ``plugin_id``."""
        return next(p for p in self.manager.plugins() if p.id == plugin_id)

    def show(self, side: str) -> str:
        """Fire the reviewer show-``side`` hook; return the JS that reached the CARD webview.

        Both recorders are cleared first, so a step reads only what this show produced.
        """
        self.anki.card_web.clear()
        self.anki.bottom_web.clear()
        self.anki.reviewer.state = side
        hook = (
            gui_hooks.reviewer_did_show_question
            if side == "question"
            else gui_hooks.reviewer_did_show_answer
        )
        hook(self.anki.card)
        return self.anki.card_web.joined()

    def grade(self, ease: int) -> int:
        """Press an answer button the way Anki's key handler does; return the ease actually graded."""
        from aqt.reviewer import Reviewer

        before = len(self.graded)
        Reviewer._answerCard(self.anki.reviewer, ease)
        assert len(self.graded) == before + 1, "the ease pipeline swallowed the answer"
        return self.graded[-1]

    def bridge(self, plugin_id: str, op: str, data: dict[str, Any]) -> tuple[bool, Any]:
        """Send a ``pycmd`` bridge message exactly as the reviewer webview does."""
        return gui_hooks.webview_did_receive_js_message(
            (False, None), build_message(plugin_id, op, data), None
        )

    def menu_labels(self, fire: Callable[[QMenu], None]) -> list[str]:
        """Run ``fire`` against a fresh ``QMenu`` and return the action labels it gained."""
        menu = QMenu()
        fire(menu)
        return [action.text() for action in menu.actions()]

    def editor_stub(self) -> SimpleNamespace:
        """A stand-in for Anki's ``Editor`` (field 0 = the note's first field)."""
        return SimpleNamespace(
            note=self.anki.note,
            web=self.anki.web,
            currentField=0,
            parentWindow=None,
            addButton=lambda **kw: "<button>",
            loadNote=lambda: None,
            loadNoteKeepingFocus=lambda: None,
        )


# --- steps ----------------------------------------------------------------------------
def _assert_markers(js: str, side: str) -> None:
    """Assert every plugin that injects into ``side`` left its payload in ``js``."""
    for plugin_id, marker in _REVIEWER_JS_MARKERS[side].items():
        assert marker in js, f"{side} side carries no {plugin_id} payload ({marker!r})"


def _check_setup(smoke: OmniaSmoke) -> None:
    """Installing the seams wraps the REAL ``Reviewer._answerCard`` exactly once."""
    from aqt.reviewer import Reviewer

    smoke.manager.setup()
    assert (
        Reviewer._answerCard.__qualname__ == "EasePipeline.install.<locals>._answerCard"
    ), f"the ease pipeline did not wrap Reviewer._answerCard: {Reviewer._answerCard!r}"


def _check_every_plugin_enables(smoke: OmniaSmoke) -> None:
    """Ticking every feature on must actually activate every feature.

    ``set_enabled``'s return value is the whole point: an ``on_enable`` that raises is swallowed
    by the manager's plugin-isolation boundary, so a discarded False is a feature that silently
    never turned on.
    """
    refused = sorted(
        pid for pid in smoke.plugin_ids() if not smoke.manager.set_enabled(pid, True)
    )
    assert not refused, f"set_enabled returned False for {refused}"
    assert smoke.active_ids() == set(
        smoke.plugin_ids()
    ), f"active {sorted(smoke.active_ids())} != registered {sorted(smoke.plugin_ids())}"
    print(f"     active: {sorted(smoke.active_ids())}")


def _check_question_side_injection(smoke: OmniaSmoke) -> None:
    """The question side carries auto_flip's media watcher — and nothing typed_accuracy owns."""
    js = smoke.show("question")
    _assert_markers(js, "question")
    assert "window.__omniaAutoFlipMedia" in js, "auto_flip's watcher guard is missing"
    assert (
        'plugin: "typed_accuracy"' not in js
    ), "typed_accuracy's ANSWER-side asset leaked onto the question side"


def _check_answer_side_injection(smoke: OmniaSmoke) -> None:
    """The answer side carries BOTH injecting plugins' payloads, in one eval."""
    js = smoke.show("answer")
    _assert_markers(js, "answer")
    assert (
        "typeans" in js
    ), "typed_accuracy's asset never reads Anki's typed-answer element"


def _check_display_interval_label(smoke: OmniaSmoke) -> None:
    """display_interval hides its label on the question and renders it on the answer.

    Its output goes to the grading bar, not the card webview, and it consults the ease pipeline
    to do so — so the rendered label is also evidence that overdue_guard's cap reached it.
    """
    smoke.show("question")
    hidden = smoke.anki.bottom_web.joined()
    assert "__TA_NEXT_IVL" in hidden, "no grading-bar JS on the question side"
    assert "interval: " not in hidden, "the label was rendered on the QUESTION side"

    smoke.show("answer")
    shown = smoke.anki.bottom_web.joined()
    assert (
        "__TA_NEXT_IVL" in shown and "interval: " in shown
    ), f"the answer side rendered no interval label: {shown!r}"
    # The preview the label is built from must reflect overdue_guard WITHOUT consuming anything.
    assert smoke.ease.compute_ease(smoke.anki.card, _GOOD, apply=False) == _HARD

    # Second surface: the value handed to card templates through the card_will_show filter.
    exposed = gui_hooks.card_will_show(
        "<div>card</div>", smoke.anki.card, "reviewAnswer"
    )
    assert (
        "window.omniaIntervals" in exposed
    ), "templates were given no interval payload"
    assert f'"current_days": {_FORGED_IVL_DAYS}' in exposed, exposed[:200]
    assert '"state": "review"' in exposed, exposed[:200]
    untouched = gui_hooks.card_will_show(
        "<div>card</div>", smoke.anki.card, "reviewQuestion"
    )
    assert untouched == "<div>card</div>", "the QUESTION html was rewritten"


def _check_overdue_guard_caps_a_grade(smoke: OmniaSmoke) -> None:
    """Pressing Easy on a very overdue card must come back Hard.

    ``force_again_after_days`` is set to 0 in the config so the answer is the rule's own constant
    rather than Anki's SM-2 prediction for a Hard press — that prediction moves with the card's
    due date and would make this assertion flake instead of check.
    """
    assert smoke.grade(_EASY) == _HARD, "overdue_guard did not cap an Easy press"
    assert smoke.grade(_GOOD) == _HARD, "overdue_guard did not cap a Good press"
    # An explicit Again is respected — the rule refuses to upgrade the user's own worst grade.
    assert smoke.grade(_AGAIN) == _AGAIN, "overdue_guard rewrote an explicit Again"


def _check_typed_accuracy_composes_with_overdue_guard(smoke: OmniaSmoke) -> None:
    """The two grading features cooperate through the pipeline, in priority order.

    typed_accuracy (100) substitutes the ease its staged typing score decided, then overdue_guard
    (200) caps it. One number proves both ran and ran in the right order — and no unit test can
    prove it against real ``aqt``. The staged ease is consumed exactly once, so the identical
    press afterwards produces a different, equally specified answer.
    """
    handled, result = smoke.bridge(
        "typed_accuracy", "rated", {"ratio": 0.9, "hasGood": True}
    )
    assert (handled, result) == (True, {"ok": True}), (handled, result)

    # Again(1) -> typed_accuracy substitutes Good(3) -> overdue_guard caps it to Hard(2).
    assert (
        smoke.grade(_AGAIN) == _HARD
    ), "the staged typing grade never reached the pipeline"
    # Nothing staged now: Again stands, because overdue_guard leaves an explicit Again alone.
    assert smoke.grade(_AGAIN) == _AGAIN, "the staged ease was applied more than once"


def _check_auto_flip_surfaces(smoke: OmniaSmoke) -> None:
    """auto_flip's Tools action, its per-deck gear entry, and its HTML5-media bridge."""
    tools = [action.text() for action in smoke.anki.form.menuTools.actions()]
    assert "Omnia · Auto-Flip (Ctrl+J)" in tools, tools

    gear = smoke.menu_labels(
        lambda menu: gui_hooks.deck_browser_will_show_options_menu(
            menu, smoke.anki.deck_id
        )
    )
    assert "Omnia: Auto-Flip…" in gear, gear

    plugin = smoke.plugin("auto_flip")
    handled, _ = smoke.bridge("auto_flip", "media_busy", {})
    assert handled, "the media_busy pycmd route is not registered"
    assert plugin._media_busy is True, "media_busy did not hold the countdown"
    smoke.bridge("auto_flip", "media_idle", {})
    assert plugin._media_busy is False, "media_idle did not release the countdown"


def _check_note_maintenance_plans_a_change(smoke: OmniaSmoke) -> None:
    """note_maintenance offers its Browser action and plans a real before/after.

    The plan is a proposal — nothing is written — so the note keeps the text the later
    word_lookup step searches for.
    """
    from omnia.plugins.note_maintenance import _note_views

    labels = smoke.menu_labels(
        lambda menu: gui_hooks.browser_will_show_context_menu(
            SimpleNamespace(selectedNotes=lambda: [smoke.anki.note.id]), menu
        )
    )
    assert "🧹 Omnia · Maintain Notes…" in labels, labels

    smoke.repo.update_section(
        "note_maintenance",
        {
            "note_types": {
                "Basic": {
                    "enable": True,
                    "tasks": {
                        "replace_text_all_fields": {
                            "enable": True,
                            "find": "front",
                            "replace": "FRONT",
                        }
                    },
                }
            }
        },
    )
    smoke.manager.reload("note_maintenance")
    planner = smoke.plugin("note_maintenance").build_planner()
    assert planner.has_runnable_note_type, "no note type resolved to a runnable task"

    plan = planner.plan(_note_views([smoke.anki.note.id]))
    assert plan.note_count == 1 and plan.field_count == 1, (
        plan.note_count,
        plan.field_count,
    )
    change = plan.notes[0].fields[0]
    assert (change.field, change.before, change.after) == (
        "Front",
        "front",
        "FRONT",
    ), change


def _check_word_lookup_endpoint(smoke: OmniaSmoke) -> None:
    """word_lookup answers a real HTTP request from the real collection.

    Asserted over the socket, not just by calling the plugin: the loopback service, its
    main-thread marshalling and its JSON shape are what the companion clipper actually depends
    on, and none of them exist in a stubbed test.
    """
    import json

    port = smoke.repo.feature_settings("word_lookup").port
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/lookup?word=front", timeout=10
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["found"] is True, payload
    assert [card["title"] for card in payload["cards"]] == ["front"], payload
    assert payload["cards"][0]["state"] == "review", payload["cards"][0]

    miss = smoke.plugin("word_lookup").lookup("nosuchword")
    assert miss == {
        "word": "nosuchword",
        "found": False,
        "truncated": False,
        "cards": [],
    }, miss


def _check_smart_notes_surfaces(smoke: OmniaSmoke) -> None:
    """smart_notes' editor button, its two context menus, and its dialog's offline ops.

    Auto-smart and generation are deliberately NOT driven: they call a real provider off-thread.
    Only the synchronous, network-free ops go through the same ``_on_cmd`` envelope the webview
    uses, and the saved rules are read back through the store that owns them.
    """
    from omnia.gui.smart_notes.dialogs import SmartNotesDialog
    from omnia.plugins.smart_notes.integration import SmartNotesStore

    buttons: list[Any] = []
    gui_hooks.editor_did_init_buttons(buttons, smoke.editor_stub())
    assert buttons, "smart_notes added no editor button"

    browser = smoke.menu_labels(
        lambda menu: gui_hooks.browser_will_show_context_menu(
            SimpleNamespace(selectedNotes=lambda: [smoke.anki.note.id]), menu
        )
    )
    assert "✨ Omnia · Generate Smart Fields" in browser, browser

    sidebar = smoke.menu_labels(
        lambda menu: gui_hooks.browser_sidebar_will_show_context_menu(
            SimpleNamespace(),
            menu,
            SimpleNamespace(
                item_type=SimpleNamespace(name="DECK"),
                full_name="Default",
                name="Default",
            ),
            0,
        )
    )
    assert "✨ Omnia · Generate Smart Fields" in sidebar, sidebar

    field_menu = smoke.menu_labels(
        lambda menu: gui_hooks.editor_will_show_context_menu(
            SimpleNamespace(editor=smoke.editor_stub()), menu
        )
    )
    assert "✨ Omnia · Generate this field" in field_menu, field_menu

    dialog = SmartNotesDialog(smoke.repo, None)

    def op(name: str, data: dict[str, Any]) -> Any:
        return dialog._on_cmd(build_message("smart_notes", name, data))

    names = op("list_note_types", {})
    assert names and "Basic" in names, names
    loaded = op("load", {"note_type": "Basic"})
    assert loaded["base_field"] == "Front", loaded
    assert [row["field"] for row in loaded["rows"]] == ["Back"], loaded
    assert loaded["providers"], "no LLM providers listed"
    rebased = op("set_base_field", {"note_type": "Basic", "base_field": "Back"})
    assert [row["field"] for row in rebased["rows"]] == ["Front"], rebased
    created = op("create_field", {"note_type": "Basic", "field_name": "Example"})
    assert "Example" in created.get("all_fields", []), created
    saved = op(
        "save",
        {
            "note_type": "Basic",
            "base_field": "Front",
            "rows": [
                {
                    "field": "Back",
                    "enabled": True,
                    "type": "text",
                    "prompt": "Define {{Front}}",
                },
                {
                    "field": "Example",
                    "enabled": True,
                    "type": "text",
                    "prompt": "Use {{Front}}",
                },
            ],
        },
    )
    assert saved == {"ok": True}, saved

    # Rules persist in the COLLECTION (synced), so the round-trip reads back through the store.
    config = SmartNotesStore().load().note_type_config("Basic")
    assert config is not None and config.base_field == "Front", config
    assert {rule.field for rule in config.generatable_fields()} == {
        "Back",
        "Example",
    }, config


def _check_every_configure_dialog(smoke: OmniaSmoke) -> None:
    """Construct what the settings screen's Configure button opens, for EVERY plugin.

    Mirrors ``SettingsDialog._configure``: a bespoke dialog when the plugin declares one, else the
    generic ``ConfigField`` form. Driven off ``manager.plugins()`` rather than a hard-coded id
    list, so plugin number eight is covered the day it is registered — a hard-coded tuple of three
    is exactly how "constructs every dialog" quietly stopped being true.
    """
    built: dict[str, str] = {}
    for plugin in smoke.manager.plugins():
        if plugin.has_custom_config_dialog():
            dialog = plugin.custom_config_dialog(smoke.repo, None)
            assert (
                dialog is not None
            ), f"{plugin.id}: custom_config_dialog returned None"
        else:
            schema = plugin.config_schema()
            assert (
                schema
            ), f"{plugin.id}: no custom dialog and no config schema to render"
            settings = smoke.repo.feature_settings(plugin.id)
            dialog = PluginConfigDialog(
                plugin.name or plugin.id,
                schema,
                settings.dict() if settings is not None else {},
                None,
            )
        built[plugin.id] = type(dialog).__name__
    assert set(built) == set(smoke.plugin_ids()), sorted(built)
    print(f"     {built}")


def _check_menu_reached_dialogs(smoke: OmniaSmoke) -> None:
    """The two dialogs reached from Anki's own menus rather than from Configure."""
    from omnia.gui.auto_flip.deck_options import AutoFlipDeckDialog
    from omnia.gui.settings_dialog import SettingsDialog
    from omnia.gui.smart_notes.dialogs import CustomPromptDialog

    AutoFlipDeckDialog(
        smoke.anki.deck_id, smoke.repo.feature_settings("auto_flip"), None
    )
    CustomPromptDialog(
        smoke.repo,
        kind="text",
        note_type="Basic",
        field_names=["Front", "Back"],
        target_field="Back",
        on_save=lambda _value: None,
        parent=None,
    )
    SettingsDialog(smoke.manager, None)


def _check_every_plugin_disables(smoke: OmniaSmoke) -> None:
    """Untick everything: a disabled feature must leave NO trace behind.

    This is the enforcement point for the teardown rule in CONVENTIONS Part 2 — and it cannot be
    checked anywhere else, because the traces are a real ``QMenu``, real ``gui_hooks`` and the
    live seams.
    """
    for plugin_id in smoke.plugin_ids():
        smoke.manager.set_enabled(plugin_id, False)

    assert smoke.active_ids() == set(), sorted(smoke.active_ids())
    assert not smoke.ease.has_transformers(), "an ease transformer outlived its plugin"
    assert (
        smoke.web.collect_js("question") == ""
    ), "a question-side asset was left behind"
    assert smoke.web.collect_js("answer") == "", "an answer-side asset was left behind"
    assert (
        smoke.anki.form.menuTools.actions() == []
    ), "a Tools-menu action was left behind"
    assert smoke.show("answer") == "", "the reviewer still receives JS"
    assert smoke.anki.bottom_web.evals == [], smoke.anki.bottom_web.evals
    assert smoke.grade(_EASY) == _EASY, "a grade is still being rewritten"


#: The run, in order. Later steps depend on earlier ones (the features must be on before their
#: surfaces exist; the staged typing grade must outlive no show-question, which clears it).
_STEPS: tuple[tuple[str, Callable[[OmniaSmoke], None]], ...] = (
    ("manager.setup() (seams installed on real aqt)", _check_setup),
    ("enable EVERY plugin (active set)", _check_every_plugin_enables),
    ("web injector · question side = auto_flip only", _check_question_side_injection),
    (
        "web injector · answer side = auto_flip + typed_accuracy",
        _check_answer_side_injection,
    ),
    (
        "display_interval · grading-bar label + template payload",
        _check_display_interval_label,
    ),
    ("overdue_guard · caps a real grade", _check_overdue_guard_caps_a_grade),
    (
        "typed_accuracy + overdue_guard · composed through the pipeline",
        _check_typed_accuracy_composes_with_overdue_guard,
    ),
    ("auto_flip · Tools action, deck gear, media bridge", _check_auto_flip_surfaces),
    (
        "note_maintenance · Browser action + planned change",
        _check_note_maintenance_plans_a_change,
    ),
    ("word_lookup · loopback endpoint answers", _check_word_lookup_endpoint),
    ("smart_notes · editor/menus + dialog ops", _check_smart_notes_surfaces),
    ("Configure dialog for EVERY plugin", _check_every_configure_dialog),
    ("AutoFlip deck / CustomPrompt / Settings dialogs", _check_menu_reached_dialogs),
    ("disable EVERY plugin (no trace left)", _check_every_plugin_disables),
)


def main() -> int:
    """Run every step against a throwaway collection; return the process exit code."""
    workdir = Path(tempfile.mkdtemp(prefix="omnia-smoke-"))
    anki = AnkiStandIn.build(workdir)
    smoke = OmniaSmoke(anki, workdir)
    runner = SmokeRunner()
    for label, check in _STEPS:
        runner.step(label, lambda check=check: check(smoke))
    anki.col.close()
    return runner.report()


# Guarded so the module can be IMPORTED for its stand-in and dialog-building helpers (the
# screenshot capture reuses them) without running the whole smoke as a side effect. Running the
# file directly is unchanged.
if __name__ == "__main__":
    sys.exit(main())
