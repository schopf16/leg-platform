/* Checks while typing, for the fields of the dialogs that carry data-check (D-094). The server checks everything
   again and has the last word; this script only says it sooner, with the same messages (they come from the
   server in data attributes, in the language of the page).

   - data-check="digits": the typed last digits of a metering point number. Only the digits 0-9, at most
     data-limit of them. On leaving the field the digits are filled up with zeros on the left (23 becomes 0023,
     twenty digits in all), the complete number is shown under the field and the number is looked up
     (data-check-url): a message, or the notice "this number exists already" with the link to it. A text that
     starts with the fixed prefix (data-prefix) loses the prefix.
   - data-check="decimal": digits and one decimal point or comma, at most data-places decimals, at most data-max.
     On leaving the field a comma becomes a point.

   The message stands in the slot "<field id>_error" under the field, the field gets aria-invalid and a 2 px red
   border (CSS); the screen reader is told through one polite live region, a moment after the last key, never on
   every key. Nothing the person typed is deleted or changed except the two changes on leaving the field. Answers
   from the server are shown as text (textContent); nothing is built from markup; no dynamic code. */
(() => {
  "use strict";

  const inputs = document.querySelectorAll("input[data-check]");
  if (!inputs.length) {
    return;
  }

  let announcer = null;
  let announceTimer = 0;
  const announce = (text) => {
    if (!announcer) {
      announcer = document.createElement("div");
      announcer.className = "visually-hidden";
      announcer.setAttribute("role", "status");
      announcer.setAttribute("aria-live", "polite");
      announcer.setAttribute("aria-atomic", "true");
      document.body.appendChild(announcer);
    }
    window.clearTimeout(announceTimer);
    announceTimer = window.setTimeout(() => {
      announcer.textContent = text;
    }, 400);
  };

  const text = (tag, content, className) => {
    const element = document.createElement(tag);
    element.textContent = content;
    if (className) {
      element.className = className;
    }
    return element;
  };

  const forms = new Map(); // form -> set of inputs that block saving
  const blocking = (input, blocked) => {
    const form = input.form;
    if (!form) {
      return;
    }
    const set = forms.get(form) || new Set();
    forms.set(form, set);
    if (blocked) {
      set.add(input);
    } else {
      set.delete(input);
    }
    const submit = form.querySelector('button[type="submit"]');
    if (submit) {
      submit.disabled = set.size > 0;
    }
  };

  const slotOf = (input) => {
    let slot = document.getElementById(input.id + "_error");
    if (!slot) {
      slot = text("div", "", "mgmt-field-error");
      slot.id = input.id + "_error";
      const anchor = input.closest(".mgmt-number-field, .mgmt-input-unit") || input;
      anchor.after(slot);
    }
    return slot;
  };

  const show = (input, message) => {
    const slot = slotOf(input);
    slot.textContent = message;
    slot.hidden = !message;
    if (message) {
      input.setAttribute("aria-invalid", "true");
      announce(message);
    } else {
      input.removeAttribute("aria-invalid");
    }
    blocking(input, Boolean(message));
  };

  // ---- Decimal numbers ----
  const decimalProblem = (input) => {
    const value = input.value.trim();
    if (value === "") {
      return "";
    }
    if (!/^[0-9]*[.,]?[0-9]*$/.test(value) || !/[0-9]/.test(value)) {
      return input.dataset.messageInvalid;
    }
    const places = Number(input.dataset.places);
    const fraction = value.replace(",", ".").split(".")[1] || "";
    if (fraction.length > places) {
      return input.dataset.messagePlaces;
    }
    if (Number(value.replace(",", ".")) > Number(input.dataset.max)) {
      return input.dataset.messageRange;
    }
    return "";
  };

  // ---- The typed digits of a metering point number ----
  const digitsProblem = (input, leaving) => {
    const value = input.value.trim();
    if (value === "") {
      return leaving && input.required ? input.dataset.messageEmpty : "";
    }
    if (!/^[0-9]+$/.test(value)) {
      return input.dataset.messageDigits;
    }
    const limit = Number(input.dataset.limit);
    if (value.length > limit) {
      return input.dataset.messageMany.replace("%(count)s", String(value.length));
    }
    return "";
  };

  const sequences = new Map();
  const lookup = (input, digits) => {
    const url = input.dataset.checkUrl;
    const feedback = input.closest(".mgmt-dialog-field").querySelector("[data-number-feedback]");
    if (!url || !feedback) {
      return;
    }
    const previous = sequences.get(input);
    if (previous) {
      previous.abort();
    }
    const controller = new AbortController();
    sequences.set(input, controller);
    fetch(url + "?" + new URLSearchParams({ digits }).toString(), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        if (!data || input.value.trim() !== digits) {
          return;
        }
        feedback.replaceChildren();
        if (data.message) {
          show(input, data.message);
        } else if (data.duplicate) {
          const found = data.duplicate;
          const box = text("div", "", "mgmt-banner mgmt-duplicate");
          box.setAttribute("role", "status");
          const line = document.createElement("p");
          line.appendChild(text("b", data.title));
          const parts = [found.number, found.direction, found.location, found.leg, found.status].filter(Boolean);
          line.appendChild(document.createTextNode(" " + parts.join(", ") + "."));
          box.appendChild(line);
          if (typeof found.url === "string" && found.url.startsWith("/") && !found.url.startsWith("//")) {
            const linkLine = document.createElement("p");
            const link = text("a", data.link);
            link.href = found.url;
            linkLine.appendChild(link);
            box.appendChild(linkLine);
          }
          feedback.appendChild(box);
          blocking(input, true);
        }
      })
      .catch(() => {});
  };

  const completeNumber = (input, padded) => {
    const line = input.closest(".mgmt-dialog-field").querySelector("[data-number-complete]");
    if (!line) {
      return;
    }
    if (padded) {
      line.textContent = (input.dataset.completeLabel || "") + " " + (input.dataset.prefix || "") + padded;
      line.hidden = false;
    } else {
      line.hidden = true;
    }
  };

  const clearFeedback = (input) => {
    const field = input.closest(".mgmt-dialog-field");
    const feedback = field && field.querySelector("[data-number-feedback]");
    if (feedback) {
      feedback.replaceChildren();
    }
  };

  const padDigits = (input) => {
    const value = input.value.trim();
    if (/^[0-9]+$/.test(value) && value.length <= Number(input.dataset.limit)) {
      input.value = value.padStart(Number(input.dataset.limit), "0");
    }
  };

  inputs.forEach((input) => {
    const kind = input.dataset.check;
    if (kind === "decimal") {
      input.addEventListener("input", () => show(input, decimalProblem(input)));
      input.addEventListener("blur", () => {
        if (!decimalProblem(input)) {
          input.value = input.value.trim().replace(",", ".");
        }
      });
    } else if (kind === "digits") {
      input.addEventListener("input", () => {
        const prefix = (input.dataset.prefix || "").toUpperCase();
        if (prefix && input.value.toUpperCase().startsWith(prefix)) {
          input.value = input.value.slice(prefix.length);
        }
        clearFeedback(input);
        completeNumber(input, "");
        show(input, digitsProblem(input, false));
      });
      input.addEventListener("blur", () => {
        const problem = digitsProblem(input, true);
        show(input, problem);
        if (problem || input.value.trim() === "") {
          return;
        }
        padDigits(input);
        const digits = input.value.trim();
        completeNumber(input, digits);
        lookup(input, digits);
      });
      if (input.form) {
        // Enter in another field: the server gets the filled-up digits.
        input.form.addEventListener("submit", () => {
          if (!digitsProblem(input, false)) {
            padDigits(input);
          }
        });
      }
    }
  });
})();
