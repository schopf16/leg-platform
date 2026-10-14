/* "Show password": switches the type of a password field between password and text.
   The button is an eye icon inside the field. It is rendered hidden by the server and shown here,
   so that without this script the field is a plain password field. No request, no storage, no
   eval (CSP: script-src 'self'). */
(() => {
  "use strict";
  const buttons = Array.from(document.querySelectorAll("[data-password-toggle]"));
  const fieldOf = (button) => document.getElementById(button.getAttribute("aria-controls"));
  const show = (button, visible) => {
    const field = fieldOf(button);
    if (!field) return;
    field.type = visible ? "text" : "password";
    button.classList.toggle("is-visible", visible);
    button.setAttribute("aria-pressed", String(visible));
    button.setAttribute("aria-label", visible ? button.dataset.hideLabel : button.dataset.showLabel);
  };
  buttons.forEach((button) => {
    if (!fieldOf(button)) return;
    button.hidden = false;
    button.addEventListener("click", () => show(button, fieldOf(button).type === "password"));
  });
  // The language of the page changed: the labels follow (login-texts.js).
  document.addEventListener("leg:language", (event) => {
    buttons.forEach((button) => {
      button.dataset.showLabel = event.detail.showPassword;
      button.dataset.hideLabel = event.detail.hidePassword;
      show(button, button.classList.contains("is-visible"));
    });
  });
  // A page that comes back from the browser's cache shows the passwords hidden again.
  window.addEventListener("pageshow", () => buttons.forEach((button) => show(button, false)));
})();
