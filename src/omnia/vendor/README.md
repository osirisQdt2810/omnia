# vendor/

Vendored **pure-Python, cross-platform** third-party dependencies, added to `sys.path` at
add-on startup (Anki does not pip-install for users).

Empty by design today — the provider layer uses only the standard library. To add a dep,
list it in `requirements/requirements-vendor.txt` and run `python scripts/vendor_deps.py`.
No compiled/binary wheels (they break on the other OS). See `.claude/CONVENTIONS.md` Part 2.
