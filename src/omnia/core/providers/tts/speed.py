"""How fast the generated voice speaks, and what each engine calls that.

One number reaches the user — a multiplier where ``1.0`` is the voice's natural pace, ``0.8``
is a fifth slower, ``1.25`` a quarter faster. Every engine then wants it in its own dialect:

============== ============================================== ==================
engine         parameter                                      accepts
============== ============================================== ==================
Edge           ``rate`` in the SSML ``<prosody>``, a PERCENT   ``-100%``…``+200%``
               DELTA from normal — ``+0%`` is the default it
               was hardcoded to
Google Cloud   ``speakingRate``, the multiplier as-is         ``0.25``…``4.0``
OpenAI (and    ``speed``, the multiplier as-is                ``0.25``…``4.0``
viet-tts)
piper          ``--length_scale``, the INVERSE — it scales     any positive float
               duration, so slower is a BIGGER number
Google         ``ttsspeed``, which the ``tw-ob`` endpoint      slow / normal only
Translate      honours as slow-or-not rather than as a rate
============== ============================================== ==================

The conversions live here rather than in each provider because they are the kind of thing that
is wrong in exactly one direction and stays wrong quietly: an inverted ``length_scale`` still
synthesizes, still returns valid audio, and is simply faster when the user asked for slower.
Pure functions with the table above next to them can be tested against it directly.
"""

from __future__ import annotations

#: The voice's natural pace — the value that means "do not ask for anything".
NORMAL = 1.0

#: What the UI offers. Wider than anyone wants in practice, narrow enough that every engine
#: above can honour the ends: half speed is still intelligible, double is still a voice.
MIN_SPEED = 0.5
MAX_SPEED = 2.0


def clamp(speed: float, *, low: float = MIN_SPEED, high: float = MAX_SPEED) -> float:
    """``speed`` confined to a usable range, with a non-positive or unreadable value meaning
    :data:`NORMAL`.

    Zero is the "inherit" value the config layer uses, and a negative or non-numeric one can
    only be corruption; both mean the caller is not asking for a rate. Answering ``NORMAL``
    rather than raising keeps a bad stored value from failing the synthesis — the note gets its
    audio at the natural pace, which is what it would have had anyway.
    """
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return NORMAL
    if value <= 0:
        return NORMAL
    return max(low, min(high, value))


def as_percent_delta(speed: float) -> str:
    """``speed`` as Edge's signed percentage delta: ``1.25`` → ``"+25%"``, ``0.8`` → ``"-20%"``.

    Always signed, because Edge's SSML wants the sign even at zero — the value this replaced
    was the literal ``'+0%'``.
    """
    percent = round((clamp(speed) - NORMAL) * 100)
    return f"{percent:+d}%"


def as_length_scale(speed: float) -> float:
    """``speed`` as piper's ``length_scale``, which scales DURATION and so runs backwards.

    ``0.5`` (half speed) is a length scale of ``2.0``: each phoneme lasts twice as long.
    """
    return round(NORMAL / clamp(speed), 4)


def is_normal(speed: float) -> bool:
    """Whether ``speed`` asks for anything at all.

    Providers use this to leave the request byte-identical to what it was before speed existed
    — a parameter an endpoint has never been sent is one it cannot reject.
    """
    return clamp(speed) == NORMAL
