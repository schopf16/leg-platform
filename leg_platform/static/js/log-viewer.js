/* The live view of a log file (D-092): ask the tail address every few seconds, append the new lines, keep the
   last part only. Text only (textContent), never markup. "Pause" stops asking; "Scroll to the end" follows. */
(function () {
  "use strict";

  var box = document.getElementById("log-viewer");
  var output = document.getElementById("log-output");
  if (!box || !output || !window.fetch) {
    return;
  }
  var url = box.dataset.tailUrl;
  var seconds = parseInt(box.dataset.pollSeconds, 10) || 2;
  var live = box.dataset.live === "1";
  var MAX_CHARACTERS = 400000;
  var offset = null;
  var fileId = "";
  var paused = false;
  var timer = null;
  var failures = 0;
  var state = document.getElementById("log-state");
  var pause = document.getElementById("log-pause");
  var follow = document.getElementById("log-follow");

  function say(text) {
    if (state) {
      state.textContent = text;
    }
  }

  function show(text, replace) {
    // A line break of a Windows file (CR LF) must not show as two (a blank line after every entry).
    text = text.replace(/\r\n?/g, "\n");
    var content = replace ? text : output.textContent + text;
    if (content.length > MAX_CHARACTERS) {
      content = content.slice(content.length - MAX_CHARACTERS);
      var cut = content.indexOf("\n");
      content = cut === -1 ? content : content.slice(cut + 1);
    }
    output.textContent = content;
    if (!follow || follow.checked) {
      output.scrollTop = output.scrollHeight;
    }
  }

  function query() {
    if (offset === null) {
      return url;
    }
    var glue = url.indexOf("?") === -1 ? "?" : "&";
    return url + glue + "offset=" + encodeURIComponent(String(offset)) + "&file_id=" + encodeURIComponent(fileId);
  }

  function schedule(delay) {
    window.clearTimeout(timer);
    if (live && !paused) {
      timer = window.setTimeout(poll, delay);
    }
  }

  function poll() {
    fetch(query(), { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } })
      .then(function (response) {
        var type = response.headers.get("content-type") || "";
        if (!response.ok || response.redirected || type.indexOf("application/json") !== 0) {
          var ended = new Error("ended");
          ended.ended = true;
          throw ended;
        }
        return response.json();
      })
      .then(function (data) {
        failures = 0;
        if (!data.exists) {
          say(box.dataset.textMissing);
        } else if (!paused) {
          say(live ? box.dataset.textLive : box.dataset.textStatic);
        }
        if (data.exists && (data.reset || offset === null)) {
          show(data.text, true);
        } else if (data.text) {
          show(data.text, false);
        }
        offset = data.offset;
        fileId = data.file_id;
        schedule(seconds * 1000);
      })
      .catch(function (error) {
        if (error && error.ended) {
          say(box.dataset.textEnded);
          return;
        }
        failures += 1;
        say(box.dataset.textRetry);
        schedule(seconds * 1000 * Math.min(failures + 1, 5));
      });
  }

  if (pause) {
    pause.addEventListener("click", function () {
      paused = !paused;
      pause.textContent = paused ? pause.dataset.labelContinue || pause.textContent : pause.dataset.labelPause || pause.textContent;
      if (paused) {
        window.clearTimeout(timer);
        say(box.dataset.textPaused);
      } else {
        say(box.dataset.textLive);
        poll();
      }
    });
  }

  // The first answer also works for an older file (it is read once); only a live file keeps asking.
  poll();
})();
