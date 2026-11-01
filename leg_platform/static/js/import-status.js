/* The data imports page (D-090, D-132, D-133): while an import is waiting or running, ask the status address every few
   seconds (not while the tab is hidden: the first look after it is shown is at once) and write the answer into the
   elements of the page. Only textContent and attributes are set (never markup); the
   texts come from the server. Without this script the page shows the status of the last load.

   Rules: a published figure that is filled is never overwritten by a dash or an empty value (only the "In progress"
   column may show a dash); the percent of a run never goes back; the remaining time is smoothed, counted down every
   second and hidden while the server cannot tell it. */
(function () {
  "use strict";

  var box = document.getElementById("import-status");
  if (!box || !window.fetch) {
    return;
  }
  var url = box.dataset.statusUrl;
  var seconds = parseInt(box.dataset.pollSeconds, 10) || 5;
  var DASH = "\u2013";
  var failures = 0;
  var memory = {}; // per source: run, percent, remaining (seconds, counted down here), state, announced step
  var timer = null;
  var next = null; // the timer of the next poll
  var inflight = false;

  function find(root, field) {
    return root.querySelector('[data-field="' + field + '"]');
  }

  function setText(element, text) {
    if (element && element.textContent !== text) {
      element.textContent = text;
    }
  }

  function keepFilled(element, value) {
    // The value of the server replaces the text unless it is empty and the text is filled.
    if (!element) {
      return;
    }
    var empty = value === "" || value === null || value === undefined || value === DASH;
    var current = element.textContent.trim();
    if (empty && current !== "" && current !== DASH) {
      return;
    }
    setText(element, empty ? DASH : value);
  }

  function note(text) {
    var element = document.getElementById("import-script-note");
    if (element) {
      element.textContent = text || "";
      element.hidden = !text;
    }
  }

  function announce(text) {
    var live = document.getElementById("import-live");
    if (live) {
      live.textContent = text;
    }
  }

  function clock(total) {
    var s = Math.max(0, Math.round(total));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var r = s % 60;
    var tail = (r < 10 ? "0" : "") + r;
    if (h > 0) {
      return h + ":" + (m < 10 ? "0" : "") + m + ":" + tail;
    }
    return (m < 10 ? "0" : "") + m + ":" + tail;
  }

  function showRemaining(root, mem) {
    var element = find(root, "remaining");
    if (!element) {
      return;
    }
    if (mem.remaining === null) {
      element.hidden = true;
      element.textContent = "";
      return;
    }
    element.hidden = false;
    setText(element, clock(mem.remaining));
  }

  function renderButton(root, source) {
    var button = root.querySelector("input.import-start");
    if (!button) {
      return;
    }
    var label = {
      running: box.dataset.textRunning,
      queued: box.dataset.textWaiting,
      failed: box.dataset.textRetryRun,
      interrupted: box.dataset.textRetryRun
    }[source.state] || box.dataset.textUpdate;
    if (label) {
      button.value = label;
    }
    if (source.busy) {
      button.setAttribute("disabled", "");
    } else {
      button.removeAttribute("disabled");
    }
  }

  function renderSource(source) {
    var root = box.querySelector('tbody[data-source="' + source.key + '"]');
    if (!root) {
      return;
    }
    var mem = memory[source.key] || (memory[source.key] = { run: "", percent: 0, remaining: null, state: "", step: -1 });
    var running = source.state === "running";
    var started = running && mem.state !== "running";
    if (!running || mem.run !== source.started_at) {
      mem.run = running ? source.started_at : "";
      mem.percent = 0;
      mem.remaining = null;
      mem.step = -1;
    }
    root.dataset.state = source.state;
    var dot = find(root, "dot");
    if (dot) {
      dot.className = "status-dot status-" + source.tone;
    }
    setText(find(root, "state"), source.state_label);
    var position = find(root, "position");
    if (position) {
      position.hidden = !source.position;
      setText(position, source.position ? "(" + source.position + ")" : "");
    }

    var progress = find(root, "progress");
    if (progress) {
      progress.hidden = !running || source.percent === null || source.percent === undefined;
      if (running && source.percent !== null && source.percent !== undefined) {
        mem.percent = Math.max(mem.percent, source.percent);
        var bar = find(root, "bar");
        if (bar) {
          bar.value = mem.percent;
        }
        setText(find(root, "percent"), mem.percent + " %");
      }
    }
    if (running && source.remaining_seconds !== null && source.remaining_seconds !== undefined) {
      mem.remaining = mem.remaining === null ? source.remaining_seconds : 0.7 * mem.remaining + 0.3 * source.remaining_seconds;
    } else {
      mem.remaining = null;
    }
    showRemaining(root, mem);

    var error = find(root, "error");
    if (error) {
      setText(error, source.error);
      error.hidden = !source.error;
      if (source.error) {
        error.setAttribute("role", "alert");
      } else {
        error.removeAttribute("role");
      }
    }
    var stall = find(root, "stall");
    if (stall) {
      setText(stall, source.stall);
      stall.hidden = !source.stall;
    }
    keepFilled(find(root, "last_run"), source.last_run);
    keepFilled(find(root, "key_figures"), source.key_figures);
    var warning = find(root, "warning");
    if (warning) {
      setText(warning, source.warning);
      warning.hidden = !source.warning;
    }

    // The figures: the published value never turns into a dash; the in-progress column exists while it runs.
    root.querySelectorAll(".import-pending").forEach(function (cell) {
      cell.hidden = !source.pending;
    });
    (source.figures || []).forEach(function (figure) {
      var row = root.querySelector('tr[data-figure="' + figure.key + '"]');
      if (!row) {
        return;
      }
      keepFilled(row.querySelector("[data-published]"), figure.published);
      var pending = row.querySelector("[data-pending]");
      if (pending) {
        setText(pending, figure.pending === null || figure.pending === undefined || figure.pending === "" ? DASH : figure.pending);
      }
    });
    if (started) {
      var details = root.querySelector("details");
      if (details) {
        details.open = true;
      }
    }
    renderButton(root, source);

    // The live region says the state words and every 25 percent, not every number.
    if (mem.state !== source.state) {
      announce(source.name + ": " + source.state_label);
    } else if (running && Math.floor(mem.percent / 25) > mem.step && mem.step >= 0) {
      announce(source.name + ": " + mem.percent + " %");
    }
    if (running) {
      mem.step = Math.max(mem.step, Math.floor(mem.percent / 25));
    }
    mem.state = source.state;
  }

  function render(status) {
    setText(document.getElementById("import-summary"), status.summary || "");
    var notice = document.getElementById("import-worker-notice");
    if (notice) {
      setText(notice, status.worker_notice || "");
      notice.hidden = !status.worker_notice;
    }
    var all = document.getElementById("import-start-all");
    if (all) {
      if (status.all_busy) {
        all.setAttribute("disabled", "");
      } else {
        all.removeAttribute("disabled");
      }
    }
    (status.sources || []).forEach(renderSource);
  }

  function tick() {
    // Once a second: count the remaining time down without asking the server.
    Object.keys(memory).forEach(function (key) {
      var mem = memory[key];
      if (mem.remaining !== null && mem.remaining > 0) {
        mem.remaining -= 1;
        var root = box.querySelector('tbody[data-source="' + key + '"]');
        if (root) {
          showRemaining(root, mem);
        }
      }
    });
  }

  function later(delay) {
    if (next !== null) {
      window.clearTimeout(next);
    }
    next = window.setTimeout(poll, delay);
  }

  function poll() {
    next = null;
    if (document.visibilityState === "hidden") {
      // A hidden tab asks nothing; the change to visible asks at once (see below).
      return;
    }
    inflight = true;
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
        inflight = false;
        failures = 0;
        note("");
        render(status);
        if (status.active) {
          later(seconds * 1000);
        } else if (timer !== null) {
          window.clearInterval(timer);
          timer = null;
        }
      })
      .catch(function (error) {
        inflight = false;
        if (error && error.ended) {
          note(box.dataset.textEnded);
          return;
        }
        failures += 1;
        note(box.dataset.textRetry);
        later(seconds * 1000 * Math.min(failures + 1, 5));
      });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible" && timer !== null && !inflight && next === null) {
      poll();
    }
  });

  if (box.dataset.active === "1") {
    timer = window.setInterval(tick, 1000);
    window.setTimeout(poll, 0);
  }
})();
