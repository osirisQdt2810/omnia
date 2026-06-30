# Omnia — All-in-One Anki Toolkit

Omnia is a single Anki add-on that hosts many independent **feature plugins** — auto-flip,
typing-accuracy grading, AI note generation, and more. A clean settings UI lists every
plugin with an enable toggle; tick one and that feature turns on. Adding a new feature is
the same "pluginize" move every time: drop a `FeaturePlugin` subclass into `features/`,
register it, and it appears in the UI.

## Why a plugin architecture
Most Anki power-features touch the same few seams — they rewrite a card's grade, inject JS
into the reviewer, call an AI provider, or read config. Omnia builds those seams **once**
in `core/` and keeps each feature thin and isolated:

- **Plugin system** — `FeaturePlugin` + `@register` + a `PluginManager` lifecycle.
- **Reviewer ease pipeline** — one wrap of `Reviewer._answerCard`; features register
  ordered ease transformers and *cooperate* instead of fighting over the monkeypatch.
- **Web injector** — one place to inject reviewer JS/CSS and route the `pycmd` bridge.
- **Provider layer** — `LLMProvider` / `TTSProvider` interfaces so AI features work against
  an interface; adding a provider is one subclass.

## Bundled feature plugins
| Plugin | What it does |
|---|---|
| `auto_flip` | Auto-advances question → answer → grade after a configurable delay. |
| `typed_accuracy` | Grades a typed card again/hard/good/easy from typing accuracy. |
| `display_interval` | Shows the predicted next interval on the answer side. |
| `overdue_guard` | Forces very overdue cards to Hard/Again regardless of input. |
| `smart_notes` | Generates note fields (text/image) and TTS audio via an LLM/TTS provider. |

## Develop
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements/requirements-dev.txt
pre-commit install

pytest tests/ -vv                 # logic tests (Anki is stubbed)
python scripts/install_addon.py   # assemble into local Anki for manual testing
python scripts/build_addon.py     # -> dist/omnia.ankiaddon
```

The add-on runs inside Anki's bundled Python (3.13, latest Anki) with PyQt6 — no server, no external
services. Third-party runtime deps (if any) are vendored, pure-Python, cross-platform.

See `.claude/CLAUDE.md` for the full architecture and `.claude/CONVENTIONS.md` for coding
standards. `references/` holds read-only snapshots of the add-ons Omnia draws from.
