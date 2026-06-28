"""Pure typing-accuracy stats: aggregation, an offline SVG donut + HTML card, and a JSON store.

No ``aqt``/``anki`` imports — every function here is unit-testable headless. The Anki glue
(:mod:`omnia.features.typed_accuracy`) records each typed-answer result into the
:class:`StatsStore` and asks :func:`summarize` + :func:`stats_card_html` to render a card on
the deck overview. The SVG is self-contained (no external JS/CSS/fonts) so it renders inside
Anki's content-security-policy sandbox while offline.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class TypedResult:
    """One recorded typed-answer outcome."""

    ts: float
    ratio: float
    passed: bool
    deck_id: Optional[int]


@dataclass
class StatsSummary:
    """Aggregate of a set of :class:`TypedResult`\\ s.

    ``pass_rate`` and ``avg_ratio`` are 0.0 when ``total == 0`` (no division by zero).
    """

    total: int
    passed: int
    failed: int
    pass_rate: float
    avg_ratio: float


def summarize(results: list[TypedResult]) -> StatsSummary:
    """Aggregate ``results`` into a :class:`StatsSummary` (empty input → all zeros)."""
    total = len(results)
    if total == 0:
        return StatsSummary(total=0, passed=0, failed=0, pass_rate=0.0, avg_ratio=0.0)
    passed = sum(1 for r in results if r.passed)
    avg_ratio = sum(r.ratio for r in results) / total
    return StatsSummary(
        total=total,
        passed=passed,
        failed=total - passed,
        pass_rate=passed / total,
        avg_ratio=avg_ratio,
    )


_DONUT_STROKE = 12
_RING_BG = "#d9d9d9"
_RING_FG = "#2e8b57"
_TEXT_COLOR = "#333333"


def donut_svg(summary: StatsSummary, *, size: int = 120) -> str:
    """Return a self-contained SVG donut whose ring fills to the pass rate.

    The ring is a single circle drawn with ``stroke-dasharray`` so the filled arc equals
    ``summary.pass_rate`` of the circumference; the centre shows the pass rate as a percentage.
    A zero-total summary renders a grey ring and ``0%``. No external JS/CSS/fonts (CSP-safe).

    Args:
        summary: the aggregate to visualise.
        size: the SVG width/height in pixels.
    """
    radius = (size - _DONUT_STROKE) / 2
    centre = size / 2
    circumference = 2 * math.pi * radius
    filled = circumference * summary.pass_rate
    percent = round(summary.pass_rate * 100)
    # Start the arc at 12 o'clock (rotate -90deg about the centre).
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<circle cx="{centre}" cy="{centre}" r="{radius:.2f}" fill="none" '
        f'stroke="{_RING_BG}" stroke-width="{_DONUT_STROKE}"/>'
        f'<circle cx="{centre}" cy="{centre}" r="{radius:.2f}" fill="none" '
        f'stroke="{_RING_FG}" stroke-width="{_DONUT_STROKE}" stroke-linecap="round" '
        f'stroke-dasharray="{filled:.2f} {circumference:.2f}" '
        f'transform="rotate(-90 {centre} {centre})"/>'
        f'<text x="{centre}" y="{centre}" text-anchor="middle" '
        f'dominant-baseline="central" font-size="{size // 5}" font-weight="bold" '
        f'fill="{_TEXT_COLOR}">{percent}%</text>'
        f"</svg>"
    )


def stats_card_html(summary: StatsSummary) -> str:
    """Return a styled HTML card embedding :func:`donut_svg` and the headline numbers.

    Inline styles only (no external CSS), so it drops straight into the overview content.
    """
    donut = donut_svg(summary)
    avg_percent = round(summary.avg_ratio * 100)
    pass_percent = round(summary.pass_rate * 100)
    return (
        '<div style="display:inline-block;margin:12px auto;padding:16px 20px;'
        "border:1px solid #ccc;border-radius:10px;text-align:center;"
        'font-family:inherit;">'
        '<div style="font-weight:bold;margin-bottom:8px;">Typing accuracy</div>'
        f"{donut}"
        '<div style="margin-top:10px;font-size:13px;color:#555;">'
        f"<div>{summary.total} reviews</div>"
        f"<div>Pass rate: {pass_percent}%</div>"
        f"<div>Avg accuracy: {avg_percent}%</div>"
        "</div>"
        "</div>"
    )


class StatsStore:
    """Append-only persistence for :class:`TypedResult`\\ s over a single JSON file.

    No Anki imports; ``now`` is injected into :meth:`record` so callers control time (the
    glue passes ``time.time()``; tests pass fixed values). A missing or corrupt file loads as
    an empty history rather than raising.
    """

    def __init__(self, path: Path, *, max_records: int = 5000) -> None:
        """Initialise the store.

        Args:
            path: the JSON file backing the history.
            max_records: keep at most this many newest records (older ones are dropped).
        """
        self._path = path
        self._max_records = max_records

    def record(
        self,
        ratio: float,
        threshold: float,
        *,
        deck_id: Optional[int] = None,
        now: float,
    ) -> None:
        """Append one outcome (passed = ``ratio >= threshold``), trim, and save."""
        history = self._load()
        history.append(
            TypedResult(ts=now, ratio=ratio, passed=ratio >= threshold, deck_id=deck_id)
        )
        if self._max_records > 0 and len(history) > self._max_records:
            history = history[-self._max_records :]
        self._save(history)

    def results(self, deck_id: Optional[int] = None) -> list[TypedResult]:
        """Return all recorded results, or only those for ``deck_id`` when given."""
        history = self._load()
        if deck_id is None:
            return history
        return [r for r in history if r.deck_id == deck_id]

    def clear(self) -> None:
        """Drop all recorded results."""
        self._save([])

    def _load(self) -> list[TypedResult]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []  # missing or corrupt file → start fresh
        if not isinstance(raw, list):
            return []
        out: list[TypedResult] = []
        for item in raw:
            if isinstance(item, dict):
                out.append(
                    TypedResult(
                        ts=float(item.get("ts", 0.0)),
                        ratio=float(item.get("ratio", 0.0)),
                        passed=bool(item.get("passed", False)),
                        deck_id=item.get("deck_id"),
                    )
                )
        return out

    def _save(self, history: list[TypedResult]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps([asdict(r) for r in history]), encoding="utf-8"
        )
