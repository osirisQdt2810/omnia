"""The two config-model bases: how a model meets data it does not recognise.

Omnia's settings are read AND written by more than one Omnia version. ``omnia.toml`` (log
level + the plugin enable map) and ``features.toml`` (every per-feature section) live in the
Anki collection config (ADR-006/ADR-008), and smart_notes keeps its own blob there too — so
the same JSON is loaded, and saved back, by every device the user syncs (macOS + Windows +
Ubuntu), each possibly on a different release. A key one device has never heard of is
therefore normal traffic, not corruption.

Every config model picks one of the two bases below:

* :class:`PersistedModel` — for anything parsed from, or serialized into, persisted config
  (the synced collection config or ``providers.toml``). Unknown keys are KEPT.
* :class:`StrictModel` — for models that are never persisted (in-memory shapes compiled by the
  engine, payloads posted by the settings webview). There an unknown key really is a typo, so
  it is rejected loudly.

Pydantic v1 (see :mod:`omnia.core.config.models` for why v1) offers three answers to an
unknown key — ``forbid``, ``ignore`` and ``allow`` — and only ``allow`` is lossless:

===========  ==================  ================================================
``extra``    load on old client  what ``.dict()`` writes back
===========  ==================  ================================================
``forbid``   ValidationError     — (never gets that far)
``ignore``   loads               the newer device's keys are SILENTLY DROPPED
``allow``    loads               every key round-trips verbatim
===========  ==================  ================================================
"""

from __future__ import annotations

from pydantic import BaseModel


class StrictModel(BaseModel):
    """Base for models that are NEVER persisted — an unknown key is a typo, so reject it.

    Use this for in-memory shapes only: the rules the engine compiles, the dicts the settings
    webview posts back. Anything that reaches disk or the collection config must use
    :class:`PersistedModel` instead, or a newer version's data becomes a crash on an older one.
    """

    class Config:
        extra = "forbid"


class PersistedModel(BaseModel):
    """Base for every model stored in (or loaded from) persisted config — keeps unknown keys.

    ``extra = "allow"`` rather than ``"ignore"``: both let an OLD client load a blob written by
    a NEWER one, but ``"ignore"`` drops the keys it did not recognise, and because the same
    blob is written straight back (``store.save(settings.dict())``) the old client would then
    DESTROY the newer device's settings on the next sync. ``"allow"`` retains unknown keys as
    extra attributes and round-trips them through ``.dict()``/``.copy()``, so an old client is
    a faithful pass-through for config it cannot yet interpret.

    Declared fields are still fully validated — this tolerance is about unknown KEYS. A new
    VALUE for an existing field (a generation type this version does not implement) is a
    separate concern each model handles itself; see
    :class:`omnia.plugins.smart_notes.config.SmartNotesFieldConfig`.
    """

    class Config:
        extra = "allow"
