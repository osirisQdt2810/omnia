#!/usr/bin/env python3
"""Turn a ``coverage.xml`` into a shields.io endpoint payload.

The README's coverage badge is served by shields.io reading a JSON file this writes, rather
than by a coverage service. Codecov refuses this repo's uploads ("Token required - not valid
tokenless upload") and the workflow's ``fail_ci_if_error: false`` made that refusal silent, so
the badge read ``unknown`` while CI reported success. A badge with no account and no token
behind it has nothing to expire.

Usage::

    python scripts/coverage_badge.py coverage.xml badge/coverage.json

Stdlib only — it runs in CI before anything is vendored.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

#: Thresholds → shields.io colour, worst first. Kept coarse on purpose: a badge that changes
#: colour on a one-point move trains people to ignore it.
_COLOURS: tuple[tuple[float, str], ...] = (
    (50.0, "red"),
    (70.0, "orange"),
    (80.0, "yellow"),
    (90.0, "green"),
    (100.1, "brightgreen"),
)


def percent(coverage_xml: Path) -> float:
    """Return the line-coverage percentage recorded in ``coverage_xml``.

    Raises:
        ValueError: If the file carries no ``line-rate`` — better to fail the job than to
            publish a confident-looking badge built from a number that was not there.
    """
    rate = ET.parse(coverage_xml).getroot().get("line-rate")
    if rate is None:
        raise ValueError(f"{coverage_xml} has no line-rate attribute")
    return float(rate) * 100.0


def colour(value: float) -> str:
    """Return the shields.io colour for a coverage percentage."""
    return next(name for limit, name in _COLOURS if value < limit)


def payload(value: float) -> dict[str, object]:
    """Return the shields.io endpoint object for a coverage percentage."""
    return {
        "schemaVersion": 1,
        "label": "coverage",
        "message": f"{value:.0f}%",
        "color": colour(value),
    }


def main(argv: list[str]) -> int:
    """Write the endpoint payload for ``argv[1]`` to ``argv[2]``."""
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    destination = Path(argv[2])
    destination.parent.mkdir(parents=True, exist_ok=True)
    value = percent(Path(argv[1]))
    destination.write_text(json.dumps(payload(value)) + "\n", encoding="utf-8")
    print(f"coverage {value:.2f}% -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
