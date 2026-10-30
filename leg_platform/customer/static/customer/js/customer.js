/* The customer's pages (D-103): small helps only. Every page works without this script, the server checks everything again
   (progressive enhancement). It keeps a button inactive until its boxes are ticked or its date is chosen, shows or hides a
   part of a form after a choice of radio buttons, and groups an IBAN in fours when the field is left. It reads only the
   page itself: no request, no storage, no dynamic code, no HTML from strings (CSP: script-src 'self'). */
(() => {
  "use strict";

  // A button with data-gate-all="id id" stays inactive until every named box is ticked (contract: two ticks).
  document.querySelectorAll("[data-gate-all]").forEach((button) => {
    const boxes = (button.getAttribute("data-gate-all") || "")
      .split(/\s+/)
      .map((id) => document.getElementById(id))
      .filter(Boolean);
    if (!boxes.length) return;
    const update = () => {
      button.disabled = !boxes.every((box) => box.checked);
    };
    boxes.forEach((box) => box.addEventListener("change", update));
    update();
  });

  // data-gate-date="id": inactive while "Desired date" is chosen and the date field is empty (start date).
  document.querySelectorAll("[data-gate-date]").forEach((button) => {
    const form = button.closest("form");
    const field = document.getElementById(button.getAttribute("data-gate-date") || "");
    if (!form || !field) return;
    const update = () => {
      const chosen = Array.from(form.querySelectorAll("input[type=radio]")).find(
        (radio) => radio.name === "start" && radio.checked
      );
      button.disabled = Boolean(chosen && chosen.value === "date" && !field.value);
    };
    form.addEventListener("change", update);
    field.addEventListener("input", update);
    update();
  });

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

  // The deregistration: the button stays inactive until a metering point is chosen, the proof is typed (an input with
  // data-proof) and, when a desired date is chosen, the date is filled in; the count in the summary follows the boxes.
  document.querySelectorAll("[data-gate-dereg]").forEach((button) => {
    const form = button.closest("form");
    if (!form) return;
    const summary = form.querySelector("[data-summary]");
    const update = () => {
      const count = form.querySelectorAll('input[name="metering_points"]:checked').length;
      const withdrawn = form.querySelectorAll('input[name="withdraw_applications"]:checked').length;
      const proof = form.querySelector("[data-proof]");
      const termination = Array.from(form.querySelectorAll('input[name="termination"]')).find((radio) => radio.checked);
      const date = form.querySelector('input[name="desired_date"]');
      const dateMissing = Boolean(count > 0 && termination && termination.value === "date" && date && !date.value);
      button.disabled = (count === 0 && withdrawn === 0) || !proof || proof.value.trim() === "" || dateMissing;
      // The hint only while the proof is the missing piece (nothing to say when no proof is needed).
      const hint = form.querySelector("[data-proof-hint]");
      if (hint) hint.hidden = !(button.disabled && proof && proof.value.trim() === "");
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
