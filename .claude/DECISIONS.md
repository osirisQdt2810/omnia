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

