# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**Omnia** is an all-in-one, **pluginized Anki add-on**. One add-on hosts many independent
"feature plugins" (auto-flip, typed-accuracy, smart-notes, …). A clean settings UI lists
every plugin with an enable toggle; ticking one activates that feature. New features are
added by the same "pluginize" method — drop a `FeaturePlugin` subclass into `plugins/`,
register it, and it shows up in the UI.

The whole point of the architecture is **shared seams**: features are thin, and the
repetitive/interacting parts (rewriting a card's ease, injecting JS into the reviewer,
calling an LLM/TTS provider, reading namespaced config) live in `core/` and are reused.

## Stack & runtime reality (read this before anything else)

- **Language:** Python 3.13 — the latest Anki's bundled interpreter (Anki 25.09+/26.x).
  Keep code 3.10+-compatible (Anki's minimum supported Python).
- **GUI:** PyQt6 (bundled with Anki). Anki APIs: `anki` (backend/collection/scheduler) and
  `aqt` (Qt GUI, hooks, reviewer, main window `mw`).
- **This is NOT a server.** There is **no Flask, no Celery, no Redis, no Supabase, no
  Docker runtime**. The add-on runs *inside the user's Anki* on macOS **and** Windows.
- **Background work** uses `aqt.operations.QueryOp` / `mw.taskman` / `mw.progress.timer`,
  never a task queue. The Qt main thread must never block on network/LLM/TTS calls.
- **Third-party deps are vendored** into the repo-root `vendor/` (assembled next to the
  package at deploy) and added to `sys.path` at startup — Anki does not `pip install` for you.
  Vendored deps **must be pure-Python and cross-platform** (no compiled wheels), or the add-on
  breaks on Windows.
- **Config** is Anki's per-add-on JSON (`mw.addonManager.getConfig/writeConfig`), accessed
  only through the namespaced `ConfigStore` — never scattered raw calls.

### Where commands run
On the **host (macOS)**, in the repo's dev virtualenv. There is no container. Install dev
deps with `pip install -r requirements/requirements-dev.txt`. The `anki` package is
pip-installable for testing collection/scheduler logic; `aqt` (the GUI) is **not** run
headless — its hooks are stubbed in tests (`tests/conftest.py`). Docker (`deploy/Dockerfile`)
exists only to run the test suite + linters in a pinned, reproducible environment for CI /
cross-platform sanity — it never runs the add-on itself.

## Architecture (modular monolith, plugin-oriented)

```
src/omnia/                  # SOURCE-ONLY add-on package — assembled into Anki's addons21/ by scripts/install_addon.py
├── __init__.py             # Anki entry point: vendor sys.path, build PluginManager, run enabled plugins
├── manifest.json           # Anki add-on manifest
├── envs.py                 # environment/runtime helpers
├── core/                   # SHARED SEAMS — reused by every feature; never imports plugins
│   ├── registry.py         # @register decorator + FEATURE_REGISTRY
│   ├── plugin.py           # FeaturePlugin base class + PluginContext (handed to each plugin)
│   ├── manager.py          # PluginManager: reads config, instantiates, enable/disable lifecycle
│   ├── config_store.py     # namespaced read/write on top of addonManager config
│   ├── logging/            # add-on logger + crash diagnostics + run-capture session
│   ├── anki_compat.py      # Anki version shims + safe hook helpers
│   ├── reviewer/
│   │   ├── ease_pipeline.py   # ONE wrap of Reviewer._answerCard; ordered ease transformers
│   │   └── web_injector.py    # inject JS/CSS into the reviewer webview; route pycmd messages
│   └── providers/             # LLM + TTS provider abstraction (adapted from a prior project)
│       ├── llm/   (base, factory, + concrete providers)
│       └── tts/   (base, factory, + concrete providers)
├── plugins/                # ONE folder per feature plugin (thin; sit on top of core seams)
│   ├── __init__.py         # imports each feature module so its @register runs at load time
│   ├── auto_flip/
│   ├── typed_accuracy/
│   ├── display_interval/
│   ├── overdue_guard/
│   └── smart_notes/
└── gui/                    # settings dialog (the plugin list + per-plugin config panels)

# Non-source data lives at the repo ROOT (not under src/omnia, which is source-only). The
# install/build scripts assemble these in next to the package as siblings:
vendor/                     # vendored pure-Python deps (committed; on sys.path at startup)
models/                     # bundled TTS voice models (piper .onnx via Git LFS + small json/README)
config/                     # tracked *.example.toml templates + secrets.README.md (live *.toml/.secrets are runtime, gitignored)

tests/                      # pytest; conftest.py stubs aqt/anki so logic tests run headless
scripts/                    # build_addon.py (-> dist/*.ankiaddon), install_addon.py (assemble into addons21/)
requirements/               # requirements-dev.txt (host tooling), requirements-vendor.txt (deps to vendor)
deploy/Dockerfile           # CI/test image only
```

### The four shared seams (this is the design — protect it)
1. **Plugin system** (`core/registry.py`, `core/plugin.py`, `core/manager.py`): every
   feature is a `FeaturePlugin` subclass registered with `@register("<id>")`. The
   `PluginManager` reads the `enabled` map from config, and calls `on_enable`/`on_disable`.
   Each plugin receives a `PluginContext` (config accessor, logger, the reviewer seams).
2. **Reviewer ease pipeline** (`core/reviewer/ease_pipeline.py`): `Reviewer._answerCard` is
   wrapped **exactly once**. Features that want to change the graded ease register an
   ordered *ease transformer* `(card, requested_ease) -> new_ease`. typed-accuracy and
   overdue-guard both use this — they cooperate instead of fighting over the monkeypatch.
3. **Web injector** (`core/reviewer/web_injector.py`): one place to inject JS/CSS into the
   reviewer webview on show-question / show-answer, and one `pycmd` router that dispatches
   `omnia:<plugin>:<op>` messages to the owning plugin. auto-flip, typed-accuracy, and
   display-interval all use this.
4. **Provider layer** (`core/providers/`): `LLMProvider` and `TTSProvider` base classes, so
   smart-notes (and future AI features) work against an interface. Adding a provider = one
   subclass + one registration, no feature code changes — TTS self-registers via
   `@register_tts` (`core/providers/tts/registry.py`); LLM still uses `core/providers/llm/factory.py`.

### Coupling rule (enforced in review)
`plugins/*` depend on `core/*`; **`core/*` never imports `plugins/*`**. Seams don't know
which features use them. Pure logic modules (accuracy math, overdue rule, provider HTTP)
must not import `aqt`/`anki` at top level so they unit-test headless.

## Adding a new feature plugin
1. Create `src/omnia/plugins/<name>/__init__.py` (and a `logic.py` for pure logic).
2. Subclass `FeaturePlugin`, set `id`/`name`/`description`, implement `on_enable(ctx)` /
   `on_disable(ctx)` and (optionally) `config_schema()` for the settings panel.
3. Decorate the class with `@register("<name>")` from `core/registry.py`.
4. Import the module in `src/omnia/plugins/__init__.py` so the decorator runs.
5. Use the seams — ease pipeline / web injector / providers / config store — not raw Anki.
6. Add `tests/plugins/test_<name>.py` (pure logic; stub Anki).
7. Add a default config block under the plugin's namespace in `config.json`.

## Adding a new provider (LLM or TTS)
1. Subclass `LLMProvider` (or `TTSProvider`) in `core/providers/llm|tts/<name>.py`.
2. Implement the abstract methods; keep network calls in pure modules (vendored HTTP lib
   or stdlib), no UI imports.
3. Register it — **the two sides differ**:
   - **TTS:** decorate the class with `@register_tts("<name>")` from `core/providers/tts/registry.py`,
     give it a `from_config(cls, config, http)` classmethod, declare `CURATED_VOICES` (so it shows
     in the voice pickers / Auto-detect map), and import the module in `core/providers/tts/__init__.py`
     so the decorator runs. (No central factory dict.)
   - **LLM:** add an entry to `core/providers/llm/factory.py` (`_BUILDERS` + `_PROVIDER_CLASSES`).
     *(LLM has not yet been migrated to a registry; if you do, mirror the TTS pattern and update this doc.)*
4. Add tests with the HTTP layer mocked — never hit a real API in unit tests.

## Branching & PRs (branch + PR for everything non-trivial — ALL repos)

**Rule (since 2026-07-15): never commit directly to `main`/`master` for non-trivial work, in
ANY repo** (`omnia`, `omnia-web-clipper`, `omnia-desktop-clipper`, and any future one). For
every **large feature, new feature, or bug fix**, behave like a professional engineer:

1. If on `main` (or the tree carries unrelated staged work), **create a branch first** —
   `git checkout -b feat/<short-kebab-topic>` (use `fix/…` for bug fixes, `chore/…` for
   maintenance, `docs/…` for documentation) — then implement on that branch.
2. **Push the branch and let the user merge the PR themselves** — surface the PR / compare URL.
   Do **not** merge into main yourself unless explicitly told to.
3. **Small standalone edits NOT tied to any feature** — adding a `.gitignore` entry, editing
   `.claude/*`, a one-line housekeeping fix — go **directly on main** (no branch/PR). The
   branch+PR rule is for feature/fix/refactor work; everything else a reviewer would want to see
   still goes through a branch + PR.
4. Push only when the work is ready; use the SSH alias `github-osirisqdt` for pushes to the
   osirisQdt2810 account.

This is the default even when the user does not say "make a branch" — non-trivial work implies
its own branch and its own PR.

### PR description format (MANDATORY — all repos, since 2026-08-14)

Every PR body uses `.github/pull_request_template.md`: **Context → Content / Changes → Test
Plan** (with `### Test Details` and `### Test Output / Feature Demonstration`), then the
repo's own checklist. GitHub pre-fills it in the web UI, but **`gh pr create --body` bypasses
the template**, so when opening a PR from the CLI, fill the template explicitly:

```bash
# Start FROM the template so no section is ever dropped
sed -e 's/^<!--.*-->$//' .github/pull_request_template.md > /tmp/pr-body.md   # then fill it in
gh pr create --base main --title "…" --body-file /tmp/pr-body.md
```

Rules for filling it:
- **Never delete a heading.** A section that truly does not apply gets `N/A — <reason>`, not a
  silent removal; the CI `pr-body` check fails a PR missing any required heading.
- **Test Output is evidence, not a claim.** Paste the actual `pytest` tail, the lint result,
  the CDP/manual-run output, or a screenshot. "Tests pass" on its own is not a test plan.
- The checklist at the bottom is where the recurring bugs live (ADR-010 config compatibility,
  cross-platform vendoring, `core/*` blast radius) — answer it, don't tick it blindly.

## Common Commands

```bash
# Dev deps (host venv)
pip install -r requirements/requirements-dev.txt

# Run tests (Anki stubbed; logic only)
pytest tests/ -vv

# A single test file
pytest tests/plugins/test_overdue_guard.py -vv

# Lint / format / types (pre-commit runs these on staged files)
pre-commit run            # staged
pre-commit run --all-files
mypy src/omnia            # manual (strict; not a commit gate yet)

# Vendor third-party deps into the add-on (pure-Python only)
python scripts/vendor_deps.py            # installs requirements-vendor.txt into vendor/ (repo root)

# Install into local Anki for manual testing (assemble src/omnia + vendor/models/config -> addons21/)
python scripts/install_addon.py

# Build the distributable add-on
python scripts/build_addon.py            # -> dist/omnia.ankiaddon

# Run the full check suite in the CI/test container
docker build -f deploy/Dockerfile -t omnia-ci . && docker run --rm omnia-ci
```

## Documentation Language
All documentation (README, `docs/`, `.md` describing a process/feature, code docstrings,
comments) **must be in English**, regardless of the conversation language. Exception: only
when the user explicitly asks for Vietnamese in a specific file.

## Coding Conventions
Standards live in **`.claude/CONVENTIONS.md`** — read it in full before non-trivial work.
Part 1 = universal Python (PEP 8, Google style, type hints, Black/Ruff/isort/mypy).
Part 2 = project-specific (FeaturePlugin/@register, config namespacing, vendoring &
cross-platform, logic/glue separation, the provider interface) + the **Agent Working
Principles** adapted from the karpathy guidelines (think before coding, simplicity first,
surgical changes, goal-driven execution).

## Project State Files (DYNAMIC — read at session start)
- **`.claude/JOURNAL.md`** — daily work log; newest on top. Read first to get recent context.
- **`.claude/FEATURE_LOG.md`** — one entry per large feature (append-only, newest on top).
- **`.claude/DECISIONS.md`** — ADRs; read before changing core patterns. Append-only.
- **`.claude/WORKFLOW.md`** — startup, agent workflow, pre-commit details.

**Feature-log rule:** after any large feature/change (a new feature plugin, a new
provider, a change to a shared seam), append an entry to `.claude/FEATURE_LOG.md`. Skip it
for tiny edits, typo fixes, and pure docs.

## Sub-agent Workflow

> **STATUS: ENABLED.** Use the specialist subagents for non-trivial feature/refactor work.
> All four run on **Claude Opus 4.8** (no Codex). Small tasks (<~20 lines) or obvious
> fixes: do them directly in the main session.

| Phase  | Agent                | Tools                   | Purpose |
|--------|----------------------|-------------------------|---------|
| Search | `Explore` (built-in) | read-only               | Locate code/patterns before planning. |
| Plan   | `planner`            | Read, Grep, Glob        | Produce the implementation plan; writes no code. |
| Build  | `coder`              | Bash, Read, Write, Edit | Implement the plan + tests directly; validates with the tooling. |
| Verify | `reviewer`           | Read, Grep, Bash        | Senior **solution-architecture** review: abstraction, reuse, cohesion, coupling — plus correctness, conventions, tests. |
| Fix    | `debugger`           | Read, Grep, Bash, Edit  | Root-cause a failing test/error; minimal fix. |

Typical order: **Explore → planner → coder → reviewer**, invoking `debugger` only when a
test or run fails. The `reviewer` is deliberately a solution architect: it judges whether
the design achieves abstraction, reuse, high cohesion, and low coupling — not just whether
the code runs.

## Slash Commands
- `/resume` — read JOURNAL.md and brief on where work left off
- `/daily-wrap` — update JOURNAL.md with today's session summary
- `/adr` — create a new Architecture Decision Record in DECISIONS.md
