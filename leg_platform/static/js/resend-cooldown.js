/* Counts down the pause before a mail can be requested again (templates/accounts/email_sent.html).
   The server enforces the pause; this only shows it. Without JavaScript the button is active only
   when the page was loaded after the pause. */
(function () {
  "use strict";
  var form = document.querySelector("[data-resend]");
  if (!form) {
    return;
  }
  var button = form.querySelector("[data-resend-button]");
  var note = form.querySelector("[data-resend-note]");
  var counter = form.querySelector("[data-resend-seconds]");
  var remaining = parseInt(form.getAttribute("data-cooldown"), 10) || 0;
  if (remaining <= 0 || !button) {
    return;
  }
  var deadline = Date.now() + remaining * 1000;
  var timer = window.setInterval(function () {
    var left = Math.ceil((deadline - Date.now()) / 1000);
    if (left <= 0) {
      window.clearInterval(timer);
      button.disabled = false;
      if (note) {
        note.hidden = true;
      }
      return;
    }
    if (counter) {
      counter.textContent = String(left);
    }
  }, 500);
}());
