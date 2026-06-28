"""Tests for the reviewer web injector (pure parsing/routing + asset collection)."""

from __future__ import annotations

from omnia.core.reviewer.web_injector import (
    MessageRouter,
    WebAsset,
    WebInjector,
    build_message,
    parse_message,
)


class TestMessageParsing:
    def test_parse_roundtrip(self):
        msg = build_message("typed_accuracy", "rated", {"cid": 7, "ease": 3})
        parsed = parse_message(msg)
        assert parsed is not None
        assert parsed.plugin == "typed_accuracy"
        assert parsed.op == "rated"
        assert parsed.data == {"cid": 7, "ease": 3}

    def test_parse_rejects_foreign_or_invalid(self):
        assert parse_message("notomnia:{}") is None
        assert parse_message("omnia:not json") is None
        assert parse_message("omnia:[1,2]") is None
        assert parse_message('omnia:{"op": "x"}') is None  # missing plugin


class TestMessageRouter:
    def test_router_dispatch_routes_to_handler(self):
        router = MessageRouter()
        seen = {}
        router.register("p", "op", lambda data, ctx: seen.update(data) or "ok")
        handled, result = router.dispatch(
            build_message("p", "op", {"a": 1}), context=None
        )
        assert handled is True
        assert result == "ok"
        assert seen == {"a": 1}

    def test_router_passes_through_unknown(self):
        router = MessageRouter()
        handled, result = router.dispatch(
            build_message("p", "missing", {}), context=None
        )
        assert handled is False
        assert result is None
        assert router.dispatch("not-ours", None) == (False, None)

    def test_router_unregister_plugin(self):
        router = MessageRouter()
        router.register("p", "a", lambda d, c: 1)
        router.register("p", "b", lambda d, c: 2)
        router.register("q", "a", lambda d, c: 3)
        router.unregister_plugin("p")
        assert router.dispatch(build_message("p", "a", {}), None) == (False, None)
        assert router.dispatch(build_message("q", "a", {}), None)[0] is True


class TestWebInjector:
    def test_collect_js_includes_side_specific_js_and_css(self):
        inj = WebInjector()
        inj.add_asset("p", WebAsset(css=".x{}", question_js="Q();", answer_js="A();"))
        q = inj.collect_js("question")
        a = inj.collect_js("answer")
        assert "Q();" in q and "A();" not in q
        assert "A();" in a and "Q();" not in a
        assert "omnia-style-p" in q  # css injected per-plugin

    def test_remove_clears_asset_and_handlers(self):
        inj = WebInjector()
        inj.add_asset("p", WebAsset(question_js="Q();"))
        inj.add_handler("p", "op", lambda d, c: 1)
        inj.remove("p")
        assert inj.collect_js("question") == ""
        handled, _ = inj._router.dispatch(build_message("p", "op", {}), None)
        assert handled is False
