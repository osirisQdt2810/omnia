# Daily Workflow

Quick reference for working on **Omnia** (a pluginized Anki add-on) with Claude Code.

---

## First-time setup (once)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements/requirements-dev.txt
pre-commit install            # installs git hooks into .git/hooks/
```

Verify hooks:
```bash
pre-commit run --all-files    # dry run on everything
```

---

## Start a session

```bash
cd /Users/phucnp/workspace/genesis/anki/addons/addons
claude
```

Inside Claude Code, run `/resume` to read JOURNAL.md and get oriented.

---

## The build/verify loop (host venv — no container)

```bash
pytest tests/ -vv                 # logic tests (Anki stubbed via tests/conftest.py)
pre-commit run                    # Black + Ruff + isort + hygiene on staged files
mypy src/omnia                    # types (manual; strict; not a commit gate yet)
```

Manual testing in real Anki:
```bash
python scripts/install_dev.py     # symlink src/omnia into the local Anki addons21/
# then launch Anki and open Tools → Omnia
```

Build a distributable:
```bash
python scripts/build_addon.py     # -> dist/omnia.ankiaddon
```

---

## Subagent workflow (ENABLED — all on Opus 4.8)

For non-trivial features/refactors:
1. **Explore** (built-in) — locate the relevant seams, plugins, tests.
2. **planner** — produce the plan (files, signatures, edge cases). No code.
3. **coder** — implement the plan + tests directly; validate with the tooling.
4. **reviewer** — solution-architecture review (abstraction, reuse, cohesion, coupling) +
   correctness, conventions, tests.
5. **debugger** — only when a test/run fails; root-cause + minimal fix.

Skip the flow for small tasks (<~20 lines) or obvious fixes — do them directly.

---

## Pre-commit details

`.pre-commit-config.yaml` runs on **staged files only**:
- trailing-whitespace, check-yaml, check-json, check-added-large-files (>5MB)
- isort (profile=black) → Black → Ruff (`--fix`)

mypy is intentionally **not** a commit gate yet (the package is not fully typed for
`strict`); run it manually / in CI as typing is filled in.

Never run `black .` / `ruff check .` / `mypy .` at the repo root — only on changed files
(see CONVENTIONS.md → "Convention Enforcement on Existing Code"). Do NOT lint the
vendored `src/omnia/vendor/` tree or the `references/` snapshots.

---

## End a session

Run `/daily-wrap` to append today's summary to JOURNAL.md. After a large feature, also add
an entry to FEATURE_LOG.md.
