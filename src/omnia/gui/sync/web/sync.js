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

  if (check) {
    check.addEventListener("click", function () {
      const id = (peer && peer.value) || "";
      if (!id.trim()) {
        return;
      }
      check.disabled = true;
      check.textContent = "Checking…";
      send("connect", {id: id});
    });
  }

  const copy = document.getElementById("sync-copy");
  if (copy) {
    copy.addEventListener("click", function () {
      const id = document.getElementById("sync-id");
      if (!id) {
        return;
      }
      // The dialog is a webview inside Anki, where the clipboard API is unreliable; Python
      // owns the clipboard here so the button works the same on every platform.
      send("copy", {text: id.textContent || ""});
      copy.textContent = "Copied";
      setTimeout(function () {
        copy.textContent = "Copy";
      }, 1200);
    });
  }

  const regen = document.getElementById("sync-regen");
  if (regen) {
    regen.addEventListener("click", function () {
      regen.disabled = true;
      send("regenerate", {});
    });
  }

  if (peer) {
    peer.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && check) {
        check.click();
      }
    });
  }
})();
