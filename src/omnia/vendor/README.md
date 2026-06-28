# vendor/

Vendored **pure-Python, cross-platform** third-party dependencies, added to `sys.path` at
add-on startup (Anki does not pip-install for users).

Everything lives in `universal/` — a single dir that works on macOS and Windows alike. The
deps are pydantic v1 (typed config), tomli_w (write TOML), and rsa + pyasn1 (gemini_vertex
service-account JWT signing). No compiled/binary wheels (`*.so`/`*.pyd`) — they break on the
other OS. Pydantic v1 is used (not v2) precisely because v2 needs the compiled `pydantic_core`.

To refresh: edit `requirements/requirements-vendor.txt` and run
`python scripts/vendor_deps.py`. See `.claude/CONVENTIONS.md` Part 2.
