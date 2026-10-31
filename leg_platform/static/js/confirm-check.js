/* Checks the confirmation box before the form is sent, so that nothing typed is lost (a password is never shown
   again after a round trip; the recovery codes are shown only once, D-059). The button stays active. The error stands
   under the box, the focus goes to it. The server checks the box again. No request, no storage (CSP: script-src 'self'). */
(() => {
  "use strict";
  document.querySelectorAll("[data-confirm-check]").forEach((button) => {
    const form = button.form;
    const box = document.getElementById(button.getAttribute("data-confirm-check"));
    if (!form || !box) return;
    // The portal shows the error, not the bubble of the browser.
    form.noValidate = true;
    const errorId = `${box.id}_error`;
    const label = box.closest("label");
    let created = null;

    const clear = () => {
      if (created) created.remove();
      created = null;
      box.removeAttribute("aria-invalid");
      const rest = (box.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== errorId);
      if (rest.length) box.setAttribute("aria-describedby", rest.join(" "));
      else box.removeAttribute("aria-describedby");
    };

    form.addEventListener("submit", (event) => {
      if (box.checked) return;
      event.preventDefault();
      if (!created && !document.getElementById(errorId) && label) {
        created = document.createElement("div");
        created.className = "invalid-feedback d-block";
        created.id = errorId;
        created.textContent = box.getAttribute("data-error") || "";
        label.insertAdjacentElement("afterend", created);
      }
      box.setAttribute("aria-invalid", "true");
      const ids = (box.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      if (!ids.includes(errorId)) box.setAttribute("aria-describedby", [...ids, errorId].join(" "));
      box.focus();
    });
    box.addEventListener("change", () => {
      if (box.checked) clear();
    });
  });
})();
