/* Disables the submit button of a form as soon as it is submitted, so that a second click does not
   send the form twice (registration, details step). The first submit is not stopped: the button is
   disabled in the next task, after the browser has collected the form. Without this script the
   server still lets only one submission create the account (it locks the account and the link).
   No request, no storage (CSP: script-src 'self'). */
(() => {
  "use strict";
  document.querySelectorAll("form[data-submit-once]").forEach((form) => {
    form.addEventListener("submit", () => {
      window.setTimeout(() => {
        form.querySelectorAll("button[type=submit]").forEach((button) => {
          button.disabled = true;
        });
      }, 0);
    });
  });
})();
