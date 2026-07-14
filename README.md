# Omnia — All-in-One Anki Toolkit

Omnia is a single Anki add-on that hosts many independent **feature plugins** — auto-flip,
typing-accuracy grading, AI note generation, and more. A clean settings UI lists every
plugin with an enable toggle; tick one and that feature turns on. Adding a new feature is
the same "pluginize" move every time: drop a `FeaturePlugin` subclass into `plugins/`,
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

---

## Install on your machine

Omnia runs **inside Anki** (25.09+ / 26.x, which bundles Python 3.13 + PyQt6). It is **not**
a server — there is nothing to run separately.

### A. End user — install the packaged add-on (macOS & Windows)
1. Get `omnia.ankiaddon` (from a release, or build it yourself — see *Develop* below:
   `python scripts/build_addon.py` writes it to `dist/`).
2. In Anki: **Tools → Add-ons → Install from file…** and pick `omnia.ankiaddon`.
3. **Restart Anki.** You now have a **Tools → Omnia** menu.

That is the whole install. Everything else (which plugins are on, their settings) is done in
the GUI and stored in your collection — see *Where your settings & data live* below. There is
no config file to edit **unless** you use the AI features, which need `providers.toml` (next
section).

> Installing on another machine? Just repeat A on that machine. Your **plugin settings sync
> automatically** with the rest of your collection through AnkiWeb (they live in the collection,
> not in a local file). The only per-machine step is re-creating `providers.toml` with your API
> keys, because secrets are deliberately **not** synced (see below).

### B. Developer — run from source into your local Anki
```bash
git clone --recurse-submodules <this-repo-url>      # --recurse-submodules pulls the clippers
cd omnia
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements/requirements-dev.txt
pre-commit install

python scripts/install_addon.py    # assemble src/omnia + vendor/ + models/ + config/ into addons21/
```
Restart Anki; edits to the source are picked up on the next Anki start (re-run
`install_addon.py` if the assembled layout changed). `pytest tests/ -vv` runs the logic tests
headless (Anki is stubbed).

---

## Where your settings & data live (no files in this repo — by design)

You will not find `storage.json`, `usage.json`, `voices.json`, or a live `config` in this
repository. That is intentional: **Omnia keeps its runtime state inside Anki's own database,
created on your machine on first run** (ADR-006). Nothing runtime is committed to the repo.

| What | Where it is stored | Synced by AnkiWeb? |
|---|---|---|
| Plugin settings + enable toggles | Anki **collection config** (`col.set_config`) | ✅ Yes — follows your collection to every device |
| AI usage accounting (LLM/TTS call counts) | Anki **collection config** (`col.set_config`) | ✅ Yes — usage aggregates across your devices |
| Fetched-voice cache (TTS voice lists) | Anki **collection config** | ✅ Yes |
| A tiny backend marker | `user_files/.storage.json` | ❌ No — device-local |
| **AI provider config + API keys** | **`user_files/config/providers.toml`** + `user_files/config/.secrets/` | ❌ No — stays a local file, never synced |

`user_files/` is the one directory Anki **preserves across add-on updates**, so your local
config and secrets survive upgrades (the rest of the add-on folder is replaced on update).

**Swappable backends.** Each storage concern above is dispatched by an environment knob so a
backend can be swapped without losing data — `OMNIA_CONFIG_STORAGE`, `OMNIA_USAGE_STORAGE`,
`OMNIA_VOICE_CACHE_STORAGE` (each defaults to `database`). When you change a knob, Omnia reads
the `user_files/.storage.json` marker, notices the change, and **syncs the concern's data from
the old backend into the new one** before using it — so switching is safe. Most users never
touch these; the default (everything in the Anki DB) is what the tables above describe.

---

## Set up AI providers (only for `smart_notes` / TTS)

The AI features need provider credentials. **You do not create or copy any file** — the add-on
**auto-creates** `user_files/config/providers.toml` (from the shipped template) on first run.
Configure everything **in the GUI**:

> **Tools → Omnia**, open the **Smart Notes** plugin's **Configure**, go to the **Usage & Keys**
> tab, pick your LLM/TTS provider + model and paste your API key. That's it — the dialog writes
> `providers.toml` and stores the key under `user_files/config/.secrets/` for you.

Keys are the **one** thing kept in a local file rather than the synced collection, so they never
sync to AnkiWeb or land in the DB. *Advanced:* you can edit `user_files/config/providers.toml`
directly instead — `config/providers.example.toml` documents every option and
`config/secrets.README.md` explains the `.secrets/` references. Both `providers.toml` and
`.secrets/` are gitignored and live-only. (Do **not** copy the template over an existing
`providers.toml` — that would overwrite your keys.)

---

## Companion clippers (capture into Anki from anywhere)

Two optional companion tools push words + context into your running Anki (via AnkiConnect),
where `smart_notes` auto-generates the rest of the card. They are tracked here as **git
submodules** under [`3rdparty/`](3rdparty/) — each has its own repo and its own README with
per-machine install steps:

| Tool | Capture from | Repo |
|---|---|---|
| [Omnia Web Clipper](3rdparty/omnia-web-clipper) | any web page (Chrome/Chromium) | <https://github.com/osirisQdt2810/omnia-web-clipper> |
| [Omnia Desktop Clipper](3rdparty/omnia-desktop-clipper) | any desktop app + screen OCR (macOS/Windows/Linux) | <https://github.com/osirisQdt2810/omnia-desktop-clipper> |

Enable each in **Tools → Omnia** → the **Smart Notes** plugin's **Configure** → **Integrations**
tab. If you cloned without `--recurse-submodules`, run `git submodule update --init --recursive`.

---

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
