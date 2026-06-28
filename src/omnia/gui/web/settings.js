
(function () {
  function send(op, data, cb) {
    pycmd("omnia:" + JSON.stringify({ plugin: "settings", op: op, data: data }), cb);
  }
  function setStatus(card, text, failed) {
    var s = card.querySelector(".omnia-card-status");
    if (s) s.textContent = text;
    card.classList.toggle("omnia-failed", !!failed);
  }
  document.querySelectorAll(".omnia-switch input").forEach(function (input) {
    input.addEventListener("change", function () {
      var card = input.closest(".omnia-card");
      var id = input.getAttribute("data-id");
      var enabled = input.checked;
      send("toggle", { id: id, enabled: enabled }, function (res) {
        var active = !!(res && res.active);
        if (enabled && !active) {
          input.checked = false;
          setStatus(card, "failed to enable — see logs", true);
        } else {
          setStatus(card, active ? "active" : "off", false);
        }
      });
    });
  });
  document.querySelectorAll(".omnia-configure").forEach(function (btn) {
    btn.addEventListener("click", function () {
      send("configure", { id: btn.getAttribute("data-id") }, null);
    });
  });
})();
