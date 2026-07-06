"""Registry of external integrations that can auto-generate smart_notes cards (Feature B).

An *integration* is a third-party source (today: the Omnia browser extension) that pushes new
notes into Anki. Each integration tags the notes it creates with a distinctive ``source_tag`` so
the gateway can recognise them, and exposes a settings toggle keyed by ``key``. Adding a new
integration is one :class:`Integration` entry here (plus its UI row) — the gateway, config, and
settings tab all iterate this tuple, so no gateway logic changes.

Pure data + lookup: this module imports no ``aqt``/``anki`` and unit-tests headless.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Caller guard: every auto-generatable note an integration creates carries this tag (the clipper
# adds it when its "Auto-generate" option is on). Its presence is the CHEAP first gate — without
# it the gateway returns immediately, so ordinary note adds pay almost nothing.
AUTOGEN_TAG = "omnia-autogen"


@dataclass(frozen=True)
class Integration:
    """One external note source that can trigger smart_notes auto-generation.

    Attributes:
        key: Stable id used as the settings toggle key (``auto_generate_integrations[key]``).
        source_tag: The tag the integration stamps on notes it creates, used to recognise them.
        name: Human-readable name shown in the Integrations settings tab.
        description: One-line explanation shown under the name.
    """

    key: str
    source_tag: str
    name: str
    description: str


# The registered integrations. One entry today (the browser extension); add a new source by
# appending an ``Integration`` here + a UI row — the gateway/config iterate this tuple.
INTEGRATIONS: tuple[Integration, ...] = (
    Integration(
        key="web_clipper",
        source_tag="omnia-web-clipper",
        name="Omnia Web Clipper",
        description="Auto-generate cards saved by the Omnia browser extension.",
    ),
)


def integration_for_tags(tags: Iterable[str]) -> Integration | None:
    """Return the first registered integration whose ``source_tag`` is among ``tags``.

    Args:
        tags: The note's tags.

    Returns:
        The matching :class:`Integration`, or ``None`` when no registered source tag is present.
    """
    tag_set = set(tags)
    for integration in INTEGRATIONS:
        if integration.source_tag in tag_set:
            return integration
    return None
