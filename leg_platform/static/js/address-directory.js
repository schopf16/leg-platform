/* Suggestions and notice of the address directory (D-054, D-065). Local-only: the requests go to
   the own origin, nothing is inserted as HTML, and only a deliberate choice changes the fields.

   Two places, each where the person looks when the feedback appears:
   - the panel with suggestions, directly under the street input (the "suggest" request);
   - the notice, after the postal code and town (the "check" request): shown only when the address
     is complete and not found, or when the service says something. It never takes the focus and
     never blocks saving; the form is never intercepted.
   The suggest and the check requests have their own stale-response protection. The panel is not
   cleared while the person types: it is replaced or hidden when the answer arrives. The check never
   runs on change or blur: a notice that appears then shifts the Save button between mouse-down and
   mouse-up, and the click is lost. */
(() => {
  "use strict";

  const MAX_OPTIONS = 5;
  const SUGGEST_DELAY = 300;
  const CHECK_DELAY = 1000;

  // One pass over the template: each "%(name)s" is replaced by its value, and a value is never
  // searched again, so a typed text that contains a placeholder cannot change the note. The
  // replacement is a function, so no pattern characters of a value are interpreted.
  const fill = (template, values) =>
    (template || "").replace(/%\((\w+)\)s/g, (token, name) =>
      Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : token
    );

  document.querySelectorAll("[data-address-directory]").forEach((widget) => {
    const form = widget.closest("form");
    const panel = widget.querySelector("[data-address-suggest]");
    const title = widget.querySelector("[data-address-title]");
    const note = widget.querySelector("[data-address-note]");
    const list = widget.querySelector("[data-address-results]");
    const more = widget.querySelector("[data-address-more]");
    const announce = widget.querySelector("[data-address-announce]");
    const notice = document.getElementById(widget.dataset.noticeId || "");
    const text = widget.dataset;
    const fields = {};
    ["street", "house_number", "postal_code", "locality", "country"].forEach((name) => {
      const key = name.replace(/_([a-z])/g, (match, letter) => letter.toUpperCase()) + "Id";
      fields[name] = document.getElementById(widget.dataset[key]);
    });
    if (!form || !panel || !title || !note || !list || !more || !announce) return;
    if (Object.values(fields).some((field) => !field)) return;
    const endpoint = new URL(widget.dataset.endpoint, location.href);
    if (endpoint.origin !== location.origin) return;
    const addressFields = [fields.street, fields.house_number, fields.postal_code, fields.locality];

    let suggestTimer;
    let checkTimer;
    const channels = {
      suggest: {version: 0, controller: null},
      check: {version: 0, controller: null},
    };

    const isSwiss = () => fields.country.value === "CH";
    const street = () => fields.street.value.trim();
    const postalCode = () => fields.postal_code.value.trim();

    // --- The notice (after the postal code and town) ---
    // While it is visible its id is in aria-describedby of the four address fields; the tokens that
    // the fields had stay and are restored when it is hidden.
    const describe = (on) => {
      if (!notice) return;
      addressFields.forEach((field) => {
        const tokens = (field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
        const rest = tokens.filter((token) => token !== notice.id);
        const next = on ? [...rest, notice.id] : rest;
        if (next.length) field.setAttribute("aria-describedby", next.join(" "));
        else field.removeAttribute("aria-describedby");
      });
    };
    const hideNotice = () => {
      if (!notice) return;
      notice.replaceChildren();
      describe(false);
    };
    const showNotice = (level, message) => {
      if (!notice || !message) return;
      const paragraph = document.createElement("p");
      paragraph.className = `alert alert-${level}`;
      paragraph.textContent = message;
      notice.replaceChildren(paragraph);
      describe(true);
    };

    // --- The panel (under the street input) ---
    const hidePanel = () => {
      panel.hidden = true;
      list.replaceChildren();
      more.hidden = true;
      more.textContent = "";
      title.textContent = "";
      note.textContent = "";
      delete panel.dataset.kind;
    };
    const say = (message) => {
      announce.textContent = message;
    };
    const options = () => Array.from(list.querySelectorAll(".address-option"));

    // Abort what is in flight and make its answer stale.
    const invalidate = () => {
      Object.values(channels).forEach((channel) => {
        channel.version += 1;
        if (channel.controller) channel.controller.abort();
      });
    };

    // The answer, or null if it is stale or was aborted.
    const send = async (mode) => {
      const channel = channels[mode];
      const version = ++channel.version;
      if (channel.controller) channel.controller.abort();
      channel.controller = new AbortController();
      const body = new URLSearchParams({mode});
      Object.entries(fields).forEach(([name, field]) => body.set(name, field.value));
      try {
        const token = form.querySelector("[name=csrfmiddlewaretoken]");
        if (!token) throw new Error("Missing CSRF token");
        const response = await fetch(endpoint.href, {
          method: "POST", credentials: "same-origin", mode: "same-origin",
          headers: {"X-CSRFToken": token.value, "Content-Type": "application/x-www-form-urlencoded"},
          body, signal: channel.controller.signal, cache: "no-store",
        });
        const data = await response.json();
        if (version !== channel.version) return null;
        return {ok: response.ok, data};
      } catch (error) {
        if (error.name === "AbortError" || version !== channel.version) return null;
        return {ok: false, data: null};
      }
    };

    const showPanel = (data) => {
      const approximate = data.kind === "approximate";
      const shown = data.results.slice(0, MAX_OPTIONS);
      const several = data.results.length > 1;
      list.replaceChildren(...shown.map(buildOption));
      if (approximate) {
        title.textContent = several ? text.titleApproximateMany : text.titleApproximateOne;
        note.textContent = fill(text.noteApproximate, {street: street(), postal_code: postalCode()});
      } else {
        title.textContent = text.titlePrefix;
        note.textContent = text.notePrefix;
      }
      more.textContent = text.more;
      more.hidden = data.results.length <= MAX_OPTIONS;
      panel.dataset.kind = approximate ? "approximate" : "prefix";
      panel.hidden = false;
      const key = (approximate ? "announceApproximate" : "announcePrefix") +
        (shown.length === 1 ? "One" : "Many");
      say(fill(text[key], {count: shown.length}));
    };

    const suggest = async () => {
      if (!isSwiss() || street().length < 2) {
        hidePanel();
        return;
      }
      const answer = await send("suggest");
      if (!answer) return;
      const data = answer.data;
      if (answer.ok && data && Array.isArray(data.results) && data.results.length) showPanel(data);
      else hidePanel();
    };

    const complete = () => isSwiss() && street() !== "" && /^[0-9]{4}$/.test(postalCode()) &&
      fields.locality.value.trim() !== "";

    // ``atLoad``: the form was just opened. Then an address that is not found also fills the panel,
    // so that the "Check address" link after saving lands on a ready list.
    const check = async (atLoad) => {
      if (!complete()) return;
      const answer = await send("check");
      if (!answer) return;
      const data = answer.data;
      if (!answer.ok) {
        showNotice("info", (data && data.warning) || text.unavailable);
        return;
      }
      if (data.status === "available" && data.match === false) {
        showNotice("warning", data.warning);
        if (atLoad) suggest();
      } else if (data.warning) {
        showNotice("info", data.warning);
      }
    };

    const choose = (address) => {
      // A deliberate choice is the only operation that changes fields.
      clearTimeout(suggestTimer);
      clearTimeout(checkTimer);
      invalidate();
      ["street", "house_number", "postal_code", "locality"].forEach((name) => {
        fields[name].value = address[name];
      });
      hidePanel();
      hideNotice();
      const label = `${address.street} ${address.house_number}`.trim() +
        `, ${address.postal_code} ${address.locality}`;
      say(fill(text.filled, {address: label}));
      fields.street.focus();
      check(false);
    };

    function buildOption(address) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "address-option";
      button.tabIndex = -1;
      const main = document.createElement("span");
      main.className = "address-option-main";
      main.textContent = `${address.street} ${address.house_number}`.trim();
      const separator = document.createElement("span");
      separator.className = "visually-hidden";
      separator.textContent = ", ";
      const sub = document.createElement("span");
      sub.className = "address-option-sub";
      sub.textContent = `${address.postal_code} ${address.locality}`;
      button.append(main, separator, sub);
      button.addEventListener("click", () => choose(address));
      item.append(button);
      return item;
    }

    // --- Events ---
    const onInput = (name) => () => {
      // Whatever was asked before describes the old values.
      invalidate();
      hideNotice();
      clearTimeout(suggestTimer);
      clearTimeout(checkTimer);
      if (!isSwiss()) hidePanel();
      checkTimer = setTimeout(() => check(false), CHECK_DELAY);
      // Only the street and the house number ask for suggestions: a list that appears while the
      // person types in a field below it would shift that field. The panel itself stays until the
      // answer arrives, so it does not flicker.
      if (name === "street" || name === "house_number") {
        if (!isSwiss() || street().length < 2) hidePanel();
        else suggestTimer = setTimeout(suggest, SUGGEST_DELAY);
      }
    };
    Object.entries(fields).forEach(([name, field]) => field.addEventListener("input", onInput(name)));

    // A press on the list must not move the focus out of the street field.
    panel.addEventListener("mousedown", (event) => event.preventDefault());

    // Keyboard: Tab goes past the options (tabindex -1); the arrow keys move between them.
    const hasModifier = (event) => event.altKey || event.ctrlKey || event.metaKey || event.shiftKey;
    [fields.street, fields.house_number].forEach((field) => {
      field.addEventListener("keydown", (event) => {
        if (panel.hidden || hasModifier(event)) return;
        if (event.key === "ArrowDown") {
          const first = options()[0];
          if (first) {
            event.preventDefault();
            first.focus();
          }
        } else if (event.key === "Escape") {
          hidePanel();
        }
      });
    });
    panel.addEventListener("keydown", (event) => {
      if (hasModifier(event)) return;
      const all = options();
      const index = all.indexOf(document.activeElement);
      if (index < 0) return;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        all[Math.min(index + 1, all.length - 1)].focus();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        if (index === 0) fields.street.focus();
        else all[index - 1].focus();
      } else if (event.key === "Escape") {
        event.preventDefault();
        hidePanel();
        fields.street.focus();
      }
    });

    // Never intercept form submission: directory failure cannot block saving.
    check(true);
  });
})();
