/* "Show password": switches the type of a password field between password and text.
   The button is rendered hidden by the server and shown here, so that without this script the
   field is a plain password field. No request, no storage, no eval (CSP: script-src 'self'). */
(() => {
  "use strict";
  const buttons = Array.from(document.querySelectorAll("[data-password-toggle]"));
  const fieldOf = (button) => document.getElementById(button.getAttribute("aria-controls"));
  const hide = (button) => {
    const field = fieldOf(button);
    if (!field) return;
    field.type = "password";
    button.textContent = button.dataset.showLabel;
  };
  buttons.forEach((button) => {
    const field = fieldOf(button);
    if (!field) return;
    button.hidden = false;
    button.addEventListener("click", () => {
      const reveal = field.type === "password";
      field.type = reveal ? "text" : "password";
      button.textContent = reveal ? button.dataset.hideLabel : button.dataset.showLabel;
    });
  });
  // A page that comes back from the browser's cache shows the passwords hidden again.
  window.addEventListener("pageshow", () => buttons.forEach(hide));
})();
