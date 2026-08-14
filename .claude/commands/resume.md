---
description: Resume work from last session by reading the journal
---

Help me pick up where I left off in this project.

Steps:

1. Read `.claude/JOURNAL.md` and find the most recent 1-2 entries (entries are ordered newest-first).
2. If the latest journal entry references specific files (e.g. `file.py:line`), briefly Read those files to refresh context on the in-progress code.

Then produce this summary for me:

```
## Where you left off

**Last session**: <date from most recent journal entry>

### You were working on
<the "In progress" items from latest journal>

### Status of in-progress code
<brief note from reading the referenced files — what state are they in?>

### Blockers (if any)
<from latest journal>

### Planned for today
<the "Tomorrow" section from latest journal>

### Suggested starting point
<your recommendation: which item to tackle first, considering dependencies
and which blockers are still active>
```

Keep the summary tight. The goal is to get me productive in under 60 seconds, not to write a report.

If `.claude/JOURNAL.md` doesn't exist yet or has no real entries, just say so and offer to help me set up the first entry.