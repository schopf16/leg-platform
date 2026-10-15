/* Print and copy for the page of the recovery codes. Both buttons are rendered hidden by the
   server and shown here only if the browser can do it, so that without this script the page
   stays complete (the person can still select the codes and use the print function of the
   browser). The codes are only read from the page: they are not stored, logged or sent anywhere
   (CSP: script-src 'self'). */
(() => {
  "use strict";
  const status = document.querySelector("[data-codes-status]");
  const say = (text) => {
    if (status) status.textContent = text;
  };

  document.querySelectorAll("[data-codes-print]").forEach((button) => {
    if (typeof window.print !== "function") return;
    button.hidden = false;
    button.addEventListener("click", () => window.print());
  });

  document.querySelectorAll("[data-codes-copy]").forEach((button) => {
    const list = document.getElementById(button.getAttribute("aria-controls"));
    // The clipboard needs a secure context (HTTPS or localhost) and the permission of the browser.
    if (!list || !window.isSecureContext || !navigator.clipboard || !navigator.clipboard.writeText) {
      return;
    }
    button.hidden = false;
    button.addEventListener("click", () => {
      const text = Array.from(list.querySelectorAll("li"))
        .map((item) => item.textContent.trim().toUpperCase())
        .join("\n");
      navigator.clipboard.writeText(text).then(
        () => say(button.dataset.done),
        () => say(button.dataset.failed),
      );
    });
  });
})();
