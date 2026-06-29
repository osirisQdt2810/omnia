"""Tests for the Smart Notes "Native runtimes" panel: payload + install/uninstall routing.

Fully offline — a ``_FakeManager`` scripts ``is_installed`` and records ``ensure_installed`` /
``uninstall`` calls, so no real venv, pip, subprocess, or network is touched. The dialog glue
(``dialog.py``) can't import headless (it pulls ``aqt.theme`` / a QDialog), so the pure builders
in ``html.py`` (``native_runtimes_payload`` + ``set_native_runtime``) are tested directly with
the injected collaborators the dialog otherwise supplies.
"""

from __future__ import annotations

from omnia.core.providers.native_runtime import (
    NativeRuntimeSpec,
    native_runtimes_by_section,
)
from omnia.gui.smart_notes import html


class _FakeManager:
    """Records install/uninstall calls and scripts ``is_installed`` per spec name."""

    def __init__(self, installed: set[str] | None = None) -> None:
        self._installed = set(installed or ())
        self.installed_calls: list[str] = []
        self.uninstalled: list[str] = []
        self.progress_emitted: list[str] = []

    def is_installed(self, spec: NativeRuntimeSpec) -> bool:
        return spec.name in self._installed

    def ensure_installed(self, spec: NativeRuntimeSpec, on_progress=None) -> None:
        self.installed_calls.append(spec.name)
        if on_progress is not None:
            on_progress("installing…")
            self.progress_emitted.append(spec.name)
        self._installed.add(spec.name)

    def uninstall(self, spec: NativeRuntimeSpec) -> None:
        self.uninstalled.append(spec.name)
        self._installed.discard(spec.name)


def _spec(name: str, section: str) -> NativeRuntimeSpec:
    return NativeRuntimeSpec(
        name=name,
        section=section,
        label=f"{name} label",
        pip_packages=(name,),
        mode="cli",
        size_hint="~10 MB",
        cli_argv=("{bin}/" + name,),
    )


class TestNativeRuntimesPayload:
    def test_groups_by_section_with_installed_flags(self, monkeypatch):
        grouped = {
            "tts": [_spec("alpha", "tts"), _spec("beta", "tts")],
            "llm": [_spec("gamma", "llm")],
        }
        monkeypatch.setattr(
            html, "native_runtimes_by_section", lambda: grouped, raising=False
        )
        # native_runtimes_payload imports the symbol lazily from the source module, so patch
        # there too.
        monkeypatch.setattr(
            "omnia.core.providers.native_runtime.native_runtimes_by_section",
            lambda: grouped,
        )
        manager = _FakeManager(installed={"beta"})
        payload = html.native_runtimes_payload(manager)

        sections = payload["sections"]
        assert [s["section"] for s in sections] == ["tts", "llm"]
        tts_rows = sections[0]["runtimes"]
        assert [r["name"] for r in tts_rows] == ["alpha", "beta"]
        assert tts_rows[0]["installed"] is False
        assert tts_rows[1]["installed"] is True
        assert tts_rows[0]["label"] == "alpha label"
        assert tts_rows[0]["size_hint"] == "~10 MB"
        assert tts_rows[0]["section"] == "tts"

    def test_real_registry_specs_round_trip(self):
        # viettts + piper register at import; with nothing installed every flag is False and the
        # manager's is_installed is consulted for each registered spec.
        manager = _FakeManager()
        payload = html.native_runtimes_payload(manager)
        names = {r["name"] for s in payload["sections"] for r in s["runtimes"]}
        assert {"viettts", "piper"} <= names
        registered = {
            spec.name
            for specs in native_runtimes_by_section().values()
            for spec in specs
        }
        # Every registered runtime appears exactly once in the payload.
        assert names == registered
        for section in payload["sections"]:
            for row in section["runtimes"]:
                assert row["installed"] is False


class _Recorder:
    """Captures the injected push hooks + the (optional) synchronous run_async."""

    def __init__(self) -> None:
        self.progress: list[tuple[str, str]] = []
        self.done: list[tuple[str, bool, str]] = []

    def push_progress(self, name: str, msg: str) -> None:
        self.progress.append((name, msg))

    def push_done(self, name: str, installed: bool, error: str = "") -> None:
        self.done.append((name, installed, error))


def _run_sync(op, on_success, on_failure) -> None:
    """A synchronous stand-in for the dialog's off-thread runner."""
    try:
        op()
    except Exception as exc:  # mirror QueryOp's failure routing
        on_failure(exc)
    else:
        on_success()


class TestSetNativeRuntimeRouting:
    def test_enable_installs_off_thread_and_pushes_done(self):
        manager = _FakeManager()
        rec = _Recorder()
        result = html.set_native_runtime(
            manager,
            "piper",
            True,
            run_async=_run_sync,
            push_progress=rec.push_progress,
            push_done=rec.push_done,
        )
        assert result is None  # async path returns nothing; the row updates via pushes
        assert manager.installed_calls == ["piper"]
        assert manager.uninstalled == []
        assert rec.progress == [("piper", "installing…")]
        assert rec.done == [("piper", True, "")]

    def test_disable_uninstalls_sync_and_returns_payload(self):
        manager = _FakeManager(installed={"piper"})
        rec = _Recorder()
        result = html.set_native_runtime(
            manager,
            "piper",
            False,
            run_async=_run_sync,
            push_progress=rec.push_progress,
            push_done=rec.push_done,
        )
        assert result == {"name": "piper", "installed": False}
        assert manager.uninstalled == ["piper"]
        assert manager.installed_calls == []
        assert rec.done == []  # uninstall reports through the synchronous return value

    def test_unknown_runtime_pushes_error(self):
        manager = _FakeManager()
        rec = _Recorder()
        result = html.set_native_runtime(
            manager,
            "does-not-exist",
            True,
            run_async=_run_sync,
            push_progress=rec.push_progress,
            push_done=rec.push_done,
        )
        assert result is None
        assert manager.installed_calls == []
        assert rec.done == [("does-not-exist", False, "Unknown runtime.")]

    def test_install_failure_pushes_friendly_error(self):
        class _Boom(_FakeManager):
            def ensure_installed(self, spec, on_progress=None):
                from omnia.core.providers.errors import ProviderError

                raise ProviderError("no host Python found")

        rec = _Recorder()
        result = html.set_native_runtime(
            _Boom(),
            "piper",
            True,
            run_async=_run_sync,
            push_progress=rec.push_progress,
            push_done=rec.push_done,
        )
        assert result is None
        assert len(rec.done) == 1
        name, installed, error = rec.done[0]
        assert name == "piper" and installed is False
        assert "no host Python found" in error


class TestPageMarkup:
    def test_native_runtimes_panel_baked(self):
        markup = html.build_smart_notes_html(dark=False)
        assert "sn-native-list" in markup
        assert "Native runtimes" in markup
        assert "set_native_runtime" in markup
        assert "native_runtimes" in markup
        assert "window.__snNativeRuntimeProgress" in markup
        assert "window.__snNativeRuntimeDone" in markup
