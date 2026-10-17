/* The address directory page (D-090): while an import is queued or running, ask the status address every
   few seconds and write the answer into the elements of the page. Only textContent is set (never markup);
   the texts come from the server. Without this script the page shows the status of the last load. */
(function () {
  "use strict";

  var box = document.getElementById("import-status");
  if (!box || !window.fetch) {
    return;
  }
  var url = box.dataset.statusUrl;
  var seconds = parseInt(box.dataset.pollSeconds, 10) || 2;
  var failures = 0;

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, text) {
    var element = byId(id);
    if (element) {
      element.textContent = text;
    }
  }

  function setHidden(id, hidden) {
    var element = byId(id);
    if (element) {
      element.hidden = hidden;
    }
  }

  function note(text) {
    setText("import-script-note", text || "");
    setHidden("import-script-note", !text);
  }

  function dash(text) {
    return text ? text : "\u2013";
  }

  function render(status) {
    var panel = byId("import-box");
    if (panel) {
      panel.className = "import-box import-" + status.state;
    }
    setText("import-state", status.state_label);
    setText("import-message", status.message);
    setHidden("import-spinner", !status.active);
    var hasPercent = status.percent !== null && status.percent !== undefined;
    setHidden("import-progress-row", !hasPercent);
    if (hasPercent) {
      byId("import-progress").value = status.percent;
      setText("import-percent", status.percent + " %");
    }
    setText("import-detail", status.detail);
    setHidden("import-detail", !status.detail);
    setText("import-error-text", status.error);
    setHidden("import-error", !status.error);
    setText("import-requested-at", dash(status.requested_at));
    setText("import-started-at", dash(status.started_at));
    setText("import-finished-at", dash(status.finished_at));
    setText("import-last-automatic", dash(status.last_automatic_attempt_at));
    var directory = status.directory;
    setText("directory-data-date", dash(directory.data_date));
    setText("directory-imported-at", dash(directory.imported_at));
    setText("directory-row-count", directory.available ? String(directory.row_count) : "\u2013");
    setText("directory-file-size", directory.file_megabytes ? directory.file_megabytes + " MB" : "\u2013");
    setText("directory-warning", directory.warning);
    setHidden("directory-warning", !directory.warning);
    setText("import-automatic", status.automatic_text);
    setHidden("import-leave", !status.active);
    var start = byId("import-start");
    if (start) {
      start.disabled = status.active;
      var retry = status.state === "failed" || status.state === "interrupted";
      var label = retry ? start.dataset.labelRetry : start.dataset.labelStart;
      if (label) {
        start.value = label;
      }
    }
  }

  function poll() {
    fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" }
    })
      .then(function (response) {
        var type = response.headers.get("content-type") || "";
        if (!response.ok || response.redirected || type.indexOf("application/json") !== 0) {
          // Signed out (the answer is the login page or a 401) or no longer allowed: stop asking.
          var ended = new Error("ended");
          ended.ended = true;
          throw ended;
        }
        return response.json();
      })
      .then(function (status) {
        failures = 0;
        note("");
        render(status);
        if (status.active) {
          window.setTimeout(poll, seconds * 1000);
        }
      })
      .catch(function (error) {
        if (error && error.ended) {
          note(box.dataset.textEnded);
          setHidden("import-spinner", true);
          return;
        }
        failures += 1;
        note(box.dataset.textRetry);
        window.setTimeout(poll, seconds * 1000 * Math.min(failures + 1, 5));
      });
  }

  if (box.dataset.active === "1") {
    window.setTimeout(poll, seconds * 1000);
  }
})();
