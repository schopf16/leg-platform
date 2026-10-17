/* Progressive enhancement of the settings page (D-084): a field is checked when it is left. The server answers with
   its own message for the field (``settings/check/``), so the text is the one that saving shows; the message
   stands under the field and goes when the field is changed. Without this script nothing is lost: saving checks
   everything. The answer is shown as text (textContent); nothing is built from markup; no dynamic code. */
(() => {
  "use strict";

  const form = document.querySelector("form[data-validate-url]");
  if (!form) {
    return;
  }
  const url = form.dataset.validateUrl;

  const holder = (input) => input.closest(".mgmt-field-unit") || input;

  const clear = (input) => {
    const old = document.getElementById(input.id + "_error");
    if (old) {
      old.remove();
    }
    input.removeAttribute("aria-invalid");
    if (input.dataset.describedby !== undefined) {
      if (input.dataset.describedby) {
        input.setAttribute("aria-describedby", input.dataset.describedby);
      } else {
        input.removeAttribute("aria-describedby");
      }
    }
  };

  const show = (input, message) => {
    clear(input);
    if (!message) {
      return;
    }
    const error = document.createElement("div");
    error.className = "mgmt-field-error";
    error.id = input.id + "_error";
    error.textContent = message;
    holder(input).insertAdjacentElement("afterend", error);
    input.setAttribute("aria-invalid", "true");
    const before = input.getAttribute("aria-describedby") || "";
    input.dataset.describedby = before;
    input.setAttribute("aria-describedby", (before + " " + error.id).trim());
  };

  form.querySelectorAll("input.form-control").forEach((input) => {
    if (!input.id || input.disabled) {
      return;
    }
    input.addEventListener("input", () => clear(input));
    input.addEventListener("blur", () => {
      const value = input.value;
      const params = new URLSearchParams({ field: input.name, value });
      fetch(url + "?" + params.toString(), { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => {
          if (data && input.value === value) {
            show(input, data.message);
          }
        })
        .catch(() => {});
    });
  });
})();
