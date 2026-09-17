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
NEVER = "never"
SCOPES = (ALWAYS, OURS_ONLY, NOT_OURS, NEVER)


#: The prefix :func:`~omnia.plugins.smart_notes.integration.batch.materialize` gives every media
#: file it writes. A real collection holds media from several sources — AwesomeTTS and HyperTTS
#: write ``googletts-…``, pasted images land as ``paste-…`` — and none of them are ours.
OUR_MEDIA_PREFIX = "omnia-"

_MEDIA_REF_RE = re.compile(
    r"\[sound:([^\]]+)\]|<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE
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


def is_ours(content: str) -> bool:
    """Whether this field's current contents are Omnia's, and unchanged since.

    Two kinds of field, answered two ways, because the honest answer differs:

    * **Media** — the filename says it. A field holding ``[sound:omnia-…]`` was written by
      Omnia, and there is nothing a user can edit *inside* a sound reference: changing it means
      replacing or removing it, and then it no longer points at our file. No mark needed, which
      is worth avoiding — a comment beside an audio tag is clutter in a field that is one tag.
    * **Text** — needs the mark, because text is exactly the thing a user edits in place.
    """
    if not (content or "").strip():
        return False
    if is_untouched(content):
        return True
    refs = media_refs(content)
    return bool(refs) and all(r.startswith(OUR_MEDIA_PREFIX) for r in refs)


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
    return scope != NEVER
