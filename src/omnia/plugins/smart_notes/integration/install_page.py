"""The page the web-clipper install lands on, because Chrome refuses to open its own.

Chrome silently DROPS any ``chrome://`` URL passed on the command line and opens the new-tab
page instead. Measured on Chrome 152, macOS 15.6 and Windows 11, every spelling: with and
without the trailing slash, ``--new-window``, ``--app=``, and ``chrome://settings/`` as a
control. An ``https://`` or ``file://`` URL in the same position opens normally, so this is a
scheme filter, not a launch bug — and it means "we opened the extensions page for you" is a
promise the installer cannot keep.

What it CAN do is open a local page in the right profile. So the install writes this one: the
folder to load, a button that copies it, the address to paste, and the three steps in order.
That leaves the user one paste from the end instead of on a blank tab wondering what happened.

Pure: builds a string from a path and a profile name. No Anki, no Qt, no filesystem.
"""

from __future__ import annotations

import html
from pathlib import Path

#: What the user must paste into the address bar. Chrome blocks a LINK to it from a page just
#: as it blocks the command line, so this is text with a copy button, not an ``<a href>``.
EXTENSIONS_URL = "chrome://extensions/"

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Finish installing the Omnia Web Clipper</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font: 15px/1.6 -apple-system, "Segoe UI", system-ui, sans-serif;
    margin: 0; padding: 48px 24px; background: #f6f7f9; color: #1c1e21;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #16181c; color: #e8eaed; }}
    .card {{ background: #202226 !important; box-shadow: none !important; }}
    code {{ background: #2b2e33 !important; }}
  }}
  .card {{
    max-width: 640px; margin: 0 auto; background: #fff; border-radius: 12px;
    padding: 32px 36px; box-shadow: 0 1px 3px rgba(0,0,0,.12);
  }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  p.lead {{ margin: 0 0 28px; color: #5f6368; }}
  ol {{ margin: 0; padding-left: 22px; }}
  li {{ margin-bottom: 18px; }}
  code {{
    background: #eef0f3; border-radius: 6px; padding: 3px 7px;
    font: 13px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace;
    word-break: break-all;
  }}
  button {{
    font: inherit; font-size: 13px; cursor: pointer; margin-left: 8px;
    border: 1px solid #c7cad1; background: #fff; color: inherit;
    border-radius: 6px; padding: 3px 10px;
  }}
  button:hover {{ background: #eef0f3; }}
  .note {{ margin-top: 26px; font-size: 13px; color: #5f6368; }}
</style>
</head>
<body>
<div class="card">
  <h1>Almost there &mdash; one manual step</h1>
  <p class="lead">{lead}</p>
  <ol>
    <li>Copy <code id="omnia-url">{url}</code>
        <button data-copy="omnia-url">Copy</button>
        and paste it into the address bar above, then press Enter.</li>
    <li>Turn on <strong>Developer mode</strong> (top-right of that page).</li>
    <li>Click <strong>Load unpacked</strong> and pick this folder:<br>
        <code id="omnia-path">{path}</code>
        <button data-copy="omnia-path">Copy</button></li>
  </ol>
  <p class="note">{note}</p>
</div>
<script>
// What gets copied is READ BACK OUT OF THE DOM, never baked into a JS string literal: a
// Windows path is full of backslashes, and one inside a naive literal is a broken escape
// that takes the script down and with it the only buttons on the page.
function flash(button) {{
  var was = button.textContent;
  button.textContent = "Copied";
  setTimeout(function () {{ button.textContent = was; }}, 1500);
}}
// file:// is a secure context in Chrome, so the async clipboard API is normally available;
// this is for the browser, or the policy, where it is not.
function fallback(text, button) {{
  var area = document.createElement("textarea");
  area.value = text;
  document.body.appendChild(area);
  area.select();
  try {{ document.execCommand("copy"); flash(button); }} catch (e) {{ /* nothing left to try */ }}
  document.body.removeChild(area);
}}
document.querySelectorAll("button[data-copy]").forEach(function (button) {{
  button.addEventListener("click", function () {{
    var text = document.getElementById(button.getAttribute("data-copy")).textContent;
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(text).then(
        function () {{ flash(button); }},
        function () {{ fallback(text, button); }}
      );
    }} else {{
      fallback(text, button);
    }}
  }});
}});
</script>
</body>
</html>
"""


def render_install_page(extension_dir: str | Path, profile_name: str = "") -> str:
    """Return the HTML for the "finish the install" page.

    Args:
        extension_dir: The cloned folder the user must pick in "Load unpacked".
        profile_name: The Chrome profile this page was opened in, when it is known. Named in
            the page because a user with eight profiles needs to see WHICH one is about to get
            the extension — installing into the wrong one is the failure this whole path exists
            to avoid.

    Returns:
        A complete, self-contained HTML document (no external assets: it is loaded over
        ``file://``, where a missing stylesheet would just render as nothing).
    """
    lead = (
        "Chrome does not allow an extension to be installed from outside the Web Store, so "
        "the last step is yours. It takes about ten seconds."
    )
    note = (
        f"This page opened in your <strong>{html.escape(profile_name)}</strong> profile "
        "&mdash; the one Chrome used last. The extension will be installed there."
        if profile_name
        else "The extension is installed into whichever Chrome profile this page is open in."
    )
    return _PAGE.format(
        lead=lead,
        note=note,
        url=html.escape(EXTENSIONS_URL),
        path=html.escape(str(extension_dir)),
    )
