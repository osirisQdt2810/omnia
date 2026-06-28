# Coding Conventions

This file defines coding standards for **Omnia** (a pluginized Anki add-on). All code must
comply. The `reviewer` agent uses this file as its rubric.

## How this file is organized

Three parts:
- **Part 1 — Universal Python Standards**: language-level, professional conventions that
  apply to any modern Python project (PEP 8, Google Python Style Guide, best practices).
- **Part 2 — Project-Specific Rules**: rules unique to *this* Anki add-on — the plugin
  model, the shared seams, vendoring, cross-platform, logic/glue separation.
- **Part 3 — Agent Working Principles**: how the agent should *work* (adapted from the
  karpathy guidelines): think before coding, simplicity first, surgical changes,
  goal-driven execution.

When standards conflict, Part 2 overrides Part 1, and both override personal preference.
Part 3 governs *behavior*, not code shape.

---

# PART 1 — Universal Python Standards

These define what professional Python looks like at companies like Google, Meta, Anthropic.

## Style Guide References
- **PEP 8** — https://peps.python.org/pep-0008/
- **Google Python Style Guide** — https://google.github.io/styleguide/pyguide.html
- **PEP 484 (type hints)**, **PEP 257 (docstrings)**, **PEP 604 (unions)**, **PEP 585 (generics)**

## Tooling (enforced automatically)

| Tool | Purpose | Config |
|---|---|---|
| Black | Code formatter | line length 88, target Python 3.13 |
| Ruff | Linter | rules: E, F, W, N, UP, B, SIM, RUF |
| isort | Import sorter | profile = "black" |
| mypy | Type checker | strict mode (manual / CI; not a commit gate yet) |
| pytest | Test runner | with `pytest-mock` |
| pre-commit | Git hooks | runs Black, Ruff, isort, hygiene on staged files |

Run checks locally before committing:
```bash
pre-commit run
pytest tests/ -vv
```

## Type Hints — Required
- **Mandatory** on all public function/method parameters and return values.
- Use `from __future__ import annotations` at the top of every file (keeps annotations as
  strings — cheap, import-safe, and avoids runtime evaluation surprises).
- Generic types: `list[str]` not `List[str]` (PEP 585).
- Unions: prefer `X | None` (PEP 604). `Optional[X]` is tolerated where it reads clearer.
- No bare `Any` without a comment explaining why.

```python
from __future__ import annotations

def accuracy_ratio(good: int, bad: int, missed: int) -> float:
    ...
```

## Naming

| Item | Convention | Example |
|---|---|---|
| Module | `snake_case` | `ease_pipeline.py` |
| Function/method | `snake_case` | `register_transformer` |
| Variable | `snake_case` | `card_id` |
| Class | `PascalCase` | `FeaturePlugin` |
| Constant | `UPPER_SNAKE_CASE` | `DEFAULT_THRESHOLD` |
| Private | prefix `_` | `_apply_ease` |
| Test file | `test_*.py` | `test_overdue_guard.py` |
| Test class | `Test*` (PascalCase) | `TestOverdueRule` |
| Test method | `test_*` | `test_forced_ease_when_overdue` |

## Docstrings (Google Style)
Required on every public function/method, every class, and every module (top-of-file).
```python
def forced_ease(card: Card, requested: int) -> int | None:
    """Return the ease an overdue card should be graded at.

    Args:
        card: The card being answered.
        requested: The ease the user/another feature requested (1-4).

    Returns:
        The forced ease (1 or 2), or None to leave ``requested`` unchanged.
    """
```

## Error Handling
- **No** bare `except:` / `except Exception:` without re-raising — catch specific types.
- **No** silent failures (`except: pass`).
- Log with context; re-raise after logging unless at a process/UI boundary.
- In Anki glue, surface failures to the user (a dialog / tooltip), never swallow silently.

## Imports
- isort order: stdlib → third-party → local (blank line between groups).
- No wildcard imports. Absolute imports preferred.
- **Lazy-import heavy or optional deps inside functions**, not at module top.

## Testing
- pytest only. File `test_<module>.py`. **Tests are grouped in `Test<Topic>` classes** —
  every test is a method `test_<behavior>_<condition>(self, …)`; **no bare module-level
  `def test_*` functions**. Module-level helpers/fixtures stay module-level (prefixed `_`).
- Coverage target ≥80% for new logic in `core/` and feature logic modules.
- Use `pytest.fixture` for setup (fixtures inject into methods normally).
- Anki is stubbed in `tests/conftest.py`; pure logic should be testable without it.
- **Provider functionality uses the contract pattern, not mock-only.** Default unit tests
  mock the HTTP/transport layer (inject a fake), but each provider's behaviour is also
  exercised against the REAL provider via an abstract base test class with two subclasses: a
  `Fake…` subclass that always runs (free, offline) and a real subclass marked
  `@pytest.mark.llm` (LLM) or `@pytest.mark.integration` (other live APIs) that builds the
  configured provider and **auto-skips when credentials are absent** (so CI stays green).
  Real creds come from an untracked override (`user_files/omnia.toml` or `OMNIA_TEST_CONFIG`),
  never the tracked bundled config. Run `-m "not llm and not integration"` to stay free.

## Code Organization
- Single Responsibility: 1 class = 1 clear purpose.
- Soft limits: function < 50 lines, class < 300 lines — split if larger.
- Constructor injection for dependencies — **no** global mutable state, no singletons
  (the one allowed singleton is the `PluginManager`, created once at add-on startup).

## Security
- **Never** hard-code secrets/API keys. Provider keys live in the config store.
- Validate input at boundaries (config values, `pycmd` payloads, note fields).
- **Never** log API keys, auth tokens, or PII.

## Comments
- Comments explain **why**, not **what**. No commented-out code (Git remembers).
- TODO format: `# TODO(username): description`.

## Git Commit
- Conventional Commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.
- One commit = one logical change. Subject ≤72 chars, English, imperative.

## Convention Enforcement on Existing Code
"Touch it, fix it": bring lines you change up to standard; don't auto-format surrounding
unchanged lines. New files: full compliance. Pre-commit runs on **staged files only** —
never `black .` / `ruff check .` at the repo root.

---

# PART 2 — Project-Specific Rules (Omnia / Anki add-on)

## The plugin model (use it; don't bypass it)
- Every feature is a `FeaturePlugin` subclass in `src/omnia/features/<name>/`, registered
  with `@register("<id>")` from `core/registry.py`, and imported in
  `features/__init__.py` so the decorator runs at load time.
- A plugin implements `on_enable(ctx)` / `on_disable(ctx)` and must **fully tear down** on
  disable — remove its hooks, ease transformers, injected assets, and pycmd handlers. A
  user toggling a feature off at runtime must leave no trace behind.
- Plugins receive everything they need via the `PluginContext` (config, logger, the
  reviewer seams, providers). **No global state**, no reaching into `mw` for config.

## Use the shared seams — never patch Anki from a feature
- **Ease changes** (rewriting the graded ease) go through `core/reviewer/ease_pipeline`.
  Register an ordered transformer; never wrap `Reviewer._answerCard` from a feature.
- **Reviewer JS/CSS** goes through `core/reviewer/web_injector`. Register assets + a
  `pycmd` handler keyed `omnia:<plugin>:<op>`; never call `web.eval` ad-hoc from a feature.
- **LLM/TTS** goes through `core/providers`. Features depend on `LLMProvider` /
  `TTSProvider` interfaces, never on a concrete SDK.
- **Config** goes through `ConfigStore` namespaced by plugin id. Never call
  `mw.addonManager.getConfig/writeConfig` directly from feature code.

## Object-Oriented Design (SOLID) — maximize classes with a real responsibility
- **Prefer a class** wherever there is a genuine responsibility, state, or >1 variant.
  Before writing a module-level function, ask: *should this be a method on a class?* Use a
  plain function ONLY for a genuinely stateless, single-purpose pure helper.
- **SRP**: one class = one reason to change. Split god-objects.
- **OCP/LSP**: model variants as a small ABC/Protocol + concrete implementations behind it
  (e.g. `LLMProvider`/`TTSProvider`, `HttpClient`, `TokenSource`). New variant = new subclass,
  no edits to existing ones. Subclasses must be substitutable for their base.
- **Template method** for shared-flow/variant-hook (e.g. `GeminiProvider` →
  `GeminiVertexProvider` overrides only `_endpoint`/`_headers`).
- **Strategy** for interchangeable behaviors selected at runtime (e.g. `TokenSource`
  strategies resolved from config).
- **ISP**: keep interfaces small — depend on the narrowest surface needed.
- **DIP**: inject collaborators through the constructor (e.g. providers receive an
  `HttpClient`; `TokenSource` receives http/signer/clock). Don't reach for module globals;
  this also makes tests inject fakes instead of monkeypatching.
- This is in tension with "simplicity first" (Part 3) — resolve it by earning each
  abstraction with a real need (state, a variant, or an injection seam), never speculatively.

## Coupling & cohesion (the design we protect)
- `features/*` → may import `core/*`. **`core/*` must NOT import `features/*`.** No
  circular deps. Seams are feature-agnostic.
- **Logic/glue separation:** put pure logic (accuracy math, overdue rule, provider HTTP,
  config validation) in modules that do **not** import `aqt`/`anki` at top level. The Anki
  glue is a thin shell around tested logic.

## Vendoring & cross-platform (macOS + Windows)
- Vendored deps go in `src/omnia/vendor/` and must be **pure-Python, no compiled wheels**.
  If a dep needs a C extension, find a pure-Python alternative or call the REST API with
  stdlib/`requests`. Flag any binary dep in review.
- Use `pathlib`, not string path math. No `os.system`, no POSIX-only assumptions, no
  hard-coded `/tmp`. Don't assume `ffmpeg`/other binaries are on PATH.
- Target Python 3.13 (latest Anki's bundled interpreter); keep code 3.10+-compatible
  (Anki's minimum supported Python).

## Threading (never block the Qt main thread)
- Network/LLM/TTS/file-heavy work runs off the main thread via `QueryOp` / `mw.taskman`;
  results are applied back on the main thread (`mw.taskman.run_on_main` or the QueryOp
  success callback). Timers use `mw.progress.timer(..., repeat=False)`.

## Config schema
- `config.json` holds an `enabled` map (`{"<plugin_id>": bool}`) plus one nested object per
  plugin id for that plugin's settings. Provide sane defaults for everything.
- A plugin declares its options via `config_schema()` so the settings GUI can render them
  generically where practical.

## Logging
- Use the add-on logger from `core/logging`. Include the plugin id in messages.
- Never log secrets, auth tokens, raw note content beyond what's needed, or PII.

## When you discover a new project rule
1. Mention it in `/daily-wrap` (→ JOURNAL.md). 2. If significant, raise an ADR via `/adr`.
3. Once stable, add it here. Never add personal preference — only correctness/consistency.

---

# PART 3 — Agent Working Principles

Adapted from the *karpathy guidelines* (`references/andrej-karpathy-skills`). These govern
how the agent should work, not how code is shaped.

## 1. Think before coding
- State assumptions explicitly. If genuinely uncertain, ask — don't pick silently.
- If multiple interpretations exist, surface them. Push back when a simpler path exists.

## 2. Simplicity first
- The minimum code that solves the problem. No features beyond what was asked.
- No abstraction for single-use code; an abstraction is earned by ≥2 real call sites.
- No "flexibility"/config that wasn't requested. No error handling for impossible cases.
- Heuristic: "Would a senior engineer call this overcomplicated?" If yes, rewrite. If 200
  lines could be 50, rewrite. (This sits in tension with Part 2's seams — the seams *are*
  the earned abstractions here, justified by multiple features; do not invent new ones
  speculatively beyond them.)

## 3. Surgical changes
- Touch only what the task requires. Every changed line traces to the request.
- Don't "improve" adjacent code, comments, or formatting. Match existing style.
- Remove imports/vars/functions *your* change made unused; leave pre-existing dead code
  (mention it, don't delete it) unless asked.

## 4. Goal-driven execution
- Turn imperative tasks into verifiable goals:
  - "Add validation" → "write tests for invalid inputs, then make them pass".
  - "Fix the bug" → "write a test that reproduces it, then make it pass".
- For multi-step work, state a brief plan with a verification check per step, then loop
  until every check is green.
