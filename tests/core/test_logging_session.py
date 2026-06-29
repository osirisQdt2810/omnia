"""Tests for the switchable run-capture logging session (offline, tmp_path)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnia.core.logging.session import (
    AsyncLoggingSession,
    LoggingSession,
    get_logging_session,
    logging_session,
)


class TestDisabledSession:
    """A disabled session must be a complete no-op."""

    def test_disabled_records_nothing(self, tmp_path: Path) -> None:
        session = LoggingSession("disabled", enable=False, run_dir=str(tmp_path))
        session.timing("stage", 1.0)
        session.metric("count", 3)
        session.io("call", request={"a": 1}, response={"b": 2})
        session.flush()
        assert session._timings == []
        assert session._metrics == []
        assert session._io == []

    def test_disabled_writes_no_files(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        session = LoggingSession("disabled", enable=False, run_dir=str(run_dir))
        assert session.save_text("note.txt", "hello") is None
        assert session.save_json("note.json", {"x": 1}) is None
        session.flush()
        assert not run_dir.exists()


class TestEnabledRecording:
    """An enabled session records timings/metrics/IO and flushes a report."""

    def test_records_timing_metric_io(self, tmp_path: Path) -> None:
        session = LoggingSession("rec", run_dir=str(tmp_path))
        session.timing("generate", 0.5)
        session.metric("notes", 7)
        session.io("generate", request={"q": "x"}, response={"a": "y"})
        assert session._timings == [("generate", 0.5)]
        assert session._metrics == [("notes", 7)]
        assert session._io == [("generate", {"q": "x"}, {"a": "y"})]

    def test_flush_writes_report_and_io(self, tmp_path: Path) -> None:
        session = LoggingSession("flow", run_dir=str(tmp_path))
        session.timing("generate", 0.25)
        session.metric("notes", 2)
        session.io("generate", request={"q": "x"}, response={"a": "y"})
        session.flush()

        metrics_json = tmp_path / "metrics.json"
        metrics_md = tmp_path / "metrics.md"
        assert metrics_json.exists()
        assert metrics_md.exists()

        data = json.loads(metrics_json.read_text(encoding="utf-8"))
        assert data["name"] == "flow"
        assert data["total_seconds"] == 0.25
        assert data["timings"] == [{"stage": "generate", "seconds": 0.25}]
        assert data["metrics"] == [{"name": "notes", "value": 2}]
        assert data["io_stages"] == ["generate"]

        req = tmp_path / "io" / "01_generate.request.json"
        resp = tmp_path / "io" / "01_generate.response.json"
        assert json.loads(req.read_text(encoding="utf-8")) == {"q": "x"}
        assert json.loads(resp.read_text(encoding="utf-8")) == {"a": "y"}


class TestSaveArtifacts:
    """save_text/save_json/save_file write into run_dir."""

    def test_save_text(self, tmp_path: Path) -> None:
        session = LoggingSession("art", run_dir=str(tmp_path))
        target = session.save_text("sub/note.txt", "hello")
        assert target is not None
        assert Path(target).read_text(encoding="utf-8") == "hello"

    def test_save_json(self, tmp_path: Path) -> None:
        session = LoggingSession("art", run_dir=str(tmp_path))
        target = session.save_json("note.json", {"x": [1, 2]})
        assert target is not None
        assert json.loads(Path(target).read_text(encoding="utf-8")) == {"x": [1, 2]}

    def test_save_file_copies_source(self, tmp_path: Path) -> None:
        src = tmp_path / "src.bin"
        src.write_bytes(b"audio")
        run_dir = tmp_path / "run"
        session = LoggingSession("art", run_dir=str(run_dir))
        target = session.save_file(str(src), "out/audio.bin")
        assert target is not None
        assert Path(target).read_bytes() == b"audio"

    def test_save_file_missing_source_returns_none(self, tmp_path: Path) -> None:
        session = LoggingSession("art", run_dir=str(tmp_path))
        assert session.save_file(str(tmp_path / "nope.bin"), "out.bin") is None


class TestAsyncSession:
    """The async session offloads writes but they land after flush drains."""

    def test_writes_land_after_flush(self, tmp_path: Path) -> None:
        session = AsyncLoggingSession("async", run_dir=str(tmp_path))
        session.timing("generate", 0.1)
        session.save_text("deferred.txt", "later")
        session.flush()  # drains the worker queue

        assert (tmp_path / "deferred.txt").read_text(encoding="utf-8") == "later"
        assert (tmp_path / "metrics.json").exists()
        assert (tmp_path / "metrics.md").exists()


class TestRegistry:
    """get_logging_session reuses by name; logging_session owns the lifecycle."""

    def test_get_reuses_by_name(self, tmp_path: Path) -> None:
        first = get_logging_session("reused", run_dir=str(tmp_path))
        second = get_logging_session("reused", run_dir=str(tmp_path))
        try:
            assert first is second
        finally:
            from omnia.core.logging.session import _REGISTRY

            _REGISTRY.pop("reused", None)

    def test_context_manager_flushes_and_unregisters(self, tmp_path: Path) -> None:
        from omnia.core.logging.session import _REGISTRY

        with logging_session("ctx", run_dir=str(tmp_path)) as session:
            session.timing("generate", 0.2)
            assert _REGISTRY.get("ctx") is session

        assert "ctx" not in _REGISTRY
        assert (tmp_path / "metrics.json").exists()


@pytest.fixture(autouse=True)
def _clear_registry() -> object:
    """Ensure each test starts and ends with a clean session registry."""
    from omnia.core.logging.session import _REGISTRY

    _REGISTRY.clear()
    yield
    _REGISTRY.clear()
