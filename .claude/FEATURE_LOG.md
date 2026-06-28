# Feature Log

Per-feature record of **large** changes, so anyone can see what was done and why
without re-reading the whole diff. Newest entries at the top.

One entry per large feature/change (a new feature plugin, a new provider, a change to a
shared seam). Skip tiny edits, typo fixes, and pure docs — those belong in `JOURNAL.md`
(daily) or nowhere.

Format for each entry:

```
## YYYY-MM-DD — <Feature / change title>

**What:** <1–3 sentences: what now exists or changed>
**Why:** <the goal / problem it solves>
**Files:** <key files added/modified — paths>
**How to verify:** <exact command(s) or steps to confirm it works>
**Notes / rollback:** <gotchas, follow-ups, how to undo if needed>
```

---

## 2026-06-28 — Real per-provider sweep + provider classification + xfail-on-quota

**What:** Expanded real-provider testing from one contract to a full per-provider sweep, both at
the provider level and through the feature that uses them. (1) **Provider classification**: each
provider declares ``requires_api`` (False for keyless/offline — google_translate, edge_tts,
piper); factories expose ``available_{llm,tts}_providers_requiring_api()`` /
``available_keyless_{llm,tts}_providers()``. (2) **Per-provider real tests** (one parametrized
case each) derive their marker from that classification: ``@pytest.mark.llm`` for LLM,
``@pytest.mark.tts`` for keyed/cloud TTS, UNMARKED (always-run) for keyless TTS — via
``pytest.param(..., marks=...)``. (3) **xfail-on-limit**: ``ProviderError.status_code`` +
``conftest.call_or_xfail`` turn a quota / rate / token / transient (incl. network) limit into
``xfail`` (recorded, not a failure); genuine wiring bugs still fail; no-creds → per-provider
skip. (4) **smart_notes** is now tested end-to-end against each real LLM (text/image) + TTS
provider. (5) Offline ``test_provider_metadata`` guards the classification (partitions all
providers; matches each class's ``requires_api``; name→class map matches the builders).
(6) **Security**: real keys moved out of the tracked ``providers.toml`` into the gitignored
``user_files/omnia.toml``.
**Why:** User wanted every LLM/TTS provider swept for real (keys now provided), markers split per
provider by what each actually needs, free/open-source providers to always run, and quota/token
limits reported as xfail rather than failing the suite.
**Files:** `core/providers/errors.py` (status_code), `core/providers/http.py`, `core/providers/
{llm,tts}/base.py` (requires_api), `tts/{google_translate,edge_tts,piper}.py`, `{llm,tts}/factory.py`
+ package `__init__`s + `providers/__init__.py` (classification fns), `pyproject.toml` (tts marker),
`tests/conftest.py` (call_or_xfail, per-provider builders), `tests/providers/{test_real_llm_providers,
test_real_tts_providers,test_provider_metadata}.py`, `tests/features/test_smart_notes_real.py`,
`config/providers.toml` (keys blanked).
**How to verify:** `pytest -m "not llm and not tts and not integration"` green offline;
`pytest -m "llm or tts"` runs live (gemini_vertex+openrouter+google_cloud pass, gemini xfails on
free-tier quota, no-creds skip); full `pytest` = 149 passed / 26 skipped / 2 xfailed.
**Notes / rollback:** Live keys ONLY in gitignored `user_files/omnia.toml` (or `OMNIA_TEST_CONFIG`).
edge_tts/piper skip unless their package/binary is installed. Adding a keyless LLM later flips
`available_keyless_llm_providers()` (today empty) and the metadata guard will confirm it.

## 2026-06-28 — Per-provider LLM/TTS config + real-LLM contract testing (+ 3 live-caught bug fixes)

**What:** Reshaped the provider/config shared seam. (1) **Per-provider config**: `[llm]` and
`[tts]` in `providers.toml` now have one subsection per provider (`[llm.gemini_vertex]`,
`[llm.openai]`, `[tts.google_cloud]`, …); `provider` selects the active one. Vertex auth was
folded into `[llm.gemini_vertex]` (deleted `vertex.toml` + `VertexSettings`); `google_cloud`
TTS reuses that Google auth, bridged by the hub. Shared `LLMModelSettings` base holds the
common `text_model`/`image_model`/`embedding_model`. The factories stay flat-dict-based —
`ProviderHub._llm_config`/`_tts_config` project the active nested subsection into the flat dict
(`text_model`→`model`), so the provider layer never sees config-file structure.
(2) **Real-LLM testing** (no `--fake-llm` flag): a `llm` marker + an abstract `LLMProviderContract`
with two always-collected subclasses — `TestFakeLLMContract` (canned, free) and `@pytest.mark.llm
TestRealLLMContract` (the configured provider; auto-skips without creds from an untracked
`user_files/omnia.toml`/`OMNIA_TEST_CONFIG`). (3) The live Vertex run **caught 3 real bugs the
mocks hid**: the OAuth2 token exchange sent a JSON body (added `HttpClient.post_form` form-encoding),
Vertex requires `contents[].role="user"`, and reasoning models starve on tiny `max_tokens`
(hardened the Gemini parser + budget). (4) **All pytest tests converted to `Test*` classes**
(no bare `def test_*`); convention codified in CONVENTIONS.
**Why:** User asked for per-provider config split (llm then tts), real-LLM testing as the default
behaviour, a shared model-id base, and class-based tests.
**Files:** `core/config/models.py` (nested LLM/TTS models + `LLMModelSettings`, `active()`,
`google_auth()`; removed `VertexSettings`), `core/config/{loader,repository,__init__}.py`,
`core/providers/__init__.py` (hub projection + auth bridge), `core/providers/http.py` (`post_form`),
`core/providers/token_source.py` (form exchange), `core/providers/llm/gemini.py` (role + parse),
`config/providers.toml` (new shape; deleted `config/vertex.toml`), `core/manager.py`, `pyproject.toml`
(`llm` marker), `tests/conftest.py` (FakeLLMProvider, `real_llm_provider_or_skip`), `tests/providers/
{test_llm_contract,test_provider_hub,test_http_retry,test_token_source}.py`, all `tests/**` (class-based),
`.claude/CONVENTIONS.md`.
**How to verify:** `.venv/bin/python -m pytest tests/ -m "not integration" -q` (133 pass with creds
wired, else 130 pass + 3 `llm` skipped); `-m "not llm and not integration"` stays free/offline;
ruff/black/isort clean; Anki bundled-python `import omnia` OK. Real Vertex path verified live
(project vio-ai-500116, gemini-2.5-flash).
**Notes / rollback:** Live `@llm` creds live ONLY in gitignored `src/omnia/user_files/omnia.toml`
(taken from `vio-ai/config/vertex.toml`) or `OMNIA_TEST_CONFIG` — never the tracked `providers.toml`,
so no secret is committed and CI without creds auto-skips. `embedding_model` is config-only
(reserved; no consumer yet).

## 2026-06-28 — More TTS providers, per-feature config GUI, vendoring + Anki-load verify, HTTP retry

**What:** (1) Added TTS providers from vio-ai — **google_cloud** (REST, reuses the Vertex
`TokenSource`), **edge_tts** (injectable `EdgeSynthesizer`), **piper** (injectable
`PiperRunner`); TTS now has 7 providers, LLM 5. (2) The provider **sweep** now builds + runs
+ asserts non-empty output for EVERY llm/tts config. (3) Each feature declares
`config_schema()`; the settings dialog renders a generic **Configure** form (write-back +
live reload). (4) **Vendored** pydantic/pydantic_core(cp313)/PyYAML/tomli_w/rsa/pyasn1 into
`src/omnia/vendor` and verified the add-on **loads in real Anki 25.09.2** (all 5 plugins
register, config validates, GUI imports, all 8 gui_hooks exist). (5) Adapted vio-ai's HTTP
**retry/backoff** into `UrllibHttpClient` via an injectable `RetryPolicy`. Moved `TokenSource`
to `core/providers/` (shared by gemini_vertex + google_cloud).
**Why:** User asked for more TTS types, a complete config sweep, real per-feature settings UI,
and to confirm the add-on runs in Anki; adapt valuable vio-ai core.
**Files:** `core/providers/tts/{google_cloud,edge_tts,piper,factory}.py`,
`core/providers/token_source.py`, `core/providers/http.py` (RetryPolicy), `core/providers/__init__.py`,
`core/config/models.py`, `config/providers.toml`, `gui/{config_form,settings_dialog}.py`,
`features/*/__init__.py` (config_schema), `src/omnia/vendor/`, `tests/providers/*`, `tests/features/test_config_schema.py`.
**How to verify:** `pytest tests/ -m "not integration"` (121 pass); vendoring +
`QT_QPA_PLATFORM=offscreen "<AnkiProgramFiles>/.venv/bin/python" -c "import omnia"` loads clean.
**Notes / rollback:** Vendored `pydantic_core` is **cp313 macOS arm64 only** — Windows needs
its own wheel + a platform-selecting loader (TODO). Per-reference bespoke UIs (smart_notes ✨
editor button, typed_accuracy stats card, auto_flip reviewer countdown, deck-options) are NOT
yet built — only the unified settings dialog + per-feature config form exist.

## 2026-06-28 — Five feature plugins + settings GUI

**What:** Implemented the five bundled features as thin `FeaturePlugin`s on the shared
seams: `auto_flip` (timed auto-advance), `typed_accuracy` (typing-accuracy → ease via JS +
pycmd + ease transformer), `display_interval` (per-card answer-side overlay), `overdue_guard`
(forces overdue cards to Hard/Again via an ease transformer), and `smart_notes` (LLM
text/image + TTS field generation from the Browser, off the UI thread). Added the
card-based settings dialog (Tools → Omnia) listing every plugin with a live enable toggle.
**Why:** Deliver the user's target features and the "tick to enable" all-in-one UI; prove the
pluginize architecture (typed_accuracy was split into 3 cooperating plugins).
**Files:** `src/omnia/features/{auto_flip,typed_accuracy,display_interval,overdue_guard,smart_notes}/`,
`src/omnia/features/__init__.py`, `src/omnia/gui/settings_dialog.py`, `tests/features/*`.
**How to verify:** `.venv/bin/python -m pytest tests/ -m "not integration"` (101 pass);
`python scripts/install_dev.py` then in Anki open Tools → Omnia and toggle features.
**Notes / rollback:** Each plugin fully tears down on disable (verified by tests). Known
limitation: `display_interval` reflects `overdue_guard` (synchronous) but not `typed_accuracy`
(its ease arrives async via pycmd after the overlay computes) — documented in its docstring.

## 2026-06-28 — Core foundation: plugin system, shared seams, provider layer, typed config

**What:** Built the modular monolith: `@register` registry + `FeaturePlugin`/`PluginContext`
+ `PluginManager` lifecycle; four shared seams (reviewer **ease pipeline** with one
`_answerCard` wrap + ordered transformers, **web injector** for reviewer JS/CSS + pycmd
routing + per-card dynamic JS, **provider layer** with `LLMProvider`/`TTSProvider` +
`HttpClient` DIP + `TokenSource` Strategy, `anki_compat` shims); and a Pydantic v2 config
layer loading split YAML/TOML defaults (`config/`) + user overrides (`user_files/omnia.toml`).
LLM: openai-compatible, gemini, **gemini_vertex** (service-account/gcloud/token). TTS: free
google_translate + openai-compatible.
**Why:** Make features thin and cooperative; adapt vio-ai's provider design; OOP/SOLID.
**Files:** `src/omnia/core/**`, `src/omnia/config/**`, `src/omnia/__init__.py`,
`tests/core/**`, `tests/providers/**`.
**How to verify:** `.venv/bin/python -m pytest tests/ -m "not integration"`; provider sweep
covers every LLM/TTS provider (mocked); set `OMNIA_IT_VERTEX_PROJECT`/creds + `pytest -m
integration` for real Vertex.
**Notes / rollback:** Runs in Anki 3.13. `pydantic_core` (binary) + `rsa`/`pyasn1` (for
Vertex service-account auth) must be vendored per-platform — see requirements-vendor.txt.

*(Add feature entries below, newest first.)*
