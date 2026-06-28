---
description: Draft a new Architecture Decision Record in DECISIONS.md
---

Help me capture an architectural decision as an ADR in `.claude/DECISIONS.md`.

Process:

1. Ask me (briefly, in one message) what decision needs recording. Specifically gather:
   - The decision itself (one line)
   - The context — what problem prompted this
   - Why we chose it over alternatives
   - Expected positive and negative consequences

2. Read `.claude/DECISIONS.md` to find the next ADR number (highest existing N + 1).

3. Draft the ADR using the format defined at the top of DECISIONS.md:
   - Title: `## ADR-NNN: <short decision title>` (use 3-digit zero-padded number)
   - Date: today
   - Status: `Accepted` (unless I say otherwise)
   - All sections: Context, Decision, Rationale, Consequences, Alternatives considered

4. Show me the draft and confirm before appending to `.claude/DECISIONS.md`.

5. After appending, suggest whether this decision should also be mentioned in today's JOURNAL.md entry (recommend `/daily-wrap`).

Rules:

- ADRs are append-only. **Never** edit an existing ADR's content — to revise a decision, write a new ADR with status `Supersedes ADR-XXX` and update the old ADR's status to `Superseded by ADR-YYY`.
- Keep entries focused. Each ADR records **one** decision, not a bundle.
- Skip the ADR if the decision is trivial (naming a variable, picking a small library with no real tradeoff). ADRs are for decisions someone will ask "why did we do this?" about six months from now.