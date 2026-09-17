"""Whether Omnia wrote a field's current contents — and whether anyone has touched it since.

Why the second half matters
---------------------------
"Omnia generated this field" is not the question worth answering. A user who regenerates a note
type wants the fields Omnia made to be refreshed and the sentences they wrote by hand to be left
alone — so the predicate has to be *"Omnia wrote this, and it is still exactly what Omnia
wrote"*. A mark that only says "mine" survives the user's edits and then authorises destroying
them, which is the one outcome the setting exists to prevent.

So the mark carries a fingerprint of the content it was applied to:

    <!--omnia:9f2b1c--><b>a definition</b>

Reading it back: take the mark off, fingerprint what is left, compare. Equal means untouched.
Different means someone edited the field with the mark still in place — which is exactly what
happens, because an HTML comment is a DOM node that survives typing around it. Absent means the
field is not ours at all.

An HTML comment was chosen over a wrapper element or a side table because of what each does when
the user edits. A ``<span data-omnia>`` keeps its attribute while its contents change, so it lies
in the same way a bare mark does. A side table has to be kept in step with a field it cannot
observe, and does not travel to the user's other machines. A comment travels with the note, is
invisible while reviewing, and — verified against Anki 25.09 and its Chromium editor — survives
storage, ``fix_integrity`` (Check Database), ``munge_html``, and a contenteditable focus/blur
round trip, while a real edit changes the text beside it.

Pure: no ``aqt``, no ``anki``, no hashing of anything but the string it is handed.
"""

from __future__ import annotations

import hashlib
import re

from omnia.core.lang.text import strip_markup

#: How much of the digest to keep. Six hex characters is 16.7M buckets — far past what is needed
#: to notice a human edit, and short enough that the mark stays unobtrusive in the HTML editor.
#: A collision would mean an edit went unnoticed and the field was overwritten, which is the same
#: outcome as not having the mark at all, so the cost of being wrong here is bounded.
_DIGEST_CHARS = 6

_MARK_RE = re.compile(rf"^\s*<!--omnia:([0-9a-f]{{{_DIGEST_CHARS}}})-->")


def fingerprint(content: str) -> str:
    """The digest recorded for ``content``."""
    return hashlib.sha1((content or "").encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def stamp(content: str) -> str:
    """``content`` with Omnia's mark on it, replacing any mark already there.

    Applied when the field is WRITTEN, not when the value is generated: a tool later in the
    chain reads the field it is chained from, and a comment in its input would end up quoted
    into a prompt.
    """
    body = unstamp(content)
    return f"<!--omnia:{fingerprint(body)}-->{body}"


def unstamp(content: str) -> str:
    """``content`` without Omnia's mark — what the reader actually sees."""
    return _MARK_RE.sub("", content or "", count=1)


def is_marked(content: str) -> bool:
    """Whether Omnia ever wrote this field. Says nothing about whether it still holds."""
    return bool(_MARK_RE.match(content or ""))


def is_untouched(content: str) -> bool:
    """Whether Omnia wrote this field and nobody has edited it since.

    The one question the overwrite setting asks. False for an unmarked field (not ours), and
    false for a marked one whose text no longer matches its mark (ours once, edited since).
    """
    match = _MARK_RE.match(content or "")
    if not match:
        return False
    return match.group(1) == fingerprint(unstamp(content))


#: What a regeneration is allowed to replace, once the per-field Overwrite flag has already
#: said that filled fields should be refreshed at all.
#:
#: Two different things are worth protecting and they pull in opposite directions, which is why
#: this is a choice rather than a switch:
#:
#: * ``OURS_ONLY`` protects what YOU wrote. A sentence typed by hand and one Omnia generated are
#:   indistinguishable to a rule that only knows the field is non-empty.
#: * ``NOT_OURS`` protects what OMNIA wrote. Audio already generated cost real provider money,
#:   and regenerating it buys an identical file for the price of a second one.
ALWAYS = "always"
OURS_ONLY = "ours_only"
NOT_OURS = "not_ours"
SCOPES = (ALWAYS, OURS_ONLY, NOT_OURS)

# There is deliberately no "never". It would mean "replace nothing that has content", which the
# per-field Overwrite switch and `regenerate_when_batching` already say — and say in the place a
# reader looks for them. The only case it expressed that they cannot is a global override of a
# field whose own Overwrite is on, which is not worth an option nobody can explain the purpose
# of. Every scope here needs the mark to exist; that is what makes them worth having.


#: The prefix :func:`~omnia.plugins.smart_notes.integration.batch.materialize` gives every media
#: file it writes. A real collection holds media from several sources — AwesomeTTS and HyperTTS
#: write ``googletts-…``, pasted images land as ``paste-…`` — and none of them are ours.
OUR_MEDIA_PREFIX = "omnia-"

#: A media reference inside a field: ``[sound:x.mp3]`` or ``<img src="x.png">``. Both quote
#: styles, because Anki's editor writes double and hand-edited fields carry single.
#:
#: The ``<img>`` branch consumes the WHOLE tag, closing ``>`` included. It used to stop at the
#: quote, which is invisible to :func:`media_refs` (the capture group is the same) but not to
#: :func:`is_ours`, which asks whether anything remains once the refs are removed: a lone ``>``
#: is left over, so an image field of ours read as somebody else's.
_MEDIA_REF_RE = re.compile(
    r"\[sound:([^\]]+)\]|<img[^>]+src=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE
)


def media_refs(content: str) -> list[str]:
    """Every media filename ``content`` references, in order."""
    return [(s or i).strip() for s, i in _MEDIA_REF_RE.findall(content or "")]


def our_media_refs(content: str) -> list[str]:
    """The ones Omnia wrote — the only files it may ever replace or trash."""
    out: list[str] = []
    for name in media_refs(content):
        if name.startswith(OUR_MEDIA_PREFIX) and name not in out:
            out.append(name)
    return out


def to_store(value: str, kind: str) -> str:
    """``value`` as it should be WRITTEN into a note field — Omnia's mark applied where it helps.

    Every path that writes generated content goes through here, and that is the point. Stamping
    lived in the batch runner alone, so the editor's Generate button, review-time pre-generation
    and the clipper's regeneration all wrote Omnia's text unmarked. Under ``ours_only`` those
    fields were then treated as somebody else's work and never refreshed; under ``not_ours``
    they were regenerated on every batch and paid for again. Both are silent.

    NOT folded into ``materialize``, which runs during generation: its value is what a chained
    tool reads next, and a mark there would be quoted into the following prompt.

    Text only. A media field is one ``[sound:omnia-…]`` tag whose NAME already says who wrote
    it, and a comment beside it would be clutter for no new information.

    Args:
        value: What generation produced for the field.
        kind: The result's kind — ``"text"``, ``"tts"`` or ``"image"``.

    Returns:
        The string to store.
    """
    if kind != "text" or not value.strip():
        # An empty value is left empty. Marking it produces a bare `<!--omnia:da39a3-->`, which
        # is not empty to anything that asks: `should_skip_rule` reads the field as filled and
        # skips it, so a field that generated nothing would never be tried again.
        return value
    return stamp(value)


def is_ours(content: str) -> bool:
    """Whether this field's current contents are Omnia's, and unchanged since.

    Two kinds of field, answered two ways, because the honest answer differs:

    * **Media** — the filename says it. A field holding ``[sound:omnia-…]`` was written by
      Omnia, and there is nothing a user can edit *inside* a sound reference: changing it means
      replacing or removing it, and then it no longer points at our file. No mark needed, which
      is worth avoiding — a comment beside an audio tag is clutter in a field that is one tag.
    * **Text** — needs the mark, because text is exactly the thing a user edits in place.

    The media branch requires the field to be our refs and NOTHING else. "There is nothing to
    edit inside a sound reference" is true of the reference, not of the field: a user who typed
    ``[sound:omnia-1-A.mp3] (stress on the second syllable)`` has put their work beside our tag,
    and counting that as ours throws the note away under ``ours_only`` — the one setting chosen
    to prevent exactly that.
    """
    if not (content or "").strip():
        return False
    if is_untouched(content):
        return True
    refs = media_refs(content)
    if not refs or not all(r.startswith(OUR_MEDIA_PREFIX) for r in refs):
        return False
    # "Is there any TEXT left", not "is there anything left". `.strip()` only removes literal
    # whitespace, so a trailing `<br>`, an `&nbsp;` or a `<div>` wrapper counted as the user's
    # work — and a field that has been through Anki's contenteditable routinely comes back with
    # exactly those. `materialize` writes a bare tag; the field does not stay bare. Under
    # `not_ours` that meant re-paying for audio Omnia had already generated, and under
    # `ours_only` it meant refusing to refresh it while telling the user the content was theirs.
    return not strip_markup(content).strip()


def superseded_media(previous: str) -> list[str]:
    """The files ``previous`` referenced that Omnia wrote, and may therefore replace.

    Regenerating a field does NOT overwrite the old file: Anki renames on a real collision, so
    every regeneration that produces different bytes leaves the previous audio behind referenced
    by nobody. Five regenerations of one field is five files and one useful one; on a real
    collection that ran to hundreds of megabytes of audio nothing could play.

    Only files carrying :data:`OUR_MEDIA_PREFIX` are returned — another add-on's TTS, a pasted
    image, are somebody else's and are not ours to remove. This is the single function deciding
    whether a file may be trashed, and it lives here beside the prefix that decides it: the
    batch runner used to carry its own copy of this, the prefix and the regex, with both
    docstrings claiming to be the load-bearing one.
    """
    return our_media_refs(previous)


def may_overwrite(content: str, scope: str) -> bool:
    """Whether a field holding ``content`` may be regenerated over.

    An EMPTY field is always writable whatever the scope — there is nothing to protect, and a
    setting about overwriting has no business stopping a field from being filled in the first
    place.

    An unrecognised scope is treated as :data:`ALWAYS`, which is what the field did before this
    setting existed: a config carrying a value from a newer Omnia must not quietly stop
    generating.
    """
    if not (content or "").strip():
        return True
    if scope == OURS_ONLY:
        return is_ours(content)
    if scope == NOT_OURS:
        return not is_ours(content)
    # ALWAYS, and anything unrecognised, land here.
    return True
