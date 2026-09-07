"""Audio-speed settings model (the plugin's own Pydantic v1 config).

Co-located with the plugin so the feature owns its schema; the generic settings form is
derived from it via :func:`omnia.core.config.schema.schema_from_model`. Field descriptions
become GUI tooltips and ``ge``/``le`` bounds drive the numeric widgets.

``rate`` is both a setting and live state: the shortcuts change it during review and, with
``remember_rate`` on, write it straight back here so the next session starts where this one
left off. That is deliberate — a speed you chose once is a preference, not a per-session whim,
and it is the thing the third-party add-on this replaces never did.
"""

from __future__ import annotations

from pydantic import Field, validator

from omnia.core.config.base import PersistedModel


class AudioSpeedSettings(PersistedModel):
    """Settings for the audio-speed feature."""

    rate: float = Field(
        1.0,
        ge=0.25,
        le=4.0,
        description=(
            "Playback speed for card audio, as a multiplier (1.0 = normal).\n"
            "• Applies to [sound:] clips AND to <audio>/<video> elements a template plays itself.\n"
            "• Changed live with the shortcuts below; saved back here when 'Remember' is on."
        ),
    )
    step: float = Field(
        0.1,
        ge=0.05,
        le=1.0,
        description="How much each 'speed up' / 'slow down' press changes the rate.",
    )
    min_rate: float = Field(
        0.5,
        ge=0.25,
        le=4.0,
        description="Slowest speed the shortcuts will go to.",
    )
    max_rate: float = Field(
        3.0,
        ge=0.25,
        le=4.0,
        description="Fastest speed the shortcuts will go to.",
    )

    @validator("max_rate")
    def _max_not_below_min(cls, value: float, values: dict) -> float:
        """Reject a crossed pair here, where the settings form can show the error.

        Each bound is range-checked on its own, but the PAIR is what ``SpeedBounds`` refuses.
        Without this the save succeeds and the failure surfaces one layer down, inside
        ``on_enable``, where the manager's isolation boundary logs it and the user is left
        with a ticked plugin that silently does nothing.
        """
        minimum = values.get("min_rate")
        if minimum is not None and value < minimum:
            raise ValueError(
                f"max_rate ({value}) must not be below min_rate ({minimum})"
            )
        return value

    remember_rate: bool = Field(
        True,
        description=(
            "Keep the chosen speed across sessions and sync it with your collection.\n"
            "Off = every Anki launch starts at 1.0×."
        ),
    )
    show_tooltip: bool = Field(
        True,
        description="Flash the new speed in Anki's tooltip each time a shortcut changes it.",
    )
    speed_up_shortcut: str = Field(
        "]",
        description="Keyboard shortcut to speed up (Qt key sequence, e.g. ']' or 'Ctrl+Up').",
    )
    slow_down_shortcut: str = Field(
        "[",
        description="Keyboard shortcut to slow down.",
    )
    reset_shortcut: str = Field(
        "Ctrl+]",
        description="Keyboard shortcut to return to 1.0×.",
    )
