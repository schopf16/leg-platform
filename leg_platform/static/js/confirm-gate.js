/* Keeps a button inactive until its box is ticked (profile: "Fertig" after the recovery codes and
   "Konto endgültig löschen"). Without this script the button is active and the server checks the
   box, so nothing depends on it (D-059). No request, no storage (CSP: script-src 'self'). */
(() => {
  "use strict";
  document.querySelectorAll("[data-gate]").forEach((button) => {
    const box = document.getElementById(button.getAttribute("data-gate"));
    if (!box) return;
    const update = () => {
      button.disabled = !box.checked;
    };
    box.addEventListener("change", update);
    update();
  });
})();
