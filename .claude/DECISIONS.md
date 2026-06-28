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
The repository was scaffolded from `vio-ai`, a Flask/Celery/Redis/Supabase backend. Omnia
is an Anki add-on: it runs inside the user's Anki (bundled Python + PyQt6) on macOS and
Windows, distributed as a folder/`.ankiaddon` with vendored dependencies.

### Decision
Strip all server infrastructure (Flask, Celery, Redis, Supabase, Manim, Docling, the
multi-service docker-compose stack, the "run inside the vio-ai-dev container" rule). The
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

## ADR-004: LLM/TTS provider abstraction adapted from vio-ai, vendored & pure-Python

**Date**: 2026-06-28
**Status**: Accepted

### Context
smart-notes generates text/images (LLM) and voice (TTS). The user has no OpenAI/Google keys
but has the provider configurations used in `vio-ai`. vio-ai's provider design
(`LLMProvider`/`BaseTTSProvider` + factory + concrete providers) is clean, but its code
depends on `pydantic-settings` and a server settings system, and the SDKs (`google-genai`,
`openai`) are heavy and may carry binary deps unsuitable for a vendored cross-platform
add-on.

### Decision
Adapt vio-ai's **interface and provider-selection design** rather than import its code.
Define `LLMProvider`/`TTSProvider` base classes + factories in `core/providers/`. Implement
providers with lightweight HTTP (stdlib / a small vendored client) against the providers'
REST APIs, plus key-free TTS options (e.g. edge-tts/gTTS-style) so the add-on works without
paid keys. Provider keys/config live in the config store, never in code or env at import.

### Rationale
This gives the same "add a provider = one subclass" extensibility the user wants, keeps the
add-on light and cross-platform (no heavy/binary SDK vendoring), and lets smart-notes work
with free providers out of the box while still supporting the vio-ai-style configured ones.

### Consequences
- (+) Easy to add providers; features depend only on the interface.
- (+) No paid key required to try the feature (free TTS path).
- (−) We maintain thin HTTP clients instead of leaning on vendor SDKs; API drift is on us.

### Alternatives considered
- **Vendor the full `google-genai`/`openai` SDKs**: rejected for size and binary-wheel /
  transitive-dep risk inside Anki's Python.
- **Import vio-ai directly**: impossible at the user's machine — vio-ai isn't installed
  inside Anki and pulls in `pydantic-settings`/server deps.

---

*(Add new ADRs below this line)*
