
  function renderRow(row) {
    var tr = document.createElement("tr");
    tr.setAttribute("data-field", row.field);

    var tdName = document.createElement("td");
    tdName.className = "sn-fieldname"; tdName.textContent = row.field;
    tr.appendChild(tdName);

    var tdOn = document.createElement("td"); tdOn.className = "sn-center";
    tdOn.appendChild(makeCheckbox("sn-enabled", row.enabled));
    tr.appendChild(tdOn);

    var tdType = document.createElement("td");
    tdType.appendChild(makeSelect("sn-type", SN_TYPES, row.type || "text"));
    tr.appendChild(tdType);

    var tdPrompt = document.createElement("td");
    var wrap = document.createElement("div"); wrap.className = "sn-prompt-cell";
    var ta = document.createElement("textarea");
    ta.className = "sn-prompt"; ta.value = row.prompt || "";
    ta.placeholder = "e.g. Give the IPA for {{" + (baseSel.value || "Base") + "}}";
    var lock = document.createElement("button");
    lock.type = "button"; lock.className = "sn-lock" + (row.prompt_locked ? " sn-locked" : "");
    lock.textContent = row.prompt_locked ? "🔒" : "🔓";
    lock.title = "Lock to protect this prompt/type from Auto-smart";
    lock.addEventListener("click", function () {
      var locked = lock.classList.toggle("sn-locked");
      lock.textContent = locked ? "🔒" : "🔓";
    });
    wrap.appendChild(ta); wrap.appendChild(lock);
    tdPrompt.appendChild(wrap);
    tr.appendChild(tdPrompt);

    var providerVals = [""].concat(providers);
    var tdProvider = document.createElement("td");
    tdProvider.appendChild(makeSelect("sn-provider", providerVals, row.provider || ""));
    tr.appendChild(tdProvider);

    var tdModel = document.createElement("td");
    tdModel.appendChild(makeInput("sn-model", row.model, "(inherit)"));
    tr.appendChild(tdModel);

    var tdOver = document.createElement("td"); tdOver.className = "sn-center";
    tdOver.appendChild(makeCheckbox("sn-overwrite", row.overwrite));
    tr.appendChild(tdOver);

    return tr;
  }

  function renderRows(rows) {
    tbody.innerHTML = "";
    rows.forEach(function (row) { tbody.appendChild(renderRow(row)); });
    emptyEl.textContent = rows.length
      ? ""
      : "This note type has no other fields to generate. Add one with “+ Create field”.";
  }

  function collectRows() {
    var rows = [];
    Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
      var lock = tr.querySelector(".sn-lock");
      rows.push({
        field: tr.getAttribute("data-field"),
        enabled: tr.querySelector(".sn-enabled").checked,
        type: tr.querySelector(".sn-type").value,
        prompt: tr.querySelector(".sn-prompt").value,
        prompt_locked: lock.classList.contains("sn-locked"),
        provider: tr.querySelector(".sn-provider").value,
        model: tr.querySelector(".sn-model").value,
        voice: tr.getAttribute("data-voice") || "",
        overwrite: tr.querySelector(".sn-overwrite").checked
      });
    });
    return rows;
  }