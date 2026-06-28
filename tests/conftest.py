"""Test harness: stub ``aqt``/``anki`` so Omnia imports headless, and shared fixtures.

Anki's GUI (``aqt``) can't run headless, so we inject lightweight fake modules into
``sys.modules`` *before* any ``omnia`` import. Pure logic doesn't touch these; the glue
(ease pipeline install, web injector hooks, entry point) imports cleanly against them.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import Optional

import pytest

# --- make `import omnia` resolve to src/omnia ---------------------------------------
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class FakeHook:
    """Stand-in for an ``aqt.gui_hooks`` hook: supports append/remove and manual firing."""

    def __init__(self) -> None:
        self._callbacks: list = []

    def append(self, cb) -> None:
        self._callbacks.append(cb)

    def remove(self, cb) -> None:
        if cb in self._callbacks:
            self._callbacks.remove(cb)

    def count(self) -> int:
        return len(self._callbacks)

    def fire(self, *args, **kwargs):
        """Invoke callbacks; for filter hooks, thread the first arg through each return."""
        result = args[0] if args else None
        for cb in list(self._callbacks):
            out = cb(*args, **kwargs)
            if out is not None:
                result = out
        return result


class FakeReviewer:
    """Minimal reviewer whose ``_answerCard`` records the ease it ultimately receives."""

    def __init__(self, card=None) -> None:
        self.card = card
        self.answered_with: list = []

    def _answerCard(self, ease: int) -> None:
        self.answered_with.append(ease)


def _install_anki_stubs() -> None:
    if "aqt" in sys.modules:
        return

    gui_hooks = types.SimpleNamespace(
        profile_did_open=FakeHook(),
        profile_will_close=FakeHook(),
        reviewer_did_show_question=FakeHook(),
        reviewer_did_show_answer=FakeHook(),
        reviewer_will_answer_card=FakeHook(),
        webview_did_receive_js_message=FakeHook(),
        reviewer_will_play_question_sounds=FakeHook(),
        reviewer_will_play_answer_sounds=FakeHook(),
        av_player_did_end_playing=FakeHook(),
        browser_will_show_context_menu=FakeHook(),
        # Bespoke per-feature UIs (smart_notes editor, typed_accuracy stats, auto_flip decks).
        editor_did_init_buttons=FakeHook(),
        editor_did_load_note=FakeHook(),
        overview_will_render_content=FakeHook(),
        deck_browser_will_show_options_menu=FakeHook(),
        state_did_change=FakeHook(),
    )

    aqt = types.ModuleType("aqt")
    aqt.mw = None  # tests that need mw set it themselves
    aqt.gui_hooks = gui_hooks

    reviewer_mod = types.ModuleType("aqt.reviewer")
    reviewer_mod.Reviewer = FakeReviewer
    aqt.reviewer = reviewer_mod

    operations_mod = types.ModuleType("aqt.operations")

    class QueryOp:  # minimal synchronous stand-in
        def __init__(self, *, parent=None, op=None, success=None):
            self._op = op
            self._success = success
            self._failure = None

        def with_progress(self, *_a, **_k):
            return self

        def failure(self, cb):
            self._failure = cb
            return self

        def run_in_background(self):
            try:
                result = self._op(None) if self._op else None
            except Exception as exc:  # route to failure like real QueryOp
                if self._failure:
                    self._failure(exc)
                return
            if self._success:
                self._success(result)

    operations_mod.QueryOp = QueryOp
    aqt.operations = operations_mod

    sys.modules["aqt"] = aqt
    sys.modules["aqt.reviewer"] = reviewer_mod
    sys.modules["aqt.operations"] = operations_mod
    sys.modules["anki"] = types.ModuleType("anki")


_install_anki_stubs()


# --- shared fixtures ----------------------------------------------------------------
@pytest.fixture
def gui_hooks():
    """The fake ``aqt.gui_hooks`` namespace (reset between tests)."""
    import aqt

    for name, value in list(vars(aqt.gui_hooks).items()):
        if isinstance(value, FakeHook):
            setattr(aqt.gui_hooks, name, FakeHook())
    return aqt.gui_hooks


@pytest.fixture
def config_repo(tmp_path):
    """A ConfigRepository over the real bundled defaults + a temp user-override file."""
    from omnia.core.config import ConfigLoader, ConfigRepository

    config_dir = _SRC / "omnia" / "config"
    return ConfigRepository(ConfigLoader(config_dir, tmp_path / "omnia.toml"))


class FakeHttpClient:
    """Injectable fake HTTP client: records calls, returns canned/routed responses.

    Pass ``json``/``data`` for a fixed response, or ``responder(method, url, body, headers)``
    for per-call routing (used by the provider sweep).
    """

    def __init__(self, *, json=None, data=b"", responder=None) -> None:
        self.calls: list = []
        self._json = json if json is not None else {}
        self._data = data
        self._responder = responder

    def post_json(self, url, payload, *, headers=None):
        self.calls.append(("post_json", url, payload, headers))
        if self._responder:
            return self._responder("post_json", url, payload, headers)
        return self._json

    def post_form(self, url, fields, *, headers=None):
        self.calls.append(("post_form", url, fields, headers))
        if self._responder:
            return self._responder("post_form", url, fields, headers)
        return self._json

    def post_json_for_bytes(self, url, payload, *, headers=None):
        self.calls.append(("post_json_for_bytes", url, payload, headers))
        if self._responder:
            return self._responder("post_json_for_bytes", url, payload, headers)
        return self._data

    def get_bytes(self, url, *, params=None, headers=None):
        self.calls.append(("get_bytes", url, params, headers))
        if self._responder:
            return self._responder("get_bytes", url, params, headers)
        return self._data


class FakeCard:
    """A duck-typed card for pure-logic tests."""

    def __init__(self, *, ivl: int = 0, mod: int = 0, id: int = 1) -> None:
        self.ivl = ivl
        self.mod = mod
        self.id = id


@pytest.fixture
def fake_card():
    return FakeCard


# --- LLM provider test support ------------------------------------------------------
# The provider "contract" tests (tests/providers/test_llm_contract.py) run the SAME
# functional assertions against two providers: a FakeLLMProvider (always, free) and the
# REAL configured provider (marked `llm`, auto-skipped without credentials). No flags — both
# subclasses are always collected, so the suite covers every case it can in the current env.
# Imported here (not at top) so the Anki stubs are installed first; base.py is a pure module.
from omnia.core.providers.llm.base import LLMProvider as _LLMProvider  # noqa: E402


class FakeLLMProvider(_LLMProvider):
    """A canned LLMProvider: deterministic, no network. Subclasses the real ABC so the
    contract's ``isinstance(LLMProvider)`` assertion is meaningful."""

    name = "fake"

    def __init__(self, text: str = "pong", image: bytes = b"\x89PNG-fake") -> None:
        self._text = text
        self._image = image

    def generate_text(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        return self._text

    def generate_image(self, prompt: str, *, size: str = "1024x1024") -> bytes:
        return self._image


# Imported here (after the stubs) for the same reason as _LLMProvider; base.py is pure.
from omnia.core.providers.tts.base import TTSProvider as _TTSProvider  # noqa: E402


class FakeTTSProvider(_TTSProvider):
    """A canned TTSProvider: deterministic, no network. Subclasses the real ABC so the TTS
    contract's ``isinstance(TTSProvider)`` assertion is meaningful (the fake context).
    """

    name = "fake"
    audio_ext = "mp3"

    def __init__(self, audio: bytes = b"ID3-fake-audio") -> None:
        self._audio = audio

    def synthesize(
        self, text: str, *, lang: Optional[str] = None, voice: Optional[str] = None
    ) -> bytes:
        return self._audio


def _real_config_dir() -> Path:
    return _SRC / "omnia" / "config"


def llm_sub_has_credentials(provider: str, sub) -> bool:
    """Return True if the ``[llm.<provider>]`` subsection has enough config for a real call."""
    if sub is None:
        return False
    if provider == "gemini_vertex":
        return bool(sub.project) and bool(
            sub.access_token or sub.use_gcloud or sub.credentials_path
        )
    return bool(getattr(sub, "api_key", ""))


def has_llm_credentials(llm_settings) -> bool:
    """Return True if the active LLM provider has enough config to make a real call."""
    return llm_sub_has_credentials(llm_settings.provider, llm_settings.active())


def _real_repo_and_override():
    """A ConfigRepository over the bundled defaults + the UNTRACKED creds override.

    Creds come only from ``OMNIA_TEST_CONFIG`` or the gitignored ``user_files/omnia.toml`` —
    never the tracked ``providers.toml`` — so secrets are never sourced from a committable file.
    """
    from omnia.core.config import ConfigLoader, ConfigRepository

    override = os.environ.get("OMNIA_TEST_CONFIG")
    user_file = (
        Path(override) if override else _SRC / "omnia" / "user_files" / "omnia.toml"
    )
    return ConfigRepository(ConfigLoader(_real_config_dir(), user_file)), user_file


def real_llm_provider_or_skip():
    """Build the REAL configured LLM provider, or ``pytest.skip`` if creds are absent.

    Credentials are read from an **untracked** override only — never from the tracked bundled
    ``providers.toml`` — so a live, billable ``@llm`` run can't be triggered by (or leak) the
    repo's shipped config:

    * env ``OMNIA_TEST_CONFIG`` → a TOML file holding your ``[llm.<provider>]`` creds, or
    * the add-on's gitignored ``src/omnia/user_files/omnia.toml`` (what the running add-on
      writes when you configure providers in Anki).

    The bundled ``providers.toml`` still supplies non-secret defaults (provider, model ids);
    only the credential override decides whether this runs or skips.
    """
    from omnia.core.providers import ProviderHub

    repo, user_file = _real_repo_and_override()
    llm = repo.llm_settings()
    if not (user_file.exists() and has_llm_credentials(llm)):
        pytest.skip(
            f"no credentials for LLM provider {llm.provider!r}: put them in an untracked "
            f"override — set OMNIA_TEST_CONFIG to a TOML with [llm.{llm.provider}] creds, or "
            f"configure providers in Anki (writes user_files/omnia.toml) — to run @llm tests"
        )
    return ProviderHub(llm, repo.tts_settings()).llm()


def real_llm_subsection_or_skip(provider: str):
    """Return the real ``[llm.<provider>]`` settings subsection, or skip if creds are absent.

    Reads the ACTUAL configured values (model ids, key/project) so tests assert against the
    real config rather than fabricated placeholders.
    """
    repo, user_file = _real_repo_and_override()
    sub = getattr(repo.llm_settings(), provider, None)
    if not (user_file.exists() and llm_sub_has_credentials(provider, sub)):
        pytest.skip(f"no credentials for LLM provider {provider!r}")
    return sub


def real_llm_provider_for_or_skip(provider: str, *, http=None):
    """Build LLM ``provider`` from the real merged config, or skip if its creds are absent.

    Used by the real-provider SWEEP (every credentialed provider is exercised live; others
    skip individually). Pass ``http`` to inject a (fake) HTTP client for an OFFLINE wiring
    test that still uses the real configured model/credentials but makes no network call.
    """
    from omnia.core.providers import ProviderHub

    repo, user_file = _real_repo_and_override()
    llm = repo.llm_settings()
    if not (
        user_file.exists()
        and llm_sub_has_credentials(provider, getattr(llm, provider, None))
    ):
        pytest.skip(
            f"no credentials for LLM provider {provider!r} (skip in real sweep)"
        )
    return ProviderHub(
        llm.model_copy(update={"provider": provider}), repo.tts_settings(), http=http
    ).llm()


def _tts_unavailable_reason(provider: str, repo, user_file) -> Optional[str]:
    """Why the real ``provider`` TTS can't run here (or None if it can)."""
    if provider == "google_translate":
        return None  # free, no creds (needs only network — that's the test)
    if provider in ("openai", "openrouter", "openai_compatible"):
        sub = getattr(repo.tts_settings(), provider, None)
        return None if (sub and sub.api_key) else f"no api_key for TTS {provider!r}"
    if provider == "google_cloud":
        gv = repo.llm_settings().gemini_vertex
        ok = user_file.exists() and llm_sub_has_credentials("gemini_vertex", gv)
        return None if ok else "no [llm.gemini_vertex] Google auth for google_cloud TTS"
    if provider == "edge_tts":
        import importlib.util

        return (
            None
            if importlib.util.find_spec("edge_tts")
            else "edge-tts package not installed"
        )
    if provider == "piper":
        import shutil

        sub = getattr(repo.tts_settings(), provider, None)
        if not (sub and sub.model):
            return "no piper model (.onnx) configured"
        return (
            None
            if shutil.which(sub.binary)
            else f"piper binary {sub.binary!r} not found"
        )
    return f"unknown TTS provider {provider!r}"


def llm_provider_params():
    """Parametrize cases, one per LLM provider (every LLM provider needs an API today)."""
    from omnia.core.providers import available_llm_providers

    return [pytest.param(name) for name in available_llm_providers()]


def tts_provider_params():
    """Parametrize cases per TTS provider; mark ``tts`` only those needing an API/creds.

    Keyless/offline providers (google_translate, edge_tts, piper) stay UNMARKED so they run
    in the default bucket; keyed/cloud ones carry ``@pytest.mark.tts``.
    """
    from omnia.core.providers import (
        available_tts_providers,
        available_tts_providers_requiring_api,
    )

    requiring = set(available_tts_providers_requiring_api())
    return [
        pytest.param(name, marks=(pytest.mark.tts,) if name in requiring else ())
        for name in available_tts_providers()
    ]


def assert_valid_audio(audio, ext: str) -> None:
    """Assert ``audio`` is real, non-trivial audio in the ``ext`` container (by magic bytes)."""
    assert isinstance(audio, (bytes, bytearray)), f"audio is {type(audio)}"
    assert len(audio) > 500, f"audio too small to be real speech: {len(audio)} bytes"
    if ext == "mp3":
        head = bytes(audio[:3])
        frame_sync = len(audio) > 1 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0
        assert head == b"ID3" or frame_sync, f"not MP3 (head={head!r})"
    elif ext == "wav":
        assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE", "not a WAV file"


def real_tts_subsection_or_skip(provider: str):
    """Return the real ``[tts.<provider>]`` settings subsection, or skip if it can't run here."""
    repo, user_file = _real_repo_and_override()
    reason = _tts_unavailable_reason(provider, repo, user_file)
    if reason:
        pytest.skip(reason)
    return getattr(repo.tts_settings(), provider, None)


def real_tts_provider_for_or_skip(provider: str, *, http=None):
    """Build TTS ``provider`` from the real merged config, or skip if it can't run here.

    Pass ``http`` to inject a (fake) client for an OFFLINE wiring test that uses the real
    configured values/credentials but makes no network call.
    """
    from omnia.core.providers import ProviderHub

    repo, user_file = _real_repo_and_override()
    reason = _tts_unavailable_reason(provider, repo, user_file)
    if reason:
        pytest.skip(f"{reason} (skip in real TTS sweep)")
    return ProviderHub(
        repo.llm_settings(),
        repo.tts_settings().model_copy(update={"provider": provider}),
        http=http,
    ).tts()


# A real call failed because of a transient/quota/budget LIMIT (not a wiring bug) if its
# status is 429/5xx, or the message names a quota/rate/token/no-text condition. Per the user:
# such cases are `xfail` (we still record the test + the case), not hard failures.
# (HTTP 429/5xx are matched by STATUS CODE below — not as bare "429"/"503" substrings, which
# would false-xfail any message that merely contains those digits.)
_LIMIT_STATUS = frozenset({429, 500, 502, 503, 504})
_LIMIT_MARKERS = (
    "quota",
    "rate limit",
    "rate-limit",
    "resource_exhausted",
    "resource exhausted",
    "max_tokens",
    "max tokens",
    "returned no text",
    "timed out",
    "timeout",
    "temporarily",
    "overloaded",
    "network error",
    "connection refused",
    "connection reset",
)


def is_provider_limit_error(exc: Exception) -> bool:
    """True if ``exc`` is a transient/quota/token limit rather than a wiring bug."""
    if getattr(exc, "status_code", None) in _LIMIT_STATUS:
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _LIMIT_MARKERS)


def call_or_xfail(fn, *args, **kwargs):
    """Run a real provider call; ``pytest.xfail`` on a quota/rate/token/transient limit.

    The user's policy: a real provider hitting quota or a token-budget limit is a KNOWN,
    expected case — record it as ``xfail`` (the test still exists and ran) rather than failing
    the suite. Genuine errors (bad payload, auth bug, malformed response) still raise → fail.
    """
    from omnia.core.providers import ProviderError

    try:
        return fn(*args, **kwargs)
    except ProviderError as exc:
        if is_provider_limit_error(exc):
            pytest.xfail(
                f"provider limit (quota/rate/token/transient): {str(exc)[:200]}"
            )
        raise
