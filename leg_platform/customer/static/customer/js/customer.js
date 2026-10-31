/* The customer's pages (D-103): small helps only. Every page works without this script, the server checks everything again
  (progressive enhancement). It shows or hides a part of a form after a choice of radio buttons, updates the deregistration
  count, and groups an IBAN in fours when the field is left. It reads only the
   page itself: no request, no storage, no dynamic code, no HTML from strings (CSP: script-src 'self'). */
(() => {
  "use strict";

  // data-reveal-name / data-reveal-value: the part is shown while that radio button is chosen. Without radio buttons
  // (the answer is fixed by the server) the part stays as the server rendered it.
  document.querySelectorAll("[data-reveal-name]").forEach((part) => {
    const name = part.getAttribute("data-reveal-name");
    const value = part.getAttribute("data-reveal-value");
    const form = part.closest("form");
    if (!form) return;
    const radios = Array.from(form.querySelectorAll("input[type=radio]")).filter((radio) => radio.name === name);
    if (!radios.length) return;
    const update = () => {
      const chosen = radios.find((radio) => radio.checked);
      part.classList.toggle("is-open", Boolean(chosen && chosen.value === value));
    };
    radios.forEach((radio) => radio.addEventListener("change", update));
    update();
  });

  // The deregistration count follows the selected metering points.
  document.querySelectorAll("[data-dereg-summary]").forEach((button) => {
    const form = button.closest("form");
    if (!form) return;
    const summary = form.querySelector("[data-summary]");
    const update = () => {
      const count = form.querySelectorAll('input[name="metering_points"]:checked').length;
      if (summary) {
        const text = count === 0 ? summary.dataset.none : count === 1 ? summary.dataset.one : summary.dataset.many;
        summary.textContent = (text || "").replace("%(count)s", String(count));
      }
    };
    form.addEventListener("change", update);
    form.addEventListener("input", update);
    update();
  });

  // The IBAN in groups of four when the field is left; nothing is removed that is not white space or a hyphen.
  document.querySelectorAll(".iban-input").forEach((input) => {
    input.addEventListener("blur", () => {
      const compact = input.value.replace(/[\s-]+/g, "").toUpperCase();
      if (compact && /^[A-Z0-9]+$/.test(compact)) {
        input.value = compact.replace(/(.{4})/g, "$1 ").trim();
      }
    });
  });
})();
