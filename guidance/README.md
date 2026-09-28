# Guidance — how to test Omnia's big features by hand

The automated suite (`scripts/run_tests.sh`) proves the logic. These guides cover what it cannot:
driving the real feature in a real Anki, against a real provider, and knowing what "working"
looks like. One folder per large feature:

| Feature | Guide | What you test |
|---|---|---|
| Self-hosted models | [`local-server/`](local-server/README.md) | Your own GPU box serving text + image models to Omnia over an SSH tunnel |
| Smart Notes | [`smart-notes/`](smart-notes/README.md) | Generating fields: providers, tools, blocking, overwrite rules, batches |
| Sync | [`sync/`](sync/README.md) | Pulling decks, note types and settings from another computer |
| Typed Accuracy | [`typed-accuracy/`](typed-accuracy/README.md) | Grading typed answers into fail / pass / high-ease bands |

Not written yet: Phrase Check, Note Maintenance, Word Lookup, Audio Speed, and the reviewer
plugins (Auto Flip, Display Interval, Overdue Guard). Each of those has a `How to verify` block in
[`.claude/FEATURE_LOG.md`](../.claude/FEATURE_LOG.md) in the meantime.

## Ground rules

- **Test on a throwaway profile.** Anki → *File → Switch Profile → Add*. Several recipes regenerate
  or import content on purpose; do not run them on the collection you study from.
- **This repository is public.** Nothing in these guides names a real host, address, port, user or
  key, and nothing you add may either. Use placeholders (`<your-host>`) and keep real values in
  gitignored places: `~/.ssh/config`, `user_files/config/`, the add-on's secrets store.
- **Where commands run** is always stated: *on your computer* (the one running Anki), or *on the
  GPU host*.
- Every command in these guides was run when it was written. If one no longer works, the guide
  is wrong — fix it in the same PR as the change that broke it.
