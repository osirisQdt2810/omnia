(function () {
  "use strict";

  function send(op, data, cb) {
    pycmd("omnia:" + JSON.stringify({plugin: "sync", op: op, data: data}), cb);
  }

  const peer = document.getElementById("sync-peer");
  const check = document.getElementById("sync-check");
  const sharing = document.getElementById("sync-sharing");

  if (sharing) {
    sharing.addEventListener("change", function () {
      // Disabled while Python answers: the switch controls a SOCKET, and a second click
      // mid-bind would ask for the opposite of something not finished yet.
      sharing.disabled = true;
      send("sharing", {on: sharing.checked});
    });
  }

  const peerCode = document.getElementById("sync-peer-code");

  if (check) {
    check.addEventListener("click", function () {
      const id = (peer && peer.value) || "";
      const code = (peerCode && peerCode.value) || "";
      // Both halves are required, and the empty one gets the focus rather than a message: the
      // user knows what is missing the moment the cursor lands in it.
      if (!id.trim()) { if (peer) peer.focus(); return; }
      if (!code.trim()) { if (peerCode) peerCode.focus(); return; }
      check.disabled = true;
      check.textContent = "Checking…";
      send("connect", {id: id, code: code});
    });
  }

  // One handler for every Copy button; each names what it copies with data-copy. The dialog is
  // a webview inside Anki, where the clipboard API is unreliable, so Python owns the clipboard
  // and the button behaves the same on every platform.
  Array.prototype.forEach.call(
    document.querySelectorAll("[data-copy]"),
    function (button) {
      button.addEventListener("click", function () {
        const source = document.getElementById(button.getAttribute("data-copy"));
        if (!source) { return; }
        send("copy", {text: source.textContent || ""});
        button.textContent = "Copied";
        setTimeout(function () { button.textContent = "Copy"; }, 1200);
      });
    }
  );

  const regen = document.getElementById("sync-regen");
  if (regen) {
    regen.addEventListener("click", function () {
      regen.disabled = true;
      send("regenerate", {});
    });
  }

  [peer, peerCode].forEach(function (box) {
    if (!box) { return; }
    box.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && check) { check.click(); }
    });
  });
})();
