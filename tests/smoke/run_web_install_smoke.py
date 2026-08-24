"""Drive the web clipper's last step in a REAL Chrome, on either platform.

The unit tests can prove what :func:`render_install_page` returns; they cannot prove that
Chrome opens it, that its script parses, or that its button reaches the system clipboard. This
does, against a throwaway profile, the same way ``ClipperInstaller._open_chrome`` does it:
``--profile-directory`` plus the page's ``file://`` URI.

It exists because the thing it replaced looked fine and did not work. Chrome silently DROPS a
``chrome://`` URL given on the command line — measured here on Chrome 152, macOS 15.6 and
Windows 11, for every spelling including ``--new-window``, ``--app=`` and ``chrome://settings/``
as a control — so the install's "we opened chrome://extensions for you" landed the user on a
blank new tab. ``file://`` in the same position opens normally, which is what this pins.

Run it with any Python 3.10+ (it does not need Anki):
    python tests/smoke/run_web_install_smoke.py

Stdlib only — Anki's interpreter has no websocket package — so the WebSocket framing that
carries CDP is inline below.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
SRC = _REPO / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(_REPO / "scripts"))

from common import enable_utf8_output  # noqa: E402

enable_utf8_output()

import importlib.util  # noqa: E402

# Loaded by path, not as ``omnia.…``: the package's __init__ wants the vendored deps, and this
# module is deliberately stdlib-only so it can be checked without them.
spec = importlib.util.spec_from_file_location(
    "install_page", SRC / "omnia/plugins/smart_notes/integration/install_page.py"
)
install_page = importlib.util.module_from_spec(spec)
sys.modules["install_page"] = install_page
spec.loader.exec_module(install_page)


class WS:
    """The smallest RFC6455 text client that can carry CDP."""

    def __init__(self, url: str) -> None:
        self._next_id = 0
        rest = url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        assert b"101" in buf.split(b"\r\n")[0], buf[:80]
        self.buf = buf.split(b"\r\n\r\n", 1)[1]

    def send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        n = len(data)
        header = b"\x81"
        if n < 126:
            header += struct.pack("!B", 0x80 | n)
        elif n < 1 << 16:
            header += struct.pack("!BH", 0x80 | 126, n)
        else:
            header += struct.pack("!BQ", 0x80 | 127, n)
        self.sock.sendall(header + mask + masked)

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self) -> dict:
        while True:
            b1, b2 = self._read(2)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            payload = self._read(length)
            if b1 & 0x0F == 1:
                return json.loads(payload)

    def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        wanted = self._next_id
        self.send({"id": wanted, "method": method, "params": params or {}})
        while True:  # CDP interleaves events with replies; skip anything not ours
            msg = self.recv()
            if msg.get("id") == wanted:
                return msg


def main() -> int:
    platform = sys.platform
    exe = {
        "darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    }.get(platform, r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not Path(exe).exists():
        print("FAIL chrome not found:", exe)
        return 1

    tmp = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")
    udd = tmp / "omnia-livepage-udd"
    shutil.rmtree(udd, ignore_errors=True)
    udd.mkdir(parents=True)

    # A path with the shapes that break naive rendering: a backslash run on Windows, a space.
    folder = (
        r"C:\Users\PC\AppData\Roaming\Anki2\addons21\omnia\user_files\clippers\web_clipper"
        if platform.startswith("win")
        else "/Users/phucnp/Library/Application Support/Anki2/clippers/web_clipper"
    )
    page = tmp / "omnia-finish-install.html"
    page.write_text(install_page.render_install_page(folder, "moreh.com.vn"), "utf-8")

    port = 9612
    argv = [
        exe,
        "--profile-directory=Profile 7",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={udd}",
        "--no-first-run",
        "--no-default-browser-check",
        page.as_uri(),
    ]
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target = None
    for _ in range(40):
        time.sleep(1)
        try:
            listed = json.load(
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2)
            )
        except Exception:
            continue
        target = next(
            (t for t in listed if t.get("url", "").startswith("file://")), None
        )
        if target:
            break

    failures = []

    def check(label, ok, detail=""):
        print(("OK   " if ok else "FAIL ") + label + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    check(
        "1. the file:// page actually opened",
        target is not None,
        target["url"][:70] if target else "no page target",
    )
    if not target:
        proc.terminate()
        return 1

    ws = WS(target["webSocketDebuggerUrl"])
    ws.call("Runtime.enable")

    def js(expr):
        r = ws.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True},
        )
        return r.get("result", {}).get("result", {}).get("value")

    check(
        "2. it is the finish-install page",
        js("document.title") == "Finish installing the Omnia Web Clipper",
        js("document.title"),
    )
    check(
        "3. the folder is on the page, unmangled",
        js("document.getElementById('omnia-path').textContent") == folder,
        js("document.getElementById('omnia-path').textContent"),
    )
    check(
        "4. the address to paste is on the page",
        js("document.getElementById('omnia-url').textContent")
        == "chrome://extensions/",
    )
    check(
        "5. the profile it opened in is named",
        "moreh.com.vn" in (js("document.body.innerText") or ""),
    )
    check(
        "6. the script parsed (no broken escape)",
        js("typeof flash === 'function' && typeof fallback === 'function'"),
    )
    check(
        "7. both copy buttons are wired",
        js("document.querySelectorAll('button[data-copy]').length") == 2,
    )

    # Clipboard: grant the permission, click the page's own button, read it back.
    ws.call(
        "Browser.grantPermissions",
        {"permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"]},
    )
    # Read the label BEFORE its 1500ms timeout puts "Copy" back, then read the clipboard.
    label = js(
        "(function () {"
        "  var b = document.querySelector('button[data-copy=\"omnia-path\"]');"
        "  b.click();"
        "  return new Promise(function (r) { setTimeout(function () { r(b.textContent); }, 400); });"
        "})()"
    )
    pasted = js("navigator.clipboard.readText()")
    check(
        "8. clicking Copy puts the exact folder on the clipboard",
        pasted == folder,
        repr(pasted)[:80],
    )
    check("9. the button confirms it copied", label == "Copied", repr(label))

    proc.terminate()
    print("=" * 60)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("FINISH-INSTALL PAGE VERIFIED IN REAL CHROME (9 checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
