(function () {
  "use strict";

  function send(op, data, cb) {
    pycmd("omnia:" + JSON.stringify({plugin: "sync_offer", op: op, data: data}), cb);
  }

  const SEP = "::";
  const rows = Array.prototype.slice.call(document.querySelectorAll(".offer-row"));
  const byName = new Map();
  rows.forEach(function (row) { byName.set(row.getAttribute("data-deck"), row); });

  // --- expand / collapse ---------------------------------------------------------------
  // Presentation only, so it stays here: which rows are on screen is not a decision anybody
  // else needs to agree with. Structure is read out of the NAMES — a sub-deck's name is its
  // parent's plus "::" — rather than from a second model that could drift from the page.
  function descendantsOf(name) {
    const prefix = name + SEP;
    return rows.filter(function (row) {
      return row.getAttribute("data-deck").indexOf(prefix) === 0;
    });
  }

  function childrenOf(name) {
    const prefix = name + SEP;
    return rows.filter(function (row) {
      const deck = row.getAttribute("data-deck");
      return deck.indexOf(prefix) === 0 && deck.indexOf(SEP, prefix.length) === -1;
    });
  }

  function collapse(row) {
    row.classList.remove("offer-open");
    // The whole subtree, not just the children: reopening shows one level, so a grandchild left
    // visible would appear under a child that is shut.
    descendantsOf(row.getAttribute("data-deck")).forEach(function (below) {
      below.classList.add("offer-hidden");
      below.classList.remove("offer-open");
    });
  }

  function expand(row) {
    row.classList.add("offer-open");
    childrenOf(row.getAttribute("data-deck")).forEach(function (child) {
      child.classList.remove("offer-hidden");
    });
  }

  rows.forEach(function (row) {
    const arrow = row.querySelector("button.offer-arrow");
    if (!arrow) { return; }
    arrow.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (row.classList.contains("offer-open")) { collapse(row); } else { expand(row); }
    });
  });

  // --- picking -------------------------------------------------------------------------
  // Python owns WHAT a click selects; this only paints the answer. The page kept its own copy
  // of that rule once and got it wrong in a way no test could see: it read the set while still
  // mutating it, and every expandable sibling of a picked deck came out looking half-selected.
  const tallyText = document.getElementById("offer-tally");
  const go = document.getElementById("offer-go");
  const chips = new Map();
  let busy = false;

  document.querySelectorAll(".offer-chip").forEach(function (chip) {
    chips.set(chip.getAttribute("data-kind") + "/" + chip.getAttribute("data-name"), chip);
  });

  const CLASSES = ["offer-picked", "offer-partial", "offer-needed", "offer-dropped", "offer-idle"];

  function paint(element, state) {
    CLASSES.forEach(function (name) { element.classList.remove(name); });
    element.classList.add("offer-" + state);
  }

  function applyStates(answer) {
    busy = false;
    if (!answer) { return; }
    Object.keys(answer.decks || {}).forEach(function (name) {
      const row = byName.get(name);
      if (row) { paint(row, answer.decks[name]); }
    });
    ["note_types", "features"].forEach(function (kind) {
      const key = kind === "note_types" ? "note-type" : "feature";
      Object.keys(answer[kind] || {}).forEach(function (name) {
        const chip = chips.get(key + "/" + name);
        if (chip) { paint(chip, answer[kind][name]); }
      });
    });
    render(answer.tally);
  }

  function render(tally) {
    if (!tally) { return; }
    const parts = [];
    if (tally.decks) {
      parts.push(tally.decks + (tally.decks === 1 ? " deck" : " decks"));
      parts.push(tally.cards.toLocaleString() + (tally.cards === 1 ? " card" : " cards"));
    }
    if (tally.note_types) {
      parts.push(tally.note_types + (tally.note_types === 1 ? " note type" : " note types"));
    }
    if (tally.dropped) {
      parts.push(tally.dropped + " left behind");
    }
    if (tally.features) {
      parts.push(tally.features + (tally.features === 1 ? " setting" : " settings"));
    }
    tallyText.textContent = parts.length ? parts.join(", ") : "Nothing picked yet.";
    go.disabled = !tally.decks && !tally.features;
  }

  // One click at a time: the answer decides what every row looks like, and a second click sent
  // before the first came back would paint an older answer over a newer one.
  function ask(op, data) {
    if (busy) { return; }
    busy = true;
    send(op, data, applyStates);
  }

  rows.forEach(function (row) {
    const name = row.querySelector("button.offer-name");
    if (!name) { return; }
    name.addEventListener("click", function () {
      ask("pick_deck", {name: row.getAttribute("data-deck")});
    });
  });

  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      const kind = chip.getAttribute("data-kind");
      ask(kind === "feature" ? "pick_feature" : "drop_note_type",
          {name: chip.getAttribute("data-name")});
    });
  });

  go.addEventListener("click", function () {
    go.disabled = true;
    go.textContent = "Copying…";
    send("pull", {}, function (answer) {
      go.textContent = "Copy to this computer";
      go.disabled = false;
      if (answer && answer.message) { tallyText.textContent = answer.message; }
    });
  });

  render({decks: 0, cards: 0, note_types: 0, dropped: 0, features: 0});
})();
