/* Keeps a button inactive until its box is ticked (profile: "Fertig" after the recovery codes and
   "Konto endgültig löschen"). Without this script the button is active and the server checks the
   box, so nothing depends on it (D-059). No request, no storage (CSP: script-src 'self'). */
(() => {
  "use strict";
  document.querySelectorAll("[data-gate]").forEach((button) => {
    const box = document.getElementById(button.getAttribute("data-gate"));
    if (!box) return;
    // The optional hint under the box (the deletion page) is shown only while the box is empty.
    const hint = document.getElementById("confirm-gate-hint");
    const update = () => {
      button.disabled = !box.checked;
      if (hint) hint.hidden = box.checked;
    };
    box.addEventListener("change", update);
    update();
  });
})();
