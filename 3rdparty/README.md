# Omnia companion clippers (`3rdparty/`)

This directory hosts the **companion capture tools** for Omnia. Each is an independent project
tracked here as a **git submodule** — so its own history, issues, and releases live in its own
repository, and Omnia only pins the exact commit it was verified against.

| Submodule | What it is | How you capture | Repo |
| --------- | ---------- | --------------- | ---- |
| [`omnia-web-clipper`](omnia-web-clipper) | Chrome/Chromium MV3 extension | Double-click a word (floating **"+"**) or right-click a phrase on any web page | <https://github.com/osirisQdt2810/omnia-web-clipper> |
| [`omnia-desktop-clipper`](omnia-desktop-clipper) | Standalone PyQt6 tray app (macOS/Windows/Linux) | Global hotkey on the selection in **any** app, or screen-region **OCR** for non-selectable text | <https://github.com/osirisQdt2810/omnia-desktop-clipper> |

Both send the captured **word/phrase + surrounding context** into your running Anki through the
[AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on, tagged so that Omnia's
**Smart Notes** feature auto-generates the rest of the card. Each is enabled as an integration
in **Tools → Omnia** → the **Smart Notes** plugin's **Configure** → **Integrations** tab. Neither
is part of the Python add-on itself
— they talk to Anki over the local network — which is why they live here as submodules rather than
inside `src/omnia/`.

## Working with the submodules

```bash
# Fresh clone of Omnia, including the clippers:
git clone --recurse-submodules <omnia-repo-url>

# Existing clone that predates the submodules (or after pulling a submodule bump):
git submodule update --init --recursive

# Update a clipper to its latest upstream main and pin the new commit in Omnia:
git submodule update --remote 3rdparty/omnia-web-clipper
git add 3rdparty/omnia-web-clipper && git commit -m "chore: bump web-clipper submodule"
```

Full install, configuration, and usage instructions live in **each submodule's own `README.md`**.
