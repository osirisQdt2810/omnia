"""Display-interval settings model (the plugin's own Pydantic v1 config).

The next-interval overlay currently has no options, but the plugin still owns a settings
model so :class:`~omnia.core.plugin.FeaturePlugin` can derive a (here empty) schema uniformly.
"""

from __future__ import annotations

from pydantic import BaseModel


class _Strict(BaseModel):
    """Base model that rejects unknown keys (catches config typos early)."""

    class Config:
        extra = "forbid"


class DisplayIntervalSettings(_Strict):
    """Settings for the next-interval overlay (currently no options)."""
