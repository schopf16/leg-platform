/* The display around the creation of a passkey (D-106; owner specification, chapter 8). The browser ceremony itself is
 * passkeys.js and is not touched here: this file only (1) passes the chosen device to the button whose value passkeys.js
 * reads as the registration mode, (2) shows the screen "Confirm in the window" when the ceremony starts and (3) shows the
 * screen "Did not work" when passkeys.js reports a failure through its status line. No request, no storage, no logging.
 */
(() => {
  "use strict";
  const form = document.querySelector("form[data-passkey-register]");
  if (!form) return;
  const panels = {};
  form.querySelectorAll("[data-passkey-panel]").forEach(panel => {
    panels[panel.dataset.passkeyPanel] = panel;
  });
  const status = form.querySelector("[data-passkey-status]");
  if (!panels.device || !panels.waiting || !panels.failed || !status) return;

  function show(name) {
    Object.entries(panels).forEach(([key, panel]) => {
      panel.hidden = key !== name;
    });
    const heading = panels[name].querySelector("h2");
    if (heading) heading.focus();
  }

  // Runs on the form, before the listener of passkeys.js on the document reads the submitter.
  form.addEventListener("submit", event => {
    const choice = form.querySelector('input[name="device_choice"]:checked');
    if (event.submitter && choice) event.submitter.value = choice.value;
    show("waiting");
  });

  // passkeys.js marks a failure by role="alert" on its status line and writes the text there.
  new MutationObserver(() => {
    if (status.getAttribute("role") !== "alert") return;
    const detail = panels.failed.querySelector("[data-passkey-error-text]");
    const heading = panels.failed.querySelector("h2");
    const text = status.textContent.trim();
    if (detail) {
      detail.textContent = text === heading.textContent.trim() ? "" : text;
      detail.hidden = !detail.textContent;
    }
    show("failed");
  }).observe(status, {attributes: true, attributeFilter: ["role"]});
})();
