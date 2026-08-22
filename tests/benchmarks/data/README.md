# Live-benchmark rows — the evidence behind the smart-notes generation defaults

Raw output of `tests/benchmarks/smart_notes_live.py --out <file>`, committed because four
tracked files cite these measurements as the reason for a shipped default. A default whose
evidence lives only in one machine's scratch directory cannot be audited, re-derived, or
re-checked when the next model changes the answer — so the rows live here, next to the harness
that produced them.

Every file is a JSON array of `ArmResult` rows. An arm is `<workers>x<K>`, where `K = 1` means
batching off. Re-derive any table in the shipped comments with, e.g.:

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

## What each session is, and what it does and does not establish

| file | workload | what it was for |
|---|---|---|
| `live_100notes_2026-08-22.json` | 100 notes, 19 generated fields each, 6 arms x 2 repeats | the main study |
| `live_20notes_repro_2026-08-22.json` | 20 notes, 5 arms x 2 repeats | an independent re-run of the same arms |
| `live_10notes_old_default_2026-08-22.json` | 10 notes, 3 arms x 1 | measuring the OLD default (1 worker, K=10) directly |

All three ran against the real Vertex endpoint (`gemini-3.6-flash`) on one Vertex project,
reading `AnkiVocabulary` notes from a read-only copy of the author's collection — the same notes
in the same order in every arm of a session — with the image field skipped in every arm. Total
measured arm time in `live_100notes` is 15,241.8 s (4 h 14 m), excluding setup and the warm-up
call.

**Established: the worker count.** 4 -> 8 workers is the one comparison whose ranges do not
overlap, at the same K, in both sessions that measured it:

| session | 4x1 | 8x1 |
|---|---|---|
| 100 notes | 1851.8-1958.4 s | 1210.4-1297.8 s |
| 20 notes | 370.9-393.7 s | 206.3-221.5 s |

The 10-note session measured the pre-concurrency default itself: `1x10` 167.1 s against `8x1`
110.1 s and `8x20` 100.0 s. That pairing (one worker, batching on) appears in none of the
100-note rows, so it is quoted from here and from nowhere else.

**Established: batching cuts requests.** At 8 workers over 100 notes: 1300 provider calls
ungrouped, 794 at K=10 (-39%), 574 at K=20 (-56%). The 20-note session reproduces the same
proportions: 260 ungrouped, a mean of 152.5 at K=10 (-41%) and 106 at K=20 (-59%).

**NOT established: what batching does to wall clock.** The two sessions disagree, and neither
can be dismissed — same harness, same collection, same settings, same account:

| arm | 100-note session | 20-note session |
|---|---|---|
| 8x1 | 1254.1 s (1210.4-1297.8) | 213.9 s (206.3-221.5) |
| 8x10 | 1162.5 s (1110.0-1214.9) | 476.5 s (435.4-517.6) |
| 8x20 | 1049.5 s (998.3-1100.7) | 215.2 s (175.0-255.4) |

Read as "grouping is mildly faster" in the first and "K=20 ties, K=10 is 2.2x slower" in the
second, with within-arm spread as wide as the between-arm gap. Two samples per arm cannot
characterise a network-bound run this variable. **The K effect on latency is unproven, in both
directions.** An earlier round of this work shipped a default and a UI tooltip on the strength
of one session of it; that is the mistake this directory exists to prevent.

## Reading the columns honestly

- **`contaminated_fields`** is a headword scan, and a weak one. Against a constructed neighbour
  swap on this deck it catches ~42% of deliberate mis-attributions — ~100% on fields whose right
  answer restates its headword, 0-12% on `Definition`, `Antonyms`, `Meaning (vi)`, part of speech
  and IPA. A flat bleed column means "not detected", never "did not happen". The harness now
  prints its own recall next to the number.
- **`http_429` / retries** cover the urllib providers only. Roughly a third of each run's
  provider calls are `edge_tts`, which speaks a WebSocket and never enters the HTTP client
  (`provider_calls - http_requests` is 198-201 in every 100-note row). Throttling there arrives
  as a socket timeout, and the study's only provider error was on exactly that path. "Zero 429s"
  is a statement about Vertex, on one generous account.
- **`fields_filled`** scores a half-length answer as a success. Batched answers measured about
  20% shorter than solo ones on the same fields, at K=10 and K=20 alike.
- **retries** are 4 across the twelve 100-note runs — one each in `4x1` rep 1, `4x10` rep 1,
  `8x20` rep 2 and `16x10` rep 1, every one a network error rather than a throttle.

## Reproducing

The harness reads real notes from a real collection and spends real provider money, so it is not
part of the test suite and CI never runs it. To re-run it you need an Anki collection containing
the note type (`--note-type`, default `AnkiVocabulary`), a `user_files/config` with working
provider credentials, and `--collection` pointed at your own collection file — the default is one
developer's path. Nothing is written back: `WriteGuard` blocks the `aqt`/`anki` imports and
denies every real `anki_compat` seam for the duration, and the collection is opened read-only
from a copy.
