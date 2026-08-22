# Architecture Decision Records (ADRs)

This file records significant architectural and design decisions. Each ADR captures the context, the decision, and its consequences so future contributors understand the **why**, not just the **what**.

Add new ADRs at the **bottom** with the next sequential number.

## Format

```
## ADR-NNN: <short decision title>

**Date**: YYYY-MM-DD
**Status**: Proposed | Accepted | Deprecated | Superseded by ADR-XXX

### Context
<What is the issue we're seeing? What forces are at play?>

### Decision
<What did we decide to do?>

### Rationale
<Why did we decide this? What alternatives were considered?>

### Consequences
<Positive and negative outcomes. What becomes easier? What becomes harder?>

### Alternatives considered
<List of options that were evaluated and rejected, with reasons.>
```

Use `/adr` slash command to have Claude help draft a new ADR.

---

## ADR-001: Omnia is a client-side Anki add-on, not a server

**Date**: 2026-06-28
**Status**: Accepted

### Context
The repository was scaffolded from `a prior project`, a Flask/Celery/Redis/Supabase backend. Omnia
is an Anki add-on: it runs inside the user's Anki (bundled Python + PyQt6) on macOS and
Windows, distributed as a folder/`.ankiaddon` with vendored dependencies.

### Decision
Strip all server infrastructure (Flask, Celery, Redis, Supabase, Manim, Docling, the
multi-service docker-compose stack, the "run inside the a prior project-dev container" rule). The
add-on does background work with `QueryOp`/`mw.taskman`/`mw.progress.timer`. Persistence is
Anki's per-add-on JSON config and the user's collection. Docker remains only as an optional
CI/test image.

### Rationale
None of the server components can run inside Anki, and the add-on must not depend on an
external service to function. Keeping them would be dead weight and actively misleading to
future contributors.

### Consequences
- (+) The codebase matches the runtime; no false affordances.
- (+) Cross-platform by virtue of pure-Python + PyQt6.
- (−) No server-side compute; heavy AI calls go straight to provider REST APIs from the
  client, off the Qt main thread.

### Alternatives considered
- **Keep a thin backend** (like smart_notes' Railway server): rejected — adds an operational
  dependency and a hosting cost for what can be done client-side with the user's own keys.

---

## ADR-002: Pluginize via a FeaturePlugin registry + a PluginManager lifecycle

**Date**: 2026-06-28
**Status**: Accepted

### Context
Omnia must host many independent features, each individually enable/disable-able from one
settings UI, and adding a feature later must be cheap and uniform ("pluginize").

### Decision
Each feature is a `FeaturePlugin` subclass registered with `@register("<id>")`. A single
`PluginManager`, built once at startup, reads the `enabled` map from config and drives
`on_enable(ctx)`/`on_disable(ctx)`. Each plugin gets a `PluginContext` exposing the config
store, logger, and the shared reviewer seams. Plugins must fully tear down on disable.

### Rationale
A registry + base class + lifecycle manager is the smallest structure that makes features
uniform, discoverable by the GUI, runtime-togglable, and independent. It mirrors the
`@register` pattern the team already knows.

### Consequences
- (+) New feature = subclass + register + import; the GUI lists it automatically.
- (+) Features are isolated; one can't silently break another.
- (−) Plugins carry the discipline of clean teardown; reviewed explicitly.

### Alternatives considered
- **Separate add-ons per feature**: rejected — defeats the "all-in-one" goal and duplicates
  the shared seams in every add-on.
- **Feature flags inside one monolithic module**: rejected — low cohesion, high coupling,
  no clean runtime toggle.

---

## ADR-003: One reviewer ease pipeline; features register ordered transformers

**Date**: 2026-06-28
**Status**: Accepted

### Context
Multiple features change the ease a card is graded at (typed-accuracy maps typing accuracy
to again/hard/good/easy; overdue-guard forces overdue cards to hard/again). The reference
add-on monkeypatched `Reviewer._answerCard`. If two features each wrap that method
independently, ordering is undefined and they corrupt each other.

### Decision
Wrap `Reviewer._answerCard` **exactly once** in `core/reviewer/ease_pipeline.py`. Features
register an ordered ease transformer `(card, ease) -> ease`. The pipeline folds the
requested ease through the enabled transformers in priority order. Reviewer JS/CSS and the
`pycmd` bridge are likewise centralized in `core/reviewer/web_injector.py`.

### Rationale
The reviewer is the single most contended Anki seam. Centralizing the patch removes the
ordering/conflict hazard, lets features compose, and keeps each feature's logic pure and
testable (a transformer is a pure function).

### Consequences
- (+) typed-accuracy and overdue-guard cooperate deterministically.
- (+) Pure transformers/rules are unit-testable without Anki.
- (−) The pipeline owns priority ordering; new ease features must pick a sensible priority.

### Alternatives considered
- **Each feature patches `_answerCard` itself**: rejected — the exact conflict this avoids.

---

## ADR-004: LLM/TTS provider abstraction adapted from a prior project, vendored & pure-Python

**Date**: 2026-06-28
**Status**: Accepted

### Context
smart-notes generates text/images (LLM) and voice (TTS). The user has no OpenAI/Google keys
but has the provider configurations used in `a prior project`. a prior project's provider design
(`LLMProvider`/`BaseTTSProvider` + factory + concrete providers) is clean, but its code
depends on `pydantic-settings` and a server settings system, and the SDKs (`google-genai`,
`openai`) are heavy and may carry binary deps unsuitable for a vendored cross-platform
add-on.

### Decision
Adapt a prior project's **interface and provider-selection design** rather than import its code.
Define `LLMProvider`/`TTSProvider` base classes + factories in `core/providers/`. Implement
providers with lightweight HTTP (stdlib / a small vendored client) against the providers'
REST APIs, plus key-free TTS options (e.g. edge-tts/gTTS-style) so the add-on works without
paid keys. Provider keys/config live in the config store, never in code or env at import.

### Rationale
This gives the same "add a provider = one subclass" extensibility the user wants, keeps the
add-on light and cross-platform (no heavy/binary SDK vendoring), and lets smart-notes work
with free providers out of the box while still supporting the a prior project-style configured ones.

### Consequences
- (+) Easy to add providers; features depend only on the interface.
- (+) No paid key required to try the feature (free TTS path).
- (−) We maintain thin HTTP clients instead of leaning on vendor SDKs; API drift is on us.

### Alternatives considered
- **Vendor the full `google-genai`/`openai` SDKs**: rejected for size and binary-wheel /
  transitive-dep risk inside Anki's Python.
- **Import a prior project directly**: impossible at the user's machine — a prior project isn't installed
  inside Anki and pulls in `pydantic-settings`/server deps.

---

## ADR-005: Native-runtime providers run in an add-on-managed sidecar venv

**Date**: 2026-06-30
**Status**: Accepted

### Context
Some open-source providers need a **native** runtime: `piper` needs `onnxruntime` (compiled
C++), `viet-tts` needs PyTorch. These cannot be vendored — compiled wheels are specific to each
OS / CPU arch / Python ABI and are huge, which violates ADR-004's pure-Python, cross-platform
vendoring rule. They also must not be `pip install`-ed into Anki's **frozen bundled
interpreter**: it often has no pip/venv, the install is fragile and can break Anki, and a native
wheel only loads if it matches that interpreter's exact ABI. `vendor/` therefore works for
pure-Python deps only, and the default providers (edge_tts, google_translate, cloud) already run
zero-install via the stdlib HTTP/WebSocket transports — but there is no clean way to offer the
offline, self-hosted native providers.

### Decision
Run native-runtime providers as **out-of-process sidecars in an add-on-managed, per-provider
virtualenv**, behind a new `NativeRuntimeManager` seam. The manager: detects a host Python
(system `python3`/`py`, else an optionally downloaded python-build-standalone), creates and
caches a venv under `user_files/native_envs/<provider>/`, `pip install`s the provider's declared
deps into it, launches the provider's local server process, then health-checks, reuses, and
cleans it up. Each native provider declares `{pip_packages, server_cmd, port/transport}`; Anki
talks to the sidecar over a localhost socket/HTTP using the **existing** provider transports.
This is an **opt-in advanced** path (first run downloads + installs, run off the Qt thread with
progress); the defaults stay zero-install. It generalizes the existing viet-tts subprocess+HTTP
pattern.

### Rationale
The venv's own interpreter owns the native ABI, so the wheels match by construction — decoupling
native code from Anki's interpreter entirely. Process isolation means a bad install or a runtime
crash stays contained in the venv/sidecar and never touches Anki's Python or the app. It is
cross-platform (venv + pip + a local socket are portable), and it keeps the shipped add-on light
(no native wheels bundled; `vendor/` stays pure-Python per ADR-004).

### Consequences
- (+) piper / viet-tts become usable offline without polluting or risking Anki's interpreter;
  install/crash failures are isolated to the sidecar.
- (+) No ABI coupling to Anki's interpreter; genuinely cross-platform.
- (+) `vendor/` and the default zero-install providers stay light and pure.
- (−) Needs a host Python to bootstrap the venv — users with only the packaged Anki may have to
  install Python 3.x (or we ship python-build-standalone, which is heavy). This is the main UX gate.
- (−) First-run setup is slow and network-dependent (viet-tts/torch ~GB); requires progress UI +
  robust handling of no-python / no-network / install-failure / port-conflict.
- (−) More moving parts: venv + process lifecycle, port management, cleanup on Anki exit.

### Alternatives considered
- **Vendor native wheels into `vendor/`**: rejected — per-OS/arch/ABI and huge; breaks ADR-004.
- **`pip install` into Anki's bundled interpreter**: rejected — frozen / no pip, fragile, can
  break Anki, and ABI-locked to that interpreter.
- **Ship per-platform native binaries in the add-on**: rejected — heavy and a cross-OS/arch
  maintenance burden.
- **Make Anki itself run in a venv**: rejected — packaged Anki uses an embedded interpreter you
  cannot redirect, and it would not solve the ABI problem anyway.
- **Remote-only (no local native runtime)**: that is the zero-install default; it gives no
  offline/self-hosted option for these providers, so the sidecar venv is the opt-in complement.

---

*(Add new ADRs below this line)*

## ADR-006: DB-backed persistence via swappable storage backends (Hybrid)

**Date**: 2026-07-07
**Status**: Accepted — refined by ADR-008 (usage now defaults to synced `col` config, not a `col.db`
table; the sync mechanism is per-concern closures — `write_file(read_file())` for config,
`save(load())` for usage/voices — not the `write_all`/`read_all` this ADR's body sketches)

### Context
Omnia persisted almost everything to files under the add-on folder: the config lived in three
live TOML files (`omnia.toml` = log level + plugin enable flags, `features.toml` = per-feature
settings, `providers.toml` = LLM/TTS + secret refs), provider usage stats in `user_files/usage.json`,
and the fetched-voice cache in `user_files/voices.json`. Two concerns already used the collection
DB (`smart_notes` rules via `col.set_config`; `typed_accuracy` via a `typed_answer_log` SQLite
table). The user wants the remaining file-based state moved into "the database" so settings follow
the user and the add-on folder stops accumulating JSON/TOML — **except** `providers.toml` + the
`.secrets/` store, which must stay files (secrets must never enter a synced collection).

Two forces complicate a naive "write to the DB" change:
1. **Provider credentials** must not sync — `providers.toml`/`.secrets` are deliberately local.
2. **Usage is written from background generation threads** — the reason it uses a self-owned
   file + lock today. `col.set_config` / `mw.addonManager.writeConfig` are not safe to call
   per-record off the Qt main thread.

### Decision
Introduce a **storage-backend abstraction per persistence concern** (extend, don't replace): keep
each existing base interface + its file implementation, and add a DB/collection implementation
selected at bootstrap. Each concern's two backends are **100% independent implementations of one
ABC — neither composes or reads the other's storage**. The default routing is **Hybrid**:
- **Settings + enable flags** (`omnia.toml` = log level + `[plugins]`; `features.toml` = per-feature
  sections) → `col.get_config`/`set_config` (synced across devices). `ConfigRepository` is
  unchanged — it still talks to the `read_file`/`write_file`/`load`/`load_merged` contract, now an
  ABC (`BaseConfigLoader`). `TomlConfigLoader` (alias `ConfigLoader`) is the all-files backend;
  `CollectionConfigLoader` is a **separate** class that keeps `omnia`/`features` **only** in `col`
  (never touches those two files) and reads/writes **`providers.toml` from disk directly** (its own
  minimal TOML I/O via shared stateless helpers — it does NOT wrap `TomlConfigLoader`). Because the
  settings live in `col`, a new device gets them via collection **sync** — no config files needed.
- **Voice cache** → `col.set_config` (`omnia:voices`, synced) via `CollectionVoiceCache`.
  **Usage** → a dedicated `col.db` **aggregate table** (`omnia_usage`, one row per
  `kind|provider|model`) via `ColUsageStore`, mirroring the `typed_accuracy` `col.db` table
  pattern. So **everything now lives in Anki's collection DB** — the earlier `mw.addonManager` /
  `meta.json` (`AddonConfigStore`) layer is **DROPPED**; only `providers.toml` + `.secrets/` stay
  as files. `JsonUsageStore` / `JsonVoiceCache` remain the selectable file backends.
  *(Chosen over the `meta.json` Hybrid: `meta.json` is still a file, whereas `col` is the real DB
  and keeps everything consistent. The voice cache now syncs across devices (a harmless
  re-fetchable cache stored via `col.set_config`); the `omnia_usage` table is device-local and does
  NOT sync — AnkiWeb replicates only the collection tables it knows about, so a custom add-on table
  behaves like the `typed_accuracy` `col.db` table: per-device approximate stats. See the
  2026-07-13 amendment below.)*
- **Usage** uses a `BufferedUsageRecorder`: it folds each call into a thread-safe in-memory
  aggregate and **flushes on the Qt main thread** (coalesced `run_on_main` after each record + a
  synchronous flush on profile close), so background generation threads never touch `col`.
  Per the review, usage collapses to **one `UsageStore` seam** (`JsonUsageStore` / `ColUsageStore`)
  with `JsonUsageRecorder` delegating its I/O to `JsonUsageStore` (no duplicated file I/O), a shared
  `_fold_call` row-aggregation helper, and `snapshot()` lifted onto the `UsageRecorder` ABC
  (`NullUsageRecorder` returns `[]`).
- **No migration / no convert.** The DB backends are created **fresh from defaults** — legacy files
  are never read or converted into the DB (the file world and the DB world are separate). The
  existing dev live files (`omnia.toml`, `features.toml`, `usage.json`, `voices.json`, plus the
  orphaned `typed_accuracy_stats.json`) were **deleted** as part of the cutover; the shipped code
  simply ignores any legacy file it finds. `providers.toml` + `.secrets/` are untouched.
  `typed_accuracy`/`smart_notes` are already DB-backed.

- **Dispatch is env-driven** via `envs.py` — one knob per concern, precise `*_STORAGE` names:
  `OMNIA_CONFIG_STORAGE` (`"database"` | `"toml"`), `OMNIA_USAGE_STORAGE` (`"database"` | `"json"`),
  `OMNIA_VOICE_CACHE_STORAGE` (`"database"` | `"json"`); default `"database"`. The file backends
  (`TomlConfigLoader`, `JsonUsageStore`, `JsonVoiceCache`) stay first-class and selectable.
- **Sync on dispatch change** (distinct from the legacy cutover above): a `PersistenceDispatcher`
  records the **last-used** storage per concern in a marker kept in a small local
  `user_files/.storage.json` (device-local dispatch bookkeeping — `col` config is *synced*, so it
  cannot hold a per-device marker; `user_files/` is local + preserved across add-on updates). At
  startup it compares the marker
  to the current `envs` value per concern: missing marker → first run, no sync; equal → no-op;
  **changed → copy ALL data from the last-used backend to the newly-selected one, then update the
  marker.** Because both backends implement the same ABC, the copy is generic
  (`new.write_all(old.read_all())`); switching back and forth never loses data because the sync
  always reads from the last-used store (which holds the latest edits). `providers.toml` is a file
  in both config backends, so a config sync moves only `omnia`/`features`. This is NOT a convert of
  the legacy files (those are gone) — it is a same-shape copy between two backends of the new system.

### Rationale
The `read_file`/`write_file` filename-keyed contract the `ConfigRepository` already used is a
natural polymorphism seam — making it an ABC and adding a collection backend keeps the repository
and every caller unchanged. Keeping the two backends fully independent (rather than the DB backend
delegating to the file backend) means the DB path has **no hidden dependency on the settings files**
— the exact failure mode of a device that syncs `col` but has no `omnia.toml`/`features.toml`.
Mirrors the proven `smart_notes` `col.set_config` pattern and the `UsageRecorder`/`JsonUsageRecorder`
ABC already in the tree, so it's the established OOP shape extended, not a rewrite. Buffering usage
is the correctness-critical piece: it preserves the "never touch shared state from a bg thread"
invariant while still moving usage off its own file. Skipping data conversion keeps the two worlds
cleanly separate; for a dev cutover a fresh default store is preferable to a fiddly one-shot import.

### Consequences
- (+) Plugin settings + enable flags sync across devices (like the smart_notes rules already do);
  a fresh device works from `col` sync alone, with no settings files present.
- (+) The add-on folder stops accumulating `usage.json`/`voices.json`/`omnia.toml`/`features.toml`;
  only `providers.toml` + `.secrets/` remain, which is correct.
- (+) Backends are swappable and independent — the file path stays supported and testable headless
  (tests keep constructing `ConfigLoader(tmp_path)` = the Toml backend; fakes inject a dict-backed
  col/config). No cross-backend coupling to reason about.
- (−) **No carry-over**: switching to the DB backend starts settings from defaults (plugins must be
  re-enabled / features re-configured once). Accepted for a dev cutover; the legacy files were
  deleted rather than kept as a backup, so there is no recovery of the old values from disk.
- (−) Usage durability weakens slightly: a hard crash in the sub-second window before a flush can
  lose the last few uncounted calls (negligible for approximate stats).
- (−) More types/indirection (a base + 2 impls per concern). A profile with no collection loaded has
  no settings source for the DB backend (returns defaults; in practice bootstrap runs on
  `profile_did_open`, so `col` is always available then).

### Alternatives considered
- **DB backend delegates to / falls back to the file backend** (the first draft): rejected — it made
  the DB path silently depend on `omnia.toml`/`features.toml`, which breaks on a synced-but-fileless
  new device. The two backends are kept 100% independent instead.
- **One-time convert (import legacy files into the DB, rename `*.migrated`/`.bak`)**: rejected — the
  user wants the DB store created fresh, not converted; the two worlds stay separate and the old
  files were deleted outright.
- **Everything into `col` (including usage/voices)**: rejected — usage's background writes are
  unsafe against `col`, and device-local caches don't belong in the synced collection.
- **Everything into `mw.addonManager` config**: rejected — settings then wouldn't sync, losing a
  key benefit; and it's still a single local file, no better than the TOML for the sync goal.
- **A new SQLite table per concern** (like `typed_accuracy`): rejected as overkill for small
  key/value blobs; `col.set_config` / addon config already give durable, synced/local JSON stores.
- **Keep usage.json as a file exception** (like `providers.toml`): considered and offered; the
  user chose to move it to the DB with buffered main-thread flushing.

### Amendment (2026-07-13) — correct the cross-device sync claim
The original Decision/Consequences implied that **both** the voice cache and usage "sync across
devices." That is only half true, and this amendment corrects the record (the chosen design is
unchanged — usage still lives in `col.db` for durability and to stop accumulating files):
- **Voice cache — DOES sync.** Stored via `col.set_config` (`omnia:voices`), which AnkiWeb
  replicates as part of the collection config.
- **Usage — does NOT sync.** `ColUsageStore` writes a **custom** `col.db` table (`omnia_usage`).
  AnkiWeb syncs only the collection tables it recognizes; a custom add-on table is **not**
  replicated, and a full-download sync (server → client) can **drop** it. So usage is
  **device-local**, exactly like the `typed_accuracy` `col.db` table — per-device approximate
  stats, not a cross-device aggregate. The earlier "aggregated usage across devices" expectation
  does not hold.

---

## ADR-007: System-wide desktop clipper as a standalone PyQt6 tray app (AnkiConnect client)

**Date**: 2026-07-14
**Status**: Accepted

### Context
The `3rdparty/omnia-web-clipper` browser extension lets a user double-click/select text on a web
page, see a floating "+", and add a note (word + surrounding context) to Anki — which the Omnia
add-on's integration gateway then auto-generates. The user wants the SAME capability **outside the
browser**: on macOS/Windows/Ubuntu, in ANY app that shows text (PDF viewers, Word, editors, …),
select or double-click text → a "+" → add to Anki with the word + its context phrase. A browser
extension cannot reach other apps; this needs a native, cross-platform desktop helper.

### Decision
Build a **standalone desktop app** at `3rdparty/omnia-desktop-clipper/` (a sibling of the web
clipper — it is likewise a *client* of Omnia-Anki, not part of the add-on package). Stack:
**Python + PyQt6** (reuses the project's language/GUI toolkit; overlay + tray are straightforward).
It is a **front-end only** — the Anki side is reused unchanged: the app POSTs to **AnkiConnect**
(`addNote`) with the exact same payload + tags the web clipper uses (`tags: ["omnia-web-clipper",
"omnia-autogen"]`), so the add-on's `IntegrationGateway` recognizes it and auto-generates. No
Anki-side change.

Two capture concerns, each behind a small seam so backends swap per-OS and per-phase:
- **Selection capture** — `SelectionCapture` ABC. **v0**: `ClipboardCapture` (save clipboard →
  synth Cmd/Ctrl+C → read → restore) — works in almost any app on all three OSes. **v1**:
  accessibility backends (macOS `AXSelectedText`, Windows UIA `TextPattern`, Linux X11 PRIMARY)
  that read the selection + its enclosing sentence WITHOUT touching the clipboard.
- **Trigger** — **v0**: a global **hotkey** (via `pynput`) → capture → a small popup near the
  cursor (word + editable context) → add. **v1**: a global **mouse hook** (double-click / drag
  mouse-up) → a floating **"+"** overlay (always-on-top borderless PyQt6 window) → add.

Ship **all three OSes** from the start; deliver **phased** (v0 hotkey MVP first, then v1 floating-+).

### Rationale
Reusing AnkiConnect + the existing gateway means the hard part (generation, note-type mapping,
autogen) is already built and shared with the web clipper — the desktop app only adds a new
capture front-end. PyQt6 keeps it in the project's stack. The hotkey+clipboard MVP is the only
approach that works *everywhere* with no special permissions or per-app fragility, so it de-risks
"any app on any OS" immediately; the floating-"+" (mouse hook + accessibility) is a strictly
additive nicety layered on the same core. Seams (`SelectionCapture`, trigger, `AnkiConnectClient`)
let each OS/phase plug in without rewrites.

### Consequences
- (+) Same add-to-Anki + auto-generation UX as the web clipper, now in PDFs/Word/editors/etc.
- (+) Zero Anki-side work — the gateway already handles the tagged notes; the two clippers converge.
- (+) v0 works on all three OSes on day one (clipboard copy is near-universal).
- (−) **Wayland (newer Ubuntu)** blocks global input hooks + reading another app's selection, so
  the floating-"+" (v1) is largely unavailable there; X11 works, and the hotkey MVP degrades but is
  limited. macOS needs Accessibility + Input Monitoring permissions; some sandboxed/Java apps expose
  neither copy nor accessibility; scanned-image PDFs have no text layer (would need OCR — future).
- (−) A second app to package/sign/distribute per-OS (tray app), separate from the add-on.
- (−) v0 clipboard capture briefly clobbers the clipboard (saved + restored) and gets only the
  selection; rich auto-context waits for the v1 accessibility backends.

### Alternatives considered
- **Browser extension only**: rejected — cannot reach non-browser apps (the whole point).
- **Tauri (Rust) / Electron**: viable and lighter (Tauri) or easier-libbed (Electron), but Tauri is
  a new language with no Python reuse, and Electron bundles Chromium (heavy); PyQt6 reuses the stack.
- **Accessibility-first (no clipboard) for v0**: rejected as the MVP — it's app-dependent and
  permission-gated; clipboard copy is the robust "works everywhere" baseline, with accessibility as
  the v1 upgrade for clean, auto-context capture.
- **A new backend endpoint instead of AnkiConnect**: rejected — AnkiConnect + the existing gateway
  already do exactly this; reusing them keeps the two clippers on one contract.

---

## ADR-008: Usage aggregate syncs by default (collection config, not a col.db table)

**Date**: 2026-07-15
**Status**: Accepted (refines ADR-006)

### Context
ADR-006 stored the self-tracked LLM/TTS **usage aggregate** in a dedicated `col.db` table
(`omnia_usage`), matching the `typed_accuracy` pattern. But a custom `col.db` table is **not** synced
by AnkiWeb (only Anki's own tables sync), so usage was device-local — a user's counts did not
aggregate across their machines. The user wants usage to follow them across devices (and to be
readable from a future web view), while config and voices already sync via `col.set_config`.

### Decision
Store the usage aggregate in the **synced collection config** (`col.set_config` under `omnia:usage`)
by default, via a new `CollectionUsageStore` that mirrors `CollectionVoiceCache`. The aggregate is a
small bounded `{kind|provider|model: row}` map, so it is a fine fit for `col` config and rides along
with AnkiWeb sync. `OMNIA_USAGE_STORAGE` still selects `json` (a device-local file) for anyone who
wants it. The `col.db`-table `ColUsageStore` is removed. The `BufferedUsageRecorder` is retained —
`col.set_config` is also Qt-main-thread-only, so background generation threads still buffer in memory
and flush on the main thread.

Also (correcting ADR-006's sketch): the backend-switch sync is a **per-concern closure**, not a
generic `write_all`/`read_all`. Config copies `write_file(name, old.read_file(name))` per domain
(`omnia`/`features`, never `providers.toml`); usage/voices copy `new.save(old.load())`. `UsageStore`/
`VoiceCache` `save()` now **return a bool** (did it persist?), and the dispatcher advances the
`.storage.json` marker **only when the copy actually persisted** — a col-less boot leaves the marker
so the copy retries next boot instead of orphaning the old backend's data.

### Consequences
- **Positive**: usage aggregates across a user's devices; usage/voices/config all live in `col` config
  by default (one consistent, synced home); the marker can no longer advance past a no-op copy.
- **Negative**: existing `col.db` `omnia_usage` rows from the prior build are not migrated (usage is
  non-critical accounting, and the project follows "migrate = start fresh, not convert"); usage now
  contributes to collection-config size (bounded + tiny, so negligible).

### Alternatives considered
- **Keep the col.db table**: rejected — it never syncs, defeating the cross-device goal.
- **Add a third "coldb" knob value**: rejected as needless surface; `database` (synced) + `json`
  (device-local file) already cover both intents.

## ADR-009: Word lookup is served to the clipper over a read-only loopback endpoint

**Date**: 2026-08-07
**Status**: Accepted

### Context
The companion desktop clipper (ADR-007) floats a "+" over whatever app the user is reading. We
added a second action — look the selected word up in the collection and show the card Anki
already has. That needs three things the clipper cannot know on its own:

* **which note types are searchable** (the user's choice, stored with the collection),
* **which of a note type's fields are worth showing** — the user's `AnkiVocabulary` has ~35
  fields, mostly empty on a given note, with values full of raw HTML, `[sound:…]`, `<img>` and
  cloze markup,
* **how hits are ranked** (exact title beats prefix beats substring).

AnkiConnect can return raw notes/cards, so the clipper *could* do all of this itself. But then
every client re-implements the triage, and improving it means rebuilding and reinstalling the
clipper app. CLAUDE.md is also explicit that omnia is **not a server** — so adding any listening
socket to the add-on is a pattern change that needs recording.

A further hard constraint: Anki's collection is **main-thread-only**, while any socket handler
runs on a worker thread.

### Decision
The omnia add-on gains a `word_lookup` **feature plugin** that owns the logic and exposes
**one read-only endpoint**, `GET /lookup?word=…`, on **127.0.0.1 only**, from a stdlib
`ThreadingHTTPServer` on a daemon thread started/stopped by the plugin's enable/disable.
The clipper is a thin renderer: it calls the endpoint and draws the display-ready JSON.

Guard rails baked into the implementation:

* `LookupService.start()` **refuses to bind a non-loopback host** — the collection is never
  exposed to a network.
* There is **no write path**: one GET, no mutation, no eval.
* Every request marshals its collection read onto the Qt main thread via
  `anki_compat.run_on_main` and **waits with a timeout**, answering `503` if Anki is busy rather
  than hanging the clipper.
* A bind failure (port taken) is logged and reported, never raised — the feature degrades to
  "unavailable" instead of breaking plugin activation.

### Rationale
The split follows the user's explicit direction ("logic in omnia, UI in the clipper") and the
repo's own coupling rule: decisions about the collection belong with the collection. Because the
payload is display-ready, a client never has to understand Anki's field HTML or scheduler
internals, and the triage can improve without touching the clipper at all.

A loopback HTTP endpoint is the same mechanism AnkiConnect itself uses, so it is familiar,
debuggable with `curl`, and language-agnostic for any future client. "Not a server" in CLAUDE.md
targets web frameworks and background job queues; a ~150-line stdlib listener bound to loopback
for one read-only query is a different thing in kind — but it is a genuine pattern change, hence
this ADR.

### Consequences
**Positive**
* One place owns searchability, triage and ranking; the clipper stays a renderer.
* Any future client (another app, a script, a second clipper) gets the same answer for free.
* The pure logic (`logic.py`) unit-tests headless; the service tests over real sockets without
  Anki (45 tests total).

**Negative**
* The add-on now listens on a port while the plugin is enabled — a new surface, mitigated by
  loopback-only binding, no write path, and being off when the plugin is off.
* A port can collide; the port is therefore configurable and a failure is surfaced, not fatal.
* Lookups are unavailable while Anki is closed. This is inherent — the collection lives in Anki
  — and the clipper says so instead of failing silently.

### Alternatives considered
* **Clipper queries AnkiConnect directly.** Rejected: it duplicates the triage/ranking in every
  client, and the clipper still could not read the user's searchable-note-type choice, which is
  stored in the collection config.
* **Share config through a file** the add-on writes and the clipper reads. Rejected: it only
  moves the *config*, leaving the logic duplicated, and deriving the add-on's `user_files` path
  from the clipper is fragile across profiles and platforms.
* **Piggyback on AnkiConnect** by registering a custom action. Rejected: it makes omnia depend
  on another add-on's internals, which change without notice.
* **Poll a request/response file pair.** Rejected: laggy and racy for an interactive gesture.

## ADR-010: Persisted config models tolerate unknown keys (`PersistedModel`), payload models stay strict

**Date**: 2026-08-14
**Status**: Accepted

### Context
Every Omnia setting now lives in the **synced collection config** (ADR-006/ADR-008): `omnia.toml`
(log level + the `[plugins]` enable map) and `features.toml` (every per-feature section) are stored
via `col.set_config`, and `smart_notes` keeps its own blob there (`omnia:smart_notes`). The user
runs Anki on **macOS, Windows and Ubuntu**, and those devices are **not upgraded at the same time** —
AnkiWeb happily syncs a blob written by a newer Omnia down onto a device running an older one.

Every config model was `extra = "forbid"` — the same `_Strict` base copy-pasted into
`core/config/models.py` and all six `plugins/*/config.py`. So a key a newer release added was a
`ValidationError` on the older device, and each place it surfaced failed quietly:

* `ConfigRepository.feature_settings()` is called from inside `PluginManager._activate`'s
  try-block → the exception is logged and the **feature simply never enables**.
* `OmniaConfig.parse_obj` runs at load → an unknown `[llm.<provider>]` key **bricks config load**.
* `SmartNotesStore.load()` runs inside note-add / review hooks → smart-notes dies mid-review.

The first attempt at a fix flipped smart_notes' models to `extra = "ignore"`. Review proved that is
worse than it looks: `store.save` persists `settings.dict()`, so an "ignoring" old client **loads a
blob, silently drops the keys it did not understand, and writes the stripped version back** —
destroying the newer device's settings on the next sync. Silent data loss instead of a loud crash.

### Decision
One seam, `src/omnia/core/config/base.py`, exporting the two possible answers; every config model
picks one:

* **`PersistedModel`** (`extra = "allow"`) — anything parsed from or serialized into persisted
  config: `OmniaConfig` + all `[llm]`/`[tts]`/`[plugins]` models, all six plugin settings models,
  and the whole smart_notes tree (`SmartNotesSettings`, `SmartNotesNoteTypeConfig`,
  `SmartNotesFieldConfig`, `FieldDep`). Unknown keys are retained as extra attributes **and
  round-trip through `.dict()`/`.copy()`**, verified against the vendored pydantic 1.10.26.
* **`StrictModel`** (`extra = "forbid"`) — models that never reach storage, where an unknown key
  really is a typo: today only `SmartNotesFieldRule`, the in-memory rule the engine compiles.

Unknown **values** of known fields are also **kept verbatim** — never rewritten on load — and
neutralized where they are CONSUMED, so the older device is a pass-through for values it cannot
interpret exactly as it is for keys:

* `SmartNotesFieldConfig.type`: a generation type this version does not implement loads as-is.
  Two consumption seams disarm it: `SmartNotesNoteTypeConfig.generatable_fields()` skips the row
  (so this version never generates the WRONG content into a field the newer version means to fill
  differently), and `compile_field_rule` degrades `kind` to `"text"` (so the graph/consistency
  views, which compile every row, don't trip the strict `SmartNotesFieldRule.kind`). A third
  consumer, `account.py`, already skipped unmapped types. Rewriting the row on load instead was
  tried and rejected — see *Alternatives considered*.
* `FieldDep.kind`: same rule. Every consumer asks "is this edge `hard`?", so an unknown kind
  already degrades to the weaker `soft` behaviour — it can order generation, never block it — and
  the older device hands the edge back to the newer one unchanged.

### Rationale
* **Why `allow` beats `ignore`**: both let the old client load, but only `allow` survives the
  **round trip**. Omnia's writes are whole-object (`col.set_config(KEY, settings.dict())`), so with
  `ignore` the older device is a data shredder — the failure is silent, cross-device, and only
  noticed after the newer device's settings are gone. With `allow` the older device is a faithful
  pass-through for config it cannot interpret.
* **Why fix the seam, not the leaf**: the identical `_Strict` base was duplicated in seven files;
  patching only smart_notes left the same hazard on every plugin section, where the symptom
  ("the feature just doesn't turn on") is even harder to diagnose.
* **Why keep a strict base at all**: for a model built from a GUI payload or compiled in memory,
  an unknown key is a bug in *our* code and should fail loudly. Deleting strictness everywhere
  would trade one silent failure mode for another.
* Declared fields are still fully validated — this is about unknown keys, not loose typing.

### Consequences
* **Positive**: a mixed-version fleet is safe in both directions — the newer device's settings
  survive an older device loading and rewriting them, and an older device no longer loses a whole
  feature (or its whole config) to a key it has never heard of. New keys can be shipped without a
  migration or a "everyone must upgrade first" step.
* **Negative**: a genuine typo in a hand-edited `features.toml` section is now silently ignored
  instead of raising (mitigated: the settings GUI writes those sections, and the schema-derived
  form only offers real fields).
* **Bounded loss remains at exactly one spot, and it is user-initiated**: the smart-notes dialog
  rebuilds the **edited** note type's rows from its JS payload, so that one note type's unknown row
  keys (and an unknown `type` the dropdown cannot show) collapse when the user saves that note
  type — every other note type and the top level pass through untouched. Loading, enabling a
  feature, generating, or saving a *different* note type never rewrites anything.
* Future changes should **arrive as a new KEY**, not as a new value of an existing enum-ish field,
  unless that field's consumers neutralize unknown values (today: `type`, `kind`). Note
  `TypedAccuracySettings.pass_ease` is a `Literal` and still rejects an unknown value.

### Alternatives considered
* **`extra = "ignore"`** (the first attempt): rejected — loads fine, then silently erases the newer
  device's keys on the next save. Forward compatibility without round-trip fidelity is a data-loss
  bug wearing a compat hat.
* **Keep `forbid` and version the blob** (write a `schema_version`, migrate on read): rejected as
  far more machinery for the same outcome, and it still cannot help an OLD client read a FUTURE
  version it has no migration for.
* **Catch `ValidationError` at each call site** and fall back to defaults: rejected — it turns a
  newer device's real settings into defaults (data loss again) and scatters the same `try` over
  the manager, the loader and every smart-notes hook instead of fixing the model contract.
* **Neutralize an unknown value on LOAD** (a `pre` root validator rewriting `SmartNotesFieldConfig.type`
  to the default + `enabled = False`; the symmetric idea for `FieldDep.kind`): implemented first,
  then rejected — it reintroduces the very bug this ADR exists to kill. `store.save` persists the
  WHOLE `settings.dict()`, and the settings dialog re-serializes all of it whenever the user saves
  ANY note type, so a load-time rewrite means editing note type B silently destroys note type A's
  row on the authoring device. Neutralizing at the consumption seams costs two small guards and
  keeps the data intact.

## ADR-011: A safety-critical tool fails closed — no decline, no rival, no degraded value

**Date**: 2026-08-14
**Status**: Superseded by ADR-012

### Context
The smart-notes **tools** seam is built on falling through: a field carries an ordered chain
(`SmartNotesFieldRule.tools`) and `GenerationPipeline.run` tries each tool until one produces —
a deterministic tool declines (`NotApplicable`), the paid `ai` tool takes over, nobody notices.
That default is right for `cloze`: if the word is not in the sentence, letting the LLM write the
cloze is merely a different, more expensive answer.

`cloze_audio` broke the assumption. It exists because plain TTS of a cloze field speaks the
answer out loud (`core/text.strip_markup` unwraps `{{c1::survive}}` to `survive` before the text
reaches a provider), so it synthesizes the sentence in pieces with the answer replaced by
silence. For this tool "the next tool fills the field instead" is not a different answer — it is
the exact card-ruining outcome the tool exists to prevent, arriving silently, with a green batch
summary and nothing in the trace.

Three review rounds each found a different door left open, and all three were the same mistake —
a *local* judgement that falling through was harmless:

1. **A value it could not parse.** `ClozeAudioParams` is a `PersistedModel` (ADR-010), so an
   unknown KEY survives. But a known key holding an unparsable VALUE (`beep_hz: "auto"` from a
   newer release, a hand-edited blob, or the picker's own `Number("1e999")` → `Infinity` →
   `null`) made `Tool.parse_params` raise inside the pipeline's attempt guard — one failed
   attempt, chain continues, `ai` speaks the sentence.
2. **The "one safe decline".** It returned `NotApplicable` when its own `source_field` held
   nothing speakable, reasoning "no sentence, so no answer to leak". The next tool does not
   speak `source_field`; it speaks `tts_text(rule, fields)` — the RULE's prompt refs. Prompt
   `{{Sentence}} {{Hint}}` with `Sentence = "&nbsp;"` (or `<br>`, `[sound:x.mp3]`, `<b></b>` —
   everyday field contents, all non-blank so the hard-prerequisite gate does not block) and the
   answer in `Hint` leaks exactly as loudly.
3. **The other ordering.** `TerminalToolError` (which stops the chain) only protects the tools
   that come AFTER. The picker APPENDS a newly ticked tool, so ticking AI and then Cloze audio
   builds `[ai, cloze_audio]` — `ai` simply wins first. The first guard against this was a
   picker warning that hard-coded `entry.tool === "cloze_audio"` and inspected only the entries
   *after* it: one feature tool's safety semantics living in shared UI infrastructure, checking
   one ordering.

### Decision
A tool whose replacement is HARMFUL rather than merely different declares that on the class, and
the seams enforce it generically. Three rules, all now on `Tool`/`cloze_audio`, none in the UI:

* **Unparsable params are terminal, not one bad attempt.** A safety-critical tool overrides
  `parse_params` and re-raises `ValidationError` as `TerminalToolError`. ADR-010 still holds for
  KEYS (unknown keys survive the round trip); it does not extend to a known key whose VALUE this
  build cannot interpret when acting on the wrong interpretation is unsafe.
* **It never returns `NotApplicable`.** A tool cannot see what a later tool would generate, so
  it cannot prove a decline is harmless. Every inability to produce raises `TerminalToolError`,
  stated in the class docstring so the next contributor cannot re-add a "safe" decline.
* **It refuses to share a chain.** `Tool.exclusive: ClassVar[bool] = False` means "this tool must
  not share a chain with another tool that can generate the same kind, because a sibling
  producing instead of it would be unsafe". `cloze_audio` sets it; `registry.chain_conflict`
  finds such a pairing (rivals scoped to the field's `kind`; tools this build lacks are skipped,
  since nothing here can know what they would produce) and `GenerationPipeline.run` refuses the
  chain BEFORE any tool runs, in either order, failing the field with a message naming the
  problem. `tools_catalog` surfaces the flag so the picker warns from the declared property —
  no tool name in the JS.

### Rationale
* **Fail closed beats fail open** when the failure is silent and the artifact is a card the user
  will study for years. A refused field is one red count in the batch summary and a note kept for
  retry; a leaked answer is invisible until review time and indistinguishable from a bad prompt.
* **A warning is not a control.** The user can save the chain anyway, the config syncs to other
  devices, and a device on an older Omnia resolves the entry to `unknown_tool` and lets the rival
  produce. The runtime refusal is the only part of that story this build controls; the warning
  stays as the *explanation*, shown in BOTH orderings.
* **The property belongs on the tool.** The picker's hard-coded tool name is what made the guard
  cover exactly one ordering: shared infrastructure was re-deriving a rule it had no business
  knowing. Declared on the class, the same fact drives the pipeline and the picker, and the next
  tool with the same need is one `ClassVar` away.
* **Scoped to the kind, not the chain.** An image tool sitting in a tts field's chain can never
  produce there, so it is not a rival — the refusal stays as narrow as the hazard.

### Consequences
* **Positive**: `[cloze_audio, ai]` and `[ai, cloze_audio]` both fail loudly instead of one of
  them producing audio that reads the answer. Every branch of the tool is now terminal, so a
  future edit cannot reintroduce a leak by adding a decline. The picker names no feature tool
  (a test asserts `"cloze_audio" not in` the built page).
* **Negative**: a field whose source is legitimately empty now FAILS instead of quietly falling
  back to the LLM. That is a real regression in convenience, accepted because "quietly" is the
  problem; the empty case is largely already blocked upstream by the hard-prerequisite gate.
* **Negative**: a user who genuinely wants "cloze audio, else plain TTS" cannot express it. That
  configuration is precisely the leak, so it is refused by design, not by omission.
* **Bounded by intent**: everything here is opt-in per tool. `cloze`, `ai` and every future tool
  keep the fall-through semantics the chain exists for; `exclusive` defaults to False and
  `NotApplicable` remains the normal way to decline.
* The three rules are one idea and travel together: a tool that fails closed must fail closed on
  its inputs (params), on its own inability (never decline), and on its neighbours (exclusive).
  Adopting one without the others leaves a door open — which is how each of these was found.

### Alternatives considered
* **Warn in the picker only** (the first attempt at ordering safety): rejected — a warning does
  not stop a save, does not survive a sync to a device with an older build, and covered only the
  ordering the hard-coded check looked at.
* **Refuse the chain at SAVE time** (block the dialog): rejected as insufficient on its own — a
  chain also arrives by sync and by hand-editing the blob, so the runtime must refuse regardless;
  and blocking the save would prevent a user from editing a chain that arrived already broken.
* **Degrade an unparsable param to its default** (the ADR-010 reflex): rejected — "hide the
  answer with the settings I guessed" is exactly the silent wrong action this tool cannot take.
  Unknown KEYS still survive; only an uninterpretable VALUE of a known key is fatal, and only
  for a tool that declares itself safety-critical.
* **Let `cloze_audio` produce silence / skip the field quietly** instead of failing: rejected —
  it writes a plausible-looking media file (or nothing) into the note and the user discovers it
  at review time, which is the same silent-failure class as the leak.
* **A pipeline-level "never fall through after tool X" config knob**: rejected — it is the same
  hard-coded knowledge, moved from the JS into config, and it would need to be set correctly on
  every field by every user.


## ADR-012: The tool chain has exactly one rule — run in order, fall through on failure

**Date**: 2026-08-14
**Status**: Accepted
**Supersedes**: ADR-011

### Context
ADR-011 gave the tools seam two exceptions to falling through, both existing to protect one
tool. `cloze_audio` speaks a sentence with the answer replaced by silence; when it cannot mask,
a chain of `[cloze_audio, ai]` would hand the same field to plain TTS, which reads the answer
aloud (`strip_markup` unwraps `{{c1::survive}}` to `survive`). The two mechanisms were:

* `TerminalToolError` — a failure that STOPS the chain rather than falling through;
* `Tool.exclusive` + `registry.chain_conflict` — a tool that refuses to share a chain with any
  other tool serving the same kind, checked before anything runs.

Both worked. Both were also invisible from the settings UI: the picker showed an ordered list
that the runtime would sometimes decline to run, and sometimes stop halfway, for reasons
belonging to one tool. Reviewing the picker, the project owner ruled that a chain should
simply run its tools in the configured order and move to the next one whenever a tool fails —
and that this needs no exception.

### Decision
A chain runs its tools in the configured order. Every failure — a decline, an empty result, a
raised `ToolError`, an unparsable params dict, a tool this build does not have — moves to the
next tool. There are no exceptions, no tool can halt the chain, and no chain is refused before
it runs. `TerminalToolError`, `Tool.exclusive` and `chain_conflict` are removed.

A tool's guarantee is therefore about ITSELF, never about what follows it. `cloze_audio`'s is
unchanged and absolute: *it* never speaks the answer — it masks or it raises, and it never
returns `NotApplicable`.

### Rationale
A rule with one special case is a rule nobody can predict from the UI. The picker presents an
ordered list; "these run top-down until one produces" is a sentence a user can hold, and every
exception to it is behaviour they can only discover by hitting it. The previous design also put
safety semantics for one tool into the shared pipeline and the shared picker, which is the
coupling the seam exists to avoid.

The protection was narrower than it looked. Halting only guards the tools ordered AFTER the
failing one, so `[ai, cloze_audio]` — the ordering the picker's append-to-end made easiest to
build — was never covered by it at all; that ordering needed the separate `exclusive` check.
Two mechanisms for one hazard, neither complete alone.

### Consequences
**Positive**: one rule, stated in one sentence, identical in the picker and at runtime. The
pipeline no longer carries any tool's semantics. `Tool` loses two class attributes and the
registry loses a function.

**Negative, and stated plainly**: a field configured `[cloze_audio, <any tts tool>]` whose
`cloze_audio` fails WILL have the next tool speak the sentence, answer included. This is a real
way to ruin a card, and it is now a configuration decision rather than something the runtime
prevents. Mitigations that remain: the tool raises (never declines) so the trace always names
the failure; its `description` and module docstring say to put no tts tool after it; and its
`required_params` stop the commonest cause — a blank `source_field`/`word_field` guessing at
the wrong field.

### Alternatives considered
- **Keep ADR-011.** Rejected by the ruling above.
- **Keep only `exclusive` (refuse the chain, drop the halt).** Still a special case in the
  shared picker and pipeline, and still a chain the UI offers but the runtime will not run.
- **An advisory-only warning in the picker.** Not adopted here, but compatible with this ADR —
  advice in the UI is not a special case in the chain. Worth revisiting.


## ADR-013: The import allowlist is not the boundary — the informed review is

**Date**: 2026-08-15
**Status**: Accepted

### Context
A user-authored tool is Python the user describes in plain English, an LLM writes, and the user
reads and test-runs before it is saved to `user_files/tools/`. `ImportGuard` restricted it to a
small allowlist — no `os`, no `subprocess`, no `pathlib`, and `open` was a flagged call.

That list was presented as a safety boundary, and it was not one. An Anki add-on runs
unrestricted Python in Anki's own process; Omnia itself can do anything the user can. The Tools
tab has always said so on screen: *"Everything here runs with the same access as the add-on
itself, which is why nothing is saved until you have read the code and run it."* The narrow
list bought no containment while the surrounding reality was wide open.

What it did cost was the feature's point. A transform that CONVERTS rather than rewrites — pull
the audio out of a video, resize a picture, read a sidecar file — cannot be written without
touching a file. Asked for one, the model could not comply and could not refuse (the output
rules demanded a module), so it produced the nearest plausible thing: a tool that renamed
`".mp4"` to `".mp3"` in a string, created no file, saved cleanly, and wrote a reference to
something that did not exist.

### Decision
The allowlist admits the filesystem and process modules (`os`, `subprocess`, `pathlib`,
`shutil`, `io`, `tempfile`, `wave`, …), and `open` stops being a flagged call.

The guard is kept, with a smaller and honest job:

* **the source you read is the source that runs.** An import outside the list is refused at
  LOAD, not only at save, so a file edited after review cannot silently gain a capability; and
  `eval`/`exec`/`compile`/`__import__` stay refused — not for containment, but because building
  code from a string defeats exactly that guarantee.

The control that replaces it is the **informed review**, which was always the real gate:

* `risky_operations()` reads the module and states what it reaches for in the reader's words —
  "reads and writes files", "runs other programs on your computer" — rendered above the code.
* It must arrive **before the code runs**, not after. The review gate requires pressing Run, and
  Run executes; a summary computed from the test RESULT describes damage already done. So it
  ships with the source on every path that ends in Run: generate, edit an existing tool, and
  paste into the editor.
* A tool that only reshapes text raises nothing, so the banner appearing is itself the signal —
  which makes an empty banner a positive claim, and any path that leaves it empty over risky
  code a bug rather than an omission.

A **test run** is treated as more dangerous than a real one, because it executes code the user
has not finished reviewing. Its `media_dir()` resolves only to a temp stage of copies, never to
the collection. Testing against a file that IS in the collection stays one click — the picker
opens there and returns a copy — so a tool that writes over its own input during a test damages
a copy instead of the user's media.

### Rationale
A boundary nobody can rely on is worse than no boundary, because it is budgeted for. The
allowlist looked like containment, so the review looked optional; in fact the review was
load-bearing and uninformed. Widening the imports and informing the review moves the honesty
and the protection to the same place.

The alternative shapes were considered and are worse for this codebase: a curated media API
(`ctx.media.read`/`write`) constrains what the next transform can be, and this is a feature
whose whole premise is that the next request is unpredictable; a path-confined filesystem is
real containment but does not survive `subprocess`, which the conversions people ask for
require.

### Consequences
**Positive**: the feature can express what users ask it for. The reviewer is told what to look
for instead of being expected to spot an import on line 3 of forty. A test cannot damage the
collection.

**Negative, stated plainly**: an approved user tool can do anything the user running Anki can
do. It can read `~/.ssh`, delete documents, or corrupt the collection — and a corrupted
collection SYNCS. The mitigations are the mandatory read-and-run review, the pre-run summary,
and that tools are files on disk in `user_files/` rather than synced config, so approving one
never executes code on another device.

### Alternatives considered
- **Keep the allowlist narrow.** Rejected: it contained nothing and blocked the feature.
- **A curated media API on `ctx`.** Rejected as the primary mechanism: it presumes the shape of
  transforms nobody has asked for yet. `ctx.audio` remains for the audio runtime specifically,
  because that is Omnia's own managed process rather than a tool reaching out.
- **A path-confined filesystem (media folder only, no delete, no overwrite).** Genuinely safer
  and genuinely flexible, and the right answer if `subprocess` were off the table. It is not:
  conversion means running a codec.
- **Refuse the request instead.** What the previous release did, and correct while the
  capability was absent. Once it exists, refusing is as wrong as inventing.


---

## ADR-014: One generic `ProviderRegistry`; LLM drops its builder table

**Date**: 2026-08-15
**Status**: Accepted

### Context
The provider layer had two kinds and two mechanisms for the same job. TTS self-registered:
`@register_tts("<name>")` bound a class into `TTS_REGISTRY`, and `create_tts_provider` called
`cls.from_config(config, http)`. LLM did not — `core/providers/llm/factory.py` carried a
hand-maintained `_BUILDERS` dict of `_build_*` closures **plus** a second `_PROVIDER_CLASSES`
dict mapping the same names to classes, because the closures alone could not answer "does this
provider need a key?" without building one. Two dicts, one truth: a test existed purely to
assert they had not drifted apart.

The duplication had already leaked out of `core/`. `plugins/smart_notes/account.py` needed the
provider CLASS name to join usage rows onto the models a collection uses, and the only way to
get it was to import the private `_PROVIDER_CLASSES` — a feature reaching into a seam's
internals, which is exactly what the coupling rule exists to prevent.

A third kind is foreseeable, and would have arrived to a choice between two patterns with no
stated reason to prefer either.

### Decision
Lift the mechanism to the root of `core/providers`, and bind it once per kind.

* **`core/providers/base.py`** — `ProviderBase`, the kind-agnostic contract: `name`,
  `requires_api`, `from_config(config, http)`. `LLMProvider` and `TTSProvider` now subclass it
  and keep only what is theirs (`generate_text`; `synthesize`/`audio_ext`/`CURATED_VOICES`).
* **`core/providers/registry.py`** — `ProviderRegistry`, one instance per kind, carrying
  `register`, `create`, `names`, `classes`, `requiring_api`, `keyless`. It subclasses
  `collections.abc.Mapping`, so every existing read of a registry (`registry[name]`,
  `dict(registry)`, `set(registry)`, `.items()`, `.values()`, truthiness, `dict == registry`)
  kept working untouched — the whole TTS suite passed unedited across the change, which is what
  proved the swap was source-compatible rather than merely plausible.
* **`llm/registry.py`** binds `LLM_REGISTRY` (`default="openai_compatible"`); `tts/registry.py`
  binds `TTS_REGISTRY` (`default="google_translate"`). The per-kind default is a constructor
  argument because it is data, not logic.
* **`factory.py` is deleted, with no shim.** The three `_build_*` bodies moved verbatim into
  `from_config` classmethods on the providers they built. `account.py` now calls the public
  `get_llm(name)`.

No provider was renamed, added, or removed. That is deliberate and is what leaves ADR-010
config, `.secrets/llm.<name>.*`, synced `SmartNotesFieldConfig.provider` values, and
`omnia:usage` rows untouched.

### Rationale
A second mechanism is a second thing to keep correct, and this one had already produced a
drift-guard test and a private-symbol import from a plugin. Registering *is* the abstraction —
it is not per-kind — so the kinds should differ only where they genuinely differ:
`tts_providers_with_ext` reads `audio_ext` and has no LLM analogue, so it stays in the TTS
binding; the curated GUI subsets and the per-kind default stay per kind.

Deleting the factory rather than leaving a shim was the sharper call. A shim would have meant
two doorways to one mechanism, a doc that could not state one recipe, and — worse — the
drift-guard tests would have kept importing a dead module and passing, still pointed at the
thing that no longer ran.

### Consequences
**Positive**: one recipe to document and to follow; a third kind costs one line
(`ProviderRegistry("CV", default=...)`) instead of a design decision. The last plugin→core
private reach is gone. The mechanics are now testable on a throwaway registry, so registration
rules are covered without mutating the live ones.

**Negative / constraints this imposes**:
- **`register` must never stamp `cls.name`** (unlike `core/registry.py`, which stamps `cls.id`).
  One class serves several names; the usage rows and the Account tab's join read that single
  class name, and stamping would both corrupt them and make the last registered name win.
- **`from_config` must stay pure construction.** `ProviderHub.llm()` calls `create` while
  holding its cache lock. (`GeminiVertexProvider.from_config` reads the service-account file
  when `credentials_path` is set — that already happened inside the lock before this change and
  sits at the identical point in the call chain, so it is preserved, not worsened. Do not add
  more I/O there.)
- **`from_config` must be defined on the class, not inherited.** `GeminiVertexProvider`
  subclasses `GeminiProvider`; inheriting its builder would reject a valid Vertex project with
  "Gemini provider requires an api_key". Pinned by a test over `registry.classes()`.
- **A registered name still needs its own `LLMSettings`/`TTSSettings` subsection**, or the hub
  hands the provider `{"provider": name}` with no credentials and the user sees an auth error
  where a config error belongs. Pinned by `test_every_registered_name_has_a_config_subsection`.
- `create` raises for an unknown name and never falls back to the default class — a config
  synced from a newer device must fail loudly, not silently generate on another provider
  (ADR-010's world, where unknown keys survive a round-trip).
- `ProviderRegistry` instances are unhashable (`Mapping` sets `__hash__ = None`). Nothing
  hashes them today.

### Alternatives considered
- **Keep two mechanisms, just make LLM's private dicts public.** Rejected: it fixes the
  coupling symptom and leaves the duplication, plus the drift-guard test, in place.
- **Migrate LLM onto a copy of the TTS registry module.** Rejected: three copies of the
  mechanism once a third kind lands.
- **Keep `factory.py` as a deprecation shim re-exporting the new functions.** Rejected — see
  Rationale; the shim's main effect would have been to keep dead imports passing.
- **Also migrate `smart_notes/engine/tools/registry.py` and `note_maintenance/registry.py`
  onto `ProviderRegistry`.** Rejected: they stamp attributes (`cls.task_id`) and preserve
  registration order for the UI, both of which this registry deliberately does not do — and
  `core/*` may not know about `Tool`/`MaintenanceTask` anyway.

---

## ADR-015: A run path may fetch inert DATA on first use; it still never installs a RUNTIME

**Date**: 2026-08-15
**Status**: Accepted (refines ADR-005)

### Context
ADR-005 gave native-runtime providers an add-on-managed sidecar venv and one hard invariant,
repeated in three docstrings and in `native_runtime`'s module header: **the synthesis/run paths
never auto-install**. Installing is an explicit user toggle; a run path that finds the runtime
missing raises "enable it in Smart Notes → Options → Advanced" and stops. The stated reason is
that this "keeps the slow, network-heavy first-run install out of the synthesis path".

Shipping the piper voice weights inside the `.ankiaddon` then became untenable: the package was
59 MB, ~99% of it one Vietnamese voice, re-downloaded by every user on install *and on every
update*, for a voice most of them will never play. The weights moved out, and the first
synthesis with a piper voice now fetches them into `user_files/models/piper/`.

That is a run path reaching the network on first use — close enough to the ADR-005 invariant
that a reader is entitled to think the rule was quietly abandoned. It was not. But recording
that only in FEATURE_LOG left the next contributor reading ADR-005 and three `require_installed`
docstrings with an invariant the code appears to break, and no stated boundary.

### Decision
Draw the line at **what is being acquired**, not at how long it takes:

* **A RUNTIME is never auto-acquired.** A venv + `pip install` needs a host interpreter Omnia
  may not find, pulls arbitrary transitive code, can fail in a dozen ways a user cannot read,
  and costs up to gigabytes. It stays an explicit toggle. ADR-005 is unchanged.
* **Inert DATA may be fetched once, on the run path**, when it is: a single URL, pinned by exact
  byte count *and* SHA-256, written atomically, cached in `user_files` (so it survives every
  add-on update and is fetched once per machine, not once per release), and reportable in the
  progress dialog the op already owns. A voice model is data: nothing is executed, nothing is
  installed, and a wrong or truncated byte stream is rejected rather than used.
* **The cheap check runs FIRST.** Any provider whose run path can fetch data must verify its
  runtime is available *before* resolving anything expensive. `PiperRunner.ensure_ready()` is
  that seam: `PiperTTS.synthesize` calls it, and only then resolves the voice.
  `NativeRuntimeManager._require_installed` became public `require_installed` so the runner
  reuses the one message rather than re-wording it.

### Rationale
The ordering is the whole point of ADR-005's rationale, and reversing it produced the worst
possible outcome: piper's runtime is opt-in and **off by default**, so a user who had never
enabled it downloaded all 63 MB — minutes, with a ticking progress label — and *then* read
"piper isn't installed". The expensive step ran exclusively for people it could not help.

With the check first, the two costs are correctly ordered: an unavailable runtime fails in one
`is_dir()`, and the 63 MB is only ever spent by a user who can actually synthesize with it.

The data/runtime distinction is not a loophole for convenience — it is the difference between
"the add-on downloaded a file it verified against a digest" and "the add-on installed and ran
software on the user's machine". The first is what any add-on with a model does; the second is
what ADR-005 exists to keep opt-in.

### Consequences
- (+) The 59 MB package is ~1 MB; nobody re-downloads a voice on every add-on update.
- (+) A user without the piper runtime gets the actionable "enable it in Advanced" message
  immediately, having spent no bandwidth.
- (−) The first piper synthesis on a new machine is slow (a ~60 MB fetch) — reported in, and
  cancellable from, the progress dialog on the interactive paths. Review-time pre-generation has
  no dialog by design, so there the fetch is silent; that is stated in the README rather than
  papered over with a dialog that would interrupt a review.
- (−) One more thing that can fail at synthesis time. Contained by construction: every failure
  (dead network, truncated chunked body, over-long body, full disk, bad digest, user cancel)
  becomes ONE `ProviderError` naming the voice, and leaves no partial file behind.

### The rule this imposes on the next provider
A new native provider that wants first-use data must: (1) pin size + digest, (2) write via
temp-file → verify → `fsync` → `os.replace` into `user_files`, (3) implement `ensure_ready()`
and have the provider call it before resolving the data, and (4) funnel every failure into one
message that names the asset. If any of those is impractical, the data is not "inert" enough
and belongs behind the install toggle with the runtime.

### Alternatives considered
- **Keep bundling the weights.** Rejected: 59 MB per install and per update, over AnkiWeb's
  practical limit, for a voice most users never play.
- **Make the voice download part of the runtime install toggle.** Rejected: it welds an
  8 MB-ish decision to a ~50 MB one, forces users who already have a voice on disk (developers
  with Git LFS, anyone who dropped in their own `.onnx`) through an install they do not need,
  and still leaves the run path needing a fallback when a *different* voice is selected later.
- **Ship a smaller default voice.** Rejected: it shrinks the problem without changing its shape
  — every user still pays for a voice on every update — and costs audio quality.
- **Auto-install the runtime too, "since we're downloading anyway".** Rejected: that is exactly
  ADR-005's decision, and none of the properties that make a voice safe (one URL, pinned digest,
  inert bytes) hold for a pip install.

## ADR-016: Generation concurrency is bounded at the HTTP seam; the provider interface grows by new method, never by new keyword

**Date**: 2026-08-21
**Status**: Accepted

### Context
Smart-notes generation was entirely sequential. `BatchGenerator` chunked notes only to poll
cancel and update progress (`_CHUNK_SIZE = 5`, whose comment claimed the chunking avoided
"flooding the provider"), then ran notes one at a time; `GenerationService.generate_note` ran
rules one at a time. On the note type this was measured against — 35 fields, 17 with generation
on, 8435 notes — that is 17 serial round trips per note, when the dependency graph puts those 17
fields in only **5 levels** (widths 2/4/6/4/1). Fields inside a level are independent by
construction, so the sequential floor is 5 round trips, not 17, and nothing overlaps two notes
at all.

Where that shape comes from, stated because everything downstream rests on it: it is the
author's own collection, read once, and there is **no artefact of it in this repo** — no export,
no fixture, nothing to re-derive it from. The benchmark's workload is a hand-built replica of
those counts (`_WORKLOAD` in `tests/benchmarks/smart_notes_throughput.py`), so every number this
ADR quotes is exact for that replica and only as representative as the replica is. A deck with
fewer AI fields sharing one template, or more dependency levels, moves both the L1 speed-up and
the L3 call-count win.

Making it concurrent raises three questions that have to be answered together, because getting
any one wrong is worse than staying slow:

1. **What bounds the provider?** Sequential execution was an accidental throttle. Remove it and
   nothing stands between N workers and a systematic 429. `RetryPolicy` is not that thing — it
   absorbs an occasional 429; it cannot prevent a systematic one, and under fan-out it amplifies.
2. **Where do threads live?** `engine/` is pure and unit-tests headless; a `ThreadPoolExecutor`
   in it would be both a layering violation and a lifetime bug (nothing may outlive the `QueryOp`
   that owns it — and the test harness runs `op()` inline, so a leaked pool would pass CI).
3. **What must not change?** The dependency order, the block gate, the silent-skip predicate,
   the progress/cancel behaviour, and — above all — which note each result and each error belongs
   to. This feature has just spent a release removing a "no output, no error" bug; introducing
   "wrong output, no error" would be strictly worse.

### Decision

**The limiter lives at the HTTP request boundary, and there is exactly one of it.**
`core/network/limiter.py` holds a `ProviderLimiter` (a re-sizable counting gate over a
`threading.Condition`) and a module-level `PROVIDER_LIMITER`; `core/network/http.py` holds a
`ThrottledHttpClient` decorator, and `DEFAULT_HTTP_CLIENT` is now
`ThrottledHttpClient(UrllibHttpClient(), PROVIDER_LIMITER)`.

Rejected homes, and why:
* **`GenerationService`** — four construction sites, and the authoring/preview paths call
  `hub.llm().generate_text` directly. A limiter here does not exist for a user previewing a
  prompt while a batch runs.
* **`ProviderHub`** — three construction sites, one per dialog. Per-hub means per-dialog, which
  is exactly the per-note/per-notetype scoping the quota forbids.
* **Per rule, or per `generate_text`** — one rule is not one provider call. A voice-less TTS rule
  makes an extra LLM call for language detection; `cloze_audio` synthesises N segments in one
  run; a tool chain may call a provider several times before one produces. A bound sized "rules
  in flight" under-counts, and a rule holding a permit while its nested detector call waits for
  one is a self-deadlock.
* **The HTTP boundary** — every provider call passes through `HttpClient`, and an HTTP request
  never contains another HTTP request. So the bound is complete AND acquisition cannot nest.
  (`get_json` delegates to the *inner* client's `get_bytes`, so one round trip spends one permit;
  a test pins that.)

**The permit is held across `RetryPolicy`'s backoff.** That is natural backpressure and it is the
split made real: the limiter prevents a systematic 429, retry absorbs an occasional one.
Releasing during the sleep would let the pool refill inside the rate-limit window.

**Capacity is `request_capacity(workers)`, and the previous value is restored on exit.**
`pooled_dispatch` — the one place that knows both numbers — narrows the limiter for the duration
of a run and puts it back afterwards.

> **AMENDED after review (2026-08-21).** This paragraph originally read *"capacity is
> `workers + 1`, and the `+1` is a reserved lane for an interactive call"*. Both halves were
> wrong. The lane was unreachable — every interactive path goes through a `QueryOp` on Anki's
> single-worker collection executor, so nothing can run alongside a batch — and a capacity one
> greater than the maximum number of permits anybody can hold is a bound that provably never
> binds: measured at N=1/2/4/8/16, the limiter's peak was always exactly `capacity − 1` and its
> total wait was tens of microseconds. LAYER 1's stated bound was being delivered entirely by
> the pool width. The `+1` is gone; the capacity is now `workers`, which is honest but still
> only a restatement of the pool unless it can be set independently — so
> `OMNIA_MAX_CONCURRENT_REQUESTS` overrides it, and expresses the case that motivated a
> separate mechanism in the first place: *run 8 fields at once, keep 3 requests in flight*, for
> a note type where one unit is several calls. `tests/plugins/smart_notes/test_concurrency.py`
> now drives 8 pool workers at a bound of 3 through the real `ThrottledHttpClient` and asserts
> the observed peak is 3 where an unthrottled control run observes 8.
>
> The RESTING capacity also changed, from 1 to 16 (the widest fan-out this build starts). At 1
> the limiter serialised two unrelated interactive calls process-wide — an Account credit fetch
> and a Test key — which were concurrent before this module existed. Nothing fans out at rest,
> so nothing needs bounding there; the bound belongs to the run, and now goes away with it.

**The engine gets a `Dispatch` seam, defaulting to sequential.**
`core/concurrency/dispatch.py` defines `Dispatch.run(units) -> list[result | Exception]`,
order-preserving and never-raising, plus `SequentialDispatch`. The `ThreadPoolExecutor`-backed
`PooledDispatch` and the `pooled_dispatch` context manager live in `core/concurrency/pool.py`.
`engine/` imports no `concurrent.futures`, no `aqt`.

> **AMENDED after review (2026-08-21).** Both modules originally sat under
> `plugins/smart_notes/` (`engine/dispatch.py` and `integration/dispatch.py`). Neither contained
> one line of smart-notes: they run argument-less callables and bound a process-wide limiter, so
> the next plugin that needs bounded work would have had to import them THROUGH smart_notes or
> copy them. They are now `core/concurrency/{dispatch,pool}.py`, and the split between the two
> files is unchanged and still load-bearing — it is what keeps a pure-logic module able to
> depend on the protocol without pulling in `concurrent.futures`. The limiter's docstring also
> stopped naming a plugin, in prose as well as in code: `core/*` must not know which feature
> uses it.
>
> Moving the pool into `core` surfaced a latent import cycle it had been hiding behind: importing
> `core.network.limiter` ran `core/network/__init__`, which eagerly imported `http`, which
> imports `core.providers.errors` for `ProviderError`, whose package imports every concrete
> provider, each of which imports `core.network.http` — half-built. It only ever worked because
> every import path in the tree happened to reach `core.providers` first. `core/network/__init__`
> now resolves its re-exports lazily (PEP 562, as `envs.py` does), so importing a submodule costs
> that submodule and nothing else; a cold-interpreter test pins it.

> **AMENDED (2026-08-21), superseded in part by ADR-017.** This sentence originally also said
> "no `threading`", and that stopped being true in the same change set that shipped it:
> `engine/batching.py` imports `threading` for `FieldBudget`'s lock. ADR-017 records the fact,
> but an ADR nobody has amended is the one people grep, and a rule a grep falsifies is worse
> than no rule. The invariant that actually holds, and the one to enforce in review, is
> **`engine/` imports no `aqt`, no `anki`, and no `concurrent.futures`** — no Anki, and no
> threading POLICY. A lock over the engine's own state is not policy; deciding how many threads
> exist is, and that still lives in `integration/`.

**Levels come from the same edge set `order_rules` walks.** `ordering._build_adjacency` is
extracted and shared by `order_rules` and the new `order_rule_levels`, so the two can never
disagree about a dependency. `order_rules` is deliberately NOT redefined as the flattening of the
levels: for the golden fixture, flattening gives `[Def, Pic, Audio, Usage]` where three tests pin
`[Def, Usage, Pic, Audio]`. `order_rules` stays the authority on ORDER; levels supply only
PARALLELISM. Levels are also not derived from `FieldGraph._layered_columns`, which is a *display*
layering: cycle-tolerant, includes the base field, and does not drop cyclic soft edges.

**`NoteRun` owns one note's semantics, once.** `engine/note_run.py` splits the old per-rule loop
into the two phases it always had implicitly: a READ phase (block gate + skip predicate, over a
frozen `MappingProxyType` snapshot of the working map) and a WRITE phase (results, `produced`,
chaining, `materialize`). Both run on the driver thread; only the tool chains are dispatched.
`finish()` sorts all three output lists back into `order_rules` order, so nothing observable
depends on the level structure, the worker count or completion order. The batch's cohort runner
drives many `NoteRun`s rather than mirroring their gates — a second implementation would drift,
and the drift would be silent.

**`materialize` — hence `add_media_file` — is called only from the driver thread.** That is what
makes the per-note materializer memo safe without a lock, and what makes a cancelled wave unable
to orphan a media file. `core/anki_compat`'s comment on this was corrected in passing: what
serialises a QueryOp's collection access is `TaskManager._collection_executor =
ThreadPoolExecutor(max_workers=1)`, not "the backend".

**Cancel means: finish the cohort you are in, start no more.** `progress_was_cancelled` reads
an app-wide flag, so it is polled once per COHORT, before that cohort starts, and latched. Notes
never started emit **no outcome at all** — an outcome with no results lands in `empty_note_ids`,
which the integration gateway feeds to `_discard_unfilled` → `col.remove_notes`, so a cancel
that let an untouched note look "empty" would delete the user's clips.

> **AMENDED after review (2026-08-21).** This originally read *"stop submitting, drain the
> in-flight wave, commit it"*, with the poll once per ROUND. A round is a dependency LEVEL, not
> a note, so that cut notes mid-walk: a 17-field note type in 5 levels came out with levels 1–2
> written and 3–5 empty, written to the collection and counted in no summary bucket — the
> tooltip said "Cancelled — Processed 3" while seven notes had been modified. Before
> concurrency a cancel could only land between whole notes, and that contract is restored by
> moving the poll to the cohort boundary. `_NoteOutcome.cancelled` is gone with it: no note can
> reach that state any more, and leaving the branch in place invited the bug back. The price is
> responsiveness — a cancel now takes up to one cohort (at most `max(workers, K)` notes, run
> concurrently) instead of up to five sequential notes.
>
> Note what changed is the SHAPE of the guarantee, not just a number. Cancel used to be polled
> every 5 notes; it is now polled once per cohort, which at the shipped defaults (1 worker,
> K = 10) is a cohort of 10 — coarser than the old 5 on the poll, finer than it on the promise,
> because no note is ever left half-written. Progress is likewise coalesced to one publish per
> 0.25 s instead of one per 5 notes, with a guaranteed final publish at the total, so the
> counter still ends exactly where it should. A run that also raises the worker count buys
> throughput with cancel latency, and that trade should be stated to anyone who does.

**The setting is `SmartNotesSettings.max_concurrent_generations`, default 1, and carries no
pydantic range.** The bound is applied where the value is used (`SmartNotesSettings.workers()`,
the single read site all three generation paths go through) and at the GUI boundary.

> **SUPERSEDED (2026-08-22): the default is now 8** — see ADR-018 for the measurement. Everything
> else in this paragraph stands: the same single read site, the same absence of a pydantic range,
> the same clamp at the GUI boundary. The two Consequences below that argue FROM the default of 1
> ("reverting is the shipped default"; "default 1 also means nobody meets this without opting
> in") no longer hold — `= 1` is still the revert, but it is now something a user has to choose.

> **AMENDED after review (2026-08-21).** The default was 3. That shipped concurrency ON for
> every existing user: someone who changed no setting got 3-thread pools and up to 4 requests in
> flight against a key that previously saw 1, and the add-on's own settings page describes 1 as
> "the old behaviour". A performance feature that raises the load on a shared provider account
> has to be opted into. The editor and review-time paths also read the raw field and were
> bounded by nothing; they go through `workers()` now, so a value from a release that raised the
> ceiling cannot make them fan out wider than the batch runner. A `ge`/`le` on a field of a blob that SYNCS turns a value from a future release
into a `ValidationError`, and `SmartNotesStore.load` has no `try` around `parse_obj` — so
`PluginManager` would swallow it into "the feature silently never enables" (ADR-010). Clamping on
load is no better: it would rewrite the other device's value on the next save. The key is also
pruned from `dict()` while it equals the default, so a user who never opens Advanced keeps
writing a blob byte-identical to today's.

> **Superseded by ADR-018.** The prune sentence above is no longer true: pruning on *equality
> with the default* meant that moving a default onto a value silently deleted the stored choice
> of every user who had deliberately set exactly that value — and ADR-018 moves two defaults.
> The prune is now on PROVENANCE (`__fields_set__`). The byte-identical-blob promise for a user
> who never opens Advanced is unchanged; only the mechanism that keeps it is.

### Consequences
- (+) Measured on the real workload shape (50 notes × 17 fields, 5 levels, 50 ms per call):
  38.2 s sequential → 13.0 s at N=3 (2.9×) → 5.1 s at N=8 (7.6×), with identical output, identical
  call counts (700) and zero lost fields.
- (+) The bound is process-wide and real, and now demonstrated rather than asserted. **One
  workload, named, and the same four numbers the FEATURE_LOG entry quotes** — they used to differ
  by ~5× because each document reported a different (unnamed) run:
  `tests/benchmarks/smart_notes_throughput.py --notes 50 --latency 0.01 --workers 8
  --output-share 0.5`, reading each run's `+L1 (N=8, …)` row.

  | run | wall (s) | 429s | limiter peak | limiter wait (s) |
  |---|---:|---:|---:|---:|
  | baseline (sequential) | 10.02 | 0 | 1 | 0.00 |
  | N=8, provider 429s above 4, no request bound | 33.88 | 91 | 8 | 0.00 |
  | N=8, same, `--request-limit 4` | 2.64 | 0 | 4 | 7.89 |
  | N=8, no rate limit, `--request-limit 3` | 3.55 | 0 | 3 | 13.09 |

  Against a provider that 429s above 4 concurrent, an unbounded N=8 takes 91 rate-limit errors
  and finishes **3.4× SLOWER than sequential** — retry absorbs every one of them, so nothing is
  lost (850/850 fields, 50/50 identical), but the backoff is the cost; bounded to 4 requests the
  same run takes zero 429s and is 12.8× faster than unbounded. The last row is the bound biting
  with no rate limit in sight: peak 3 out of 8 workers, 13.09 s of measured wait. Wall clocks are
  machine- and load-dependent; the ordering is not.
- (+) Reverting is the shipped default: `max_concurrent_generations = 1` is exactly the old
  execution (no pool is even created).
- (−) **One path escapes the limiter, and one provider needed an explicit permit.** `edge_tts`
  speaks WebSocket through `core/network/websocket.py` and never touches an `HttpClient`; rather
  than exempt the provider most likely to answer a burst by closing the connection, it now takes
  a `PROVIDER_LIMITER.permit()` around each synthesis chunk. What genuinely cannot be covered is
  **user-authored tools** in `user_files/tools/*.py`: real Python that may `import urllib`
  directly. The limiter's module docstring names both, so the hole is documented where the
  mechanism is.
- (−) **An undeclared read is now a race.** `tool_referenced_fields` contributes nothing for a
  tool this device cannot resolve, and `Tool.referenced_fields` defaults to `[]`. A user tool that
  reads a field it does not declare produces no edge, so it can share a level with its de-facto
  producer. Sequentially it happened to run in config order; now the answer can differ per run.
  The fix is to declare the dependency; there is no way to infer it.
- (−) **A user tool now runs concurrently with itself, and its side effects are its own.** The
  engine guarantees a tool's INPUTS (a frozen read-only field map per level, a fresh instance per
  resolve) and can guarantee nothing about what it writes — while the import allowlist
  deliberately permits `subprocess`, `shutil`, `tempfile` and `os`. A tool written against the
  sequential engine that names a scratch file after the FIELD rather than the note will race
  itself across notes and put one note's output in another note's field, with no error anywhere.
  Stated in `Tool`'s docstring, in `user_tools`' module docstring and as rule 8b of the authoring
  system prompt; unenforceable for a hand-edited file, where the escape hatch is
  `max_concurrent_generations = 1`. Default 1 also means nobody meets this without opting in.
- (=) **`RecordingLLMProvider` still forces `temperature = 0.7`, and that is now deliberate.**
  A revision of this change set removed the literal default so the user's configured
  per-provider temperature would finally reach the model. It is a real bug and the fix is
  correct — and it was REVERTED here, because it changes generated text for anyone who
  configured another value, and a throughput change is not where someone's model output should
  start differing. (At defaults nothing differs: the configured default is 0.7 too.) The
  constant is now named `_RECORDED_TEMPERATURE`, applied to all three text methods so the plain,
  cached and batched paths sample alike, and a test pins it with the reason. Fix it in its own
  change, with a release note.
- (−) The cohort runner synchronises on rounds, so a cohort whose notes have different level
  counts leaves some workers idle at the tail. With one note-type config per cohort they do not.
- (−) **A note that breaks partway is now partially WRITTEN.** `_apply` writes the results a
  broken note had already committed instead of discarding them, so a note whose fourth field
  raises keeps its first three, is counted failed, and syncs. Deliberate — throwing away work the
  user paid a provider for, to keep a note pristine, is the worse of the two — but it is a change
  in what the collection looks like after a failed note, and worth a release note.
- (−) **Execution order inside a note is no longer `order_rules`' order, even at one worker.**
  `generate_note` walks LEVELS and dispatches a whole level per round, so provider-call order,
  usage-record order and `materialize()` order all follow the level flattening (A, C, D, B where
  the old order was A, B, C, D). The returned triple is re-sorted, so every reported list is
  unchanged. The one case with teeth is the undeclared read above: a user tool that reads a field
  it does not name in its params can now share a level with that field's producer and read the
  pre-level value.

### Alternatives considered
- **A limiter inside `RetryPolicy`.** Rejected: it conflates the two mechanisms the brief
  separates, and it would not bound the first attempt at all.
- **Submitting whole notes to the pool** (instead of a level's fields). Rejected: `materialize`
  would then run on N threads, which is a media write per thread against one collection, and the
  per-note memo would need a lock. Levels-in-a-wave keeps every write on one thread.
- **More `QueryOp`s instead of a pool.** Rejected: Anki queues every collection-using QueryOp on
  a single-worker executor, so it would change nothing; and `.without_collection()` is not
  available to us because the op genuinely touches the collection.
- **A `[llm]` home for the setting** (`providers.toml`), which is arguably where a
  per-provider-account bound belongs. Deferred: smart_notes is today the only consumer and the
  Options UI already exists. The migration path is a read that prefers `[llm]` when present —
  worth taking the first time a second AI feature needs the same bound.

---

### LAYER 2 — prompt caching (same ADR, because it is the same interface decision)

**Context.** Every note of a note type is generated from the same prompt TEMPLATE; only the
interpolated values differ. `prompt_for` interpolates before the provider ever sees the string,
so by then the instructions and one note's values are a single blob and there is nothing for any
cache — implicit or explicit — to match on. Bounded concurrency (above) made the batch faster;
it did nothing about the fact that the same instruction head is billed 8435 times.

**Decision: a NEW METHOD on `LLMProvider`, never a new keyword on `generate_text`.**
`generate_cached_text(parts: PromptParts, …) -> (text, usage)`, whose base implementation
concatenates and delegates to `generate_text_with_usage` — today's exact call, today's exact
bytes. Two reasons a keyword was rejected outright: twenty-plus test fakes pin
`generate_text`'s signature with no `**kwargs`, so a new keyword from a shared call site
`TypeError`s every one of them; and more importantly the concatenating default makes "a provider
that cannot cache behaves exactly as today" true *by construction* rather than by everyone
remembering to make it so. `supports_prompt_cache` is a class-level declaration for reporting,
not a switch anything branches on.

**`PromptParts` is lossless, and that is the whole safety argument.** `prefix + suffix` is
byte-for-byte what `interpolate` returns. `split_prompt` cuts at the FIRST `{{ref}}`: literal
head, then the rest. **Rejected:** restructuring the prompt into "instructions with the refs left
uninterpolated" plus a values block. It would maximise the cacheable prefix, but it changes the
string the model reads — and therefore the output — for every existing user on every field, in
exchange for a latency/cost win. Not a trade worth making silently. The honest cost of the
conservative split: a template that LEADS with `{{Word}}` gets an empty prefix and no benefit.

**No smart-notes-level setting.** The split changes nothing observable, so a kill switch would be
a persisted key that does nothing — one more ADR-010 surface for no behaviour. The single change
that *is* visible on the wire has its own provider-level flag (below), and the benchmark A/Bs
L2 by constructing the fake with and without a cache, not by flipping a user setting.

**Per provider, only what that provider actually needs:**
* **Gemini (both registered names, one `_build_payload`)** — caches implicitly on a shared
  leading prefix, which is exactly what `PromptParts` produces, so it needs **no override**: it
  needs the *measurement*. `usageMetadata.cachedContentTokenCount` is reported as
  `usage["cached"]`. EXPLICIT `cachedContents` is out of scope — it needs a create POST and a
  DELETE to clean up, and `HttpClient` has no DELETE; adding one is a `core/network` change with
  blast radius across both provider kinds.
* **OpenAI-compatible (three names, one class)** — OpenAI-hosted models cache automatically with
  nothing on the wire, so again just parse `usage.prompt_tokens_details.cached_tokens`.
  Anthropic-family models via OpenRouter need an explicit `cache_control` marker, and a marker
  can only ride a content PART — which means sending `content` as an array instead of a string.
  That is gated behind `[llm.<name>].prompt_cache_control`, **default false**, because support is
  per MODEL: a model that rejects the array fails generation outright, which is far worse than
  paying full price for the prefix. With an empty prefix the marker is skipped too — marking the
  whole prompt would cache one note's values and can never hit.
* **`usage["cached"]` is reported only when non-zero**, so a provider that reports no cache at all
  returns the exact three-key dict it always did.

**The recording wrapper forwards it explicitly, and a test now enumerates the interface.**
`RecordingLLMProvider` is a hand-written decorator, and the dangerous failure is not a crash but
a BYPASS: a method left to the base default runs on the *wrapper* and delegates to the wrapper's
other methods, so the wrapped provider's own override never executes — the cache marker is
dropped and nothing errors. `TestRecordingProviderForwardsEveryPublicMethod` enumerates
`LLMProvider`'s public callables and pins, for each, which method of the wrapped provider the
call must land in, exactly once.

**`last_usage` is now documented as closed to new code.** It is per-instance mutable state and the
hub hands one cached instance to every concurrent note. Recording already reads the return value;
the class docstring now says plainly that nothing new may depend on the attribute, because
"stash the result on the provider and read it back" is the natural way to write the next feature
and is exactly the shape that produces "wrong output, no error" under Layer 1.

**Consequences (L2).**
- (+) Measured on the same 50-note workload: prompt characters are unchanged at 211,082, of which
  **184,436 (87.4%) are a repeated instruction prefix** — 490 prefix hits against 10 misses (one
  miss per AI field, then 49 hits). The prefix is the 378 characters before the benchmark
  template's first `{{ref}}`, not the 386 of the whole interpolated template; 87.4% is a property
  of THAT length (34.1% on the one-line template) and of no one's deck. Wall clock, provider calls (700) and fields written (850) are
  identical with and without the cache, and all 50 notes stay byte-identical to the baseline.
- (+) L2 composes with a future L3: the K-note envelope's stable head is the same shape.
- (−) The benchmark cannot report TOKENS. There is no tokenizer in the fake, so it counts
  characters (exact) and offers chars/4 as a labelled estimate. Real token savings depend on the
  provider's cache TTL and minimum-cacheable size, neither of which a fake can model honestly.
- (−) The repeated-prefix figure is an OPTIMISTIC bound: the ledger has no TTL and no minimum
  prefix length, so it measures what a cache *could* serve, not what one *did*.
- (−) A prompt that starts with a `{{ref}}` gains nothing. Surfacing that as a hint in the field
  editor ("prompts that start with instructions cache better") is a follow-up, not part of this.
- (−) Only the smart-notes TEXT rule path is split. The authoring/tool-authoring paths and the
  language detector still call `generate_text`: their prompts are one-off, not per-note
  templates, so there is no repeated prefix to cache and no reason to touch them.

---

## ADR-017: A K-note batch matches by opaque id, or it falls back per note

**Date**: 2026-08-21
**Status**: Accepted

### Context
LAYER 1 (ADR-016) removed the idle time between a note's round trips, but it did not change how
many round trips there are. On the measured note type, 10 of the 17 enabled fields are pure-AI
text sharing ONE provider/model pair, and every note of the type is generated from the same
prompt TEMPLATE — only the interpolated values differ. Fifty notes therefore spend 500 separate
completions asking the same ten questions with fifty different words in them.

The only clean axis for merging those is **same field, many notes**. Those calls share a
provider, a model and a template by construction, and they sit in the same dependency level, so
grouping them re-orders nothing. Batching across FIELDS is never an option: it would mix
provider/model pairs and dependency levels in one request.

Which leaves one question, and it is not "how do we merge them" — it is **how does a merged
answer get back onto the right note**. Send K items, get K-1 back, pair the answers with the
notes by POSITION, and every note after the gap silently receives a different note's content,
which is then written to the collection. No exception, no counter, nothing in the log. This
add-on has just spent a release removing a "no output, no error" bug; "wrong output, no error"
is strictly worse, because the user cannot even see that something is missing.

### Decision

**Match by an explicit id, never by position, and fall back individually for anything unmatched.**
Every item in a chunk carries a fresh `secrets.token_hex(3)` generated per chunk. The parser
reads the response array's index for iteration and for nothing else; `match_items` maps content
onto notes purely through the id map it was handed. An id that is unknown, repeated, or attached
to a non-string/blank content is DISCARDED, and its note goes to an individual call. The failure
mode is therefore always "extra calls", never "wrong content".

> **CORRECTED after review (2026-08-21).** The paragraph above stated the rule; the code did not
> implement it. `match_items` skipped a duplicated id only from its SECOND occurrence, applying
> the first — so a chunk whose model had lost the id↔item correspondence wrote one note's content
> onto another, silently, and the note that was actually answered fell back to a correct solo
> call and made the run look clean. Reproduced end to end. A repeated id now discards EVERY copy
> of itself (a `Counter` pre-pass): the first copy is not "the good one", it is the one that
> arrived first.
>
> A second shape was added, because the id discipline provably cannot see it. A **collapsed
> answer** — one `content` string returned for two or more items whose own interpolated values
> DIFFER — is the commonest failure in K-item batching, and `collapsed_indexes` discards every
> item in such a group. Items with identical INPUTS are excluded, or a deck with duplicate words
> would fan out forever. This was previously dismissed in the Consequences below as
> undetectable; that was wrong for the degenerate case, which is the case that actually happens.
>
> **AMENDED again after a second review (2026-08-21): the question is asked per GROUP, not per
> chunk.** The first version asked "did EVERY matched item come back with the same string?",
> which only fires on a totally collapsed reply. A model that answers items 1-4 with item 1's
> text and gives item 5 its own answer defeats it completely: two distinct strings in the chunk,
> every id present, unique and correct, nothing discarded — and three notes are written a fourth
> note's content, committed, with `field_failures = 0` and a clean summary. Verified end to end
> before the fix. Per group, full collapse is simply the case where the one group is the whole
> chunk, and it still lands on the same "arrived but nothing could be routed" rung; a partial
> collapse now costs the affected notes their own individual calls and leaves the items the model
> really did answer alone. Which of a collapsed group was the one being answered is not knowable,
> so the whole group goes. The cost is extra calls on a legitimately low-cardinality field ("part
> of speech" honestly answering "noun" for half the deck) — the trade this module makes
> everywhere: never wrong content, sometimes more calls.

The id is deliberately not the note id (a 13-digit epoch a model may reformat or truncate, and a
stable identifier we would be leaking into a prompt) and deliberately not an ordinal (`n0..n9`
invites renumbering, and a hallucinated ordinal looks entirely plausible). An opaque token that
does not come back is unambiguously a miss.

**Eligibility is narrow, and computed as a full key.** The chunk key is
`(note type, field, provider, model, template)`. Within a cohort and a round the first and last
are already fixed, so provider/model could not differ either — the full tuple is computed anyway
so a future per-note provider override cannot silently merge two different models into one call.
It is the same `(provider, model)` pair `ProviderHub.llm` caches on, which guarantees one chunk
maps to exactly one provider instance. A rule is ineligible — and runs alone — when it is not
`text` (image and tts return BYTES: one synthesis per note), when its chain is not the lone
parameter-less `ai` tool (a deterministic or user tool may make no provider call at all, and a
chain has fallbacks one merged request cannot express), or when it has no template (its prompt IS
one field's value, so there is no shared instruction to amortise). A group that comes out one
note wide is a solo call, not a one-item chunk: the envelope would cost tokens and add a parse
step that can only lose.

**"Only notes that actually need the field" is free.** The block gate, the skip predicate and the
overwrite rule all ran on the driver thread in `NoteRun.next_dispatch()` before the wave was
built (ADR-016), so a note that was going to be skipped is not in the wave to be chunked.

**The envelope quotes the user's template verbatim and uninterpolated.** Nothing rewrites what
the user wrote. Each item carries only the values its own template's `{{refs}}` name — a smaller
payload, and less of another note's material in front of the model. The envelope's head is
identical for every chunk of a field, which is exactly the shape LAYER 2 caches, so L2 and L3
compose rather than compete.

**The fallback ladder**, in order:

| situation | action |
|---|---|
| every id matched | done — one call for K notes |
| some ids matched | apply those; the rest fall back to individual calls |
| an answer arrived but nothing routed | halve the chunk ONCE, then individual calls |
| collapsed, or every id duplicated | same rung: nothing was usable |
| provider error, not 429 | straight to individual calls |
| provider error, 429 | **no fan-out** — every note in the chunk gets a field `error` |

Two rungs need their reasoning recorded. **Halving is once, never recursive** — unbounded
halving turns one broken provider into a 2K-call storm — and it applies only when an answer
actually ARRIVED and could not be routed. A call that never produced an answer is a provider
problem, and half of a failing request is still a failing request, so that goes straight to
individual calls where `RetryPolicy` and per-field isolation apply. **A 429 does not fan out**,
because K individual retries inside a rate-limit window amplify precisely the thing that limited
us; and the resulting `FailedField(kind="error")` is what keeps those notes out of
`empty_note_ids`, whose consumer *deletes* clipped notes. A chunk failure miscategorised as
`"unproductive"` would turn a provider outage into deleted captures.

**K adapts within the run, and is not persisted.** `FieldBudget` starts from a pessimistic
512-token-per-item estimate against an 8192-token output cap and refines it per
`(note type, field)` from the longest answer observed. The estimate only ever GROWS, so K only
ever shrinks: a heuristic that could grow K back on one short answer would oscillate for the rest
of a long batch. A field whose chunk ever had to halve keeps the smaller size for the rest of the
run. None of it is written to config — a persisted heuristic is drift with no UI to explain or
reset it.

**JSON mode is an optimisation the contract never depends on.** `LLMProvider.generate_json(parts,
schema=…)` defaults to an ordinary (still prefix-cacheable) text call. Gemini overrides it with
`responseMimeType` + `responseSchema` (one override, both registered names — Vertex inherits the
wire format); the OpenAI family sends `response_format`, gated behind a new
`[llm.<name>].json_output`, default false, because support there is per MODEL and a model that
rejects the envelope fails the call. Every caller parses defensively regardless, because even a
native JSON mode can return a refusal or a near-miss. `RecordingLLMProvider` forwards the new
method explicitly, and `TestRecordingProviderForwardsEveryPublicMethod` is what caught it: a
method left to the base default runs on the *wrapper*, so the wrapped provider's override is
never entered and the schema is silently dropped.

**The env knob is K, and it decides.** `envs.OMNIA_SMART_NOTES_BATCHING` is an INT:
`-1` (or anything below 1) is off, and any value `>= 1` is the ceiling the synced
`SmartNotesSettings.batch_notes_per_call` is clamped to. `SmartNotesSettings.notes_per_call()` —
the single place either knob is read — returns `min(stored, env, MAX_NOTES_PER_CALL)`, or 1 when
the env says off. The stored number is never rewritten, so a machine can force a collection's K
down (including to off) without that decision syncing to every other device.

> **AMENDED TWICE (2026-08-21).** First the synced setting alone was the switch; then a bool env
> flag was added in front of it, default off. It is now one int, default **10**, and the feature
> ships ON. Two things changed the answer. (a) `max_concurrent_generations` also defaults to 1,
> and at one worker a chunk has no parallelism to serialise — the reason batching loses wall
> clock at N=8 simply does not exist at N=1, so the −64% request saving is free there. (b) The
> quality residual that justified "off by default" is bounded by K, and K is exactly what this
> knob now sets, so the same decision is expressible without a second key. **10** is not a taste:
> the measured deck's binding field is "Synonyms (explained)" at ~385 output tokens p95 and ~677
> at its longest, a chunk asks for K answers inside one completion, and 8192/677 ≈ 12 — 10 stays
> under the cap even when every answer in the chunk is the longest ever seen.

`SmartNotesSettings.batch_notes_per_call = 10` is the width the user asks for, one key rather
than a bool plus an int, because "off" is exactly what K = 1 expresses and every extra persisted
key is another ADR-010 surface. Like
`max_concurrent_generations` it carries no `ge`/`le` (a value from a release that raised the
ceiling must degrade, not raise a `ValidationError` that `PluginManager` swallows into "the
feature never enables"), is clamped where it is used, and is pruned from the serialized blob
while it holds its default so a user who never opens Advanced keeps writing byte-identical
config. Off is the pre-batching CODE PATH, not the batching code path at width one:
`batch_planner(notes_per_call=1)` returns `SOLO_PLANNER`, which knows nothing about envelopes,
ids or parsing.

> **SUPERSEDED IN PART (2026-08-22) — see ADR-018.** Everything above about the id discipline,
> the fallback ladder, eligibility and the env knob's SHAPE still holds. Two things in this ADR
> do not, and both are stated here so a reader who stops at ADR-017 is not misled:
>
> 1. **The Consequences below say batching is a request-count optimisation that costs wall clock
>    above one worker, and that raising the worker count should lower K.** That came from
>    `smart_notes_throughput.py` charging a chunk per OUTPUT ITEM, which makes K answers in one
>    call cost exactly what K calls cost — grouping could not have measured any other way. It was
>    an artefact of the rig. It was then briefly replaced by the opposite claim (grouping is
>    faster everywhere, from one live session at K = 20); that did not reproduce either. **The
>    effect of K on wall clock is UNPROVEN in both directions** and no default may rest on it.
>    What IS measured, and reproduces, is the request saving: 1300 → 794.5 at K = 10 and → 574.5 at
>    K = 20, at 8 workers over 100 real notes.
> 2. **`max_concurrent_generations` no longer ships at 1; it ships at 8**, on evidence that does
>    reproduce (see ADR-018). So the sentence "shipping K = 10 ON is coherent only because
>    `max_concurrent_generations` ships at 1" no longer describes the build. K = 10 stays for the
>    reason that never depended on the timing study at all — the output budget quoted in the
>    amendment above, 8192/677 ≈ 12.
>
> The prune sentence above ("pruned from the serialized blob while it holds its default") is also
> superseded: the prune is now on PROVENANCE, not on equality with the default. See ADR-018.

### Consequences

- (+) **The robust win is the CALL COUNT: 700 → 250** on the 50-note / 17-field workload, i.e.
  64% fewer provider requests, with 850/850 fields written and 50/50 notes byte-identical to the
  baseline. That number does not depend on any latency model.
- (−) **The wall-clock win largely evaporates once output size is modelled, and at N=8 it is a
  LOSS.** The original figure (13.77 s → 5.33 s, 2.6×) came from a fake that charged per HTTP
  REQUEST with no dependence on how much text the request asked for — so a K=10 chunk slept
  exactly as long as one completion, and the wall-clock column was a restatement of the call
  count. Re-measured with latency as *fixed round trip + per-output-item* (a solo call still
  costs the same at every setting, so the rows compare):

  | share of a call spent generating | N=3, L1+L2 | N=3, +L3 (K=10) | N=8, L1+L2 | N=8, +L3 (K=10) |
  |---|---:|---:|---:|---:|
  | 0% — output free (the old model) | 13.79 s | 5.31 s (2.6×) | 5.38 s | 2.74 s (2.0×) |
  | 50% — half fixed, half generated | 13.79 s | 10.34 s (1.33×) | 5.38 s | 7.20 s (**0.75×**) |
  | 100% — output-dominated | 13.79 s | 15.94 s (**0.87×**) | 5.38 s | 12.82 s (**0.42×**) |

  The mechanism is plain in hindsight: a chunk serialises K answers' worth of generation into one
  worker, and a pool that wide would have generated them in parallel. **Batching is a request-count
  optimisation, not a speed one.** It is worth turning on to stay under a rate limit or to cut
  per-request overhead — not to make a batch finish sooner, and at N=8 it will make it finish
  later. Reproduce with `smart_notes_throughput.py --output-share 0 0.5 1`.

  > **RETRACTED (2026-08-22), see ADR-018.** The table above is the FAKE rig's, and its latency
  > model is an assumption rather than a calibration: `--output-share` says what fraction of a
  > call is spent generating, nothing in this repo measures that fraction for any real model, and
  > at `output-share 0` the same rig has batching winning everywhere. Charging a chunk per output
  > item makes K answers in one call cost exactly what K calls cost — so this table could only
  > ever have concluded what it concluded. **"Batching is a request-count optimisation, not a
  > speed one" was never established, and neither is its opposite.** Two live sessions against
  > the real provider disagreed with each other; the rows are in `tests/benchmarks/data/`. Treat
  > K's effect on wall clock as unmeasured. The REQUEST saving in the bullet above this one is
  > untouched by any of it — that number depends on no latency model, which is exactly why it is
  > the one the shipped comments and the UI are allowed to quote.

  Two things follow, and the defaults encode both. Shipping K = 10 ON is coherent only because
  `max_concurrent_generations` ships at **1**, where there is no parallelism for a chunk to
  destroy and the −64% is free; **raising the worker count should lower K**, and the Advanced
  pane says so. The conclusion itself is conditional on the output share: at s = 0 batching wins
  everywhere, and it is at s ≥ ~0.3 that it stops. Nothing in this repo measures the real
  TTFT-vs-output split for any provider, so 0.5 is an assumption chosen as the middle of a
  defensible range, not a calibration — the table prints all three so the reader can pick.

  > **RETRACTED with the table (2026-08-22), see ADR-018.** `max_concurrent_generations` ships at
  > **8**, and "raising the worker count should lower K" is advice derived from the retracted
  > latency model — the Advanced pane no longer says it, because nothing measured supports it.
  > K = 10 ships for the output-budget reason alone.
- (−) **LAYER 3 can INCREASE the input tokens sent.** On the long template the envelope amortises
  (211,082 → 92,442 prompt chars); on a one-line template the envelope's own boilerplate is
  larger than what it replaces and the total goes UP (33,664 → 73,424). It also drops the
  prefix-cache hit count from 490 to 40, because one chunk replaces K cacheable calls. A short
  template is a bad candidate for batching on every axis.
- (+) The correctness property is measured, not asserted. The benchmark's `--corrupt` modes make
  every batched response hostile; all five keep 850 fields and 50/50 identical notes, at a cost of
  roughly 300 calls (`drop-one`, `duplicate-id`), ~840 (`collapse`) or ~940 (`renumber`,
  `truncate` — i.e. **worse than the 700-call baseline**). A provider that always answers badly
  makes batching a net loss, and the numbers say so rather than hiding it. The COUNTS are
  load-dependent and stated as approximate on purpose: which notes share a wave depends on
  thread timing, so `renumber` measured 940 on most runs and 890 under light instrumentation.
  What is exact in every mode, and is the actual claim, is 850/850 fields and 50/50 identical
  notes.
- (−) **Context bleed is real and is not fixed here.** A model given ten items at once can let
  one item's subject matter drift into another's answer. That is a QUALITY risk no parsing
  discipline can detect, let alone repair — its degenerate cases (one answer returned for several
  items with different inputs, whether that is the whole chunk or part of it) are caught by
  `collapsed_indexes`, but the ordinary case, a slow drift in wording or subject matter, is not
  and cannot be. It is addressed only in the prompt (an explicit isolation instruction;
  each item carrying nothing but the values its own template names) and mitigated by a smaller K.
- (−) **Batching widens one poisoned note's blast radius from 1 to K.** A note's content is
  interpolated into a request shared with K−1 others, so instruction-shaped text inside one field
  — and smart-notes' first-class input path is a web CLIPPER, i.e. text a stranger wrote — is read
  by the model as part of the same conversation answering for its neighbours. `json.dumps`
  escaping protects the envelope's structure and cannot stop a model reading what it is shown.
  Solo generation contains such a note to itself. This, with context bleed, is why K is bounded
  and why one environment variable can switch the feature off everywhere without a sync.
- (−) **A batched chunk sends an explicit `max_tokens`; the solo path sends none.** Deliberate,
  and the two are different requests: a solo call produces one answer and the provider's own
  ceiling bounds it sensibly, while a chunk produces K answers inside ONE completion, where that
  ceiling is shared between them and the token that runs out cuts the last item in half. The cap
  is `per-item estimate × K` (`FieldBudget.tokens_for`), so truncation becomes a stated contract
  rather than a property of a vendor default — the same field truncates at every K or at none.
  An unusable reply both halves K and DOUBLES the per-item estimate, because with a proportional
  cap, half the items asking for half the tokens would retry with exactly the room that just
  failed.
- (−) The Account dialog's `calls` column drops by roughly K when batching is on: `usage` records
  one call per request, and K notes are now one request. Token counts stay whole. This is a
  deliberate reporting change, not a bug — but it means "calls" and "notes generated" stop being
  the same number.
- (−) A per-note fallback runs inside the worker that owned the chunk, so K fallbacks are
  sequential. Fanning them into the pool would mean a worker submitting to the pool it is running
  in, which ADR-016 forbids outright. The cost shows up as wall clock in the `--corrupt` rows.
- (−) Gemini's JSON mode is provider-level, not per model. A model too old for `responseSchema`
  answers 400, which the ladder handles as an ordinary chunk failure — correct, but it means one
  wasted call per chunk for the whole run. The OpenAI family avoids this by being opt-in.
- (−) `FieldBudget` is the first piece of engine state written from a dispatch worker, and it
  takes a `threading.Lock` for two dict operations. `engine/` stays free of `aqt`/`anki` and of
  `concurrent.futures`, but it is no longer free of `threading`.

---

## ADR-018: A generation default ships on evidence that reproduced — the worker count did, K did not

**Date**: 2026-08-22
**Status**: Accepted (supersedes parts of ADR-016 and ADR-017)

### Context

ADR-016 shipped bounded concurrency with `max_concurrent_generations = 1`, and ADR-017 shipped
K-note batching with `batch_notes_per_call = 10`. Both defaults were chosen from
`tests/benchmarks/smart_notes_throughput.py`, a fake-provider rig whose latency model is a
parameter (`--output-share`) rather than a measurement. Its central batching conclusion —
"batching is a request-count optimisation, not a speed one, and at 8 workers it makes a run
finish later" — is an artefact of charging a chunk per OUTPUT ITEM: under that model K answers
inside one call cost exactly what K separate calls cost, so grouping cannot win. Set the same
knob to 0 and the same rig has grouping winning everywhere. Nothing in this repo measures the
real fixed-overhead-vs-generation split for any model, so neither setting of that knob is the
provider.

So the defaults were built on an assumption, and the assumption had been quoted forward into a
shipped source comment, an ADR, a FEATURE_LOG entry and a user-facing tooltip that told people
grouping was "measurably SLOWER".

A live benchmark was then written (`tests/benchmarks/smart_notes_live.py`): real notes from a
read-only copy of a real collection, the user's own settings and prompts, the real Vertex
endpoint, every arm run twice. Its first session (100 notes, six arms, 4 h 14 m of measured arm
time) produced the opposite conclusion — grouping faster at every worker count — and that
conclusion was, briefly, shipped: `batch_notes_per_call` moved 10 → 20 and the tooltip was
rewritten to sell the time saving.

**That did not reproduce.** A second session, same harness, same collection, same settings, same
account, at 8 workers: ungrouped 213.9 s (206.3–221.5), K = 20 215.2 s (175.0–255.4 — a tie),
K = 10 476.5 s (435.4–517.6 — 2.2x SLOWER). Two sessions, opposite answers, each arm's own
run-to-run spread as wide as the gap between arms. Two samples of a network-bound arm is not a
measurement of it.

### Decision

**A shipped default may cite only a result that reproduced across independent sessions. Anything
else is recorded as unproven, in those words, everywhere it is quoted.**

Applied to the three knobs:

- **`max_concurrent_generations = 8`** (was 1). The one comparison that reproduces. At the same
  K, in both sessions, the 4-worker and 8-worker ranges do not overlap: 1851.8–1958.4 s against
  1210.4–1297.8 s over 100 notes, and 370.9–393.7 s against 206.3–221.5 s over 20. Not 16, and
  the three reasons are ranked by what kind of thing they are: (1) JUDGMENT — the zero in the
  429 column is one Vertex project's quota, not a property of the world, and the widest thing
  that worked on a generous key is a rate-limit bill on a free-tier one; (2) 16 is `MAX_WORKERS`,
  so shipping it leaves the Advanced control able only to go down; (3) THIN, n = 1 — the only
  arm that lost a field was 16x10, to an `edge_tts` WebSocket timeout in one of its two runs,
  which is a flake on Microsoft's keyless TTS endpoint rather than on the LLM this knob bounds.
  16 was also only ever run at K = 10, whose cohort (`max(16, 10) = 16`) splits 10 + 6, so it was
  never measured at a K that divides its cohort cleanly.
- **`batch_notes_per_call` / `OMNIA_SMART_NOTES_BATCHING` = 10** — reverted from the 20 the
  non-reproducing session justified. 10 stands on the OUTPUT BUDGET, which is independent of any
  timing study: a chunk asks for K answers inside one completion, the measured deck's binding
  field runs ~677 output tokens at its longest, and 8192/677 ≈ 12.
- **`OMNIA_MAX_CONCURRENT_REQUESTS = 0`**, unchanged. Twelve real runs at 4, 8 and 16 workers
  returned zero 429s; the limiter's peak equalled the pool width in every arm and its total wait
  was 0.0 s. There is no measured load at which it needs to bind below the pool.

**What batching buys is REQUESTS, and that half does reproduce**: 1300 provider calls ungrouped,
794.5 at K = 10 (−39%) and 574.5 at K = 20 (−56%), at 8 workers over 100 notes; the second session's
20-note runs came out at −41% and −59%. It is the only batching number the shipped comments and the UI may quote.

**The evidence is committed.** `tests/benchmarks/smart_notes_live.py` and the raw rows of all
three sessions live in `tests/benchmarks/data/`, with a README stating what each session does and
does not establish. A default whose evidence exists only in one machine's scratch directory
cannot be audited, re-derived, or re-checked when the next model changes the answer — and four
tracked files were, for a while, citing exactly that.

**An instrument reports its own sensitivity next to its number.** The bleed column is a headword
scan: it fires only when the bleeding text restates the other note's headword. Against a
constructed neighbour swap on the measured deck it catches ~42% of deliberate mis-attributions —
~100% on `Synonyms (explained)`, `Word (family)`, `Example 1`, `Phrasal Verb`, but 12% on
`Definition` and `Antonyms` and 0–2% on `Meaning (vi)`, part of speech and IPA. The harness now
computes that recall from the collection itself, for free, before the first arm, and prints it
beside every bleed number; the tooltip says the check is blunt. "No bleed detected by an
instrument that catches two in five" is a different sentence from "no bleed", and only the first
one is true.

**A column says what it cannot see.** Roughly a third of each run's provider calls are `edge_tts`,
which speaks a WebSocket and never enters the HTTP client, so the 429/retry columns describe the
urllib providers only — and throttling there arrives as a socket timeout no classifier can
recognise. The gap is now its own printed column (`not HTTP-metered`) rather than something a
reader must derive. Token counts (in / out / prompt-cached) are captured from the usage the
provider already returns, so a defaults change on a metered API stops using call counts as a
proxy for cost. Mean answer length is captured too: `fields_filled` scores a half-length answer
as a success, and batched answers measured ~20% shorter than solo ones at both K.

**Not writing to the user's collection is enforced, not incidental.** The harness's safety used
to rest on `aqt` being absent from the dev venv — run it where `aqt` imports with a profile open
and an unpatched seam writes to the real collection with nothing to notice. `WriteGuard` now
blocks the `aqt`/`anki` imports outright for the duration of a run and installs recording raisers
over every public `anki_compat` seam, with `InertCollection`'s inert versions layered on top
during an arm; reaching a mutating seam fails the run, and reaching any other is reported.

**The prune that hides a default is on PROVENANCE, not on equality.**
`SmartNotesSettings.dict` omits `max_concurrent_generations` / `batch_notes_per_call` while
nobody has ever SET them (`__fields_set__`), instead of while they equal the current default.
Equality was safe only while these defaults could not move; the moment one moved to 8, an
equality prune deleted the stored 8 of every user who had deliberately chosen it, leaving a blob
indistinguishable from one belonging to a user who never opened Advanced — so another device on a
build with a different default silently ran something else, and the value could not be pinned at
all (only 7 or 9 survived a save). The GUI save controller cooperates: it names these keys in its
`copy(update=…)` only when the posted value DIFFERS from the stored one, so opening the dialog
and pressing Save on an untouched Advanced pane still writes nothing and the ADR-010 promise
stays attached to "the user never touched it".

### Consequences

- (+) **The one number the study settled ships**: 4 → 8 workers, from ranges that do not overlap
  in two independent sessions. On the measured deck that is 1905.1 s → 1254.1 s for a hundred
  notes at K = 1, and the pre-concurrency default itself (1 worker, K = 10 — a pairing absent
  from the 100-note table) measured 167.1 s for ten notes against 8x1's 110.1 s and 8x20's
  100.0 s in a session of its own.
- (+) **Two opposite overstatements are retired at once.** The fake rig's "batching is slower" and
  the first live session's "batching is faster" are both marked unproven in `envs.py`,
  `config.py`, ADR-016, ADR-017, `FEATURE_LOG.md`, the Advanced tooltip and the tests that pin
  it. The pane's test pins the half that reproduced (the request figure), pins the sentence
  saying the time effect is unsettled, and names each retired claim so it cannot come back
  by that wording. It does NOT ban the words "sooner" and "slower" outright: the tooltip
  needs them to report honestly that two sessions disagreed, and a ban would forbid the
  true sentence along with the false ones.
- (−) **The K question is left open, and the cost of settling it is real.** Answering it needs
  more repeats per arm than two, ideally across sessions and hours, on an endpoint whose
  time-to-first-token is not stable within a session — several more hours of paid Vertex spend.
  Until someone spends it, K is chosen by the output budget, which is a bound rather than an
  optimum.
- (−) **8 workers is a load increase for every existing user**, including one who never opens
  Advanced: the same collection now opens up to 8 connections against their provider account
  where it opened 1. The 429 column that says this is safe belongs to one generous Vertex
  project. `max_concurrent_generations = 1` remains the exact revert, and is now something the
  user has to choose rather than what they already had.
- (−) **The blast radius of a poisoned note is still K, and the instrument for it is weak.**
  ADR-017's context-bleed residual is unchanged; what changed is that the number reported against
  it now carries its own recall. A batching bug that mis-attributes one to five text fields per
  hundred would still read as noise in this table.
- (−) **A user who wants to pin the shipped default from a blob that never carried the key cannot
  do it in one step.** The controller writes only a CHANGE, so picking 8 when 8 is already the
  effective value is a no-op; setting 7 and then 8 stores it. Deliberate — the alternative is
  every dialog save writing two keys a pre-ADR-010 device rejects with a crash on every note-add
  hook — and the residual is one dialog visit rather than a lost setting.
- (=) **`smart_notes_throughput.py` keeps its job and loses one column's authority.** Call counts,
  prompt-cache hits, field identity and the hostile-provider ladder are what it proves; its
  wall-clock column may not decide a batching default. Its docstring now says so.
