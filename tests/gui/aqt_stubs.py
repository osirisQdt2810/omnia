"""The extra ``aqt`` symbols the GUI dialog modules import at module load.

The root ``conftest`` stubs what Omnia's *seams* touch (``gui_hooks``, ``operations``, a handful
of Qt symbols). A dialog module reaches further — ``aqt.theme`` for night mode, ``aqt.webview``
for :class:`AnkiWebView`, and whichever widgets it names in its own imports — so a test that
wants to assert something about a dialog CLASS (never an instance; there is no Qt to build one)
needs those present before the import runs.

Installed here rather than per test file so the pile is written once. Import this module before
importing any ``omnia.gui`` dialog module::

    from tests.gui import aqt_stubs  # noqa: F401  (installs the stubs)

Not a ``conftest`` on purpose: ``tests/gui/conftest.py`` would shadow the root one for anything
doing ``from conftest import …``. Everything uses ``setdefault``, so a file that installed its own
stubs first keeps them.
"""

from __future__ import annotations

import sys
import types


def _stub_qt_symbol(module: types.ModuleType, name: str) -> type:
    """Manufacture (and remember) a stand-in class for a Qt symbol nobody stubbed."""
    if name.startswith("__"):
        raise AttributeError(name)
    made = type(name, (), {})
    setattr(module, name, made)
    return made


def install_gui_stubs() -> None:
    """Install the stubs. Idempotent — every step is a ``setdefault`` or a re-assignment."""
    _theme = types.ModuleType("aqt.theme")
    _theme.theme_manager = types.SimpleNamespace(night_mode=False)
    sys.modules.setdefault("aqt.theme", _theme)

    _qt = sys.modules.get("aqt.qt") or types.ModuleType("aqt.qt")
    # Any Qt name a dialog module imports resolves to a throwaway class, rather than a list that
    # has to be extended every time a dialog grows an import. These tests never BUILD a widget —
    # there is no Qt here to build one with — so the only thing the symbol has to do is exist,
    # and a missing one fails as an ImportError at collection with no hint of which test wanted
    # it. Names the root conftest defines for real (QTimer, which runs its closure inline) keep
    # their behaviour: ``__getattr__`` is only consulted for what is missing.
    if not hasattr(_qt, "__getattr__"):
        _qt.__getattr__ = lambda name: (  # type: ignore[attr-defined]
            _stub_qt_symbol(_qt, name)
        )
    sys.modules["aqt.qt"] = _qt

    _webview = types.ModuleType("aqt.webview")
    _webview.AnkiWebView = type("AnkiWebView", (), {})
    sys.modules.setdefault("aqt.webview", _webview)

    import aqt  # the root conftest's stub package

    aqt.qt = _qt
    aqt.theme = sys.modules["aqt.theme"]
    aqt.webview = sys.modules["aqt.webview"]


install_gui_stubs()
