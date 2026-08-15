"""Helpers shared by every script in this folder.

First inhabitant: making script output survive a non-UTF-8 terminal.

Windows sizes ``sys.stdout`` to the ANSI codepage (cp1252 on a Western/Vietnamese install),
not UTF-8. A single non-ASCII character in a ``print()`` therefore raises
``UnicodeEncodeError`` — and because these scripts print their *result* last, the script does
all of its work, succeeds, and THEN dies with a traceback. ``install_addon.py`` symlinked the
add-on correctly and still exited with a stack trace on ``"Tools → Omnia"``, which reads to a
developer following the README as a failed install.

Reconfiguring the streams is preferred over restricting the scripts to ASCII: the arrow in
"Tools → Omnia" mirrors what Anki's own menu shows, and a rule that no one may type a non-ASCII
character in a ``print()`` is a rule that gets broken the first time someone is not thinking
about Windows. ``errors="replace"`` means even a stream that cannot be reconfigured degrades to
mojibake rather than to a crash.

Not needed on Python 3.15+, where UTF-8 mode is the default; kept because the project supports
3.10+ (Anki's minimum) and Anki itself currently bundles 3.13.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Any


def enable_utf8_output() -> None:
    """Switch stdout/stderr to UTF-8 so non-ASCII output cannot crash the script.

    Safe to call more than once, and safe when the streams are already UTF-8, replaced by a
    test harness, or missing ``reconfigure`` entirely (pytest's capture objects) — in that case
    it does nothing rather than raising, since output encoding is never the point of the script
    that called it.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure: Any = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # A detached or closed stream is not worth taking the script down for.
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")
