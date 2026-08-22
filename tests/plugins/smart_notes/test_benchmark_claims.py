"""The benchmark table in ``config.py`` must re-derive from the committed rows.

Every number the add-on quotes about its own throughput — in the ``max_concurrent_generations``
docstring, in ``envs.py``, in the Advanced pane's tooltip — was typed by hand from a run whose
raw rows are committed under ``tests/benchmarks/data/``. Hand-typed and machine-produced numbers
drift, and one already had: the table printed 794 and 574 provider calls where the rows mean
794.5 and 574.5, because a half was truncated rather than kept. Nothing caught it but a reader.

So this module recomputes the table from the JSON and compares. It is not a tautology: the
assertions run against a hand-written comment, and the only way to keep them green is to make
the comment say what the data says.
"""

from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_DATA = _ROOT / "tests" / "benchmarks" / "data"
_CONFIG = _ROOT / "src" / "omnia" / "plugins" / "smart_notes" / "config.py"

# ``#   8x10     1162.5 s  (1110–1215)     9.0%    794.5      0   89.50%      138.5``
_ROW = re.compile(
    r"^\s*#\s+(?P<arm>\d+x\d+)\s+(?P<seconds>[\d.]+) s\s+\((?P<lo>\d+)[–-](?P<hi>\d+)\)"
    r"\s+(?P<spread>[\d.]+)%\s+(?P<calls>[\d.]+)\s+(?P<n429>\d+)\s+(?P<fill>[\d.]+)%"
)


def _logical_calls(counts: dict) -> int:
    """One requested generation, retries excluded — what the table's "provider calls" means.

    ``limiter_acquired`` is the neighbouring figure and is NOT this: it counts each retry
    separately, so a run with one network blip reports 1301 where 1300 generations were asked
    for. The distinction is why 4x1 reads 1300 in the table and 1301.5 in the raw field.
    """
    return counts["solo_text"] + counts["batched"] + counts["detect"] + counts["tts"]


def _rows_by_arm(filename: str) -> dict[str, list[dict]]:
    rows = json.loads((_DATA / filename).read_text(encoding="utf-8"))
    by_arm: dict[str, list[dict]] = {}
    for row in rows:
        by_arm.setdefault(row["label"], []).append(row)
    return by_arm


@pytest.fixture(scope="module")
def hundred_notes() -> dict[str, list[dict]]:
    return _rows_by_arm("live_100notes_2026-08-22.json")


@pytest.fixture(scope="module")
def table() -> dict[str, re.Match]:
    parsed = {
        m.group("arm"): m
        for m in map(_ROW.match, _CONFIG.read_text(encoding="utf-8").splitlines())
        if m
    }
    assert (
        parsed
    ), "the benchmark table in config.py no longer parses — did its shape change?"
    return parsed


def test_every_quoted_arm_exists_in_the_committed_rows(table, hundred_notes):
    """A row in the comment with no data behind it is the failure mode this catches."""
    assert set(table) == set(
        hundred_notes
    ), f"table arms {sorted(table)} vs data arms {sorted(hundred_notes)}"


def test_the_provider_call_column_is_the_mean_of_the_runs(table, hundred_notes):
    """The column that was wrong. A truncated .5 fails here.

    Both repeats of an arm are averaged, and the mean is compared exactly — a half is a half.
    794.5 typed as 794 is a 0.5 discrepancy and this asserts equality, so it fails.
    """
    for arm, match in table.items():
        expected = statistics.mean(
            _logical_calls(r["counts"]) for r in hundred_notes[arm]
        )
        assert float(match.group("calls")) == expected, (
            f"{arm}: table says {match.group('calls')} provider calls, "
            f"rows mean {expected}"
        )


def test_the_wall_clock_column_is_the_mean_of_the_runs(table, hundred_notes):
    for arm, match in table.items():
        expected = statistics.mean(r["seconds"] for r in hundred_notes[arm])
        assert (
            abs(float(match.group("seconds")) - expected) <= 0.05
        ), f"{arm}: table says {match.group('seconds')} s, rows mean {expected:.3f} s"


def test_the_spread_column_is_the_min_and_max_of_the_runs(table, hundred_notes):
    """The parenthesised range, and the percentage spread computed off it."""
    for arm, match in table.items():
        seconds = [r["seconds"] for r in hundred_notes[arm]]
        lo, hi = min(seconds), max(seconds)
        assert abs(float(match.group("lo")) - lo) <= 1.0, f"{arm}: low end"
        assert abs(float(match.group("hi")) - hi) <= 1.0, f"{arm}: high end"
        expected_spread = (hi - lo) / statistics.mean(seconds) * 100
        assert (
            abs(float(match.group("spread")) - expected_spread) <= 0.1
        ), f"{arm}: table says {match.group('spread')}% spread, rows give {expected_spread:.2f}%"


def test_no_arm_recorded_a_rate_limit(table, hundred_notes):
    """The 429 column is what licenses the default's concurrency; it must stay derived."""
    for arm, match in table.items():
        observed = sum(r["counts"]["rate_limited_calls"] for r in hundred_notes[arm])
        assert int(match.group("n429")) == observed, f"{arm}: 429 count"


def test_the_prose_figures_match_the_table(table):
    """``config.py`` and ``envs.py`` restate two of the table's cells in words.

    A restatement is a second place to be wrong, so it is checked against the table it quotes
    rather than against the data twice.
    """
    calls = {arm: match.group("calls") for arm, match in table.items()}
    config_text = _CONFIG.read_text(encoding="utf-8")
    envs_text = (_ROOT / "src" / "omnia" / "envs.py").read_text(encoding="utf-8")
    assert (
        f"K = 10 sent {calls['8x10']} provider calls and K = 20 sent {calls['8x20']}"
        in config_text
    )
    assert (
        f"K=10 sent {calls['8x10']} provider calls and K=20 sent {calls['8x20']}"
        in envs_text
    )


def test_the_tooltip_quotes_both_runs_not_a_fractional_mean(hundred_notes):
    """The pane is user-facing, so it names the two runs rather than their .5 mean."""
    page = (
        _ROOT / "src" / "omnia" / "gui" / "smart_notes" / "web" / "page.html"
    ).read_text(encoding="utf-8")
    runs = sorted(_logical_calls(r["counts"]) for r in hundred_notes["8x10"])
    solo = {_logical_calls(r["counts"]) for r in hundred_notes["8x1"]}
    assert (
        len(solo) == 1
    ), "the ungrouped baseline differed between runs; the tooltip cannot cite one number"
    assert f"{solo.pop()} → {runs[0]} and {runs[1]} across the two runs" in page
