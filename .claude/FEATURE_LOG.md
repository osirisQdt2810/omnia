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

## 2026-08-22 — Generation defaults from a live benchmark: 8 workers, K stays 10

**What:** `max_concurrent_generations` ships at **8** instead of 1;
`OMNIA_SMART_NOTES_BATCHING` / `batch_notes_per_call` stay at **10** (they were briefly moved to
20 and moved back); `OMNIA_MAX_CONCURRENT_REQUESTS` stays 0. A live benchmark
(`tests/benchmarks/smart_notes_live.py`) and the raw rows of all three measurement sessions
(`tests/benchmarks/data/`) are now part of the repo, so a default can be re-derived rather than
taken on trust. The prune that hides these two keys from the synced blob is now on PROVENANCE
(`__fields_set__`) rather than on equality with the current default. See ADR-018.

**Why:** the previous defaults came from `smart_notes_throughput.py`, a fake rig whose latency
model is a parameter rather than a measurement — it charges a chunk per OUTPUT ITEM, so K answers
in one call cost exactly what K calls cost and grouping could only ever measure slower. That
artefact had been quoted forward into a source comment, an ADR, this file and a user-facing
tooltip that told people grouping was "measurably SLOWER". A live session then produced the
opposite result and it, too, was briefly shipped — and **it did not reproduce**.

**What reproduced, and what did not.** Both sessions ran the same harness against the same
collection with the same settings and the same account.

| comparison | 100-note session | 20-note session | verdict |
|---|---|---|---|
| 4 → 8 workers, K = 1 | 1851.8–1958.4 s → 1210.4–1297.8 s | 370.9–393.7 s → 206.3–221.5 s | **established** (ranges do not overlap, twice) |
| requests, 8 workers | 1300 → 794.5 (K=10, −39%) → 574.5 (K=20, −56%) | 260 → 152.5 (−41%) → 106 (−59%) | **established** |
| K on wall clock, 8 workers | 8x1 1254.1, 8x10 1162.5, 8x20 1049.5 | 8x1 213.9, 8x10 476.5 (2.2x slower), 8x20 215.2 (tie) | **UNPROVEN — do not claim faster OR slower** |

Within-arm spread was as wide as the between-arm gap (8x20 varied 175.0–255.4 s in the second
session). Two samples of a network-bound arm is not a measurement of it.

**So K = 10 ships for the reason that never depended on the timing study**: a chunk asks for K
answers inside one completion, the measured deck's binding field runs ~677 output tokens at its
longest, and 8192/677 ≈ 12. And **8 workers, not 16** — the 429 column's zero is one generous
Vertex project's quota rather than a property of the world (judgment), 16 is `MAX_WORKERS`
(principle), and the one field 16x10 lost was an `edge_tts` WebSocket timeout in one of its two
runs (n = 1, and on a different provider from the one this knob bounds).

**Files:** `src/omnia/envs.py`, `src/omnia/plugins/smart_notes/config.py` (both defaults, the
prune, the rationale comments), `src/omnia/gui/smart_notes/dialogs/controllers/config.py`
(`_performance_updates` — a save records only a CHANGE), `src/omnia/gui/smart_notes/web/page.html`
(the Advanced tooltip), `src/omnia/gui/smart_notes/web/05-handlers.js` (the four fallbacks),
`engine/{batching,service}.py` docstrings; new `tests/benchmarks/data/` (three sessions' rows +
README), `tests/benchmarks/smart_notes_live.py` (`WriteGuard`, bleed-recall calibration, token and
answer-length columns, the not-HTTP-metered column); `tests/benchmarks/smart_notes_throughput.py`
(a warning that its wall-clock column cannot decide a batching default); tests in
`tests/gui/{test_smart_notes_html,test_smart_notes_dialog_deps}.py` and
`tests/plugins/smart_notes/{test_batching,test_store}.py`.

**How to verify:**
`.venv/bin/python -m pytest tests/ -q -m "not llm and not tts and not integration"` →
`2291 passed, 18 skipped, 105 deselected`. Re-derive any quoted table from the committed rows:

```bash
python3 - <<'EOF'
import json, statistics
rows = json.load(open("tests/benchmarks/data/live_100notes_2026-08-22.json"))
by_arm = {}
for r in rows:
    by_arm.setdefault(r["label"], []).append(r["seconds"])
for arm, secs in by_arm.items():
    print(f"{arm:>6} {statistics.mean(secs):8.1f} s  ({min(secs):.1f}-{max(secs):.1f})")
EOF
```

**Numbers this entry deliberately does not repeat**, because they were wrong the first time and
the corrected values are the only ones that reconcile with the committed rows: the twelve
100-note runs contain **four** retries (one each in 4x1 r1, 4x10 r1, 8x20 r2, 16x10 r1), all
network errors and no 429s; their arm time sums to **15,241.8 s = 4 h 14 m**, not 3 h 07 m; fill
was 1700 or 1701 in eleven runs and 1698 in 16x10 r1, so "1700 everywhere else" was not what the
table showed.

**Notes / rollback:** `max_concurrent_generations = 1` is the exact revert to pre-concurrency
execution (no pool is created), and `OMNIA_SMART_NOTES_BATCHING=-1` still switches grouping off
machine-wide. Residuals, all in ADR-018: 8 workers is a **load increase for every existing user**
against a provider account whose 429 behaviour was measured on one generous key; the **bleed
metric catches ~42%** of a constructed neighbour swap and is near-blind (0–12%) on `Definition`,
`Antonyms`, `Meaning (vi)`, part of speech and IPA, so a flat bleed column means "not detected";
about a **third of provider calls (`edge_tts`, WebSocket) never reach the 429 instrument**, so
"zero 429s" is a statement about the HTTP providers only; **batched answers ran ~20% shorter**
than solo ones at both K, which `fields_filled` scores as a success; and **pinning the shipped
default from a blob that never carried the key takes two saves** (set another value, then set it
back) because the controller records only a change.

## 2026-08-21 — K-note batching for smart-notes text fields (LAYER 3)

**What:** A batch run can ask for the SAME field on several notes in one provider call.
`engine/batching.py` groups a wave's eligible fields by
`(note type, field, provider, model, template)`, sends them as one request whose envelope quotes
the user's template verbatim, and routes each answer back **by an explicit opaque id** — never by
position. Anything unmatched falls back to that note's ordinary individual call.
`LLMProvider.generate_json(parts, schema=…)` is the optional half: Gemini enforces the shape
natively, the OpenAI family behind a new `[llm.<name>].json_output`, and every other provider
makes an ordinary text call. **`OMNIA_SMART_NOTES_BATCHING` is K and decides**: `-1` is off (the
pre-LAYER-3 code path), any value `>= 1` is the ceiling the synced
`SmartNotesSettings.batch_notes_per_call` is clamped to, and the default is **10**. See ADR-017.

**Why:** Ten of the measured note type's seventeen enabled fields are pure-AI text on one
provider/model, and every note of the type uses the same prompt template — so fifty notes spent
500 completions asking the same ten questions. The win is the REQUEST COUNT (see the honest
numbers below); it is not a speed feature.

**Why K = 10, and why on by default.** The binding field on the measured deck is
"Synonyms (explained)" at ~385 output tokens p95 and ~677 at its longest. A chunk asks for K
answers inside ONE completion, so K·677 must fit the model's output cap: against Gemini Flash's
8192 that is 8192/677 ≈ 12 in the worst case, and 10 stays under it even when every answer in
the chunk is the longest ever seen. `FieldBudget` shrinks K further, per field, from what that
field's answers actually cost. Default ON because it pairs with
`max_concurrent_generations = 1`: at one worker there is no parallelism for a chunk to
serialise, so the −64% request saving costs nothing. **That interaction is the whole
justification — raise the worker count and K should come down.**

> **CORRECTED (2026-08-22) — see the 2026-08-22 entry at the top and ADR-018.** The output-budget
> half of this paragraph is right and is now the WHOLE justification for K = 10. The pairing half
> is not: `max_concurrent_generations` ships at 8, and "raise the worker count and K should come
> down" was derived from a fake rig's latency model that a real endpoint did not confirm — and
> that a second live session contradicted in the other direction. **K's effect on wall clock is
> unproven either way**; K = 10 stands on 8192/677 ≈ 12 alone.

**Files:** new `plugins/smart_notes/engine/batching.py` (`FieldWork`, `WaveTask`/`SoloTask`/
`ChunkTask`, `SoloPlanner`/`FieldBatchRunner`, `FieldBudget`, `parse_batch_items`, `match_items`,
`collapsed_indexes`); modified `engine/service.py` (`works_for` replaces `units_for`; `batch_planner`),
`integration/batch.py`, `plugins/smart_notes/config.py` (`notes_per_call()` — the single read
site of both knobs), `envs.py` (the flag),
`core/providers/llm/{base,gemini,openai_compatible}.py`, `core/providers/usage.py`,
`core/config/models.py`, `config/providers.example.toml`,
`gui/smart_notes/dialogs/controllers/config.py` and the Advanced-pane web files; new
`tests/plugins/smart_notes/test_batching.py`; `tests/benchmarks/{fakes,smart_notes_throughput}.py`.

**How to verify:**
`.venv/bin/python -m pytest tests/ -q -m "not llm and not tts and not integration"` →
`2272 passed, 18 skipped, 105 deselected`. Then the
benchmark — fake provider, simulated latency, never real quota.

`.venv/bin/python tests/benchmarks/smart_notes_throughput.py --notes 50 --latency 0.05 --workers 3 8`, the rows
that matter for L3 (full output below in the LAYER 1 entry):

```
| configuration                      | wall (s) | speed-up | provider calls | fields | identical |
|------------------------------------|---------:|---------:|---------------:|-------:|----------:|
| baseline (sequential)              |    39.70 |    1.00x |            700 |    850 |     50/50 |
| +L1+L2 (N=3, provider WITH cache)  |    13.79 |    2.88x |            700 |    850 |     50/50 |
| +L1+L2+L3 (N=3, K=10, output=0%)   |     5.31 |    7.48x |            250 |    850 |     50/50 |
| +L1+L2+L3 (N=3, K=10, output=50%)  |    10.34 |    3.84x |            250 |    850 |     50/50 |
| +L1+L2+L3 (N=3, K=10, output=100%) |    15.94 |    2.49x |            250 |    850 |     50/50 |
| +L1+L2 (N=8, provider WITH cache)  |     5.38 |    7.39x |            700 |    850 |     50/50 |
| +L1+L2+L3 (N=8, K=10, output=0%)   |     2.74 |   14.51x |            250 |    850 |     50/50 |
| +L1+L2+L3 (N=8, K=10, output=50%)  |     7.20 |    5.51x |            250 |    850 |     50/50 |
| +L1+L2+L3 (N=8, K=10, output=100%) |    12.82 |    3.10x |            250 |    850 |     50/50 |
```

**Read that table honestly.** `output=` is the fraction of a solo call's latency the fake charges
for GENERATING its one answer; a K-item chunk pays K times that part, because a provider does.
Compare each L3 row against the `+L1+L2` row at the SAME worker count — the speed-up column is
against the sequential baseline and flatters everything. At `output=0%` — the model the first
version of this benchmark used, where output is free — L3 is 2.6× at N=3 and 2.0× at N=8. At
50%, the honest middle, it is 1.33× at N=3 and **0.75× at N=8, i.e. slower than not batching**.
At 100% it loses at both (0.87× and 0.42×). A chunk serialises K answers' worth of generation
into one worker; a pool that wide would have generated them concurrently. **This is why the
shipped default pairs K = 10 with ONE worker: at N=1 there is no parallelism for the chunk to
serialise, so the request saving costs nothing. Raising the worker count should lower K.**

**So: LAYER 3 buys REQUESTS, not seconds. 700 → 250 calls (−64%), every time, at every setting.**
That number depends on no latency model. Turn it on to stay under a rate limit or to cut
per-request overhead; do not turn it on to make a batch finish sooner, and do not turn it on at
a high worker count at all.

> **RETRACTED IN PART (2026-08-22) — see ADR-018.** The half that survives is the one depending on
> no latency model: **LAYER 3 buys REQUESTS.** Everything above about SECONDS does not. `output=`
> is an assumption about how a provider splits a call between fixed overhead and generating
> tokens; nothing in this repo measures it, and at `output=0%` the same rig has batching winning
> everywhere. Against the real endpoint one session had grouping faster at every worker count and
> a second had K=20 tied with ungrouped and K=10 2.2x slower, with each arm's own run-to-run
> spread as wide as the gap between arms (rows in `tests/benchmarks/data/`). So: do not read
> "0.75× at N=8" as a property of the feature, do not read the live session's opposite as one
> either, and note that the shipped default no longer pairs K with one worker — it is 8 workers
> and K = 10, the latter on the output budget alone.

Input tokens are not a free win either. On the long template the envelope amortises
(211,082 → 92,442 prompt chars) but the prefix-cache hits drop 490 → 40; on a one-line template
(`--template short`) the envelope's own boilerplate is bigger than what it replaces and the total
goes UP, 33,664 → 73,424 chars.

Hostile providers (`--corrupt`, 50 notes, N=3, output=50%): **850 fields and 50/50 identical
notes in all five modes**, at roughly 300 calls (`drop-one`, `duplicate-id`), ~840 (`collapse`)
and ~940 (`renumber`, `truncate` — worse than the 700-call baseline). Approximate deliberately:
wave composition depends on thread timing, so the call column moves run to run (`renumber` was
940 on most runs and 890 under instrumentation). The exact part is the fields and the identity.

**Notes / rollback:** `OMNIA_SMART_NOTES_BATCHING=-1` is the rollback, and off is the
pre-batching CODE PATH, not the batching path at width one — `batch_planner(1)` returns
`SOLO_PLANNER`. Residuals, all in ADR-017: **context bleed** (a model answering ten items at
once can let one note's wording drift into another's) is a quality risk no parser can detect —
its degenerate cases are caught by `collapsed_indexes`, which discards any answer string shared
by two or more items whose own inputs differ (PARTIAL collapse included: an earlier whole-chunk
test saw nothing wrong with a reply that copied item 1's answer onto items 2-4 and answered item
5 properly), while the gradual case — a slow drift in wording — remains undetectable; **batching
widens one poisoned note's blast radius from 1 to K**, which matters because the web clipper is
a first-class input path;
the Account dialog's `calls` column drops by ~K (tokens stay whole); per-note fallbacks run
sequentially inside the worker that owned the chunk; and `FieldBudget` is the first `engine/`
state written from a worker, so `engine/` imports `threading` (never `aqt`/`anki`/
`concurrent.futures`). A chunk also sends an EXPLICIT `max_tokens` of `per-item estimate × K`
where the solo path sends none — deliberate, because K answers share one completion and the cap
is what makes truncation deterministic rather than a property of the vendor's default; an
unusable reply both halves K and doubles the per-item estimate, or the half-size retry would ask
for exactly the room that just failed.

---

## 2026-08-21 — Prompt caching for smart-notes text generation (LAYER 2)

**What:** `LLMProvider` grew `generate_cached_text(parts: PromptParts, …) -> (text, usage)`, whose
base implementation concatenates the two parts and delegates — i.e. today's exact call. A text
rule's prompt is now SPLIT at its first `{{ref}}` (`split_prompt` / `prompt_parts_for`) so the
template's instruction head, which is identical for every note of a note type, is offered to the
provider as a cacheable prefix. Gemini and the OpenAI family report how much of the input their
cache served (`usage["cached"]`); OpenRouter can be told to mark the prefix explicitly via a new
`[llm.<name>].prompt_cache_control`, default off. See ADR-016 (LAYER 2 section).

**Why:** Every note of a note type shares one prompt TEMPLATE and differs only in the
interpolated values, but `prompt_for` interpolated before the provider ever saw the string — so
the instructions and one note's values arrived as a single blob with nothing for any prefix cache
to match on.

**Files:** modified `core/providers/llm/base.py` (`PromptParts`, `supports_prompt_cache`,
`generate_cached_text`, and `last_usage` documented as closed to new code),
`core/providers/llm/gemini.py` and `core/providers/llm/openai_compatible.py`,
`core/providers/usage.py`, `core/config/models.py` + `config/providers.example.toml`,
`plugins/smart_notes/engine/{interpolation,rules,generators}.py`; new
`tests/plugins/smart_notes/test_prompt_cache.py`, `tests/providers/test_llm_prompt_cache.py`.

**How to verify:**
`.venv/bin/python -m pytest tests/ -q -m "not llm and not tts and not integration"` →
`2272 passed, 18 skipped, 105 deselected`. Then the benchmark's input-accounting table. L2 must
move the input accounting and NOTHING else — same 700 calls, same 850 fields, 50/50 identical
(row labels verbatim from the run; `--template short` is a separate invocation):

```
| configuration                                       | prompt chars | repeated prefix | share | hits | misses |
|-----------------------------------------------------|-------------:|----------------:|------:|-----:|-------:|
| +L1 (N=3, provider WITHOUT a prefix cache)           |      211,082 |               0 |  0.0% |    0 |      0 |
| +L1+L2 (N=3, provider WITH a prefix cache)           |      211,082 |         184,436 | 87.4% |  490 |     10 |
| ... with a one-line template (--template short)      |       33,664 |          11,466 | 34.1% |  490 |     10 |
```

**The row labels are half the correction, and they are now the labels the benchmark PRINTS.**
They used to read a bare `+L1` and `+L1+L2`, which invited "before L2 nothing repeated, after L2
87.4% repeats"; the entry then quoted corrected labels that existed nowhere in the output, so
reproducing it produced the reading the entry said was wrong. Both rows run identical engine code
and send identical bytes; the split is lossless and has no user switch, so the A/B is between a
provider that has a prefix cache and one that does not — and `_measure` names them that way.

**And 87.4% is a property of the template, not of anyone's deck.** The share is
`prefix / (prefix + value)`, where the prefix is what precedes the first `{{ref}}` — **378
characters** here, not the 386 of the whole interpolated template, a conflation this entry and
both ADRs used to repeat. It is 34.1% with a one-line template; a template that LEADS with a
`{{ref}}` gets zero. The real deck's prompts were never sampled, so no figure here is a
measurement of them — `--template short|long` exists so the number is read as the function it is,
and the benchmark's own advice line now changes with the template instead of telling a reader
running `--template short` to re-run with `--template short`.

**Notes / rollback:** The benchmark counts CHARACTERS, not tokens — the fake has no tokenizer, so
a token column would be a number with no provenance; chars/4 is printed as a labelled estimate.
The repeated-prefix figure is an optimistic bound (the ledger has no TTL and no minimum cacheable
size). Rollback is one line in `TextGenerator.generate`. Residuals: a template that LEADS with
`{{ref}}` gets no benefit; `prompt_cache_control` must stay off for any model not documented to
accept a content array; the authoring paths and the language detector still use `generate_text`
deliberately.

---

## 2026-08-21 — Bounded concurrency for smart-notes generation (LAYER 1)

**What:** Generation is no longer forced to be sequential. Three things landed together: a
process-wide `ProviderLimiter` spent at the HTTP request boundary (`ThrottledHttpClient` wraps
`DEFAULT_HTTP_CLIENT`); a `Dispatch` seam in `engine/` whose default is still one-at-a-time in
the calling thread, with the `ThreadPoolExecutor`-backed `PooledDispatch` in `integration/`; and
a `NoteRun` that walks one note a dependency LEVEL at a time, which the batch runner drives for
several notes at once (cohort → round → wave). One setting, `max_concurrent_generations`,
**default 1 — the pre-concurrency behaviour, with no pool created at all**. See ADR-016.

> **SUPERSEDED (2026-08-22): the default is 8** — see the 2026-08-22 entry at the top and
> ADR-018. `= 1` is still the exact revert; it is no longer what a user gets without asking.

**Why:** On the note type this was measured against (35 fields, 17 with generation on, 8435
notes) the dependency graph puts those 17 fields in only 5 levels of widths 2/4/6/4/1 — fields
inside a level are independent by construction — yet every one of the 17 was a separate serial
round trip, and no two notes ever overlapped. The sequential floor is 5 round trips per note, not
17. Sequential execution was also the only thing throttling the provider, so removing it required
a real bound in the same change.

**Files:** new `src/omnia/core/network/limiter.py`,
`src/omnia/core/concurrency/{dispatch,pool}.py` (the seam and the pool — in `core`, because
neither holds a line of smart-notes),
`src/omnia/plugins/smart_notes/engine/note_run.py`,
`tests/{core/test_provider_limiter,plugins/smart_notes/test_levels,plugins/smart_notes/test_concurrency}.py`,
`tests/benchmarks/{smart_notes_throughput,fakes}.py`; modified `core/network/http.py`
(`ThrottledHttpClient`, the `_open` seam), `core/network/__init__.py` (lazy re-exports, so
importing the limiter no longer drags in every provider), `core/providers/__init__.py` (the unpinned `llm()` AND
`tts()` paths are cached), `core/providers/tts/edge_tts.py` (an explicit permit),
`core/runtime/native_runtime.py` (`default_manager` under a lock), `core/providers/usage.py`,
`core/anki_compat.py`, `engine/{ordering,service}.py`, `engine/tools/{base,pipeline}.py`,
`integration/batch.py`, `plugins/smart_notes/config.py`, and the Advanced pane.

**How to verify:**
`.venv/bin/python -m pytest tests/ -q -m "not llm and not tts and not integration"` →
`2272 passed, 18 skipped, 105 deselected`.

```
$ .venv/bin/python tests/benchmarks/smart_notes_throughput.py --notes 50 --latency 0.05 --workers 3 8
workload: 50 notes x 17 fields (5 levels, widths 2/4/6/4/1), 50 ms per SOLO provider call, long template (386 chars, 378-char cacheable prefix)

| configuration | wall clock (s) | speed-up | provider calls | observed peak | limiter peak | limiter wait (s) | 429s | notes written | fields written | blocked | field errors | identical to baseline |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline (sequential) | 39.70 | 1.00x | 700 | 1 | 1 | 0.00 | 0 | 50 | 850 | 0 | 0 | 50/50 |
| +L1 (N=3, provider WITHOUT a prefix cache) | 13.76 | 2.89x | 700 | 3 | 3 | 0.00 | 0 | 50 | 850 | 0 | 0 | 50/50 |
| +L1 (N=8, provider WITHOUT a prefix cache) | 5.37 | 7.39x | 700 | 8 | 8 | 0.00 | 0 | 50 | 850 | 0 | 0 | 50/50 |
| +L1+L2 (N=3, provider WITH a prefix cache) | 13.79 | 2.88x | 700 | 3 | 3 | 0.00 | 0 | 50 | 850 | 0 | 0 | 50/50 |
| +L1+L2 (N=8, provider WITH a prefix cache) | 5.38 | 7.39x | 700 | 8 | 8 | 0.00 | 0 | 50 | 850 | 0 | 0 | 50/50 |
| +L1+L2+L3 (N=3, K=10, output=0%) | 5.31 | 7.48x | 250 | 3 | 3 | 0.00 | 0 | 50 | 850 | 0 | 0 | 50/50 |
| +L1+L2+L3 (N=3, K=10, output=50%) | 10.34 | 3.84x | 250 | 3 | 3 | 0.00 | 0 | 50 | 850 | 0 | 0 | 50/50 |
| +L1+L2+L3 (N=3, K=10, output=100%) | 15.94 | 2.49x | 250 | 3 | 3 | 0.00 | 0 | 50 | 850 | 0 | 0 | 50/50 |
| +L1+L2+L3 (N=8, K=10, output=0%) | 2.74 | 14.51x | 250 | 8 | 8 | 0.00 | 0 | 50 | 850 | 0 | 0 | 50/50 |
| +L1+L2+L3 (N=8, K=10, output=50%) | 7.20 | 5.51x | 250 | 8 | 8 | 0.00 | 0 | 50 | 850 | 0 | 0 | 50/50 |
| +L1+L2+L3 (N=8, K=10, output=100%) | 12.82 | 3.10x | 250 | 8 | 8 | 0.00 | 0 | 50 | 850 | 0 | 0 | 50/50 |
```

(Verbatim; the two further tables the same command prints are in the L2 and L3 entries.)

**The limiter, measured.** It used to be sized to `workers + 1`, so it could never block — the
pool width was doing all the bounding, and the "peak in flight" column printed the fake
transport's counter rather than the limiter's. Capacity is now `workers`, restored on exit, and
`OMNIA_MAX_CONCURRENT_REQUESTS` can narrow it below the pool.

**One workload, stated once, and ADR-016 quotes these same four rows.** Every run below is
`tests/benchmarks/smart_notes_throughput.py --notes 50 --latency 0.01 --workers 8
--output-share 0.5`, plus the flags in the row's own name; each figure is that run's
`+L1 (N=8, …)` line, and the baseline is its sequential row.

```
| run                                          | wall (s) | 429s | limiter peak | limiter wait (s) | fields |
|----------------------------------------------|---------:|-----:|-------------:|-----------------:|-------:|
| baseline (sequential)                         |    10.02 |    0 |            1 |             0.00 |    850 |
| N=8, provider 429s above 4, NO request bound  |    33.88 |   91 |            8 |             0.00 |    850 |
| N=8, provider 429s above 4, --request-limit 4 |     2.64 |    0 |            4 |             7.89 |    850 |
| N=8, no rate limit,         --request-limit 3 |     3.55 |    0 |            3 |            13.09 |    850 |
```

That is the limiter/retry division of labour with BOTH mechanisms in the rig (the fake transport
now overrides `UrllibHttpClient._open`, so the real `RetryPolicy` runs above it; the previous rig
replaced the whole client and had no retry at all). Retry keeps the run CORRECT under a
systematic 429 — 850/850 fields, 50/50 identical, even at 91 errors — and cannot keep it fast:
backoff makes N=8 finish **3.4× slower than sequential**. The bound is what prevents the storm.
(Wall-clock figures are machine- and load-dependent; the shape — a rate-limited unbounded run
losing to sequential, and the bounded run beating both — reproduces, the third decimal does not.)

**Notes / rollback:** Rollback is the shipped default. Known residuals, all in ADR-016: user
tools may `import urllib` directly and cannot be bounded (`edge_tts` now takes an explicit
permit, so it no longer escapes); a user tool that READS a field it does not DECLARE now races
its de-facto producer; and a user tool's `run` may execute concurrently with itself, which is
stated in `Tool`'s docstring, in `user_tools`' module docstring and as rule 8b of the authoring
prompt. **No behaviour change rides along in `core/`, and two that had been are now reverted on
purpose.** `RecordingLLMProvider` still forces `temperature = 0.7` (honouring the user's
configured per-provider temperature instead is a real fix and a real change in generated text —
it ships on its own, with a release note), and a POST still retries an ambiguous network error
the FULL budget, exactly as before this work; bounding it to one retry to cap a possible
double-charge was written, measured against what it costs (a field the third attempt would have
generated) and reverted. Both are pinned by tests that state the intent, so reversing either is
a deliberate act rather than a tidy-up.

## 2026-08-16 — The UI smoke becomes a real test harness, and moves out of `scripts/`

**What:** `scripts/ui_smoke.py` is now `tests/smoke/run_smoke.py`, and it asserts behaviour
instead of absence-of-exception. Everything else in `scripts/` produces an artefact (an
`.ankiaddon`, an `addons21` layout, a `vendor/` tree, a badge JSON); this produces a pass/fail,
so it belongs with the tests — under `tests/`, but named `run_smoke.py`, because pytest collects
only `test_*.py` and stubbing `aqt`/`anki` (which `tests/conftest.py` deliberately does) would
remove the only reason this harness exists. Fourteen steps now check what the add-on *did*: the
JS that actually reached the reviewer webview carries each injecting plugin's payload on the
side that plugin claims; a real press of an answer button routed through the live
`Reviewer._answerCard` patch comes back as the ease the rules specify (typed_accuracy
substitutes its staged grade, overdue_guard then caps it, and the staged ease is consumed
exactly once); all seven plugins have a step of their own; every Configure dialog is built from
`manager.plugins()` rather than a hard-coded id list; and disabling everything must leave no
transformer, no asset, no Tools-menu action and no rewritten grade behind.

**Why:** The harness had stopped being run and had stopped being true. It could not even import
(no `vendor/universal` on `sys.path` → `No module named 'pydantic'`), three steps pointed at
modules the dialogs refactor had deleted, and its `mw` stand-in was a `SimpleNamespace` — so
`QAction(label, mw)` raised, auto_flip's `on_enable` aborted, the manager's plugin-isolation
boundary swallowed it, `set_enabled` returned a `False` nobody read, and the run printed all
green with six of seven features on and one leaking a web asset plus two pycmd routes past
teardown. It also captured every injected script into `js_log` and reported only how many it had
counted. Its docstring claimed it fired "every gui_hook Omnia subscribes and constructs every
dialog"; it fired 11 of 14 and built 6 of 8. This is the ONLY thing in the repo that runs Omnia
against real `aqt` + real `anki` + real Qt, so what it fails to check, nothing checks.

**Files:** `tests/smoke/run_smoke.py` (moved with `git mv`, rewritten);
`tests/scripts/test_common.py` (the printing-guard net now scans `PRINTING_ENTRY_POINT_DIRS` —
`scripts/` *and* `tests/smoke/` — so the harness stays inside it after the move);
`.claude/JOURNAL.md` (the how-to-verify path).

**How to verify:** `.venv/bin/python -m pytest tests/ -q` → 2157 collected, unchanged by the
move, and `run_smoke.py` is not collected (it appears only as a parametrize id in
`test_common.py`). Then, with Anki's own interpreter:
`QT_QPA_PLATFORM=offscreen "<AnkiProgramFiles>/.venv/bin/python" tests/smoke/run_smoke.py` →
`ALL UI SMOKE STEPS PASSED (14 steps)`, exit 0.

**Notes / rollback:** The harness is hermetic — it seeds a temp config dir from the tracked
`*.example.toml` only (never the live `config/providers.toml`), binds an ephemeral port for
word_lookup (8766 is the port the developer's own Anki is already serving), and keeps
`taskman.run_in_background` a no-op so a `QueryOp` that would call a provider is never executed.
Two settings are written to make otherwise environment-dependent steps deterministic:
`overdue_guard.force_again_after_days = 0` (so the expected ease is the rule's own constant, not
Anki's SM-2 prediction, which moves with the card's due date) and the word_lookup port. The
card is forged overdue by inserting a `revlog` row, not by backdating `card.mod` —
`col.update_card` restamps `mod` to now. Older entries below still name `scripts/ui_smoke.py`;
this log is append-only, so they are left as they were written.

---

## 2026-08-15 — Piper voices download on first use instead of shipping in the package

**What:** The `.ankiaddon` no longer contains the `*.onnx` TTS voice weights (`build_addon.py`
excludes the suffix); the ~5 KB `.onnx.json` + README still ship. A new
`core/providers/tts/voice_models.py` holds `PiperVoiceStore`, which resolves a voice as
`user_files/models/piper/` → the `models/piper/` copy beside the add-on → a one-time download
from the upstream HuggingFace voice repo. Downloads stream through the injectable `ByteStreamer`
seam into a temp file inside the destination dir, verify the pinned byte count + SHA-256, and
`os.replace` into place; progress goes to Anki's progress dialog through the new
`anki_compat.progress_label` (marshalled to the main thread). `PiperTTS` takes an injectable
`store`; both piper runners now share one `_require_model_file` message.

**Why:** The package was 59 MB and 99% of it was one Vietnamese voice — over AnkiWeb's limit, and
every user downloaded it to install AND again on every single update, for a voice most of them
will never play. Package is now ~1 MB.

**Files:** `src/omnia/core/providers/tts/voice_models.py` (new), `.../tts/piper.py`,
`src/omnia/core/anki_compat.py`, `scripts/build_addon.py`, `models/piper/README.md`,
`config/providers.example.toml`, `README.md`, `.claude/CLAUDE.md`,
`tests/providers/test_piper_voice_models.py` (new), `tests/scripts/test_build_addon.py` (new),
`tests/providers/test_sweep.py`.

**How to verify:** `pytest tests/providers/test_piper_voice_models.py tests/scripts -q`, then
`python scripts/build_addon.py` and check the archive has no `*.onnx` and is ~1 MB.

**Notes / rollback:** The checked-in `models/` + its Git LFS setup are untouched — a developer's
local copy still wins over any download. Resolution rejects a weights file whose size differs from
the catalog's, which is what makes an unfetched LFS *pointer* (CI never runs `git lfs pull`) fall
through to a download rather than be handed to piper as a model. The native-runtime toggle's
`size_hint="~50 MB"` is unchanged: it still installs only the venv, and the voice reports its own
size in the download's progress label. The narrow exception to ADR-005's "run paths never
auto-install" — a run path may fetch inert DATA, never a RUNTIME, and must check the runtime
FIRST — is recorded as **ADR-015** in `DECISIONS.md`, which is where the next reader of ADR-005
will look. Rollback = drop `.onnx` from `EXCLUDE_SUFFIXES`.

---

## 2026-08-15 — smart_notes Try-it: a test form built from the tool, not a fixed one

**What:** The Tools tab's "Try it" panel now renders ONE control per input the tool declares, and
renders its result by the kind produced. New on the Tool contract: `input_kinds`
(`Mapping[str, str]`), `INPUT_KINDS` (`text/image/audio/video/file`) and `INPUT_KIND_EXTENSIONS`,
re-exported from `engine.tools`. A text row is typed into and carries its own attach; a media row
opens a browser filtered to its family. Output: text inline, image as name + icon into the existing
lightbox, audio/video as name + icon handed to Anki's own `av_player`. `MediaSampleStage` became
multi-slot (one staged file per input, name-collision guarded) and copy-then-replace.

**Why:** The panel showed one textarea called "Sample" plus a PERMANENT "Choose file…" button
whatever the tool read, so a tool taking a word and a clip got one undifferentiated box, and a
pure-text tool carried a browse button it had no use for. The cause was a gap in the contract, not
the CSS: a tool declared what it PRODUCES (`kinds`) and WHICH fields it reads
(`referenced_fields`), but nothing said what those fields HOLD — and one generic box is the only
honest form to draw when that is unknown.

**Files:** modified `plugins/smart_notes/engine/tools/{base,user_tools,media_sample,__init__}.py`,
`plugins/smart_notes/authoring/tool_author.py`, `gui/smart_notes/dialogs/controllers/user_tools.py`,
`gui/smart_notes/web/{page.html,page.css,10-usertools.js}`; tests in
`tests/gui/test_smart_notes_user_tools_ui.py`, `tests/plugins/test_smart_notes_{user_tools,media_sample}.py`.

**How to verify:** `pytest tests/ -q` → `1990 passed, 68 skipped, 15 xfailed`. Then LIVE, which is
the part that matters here — Anki against a throwaway `ANKI_BASE`, driven over CDP, on BOTH
platforms. A draft declaring `{"Word": "text", "Clip": "audio", "Pic": "image"}` gives, identically
on macOS 25.09.2 and Windows 26.08.1:

```
["Word📎", "Clip📎 Choose audio…", "Pic📎 Choose image…"]
{"hasOldPick": false, "hasOldSample": false, "inputsHost": true}
```

and real runs producing real bytes render each kind:

```
image  🖼️ pngdemo.png — 70 bytes    [🔍 View]
audio  🔊 wavdemo.wav — 244 bytes   [🔊 Play]
video  🎬 mp4demo.mp4 — 512 bytes   [🎬 Open]
```

**Notes / rollback:** Inputs are read from the draft source by AST, NEVER by `exec`. Compiling the
draft is the only other way to reach the ClassVar and it would run the module BEFORE the risk
banner — inverting the one safety property this flow is built on. The cost: a COMPUTED
`input_kinds` cannot be read, so that tool falls back to a single row, and that row keeps its own
attach button, so nothing becomes untestable. A blank-default param (meaning "this rule's first
prompt reference") is declared under `"Sample"` rather than being given an invented default; an
earlier revision did invent one and silently disabled that fallback for every generated tool.
Produced mp4/H.264 and m4a/AAC do NOT decode in Anki's QtWebEngine (verified against the shipped
Qt 6.8.2 via `data:`, `blob:` and `http://`), which is why playback is handed to `av_player`
rather than to an in-page element. To reverse, revert the branch; saved tools that declare nothing
already behave exactly as they did before.
## 2026-08-15 — Providers: one generic registry for both kinds; the LLM builder table is gone

**What:** The provider seam now has ONE registration mechanism instead of two. New root-level
`core/providers/base.py` (`ProviderBase`: `name` / `requires_api` / `from_config`) and
`core/providers/registry.py` (`ProviderRegistry`, a `Mapping` subclass owning `register` /
`create` / `names` / `classes` / `requiring_api` / `keyless`), bound once per kind in
`llm/registry.py` (`@register_llm`, `LLM_REGISTRY`) and `tts/registry.py` (`@register_tts`,
`TTS_REGISTRY`). `core/providers/llm/factory.py` is **deleted**: its three `_build_*` closures
moved verbatim into `from_config` classmethods on the providers they built, and its two
name-keyed dicts collapsed into the registry. No provider was renamed, added, or removed —
`available_llm_providers()` / `available_tts_providers()` return the identical lists. See
**ADR-014**.

**Why:** TTS self-registered while LLM carried a hand-maintained `_BUILDERS` table plus a
parallel `_PROVIDER_CLASSES` map that existed only so callers could read `requires_api` without
building a provider — two dicts and a test whose whole job was to catch them drifting. The
duplication had already leaked out of `core/`: `plugins/smart_notes/account.py` imported the
private `_PROVIDER_CLASSES` to get a provider's class name, the last plugin→core private reach
in the tree. A third provider kind would have arrived to two patterns and no reason to pick one.

**Files:** added `core/providers/base.py`, `core/providers/registry.py`,
`core/providers/llm/registry.py`, `tests/providers/test_provider_registry.py`; deleted
`core/providers/llm/factory.py`; modified `core/providers/{llm,tts}/base.py`,
`core/providers/llm/{__init__,gemini,gemini_vertex,openai_compatible}.py`,
`core/providers/tts/{__init__,registry}.py`, `plugins/smart_notes/account.py`,
`tests/providers/{test_provider_metadata,test_sweep,test_tts_registry}.py`,
`tests/core/test_config.py`, `.claude/CLAUDE.md`, `.claude/DECISIONS.md` (ADR-014).

A review pass then found the one thing the split had missed: `_OPENAI_DEFAULTS`, the base-URL
table for the openai family, existed byte for byte in BOTH `llm/openai_compatible.py` and
`tts/openai_compatible.py`. It describes vendor HTTP endpoints — not text, not audio — so it is
the most kind-agnostic data in the layer and belongs at the root. Added
`core/providers/openai_family.py` (`OPENAI_FAMILY_BASE_URLS` + `openai_family_base_url`) with
`tests/providers/test_openai_family.py` asserting BOTH kinds resolve through it, plus the LLM
default provider, which had been pinned on the TTS side only. Same pass removed `get_tts()`
(zero callers) and the `registered_tts_providers()` synonym.

**How to verify:** `pytest tests/ -q` → `1945 passed, 68 skipped, 15 xfailed in 112.57s` (1904
before this branch; the six deleted TTS-only/vacuous tests are replaced by the new
`test_provider_registry.py` and `test_openai_family.py`, and the branch also carries the
`tests/scripts/` suite). `mypy src/omnia/core/providers` → `Found 107 errors in 42 files
(checked 24 source files)`, the same pre-existing count as before, none in the new modules. The
names are a persisted contract, so check them without a harness:

```bash
.venv/bin/python -S -c "import sys;sys.path[:] = [p for p in sys.path if 'site-packages' not in p and '.venv' not in p];sys.path.insert(0,'src');sys.path.append('vendor/universal');from omnia.core.providers import available_llm_providers as a, available_tts_providers as b;print(a());print(b())"
# ['gemini', 'gemini_vertex', 'openai', 'openai_compatible', 'openrouter']
# ['edge_tts', 'google_cloud', 'google_translate', 'openai', 'openai_compatible', 'openrouter', 'piper', 'viettts']
```

**Notes / rollback:** Three constraints the mechanism now imposes, each pinned by a test and
spelled out in ADR-014: `register` must not stamp `cls.name` (one class serves several names,
and the usage rows join on that name); `from_config` must be defined on the class, never
inherited (`GeminiVertexProvider` would otherwise inherit Gemini's and demand an `api_key`); and
`from_config` must stay pure construction, because `ProviderHub.llm()` calls `create` under its
cache lock. `test_provider_metadata.py` now also freezes both public name lists as hand-written
literals — every other guard in that file is derived from the registry and would pass even if a
name vanished. To reverse, restore `factory.py` from git and re-point `llm/__init__.py` and
`account.py`; the `from_config` classmethods can stay (nothing else depends on their absence).

---

## 2026-08-14 — smart_notes tools: one chain rule, honest dependency edges, a picker that refuses guesswork

**What:** Three shared seams changed at once. (1) **The chain has exactly one rule** — run the
tools in the configured order, fall through on every failure. `TerminalToolError` (a tool
halting the chain) and `Tool.exclusive` + `registry.chain_conflict` (a tool refusing to share
one) are removed; see **ADR-012**, which supersedes ADR-011. (2) **Tool-param dependency edges
are labelled honestly**: a param naming a field was always a prerequisite, but the graph marked
those edges `derived=False` — "the user drew this" — so they rendered solid and Delete silently
no-op'd on them. They are now `derived` plus a new `from_tool` flag, and Delete names the Tools
picker instead of doing nothing. (3) **`Tool.required_params`** — a blank field param resolves
to a fallback the UI cannot show, so Done now refuses while one is blank, naming the tool and
the param. Plus `Tool.uses_provider` (deliberately NOT the inverse of `deterministic` —
`cloze_audio` is both), a lock narrowed to Type + Prompt, a randomised preview note, and
`match_word_forms` removed (inflections always match).

**Why:** The picker showed an ordered list the runtime would sometimes decline to run and
sometimes stop halfway, for reasons belonging to one tool; the settings row lied about which
cells applied and about what the lock froze; and a `cloze` row's dependency on the two fields
it reads was invisible in the graph.

**Files:** `plugins/smart_notes/engine/tools/{base,pipeline,registry,cloze,cloze_audio}.py`,
`plugins/smart_notes/engine/graph.py`, `gui/smart_notes/html.py`,
`gui/smart_notes/web/{03-render,06-graph,09-tools}.js`, `gui/smart_notes/web/page.css`,
`core/anki_compat.py`, `.claude/DECISIONS.md` (ADR-011 superseded, ADR-012 added).

**How to verify:** `pytest tests/ -q` (1713 passed). For the UI half, render the page with a
live tool catalog and drive it over CDP: a `cloze`-only row fades Provider/Model while a
`cloze_audio` row keeps Provider/Voice; locking a row disables only the Type select; Done is
refused while a required field param is blank and the message narrows as they are filled;
pressing Delete on a tool edge in the Dependencies view answers *"'Cloze' reads 'Sentence' in
its Tools settings"*.

**Notes / rollback:** The accepted cost of the ordering rule is in ADR-012 and pinned by
`test_a_tts_tool_after_it_speaks_the_answer_and_that_is_the_configured_rule`: a field
configured `[cloze_audio, <any tts tool>]` whose `cloze_audio` fails will have the next tool
speak the answer. `cloze_audio`'s own guarantee is unchanged — it masks or it raises, and never
declines. To reverse, restore the three symbols named in ADR-011 and write an ADR superseding
ADR-012.

---

## 2026-08-14 — smart_notes user-authored tools: describe a transform once, run it forever for free (Phase 4)

**What:** A global **Tools tab** where the user describes a transform in plain English ("from
the audio filename, extract the extension"), an LLM writes a complete `Tool` subclass, the dialog
shows the **entire source unedited**, the user runs it on a sample, and only then can save. The
result is a plain Python file at `user_files/tools/<slug>.py`, imported at plugin start so its
`@register_tool("user:<slug>")` runs — after that it is an ordinary registered tool: same picker,
same params form, same prerequisites, same failure taxonomy, and **no LLM call at run time**.
New: `engine/tools/user_tools.py` (`UserToolStore` = one file per tool, `UserToolLoader` =
guarded compile + register, `ImportGuard`, `ReviewGate`, `UserToolTester`),
`authoring/tool_author.py` (the one LLM call + a worked example the tests load and RUN),
`gui/.../controllers/user_tools.py` + `web/10-usertools.js` + the tab pane, `unregister_tool` on
the registry, and `SmartNotesSettings.fields_using_tool` so a delete can name the affected fields.

**Why:** Anything that can be done without calling the LLM should be, because every LLM call
costs tokens. A transform a model does not need to think about should not pay a model every
time. The step-DSL designed for this was rejected in favour of real Python (plan decision 3).

**Security — the design delta that makes real Python defensible (stated plainly, per the module
docstring):** a user tool runs **in-process, at the add-on's full trust level** — collection,
filesystem, network, keys. There is no sandbox and none is claimed. What makes it acceptable is
the *provenance*, and all of it is enforced: (1) the user wrote the description; (2) the FULL
source is shown, never a summary; (3) `ReviewGate` refuses `user_tool_save` unless that exact
source was test-RUN in this session (keyed by digest, so editing re-arms it) — the disabled Save
button is only the courtesy, the controller is the rule; (4) the artefact is a file on THIS disk in
`user_files/`, which AnkiWeb does **not** sync, so approving code on one machine can never execute
it on another — a device without the file resolves the chain entry to `unknown_tool` and falls
through, exactly like an uninstalled builtin. What would NOT be acceptable and is absent:
auto-saving without review, importing a tool from a URL/shared deck, or putting the source in the
synced collection blob (the rejected design's cross-device execution vector). `ImportGuard` is an
allowlist on imports + a few builtin calls (`open`, `eval`, `__import__`, …) and is documented, in
code and here, as a **speed bump, not a boundary** — `__import__` via `getattr` defeats it; its job
is to catch a generation that ignored its instructions so the source the user reads is the source
that runs.

**Files:** `src/omnia/plugins/smart_notes/engine/tools/user_tools.py`,
`src/omnia/plugins/smart_notes/authoring/tool_author.py`,
`src/omnia/gui/smart_notes/dialogs/controllers/user_tools.py`,
`src/omnia/gui/smart_notes/web/10-usertools.js` (new); `engine/tools/{registry,__init__}.py`,
`plugins/smart_notes/{__init__,config}.py`, `authoring/__init__.py`, `gui/smart_notes/html.py`,
`dialogs/studio.py`, `dialogs/controllers/__init__.py`, `web/{page.html,page.css,05-handlers.js}`,
`envs.py`; tests `tests/plugins/test_smart_notes_user_tools.py` (63) and
`tests/gui/test_smart_notes_user_tools_ui.py` (23).

**How to verify:** `pytest tests/ -q` (1700 passed, 125 skipped) + `pre-commit run --files …`. The
load-bearing tests: a module that raises at import is skipped, logged and does not stop its
siblings; a file registering `ai` (or any bare name) is rolled back and cannot shadow a builtin; a
loaded tool runs through the REAL `GenerationPipeline` against a context whose `providers` raises
on ANY attribute access (proving zero token spend) and its params feed `rule_referenced_fields`
like a builtin's; `user_tool_save` is refused before a test run and after an edit. The worked
example inside the system prompt is itself loaded, guard-checked and executed by the suite, so the
prompt and the loader cannot drift. End to end: `python scripts/install_addon.py`, open Smart Notes
→ ⚙ Options → **Tools** → New tool.

**Notes / rollback:** No persisted key is added — a field chain still stores only the tool NAME, so
the synced blob is unchanged and old devices are unaffected. The `user_files/tools` folder is
created on first save. `on_disable` unregisters exactly what the loader registered. Test runs and
generation both go off the Qt thread via `run_in_background` (a user tool is arbitrary Python; the
main thread must not be what finds that out). Reverting the PR removes the tab and the loader;
existing tool files stay on disk, harmless and unloaded. Follow-ups deliberately not done:
export/import of tools between devices, and a params form in the test box (the system prompt
requires a default for every option, so defaults always run).

---

## 2026-08-14 — smart_notes `cloze_audio`: listening-cloze audio that never speaks the answer (Phase 3)

> **Partly superseded the same day.** The `TerminalToolError` / `Tool.exclusive` mechanisms
> described below were removed — see the "one chain rule" entry above and **ADR-012**. What
> still holds is the tool's own guarantee: it masks or it raises, and never declines.

**What:** `engine/tools/cloze_audio.py` registers `cloze_audio` (`kinds={"tts"}`,
`deterministic=True` — it calls TTS, never an LLM). It splits the source field at the spans that
must stay unheard, synthesizes the surrounding segments through the SAME resolved voice, measures
the hidden word by synthesizing it once, and splices silence (or a beep) of exactly that many
frames in its place, fading every join. Spans come from the `{{cN::…}}` markers the source already
carries (the `cloze` → `cloze_audio` chain) and otherwise from `ClozeRewriter.occurrences`, now
public so both tools share one matcher — located in the RAW value, because `strip_markup` unwraps a
cloze to its answer. WAV voices (piper/viet-tts) splice with `core/audio/wav.py` and nothing to
install; MP3 voices go through a new `audio` native runtime (pip `av`, ADR-005) driven by
`core/audio/sidecar.py` + a bundled `sidecar_cli.py`, which only ever crosses the codec boundary —
the splice stays in tested pure Python. Three supporting changes: a new
`TerminalToolError` that STOPS the pipeline chain, `ResolvedVoice` extracted from `TTSGenerator` so
both callers resolve a voice identically, and `BatchSummary.errored_note_ids` (graft #4) so a note
whose every field errored is kept for retry instead of being discarded as "empty".

**Why:** A TTS field pointed at a cloze field reads the answer out loud, because `strip_markup`
unwraps `{{c1::survive}}` to `survive` before synthesis. The user's two locked decisions shaped the
rest: (1) never speak the answer — so an unmaskable field must NOT fall through, which the existing
outcome taxonomy could not express (every outcome AND every exception falls through, so
`[cloze_audio, ai]` would hand the sentence straight to plain TTS). Hence `TerminalToolError`, and a
guard around the whole produce phase so a failure in code this tool merely CALLS (an unconfigured
Auto-detect voice raises an ordinary `ProviderError`) cannot reopen the leak. (2) every TTS backend
must work — so the `av` sidecar is required scope, not optional, and until it is installed an MP3
voice hard-fails with an "install the audio runtime in Advanced" message rather than degrading.

**Files:** `src/omnia/plugins/smart_notes/engine/tools/cloze_audio.py`,
`src/omnia/core/audio/{sidecar.py,sidecar_cli.py}` (new);
`core/audio/__init__.py`, `core/text.py` (`CLOZE_RE` made public),
`engine/generators.py` (`ResolvedVoice`), `engine/tools/{base,pipeline,cloze,__init__}.py`,
`engine/__init__.py`, `integration/batch.py`; tests
`tests/plugins/test_smart_notes_cloze_audio.py`, `tests/core/test_audio_sidecar.py` (new) plus
additions to `test_smart_notes_{tools,batch}.py`. No GUI change: the picker renders the params from
the model's JSON schema and the Advanced tab renders the new `audio` section from the registry.

**How to verify:** `pytest tests/ -q` (1552 passed, 125 skipped) and
`pre-commit run --files <changed>`. The load-bearing test is `TestNeverSpeaksTheAnswer`: a fake voice
encodes the text it was asked to say into its own samples, so the produced clip is decoded back and
asserted not to contain the answer, and each failure path (no span, params that will not parse, MP3
without the runtime, provider down, unconfigured voice, an unexpected bug) is asserted to raise
`TerminalToolError` and to stop a `[cloze_audio, ai]` chain dead. Frame math is checked at 22050 Hz
mono. The sidecar transcode runs for real in `TestRealTranscode`, marked `integration` and skipped
wherever PyAV is absent (CI included): `pip install av && pytest -m integration
tests/core/test_audio_sidecar.py` — verified against PyAV 18.1.0. End to end, install with
`python scripts/install_addon.py`, enable "Audio codec" in Options → Advanced, and generate a
cloze-audio field on an edge_tts voice.

**Notes / rollback:** `TerminalToolError` is the only new chain semantic — an ordinary `ToolError`
still falls through, which a test pins. The sidecar CLI is decode/encode ONLY (files in, files out:
Windows opens the standard streams in text mode and would corrupt audio on a pipe); re-encoding to
MP3 keeps media small, and libmp3lame rate support is handled by falling back to 44100. `av` is
never auto-installed. Reverting the PR removes the tool and the runtime; no persisted key is renamed
and a field that never used `cloze_audio` is untouched.

**Second review round (same day, four fix commits on the branch).** (1) `parse_params` runs inside
the pipeline's attempt guard but BEFORE `run`, so a `ValidationError` on stored params was an
ordinary error attempt and `[cloze_audio, ai]` fell through to `ai`, which spoke the answer —
reachable from a newer release's value for a known key (ADR-010), a hand-edited blob, or the
picker's own `Number("1e999")` → `Infinity` → `null`; the tool now overrides `parse_params` and
re-raises `TerminalToolError`. (2) `availability()` returned a reason whenever the audio runtime was
missing and the picker gated on it, so a user on the bundled piper voice — for whom the tool works
with nothing installed — could not enable it; availability is advisory per the `Tool` contract, the
picker now gates only on "not installed here" / "wrong field type", and the tool's report renders as
a note. (3) The sidecar encoded mono voices as stereo (no layout on the output stream), doubling
every clip. (4) Graft #1 shipped after all — see below. (5) The provider-name lists are derived from
`TTS_REGISTRY.audio_ext` (`tts_providers_with_ext`) instead of two hardcoded, already-drifted
strings. Also: the codec runtime is injected through `ToolContext.audio` rather than reached for via
the process-wide `default_manager`.

**Residual risk, stated plainly (graft #1).** The terminal failure only protects a device that HAS
the tool. A `[cloze_audio, ai]` chain synced to an older Omnia resolves the entry to `unknown_tool`,
degrades, and `ai` speaks the sentence with the answer in it. That cannot be fixed from this side —
an older device cannot be retrofitted — so the mitigation IS: the picker warns whenever a
text-to-speech tool follows `cloze_audio` in a chain, and no chain this build writes itself ever
places one there (`default_tool_chain()` is the single `ai` tool, pinned by a test). Users on mixed
versions should upgrade every device before configuring such a chain.

---

## 2026-08-14 — smart_notes `cloze` tool + the per-row Tools picker (Phase 2)

**What:** The first deterministic tool and the UI that lets a user choose it.
`engine/tools/cloze.py` registers `cloze` (`kinds={"text"}`, `deterministic=True`): it wraps the
base word inside a sentence field as `{{c1::…}}`, matching inflected forms in BOTH directions
(the promoted de-inflector only walks inflected → base, so the sentence's own tokens are
de-inflected too — verified by probe before designing), scanning only the field's plain-text
spans so a match never starts inside an HTML tag, a `[sound:…]` reference, an entity or an
existing cloze, and rewriting the ORIGINAL value. Params (`sentence_field`, `word_field`,
`match_word_forms`, `separate_cards`, `mask`) are a `PersistedModel` (ADR-010) whose JSON schema
drives a generated mini-form in the new Tools column: a per-row modal with include checkboxes,
▲/▼ ordering and greyed-out entries carrying their `availability()` reason, painted from a
`window.__SN_TOOLS` catalog baked into the dialog like `__SN_CATALOG`. `BatchSummary` gains
`unfilled` (chain produced nothing, nothing broke) and `tool_fallbacks` (a NON-first tool
produced — the pipeline now stamps `GenerationResult.tool`).

**Why:** A `[cloze, ai]` chain is the whole point of the tools plan: cloze produces for free and
the LLM is called only when it declines (a test asserts the fake LLM is never invoked on the hit
path). `tool_fallbacks` makes a deterministic first tool that quietly stops matching — and is
therefore billing the LLM on every note — visible in the summary instead of only in the log.

**Files:** `src/omnia/plugins/smart_notes/engine/tools/cloze.py` (new),
`engine/tools/{__init__,pipeline}.py`, `engine/{generators,rules}.py`, `config.py`
(`SmartNotesFieldRule.base_field`), `integration/batch.py`, `gui/smart_notes/html.py`,
`gui/smart_notes/web/09-tools.js` (new) + `{page.html,page.css,03-render.js,04-modal.js}`,
`gui/smart_notes/dialogs/{studio.py,prompt.py,controllers/authoring.py}`;
tests `tests/plugins/test_smart_notes_cloze.py`, `tests/gui/test_smart_notes_tools_picker.py`,
plus additions to `test_smart_notes_batch.py` / `test_smart_notes_dialog_deps.py`.

**How to verify:** `pytest tests/ -q` (1468 passed) and
`pre-commit run --files <changed>`. The five sync points a dropped chain hides behind
(`collectRows`, `row_to_payload`, `field_configs_from_payload`, `SmartNotesFieldConfig.tools`,
the row Preview path) are covered end-to-end by `TestFiveSyncPoints` +
`TestPreviewRunsTheRowsToolChain`; the picker itself was additionally driven in jsdom (tick /
reorder / params / Done / Cancel / type-change reset, and the `save` + `preview` payloads).

**Notes / rollback:** `on_preview` now builds its rule through `compile_field_rule` instead of
field-by-field, which is what makes the preview inherit the chain — the one behavioural
side-effect (a promptless row's base source is no longer a derived dep) is compensated in
`_preview_inputs`. Reverting the PR restores the AI-only path: an empty `tools` chain still
compiles to `("ai",)` and no persisted key is renamed. Phase 3 (`cloze_audio`) needs
`core/audio/wav.py` and must NOT reuse plain TTS on a clozed field — `strip_markup` unwraps
cloze to the answer.

---

## 2026-08-14 — smart_notes tool seam: ToolRegistry + GenerationPipeline (Phase 1)

**What:** Field generation now runs through a per-field TOOL CHAIN instead of the service's
one-strategy-per-kind dispatch. New seam under `plugins/smart_notes/engine/tools/`: a `Tool` ABC
with the outcome taxonomy (`Produced` / `NotApplicable` / `Empty`, breakage = raise), a
`@register_tool` registry mirroring `@register_tts`, and a `GenerationPipeline` that runs a rule's
chain in order until one tool produces, recording every attempt. The whole pre-existing LLM/TTS
path is repackaged as exactly one registered tool, `ai`, and a field with no configured chain
compiles to `("ai",)` — so behaviour is unchanged (a 27-scenario differential harness against
`origin/main` found zero difference). Config gains one additive key,
`SmartNotesFieldConfig.tools`; `FailedField` gains `kind` (`"error"` vs `"unproductive"`) so an
exhausted chain is still isolated per field.

**Why:** Phases 2-4 need deterministic tools (`cloze`, `cloze_audio`, user-authored tools) that
can decline and fall through to the LLM, and the choke point had no place to put them. Doing it
as a behaviour-identical refactor first keeps the risky part (the deterministic tools) off the
critical path.

**Files:** `plugins/smart_notes/engine/tools/{base,registry,pipeline,ai,__init__}.py` (new),
`engine/service.py`, `engine/rules.py`, `engine/__init__.py`, `plugins/smart_notes/config.py`,
`gui/smart_notes/html.py`, `gui/smart_notes/dialogs/controllers/config.py`,
`tests/plugins/test_smart_notes_tools{,_golden}.py`, `tests/plugins/test_smart_notes_store.py`,
`tests/gui/test_smart_notes_{html,dialog_deps}.py`.

**How to verify:** `pytest tests/plugins/test_smart_notes_tools.py
tests/plugins/test_smart_notes_tools_golden.py tests/plugins/test_smart_notes_store.py
tests/gui/test_smart_notes_html.py -q`. The golden suite pins byte-identical generation for a
legacy (`tools=[]`) config against a fake LLM; the store suite pins that a legacy config still
writes NO `tools` key.

**Notes / rollback:** Three things a future reader must not "clean up":
1. **A large part of the new surface has no production consumer in Phase 1 — by design.**
   `ToolError`, the `Empty` outcome, `Tool.availability`, `Tool.params_model`,
   `Tool.referenced_fields`, `tools_catalog()` and `FailedField.kind` are the forward contract
   the deterministic tools and the Phase-2 tools picker are built against (the plan's §3/§8), not
   accreted cruft. They are exercised by the tests through fake tools. Deleting them as dead code
   would have to be undone in Phase 2.
2. **`SmartNotesFieldConfig.dict()` deliberately omits an empty `tools`.** The blob syncs, and a
   device on a release from before ADR-010 still validates it with `extra="forbid"` and has no
   `try` around `SmartNotesStore.load()` — an unknown key there crashes it on every note-add hook.
   Omitting the key keeps a legacy config's persisted bytes identical to a pre-tools build's, so
   the key first appears only when a user actually configures a chain.
3. **The dialog save path merges the stored chain back.** The fields table has no tools column
   yet, so `field_configs_from_payload(rows, stored)` carries `tools` over from the persisted row;
   rebuilding the row from the payload alone silently deleted it (CONVENTIONS Part 2). The Phase-2
   picker keeps that fallback and only lets the payload win when it really carries a `tools` key.
Rollback: the phase is additive — reverting the PR restores the old dispatch, since no persisted
key is renamed or removed and a legacy `tools=[]` always meant the `ai` path.

---

## 2026-08-14 — Persisted config models tolerate unknown keys (ADR-010)

**What:** One shared base for every config model: `StrictModel` (`extra="forbid"`, for payloads
parsed from the GUI and other never-stored input) and `PersistedModel` (`extra="allow"`, for
anything serialized into the synced collection config). Six duplicated strict bases across
`core/` and the plugins collapse into it. Unknown *values* are kept verbatim in storage and
neutralized where they are consumed, never rewritten.

**Why:** The user runs omnia on macOS, Windows and Ubuntu, on possibly different versions. With
`extra="forbid"` an older device raised `ValidationError` on a blob a newer one wrote — and
`PluginManager._activate` swallows that into "the feature silently never enables". `extra="ignore"`
was implemented first and rejected after a probe against the vendored pydantic 1.10.26 proved it
is worse: the dialog re-serializes the whole settings tree on save, so an older device would write
a stripped blob back and destroy the newer device's config. `allow` retains AND round-trips.

**Files:** `src/omnia/core/config/base.py` (new), `core/config/models.py`, all six
`plugins/*/config.py`, `plugins/smart_notes/engine/rules.py`, `.claude/DECISIONS.md` (ADR-010).

**How to verify:** `pytest tests/core/test_config.py tests/plugins/test_smart_notes_store.py -q` —
the round-trip tests seed unknown keys at every level of the synced blob and assert survival.

**Notes / rollback:** This is the forward-compat FLOOR for the installed fleet: semantics shipped
here cannot be fixed retroactively on devices already running it, so install it everywhere before
any release that writes new keys. One bounded loss spot remains by design (a note type rebuilt
from the dialog's JS payload loses that note type's unknown row keys) — documented in ADR-010.

---

## 2026-08-14 — core/lang/word_forms and core/audio/wav seams

**What:** The rule-based de-inflector and word-boundary regex builders move out of
`plugins/word_lookup/logic.py` into `core/lang/word_forms.py` (word_lookup re-exports them, so
behavior is bit-identical), and `core/audio/wav.py` adds a pure-stdlib 16-bit PCM toolkit:
`WavClip` with parse/verify, concat, generated silence and sine beep, and edge fades.

**Why:** Groundwork for the smart-notes `cloze` / `cloze_audio` tools, which need the same
de-inflection as the lookup panel — and plugins may not import each other, so shared logic has to
live in `core/`. The WAV toolkit avoids `audioop` (removed in Python 3.13) and any binary
dependency, keeping cloze audio zero-install on WAV providers.

**Files:** `src/omnia/core/lang/word_forms.py`, `src/omnia/core/audio/wav.py`,
`plugins/word_lookup/logic.py`, `tests/core/test_word_forms.py`, `tests/core/test_wav.py`.

**How to verify:** `pytest tests/core/test_wav.py tests/core/test_word_forms.py tests/plugins/test_word_lookup.py -q`

**Notes / rollback:** `WavClip.from_bytes` must raise `WavFormatError` for ANY malformed input —
the stdlib `wave` module raises a bare `RuntimeError` with an empty message on a chunk whose
declared size overruns the file, which is why the except clause is broad and the message carries
the exception type name.

---

## 2026-08-14 — Note Maintenance: deterministic batch clean-up of existing notes

**What:** A new feature plugin (`note_maintenance`) that repairs the text a collection ALREADY
holds — no LLM, no network, no API key. One plugin hosts many small *tasks* in a plugin-local
registry (`@register_task`): strip IPA out of a synonym list, pull the file name out of a
`[sound:…]` reference, re-pair synonyms with their transcriptions, refill an example sentence
from its clozed twin, and a literal find-and-replace across every field. Each task has its own
`enable` + `order`, runs from the Browser context menu over the SELECTED notes, and every change
is reviewed in a diff dialog (whole plan listed via a `QTreeWidget` + rich-text delegate) before
a single `CollectionOp` writes the batch as ONE undo step. Two shared seams grew with it:
`core/text.py` (the plain-text → field-HTML boundary both this plugin and smart_notes need) and
`gui/config_form.ConfigFieldEditor` — the field→widget mapping extracted out of
`PluginConfigDialog` so the bespoke per-task panel renders declared fields with the SAME controls
instead of a second widget factory; `gui/widgets.hint_label` likewise now owns the theme-safe
secondary label all three bespoke dialogs used to copy.

**Why:** smart-notes *generates* content and costs tokens; a user with 5 000 imported notes and no
API key still needs the mechanical fixes (leftover IPA, `[sound:]` wrappers, mismatched synonym
lists). Deterministic tasks make that free, repeatable and reviewable — and the preview + single
undo entry is what makes a 5 000-note rewrite safe to offer at all.

**Files:** `src/omnia/plugins/note_maintenance/` (`base`, `registry`, `runner`, `preview`, `diff`,
`apply`, `config`, `tasks/*`), `src/omnia/gui/note_maintenance/{panel,preview_dialog}.py`,
`src/omnia/gui/{widgets.py (NEW), config_form.py}`, `src/omnia/core/text.py`,
`src/omnia/core/anki_compat.py`, `config/features.example.toml`, `tests/plugins/note_maintenance/*`
+ `tests/gui/test_note_maintenance_dialogs.py`, `tests/core/test_text.py`.

**How to verify:** `pytest tests/plugins/note_maintenance tests/gui tests/core/test_text.py -q`
(all green; full suite 1232 passed). In Anki: enable *Note Maintenance*, configure the tasks in
its panel, then Browser → select notes → right-click → *🧹 Omnia · Maintain Notes…* → untick
anything → Apply → Ctrl+Z puts the whole batch back.

**Notes / rollback:** Branch `feat/note-maintenance` (not merged at time of writing). Data-safety
rules the review round hardened, keep them: the settings panel MERGES onto the raw stored `tasks`
map (`ConfigRepository.update_section` is a shallow `dict.update`, so rebuilding the map from the
registry deletes a newer Omnia's task section — ADR-010's hazard one layer up); `build_tasks`'
per-task fallback preserves the user's `enable`/`order` so an unreadable option can never flip a
task back on; the save catches broad `Exception` (the collection backend does not raise `OSError`)
and keeps the dialog open on failure. Rollback = disable the plugin (it installs only the Browser
menu entry; nothing runs on enable, on a timer, or without the diff being confirmed).

## 2026-08-07 — display_interval exposes the predicted next interval to card templates

**What:** With `expose_to_templates` (default on), display_interval now PREPENDS a `<script>`
into the answer HTML via the `card_will_show` filter setting `window.omniaIntervals =
{next_seconds, next_days, next_label, current_days}` (+ a `omnia:intervals` CustomEvent) — the
same non-destructive pipeline preview as the grading-bar label (Good folded through the ease
transformers, so overdue_guard is reflected; typed_accuracy is not — async). Prepending runs
before any template script → templates read it synchronously, no polling. **Fallback contract
(user rule):** injected ONLY while ≥1 ease transformer is active (overdue_guard/typed_accuracy
on) AND the flag is on AND compute succeeds — otherwise `window.omniaIntervals` stays undefined
and the template's own fallback (`{{info-Ivl:}}`, the current interval) applies.

**Why:** the user's `AnkiVocabulary` Back picks which audio to chain (Definition vs Example) by
interval; `{{info-Ivl:}}` only gives the CURRENT interval, but the semantically right signal is
the predicted NEXT one — the card should show the Definition while it is still being
forgotten and the Example once it is known. The template rule now prefers
`omniaIntervals.next_days` (threshold kept at 4) with an `{{info-Ivl:}}` fallback.

**Files:** `src/omnia/plugins/display_interval/{__init__,config}.py`, `tests/plugins/
test_display_interval.py` (+6), `tests/conftest.py` (card_will_show FakeHook). Template updates
applied live via AnkiConnect; backups: `.claude/tmp/ankivocabulary-templates-backup-*.json`.

**How to verify:** `pytest tests/plugins/test_display_interval.py -q` (15). In Anki (after
restart): review an answer side → `window.omniaIntervals` defined before template scripts.

**Notes / rollback:** committed on the `fix/auto-flip-html5-audio-wait` branch (stacked with the
HTML5-audio watcher so the live symlinked tree stays consistent; one PR, two commits). Turn off
per-user via the plugin's "Expose to card templates" toggle.

## 2026-07-15 — Auto-flip waits for template-JS HTML5 audio (not just av_player)

**What:** `wait_for_audio` now also holds the countdown while the card template plays audio through
its own JS on an HTML5 `<audio>/<video>` (no `[sound:…]` AV tags — invisible to `av_player`). A
`media_watch.js` injected via the **web-injector seam** on both sides tracks every playing media
element with capture-phase document listeners (idle debounced 350ms for chained clips; detached
elements pruned) and reports `media_busy`/`media_idle` over `pycmd`; the plugin holds a pending
countdown on busy (preserving the Enter-cancel) and re-arms the held side on idle, deferring to a
still-draining av_player queue in both directions.

**Why:** the `AnkiVocabulary` "Word -> Mean" Back rewrite moved audio from `[sound:]`-carrying
fields to a JS-driven `dynPlayer` — the sounds hook reported `[]`, auto-flip armed immediately and
graded mid-playback, because nothing told it how long the audio actually was.

**Files:** `src/omnia/gui/auto_flip/web/media_watch.js` (NEW), `src/omnia/plugins/auto_flip/
media_watch.py` (NEW loader), `…/auto_flip/__init__.py` (busy/idle ops + arm gating + per-side
reset), `…/auto_flip/config.py` (help text), `tests/plugins/test_auto_flip.py` (+9 tests).

**How to verify:** `pytest tests/plugins/test_auto_flip.py -q` (49). Live-verified via CDP in the
real Qt WebEngine: play on a real `<audio>` → `media_busy` over pycmd, natural `ended` →
`media_idle`, two full cycles, exact injector message format. Full suite 1013 passed.

**Notes / rollback:** branch `fix/auto-flip-html5-audio-wait` (PR pending merge). Same
`wait_for_audio` flag — no new config. Chromium autoplay policy note: programmatic `play()` needs a
user gesture (or Anki's flags); the watcher only reports what actually plays.

## 2026-07-15 — One-click clipper install from the Integrations tab (Install / Upgrade / Up-to-date)

**What:** Each integration row in Smart Notes → Options → Integrations now has an action button
that replaces the whole manual setup with one click. **Desktop** ("Install app"): clone the repo →
build a venv from a real host Python (`NativeRuntimeManager.host_python`, NOT Anki's frozen one) →
`pip install` deps + PyInstaller → run `build.py` → **the installer** copies the built app into a
per-platform location (macOS `/Applications` w/ `~/Applications` fallback, Windows
`%LOCALAPPDATA%\Programs`, Linux `~/.local/share`) → opens it. **Web** ("Set up…"): clone → reveal
the folder → open `chrome://extensions` (Chrome forbids programmatic install). A successful install
records the installed commit in an `.omnia-installed` marker; `status()` compares it to the remote
`main` HEAD (`git ls-remote`) so the button shows **Install** (no marker) / **Upgrade** (remote
ahead) / **Up to date** (equal → disabled). Runs off the Qt main thread with live progress.

**Why:** the end-user contract is "run `build.py`, then click the app — never any other terminal
command". This collapses the multi-minute venv/build/install/open/grant flow into a single button,
and keeps the installed app current with a visible Upgrade affordance.

**Files:** `src/omnia/plugins/smart_notes/integration/installer.py` (NEW — `ClipperInstaller` +
`SubprocessCommandRunner`, pure/injectable, `install_root` override for tests), `…/integration/
integrations.py` (`repo_url`/`install_kind` + `integration_for_key`), `src/omnia/gui/smart_notes/
dialogs/controllers/config.py` (`install_integration` + `refresh_install_status` ops, off-thread +
`window.__snClipperInstall*` push), `src/omnia/gui/smart_notes/web/{05-handlers.js,page.css}`,
`tests/plugins/test_clipper_installer.py` (NEW), `tests/gui/test_smart_notes_dialog_deps.py`.

**How to verify:** `pytest tests/plugins/test_clipper_installer.py -q` (installer orchestration,
cross-platform paths, marker, status) + full suite green (1004 passed). Live-verified: the built app
lands in `/Applications` and launches (pid confirmed); `git ls-remote` reaches the real repo; a stale
marker → `upgrade=True`, the current commit → up-to-date.

**Notes / rollback:** merged via PR #7 (both commits: the feature + the install/open/upgrade fix).
The installer OWNS install+open (an earlier version opened a hardcoded `/Applications` path the build
never created → nothing launched). **Follow-up done** (branch
`fix/clipper-install-cooperate-with-signed-build`): the desktop-clipper's stable-identity signing is
now merged, so the installer runs `build.py --no-install` (build + sign `dist/` only) and is the sole
installer — copying with **`ditto`** on macOS so the stable code signature survives (verified live:
`ditto` preserves the designated requirement byte-for-byte), and `shutil.copytree` on Windows/Linux.
This is what keeps Accessibility/Input-Monitoring grants across future **Upgrade** rebuilds.

## 2026-07-15 — Clippers → git submodules, usage syncs by default, post-migration audit fixes

**What:** (1) The two companion clippers were moved out of the omnia tree into their own repos
(`osirisQdt2810/omnia-web-clipper`, `…/omnia-desktop-clipper`) and re-added as **git submodules**
under `3rdparty/`; omnia's history was rewritten so no commit touches clipper source. The
web-clipper was refactored to `src/` + `assets/icons/`. (2) **Usage now syncs by default**
(ADR-008): `CollectionUsageStore` (`col.set_config`) replaces the device-local `col.db` table, and
`UsageStore`/`VoiceCache` `save()` return a bool so the dispatch marker only advances on a persisted
copy. (3) A 3-repo latent-bug/doc audit fixed real issues (web-clipper reinjection path + zip `.git`
leak; desktop-clipper config-load crash + non-atomic save + missing capture guard + non-text
clipboard wipe + incomplete/py3.13-uninstallable OCR deps; omnia README storage/menu/provider docs).

**Why:** the clippers are independent plugins that deserve their own repos/history; usage should
follow a user across devices (and a future web view); and the audit hardens everything shipped.

**Files:** `3rdparty/{README.md,.gitmodules}`, submodule repos (own history); omnia
`src/omnia/core/providers/{usage,voice_cache}.py`, `core/config/dispatch.py`, `envs.py`, `README.md`,
`tests/{providers/test_usage,core/test_persistence_dispatch}.py`; `.claude/DECISIONS.md` (ADR-008).

**How to verify:** `git clone --recurse-submodules <omnia>` hydrates both clippers;
`pytest tests/ -q` (988 passed); desktop `pytest tests -q` (56 passed); `git log --all --name-only |
grep 3rdparty/omnia-.*-clipper/` is empty (no clipper source in omnia history).

**Notes / rollback:** shipped as branches + PRs (not merged to main by me), per the branch+PR rule —
omnia `docs/install-and-storage` + `feat/usage-sync-by-default`; web-clipper `docs/per-machine-install`
+ `fix/reinjection-path-and-packaging`; desktop-clipper `docs/fix-cross-repo-links` + `fix/robustness`.
Deferred desktop follow-ups: off-main-thread add-note/OCR, HiDPI region grab, held-modifier synth-copy.
A pre-rewrite backup bundle is in the session scratchpad.

**What:** Moved all remaining file-based state into Anki's collection DB behind per-concern
storage abstractions, each with two independent backends selected by an `envs` knob. Config
(omnia/features) → `col.set_config` (`CollectionConfigLoader`, synced); usage → a `col.db`
`omnia_usage` aggregate table (`ColUsageStore`) written via a `BufferedUsageRecorder` (buffers
in memory, flushes on the Qt main thread, since `col` is main-thread-only); voice cache →
`col.set_config` (`CollectionVoiceCache`). The old `mw.addonManager`/`meta.json` layer was
dropped. Dispatch is env-driven (`OMNIA_CONFIG_STORAGE`/`OMNIA_USAGE_STORAGE`/
`OMNIA_VOICE_CACHE_STORAGE`, default `"database"`; file backends `TomlConfigLoader`/
`JsonUsageStore`/`JsonVoiceCache` stay selectable). A `PersistenceDispatcher` remembers the
last-used backend per concern in `user_files/.storage.json` and, when a knob changes, copies
that concern's data old→new so switching never loses state. `providers.toml` + `.secrets/`
(the only files kept, for credentials) moved from the add-on root to `user_files/config/` so
Anki preserves them across add-on updates; shipped `*.example.toml` templates stay at the root
`config/` and seed the live dir on first run (`template_dir` split in the loaders).

**Why:** Plugin settings + enable flags should sync across devices (like the smart_notes rules
already did) and the add-on folder should stop accumulating JSON/TOML. Provider credentials must
stay local and must survive add-on updates (Anki wipes everything except `user_files/`+`meta.json`
on update — the old root `config/`+`.secrets/` were at risk).

**Files:** `core/config/loader.py` (`BaseConfigLoader` ABC + `TomlConfigLoader`/`CollectionConfigLoader`
+ `template_dir`), `core/config/dispatch.py` (new `PersistenceDispatcher`), `core/config/repository.py`
(loader type widened), `core/providers/usage.py` (`UsageStore`/`ColUsageStore`/`JsonUsageStore` +
`_fold_call` + `snapshot()` on the ABC + `BufferedUsageRecorder`), `core/providers/voice_cache.py`
(`VoiceCache`/`CollectionVoiceCache`/`JsonVoiceCache`), `envs.py` (`OMNIA_*_STORAGE` knobs),
`__init__.py` (bootstrap wiring), `gui/smart_notes/dialogs/{studio,context,controllers/account}.py`
(voice-cache injection + off-main-thread save fix), `scripts/install_addon.py` (assembled layout);
tests under `tests/core/` + `tests/providers/`. Deleted `core/config/addon_store.py`.

**How to verify:** `pytest tests/ -q` (913 passed). `python scripts/install_addon.py` then confirm
the deployed add-on has `config/` → templates symlink and `user_files/config/{providers.toml,.secrets/}`
holding the live data. Switch a knob (e.g. `OMNIA_USAGE_STORAGE=json`) and confirm the aggregate is
copied and the marker in `user_files/.storage.json` updates.

**Notes / rollback:** Legacy cutover was fresh (no data conversion): the old `omnia.toml`/`features.toml`/
`usage.json`/`voices.json` (+ orphan `typed_accuracy_stats.json`) were deleted, so switching to the DB
backend starts settings from defaults. The dispatch marker MUST stay a local file (`col` config is
synced, so it can't hold a per-device value). Usage durability is slightly weaker (a hard crash in the
sub-second window before a flush can lose the last few uncounted calls). `typed_accuracy`/`smart_notes`
were already DB-backed and untouched.

## 2026-07-05 — Audit fix pass: 19 LOW-severity defects (core seams, providers, smart-notes UI)

**What:** Fixed 19 low-severity defects found by an audit, across the plugin manager, provider
layer, logging, and the smart-notes web UI. Highlights: (L1) `PluginManager._deactivate` now
evicts the cached `PluginContext` so a config edit made while a plugin is disabled is picked up on
re-enable; (L2) boot now sets up logging + the crash logger BEFORE the eager `ConfigRepository`
load and logs a config-load failure before re-raising; (L3) `JsonUsageRecorder._dump` writes to a
temp file then `os.replace` (no more truncate-on-crash); (L4) `resolve_token_source` wraps bad
Vertex creds into a `ProviderError`; (L5) `RecordingLLMProvider` reads the wrapped provider's
`last_usage` into a local and passes it to `_record` (less racy on a shared instance); (L6)
native-runtime registers ONE `atexit→shutdown_all` in `__init__` instead of one dead-Popen handler
per launch; (L7) `GeminiProvider.generate_image` sets `last_usage` so image tokens are recorded;
(L8) typed-accuracy honours `show_stats=False` (no injector, no style-hook subscription); (L9)
smart-notes `on_set_base_field` carries `node_positions` through so switching the base field no
longer wipes pinned graph positions; (L10) the generic config form normalizes an Enum choice value
so a non-default choice preselects; (L11) escaped a field name in a `setMsg` innerHTML sink; (L12)
"Refresh voices" now also returns + merges the per-provider `CATALOG.voices` so the Default/row
voice pickers reflect refreshed voices; (L13) the web-clipper names its message/mousedown/keydown
handlers and removes them (incl. `chrome.runtime.onMessage`) on context-gone; (L14) `SubprocessRunner`
passes `creationflags=CREATE_NO_WINDOW` on Windows (no-op elsewhere); (L15) added
`anki_compat.escape_search_term` and applied it to `note:`/`deck:` query interpolation in two
sites; (L16) `omnia.log` uses a `RotatingFileHandler` (2 MB × 3); (L17) language detection anchors
the code regex with word boundaries and validates against the ISO-639-1 set; (L18) a locked field
in `buildSyncQueue` reverts its unsynced deps to the synced baseline + `clearRemovedFor` so a
derived-edge deletion isn't silently lost; (L19) the image-prompt heuristic is now ADVISORY (warns
but no longer blocks Save), separated from the blocking unknown-ref/brace checks.

**Why:** These were latent correctness/robustness/security paper-cuts (stale config on re-enable,
usage-file truncation, unbounded log growth, an XSS sink, lost graph positions, Windows console
flashes, a valid image prompt hard-blocked from saving, etc.) surfaced by the audit.

**Files:** `core/manager.py`, `__init__.py`, `core/providers/usage.py`,
`core/providers/token_source.py`, `core/providers/native_runtime.py`,
`core/providers/llm/gemini.py`, `plugins/typed_accuracy/__init__.py`,
`gui/smart_notes/dialogs/controllers/config.py`,
`gui/smart_notes/dialogs/controllers/account.py`, `gui/config_form.py`, `core/anki_compat.py`,
`core/logging/logger.py`, `plugins/smart_notes/engine/language.py`, `plugins/smart_notes/__init__.py`,
`gui/smart_notes/web/04-modal.js`, `gui/smart_notes/web/05-handlers.js`,
`gui/smart_notes/web/06-graph.js`, `3rdparty/omnia-web-clipper/content.js`. Tests: new
`tests/core/test_anki_compat.py`, `tests/gui/test_config_form.py`; extended `test_manager.py`,
`test_usage.py`, `test_native_runtime.py`, `test_typed_accuracy.py`, `test_smart_notes_language.py`.

**How to verify:** `python3 -m pytest tests/ -q` (839 passed); ruff/black/isort clean on the
changed files; `cat src/omnia/gui/smart_notes/web/0*.js | node --check -`;
`node --check 3rdparty/omnia-web-clipper/content.js`; `python3 scripts/install_addon.py`.

**Notes / rollback:** All fixes are surgical and independent; each can be reverted in isolation.
L5 is a partial fix (usage recording no longer round-trips through the shared `last_usage`, but the
`last_usage` attribute exposed to external readers remains racy — noted inline). L12 relies on
`catalog_payload` already building the full merged `voices` map.

---

## 2026-07-05 — Smart Notes gen robustness: live provider-config + per-field isolation

**What:** Two fixes to smart-notes generation. (A) `ProviderHub` now reads its LLM/TTS settings
FRESH from the injected `ConfigRepository` instead of a startup snapshot: added an optional
`config=` kwarg, turned `_llm_settings`/`_tts_settings` into read-only properties that return
`config.llm_settings()`/`config.tts_settings()` when a repo was injected (else the constructor
snapshot), and invalidate the per-rule LLM override cache when the settings object changes
(`_maybe_invalidate_cache`, called first in `llm()`). `PluginManager` now builds
`ProviderHub(config=config)`. Dialog one-shot hubs + all tests still pass positional snapshots
(`config` is None → unchanged behavior). (B) `GenerationService.generate_note` now isolates a
single field's generation failure: it catches the exception, logs it, records a new `FailedField`,
and continues — so a sibling text field that already succeeded is no longer discarded when e.g. a
TTS field has no Auto-detect voice. `generate_note` returns a 3-tuple `(results, blocked, failed)`;
the batch summary gained a distinct `field_failures` counter ("N field error(s)").

**Why:** (A) provider-setting edits in the dialog (e.g. Auto-detect voice) took effect only after
an Anki restart because the hub held a stale snapshot. (B) one misconfigured field aborted the
whole note, silently yielding an empty card and throwing away fields that had already generated.

**Files:** `core/providers/__init__.py` (hub fresh-read + cache invalidation),
`core/manager.py` (pass repo to hub), `plugins/smart_notes/engine/service.py` (`FailedField`,
per-field try/except, 3-tuple return), `plugins/smart_notes/integration/batch.py` (`field_failures`
on `_NoteOutcome`/`BatchSummary` + message), `plugins/smart_notes/integration/review.py` +
`plugins/smart_notes/__init__.py` (unpack 3-tuple). Tests: `tests/plugins/test_smart_notes.py`
(3-tuple unpacks, new `TestGenerateNoteFieldFailure`, `_FailingLLM`, field-error summary test).

**How to verify:** `pytest tests/ -q` (812 passed, 119 skipped). `python3 scripts/install_addon.py`
runs clean. Live (requires an Anki restart to load the new code): change an Auto-detect voice in the
dialog and generate without restarting — the new voice is used. Configure a note with a good text
field + a TTS field lacking an Auto-detect voice, batch-generate — the text field fills and the
summary reads "… 1 field error(s)" instead of a whole-note failure.

**Notes / rollback:** Backward-compatible — snapshot hubs (dialogs/tests) are untouched because
`config` defaults to None. `field_failures` (per-field) is kept distinct from `failed` (whole-note).
Live effect needs an Anki restart to pick up the new module. To roll back, revert the listed files.

---

## 2026-07-05 — Smart Notes integration gateway (auto-generate externally-clipped cards)

**What:** Smart Notes now auto-fills its fields on notes pushed in from outside Anki (the Omnia
Web Clipper via AnkiConnect's `addNote`), gated by TWO guards. Transport is the backend
`anki.hooks.note_will_be_added` hook (fires on every `col.add_note`, incl. AnkiConnect — no
polling, no server, no AnkiConnect coupling). A new `IntegrationGateway` cheap-gates on the
caller tag `omnia-autogen`, resolves the source via an extensible `Integration` registry
(`integration_for_tags`), checks the per-integration toggle (`SmartNotesSettings.
auto_generate_integrations`, default OFF), verifies the note type is configured + deck-scoped +
has an empty target, then defers past the commit with `mw.taskman.run_on_main` (so `note.id` is
set) and reuses `BatchGenerator` to generate. Write-back uses `update_note` (does NOT re-fire the
hook → no loop); the one-shot `omnia-autogen` tag is stripped after firing. A new **Integrations**
tab in the Smart Notes Options modal renders one opt-in row per integration + a "Detected N cards"
status line. The clipper gained an "Auto-generate in Omnia" option (default on) that stamps the
`omnia-autogen` tag on both capture paths.

**Why:** let users clip a word in the browser and have Omnia build the whole card automatically,
without a manual batch — while keeping LLM spend opt-in (both guards default safe) and the seam
open for future third-party integrations (one `Integration` entry + a UI row).

**Files:** `core/anki_compat.py` (`subscribe_anki_hook`/`unsubscribe_anki_hook`),
`plugins/smart_notes/integration/{integrations,gateway}.py` (new), `.../integration/__init__.py`,
`plugins/smart_notes/{__init__,config}.py`, `gui/smart_notes/dialogs/controllers/config.py`,
`gui/smart_notes/web/{page.html,01-bridge.js,05-handlers.js,page.css}`,
`3rdparty/omnia-web-clipper/{shared,background,options}.js` + `options.html`. Tests:
`tests/plugins/test_smart_notes_gateway.py` (new), plus `tests/conftest.py` (anki.hooks stub) +
`tests/plugins/test_smart_notes.py` + `tests/gui/test_smart_notes_dialog_deps.py`.

**How to verify:** `pytest tests/ -q` (809 passed). `python3 scripts/install_addon.py` runs clean;
JS: `cat gui/smart_notes/web/0*.js | node --check -`. Live: enable the clipper's Auto-generate +
the Integrations → Omnia Web Clipper toggle, clip a word → its empty smart fields fill in the
background; the Integrations tab shows "Detected N cards".

**Notes / rollback:** Both guards default OFF/opt-in — nothing auto-generates until the user
turns the integration toggle on AND the note carries `omnia-autogen`. Only ONE `addNote` path
exists in `background.js` (both capture flows converge on `addCaptureToAnki`), so tags are built
once there. To add an integration: append an `Integration` to `INTEGRATIONS` + a UI row; the
gateway/config iterate the tuple.

---

## 2026-07-01 — Smart Notes dependency-graph overhaul (robustness + UX + unified Save)

**What:** One cohesive pass over the Dependencies graph. Robustness: the DISPLAY layout is now
cycle-tolerant (`_layered_columns` skips back-edges, never raises), and cycle VALIDITY is
hard-only — a hard edge orders+blocks (a hard cycle is a real deadlock) while a soft edge is
optional metadata the ordering can break, so two fields that softly reference each other are
valid/generatable (`order_rules` hard-strict + soft-best-effort; `validate_acyclic`/
`would_create_cycle`/`cycle_edge_keys`/`cycle_error_for_config` hard-only). Auto-prompt no longer
writes dead edges (`apply_auto_smart` keeps a suggested dep only if the prompt `{{ref}}`s it). UX:
hover tooltip (glass card, scrollable, viewport-capped), persisted node positions
(`node_positions` on the note-type config, survive tab-switch + Save), dynamic border anchoring
(edges attach to nearest border point; the LABEL is the move handle, elsewhere connects + shows
the tooltip), lock integration (badge + can't be an edge target + hover-unlock), smaller
arrowheads. **Unified Save:** the "↻ Sync prompts" button is gone — edges delete freely (incl.
derived, tracked in a `removedEdges` set, no warning) and **Save** folds in the graph→prompt sync
(if edges changed → per-field review popover then persist; else persist directly; Discard reverts a
pending deletion). Removes the "forget to Sync / forget to Save" pitfalls.

**Why:** live testing found the graph blank + undraggable after Auto-prompt (a cyclic dep set made
`graph_payload` raise). The user then drove a series of UX asks (tooltip, saved positions, border
connect, lock, long-tooltip, text-zone move handle, arrowhead) plus the semantic fix that a
soft cycle must not error, and the merge of Sync into Save.

**Files:** `plugins/smart_notes/engine/{graph,ordering}.py`, `plugins/smart_notes/authoring/author.py`,
`plugins/smart_notes/config.py` (`node_positions`), `gui/smart_notes/html.py`,
`gui/smart_notes/dialogs/controllers/{config,graph}.py`, `gui/smart_notes/web/{06-graph.js,
05-handlers.js,page.html,page.css}`, tests `{test_smart_notes_graph,test_smart_notes}.py` +
`tests/gui/test_smart_notes_html.py`. Commit `3d31aa0` on branch `feat/smart-notes-graph-overhaul`.

**How to verify:** `pytest tests/ -q` (796 passed). Live (Anki 25.09 over CDP): Auto-prompt a
note type → Dependencies renders all edges (soft POS↔Definition cycle shows green, no warn/error;
a hard cycle shows amber + blocks Save); drag a label to move (persists over tab-switch + Save);
delete a derived edge freely → Save opens the review popover → Apply → prompt drops the ref.

**Notes / rollback:** JS is one IIFE (01..07 concatenated) — validate with
`cat web/0*.js | node --check`. Live-driving recipe: relaunch Anki with
`QTWEBENGINE_REMOTE_DEBUGGING` + `--remote-allow-origins=*` + auto-open launcher, drive via the
scratchpad `cdp.js`. Rollback: `git revert 3d31aa0`.

---

## 2026-07-01 — Repo restructure (source-only src/omnia) + dialog decomposition

**What:** Three structural refactors. (1) `SmartNotesDialog` god-class (1496 lines) split into a
106-line shell + `SmartNotesContext` + 5 controllers (config/graph/authoring/account/native_runtime)
under `gui/smart_notes/dialogs/controllers/`; (2) the dialog modules grouped into a
`gui/smart_notes/dialogs/` subpackage (`config_dialog.py`, `context.py`, `custom_prompt.py`,
`controllers/`) and the dead `prompt_dialog.py` deleted; (3) **repo restructure**: `src/omnia/` is
now SOURCE-ONLY — `vendor/`, `models/`, `config/` (templates) moved to the repo root, and runtime
data (`config/*.toml`, `.secrets/` (was `secrets/`), `user_files/`) is deployed-only/per-user.
`scripts/install_dev.py` → `install_addon.py` ASSEMBLES `addons21/<id>/` by symlinking each source
item + `vendor/` + `models/` and creating real seeded runtime dirs; `build_addon.py` is multi-root.
**Why:** the dialog was an unmaintainable god-class; mixing source with vendor/runtime/secrets in
one folder was the user's main structural complaint; OOP/composition is the preferred style.
**Files:** `gui/smart_notes/dialogs/**` (new), `gui/smart_notes/dialog.py` (shell); `scripts/
install_addon.py` (new, was install_dev.py), `scripts/build_addon.py`, `scripts/vendor_deps.py`;
`src/omnia/__init__.py` (`_ADDON_DIR = Path(__file__).parent.resolve()` — the symlink-deploy fix —
+ `.secrets`), `core/config/{repository,secrets,__init__}.py` (`.secrets`), `core/providers/tts/
piper.py` (`_resolve_models_dir` two-layout), `tests/conftest.py` + `test_anki_runtime.py` (vendor/
config paths), `pyproject.toml`/`.gitignore`/`.gitattributes`/`README.md`/`CLAUDE.md`. Moves:
`src/omnia/{vendor,models,config/*.example.toml,secrets/README.md}` → repo root via `git mv`.
**How to verify:** `pytest -q -m "not llm and not tts"` (778 passed / 0 failed); `python scripts/
install_addon.py` then check `addons21/omnia/` = source/vendor/models symlinks + real
config/.secrets/user_files; `python scripts/build_addon.py` → zip with no secrets/runtime; deployed
`import omnia` resolves `_ADDON_DIR` to the deployed folder, loads vendored pydantic, and resolves
config + secrets. Commits: `8cf3a9c` (controllers), `d506442` (dialogs subpackage), `2db4878`
(repo restructure).
**Notes / rollback:** The deploy + live-key migration was done by the main session (never a
subagent): live keys/config backed up to `~/Library/Application Support/Anki2/omnia-backups/
restructure-*`, then restored into the deployed `.secrets/`+`config/`; the piper venv was moved
(not rebuilt) into the deployed `user_files/`. `_ADDON_DIR` MUST resolve the directory not the file
(a symlinked `__init__.py` would otherwise chase back to `src/omnia`). The changed-edge diff is JS-
canonical; no Python twin. (Earlier same-feature commits: 0a4d140/6891dcf/d6e0e1a/9c03a82/5a33782/
f05e7f1 for the two-way sync + envs move.)

## 2026-06-30 — Smart Notes two-way prompt↔graph sync + Dependencies UI redesign

**What:** Extended the field dependency graph into a TWO-WAY sync between each field's prompt and
the graph, plus a redesigned canvas. (1) prompt→graph: when a prompt changes (Save / Auto-prompt /
Improve-all) an off-thread LLM (temp 0) classifies each `{{ref}}` hard/soft and recolours the graph;
only NEW refs are classified (existing/user kinds preserved), persisted as `auto=True` so they
survive recompute. (2) graph→prompt: editing edges then "↻ Sync prompts" rewrites each changed
node's prompt via the LLM, shown in an old→new diff popover (edit / ✨ Improve), gated by a live
consistency check so a hand-edit can never break the node's graph; Apply pre-checks cycles, writes
prompt + deps in lockstep, and `_on_save` refuses to persist a cyclic config. (3) UI: a balanced
grid-wrapped `flow_layout` (no more single column), pan/zoom, connector-handle-vs-body gestures,
gradient edges + CSS animations (no SVG filters — Metal hazard).
**Why:** The graph had to reflect the *current* prompts (and whether each dependency is truly
required vs merely helpful — an LLM-only judgement), and edits to the graph had to flow back into
the prompts; the old single-column SVG was unusable at ~38 fields.
**Files:** `engine/consistency.py` (NEW — the shared seam both directions validate through:
`NodeEdgeSet.derive/.diff`, `validate_prompt_syntax`); `engine/graph.py` (`FieldGraph` OOP +
`flow_layout`); `engine/rules.py` (`compile_field_rule`, `reconcile_field_deps`); `config.py`
(`FieldDep.auto`); `authoring/{author,models,persona}.py` (classifier + edge-rewrite + popover
improve, guard-railed; batched classify); `gui/smart_notes/dialog.py` (ops: classify_deps,
validate_prompt, rewrite_edges, improve_prompt_pinned + save-cycle backstop); `gui/smart_notes/
html.py` (graph_payload x/y/bounds, cycle_error_for_config); `web/06-graph.js` (canvas + queue +
popover), `web/{04-modal,05-handlers}.js`, `page.html`, `page.css`. Many tests across
`tests/plugins/test_smart_notes*` + `tests/gui/test_smart_notes_*`.
**How to verify:** `.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"` (784 passed);
`cat src/omnia/gui/smart_notes/web/0*.js | node --check -`. In Anki (LLM key): edit a prompt → graph
recolours; drag/click/Delete edges → ↻ Sync prompts → diff popover; Apply gated by validity.
**Notes / rollback:** Built in 5 reviewed phases (8d0e4dc, 6891dcf, d6e0e1a, 9c03a82, 5a33782; envs
move f05e7f1). B1 (soft survives recompute via explicit auto entry), B2 (classifier never flips a
user/existing kind), B3 (empty prompt → no incoming edge). The changed-edge diff is client-side only
(canonical `diffEdges` in JS — the lazy queue needs live rows); no Python twin. Off-thread eval_js
is a native Qt segfault, so every push is from the QueryOp success callback. See
`docs/smartnote_graph.md` (Vietnamese) Part II for the full design.

## 2026-06-30 — Smart Notes field dependency graph (explicit, visual, generation-aware)

**What:** The previously-implicit field generation DAG (derived from prompt `{{Field}}` refs)
is now explicit, editable, and enforced. Each field carries `depends_on: [FieldDep{field,
kind}]`; the effective graph is the derived `{{ref}}` edges (default **hard**) UNIONed with
explicit edges, where an explicit entry overrides a derived edge's kind. **Hard** edges order
AND block (a dependent whose hard prerequisite is empty/blocked is skipped + reported);
**soft** edges only order. A new "Fields ⇄ Dependencies" view in the Smart Notes dialog renders
the graph as SVG (drag node→node to add a hard edge, click an edge to toggle hard↔soft, Delete
to remove; client-side cycle precheck). Auto-prompt now also emits `depends_on` per field —
proposing the graph when empty, filling gaps without clobbering user edits when not.
**Why:** Some fields can only be generated once others exist (e.g. a definition needs the word);
generation needed to respect and visualise that, and to stop producing garbage from missing
prerequisites.
**Files:** engine — `plugins/smart_notes/engine/graph.py` (new: `build_field_graph`,
`validate_acyclic`, `would_create_cycle`, `layered_layout`), `engine/rules.py`
(`rule_prerequisites` — single source of truth), `engine/ordering.py`, `engine/service.py`
(`BlockedField`, `generate_note` → `(results, blocked)`); model — `config.py` (`FieldDep` +
`depends_on`); auto-prompt — `authoring/models.py` (`AutoSmartDep`), `authoring/author.py`
(build/parse/apply + `existing_deps`); GUI — `gui/smart_notes/html.py` (`graph_payload`),
`dialog.py` (`graph_recompute` op), `web/06-graph.js` (new SVG editor; `06-init.js`→`07-init.js`),
`web/03-render.js`/`05-handlers.js`/`page.html`/`page.css`. Tests: `tests/plugins/
test_smart_notes_graph.py` (new) + additions to `test_smart_notes.py` and
`tests/gui/test_smart_notes_html.py`.
**How to verify:** `.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"` (691 passed);
`cat src/omnia/gui/smart_notes/web/0*.js | node --check -`; in Anki: Smart Notes → pick a note
type → **Dependencies** → drag/click/Delete edges, Save, reopen (edges persist). Auto-prompt on
an empty graph proposes edges.
**Notes / rollback:** Built in 3 phases (engine `0a4d140`, GUI `1e9481c`, auto-prompt). A field
that *generated this run* (incl. image/tts not chained into the text map) counts as a satisfied
prerequisite — only a genuinely blocked/never-produced prereq blocks its hard dependents.
Layout is always computed in Python (`graph_recompute`); the JS never re-implements longest-path.
No new ADR (internal smart_notes change); existing configs derive a graph for free from their
prompts.

## 2026-06-30 — Native-runtime providers via add-on-managed sidecar venvs (ADR-005)

**What:** Native-runtime TTS providers (piper→onnxruntime, viet-tts→PyTorch) now run as
out-of-process sidecars in an **add-on-managed, per-provider virtualenv**, instead of being
vendored or pip-installed into Anki's interpreter. New core seam `core/providers/native_runtime.py`:
`NativeRuntimeSpec` (`name`/`section`/`label`/`size_hint`/`pip_packages` + `mode="server"|"cli"`),
an injected `ProcessRunner`/`SubprocessRunner`, and `NativeRuntimeManager` (host-Python detection,
cross-platform venv paths, idempotent `ensure_installed`, `ensure_running`(server)/`run_in_venv`+
`run_capture`(cli), `uninstall`, `shutdown_all`, `{bin}`/`{python}`/`{host}`/`{port}` arg
substitution). A registry (`register_native_runtime` / `available_native_runtimes` /
`native_runtimes_by_section`) makes runtimes enumerable + grouped by section for the GUI.
**viet-tts** now drives the manager (server mode; base_url → the managed sidecar) and **piper**
runs piper-tts in the venv (cli mode; text on stdin → temp `.onnx` WAV). Synthesis NEVER
auto-installs — it `ensure_running`/`run_in_venv` and raises a clear "enable it in Advanced" error
when the runtime isn't installed. **GUI:** Smart Notes → Options → General → "Native runtimes"
panel lists runtimes grouped by section, each with size hint + status; ticking installs the venv
off-thread (`run_in_background`, progress pushed via `window.__snNativeRuntime{Progress,Done}`),
unticking deletes the venv immediately.

**Why:** native wheels (onnxruntime/torch) can't be vendored (per-OS/arch/ABI, huge — ADR-004)
and must not pollute Anki's frozen interpreter. A managed venv owns the native ABI (no coupling to
Anki) and isolates install/crash failures. See ADR-005 for the full rationale + alternatives.

**Files:** `core/providers/native_runtime.py` (new), `core/providers/tts/{viettts,piper}.py`
(migrated), `gui/smart_notes/{dialog.py,html.py,web/{page.html,page.css,01-bridge.js,05-handlers.js}}`
(Advanced panel + ops). Tests: `tests/providers/test_native_runtime.py` (new),
`tests/gui/test_smart_notes_native_runtimes.py` (new) + updates to `test_tts.py`/`test_sweep.py`.

**How to verify:** `.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"` (622 passed).
Real Anki: Options → General → Native runtimes → tick piper / viet-tts (downloads + installs into
a venv under `user_files/native_envs/<name>/`), then generate a sound field using that provider.

**Notes / rollback:** Bootstrap needs a host Python 3.x (system `python3`/`py`); packaged-Anki
users without one are blocked (Phase D — bundling python-build-standalone — is a future option).
First install is slow + network-heavy (viet-tts/torch ~GB). The manager + routing are unit-tested
via an injected fake `ProcessRunner`; the live venv-create/pip/sidecar path is real-Anki-only.
`core/envs.py:OMNIA_VIETTTS_STARTUP_TIMEOUT` is now unused (timeout moved into the manager) —
left in place, minor cleanup later.

## 2026-06-30 — Core structural refactors: network/ extraction, logging/ package, module-global loggers

**What:** Three cohesion refactors (no behavior change): (1) the generic transports
`http.py` + `websocket.py` moved out of `core/providers/` into **`core/network/`** (they're not
provider-specific); (2) `core/logging.py` + `core/diagnostics.py` collapsed into a **`core/logging/`
package** (`logger.py` + `diagnostics.py` + a new `session.py` — a run-capture `LoggingSession`/
`AsyncLoggingSession` ported from a prior project — re-exported from `__init__`); (3) loggers are now
**module-global** `logger = get_logger(...)` instead of `self._log`/`self._logger` class attributes
(matching `native_runtime.py`/`session.py`); the `StatsInjector` injected-logger param was dropped.

**Why:** group each concern into its own package (consistent with `reviewer/`, `providers/`,
`config/`); transports belong with the network layer, crash-diagnostics + run-capture belong with
logging; a module-global logger is the simpler, consistent idiom. (`core/{registry,plugin,manager}.py`
were considered for a `core/plugin_system/` group but deliberately kept flat — they're the
documented Seam #1; `anki_compat.py` likewise stays flat.)

**Files:** `core/network/{__init__,http,websocket}.py` (moved), `core/logging/{__init__,logger,
diagnostics,session}.py` (moved + new), + import updates across providers/GUI/tests and the
module-global-logger rewrite in ~8 modules (manager, web_dialog, smart_notes integration/GUI,
stats_injector). Docs: CLAUDE.md tree updated.

**How to verify:** `.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"` (622 passed);
`from omnia.core.network import HttpClient, WebSocketClient` and
`from omnia.core.logging import get_logger, install_crash_logger, get_logging_session` resolve.

**Notes / rollback:** Pure relocations + re-exports — every old `from omnia.core.logging import
get_logger` still works (package `__init__` re-export). No public-behavior change.

## 2026-06-30 — Piper TTS: real runner + bundled Vietnamese voice (Git LFS)

**What:** The `piper` provider now actually synthesizes. `PiperVoiceRunner` (the default
`PiperRunner`) loads a local `.onnx` voice via the user-installed `piper-tts` package and returns
WAV; a missing install degrades to a clear, actionable `ProviderError` (like viet-tts). A
Vietnamese voice (`vi_VN-vais1000-medium`, from a prior project) is bundled under
`src/omnia/models/piper/` and is piper's `CURATED_VOICES` entry, so it appears in the voice
pickers + the Auto-detect map (`piper:vi_VN-vais1000-medium` → vi). `PiperTTS._resolve_model_path`
maps a bundled voice NAME → `models/piper/<name>.onnx` (or uses an absolute `.onnx` path).

**Why:** Close the piper follow-up from the Auto-detect-voices work — piper was a stub
(`UnavailablePiperRunner` always raised). It's now a working offline provider for users who
`pip install piper-tts`.

**Files:** `core/providers/tts/piper.py` (real runner + path resolution + voices),
`src/omnia/models/piper/{vi_VN-vais1000-medium.onnx,.onnx.json,README.md}` (new), `.gitattributes`
(new — LFS), tests in `tests/providers/{test_sweep,test_catalog,test_tts}.py` + `tests/conftest.py`
(piper real-sweep skips when `piper-tts` is absent).

**How to verify:** `.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"` (556 passed).
In real Anki with `pip install piper-tts`: map a language → `piper · …` in Sound → Auto-detect
voices (or pin the piper voice on a field) and generate.

**Notes / rollback:** `.onnx` models are **Git LFS** (`.gitattributes`:
`src/omnia/models/**/*.onnx filter=lfs …`) — regular git would bloat the repo + trip the 5 MB
pre-commit hook; commit `.gitattributes` alongside the model, and the clone side needs
`git lfs pull`. `piper-tts`/`onnxruntime` are native and intentionally NOT vendored/installed in
the dev venv (Anki-runtime emulation), so the runner is exercised in tests via an injected fake;
the live path is real-Anki only. The `.ankiaddon` build includes the ~60 MB model (size tradeoff
of shipping a bundled voice).

## 2026-06-29 — Auto-detect voices (global lang→provider:voice map) + provider-layer refactor

**What:** A Smart Notes **sound** field can be left on **"Auto-detect"** (the Voice dropdown's
default): at generation the content language `L` is detected (existing LLM detector) and the
voice is resolved through a NEW global, cross-provider table `[tts.auto_voices]`
(`lang -> "provider:voice"`). One field can thus read English or Vietnamese content with the
right voice per note. The Account → Sound tab gained an **"Auto-detect voices"** editor (one
row per language, a cross-provider `{provider}·{voice}` dropdown, incl. a free
`google_translate` option for every language) + a **↻ Refresh voices** button that pulls
edge_tts's full real voice list. Unmapped language or a runtime synth failure raises a clear,
noticeable `ProviderError` (the user re-picks). The per-field Language picker was removed (a
voice fixes the language, else auto-detect); the language-detection code is retained.
Shipped with two architecture refactors: (1) **voices live with providers** —
`TTSProvider.list_voices()` classmethod + per-provider `CURATED_VOICES`, aggregated in
`core/providers/tts/__init__`; `catalog.py` is now a functions-only aggregator (provider/model/
voice/language data moved into the `llm`/`tts` package `__init__`s); (2) **TTS factory → registry**
— `@register_tts(*names)` self-registration (mirrors `core/registry.py`) + per-provider
`from_config`; `tts/factory.py` deleted.

**Why:** Voice and language were two coupled controls (a concrete voice already encodes its
language); collapsing to one Voice control + a global auto-detect map removed the redundancy
and the "active provider can't serve this language" problem (each language points at a
provider that can). The refactors fix leaked abstractions: the GUI no longer imports a concrete
provider for refresh, and the voice catalog/registry now cohere with the provider layer.

**Decoupling invariant (protect it):** the `auto_voices` map is the SOURCE OF TRUTH for
generation — `resolve_auto_voice` only splits the stored `"provider:voice"` string and synth
uses the voice id directly; the catalog/fetched-voice list is NEVER consulted at synthesis
time. Refresh updates only the voice catalog/cache, never the map; the dropdown preserves an
out-of-options saved value as `… (saved)`; no load/refresh validation pass.

**Files:** `core/config/models.py` (`TTSSettings.auto_voices`), `core/config/repository.py`
(`set_auto_voice`), `core/providers/__init__.py` (`split_provider_voice`, `tts(provider=)`,
`_tts_config(provider=)`, `resolve_auto_voice`), `core/providers/tts/{base,registry,edge_tts,
google_cloud,google_translate,openai_compatible,piper,viettts,__init__}.py`,
`core/providers/voice_cache.py` (new), `core/providers/catalog.py` (functions-only),
`core/providers/llm/__init__.py` (provider/model data), `plugins/smart_notes/engine/generators.py`,
`gui/smart_notes/dialog.py` (`set_auto_voice`/`refresh_voices`), `gui/smart_notes/web/{03-render,
05-handlers,01-bridge}.js`, `page.html`, `page.css`. Tests across `tests/providers/*`,
`tests/core/test_config.py`, `tests/plugins/test_smart_notes*.py`, `tests/gui/test_smart_notes_html.py`.
`tts/factory.py` DELETED.

**How to verify:** `.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"` (552 passed);
`node --check` on concatenated `gui/smart_notes/web/0*.js`; ruff/black/isort clean. In real Anki:
Smart Notes → a sound field → Voice = "Auto-detect"; ⚙ Options → Usage & Keys → Sound → map a
few languages, hit ↻ Refresh voices, generate a note with mixed-language content.

**Notes / rollback:** edge_tts voice fetch is the only live one (keyless); google_cloud stays on
its curated seed (needs auth). **piper is intentionally voiceless** — its follow-up bundles the
vi `.onnx` + a native runner, after which it joins `list_voices()` like any other provider.
LLM factory left on its old mechanism (registry treatment is an optional follow-up for
symmetry). Reviewer pass run post-merge.

## 2026-06-29 — Secrets out of config + Account/Keys bug-fix pass

**What:** Provider credentials no longer sit as plaintext in `providers.toml`. A new
`core/config/secrets.py::SecretsStore` keeps the real values in the gitignored
`src/omnia/secrets/` dir; the config holds only a reference — `secret:<name>` (the value IS a
file's content: api keys, access tokens) or `secret-file:<name>` (the field is a path: the
Vertex service-account JSON). `ConfigRepository` resolves these to real values after every
load, so providers/the hub are unchanged; the Keys-subtab Save (`set_provider_fields`) and
Browse (`set_provider_credential_file`) write the other way (value → secret file + ref), so a
secret never lands back in the TOML. Non-secret fields (`project`, `location`, models) stay
inline. Plus fixes to the Account/Keys UI: one Save per provider card; Keys↔kind pane overlap
(`display:flex` was overriding `[hidden]`); generated images now render in the playground +
modal preview (`data:` URI); per-kind playground state (input + result no longer bleed across
Text/Image/Sound) with a sound "Play again" button; OpenRouter credit re-checks after a key
Save and handles a `total=0` balance.

**Why:** Opening `providers.toml` (or pasting it in a bug report) leaked live keys. There is
NO Omnia account/login — authorization is purely the per-provider API key / Google
service-account the user supplies — so the only safe place for those secrets is an
out-of-config, gitignored store the config merely points at.

**Files:** `core/config/secrets.py` (new), `core/config/{repository,loader}.py`,
`core/anki_compat.py` (`play_audio` returns path, `replay_audio_file`),
`gui/smart_notes/dialog.py` (ops `set_secrets`/`browse_file`/`replay_audio`; image `data:`
URI), `gui/smart_notes/web/{page.html,page.css,04-modal.js,05-handlers.js}`. Tests:
`tests/core/test_secrets.py` (new), `tests/core/test_config.py::TestSecretsOutOfConfig`.

**How to verify:** `.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"`
(479 passed); `node --check` on the concatenated `web/0*.js`. In Anki: 🔑 Keys → reveal a key,
edit + Save (one button), Browse the Vertex JSON (it copies into `secrets/`); confirm
`providers.toml` shows only `secret:`/`secret-file:` refs. Image playground shows the picture;
switching Text/Image/Sound keeps each prompt separate.

**Notes / rollback:** A one-off migration moved the existing live `gemini`/`openrouter` keys
and the Vertex JSON into `secrets/` and rewrote `providers.toml` to refs (idempotent;
`scratchpad/migrate_secrets.py`). Secrets are machine-local by design — AnkiWeb syncs only the
collection, never add-on files — so moving machines still means copying `secrets/` +
`providers.toml` (never sync keys through the collection). A `secret:` whose file is missing
resolves to `""` → the provider's clear "missing api_key" error.

## 2026-06-29 — Smart Notes Account tab: default-model picker + Keys/Secrets subtab

**What:** Two additions to the ⚙ Options → **Account** dialog, plus polish.
- **Default-model picker** (per Text/Image/Sound subtab): a provider + model/voice picker that
  edits the central `[llm]`/`[tts]` active provider+model — the default that drives
  detect-language, ✨ Auto-prompt (renamed from "Auto-smart"; op name unchanged), ✦ Improve, and
  any field left on "(inherit)". Persists to `providers.toml`.
- **Keys/Secrets subtab** (`🔑 Keys`): one card per managed LLM provider with masked credential
  fields, an 👁 eye reveal, inline Save, a native **Browse…** for the Vertex service-account JSON,
  and a console link. Quota is **honest**: a real %/credit bar only for OpenRouter (the one
  provider that exposes a balance to an API key); a plain note for the rest (Vertex's $300 free
  credit lives in the GCP Console and is not fetchable from a key). At ≤0 OpenRouter credit a red
  "top up" button appears.
- Polish: centered table column headers; multi-line hover tooltips on the General-tab options.

**Why:** Let the user manage *which* model is the default and *their keys/quota* from inside the
dialog, without hand-editing `providers.toml` — while staying truthful about which quotas are
actually fetchable (the user assumed all providers expose quota; only OpenRouter does).

**Files:**
- Pure logic: `plugins/smart_notes/account.py` (`default_models`, `key_cards`).
- Seam writes: `core/config/repository.py` (`set_active_llm`, `set_active_tts`,
  `set_provider_secret`, `_tts_supports_voice`); `core/anki_compat.py` (`pick_file`,
  `open_external_url`).
- Glue/UI: `gui/smart_notes/dialog.py` (ops `set_default_model`, `account_keys`,
  `account_keys_credit`, `set_secret`, `browse_file`, `open_url`); `gui/smart_notes/html.py`
  (op docs); `gui/smart_notes/web/{page.html,page.css,01-bridge.js,05-handlers.js}`.
- Tests: `tests/plugins/test_smart_notes_account.py` (`TestDefaultModels`, `TestKeyCards`),
  `tests/core/test_config.py` (`TestProviderConfigWrites`).

**How to verify:** `.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"` (465 passed);
`node --check` on the concatenated `web/0*.js`. In Anki: ⚙ Options → Account → pick a default
model (persists), then 🔑 Keys → reveal/Save a key, Browse the Vertex JSON, watch the OpenRouter
bar.

**Notes / rollback:** GUI not verifiable headless (no `aqt`). Reveal is local (no "account"
gate — that remains a separate, unbuilt feature). `set_active_tts` deliberately skips writing a
`voice` for voiceless TTS providers (e.g. google_translate) whose strict model would reject it.

## 2026-06-29 — Smart Notes: rules in the collection DB (synced) + per-note-type Deck scope

**What:** Two related changes to the Smart Notes feature.
- **Rules → collection DB**: the per-note-type `SmartNotesSettings` now persists in the Anki
  collection via `col.get_config`/`col.set_config` (key `omnia:smart_notes`) through a new
  `integration/store.py::SmartNotesStore`, so the rules **sync across devices** (AnkiWeb).
  Provider config ([llm]/[tts]) stays in the TOML config. `ReviewTimeEvaluator` now takes a
  `settings_provider` callable and reads FRESH each card; the plugin's `_settings()` and the
  dialog's load/save go through the store.
- **Deck scope**: `SmartNotesNoteTypeConfig.decks: list[int]` ([] = all decks). A pure
  `engine.applies_to_deck(config, deck_id)` gates generation; `anki_compat.note_deck_ids`
  feeds it. Batch skips out-of-scope notes (counted as skipped); review skips out-of-scope
  cards. The dialog gets a **Decks picker** (toolbar button → popover: "All decks" master +
  per-deck checkboxes; baked `all_decks`), and `decks` rides the save/auto/improve payloads.

**Why:** Smart Notes config had to survive a restart and travel with Anki's own sync, which
means living in the collection database rather than a file; and a note type's generation had to
be scopeable to specific decks (All, or a ticked subset).

**Files:** `plugins/smart_notes/integration/{store.py,__init__.py,review.py,batch.py}`,
`plugins/smart_notes/__init__.py`, `plugins/smart_notes/config.py`,
`plugins/smart_notes/engine/{rules.py,__init__.py}`, `core/anki_compat.py`,
`gui/smart_notes/{dialog.py,html.py,web/*}`; tests `test_smart_notes_store.py` + deck/review/
batch/html test additions; `scripts/ui_smoke.py` (round-trips via the store).

**How to verify:** `.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"` → 415 passed;
`node --check` on the concatenated page JS clean. Real Anki: open Tools → Omnia → Configure on
Smart Notes, save rules, restart Anki → rules persist (now in the collection, not TOML); tick a
deck subset and confirm batch/review only generate there.

**Notes / rollback:** Rules moved OUT of the TOML config into the collection — a user with old
TOML-persisted rules would need to re-save once (there were none in practice). Deck ids are
collection-local (fine, the rules live in the same synced collection).

## 2026-06-29 — Config: direct-edit domain files (no override layer) + external secrets/

**What:** Replaced the "bundled defaults + single gitignored override" config model with
**direct-edit domain files**. `config/` now holds three LIVE files the user edits directly and
the add-on writes back to — `omnia.toml` (log_level + [plugins.*] enabled), `features.toml`
(per-feature settings), `providers.toml` ([llm]/[tts]) — each gitignored, with a tracked
`*.example.toml` template copied to the live name on first run (`ConfigLoader.ensure_live_files`).
Writes route to the owning file via `ConfigRepository._file_for` (plugins→omnia, features→
features, llm/tts→providers). Credential FILES live in a new top-level `src/omnia/secrets/`
(gitignored except README). No more `user_files/omnia.toml` override (user_files keeps only
logs + plugin state). `ConfigLoader` is now single-arg (`config_dir`); `read_overrides`/
`save_overrides`/`_load_defaults` removed in favour of `read_file`/`write_file`/`load_merged`.

**Why:** User wanted all config under `config/` with secrets external, and found the override
file redundant (it mirrored providers.toml/features.toml structure). The override layer's only
real benefit (surviving a packaged-add-on update) doesn't matter for the dev (symlink) workflow.

**Files:** `core/config/loader.py`, `core/config/repository.py`, `core/config/__init__.py`,
`__init__.py` (bootstrap), `.gitignore`, `tests/conftest.py`, `tests/core/test_config.py`,
`tests/core/test_manager.py`, `scripts/ui_smoke.py`; `config/*.example.toml` (templates),
`config/*.toml` (live, gitignored), `src/omnia/secrets/README.md`.

**How to verify:** `.venv/bin/python -m pytest tests/ -q -m "not llm and not tts"` → 395 passed;
fresh tmp dir seeded with only `*.example.toml` → ConfigLoader creates the live files + load
works + set_enabled/update_section round-trip to the right file. ruff/black clean.

**Notes / rollback:** CAVEAT — `config/` is overwritten on a packaged-add-on UPDATE (only
`user_files/` survives); fine for a dev symlink install. Credentials now live in
`config/providers.toml` (+ key files in `secrets/`); tests read them only via the deselected
`@llm`/`@tts` markers. To run live tests: `OMNIA_TEST_CONFIG` now names a config DIRECTORY.

## 2026-06-28 — Smart Notes table redesign + provider catalog + engine/authoring restructure

**What:** A large reworking of the Smart Notes feature, in three parts.
- **GUI redesign** (`gui/smart_notes/web/*` rebuilt as a 6-part IIFE + `dialog.py`/`html.py`): the
  per-field table now has separate **On** (generate?) and **Lock** (freeze + blur + skip
  Auto-smart/Improve, and actually disables editing) columns; the **Prompt** is edited in a popup
  (not inline); **Provider/Model/Voice** are kind-aware dropdowns (text/image → gemini/gemini_vertex/
  openrouter + curated model lists; tts → edge_tts/google_cloud/google_translate/piper + a Voice
  picker with "(auto-detect language)"); a **Preview ▶** per row + a global **Improve all**; clearer
  Auto-smart messaging (reports count / "nothing to fill"). Three new pycmd ops: `improve_prompt`,
  `improve_all`, `preview` (all off-thread, pushed back via `window.__sn*Result`).
- **New backend**: `core/providers/catalog.py` (curated models/voices baked into the page as
  `window.__SN_CATALOG`); `plugins/smart_notes/authoring/` (the **Anki Flashcard Expert** system
  prompt + `PromptAuthor`: auto_smart / improve / improve_all); TTS **language auto-detection** —
  with no pinned voice, the spoken text's language is detected (best-effort) so the voice matches.
- **Restructure** (`plugins/smart_notes/` flat → subpackages): `engine/` (pure: service +
  Generator-strategy generators + interpolation/rules/ordering/markdown/language), `authoring/`
  (pure: persona/author/models), `integration/` (impure Anki glue: batch/editor/field_menu/review).
  Three OOP/SOLID abstractions added: **Generator** strategy (Open/Closed — kind → class), **PromptAuthor**
  (DIP over LLMProvider), **LanguageDetector** (injected, best-effort).

**Why:** User `/goal` (9 items) to fix the Smart Notes config UX (the redesign) + a follow-up request
to group the growing plugin by concern with real class abstraction (the restructure).

**Files:** `core/providers/catalog.py` (new); `plugins/smart_notes/{engine,authoring,integration}/*`
(restructure); `gui/smart_notes/{dialog.py,html.py,web/01-bridge..06-init.js,page.html,page.css}`;
`gui/config_form.py` (bolder (i) icon); tests `tests/providers/test_catalog.py`,
`tests/plugins/test_smart_notes_{authoring,language}.py`, additions to `tests/gui/test_smart_notes_html.py`;
`scripts/ui_smoke.py` (new ops). Coupling kept: `engine/`+`authoring/` import no `aqt`/`anki`.

**How to verify:** `.venv/bin/python -m pytest tests/ -q` (430 passed, 66 skipped, 11 xfailed);
`.venv/bin/ruff check src/omnia tests` clean; JS concatenation `node --check` clean. Real-webengine:
`QT_QPA_PLATFORM=offscreen <Anki-python> scripts/ui_smoke.py` (needs aqt — run with Anki's interpreter),
then open Tools → Omnia → Configure on Smart Notes in real Anki to confirm the redesigned table paints.

**Notes / rollback:** The Lock column maps to the existing `prompt_locked` field and On to `enabled`
(no model migration). Curated model/voice lists are best-effort defaults; the GUI always preserves a
user's saved model/voice even if absent from the list. Language detection is on by default
(`GenerationService(..., detect_tts_language=True)`) and fully guarded (any failure → provider default).

## 2026-06-28 — Architecture refactor batch (rename, pydantic v1, per-plugin config, gui layout)

**What:** A user-requested structural cleanup, done in safe validated stages.
- **Rename** `src/omnia/features/` → `src/omnia/plugins/` (+ `tests/features`→`tests/plugins`); all
  imports/docstrings updated. (commit fec1f41)
- **Pydantic v2 → pure-Python v1**: v1.10 has a pure-Python core (verified `compiled=False` on
  cp313), so the per-OS binary `pydantic_core` vendoring is GONE — one pure-Python `vendor/universal/`
  copy ships on every OS. Migrated the v2 API (field_validator→validator, ConfigDict→class Config,
  model_dump→dict, model_validate→parse_obj, …); `vendor_deps.py` now installs `--no-binary pydantic`.
  (commit 9281c60)
- **One config class per plugin**: each plugin owns its Pydantic settings model in
  `plugins/<plugin>/config.py` with `Field(description=…)` (→ tooltips); the GUI form auto-derives via
  `core/config/schema.py:schema_from_model()`, removing the duplicate `config_schema()` ConfigField
  lists. Coupling stays clean — `core/config` never imports plugins; `feature_settings()` resolves the
  model via the registry. (commit 09ec658)
- **GUI per-plugin folders + assets-in-files**: `gui/<plugin>/` for plugin-specific dialogs;
  ALL embedded JS/HTML/CSS extracted from Python into asset files under `gui/<area>/web/`, loaded by
  `gui/assets.py` (`read_asset`/`read_assets`); the two big panels split into ordered pieces
  (typed_accuracy 5, smart_notes 4), concatenated byte-equivalent. (commits 5f12750, ebb0d97)

**Why:** user directives to make the layout cleaner, kill the binary-vendoring complexity, and stop
embedding markup in Python.
**How to verify:** `pytest tests/ -q` (388 pass); `vendor_deps.py` produces zero `.so`/`.pyd`;
offscreen `ui_smoke.py` + the real-Anki dialog-render harness show the Smart Notes table populated
(94 note types, 14 rows) from the new split asset paths.
**Notes / rollback:** v1 is maintenance-mode (accepted trade for pure-Python portability). Each refactor
is its own commit, individually validated, so any stage can be reverted in isolation.

## 2026-06-28 — Smart Notes redesign: note-type table + AI auto-smart + grouped webview UI

**What:** Reworked the Omnia settings UI and Smart Notes around the user's spec.
- **Settings UI**: rebuilt as an `AnkiWebView`-hosted HTML/CSS/JS page (new reusable
  `gui/web_dialog.py` `WebDialog` seam) — grouped (Reviewing / Grading / AI), gradient header,
  animated toggle switches, light/dark, hover tooltips. The Grading tooltip states that Typing
  Accuracy + Overdue Guard COOPERATE (typing grades, overdue caps) and need NOT be mutually
  exclusive — answering the "do they conflict?" question in the UI.
- **Smart Notes**: note-type-centric model — one BASE field (a word OR phrase, never generated)
  + per-field config (enabled, type text/tts/image, prompt referencing {{Base}}+others,
  prompt_locked, provider, model, voice, overwrite); prompts form a DAG. New `auto_smart.py`:
  the LLM ("senior language master") infers each enabled, non-locked field's type + prompt from
  its name + the base, so the user needn't hand-write prompts. New webview table dialog
  (`smart_notes_dialog.py` + pure `smart_notes_html.py`): note-type/base-field selectors,
  editable rows (On/Type/Prompt+🔒/Provider/Model/Overwrite), Create field (mutates the note
  type), ✨ Auto-smart, Save.

**Why:** `/goal` — group features, beautify the UI (gradient/animation/tooltip), and make Smart
Notes a per-note-type table that automates prompt/type authoring.
**Files:** `gui/{web_dialog,settings_html,settings_dialog,smart_notes_dialog,smart_notes_html}.py`,
`core/plugin.py` (group/tooltip), `core/manager.py` (grouping), `features/smart_notes/{auto_smart,logic,__init__,batch,review_evaluator,field_menu}.py`, `core/config/models.py`, `core/anki_compat.py` (add_note_type_field), tests.
**How to verify:** `pytest tests/ -q` (~373 pass); offscreen `ui_smoke.py` builds both webview
dialogs and round-trips toggle/load/save through the bridge.
**Notes / rollback:** Commits 388ef9b (settings UI + seam), 42f1579 (smart_notes). Conflict
answer: ease pipeline composes typed_accuracy@100 → overdue_guard@200, so they don't conflict.

## 2026-06-28 — IDEAS / roadmap for Smart Notes (for later — user will review)

Per the user's request to propose ideas freely and log them here (not implemented unless noted):

- **Web/PDF clipper → AnkiConnect → auto-gen** (BEING SCAFFOLDED in `3rdparty/`): double-click a
  word/phrase in the browser → "+" tooltip → send word + **context** (containing sentence /
  paragraph) via AnkiConnect `addNote` into a capture deck → Omnia auto-fills the rest at review
  time. Context matters: a word's card is far richer when the source sentence is captured.
- **Context-aware generation**: let a prompt reference a `{{Context}}`/`{{Sentence}}` field so
  meaning/example/translation are disambiguated by the real usage the learner saw (already
  supported by the multi-field DAG — just needs a Context field on the note type).
- **Per-language presets**: ship auto-smart presets ("English vocab", "Japanese kanji", "German
  noun+article", "IPA + audio") so a new note type is one click to a full pipeline.
- **Cloze example sentences**: generate an example and auto-cloze the target word/phrase.
- **Pronunciation/IPA + TTS pairing**: an "IPA" text field + an "Audio" tts field of the base,
  auto-detected by auto-smart (already type-infers tts for Audio/Pronunciation fields).
- **Image mnemonics**: image field generated from the meaning, not just the word, for abstract terms.
- **Disambiguation / sense selection**: when a word has multiple senses, use the captured context
  to pick the right sense before generating.
- **Cost/preview guard**: a per-note "preview" (Test with random note) before batch-generating a
  whole deck, with an estimated provider-call count.

## 2026-06-28 — Faithfulness audit + faithful re-port of all 3 reference features

**What:** A 3-agent faithfulness audit  found every feature
had substituted a simpler surface for what its reference add-on actually does; re-ported all
24 items faithfully, keeping the deliberate Omnia improvements (cooperative ease pipeline,
SVG countdown ring, central provider config). typed_accuracy: SQLite `typed_answer_log`
(4-way result, attempts vs unique-last, subdeck/time queries) + the interactive donut panel
JS-injected into the **Statistics** screen (was a static deck-overview table); answer side
retry-polls + forces Hard on empty. auto_flip: audio-aware arming, filtered-deck fix
(`odid or did`), Ctrl+J toggle, two-stage Enter cancel, mpv `--range`, use_global/use_deck.
smart_notes: DAG/chained fields + cycle detection, Markdown→HTML, per-field TTS voice, rich
per-rule PromptDialog, field menu + custom palettes, cancellable deck batch, review-time gen.
**Why:** `/goal` faithful adaptation of the (now removed) reference checkouts; user demonstrated the typed_accuracy
stats-screen gap, a full audit confirmed it was systemic.
**Files:** `src/omnia/features/{typed_accuracy,display_interval,auto_flip,smart_notes}/**`,
`src/omnia/gui/smart_notes_{dialog,prompt_dialog,custom_prompt}.py`, `core/anki_compat.py`,
`core/reviewer/*`.
**How to verify:** `pytest tests/ -q` (offline ~270 pass); `QT_QPA_PLATFORM=offscreen
"<AnkiPyenv>/bin/python" scripts/ui_smoke.py` (all hooks/dialogs incl. PromptDialog).
**Notes / rollback:** Commits 0c09f6d, 9dac893, 35d2fa0, ae75cba. Documented deferrals:
auto_flip native Deck-Options tab (Svelte, version-fragile → kept gear dialog); review-time
generation uses the safe current-card variant, not scheduler lookahead.

## 2026-06-28 — Provider corrections (a prior project-aligned) + per-OS vendoring + TOML config

**What:** (1) `model` fixed only at provider construction — removed the per-call `model=`
param; per-rule overrides build a provider via `ProviderHub.llm(*, model, provider)`. (2)
Gemini `generate_image` uses a prior project's wire shape (`responseModalities ["TEXT","IMAGE"]` +
`inlineData` base64). (3) Removed ALL subprocess/CLI (gcloud token fallback, piper shell-out).
(4) Vendor split into `universal/` + per-OS dirs (`mac_arm64/mac_x64/win_x64/linux_x64`) with
a `platform`-aware loader so the add-on runs on Windows + Mac Intel, not just Mac ARM. (5)
Defaults unified on TOML (`omnia.yaml`→`omnia.toml`); PyYAML dropped from vendoring.
**Why:** explicit user direction on provider design, cross-platform (Windows) support, config clarity.
**Files:** `core/providers/**`, `token_source.py`, `tts/piper.py`, `omnia/__init__.py`,
`scripts/vendor_deps.py`, `src/omnia/vendor/**`, `core/config/loader.py`, `config/omnia.toml`.
**How to verify:** `pytest tests/providers -q`; `python scripts/vendor_deps.py` (idempotent,
repopulates all 4 per-OS dirs); pydantic v2 imports under cp313 from `universal`+`mac_arm64`.
**Notes / rollback:** Commits c42c1b5, 35d2fa0, c447b63. piper now needs a vendored/injected
runner (no shell-out); the default raises a clear error.

## 2026-06-28 — Bespoke per-feature UIs + plugin config seam (+ git hygiene)

**What:** Built the three reference-parity UIs and the seam they needed. (1) **Plugin seam**:
`PluginContext` now carries the `ConfigRepository` + a `reload_self()` callback (so a plugin can
persist choices from its OWN in-Anki UI and re-apply), and `FeaturePlugin.custom_config_dialog()`
lets a feature supply a bespoke settings dialog (routed by the settings dialog when present).
(2) **smart_notes**: an editor ✨ button generates the configured field rules for the current
note (shared generation core with the Browser action, off-thread), and a `SmartNotesDialog`
(QTableWidget of rules) is the bespoke config dialog. (3) **typed_accuracy**: a JSON-backed
`StatsStore` records each typed result; the deck overview renders an inline-SVG donut +
pass-rate/avg-accuracy card (`overview_will_render_content`), gated by `show_stats`. (4)
**auto_flip**: a self-contained SVG countdown overlay (pushed via `anki_compat.reviewer_eval`
from the single `_schedule`/`_cancel` chokepoints, gated by `show_timer`) + a per-deck options
dialog from the deck gear menu (`deck_browser_will_show_options_menu`) writing `AutoFlipDeckOverride`
via `ctx.config` + `reload_self`. Pure logic (row↔rule mapping, plan building, stats summarize/
donut/card, countdown JS, effective_delays) is unit-tested.
**Why:** Reach feature parity with the reference add-ons (smart-notes editor button, typed-accuracy
stats, auto-flip countdown + deck options); the seam was the prerequisite.
**Files:** `core/plugin.py` (PluginContext fields + custom_config_dialog), `core/manager.py`,
`gui/settings_dialog.py`, `gui/smart_notes_dialog.py` (new), `features/smart_notes/{__init__,logic,
editor.py(new)}`, `features/typed_accuracy/{__init__,stats.py(new)}`, `features/auto_flip/{__init__,
logic,countdown.py(new),deck_options.py(new)}`, `tests/features/*`.
**How to verify:** `pytest -m "not llm and not tts and not integration"` (184 pass); Anki bundled
python imports all three UIs + `SmartNotesPlugin().has_custom_config_dialog()`; `.ankiaddon` builds.
The reviewer/overview/editor/deck-menu glue only runs inside a live Anki — import-verified + pure
logic tested headless; click-through needs a live Anki session.
**Notes / rollback:** `.claude/` (and other agent folders) are now gitignored + untracked per user
request — these working docs are LOCAL only, not committed.

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
(project <gcp-project-id>, gemini-2.5-flash).
**Notes / rollback:** Live `@llm` creds live ONLY in gitignored `src/omnia/user_files/omnia.toml`
(taken from the prior project's Vertex credentials) or `OMNIA_TEST_CONFIG` — never the tracked `providers.toml`,
so no secret is committed and CI without creds auto-skips. `embedding_model` is config-only
(reserved; no consumer yet).

## 2026-06-28 — More TTS providers, per-feature config GUI, vendoring + Anki-load verify, HTTP retry

**What:** (1) Added TTS providers from a prior project — **google_cloud** (REST, reuses the Vertex
`TokenSource`), **edge_tts** (injectable `EdgeSynthesizer`), **piper** (injectable
`PiperRunner`); TTS now has 7 providers, LLM 5. (2) The provider **sweep** now builds + runs
+ asserts non-empty output for EVERY llm/tts config. (3) Each feature declares
`config_schema()`; the settings dialog renders a generic **Configure** form (write-back +
live reload). (4) **Vendored** pydantic/pydantic_core(cp313)/PyYAML/tomli_w/rsa/pyasn1 into
`src/omnia/vendor` and verified the add-on **loads in real Anki 25.09.2** (all 5 plugins
register, config validates, GUI imports, all 8 gui_hooks exist). (5) Adapted a prior project's HTTP
**retry/backoff** into `UrllibHttpClient` via an injectable `RetryPolicy`. Moved `TokenSource`
to `core/providers/` (shared by gemini_vertex + google_cloud).
**Why:** User asked for more TTS types, a complete config sweep, real per-feature settings UI,
and to confirm the add-on runs in Anki; adapt valuable a prior project core.
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
**Why:** Make features thin and cooperative; adapt a prior project's provider design; OOP/SOLID.
**Files:** `src/omnia/core/**`, `src/omnia/config/**`, `src/omnia/__init__.py`,
`tests/core/**`, `tests/providers/**`.
**How to verify:** `.venv/bin/python -m pytest tests/ -m "not integration"`; provider sweep
covers every LLM/TTS provider (mocked); set `OMNIA_IT_VERTEX_PROJECT`/creds + `pytest -m
integration` for real Vertex.
**Notes / rollback:** Runs in Anki 3.13. `pydantic_core` (binary) + `rsa`/`pyasn1` (for
Vertex service-account auth) must be vendored per-platform — see requirements-vendor.txt.

*(Add feature entries below, newest first.)*
