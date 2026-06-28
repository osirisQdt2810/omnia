"""Tests for the Anki-glue paths: WebInjector hook round-trip + anki_compat helpers.

These exercise the parts that touch the stubbed ``aqt`` (install/fire/uninstall, QueryOp
wrapping) rather than pure logic.
"""

from __future__ import annotations

import types

import pytest

from omnia.core import anki_compat
from omnia.core.reviewer.web_injector import WebAsset, WebInjector


class _FakeWeb:
    def __init__(self) -> None:
        self.evals: list[str] = []

    def eval(self, js: str) -> None:
        self.evals.append(js)


@pytest.fixture
def fake_mw(monkeypatch):
    """Install a fake ``mw`` with a reviewer web so injection can be observed."""
    import aqt

    web = _FakeWeb()
    mw = types.SimpleNamespace(reviewer=types.SimpleNamespace(web=web))
    monkeypatch.setattr(aqt, "mw", mw)
    return mw, web


class TestWebInjectorGlue:
    def test_web_injector_install_fire_uninstall(self, gui_hooks, fake_mw):
        _mw, web = fake_mw
        inj = WebInjector()
        inj.add_asset("p", WebAsset(question_js="Q();", answer_js="A();"))
        inj.install()

        assert gui_hooks.reviewer_did_show_question.count() == 1
        gui_hooks.reviewer_did_show_question.fire(object())
        assert any("Q();" in js for js in web.evals)

        gui_hooks.reviewer_did_show_answer.fire(object())
        assert any("A();" in js for js in web.evals)

        inj.uninstall()
        assert gui_hooks.reviewer_did_show_question.count() == 0
        assert gui_hooks.webview_did_receive_js_message.count() == 0

    def test_web_injector_bridge_routes_message(self, gui_hooks, fake_mw):
        inj = WebInjector()
        seen = {}
        inj.add_handler("p", "ping", lambda data, ctx: seen.update(data) or "pong")
        inj.install()

        from omnia.core.reviewer.web_injector import build_message

        msg = build_message("p", "ping", {"x": 1})
        result = gui_hooks.webview_did_receive_js_message.fire((False, None), msg, None)
        assert result == (True, "pong")
        assert seen == {"x": 1}
        inj.uninstall()


class TestRunInBackground:
    def test_run_in_background_success(self, fake_mw):
        results = []
        anki_compat.run_in_background(lambda: 21 * 2, on_success=results.append)
        assert results == [42]

    def test_run_in_background_failure_routes(self, fake_mw):
        errors = []

        def boom():
            raise ValueError("nope")

        anki_compat.run_in_background(
            boom, on_success=lambda r: None, on_failure=errors.append
        )
        assert len(errors) == 1
        assert isinstance(errors[0], ValueError)
