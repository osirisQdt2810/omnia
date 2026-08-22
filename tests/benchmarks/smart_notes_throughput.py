"""Throughput benchmark for smart-notes generation — baseline vs LAYER 1 vs L2 vs L3.

LAYER 1 is bounded concurrency, LAYER 2 is prompt caching, LAYER 3 is K-note batching. They are
measured in one run because they answer different questions and each must be seen NOT to
disturb the ones before it: L1 moves the wall clock, L2 moves only how much of the input is
fresh, L3 moves the CALL COUNT — and none of them may change a single generated field.

Run it:

    python tests/benchmarks/smart_notes_throughput.py                 # the default matrix
    python tests/benchmarks/smart_notes_throughput.py --notes 20 --latency 0.02
    python tests/benchmarks/smart_notes_throughput.py --corrupt drop-one    # what a hostile provider costs
    python tests/benchmarks/smart_notes_throughput.py --template short      # LAYER 2 on a one-line template
    python tests/benchmarks/smart_notes_throughput.py --rate-limit 4 --request-limit 4   # the limiter, binding

**Its wall-clock column cannot decide a batching default, and has twice been read as if it
could.** ``--output-share`` is an ASSUMPTION about how a provider divides a call between fixed
overhead and generating tokens, not a calibration of one: at ``0`` grouping wins everywhere, at
``0.5`` it loses above one worker, and nothing in this repo measures the real split for any
model. The 0.5 rows were once quoted as "batching is measurably slower above one worker" and
shipped into a UI tooltip; the live rig (``smart_notes_live.py``) then produced the opposite
result, and a second live session produced neither. Use this file for what it can prove — CALL
COUNTS, prompt-cache hits, field identity, the fallback ladder under a hostile provider — and
use the live rig plus ``tests/benchmarks/data/`` for anything about seconds.

Three things this file was rebuilt to stop overstating, all of them the same mistake — a number
that is a property of the RIG being reported as a property of the WORLD:

* latency is fixed round trip PLUS per-output-item, so a K=10 chunk is not charged as one
  answer. Charged per request, LAYER 3's wall-clock column was a restatement of its call count;
* the transport sits UNDER the real ``RetryPolicy`` (it overrides ``UrllibHttpClient._open``,
  not the whole client), so a rate-limited run exercises both mechanisms the rows talk about;
* the limiter's own peak and wait are printed, and ``--request-limit`` can narrow the bound
  below the pool width so it can be seen to bind rather than merely restate the pool.

NOT collected by pytest (the filename does not match ``test_*.py``), deliberately: the whole
suite runs on five CI legs per PR and a wall-clock benchmark would add its sleep budget to
every one of them. The collected suite keeps the zero-latency invariant tests instead
(``tests/plugins/smart_notes/test_concurrency.py``); this file answers "how much faster", and
— just as importantly — "did anything stop being generated".

The workload mirrors the real note type this change was measured against: 17 enabled fields in
5 dependency levels of widths 2 / 4 / 6 / 4 / 1, of which 10 are pure-AI text on one
provider+model, 4 are media (3 TTS + 1 image), and 3 are deterministic tools that make no
provider call at all. Notes are driven through the REAL ``BatchGenerator``, so the real gates,
the real pipeline, the real commit path and the real progress/cancel plumbing are exercised —
only the provider and Anki's collection are fakes.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _path in (_REPO_ROOT / "src", _REPO_ROOT / "vendor" / "universal"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# ``scripts/`` is on sys.path automatically only for a script that LIVES there.
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from common import enable_utf8_output  # noqa: E402

# This prints a results table with box characters and a "→"; on a Windows console that is
# cp1252 and one of them takes the whole run down AFTER the measuring is finished.
enable_utf8_output()

from fakes import (  # noqa: E402
    BenchHub,
    LatencyLLMProvider,
    LatencyTTSProvider,
)

from omnia.core.network.limiter import PROVIDER_LIMITER  # noqa: E402
from omnia.plugins.smart_notes.config import (  # noqa: E402
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
    SmartNotesSettings,
)
from omnia.plugins.smart_notes.engine import GenerationService  # noqa: E402
from omnia.plugins.smart_notes.integration import batch as batch_module  # noqa: E402
from omnia.plugins.smart_notes.integration.batch import BatchGenerator  # noqa: E402

# How many notes a batched call may carry in the "+L3" rows. Ten of the seventeen fields are
# pure-AI text on one provider/model, so this is where the call count moves.
_BATCH_K = 10

NOTE_TYPE = "AnkiVocabulary"
BASE_FIELD = "Word"

# The instruction heads for the pure-AI fields. LAYER 2's headline number is ENTIRELY a
# property of this string's length: the cacheable share is head / (head + value), so quoting one
# figure as "the measured workload" is quoting the benchmark author's own template back as if it
# were a measurement of anyone's deck. Both are offered (``--template``) and both are reported,
# so the number is read as what it is — a function of how long the user's instructions are.
_LONG_TEMPLATE = (
    "You are an expert lexicographer helping an intermediate learner build a vocabulary "
    "deck. Write the {name} for the word given below.\n"
    "Rules: use natural, idiomatic English; be concise and concrete; return ONLY the answer "
    "with no preamble, no numbering, no commentary and no restating of the word; keep it "
    "under 40 words; prefer everyday register over literary register.\n"
    "Word: {{{{{parent}}}}}"
)

# What a user who types the smallest useful thing gets. Roughly 30 characters of head.
_SHORT_TEMPLATE = "Write the {name} for {{{{{parent}}}}}."

_TEMPLATES = {"long": _LONG_TEMPLATE, "short": _SHORT_TEMPLATE}

# (field, level, how it is generated). Widths per level: 2, 4, 6, 4, 1 = 17 fields.
#   "ai"    — pure-AI text on the shared provider/model (10 of them)
#   "tts"   — audio, bytes, one call per note (3)
#   "image" — a picture, bytes, one call per note (1)
#   "det"   — a deterministic tool: dispatched like anything else, but zero provider calls (3)
_WORKLOAD: tuple[tuple[str, int, str], ...] = (
    ("Reading", 0, "ai"),
    ("Meaning", 0, "ai"),
    ("Definition", 1, "ai"),
    ("Synonyms", 1, "ai"),
    ("Antonyms", 1, "det"),
    ("WordAudio", 1, "tts"),
    ("Example1", 2, "ai"),
    ("Example2", 2, "ai"),
    ("Collocations", 2, "ai"),
    ("Etymology", 2, "ai"),
    ("Picture", 2, "image"),
    ("WordFamily", 2, "det"),
    ("Example1Audio", 3, "tts"),
    ("Example2Audio", 3, "tts"),
    ("Mnemonic", 3, "ai"),
    ("Tags", 3, "det"),
    ("Summary", 4, "ai"),
)

# The field each level depends on, so the levels really are levels (and not one wide level).
_LEVEL_PARENT = {
    0: BASE_FIELD,
    1: "Meaning",
    2: "Definition",
    3: "Example1",
    4: "Mnemonic",
}


def _note_type_config(template: str = "long") -> SmartNotesNoteTypeConfig:
    """The measured note type's shape, as a smart-notes config."""
    ai_template = _TEMPLATES[template]
    fields = []
    for name, level, how in _WORKLOAD:
        parent = _LEVEL_PARENT[level]
        common = dict(field=name, enabled=True, overwrite=False)
        if how == "tts":
            fields.append(
                SmartNotesFieldConfig(
                    **common, type="tts", prompt=f"{{{{{parent}}}}}", language="en"
                )
            )
        elif how == "image":
            fields.append(
                SmartNotesFieldConfig(
                    **common, type="image", prompt=f"draw {{{{{parent}}}}}"
                )
            )
        elif how == "det":
            fields.append(
                SmartNotesFieldConfig(
                    **common,
                    type="text",
                    prompt=f"from {{{{{parent}}}}}",
                    tools=[{"tool": "bench_deterministic"}],
                )
            )
        else:
            fields.append(
                SmartNotesFieldConfig(
                    **common,
                    type="text",
                    prompt=ai_template.format(name=name, parent=parent),
                )
            )
    return SmartNotesNoteTypeConfig(
        note_type=NOTE_TYPE, base_field=BASE_FIELD, fields=fields
    )


class _BenchNote:
    """A dict-like Anki Note stand-in."""

    def __init__(self, nid: int) -> None:
        self.id = nid
        self._fields = {BASE_FIELD: f"word{nid}"}
        for name, _level, _how in _WORKLOAD:
            self._fields[name] = ""

    def keys(self):
        return list(self._fields.keys())

    def __contains__(self, key):
        return key in self._fields

    def __getitem__(self, key):
        return self._fields[key]

    def __setitem__(self, key, value):
        self._fields[key] = value

    def note_type(self):
        return {"name": NOTE_TYPE}

    def cards(self):
        return [type("C", (), {"did": 1})()]


class _BenchCompat:
    """Stands in for every ``anki_compat`` seam the batch runner touches."""

    def __init__(self, notes) -> None:
        self._notes = notes
        self.updated: list[int] = []
        self.media: list[str] = []

    def get_note(self, nid, col=None):
        return self._notes[nid]

    def note_deck_ids(self, note, col=None):
        return [1]

    def update_note(self, note, col=None):
        self.updated.append(note.id)

    def add_media_file(self, filename, data, col=None):
        self.media.append(filename)
        return filename

    def progress_start(self, label, maximum):
        pass

    def progress_update(self, label, value, maximum):
        pass

    def progress_finish(self):
        pass

    def progress_was_cancelled(self):
        return False

    def run_on_main(self, callback):
        callback()

    def run_in_background(self, op, *, on_success, on_failure=None, label=None):
        try:
            on_success(op())
        except Exception as exc:  # pragma: no cover - a bench failure must be visible
            if on_failure:
                on_failure(exc)
            raise


_COMPAT_SEAMS = (
    "get_note",
    "note_deck_ids",
    "update_note",
    "add_media_file",
    "progress_start",
    "progress_update",
    "progress_finish",
    "progress_was_cancelled",
    "run_on_main",
    "run_in_background",
)


@dataclass
class Measurement:
    """One configuration's numbers. Field loss sits beside speed on purpose.

    A run that is six times faster and quietly drops four per cent of its fields is not faster.
    """

    label: str
    seconds: float = 0.0
    provider_calls: int = 0
    peak_in_flight: int = 0
    limiter_wait: float = 0.0
    limiter_peak: int = 0
    processed: int = 0
    fields_written: int = 0
    blocked: int = 0
    field_failures: int = 0
    unfilled: int = 0
    media_files: int = 0
    rate_limited: int = 0
    prompt_chars: int = 0
    cacheable_chars: int = 0
    prefix_hits: int = 0
    prefix_misses: int = 0
    batch_calls: int = 0
    batched_items: int = 0
    solo_fallbacks: int = 0
    content: dict = field(default_factory=dict)


def _measure(
    label: str,
    notes_count: int,
    workers: int,
    latency: float,
    rate_limit_above: int = 0,
    prompt_cache: bool = False,
    notes_per_call: int = 1,
    corrupt: str = "",
    output_share: float = 0.0,
    template: str = "long",
    request_limit: int = 0,
) -> Measurement:
    """Run the whole workload once and report what happened.

    ``workers`` is LAYER 1 (1 = the sequential baseline). ``prompt_cache`` is LAYER 2, applied by
    giving the fake provider a prefix cache rather than by flipping any user setting — that split
    is lossless and has no switch. ``notes_per_call`` IS the LAYER 3 user setting (1 = off), so
    that column is measured through the real config path, ``corrupt`` and all — with the env
    knob that bounds the feature pinned to match, because it caps the stored setting and a run
    that inherited someone's environment would silently measure a different K.

    ``output_share`` is the fraction of a solo call's latency attributed to GENERATING its one
    answer (see :class:`fakes.SleepingHttpClient`); ``request_limit`` narrows the provider bound
    below the worker count so the limiter can be seen to bind.
    """
    notes = {nid: _BenchNote(nid) for nid in range(1, notes_count + 1)}
    compat = _BenchCompat(notes)
    originals = {
        name: getattr(batch_module.anki_compat, name) for name in _COMPAT_SEAMS
    }
    for name in _COMPAT_SEAMS:
        setattr(batch_module.anki_compat, name, getattr(compat, name))
    # LAYER 3's ceiling is this env knob (-1 = off, >= 1 = K), and the stored setting may only
    # ask for less. Both are pinned per row, exactly as a user would set them, and restored
    # afterwards — a row that inherited the machine's environment would silently measure some
    # other K, or the solo path.
    previous_flag = os.environ.get("OMNIA_SMART_NOTES_BATCHING")
    previous_limit = os.environ.get("OMNIA_MAX_CONCURRENT_REQUESTS")
    os.environ["OMNIA_SMART_NOTES_BATCHING"] = str(
        notes_per_call if notes_per_call > 1 else -1
    )
    if request_limit > 0:
        os.environ["OMNIA_MAX_CONCURRENT_REQUESTS"] = str(request_limit)

    llm = LatencyLLMProvider(
        latency=latency,
        rate_limit_above=rate_limit_above,
        prompt_cache=prompt_cache,
        corrupt=corrupt,
        output_share=output_share,
    )
    tts = LatencyTTSProvider(latency=latency, output_share=output_share)
    # detect_tts_language off: an Auto-detect voice would add a hidden LLM call per TTS field,
    # which is real but is a different measurement than the one this table is about.
    service = GenerationService(BenchHub(llm, tts), detect_tts_language=False)
    settings = SmartNotesSettings(
        note_types=[_note_type_config(template)],
        regenerate_when_batching=False,
        max_concurrent_generations=workers,
        batch_notes_per_call=notes_per_call,
    )

    PROVIDER_LIMITER.reset_stats()
    summaries: list = []
    started = time.perf_counter()
    try:
        BatchGenerator(service, settings).run(
            list(range(1, notes_count + 1)), summaries.append
        )
    finally:
        elapsed = time.perf_counter() - started
        for name, original in originals.items():
            setattr(batch_module.anki_compat, name, original)
        for key, value in (
            ("OMNIA_SMART_NOTES_BATCHING", previous_flag),
            ("OMNIA_MAX_CONCURRENT_REQUESTS", previous_limit),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    summary = summaries[0]
    stats = PROVIDER_LIMITER.stats
    return Measurement(
        label=label,
        seconds=elapsed,
        provider_calls=llm.stats.calls + tts.stats.calls,
        peak_in_flight=max(llm.stats.peak, tts.stats.peak),
        limiter_wait=stats.total_wait_seconds,
        limiter_peak=stats.peak_in_flight,
        processed=summary.processed,
        fields_written=sum(
            1
            for note in notes.values()
            for name, _level, _how in _WORKLOAD
            if note[name]
        ),
        blocked=summary.blocked,
        field_failures=summary.field_failures,
        unfilled=summary.unfilled,
        media_files=len(compat.media),
        rate_limited=llm.transport.rate_limited,
        prompt_chars=llm.ledger.prompt_chars,
        cacheable_chars=llm.ledger.cacheable_chars,
        prefix_hits=llm.ledger.prefix_hits,
        prefix_misses=llm.ledger.prefix_misses,
        batch_calls=llm.batch_calls,
        batched_items=llm.batched_items,
        # Every TEXT call that was not a chunk: the per-note calls the ladder had to make,
        # plus any text field a chunk never covered. It is the ladder's cost, made visible.
        # Text only — an image call is one per note by nature and would just be noise here.
        solo_fallbacks=llm.text_calls - llm.batch_calls,
        content={
            nid: {name: note[name] for name, _l, _h in _WORKLOAD}
            for nid, note in notes.items()
        },
    )


def _render_input(rows: list[Measurement]) -> str:
    """Render the LAYER 2 table: how much of the input is fresh, and how much repeats.

    CHARACTERS, not tokens — this fake has no tokenizer, so a token column would be a number
    with no provenance. The ``≈tok`` columns are the usual chars/4 rule of thumb and are labelled
    as the estimates they are. "Repeated prefix" is what a prefix cache can act on: the
    instruction head this run had already sent for that field.
    """
    head = (
        "| configuration | prompt chars | repeated prefix chars | repeated share "
        "| ≈prompt tok | ≈repeated tok | prefix hits | prefix misses |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    lines = []
    for row in rows:
        share = (
            (row.cacheable_chars / row.prompt_chars * 100) if row.prompt_chars else 0
        )
        lines.append(
            f"| {row.label} | {row.prompt_chars} | {row.cacheable_chars} "
            f"| {share:.1f}% | {row.prompt_chars // 4} | {row.cacheable_chars // 4} "
            f"| {row.prefix_hits} | {row.prefix_misses} |"
        )
    return head + "\n".join(lines)


def _render_calls(rows: list[Measurement], baseline: Measurement) -> str:
    """Render the LAYER 3 table: where the calls went, and what the ladder cost.

    "identical to baseline" is the column that decides whether the rest of the row means
    anything. Batching that halves the calls and rewrites one note's content is not a win, so
    the comparison is on the notes' FINAL FIELD VALUES, not on counts.
    """
    head = (
        "| configuration | provider calls | vs baseline | batched calls | notes batched "
        "| individual calls | fields written | identical to baseline |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    lines = []
    for row in rows:
        identical = sum(
            1
            for nid, values in row.content.items()
            if baseline.content.get(nid) == values
        )
        # Signed, because the ladder can make a row COST more than the baseline — a provider
        # that renumbers every id spends a chunk, two halves and then K individual calls. That
        # is the honest price of never misrouting, and hiding its sign would be dishonest.
        saved = row.provider_calls - baseline.provider_calls
        lines.append(
            f"| {row.label} | {row.provider_calls} | {saved:+d} "
            f"| {row.batch_calls} | {row.batched_items} | {row.solo_fallbacks} "
            f"| {row.fields_written} | {identical}/{len(row.content)} |"
        )
    return head + "\n".join(lines)


def _render(rows: list[Measurement], baseline: Measurement) -> str:
    """Render the results as a Markdown table, ready to paste into a PR's Test Output.

    Two peak columns, deliberately. "observed peak" is the fake TRANSPORT's own counter — what
    actually overlapped. "limiter peak" is what the limiter recorded, which is the only column
    that says anything about the bound; the earlier version of this table collected the limiter
    figure and then printed the transport's, so a reader comparing the peak against the stated
    capacity was reading the wrong number.
    """
    head = (
        "| configuration | wall clock (s) | speed-up | provider calls | observed peak "
        "| limiter peak | limiter wait (s) | 429s | notes written | fields written | blocked "
        "| field errors | identical to baseline |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    lines = []
    for row in rows:
        identical = sum(
            1
            for nid, values in row.content.items()
            if baseline.content.get(nid) == values
        )
        lines.append(
            f"| {row.label} | {row.seconds:.2f} | {baseline.seconds / row.seconds:.2f}x "
            f"| {row.provider_calls} | {row.peak_in_flight} | {row.limiter_peak} "
            f"| {row.limiter_wait:.2f} "
            f"| {row.rate_limited} | {row.processed} | {row.fields_written} "
            f"| {row.blocked} | {row.field_failures} "
            f"| {identical}/{len(row.content)} |"
        )
    return head + "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", type=int, default=50, help="notes in the batch")
    parser.add_argument(
        "--latency", type=float, default=0.05, help="simulated per-call seconds"
    )
    parser.add_argument(
        "--workers",
        type=int,
        nargs="*",
        default=[3, 8],
        help="concurrency levels to measure after the sequential baseline",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=_BATCH_K,
        help="notes per batched call in the +L3 rows (1 = LAYER 3 off)",
    )
    parser.add_argument(
        "--corrupt",
        choices=["", "drop-one", "renumber", "truncate", "collapse", "duplicate-id"],
        default="",
        help=(
            "damage every batched response in this way, to price the fallback ladder. The "
            "'identical to baseline' column must stay whole whichever is chosen"
        ),
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=0,
        dest="rate_limit",
        help=(
            "make the fake provider answer 429 above this many concurrent requests, to show "
            "the limiter-vs-retry split: the limiter is what prevents a SYSTEMATIC 429"
        ),
    )
    parser.add_argument(
        "--output-share",
        type=float,
        nargs="*",
        default=[0.0, 0.5, 1.0],
        dest="output_share",
        help=(
            "fraction of a SOLO call's latency spent GENERATING its one answer. 0 = output is "
            "free (the old, flattering model); 1 = output-dominated. A K-item chunk pays K "
            "times the per-item part, which is what a real provider charges. One +L3 row is "
            "printed per value"
        ),
    )
    parser.add_argument(
        "--template",
        choices=sorted(_TEMPLATES),
        default="long",
        help=(
            "which instruction head the AI fields use. LAYER 2's cacheable share is entirely a "
            "function of this length, so both are reported"
        ),
    )
    parser.add_argument(
        "--request-limit",
        type=int,
        default=0,
        dest="request_limit",
        help=(
            "narrow the provider bound BELOW the worker count (OMNIA_MAX_CONCURRENT_REQUESTS), "
            "so the limiter can be seen to bind instead of merely restating the pool width"
        ),
    )
    args = parser.parse_args(argv)

    # A rate-limited run deliberately produces hundreds of provider failures; the pipeline logs
    # each with its traceback, which would bury the table this script exists to print.
    logging.getLogger("omnia.smart_notes").setLevel(logging.CRITICAL)

    # Two different lengths, and conflating them is how "386-character instruction head" got
    # into three documents: `template_chars` is the WHOLE interpolated template, while the
    # cacheable prefix is only what precedes the first {{ref}} — the part every note of a field
    # shares byte for byte, and therefore the only part LAYER 2 is about.
    interpolated = _TEMPLATES[args.template].format(
        name="Definition", parent=BASE_FIELD
    )
    template_chars = len(interpolated)
    prefix_chars = len(interpolated.split("{{", 1)[0])
    print(
        f"workload: {args.notes} notes x {len(_WORKLOAD)} fields "
        f"(5 levels, widths 2/4/6/4/1), {args.latency * 1000:.0f} ms per SOLO provider call, "
        f"{args.template} template ({template_chars} chars, "
        f"{prefix_chars}-char cacheable prefix)\n"
    )
    common = dict(template=args.template, request_limit=args.request_limit)
    baseline = _measure(
        "baseline (sequential)", args.notes, 1, args.latency, args.rate_limit, **common
    )
    rows = [baseline]
    for workers in args.workers:
        rows.append(
            _measure(
                f"+L1 (N={workers}, provider WITHOUT a prefix cache)",
                args.notes,
                workers,
                args.latency,
                args.rate_limit,
                **common,
            )
        )
    # LAYER 2 on top of each LAYER 1 configuration. Same N, so any wall-clock difference between
    # a "+L1" row and its "+L1+L2" twin is run-to-run noise, not the cache: prompt caching does
    # not remove a round trip, and this fake refuses to pretend a cache hit answers faster.
    #
    # The row LABELS say what actually differs. Both rows run identical engine code and send
    # identical bytes; what changes is whether the fake PROVIDER declares a prefix cache. Naming
    # them "+L1" and "+L1+L2" invited the reading that the repetition appeared when L2 landed,
    # when in fact the repetition is a property of the workload and L2 is what lets a provider
    # bill for it once.
    for workers in args.workers:
        rows.append(
            _measure(
                f"+L1+L2 (N={workers}, provider WITH a prefix cache)",
                args.notes,
                workers,
                args.latency,
                args.rate_limit,
                prompt_cache=True,
                **common,
            )
        )
    # LAYER 3 on top of L1+L2, once per output-cost model. K-note batching is the only layer
    # that moves the CALL COUNT — that part is robust. Its WALL-CLOCK win is a function of how
    # much of a call's latency is generation, which is why there is a row per share instead of
    # one row and a footnote.
    suffix = f", {args.corrupt}" if args.corrupt else ""
    for workers in args.workers:
        for share in args.output_share:
            rows.append(
                _measure(
                    f"+L1+L2+L3 (N={workers}, K={args.batch}, "
                    f"output={share:.0%}{suffix})",
                    args.notes,
                    workers,
                    args.latency,
                    args.rate_limit,
                    prompt_cache=True,
                    notes_per_call=args.batch,
                    corrupt=args.corrupt,
                    output_share=share,
                    **common,
                )
            )
    print(_render(rows, baseline))
    print(
        "\nNOTE — the wall-clock column is only comparable within one output= share. A share of "
        "0% says output generation is free, which flatters LAYER 3 by construction: one call "
        "then costs the same whether it writes one answer or ten. Real completion latency is "
        "roughly TTFT + output_tokens/rate, so read the 50% row as the ordinary case and the "
        "100% row as a long-answer field. The CALL COUNT below is the layer's robust win.\n"
    )
    print("\ninput accounting (LAYER 2) — characters, not tokens; see PromptLedger:\n")
    print(_render_input(rows))
    # The advice used to be one hardcoded sentence, so `--template short` printed "re-run with
    # --template short to see the other end" while already running it — in the exact output the
    # docs tell a reviewer to reproduce.
    other_end = (
        "Re-run with --template short to see the other end."
        if args.template == "long"
        else "This IS the other end; --template long is the verbose case."
    )
    print(
        f"\nNOTE — the repeated share is prefix/(prefix+value) and nothing else: with this "
        f"{prefix_chars}-character cacheable prefix it is what the table says, and with a "
        f"longer or shorter one it moves with it. {other_end} It is NOT a measured property of "
        f"anyone's deck.\n"
    )
    print("\ncall accounting (LAYER 3) — where the round trips went:\n")
    print(_render_calls(rows, baseline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
