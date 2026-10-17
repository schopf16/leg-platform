/* Progressive enhancement of the dialogs of the metering points (D-079). Everything works without this
   script (the server checks the number and finds the location or the person when saving); the script only
   makes it faster:

   - a field in an element with data-suggest-url asks that address for suggestions while typing (at least
     two characters, a short moment after the last key) and fills the list of the field with them. The server
     answers with at most ten texts;
   - the number field in an element with data-number-check-url is checked on leaving the field: the message
     and the notice "this number exists already" (with the link to the existing metering point) appear under
     the field, and saving is blocked while one of them is shown.

   The answers are shown as text (textContent); nothing is built from markup; no dynamic code. */
(() => {
  "use strict";

  const ask = (url, params) =>
    fetch(url + "?" + new URLSearchParams(params).toString(), {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }).then((response) => (response.ok ? response.json() : null));

  // ---- Suggestions ----
  document.querySelectorAll("[data-suggest-url]").forEach((holder) => {
    const input = holder.querySelector("input");
    const list = holder.querySelector("datalist");
    if (!input || !list) {
      return;
    }
    let timer = 0;
    let last = "";
    input.addEventListener("input", () => {
      window.clearTimeout(timer);
      const text = input.value.trim();
      if (text.length < 2 || text === last) {
        return;
      }
      timer = window.setTimeout(() => {
        last = text;
        ask(holder.dataset.suggestUrl, { q: text })
          .then((data) => {
            list.replaceChildren();
            ((data && data.results) || []).forEach((value) => {
              const option = document.createElement("option");
              option.value = value;
              list.appendChild(option);
            });
          })
          .catch(() => {});
      }, 250);
    });
  });

  // ---- The number ----
  const holder = document.querySelector("[data-number-check-url]");
  const feedback = document.querySelector("[data-number-feedback]");
  if (!holder || !feedback) {
    return;
  }
  const input = holder.querySelector("input");
  const form = input && input.form;
  const submit = form && form.querySelector('button[type="submit"]');

  const clear = () => {
    feedback.replaceChildren();
    if (submit) {
      submit.disabled = false;
    }
  };
  const block = () => {
    if (submit) {
      submit.disabled = true;
    }
  };
  const text = (tag, content, className) => {
    const element = document.createElement(tag);
    element.textContent = content;
    if (className) {
      element.className = className;
    }
    return element;
  };

  input.addEventListener("input", clear);
  input.addEventListener("blur", () => {
    const digits = input.value.trim();
    if (!digits) {
      return;
    }
    ask(holder.dataset.numberCheckUrl, { digits })
      .then((data) => {
        if (!data || input.value.trim() !== digits) {
          return;
        }
        feedback.replaceChildren();
        if (data.message) {
          feedback.appendChild(text("div", data.message, "mgmt-field-error"));
          block();
        } else if (data.duplicate) {
          const found = data.duplicate;
          const box = text("div", "", "mgmt-banner mgmt-duplicate");
          box.setAttribute("role", "status");
          const line = document.createElement("p");
          line.appendChild(text("b", data.title));
          const parts = [found.number, found.direction, found.location, found.leg, found.status].filter(Boolean);
          line.appendChild(document.createTextNode(" " + parts.join(", ") + "."));
          const linkLine = document.createElement("p");
          box.appendChild(line);
          if (typeof found.url === "string" && found.url.startsWith("/") && !found.url.startsWith("//")) {
            const link = text("a", data.link);
            link.href = found.url;
            linkLine.appendChild(link);
            box.appendChild(linkLine);
          }
          feedback.appendChild(box);
          block();
        }
      })
      .catch(() => {});
  });
})();
