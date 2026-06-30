"""Hermetic guard: the add-on must work with ONLY stdlib + the repo-root ``vendor/universal``.

This is the test that would have caught the ``edge_tts`` regression where a provider quietly
depended on a pip package (``edge-tts``) that exists in the dev venv but NOT in Anki's bundled
Python — so the suite passed while the real feature failed. Each test here spawns a child
interpreter with **site-packages stripped** (the Anki runtime: stdlib + the add-on's vendored
deps, no pip packages) and asserts the providers import, build, and (with network) actually
synthesize. If a provider grows a non-vendored import, these fail loudly.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import _can_reach

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
_VENDOR = _REPO / "vendor" / "universal"

# Prologue every child runs: become an Anki-like interpreter — drop site-packages / the dev
# venv, keep stdlib, and add only the add-on source + its vendored (pure-Python) deps.
_HERMETIC_PRELUDE = f"""
import sys
sys.path = [p for p in sys.path if 'site-packages' not in p and '.venv' not in p]
sys.path.insert(0, {str(_SRC)!r})
sys.path.append({str(_VENDOR)!r})
"""


def _run_hermetic(body: str) -> subprocess.CompletedProcess:
    """Run ``body`` in a child interpreter with ``-S`` (no site) + the Anki-like sys.path."""
    code = _HERMETIC_PRELUDE + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-S", "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestAnkiLikeRuntime:
    """Providers must work with vendored-only deps — no pip packages, like real Anki."""

    def test_no_pip_deps_and_vendored_pydantic(self):
        result = _run_hermetic("""
            import importlib.util as u
            # Pip-only deps that must NOT be required at runtime (they can't ship in Anki).
            assert u.find_spec('edge_tts') is None, 'edge-tts pip pkg leaked into Anki env'
            assert u.find_spec('aiohttp') is None, 'aiohttp leaked into Anki env'
            # pydantic must resolve to the VENDORED copy, not a pip install.
            import pydantic
            assert 'vendor' in (pydantic.__file__ or ''), pydantic.__file__
            print('OK')
            """)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_every_keyless_tts_provider_builds_hermetically(self):
        # The keyless providers must construct with stdlib+vendor only (the bug was edge_tts
        # importing a missing package). Cloud/keyed providers are covered by the real sweep.
        result = _run_hermetic("""
            from omnia.core.providers.tts import create_tts_provider
            for cfg in (
                {'provider': 'google_translate'},
                {'provider': 'edge_tts'},
                {'provider': 'viettts', 'autostart': False},
                {'provider': 'piper', 'model': 'v.onnx'},
            ):
                p = create_tts_provider(cfg)
                assert p is not None
            # The whole add-on package must import in the Anki-like env, too.
            import omnia
            print('OK')
            """)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    @pytest.mark.skipif(
        not _can_reach("speech.platform.bing.com", 443),
        reason="no network for Edge TTS",
    )
    def test_edge_tts_synthesizes_hermetically(self):
        # The real proof: Edge TTS produces valid MP3 in an Anki-like env (no edge-tts/aiohttp).
        result = _run_hermetic("""
            from omnia.core.providers.tts import create_tts_provider
            audio = create_tts_provider({'provider': 'edge_tts'}).synthesize(
                'Hello from a hermetic Anki-like runtime.', lang='en'
            )
            ok = audio[:3] == b'ID3' or (len(audio) > 1 and audio[0] == 0xFF and (audio[1] & 0xE0) == 0xE0)
            assert ok and len(audio) > 500, (len(audio), audio[:4])
            print('AUDIO', len(audio))
            """)
        assert result.returncode == 0, result.stderr
        assert "AUDIO" in result.stdout
