## Context
<!-- Why is this change needed? What problem does it solve? -->
<!-- Link related issues or incidents -->
- Related issue:
- Background / motivation:
- Constraints / assumptions:

---

## Content / Changes
<!-- What exactly changed in this PR -->
-
-
-

<!-- Optional: call out non-obvious changes -->
- Refactors:
- New features:
- Removed / deprecated behavior:

---

## Test Plan
<!-- How was this change validated -->

### Test Details
<!-- Commands, configs, or steps used to test -->
-

### Test Output / Feature Demonstration
<!-- Paste test output, logs, screenshots, benchmarks, or example requests/responses -->
-

---

## Omnia checklist
<!-- Delete a line only when it genuinely cannot apply. "N/A — why" is a valid answer. -->

- **Platforms**: this add-on ships on Windows + macOS + Ubuntu. Exercised on:
  <!-- CI covers all three; say so, or name what you ran by hand and where -->
- **Python**: runtime code must import on 3.10–3.13 (Anki's minimum through the current bundle).
  Anything touching `import`s, stdlib boundaries (`tomllib`), or dataclass/typing behaviour needs
  the older end checked, not just the newest.
- **Persisted config (ADR-010)**: does this change anything stored in the collection config?
  - New/renamed/removed keys:
  - What an OLDER Omnia does when it syncs this: <!-- must not lose the key or refuse to load -->
- **Shared seams**: does this touch `core/*` (plugin system, ease pipeline, web injector,
  providers, config store)? Every plugin inherits the change — name who else is affected.
  `core/*` must not import `plugins/*`.
- **Vendored deps**: any new `vendor/` entry must be pure-Python and cross-platform (no compiled
  wheels), or the add-on breaks on Windows.
- **User-visible change / migration**: does an existing user's setup behave differently after
  upgrading? Say what they will notice and what, if anything, they must do.
- **Docs**: `.claude/FEATURE_LOG.md` for a large feature/new provider/seam change;
  `.claude/DECISIONS.md` (ADR) when a core pattern changes.
