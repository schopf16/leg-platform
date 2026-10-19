/* Disables the submit button of a form as soon as it is submitted, so that a second click does not
   send the form twice (registration, details step, the customer's pages). The first submit is not stopped: the
   button is disabled in the next task, after the browser has collected the form. Without this script the
   server still lets only one submission count (it locks the account and the row). A page that the browser
   restores from its cache with the back button gets its buttons back. No request, no storage (CSP: script-src 'self'). */
(() => {
  "use strict";
  const forms = document.querySelectorAll("form[data-submit-once]");
  forms.forEach((form) => {
    form.addEventListener("submit", () => {
      window.setTimeout(() => {
        form.querySelectorAll("button[type=submit]").forEach((button) => {
          button.disabled = true;
        });
      }, 0);
    });
  });
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    forms.forEach((form) => {
      form.querySelectorAll("button[type=submit]").forEach((button) => {
        button.disabled = false;
      });
      // A script that gates a button on the form looks again.
      form.dispatchEvent(new Event("change", { bubbles: true }));
    });
  });
})();
