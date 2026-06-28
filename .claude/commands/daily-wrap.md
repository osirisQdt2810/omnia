---
description: End-of-day journal entry summarizing today's session
---

Update `.claude/JOURNAL.md` with an entry for today's work session.

Process:

1. Review this conversation (and recent context) to identify what was accomplished, what's half-done, and what decisions were made.
2. Read existing `.claude/JOURNAL.md` to see the exact format used and to avoid duplicating an entry for today.
3. If today already has an entry, **update** it (extend "Done", refine "In progress", etc.) — do not create a duplicate.
4. Otherwise, **prepend** a new entry at the top (newest-first ordering).

Each entry must capture:

- **Done today** — completed items. Include file paths if useful for future-me.
- **In progress** — half-finished work with concrete pointers: `file.py:line` and a one-line "Status:" explaining what's left.
- **Decisions made** — brief notes. If a decision is architecturally significant, mention it should be promoted to an ADR via `/adr`.
- **Blockers** — what's stopping progress, including external dependencies (PM input, library bug, etc.).
- **Tomorrow** — 2-3 concrete next steps. Each should be actionable, not vague.

Rules:

- Be concise. Bullets, not paragraphs.
- Capture enough context that future-me (or future-Claude) can resume the work without re-reading code.
- Never invent items — only capture what actually happened in this session.
- If the session was light (e.g. just questions, no code changes), keep the entry small. Don't pad.
- Use the date format `YYYY-MM-DD (Weekday)` to match existing entries.

After writing the entry, confirm with me by showing the new entry and ask if anything should be adjusted before saving.