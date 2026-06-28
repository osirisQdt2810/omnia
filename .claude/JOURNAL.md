# Project Journal

Daily work log. Newest entries at the top.

Format for each entry:

```
## YYYY-MM-DD (Weekday)

### Done today
- <completed item with file references where useful>

### In progress
- <half-finished item>
- File: `path/to/file.py:line`
- Status: <what's left>

### Decisions made
- <brief decision; if formal, link to DECISIONS.md ADR number>

### Next up
- <what to pick up next session>
```

---

## 2026-06-28 (Sunday)

### Done today
- Bootstrapped **Omnia**, an all-in-one pluginized Anki add-on, from a scaffold that had
  been copied from the `vio-ai` Flask/Celery/Supabase server.
- Mapped the three reference add-ons (`smart_notes`, `typed_accuracy`,
  `automatically_flip_cards`), the `vio-ai` LLM/TTS provider layer, and the
  `andrej-karpathy-skills` guidelines.
- Reconfigured the four subagents to run on Opus 4.8 and dropped Codex (`coder` writes
  directly); re-enabled the subagent workflow; sharpened `reviewer` into a
  solution-architecture role.
- Rewrote `.claude/CLAUDE.md` and `.claude/CONVENTIONS.md` for the Anki add-on reality
  (no server; plugin model; shared seams; vendoring; logic/glue separation) and added the
  karpathy "Agent Working Principles" as CONVENTIONS Part 3.
- Seeded the foundational ADRs (see DECISIONS.md).
- Deleted vio-ai server cruft (Flask/Celery/Supabase scripts, server docker-compose,
  requirements); added a CI-only Dockerfile; refactored pyproject/pre-commit for the add-on.
- Built the **core foundation**: registry + `FeaturePlugin`/`PluginContext` + `PluginManager`,
  the four shared seams (ease pipeline, web injector w/ per-card dynamic JS, provider layer,
  `anki_compat`), and a Pydantic v2 config layer over split YAML/TOML (`config/`) + user
  overrides. Reviewer-subagent audited the core; fixes applied (teardown bug, web uninstall,
  package-name alias, etc.).
- **OOP/SOLID** per user direction: `HttpClient` ABC injected into providers (DIP);
  `TokenSource` Strategy for Vertex auth (static/gcloud/service-account, RS256 verified);
  Gemini→GeminiVertex template-method inheritance. Providers: openai/openrouter/gemini/
  **gemini_vertex**, free google_translate TTS. Tests **sweep** all providers (mocked) +
  gated integration tests for real creds.
- Targeted **Python 3.13** (latest Anki's bundled interpreter; min 3.10).
- Implemented the **five feature plugins** + the **settings GUI** (Tools → Omnia). Reviewer
  audited features; fixes applied (typed_accuracy stale-pending clear, auto_flip terminal
  cancel, smart_notes apply error-handling, teardown + wait_for_audio tests added).
- **101 tests pass**, ruff/black/isort clean, `.ankiaddon` builds (48 files).
- Added more TTS providers (google_cloud REST, edge_tts, piper) — TTS now 7, LLM 5; sweep
  covers EVERY config (build + run + non-empty output). Each feature now declares
  `config_schema()` rendered as a generic Configure form in the settings dialog.
- **Vendored** pydantic/pydantic_core(cp313)/PyYAML/tomli_w/rsa/pyasn1 and **verified the
  add-on loads in the real Anki 25.09.2** (offscreen import: all 5 plugins register, config
  validates, GUI imports against real aqt.qt, all 8 gui_hooks exist).
- Adapted vio-ai's HTTP **retry/backoff** into `UrllibHttpClient` (injectable `RetryPolicy`).
- **121 tests pass**; symlinked into `~/Library/Application Support/Anki2/addons21/omnia`.
- Vendored the **Windows** `pydantic_core` wheel (cp313-win_amd64 `.pyd`) alongside the macOS
  `.so` — Python auto-selects by ABI tag, no loader needed.
- Added shared `anki_compat.reviewer_eval` / `main_web_eval` + pre-staged hooks/config for the
  bespoke UIs (so parallel feature work won't collide on shared files).
- **Reshaped the provider/config seam → per-provider subsections** for BOTH `[llm]` and `[tts]`
  (`provider` selects; `[llm.gemini_vertex]`, `[tts.google_cloud]`, …); folded `vertex.toml` into
  `[llm.gemini_vertex]` (deleted it + `VertexSettings`); shared `LLMModelSettings` base for
  text/image/embedding model ids; hub projects nested→flat so the factories stay flat-dict-based.
  Reviewer-audited; applied should-fixes (derived google-auth, cred isolation, hub coverage tests).
- **Real-LLM testing** adopted (user direction): `llm` marker + abstract `LLMProviderContract`
  with a Fake subclass (always) + a real `@llm` subclass (auto-skips w/o creds). Wired real Vertex
  creds from `vio-ai/config/vertex.toml` into gitignored `user_files/omnia.toml` and **ran it live**
  — which **caught 3 real bugs mocks hid**: OAuth needed form-encoding (`HttpClient.post_form`),
  Vertex needs `role:"user"`, reasoning models need a real `max_tokens` (+ parser hardening). Fixed
  + regression-tested.
- **Converted the whole test suite to `Test*` classes** (no bare `def test_*`); codified in
  CONVENTIONS. **133 tests pass** (incl. live Vertex), ruff/black/isort clean, Anki import OK.
- **Provider classification + full real sweep**: each provider declares `requires_api`;
  factories expose requiring-api vs keyless lists; per-provider real tests derive their marker
  from it (`@llm` / `@tts` / unmarked-keyless via `pytest.param(marks=…)`). Added `call_or_xfail`
  + `ProviderError.status_code` so quota/token/transient limits **xfail** (recorded, not failed).
  smart_notes tested end-to-end against each real LLM/TTS provider; offline `test_provider_metadata`
  guards the classification. **Moved real API keys out of tracked `providers.toml` into the
  gitignored `user_files/omnia.toml`.** Full run: **149 passed / 26 skipped / 2 xfailed**
  (gemini AI-Studio free-tier quota); live pass for gemini_vertex + openrouter + google_cloud TTS.
- **First git commit** (`main`): the whole project (no secrets — `user_files/` gitignored).

### Decisions made
- Project name: **Omnia** (package `omnia`). See ADR-001..004.
- Config: Pydantic v2 + YAML (high-level) / TOML (per-domain) in `config/`, not JSON.
- **Provider config is per-provider** (one `[llm.<p>]` / `[tts.<p>]` subsection each); factories
  stay flat-dict-based, the hub projects nested→flat. Google auth lives once in
  `[llm.gemini_vertex]` and `google_cloud` TTS reuses it.
- **Real-LLM testing is the default** (no `--fake-llm`): Fake subclass always runs; real `@llm`
  subclass runs the configured provider and auto-skips without creds. Live creds only in the
  gitignored `user_files/omnia.toml` / `OMNIA_TEST_CONFIG`.
- **All pytest tests are `Test*` classes** (no bare `def test_*`).
- Known limitation: `display_interval` reflects `overdue_guard` but not `typed_accuracy`
  (the latter's ease arrives async via pycmd after the overlay computes).

### Next up
- **Bespoke per-reference UIs** (the main remaining parity gap; shared seam prep + Windows wheel
  already done): smart_notes ✨ editor button + rich field-mapping dialog; typed_accuracy stats
  donut card; auto_flip reviewer countdown (show_timer); auto_flip deck-options panel. Each needs
  live-Anki iteration to verify. (Pre-staged: `anki_compat.reviewer_eval`/`main_web_eval`, conftest
  hooks, `TypedAccuracySettings.show_stats`, `AutoFlipSettings.per_deck` + `AutoFlipDeckOverride`.)
- Launch the real Anki GUI to click through the settings dialog + each feature live.
- `mypy src/omnia` and fill remaining type gaps (not a commit gate yet; mypy not yet in the venv).
