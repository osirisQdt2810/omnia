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

## 2026-08-14 (Friday)

### Done today
- **CI that reviews and merges itself** (`.github/workflows/pr-pipeline.yml`, `claude.yml`, in all
  three repos). Every PR runs pytest on ubuntu+windows+macos (py3.13) plus py3.10, then a Claude
  review on **Opus 5** that must emit `VERDICT: APPROVE|BLOCKING`; a PR carrying the `automerge`
  label merges itself only when tests are green, the verdict is APPROVE, and the head commit is
  still the one that was reviewed (`--match-head-commit`). 11 of today's 12 PRs merged with no
  human click. Known permanent exception: a PR that edits `.github/workflows/` cannot pass review
  (the Claude app refuses a token when workflow content differs from the default branch), so those
  always need a manual merge.
- **`note_maintenance`** — the 7th feature plugin: deterministic batch note clean-up, ported from
  a standalone note-updater CLI (its AnkiConnect transport dropped for in-process `mw.col`).
  Browser context menu → diff preview → `CollectionOp` apply.
- **Two shared seams**: `core/lang/word_forms` + `core/audio/wav` (groundwork for cloze/cloze_audio),
  and `core/config/base.py` `StrictModel`/`PersistedModel` (ADR-010).
- **Bug fixes with a live cause**: three TTS bugs; Windows `SO_REUSEADDR` letting a second
  `LookupService` bind an already-served port; every retired `gemini-*` model id.
- **History rewritten**: 192 commits collapsed to one author (`osirisQdt2810` + GitHub noreply),
  which removed a stray contributor that a wrong `user.email` had introduced in the 2026-06-28
  foundation batch; the `Co-Authored-By` trailer now survives only on `feat` commits. Tree hash
  before and after is identical — metadata only.

### In progress
- **smart_notes tools Phase 4** — the global Tools tab and user-authored tools
  (branch `feat/smart-notes-user-tools`). Phases 0-3 are merged; see the tools section below.
  Spec and build log (removed once shipped) recorded what
  actually shipped versus what the phase text describes.
- Windows testing is PAUSED at the user's request until they migrate the session to that machine;
  SSH works both ways and `D:\workspaces\genesis\anki\addons\omnia` is an SSHFS symlink back to
  this Mac, so it is the same tree, not a copy.

### Decisions made
- ADR-010 — persisted config models tolerate unknown keys. `extra="ignore"` was implemented FIRST
  and rejected: because the dialog re-serializes the whole tree on save, ignoring unknown keys makes
  an older device write a stripped blob back and destroy a newer device's config. `allow` retains
  and round-trips.
- The tools plan's five open questions were answered by the user: cloze_audio hard-fails when it
  cannot mask (never silently speak the answer); user-authored tools are real Python files under
  `user_files/tools/` (NOT synced config, which would make them cross-device code execution);
  cloze_audio must cover every TTS backend, so the `av` sidecar is required scope, not optional.

### Gotchas worth remembering
- **`import tomllib` is banned by ruff** (`TID251`): it is stdlib only from 3.11 and Anki supports
  3.10, so a bare import passes locally and on the 3.13 legs then fails collection on the 3.10 one.
  Use the guarded form with the vendored `tomli`.
- **A dead value usually lives in more than one place.** The retired gemini id was in the template,
  the model default, the factory fallback AND the settings picker; fixing only the template left a
  user who writes just an `api_key` still broken. The review caught it — worth assuming N sources
  by default.
- Real-provider tests only run where credentials exist, so **CI can be green while the add-on is
  broken for real users**. Run the full suite locally before believing a provider works.
- **Test every interpreter between the minimum and the bundled one.** 3.10 and 3.13 were covered
  and the middle was not; a `MappingProxyType` dataclass default — legal from 3.12 — raises at
  CLASS-CREATION time on 3.11, so the whole add-on failed to import. CI now runs 3.10-3.13 (#29).
- **A matcher shared between a fail-open consumer and a fail-safe one cannot simply be reused.**
  `ClozeRewriter` discards a hit that reads across a tag — correct for the text `cloze` tool,
  which edits the original markup; fatal for `cloze_audio`, where the unmasked occurrence then
  gets spoken. The fail-safe caller must run it on input where the doubt cannot arise (here: the
  already-stripped text) and verify its own output afterwards. ADR-011.

### The tools system (the day's main thread)

Four phases, shipped in order, each behind its own PR and review:

- **Phase 1 (#26)** — `ToolRegistry` + `GenerationPipeline` + `AiTool`, with **zero behaviour
  change**, proved by a golden test written against the pre-refactor tree AND a 27-scenario
  differential harness diffing this branch against `main`.
- **Phase 2 (#27)** — the `cloze` tool and the per-row Tools picker. A probe run BEFORE writing
  any code found that `word_variants` de-inflects inflected→base while the headword field holds
  the lemma — so the tool as specified would have matched nothing and silently fallen through to
  the LLM every time.
- **#28** — 244 irregular forms, at the user's suggestion. Exposed two bugs: an ambiguous
  irregular (`left`→`leave`) made a "left" card cloze every "leave" in its sentence, and the
  3.11 import break above.
- **Phase 3 (#30)** — `cloze_audio`. Four review rounds, each finding a different way to speak
  the answer: params parsed before the guard; chain order (`[ai, cloze_audio]`, which the
  picker's own append-to-end produces); a "safe decline" that reasoned only about its own field;
  and the shared-matcher trap above. Also fixed a bug affecting ALL TTS: `strip_markup` decoded
  six hardcoded entities, so `caf&eacute;` was read out as its literal characters.

**Process note:** delegating each fix round to a coder+reviewer pair cost ~40 minutes per round.
For a well-specified surgical fix, doing it directly is minutes. Keep the agents for broad survey
work and adversarial review, where they earn their cost.

### Next up
- Phase 4: the global Tools tab and user-authored tools (real Python under `user_files/tools/`,
  NOT synced config — approving code on one device must not execute it on every other one).
- Windows testing is still paused at the user's request; SSH works both ways and
  `D:\workspaces\genesis\anki\addons\omnia` is an SSHFS symlink back to this Mac.

---

### ⇨ SESSION HANDOFF (2026-06-29, session 2 — Smart Notes provider/UI bug-fix pass)

**SUPERSEDED — the paths below are stale.** The repo is now `<repo root>`
(there is no inner `addons/addons`), and the suite is run with `.venv/bin/pytest tests/ -q` from
that directory. Kept for the provider/UI context only.

**WORKDIR:** all work is in the inner repo `<repo root>`
(NOT the outer `…/anki/addons`). `cd` there before running anything. **Gotcha:** `.venv/bin/python`
is RELATIVE to that dir — don't `cd` into a subdir (e.g. `src/.../web`) without `cd`-ing back, or
`.venv/bin/python` won't resolve.

**Status:** 5 tasks from the user (screenshots of the ⚙ Options → Usage & Keys dialog). Tasks
1,2,3,5 DONE; Task 4 (test redesign) DONE pending a final clean re-run. **UNCOMMITTED** (user
commits only when they say so). Offline suite GREEN: `497 passed, 14 skipped, 93 deselected` via
`.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"`. Real provider run:
`.venv/bin/python -m pytest tests/providers/test_llm.py tests/providers/test_tts.py -m "llm or tts"`
→ `25 passed, 33 skipped, 49 deselected, 9 xfailed, 0 failed` (the 9 xfails = gemini AI-Studio
free-tier quota; gemini_vertex LLM + google_cloud/edge_tts/google_translate TTS all PASS real).
Page JS `node --check` OK.

**What each task was + the fix:**
1. **gemini-3.5-flash wrongly shown in the Image usage tab** — root cause: image generations were
   recorded under the TEXT model. `RecordingLLMProvider` now also takes `image_model` and records
   `generate_image` under it (fallback to text model); `ProviderHub._record_llm` passes the config
   so `config["image_model"]` flows through. Files: `core/providers/usage.py`,
   `core/providers/__init__.py`. Tests: `tests/providers/test_usage.py`
   (`test_image_recorded_under_image_model_not_text_model`, hub `test_image_call_records_under_image_model`).
   NOTE: pre-existing rows in `user_files/usage.json` from before the fix may still show the old
   mislabel — that's stale data, not re-introduced.
2. **Generated image overflowed the dialog** — now NEVER inlined. The playground + prompt-editor
   preview show a `🖼️ Image generated …` line + a `🔍 Preview image` button that opens a
   borderless full-screen **lightbox** (click/Esc to close) over the whole UI. Files:
   `gui/smart_notes/web/{page.html (#sn-lightbox),page.css (.sn-lightbox/.sn-img-result/.sn-img-preview;
   removed .sn-acct-img),01-bridge.js (handles),04-modal.js (openLightbox/closeLightbox+imageResultNode),
   05-handlers.js (playground branch)}`, `gui/smart_notes/dialog.py` (image message text).
3. **Sound playground HTTP 400** ("language code 'en-US' doesn't match the voice 'vi-VN-Neural2-A'")
   — google_cloud TTS now derives the `languageCode` from the chosen VOICE name
   (`vi-VN-Neural2-A` → `vi-VN`); the voice wins over a mismatched configured/detected lang. File:
   `core/providers/tts/google_cloud.py` (`_language_code_from_voice` + `_resolve_lang(lang, voice)`).
   Offline regression: `tests/providers/test_tts.py::TestGoogleCloudLanguageCode`.
4. **Test redesign** (requested shape: one class per provider and one per TTS voice, exercising
   both the with-LLM and without-LLM paths against the real services): real tests are now ONE
   class per provider — `Test<Provider>RealLLM` (5, all
   `@llm`) and `Test<Provider>RealTTS` (7; keyless google_translate/edge_tts/piper UNMARKED so they
   run by default, keyed/cloud `@tts`). Each auto-skips with a clear reason when it can't run (no
   creds/package/runner) — never a fake pass. **Installed `edge-tts`** into `.venv` + added
   `edge-tts>=7.0` to `requirements/requirements-dev.txt` so the keyless edge_tts case RUNS for
   real. `conftest.is_provider_limit_error` now xfails on HTTP 402 + billing/credit/insufficient
   markers (OpenRouter at $0 credit is an env constraint, not a code bug). Files:
   `tests/providers/test_llm.py`, `tests/providers/test_tts.py`, `tests/conftest.py`.
5. **Keys UI too tall → pushed the dialog off-screen** — (a) the per-card **Save** button moved
   into the card HEADER row, right-aligned (`.sn-key-actions`, removed the `.sn-key-foot` row);
   (b) `.sn-modal-card` capped at `max-height: calc(100vh - 36px)` and the active Options pane
   scrolls INSIDE (`.sn-options-card` + `.sn-tabpane:not([hidden])` flex/overflow), so a tall
   subtab never overflows the screen. Files: `gui/smart_notes/web/{05-handlers.js,page.css}`.

**Next up:** final clean `-m "llm or tts"` re-run (last one was interrupted); `node --check` on the
concatenated `web/0*.js` (unchanged since last green check); ruff/black/isort; consider a
FEATURE_LOG entry; **real-Anki visual check still PENDING** (no `aqt` here). Then commit when the
user says so (never stage `config/*.toml` or `secrets/*`).

---

## 2026-06-29 (Monday)

A very large session: a full **Smart Notes redesign** (the /goal 9-item spec + many follow-ups),
a **config-layer refactor**, **collection-DB persistence**, a **deck-scope** feature, and a
**voice/language** rework. All committed to `main` (4 commits, below). Offline tests:
`415 passed, 19 skipped, 93 deselected` via `.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"`.

---
### ⇨ SESSION HANDOFF (2026-06-29, read this first) — resume here in a new session

**Where things stand.** On `main`, working tree has **4 committed commits (batch 0)** + a large
pile of **UNCOMMITTED** work (batches 1, 2, 3, 3b — all below). Offline suite is **green: 486
passed, 19 skipped, 93 deselected** via
`.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"`. ruff/black/isort clean. Page JS
valid (`node --check` on the concatenated `gui/smart_notes/web/0*.js`).

**Dev-env reality (unchanged):** `.venv` (Python 3.14) has `anki` but **NOT `aqt`**, so the GUI /
real-Anki paths can't run here — every GUI change below is **UNVERIFIED visually**; the user must
open Anki to confirm. Offline pytest MUST use `-m "not llm and not tts"`. Logging is file-only.
Never echo / commit secrets (live `config/*.toml` + `secrets/*` are gitignored; only
`*.example.toml` tracked).

**What's uncommitted (all done + green), by batch:**
- **Batch 1** — Account dialog: per-subtab **default-model picker** (text/image/sound → central
  `[llm]`/`[tts]`); **Auto-smart → "Auto-prompt"** (op name `auto_smart` kept); centered table
  headers; General-tab multi-line tooltips.
- **Batch 2** — **🔑 Keys subtab** (provider credential cards), token-usage tables, honest credit
  story (live bar OpenRouter-only).
- **Batch 3** — **Secrets OUT of config**: `core/config/secrets.py::SecretsStore` (`secret:` =
  file content for keys/tokens, `secret-file:` = path for the Vertex JSON); `ConfigRepository`
  resolves refs after every load; writes go value→file+ref. Plus fixes: one Save per card; Keys↔kind
  pane overlap (`.sn-keys[hidden]{display:none}`); **image renders** in playground + modal (`data:`
  URI); **per-subtab playground** state + sound **"Play again"** (`replay_audio`); "Test playground"
  section header; OpenRouter credit re-fetch after Save + `total=0` handling; fixed sound playground
  sending `kind:"sound"` (engine needs `"tts"`).
- **Batch 3b** — Account tab label → **"Usage & Keys"**; secret filename convention →
  **`<domain>.<provider>.<field>`** (dotted; domain kept to avoid `llm.openai`/`tts.openai`
  collision); **Vertex `project` now optional** — derived from the SA JSON's `project_id`
  (`token_source.service_account_project` → `factory._build_gemini_vertex`).

**The user's live data was migrated IN-PLACE (already done — do NOT re-run):** gemini + openrouter
api keys and the Vertex SA JSON were moved into `src/omnia/secrets/` (files
`llm.gemini.api_key`, `llm.openrouter.api_key`, `llm.gemini_vertex.credentials_path.json`) and
`src/omnia/config/providers.toml` rewritten to `secret:`/`secret-file:` refs. (Migration scripts
were in the session scratchpad, which is GONE in a new session — that's fine, the migration is
applied. Round-trip verified; JSON `project_id` == inline `<gcp-project-id>`.)

**OPEN — needs the user before committing:**
1. **Convention decision (asked, awaiting answer):** keep `<domain>.<provider>.<field>` (current,
   collision-safe) vs the user's literal `<provider>.<field>`. If they say drop the domain: change
   `ConfigRepository._secret_name` + re-rename the secret files + rewrite refs.
2. **Commit:** the user wants to **commit only when they say so**, grouped by main points
   (batches 1+2+3+3b → a few cohesive commits). Commit to `main`. Never stage `config/*.toml`,
   `secrets/*`, or any cred (verify `git diff --cached` for key prefixes first).
3. **GUI verify on real Anki** (pending): ⚙ Options → **Usage & Keys** → default picker persists;
   🔑 Keys: one Save, 👁 reveal, Browse Vertex JSON (copies into secrets/ + auto-renames), blank
   Project ID still works, OpenRouter bar refreshes after Save; image shows in playground;
   per-subtab playground isolation + Play again; centered headers; General tooltips.

**Auth model (clarified to the user, important):** Omnia has **NO account/email/login** — provider
authorization is purely the per-provider API key / Google service-account the user supplies.
Secrets are **machine-local by design** (AnkiWeb syncs the collection, NOT add-on files); moving
machines = copy `secrets/` + `providers.toml` (never sync keys via the collection). `project`/
`location` are NOT secret → stay inline in `providers.toml`.

**Key files this session (uncommitted):** `core/config/secrets.py` (NEW),
`core/config/{repository,loader}.py`, `core/anki_compat.py` (pick_file/open_external_url/
play_audio→path/replay_audio_file), `core/providers/token_source.py` (service_account_project),
`core/providers/llm/factory.py`, `plugins/smart_notes/account.py` (default_models/key_cards),
`gui/smart_notes/{dialog.py,html.py}`, `gui/smart_notes/web/{page.html,page.css,01-bridge.js,
04-modal.js,05-handlers.js}`. Tests: `tests/core/test_secrets.py` (NEW),
`tests/core/test_config.py` (TestProviderConfigWrites, TestSecretsOutOfConfig),
`tests/plugins/test_smart_notes_account.py` (TestDefaultModels, TestKeyCards),
`tests/providers/test_token_source.py` (TestServiceAccountProject), `tests/providers/test_llm.py`
(vertex project-derivation). FEATURE_LOG.md has 2 new entries (newest two).
---

### Later 06-29 follow-ups (UNCOMMITTED — done after the 4 commits; suite now 450 passed)
- Smart Notes dialog polish (all in `gui/smart_notes/web/*` + `dialog.py`/`html.py`): merged
  ON/Lock + clearer wording ("On"→"Generate"), prettier blur, **Generate/Lock/Overwrite header
  click = toggle-all** (no ⇅ glyph), **Field-header sort** (↕), **decks = collapsible hierarchy
  tree** (cascade tick, default-collapsed subdecks, search), **Voice+Language columns** appear
  only when a sound row exists, kind `tts` shows as **"sound"**.
- **⚙ Options → tabbed dialog** (General / Account). General = 3 flags. Account = Text/Image/Sound
  subtabs with a usage table + OpenRouter credit line + a **Test playground** (free-text input).
- **Self-tracked usage** in `core/providers/usage.py` (JsonUsageRecorder → `user_files/usage.json`,
  RecordingLLM/TTS wrappers wired through ProviderHub) — now captures **REAL tokens** from each
  LLM response (`gemini.usageMetadata`, `openai.usage` → `provider.last_usage`); char-approx only
  for TTS. `account.py` = models_in_use + merge_usage. OpenRouter `/credits` real balance; other
  providers show an **honest "no credit/quota API" note** (`_credit_note`).
- TRUTH on provider quotas (told the user twice): only OpenRouter exposes credit to a key.
  OpenAI deprecated its key billing API; Gemini AI-Studio/Vertex keep quota in GCP Console
  (Vertex is pay-as-you-go, no prepaid-credit endpoint). Exact TOKEN usage IS universal (captured).

### 06-29 batch 2 — Account/Keys + polish (DONE; UNCOMMITTED; suite now 465 passed)
All 4 of the post-/compact requests are built + green (offline `465 passed, 19 skipped`):
1. **Default-model picker** per Account subtab — `#sn-acct-default` (rendered in `05-handlers.js`
   `renderDefaultPicker`): provider + model/voice selects seeded from `account_data.defaults`.
   Changing either posts `set_default_model {kind, provider, model}` → `dialog._on_set_default_model`
   → `repo.set_active_llm` / `set_active_tts` (providers.toml). Pure builder
   `account.default_models`. Drives detect-language / Auto-prompt / Improve / inherited fields.
   **Auto-smart → "Auto-prompt"** everywhere user-facing (op name `auto_smart` UNCHANGED).
2. **Centered table headers** — `page.css` `.sn-table thead th` text-align center; `.sn-th-field`
   centres the label and the sort button is `position:absolute; right:4px` (sticky th = its CB).
3. **General-tab tooltips** — each `.sn-opt-row` has a multi-line `title` (`&#10;`) = ON / OFF
   behaviour + an example.
4. **Keys subtab** (`🔑 Keys`, `#sn-keys`, rendered by `05-handlers.js renderKeys/keyCard/keyField`):
   one card per managed LLM provider (gemini / gemini_vertex / openrouter), masked secret inputs +
   **👁 eye reveal**, inline **Save** (`set_secret`), **Browse…** for Vertex JSON (`browse_file` →
   `anki_compat.pick_file` QFileDialog → persists path), console link (`open_url` →
   `anki_compat.open_external_url`). Quota: a **real % bar for OpenRouter only** (`account_keys_credit`
   builds an OpenRouter provider straight from config + `fetch_credit`, pushed via
   `window.__snKeysCreditResult`; red "top up" button at ≤0); an **honest note** for the rest
   (Vertex $300 credit lives in GCP Console — NOT fetchable from a key). No account gate; reveal local.
   Pure builder `account.key_cards`. New repo writes: `set_active_llm` / `set_active_tts` /
   `set_provider_secret` (nested, preserves other creds; skips voice for voiceless TTS providers).
   New tests: `TestDefaultModels` + `TestKeyCards` (test_smart_notes_account.py),
   `TestProviderConfigWrites` (test_config.py).

### 06-29 batch 3 — secrets out of config + Account/Keys fixes (DONE; UNCOMMITTED; 479 passed)
From a real-Anki test the user found 7 issues; all fixed:
1+2. **Secrets out of `providers.toml`** → new `core/config/secrets.py::SecretsStore`
   (`secret:<name>` = file content for keys/tokens; `secret-file:<name>` = path for the Vertex
   JSON). `ConfigRepository._resolve_secrets` resolves on every load; `set_provider_fields` /
   `set_provider_credential_file` write value→file+ref. Non-secret `project`/`location` stay
   inline (answered the user: project is a low-sensitivity id, location is non-sensitive). Ran a
   one-off migration (`scratchpad/migrate_secrets.py`) that moved the live gemini+openrouter keys
   and copied the Vertex JSON into `src/omnia/secrets/` and rewrote the TOML to refs. Verified
   round-trip (no values echoed).
3. **OpenRouter "Credit unavailable"** → re-fetch `account_keys_credit` after a key Save; handle
   `total=0` (was the bug: `&& res.total` fell through to the "unavailable" fallback).
4. **3 Save buttons on a Vertex card → one** per card (`set_secrets` batch op).
5. **Keys↔kind pane overlap** → `.sn-keys[hidden]{display:none}` (`display:flex` had overridden
   the UA `[hidden]`).
6. **Image playground/preview** now renders the picture (`_result_payload` returns a `data:` URI;
   JS + modal build an `<img>`).
7. **Per-subtab playground** (input + last result kept per kind in `pgState`; switching no longer
   bleeds Text→Image), a sound **"Play again"** button (`replay_audio` → `anki_compat.replay_audio_file`),
   and "Test playground" promoted to a spaced section header. Also fixed a latent bug: the sound
   playground sent `kind:"sound"` but the engine dispatches on `"tts"`.
New tests: `test_secrets.py`, `TestSecretsOutOfConfig` (test_config.py). Auth model clarified to
the user: **no Omnia account/email** — authorization is the per-provider API key / Google
service-account only; secrets are machine-local (AnkiWeb syncs collection, not add-on files).

### 06-29 batch 3b — naming + Vertex project derivation (DONE; UNCOMMITTED; 486 passed)
- **Account tab → "Usage & Keys"** (user-facing label only; `data-tab="account"` + op names kept).
- **Secret filename convention → dotted**: `_secret_name` now returns `<domain>.<provider>.<field>`
  (e.g. `llm.gemini.api_key`, `llm.gemini_vertex.credentials_path.json`) — kept the domain prefix
  so `llm.openai` vs `tts.openai` don't collide. Re-migrated the live secret files + refs
  (`scratchpad/rename_secret_convention.py`). Browse always stores under the convention name
  regardless of the source filename (confirmed).
- **Vertex `project` now optional**: `token_source.service_account_project()` reads `project_id`
  from the SA JSON; `factory._build_gemini_vertex` uses `config project or <JSON project_id>`
  (explicit still wins). Keys card marks Project ID optional with a "Read from the JSON if blank"
  placeholder. Verified the live JSON's project_id == the inline `<gcp-project-id>`, so the toml field
  can be cleared. New tests: `TestServiceAccountProject`, factory derivation cases.

### Next up / verify
- **Real-Anki visual check still PENDING** (no `aqt` here): verify 🔑 Usage & Keys (one Save, eye,
  Browse, OpenRouter bar refresh, blank Project ID still works), image shows in playground,
  per-subtab playground isolation + replay, centered headers, General tooltips. Then commit
  batches 2+3 (grouped).

### Done today (by area)

**A. plugins/smart_notes/ restructured into subpackages (OOP/SOLID).**
- `engine/` (PURE, no aqt/anki): `service.py` (GenerationService dispatches via a `{kind: Generator}`
  registry), `generators.py` (`Generator` ABC + Text/Image/TTSGenerator — Strategy/Open-Closed),
  `interpolation.py`, `rules.py` (compile + skip + `applies_to_deck`), `ordering.py` (was `dag.py`),
  `markdown.py`, `language.py` (`detect_language` + `LanguageDetector`).
- `authoring/` (PURE): `persona.py` (`FLASHCARD_EXPERT_SYSTEM` + `first_json_object`), `author.py`
  (`PromptAuthor`: `auto_smart`/`improve`/`improve_all`, replacing flat auto_smart+prompt_engineer),
  `models.py` (`AutoSmartField`).
- `integration/` (IMPURE Anki glue): `batch.py`, `editor.py`, `field_menu.py`, `review.py`
  (was review_evaluator), and NEW `store.py` (`SmartNotesStore`).
- `core/providers/catalog.py` (NEW): curated LLM text/image models (incl. Gemini 3.x) + TTS voices +
  languages; baked into the page as `window.__SN_CATALOG`.

**B. Smart Notes config dialog redesign** (`gui/smart_notes/`, webview split into 6 JS parts
01-bridge/02-catalog/03-render/04-modal/05-handlers/06-init):
- Separate **Generate** (toggle switch) + **Lock** columns (lock freezes+blurs the row, disables edit).
- **Prompt** edited in a popup (not inline); ✨ **Improve** per-field + global **Improve all**;
  ▶ **Preview** (uses first note of the type, seeds a sample when base empty, fabricates when no notes —
  fixed the "(empty result)" bug).
- **Kind-aware pickers**: text/image → Provider(LLM: gemini/gemini_vertex/openrouter) + Model; switching
  to **sound** (label for `tts`) reveals **Voice + Language** columns (Model n/a on sound, Voice/Language
  n/a otherwise — animated `.sn-na` fade). Language = Auto-detect or pick.
- **Decks picker** (toolbar button → popover: "All decks" master + per-deck checkboxes).
- ✨ Auto-smart now reports counts / "nothing to fill"; bolder (i) help icon in the generic config form.

**C. TTS language**: `SmartNotesFieldConfig/Rule.language`; generation order = pinned voice → explicit
language → auto-detect (best-effort, guarded). Authored prompts default to **English** unless the user
specifies a language.

**D. Rules persist in the COLLECTION DB (synced)**: `SmartNotesStore` ↔ `col.get_config/set_config`
(`omnia:smart_notes`). Provider config stays in TOML. Review reads fresh each card (settings_provider).

**E. Decks scope**: `SmartNotesNoteTypeConfig.decks` ([]=all); `engine.applies_to_deck` +
`anki_compat.note_deck_ids`; batch skips out-of-scope notes (counted skipped), review skips out-of-scope cards.

**F. Config layer → direct-edit domain files** (`core/config/`): no override layer. `config/` holds LIVE
`omnia.toml`/`features.toml`/`providers.toml` (gitignored) + tracked `*.example.toml` templates copied on
first run (`ConfigLoader.ensure_live_files`); writes route via `ConfigRepository._file_for`. Credential
files now in top-level `src/omnia/secrets/` (gitignored except README). `OMNIA_TEST_CONFIG` = a config DIR now.

### Commits (on `main`)
- `3335e16` refactor(config): direct-edit domain config files (no override layer) + external secrets/
- `d157de4` refactor(smart_notes): engine/ + authoring/ subpackages + provider catalog + voice/language model
- `e608282` feat(smart_notes): collection-DB rules (synced) + per-note-type deck scope
- `9c57e26` feat(smart_notes): redesign config dialog — On/Lock, prompt popup, kind-aware pickers, decks, Improve/Preview

### Decisions made
- **On + Lock are SEPARATE columns** (user choice) — map to existing `enabled` + `prompt_locked`, no model migration.
- **Rules → collection DB** (synced); provider config stays TOML.
- **Config = direct-edit domain files**, no override layer; secrets external in `src/omnia/secrets/`.
  Caveat: `config/` is overwritten on a packaged-add-on UPDATE (only `user_files/` survives) — fine for the
  dev symlink workflow.
- Internal kind stays `"tts"`; UI label is `"sound"` (no churn to model/engine/tests).
- `coder` subagents did the config refactor + the collection-DB/decks build to a precise spec; reviewed here.

### Next up / verify
- **Real-Anki visual check is PENDING** — this dev env has no `aqt`, so the GUI launch couldn't be run here.
  On the user's machine: restart Anki → Tools → Omnia → Configure (Smart Notes); test sound→Voice/Language
  columns, Decks picker, Prompt popup ✨Improve/▶Preview, Save → restart → rules persist (now in collection).
  Headless: `QT_QPA_PLATFORM=offscreen "<anki-python>" tests/smoke/run_smoke.py`. Launcher:
  `scratchpad/anki_launch_smartnotes.py` (opens the dialog directly).
- Gotchas: logging must stay file-only (stderr → Anki crash dialog); nested AnkiWebView opened inside a
  pycmd callback must be DEFERRED (`QTimer.singleShot(0,…)`) or it paints blank; never commit creds
  (live `config/*.toml` + `secrets/` are gitignored; only `*.example.toml` is tracked).
- Possible follow-ups if the user requests: a "viettts" TTS provider (only existing providers surfaced now);
  packaged-add-on config-update safety (config/ vs user_files/).

## 2026-06-28 (Sunday)

### Done today
- Bootstrapped **Omnia**, an all-in-one pluginized Anki add-on, from a scaffold that had
  been copied from the `a prior project` Flask/Celery/Supabase server.
- Mapped the three reference add-ons (`smart_notes`, `typed_accuracy`,
  `automatically_flip_cards`), the `a prior project` LLM/TTS provider layer, and the
  the karpathy agent guidelines.
- Reconfigured the four subagents to run on Opus 4.8 and dropped Codex (`coder` writes
  directly); re-enabled the subagent workflow; sharpened `reviewer` into a
  solution-architecture role.
- Rewrote `.claude/CLAUDE.md` and `.claude/CONVENTIONS.md` for the Anki add-on reality
  (no server; plugin model; shared seams; vendoring; logic/glue separation) and added the
  karpathy "Agent Working Principles" as CONVENTIONS Part 3.
- Seeded the foundational ADRs (see DECISIONS.md).
- Deleted a prior project server cruft (Flask/Celery/Supabase scripts, server docker-compose,
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
- Adapted a prior project's HTTP **retry/backoff** into `UrllibHttpClient` (injectable `RetryPolicy`).
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
  creds from the prior project's Vertex credentials into gitignored `user_files/omnia.toml` and **ran it live**
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
- **Meaningful real-provider tests** (per user demand): every real test now VERIFIES the output
  satisfies the prompt, not just non-emptiness — LLM battery (factual / arithmetic / yes-no /
  exact item-count / conditional / JSON / Vietnamese translation, partial-match), TTS asserts
  VALID audio (magic bytes per `audio_ext` + real size), smart_notes asserts generated content
  matches the rule. Wiring tests are **config-driven** (built from the real `[llm.<p>]`/`[tts.<p>]`
  model + credentials with a FakeHttpClient injected — offline, no fabricated `g`/`k`/`secret`).
  Provider `requires_api` classification drives per-provider markers. Calibrated live: **30 real
  pass / 11 xfail (gemini free-tier quota) / 0 fail**; offline **151 pass**.
- **Git history**: re-split the over-stuffed initial commit into **9 focused per-feature-group
  commits** (scaffold, plugin system, reviewer seams, config, providers, features, gui, vendor,
  tests); verified the re-split tree is byte-identical (no content change). No secrets committed
  (`user_files/` gitignored).
- **Bespoke per-feature UIs** built + committed (3 coder subagents, disjoint feature folders): a
  plugin seam (`PluginContext.config`/`reload_self` + `FeaturePlugin.custom_config_dialog`);
  smart_notes editor ✨ button + `SmartNotesDialog`; typed_accuracy deck-overview stats donut +
  `StatsStore`; auto_flip reviewer countdown + per-deck options dialog. Offline **184 pass**,
  Anki import OK, `.ankiaddon` builds (309 files). Live click-through still needs a real Anki run.
- **Git hygiene** (user request): `.claude/` and other agent folders (`.codex`, `.codx`, `.cursor`,
  `.aider*`) are now **gitignored + untracked** — these working docs are LOCAL only. History after
  the re-split: 9 feature-group commits → plugin seam → git-hygiene → 3 UI commits.

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
- **Launch the real Anki GUI** and click through Tools→Omnia + each feature UI live — the only
  remaining verification that can't be done headless: smart_notes editor ✨ button + field-mapping
  dialog, typed_accuracy stats donut on the deck overview, auto_flip reviewer countdown, auto_flip
  deck gear-menu options. Iterate on styling vs the reference add-ons.
- `mypy src/omnia` once mypy is added to the dev venv (not a commit gate yet).
