/* Typing suggestions and notice of the address directory (D-054, D-065, D-066, D-111). Local-only: the requests go to
   the own origin, nothing is inserted as HTML, and only a deliberate choice changes the fields, with one exception:
   the town that follows from a complete postal code (or the postal code that follows from a complete town) goes into an
   empty field when the directory has exactly one answer, and the live region says so.
   One script for every form with an address (registration, profile, customer wizard, management dialogs and settings,
   see addresses/widget.html); it is loaded only where an address can be edited, never in a read-only view.

   Three comboboxes (ARIA 1.2: role combobox, aria-expanded, aria-controls, aria-activedescendant; the list is a
   listbox of options; a polite live region says how many there are):
   - street (at least three characters): street, house number, postal code and town of the matching addresses; the
     LEG's own area first (the server ranks it); choosing one fills all four fields;
   - postal code (four digits): the town or the towns of that postal code;
   - town (at least three characters): the postal code or the postal codes of that town.
   Keyboard: arrow down opens the list and moves down, arrow up moves up (and back to the text), Enter takes the active
   option (only then it does not send the form), Escape closes and keeps the text, Tab closes and moves on.
   The panels are in the flow of the page, directly under their input: a panel that closes while the person presses
   a button would shift the button. So nothing is closed by a mouse press elsewhere, and the form is never intercepted.

   Plus the notice, after the postal code and town (the "check" request): shown only when the directory is available
   and does not know the complete address. It never takes the focus and never blocks saving. Nothing else is said
   about the directory, except to a staff account (the server gives it the text ``data-directory-missing``): there
   are no suggestions because the directory has no addresses (D-111).
   The requests have their own stale-response protection; a 429 pauses them for the time the server names (M-4). The
   check never runs on change or blur: a notice that appears then shifts the Save button between mouse-down and
   mouse-up, and the click is lost. */
(() => {
  "use strict";

  const MIN_CHARS = 3; // street and town; the server asks for the same
  const POSTAL_CODE_LENGTH = 4;
  const MAX_OPTIONS = 5;
  const SUGGEST_DELAY = 200;
  const CHECK_DELAY = 1000;

  // One pass over the template: each "%(name)s" is replaced by its value, and a value is never
  // searched again, so a typed text that contains a placeholder cannot change the note. The
  // replacement is a function, so no pattern characters of a value are interpreted.
  const fill = (template, values) =>
    (template || "").replace(/%\((\w+)\)s/g, (token, name) =>
      Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : token
    );

  const make = (tag, className, content) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined) element.textContent = content;
    return element;
  };

  const same = (a, b) => a.trim().toLocaleLowerCase() === b.trim().toLocaleLowerCase();

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
      fields[name] = document.getElementById(widget.dataset[key] || "");
    });
    // A form without a country field (the registration, the management dialogs, where the country is Switzerland)
    // names the fixed country in data-country-value. Without a field and without that value the widget does
    // nothing; the server answers only for "CH" in any case.
    const fixedCountry = widget.dataset.countryValue || "";
    if (!fields.country && !fixedCountry) return;
    if (!form || !panel || !title || !note || !list || !more || !announce) return;
    if (Object.entries(fields).some(([name, field]) => !field && name !== "country")) return;
    const endpoint = new URL(widget.dataset.endpoint, location.href);
    if (endpoint.origin !== location.origin) return;
    const addressFields = [fields.street, fields.house_number, fields.postal_code, fields.locality];

    let checkTimer;
    let pausedUntil = 0; // set by a 429 answer: no request before this time (M-4)
    const timers = {};
    const channels = {
      suggest: {version: 0, controller: null},
      locality: {version: 0, controller: null},
      check: {version: 0, controller: null},
    };
    // What the script put into a field itself: typing in the field makes it the person's again.
    const autoFilled = {postal_code: false, locality: false};

    const country = () => (fields.country ? fields.country.value : fixedCountry);
    const isSwiss = () => country() === "CH";
    const street = () => fields.street.value.trim();
    const postalCode = () => fields.postal_code.value.trim();
    const locality = () => fields.locality.value.trim();
    const hasPostalCode = () => new RegExp(`^[0-9]{${POSTAL_CODE_LENGTH}}$`).test(postalCode());

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
    // The sentence about the directory (staff only) stays while the person types: only an answer of an available
    // directory, or ``force``, removes it.
    const hideNotice = (force) => {
      if (!notice) return;
      if (notice.dataset.kind === "directory" && !force) return;
      notice.replaceChildren();
      delete notice.dataset.kind;
      describe(false);
    };
    const showNotice = (level, message, kind) => {
      if (!notice || !message) return;
      const paragraph = document.createElement("p");
      paragraph.className = `alert alert-${level}`;
      paragraph.textContent = message;
      notice.replaceChildren(paragraph);
      if (kind) notice.dataset.kind = kind;
      else delete notice.dataset.kind;
      describe(true);
    };
    // The directory has no addresses (missing, empty or unreadable): said to a staff account only (D-111).
    const directoryProblem = (data) =>
      Boolean(text.directoryMissing) && Boolean(data) && (data.status === "missing" || data.status === "unavailable");
    const noteDirectory = (data) => {
      if (directoryProblem(data)) showNotice("secondary", text.directoryMissing, "directory");
      else if (data && data.status === "available" && notice && notice.dataset.kind === "directory") hideNotice(true);
    };

    // --- The comboboxes: street (the panel of the page), postal code and town (a panel under their input) ---
    const say = (message) => {
      announce.textContent = message;
    };

    const panelAfter = (input, name) => {
      const base = `${fields.street.id}-${name}`;
      const popup = make("div", "address-suggest");
      popup.hidden = true;
      const head = make("div", "address-suggest-head");
      const popupTitle = make("p", "address-suggest-title");
      popupTitle.id = `${base}-title`;
      const popupNote = make("p", "address-suggest-note");
      head.append(popupTitle, popupNote);
      const popupList = make("ul", "address-suggest-list");
      popupList.id = `${base}-list`;
      popupList.setAttribute("role", "listbox");
      popupList.setAttribute("aria-labelledby", popupTitle.id);
      popup.append(head, popupList);
      input.insertAdjacentElement("afterend", popup);
      return {popup, title: popupTitle, note: popupNote, list: popupList, more: null};
    };

    const combobox = (name, input, parts) => {
      input.setAttribute("role", "combobox");
      input.setAttribute("aria-autocomplete", "list");
      input.setAttribute("aria-haspopup", "listbox");
      input.setAttribute("aria-expanded", "false");
      input.setAttribute("aria-controls", parts.list.id);
      // A press on the panel must not move the focus out of the input.
      parts.popup.addEventListener("mousedown", (event) => event.preventDefault());
      return {name, input, ...parts, options: [], active: -1};
    };
    const combos = {
      street: combobox("street", fields.street, {popup: panel, title, note, list, more}),
    };
    combos.postal_code = combobox("postal_code", fields.postal_code, panelAfter(fields.postal_code, "postal-code"));
    combos.locality = combobox("locality", fields.locality, panelAfter(fields.locality, "town"));

    const hide = (combo) => {
      combo.popup.hidden = true;
      combo.list.replaceChildren();
      combo.options = [];
      combo.active = -1;
      combo.title.textContent = "";
      combo.note.textContent = "";
      if (combo.more) {
        combo.more.hidden = true;
        combo.more.textContent = "";
      }
      delete combo.popup.dataset.kind;
      combo.input.setAttribute("aria-expanded", "false");
      combo.input.removeAttribute("aria-activedescendant");
    };
    const hideAll = () => Object.values(combos).forEach(hide);
    const isOpen = (combo) => !combo.popup.hidden && combo.options.length > 0;

    const setActive = (combo, index) => {
      combo.options.forEach((option, number) => {
        const on = number === index;
        option.element.setAttribute("aria-selected", on ? "true" : "false");
        option.element.classList.toggle("is-active", on);
      });
      combo.active = index;
      if (index >= 0) {
        combo.input.setAttribute("aria-activedescendant", combo.options[index].element.id);
        combo.options[index].element.scrollIntoView({block: "nearest"});
      } else {
        combo.input.removeAttribute("aria-activedescendant");
      }
    };

    // Abort what is in flight and make its answer stale.
    const invalidate = () => {
      Object.values(channels).forEach((channel) => {
        channel.version += 1;
        if (channel.controller) channel.controller.abort();
      });
    };
    const cancelTimers = () => {
      clearTimeout(checkTimer);
      Object.keys(timers).forEach((name) => clearTimeout(timers[name]));
    };

    // The answer, or null if it is stale, was aborted or the server asked for a pause. ``given`` replaces what a field
    // holds in the request (the postal code that the script filled in itself is not a restriction of the town).
    const send = async (mode, given = {}) => {
      const channel = channels[mode];
      const version = ++channel.version;
      if (channel.controller) channel.controller.abort();
      if (Date.now() < pausedUntil) return null; // after a 429 no request until the pause has passed (M-4)
      channel.controller = new AbortController();
      const body = new URLSearchParams({mode});
      // The town and the postal code ask only for themselves.
      const names = mode === "locality" ? ["postal_code", "locality", "country"] : Object.keys(fields);
      names.forEach((name) => {
        if (Object.prototype.hasOwnProperty.call(given, name)) body.set(name, given[name]);
        else body.set(name, name === "country" ? country() : fields[name].value);
      });
      try {
        const token = form.querySelector("[name=csrfmiddlewaretoken]");
        if (!token) throw new Error("Missing CSRF token");
        const response = await fetch(endpoint.href, {
          method: "POST", credentials: "same-origin", mode: "same-origin",
          headers: {"X-CSRFToken": token.value, "Content-Type": "application/x-www-form-urlencoded"},
          body, signal: channel.controller.signal, cache: "no-store",
        });
        if (response.status === 429) {
          const seconds = Number(response.headers.get("Retry-After"));
          pausedUntil = Date.now() + (seconds > 0 && seconds <= 120 ? seconds : 60) * 1000;
          return null;
        }
        const data = await response.json();
        if (version !== channel.version) return null;
        return {ok: response.ok, data};
      } catch (error) {
        if (error.name === "AbortError" || version !== channel.version) return null;
        return {ok: false, data: null};
      }
    };

    // An option: two lines (the main text, the detail), read as "main, detail".
    const buildOption = (combo, number, main, detail, onChoose) => {
      const item = make("li", "address-option");
      item.id = `${combo.list.id}-${number}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", "false");
      item.append(make("span", "address-option-main", main));
      if (detail) {
        item.append(make("span", "visually-hidden", ", "), make("span", "address-option-sub", detail));
      }
      item.addEventListener("click", onChoose);
      return item;
    };

    const open = (combo, shown, announcement) => {
      combo.options = shown;
      combo.list.replaceChildren(...shown.map((option) => option.element));
      combo.active = -1;
      combo.input.removeAttribute("aria-activedescendant");
      combo.popup.hidden = false;
      combo.input.setAttribute("aria-expanded", "true");
      say(announcement);
    };

    // The address that a choice fills in, as the person reads it.
    const addressLabel = (address) =>
      `${address.street} ${address.house_number}`.trim() + `, ${address.postal_code} ${address.locality}`;

    const chooseAddress = (address) => {
      // A deliberate choice is the only operation that changes fields (the street suggestion).
      cancelTimers();
      invalidate();
      ["street", "house_number", "postal_code", "locality"].forEach((name) => {
        fields[name].value = address[name];
      });
      autoFilled.postal_code = false;
      autoFilled.locality = false;
      hideAll();
      hideNotice();
      say(fill(text.filled, {address: addressLabel(address)}));
      fields.street.focus();
      check(false);
    };

    const chooseLocality = (combo, row) => {
      cancelTimers();
      invalidate();
      fields.postal_code.value = row.postal_code;
      fields.locality.value = row.locality;
      autoFilled.postal_code = false;
      autoFilled.locality = false;
      hideAll();
      hideNotice();
      say(fill(text.filled, {address: `${row.postal_code} ${row.locality}`}));
      combo.input.focus();
      check(false);
    };

    const showAddresses = (data) => {
      const combo = combos.street;
      const approximate = data.kind === "approximate";
      const rows = data.results.slice(0, MAX_OPTIONS);
      const shown = rows.map((address, number) => ({
        element: buildOption(
          combo, number, `${address.street} ${address.house_number}`.trim(),
          `${address.postal_code} ${address.locality}`, () => chooseAddress(address)
        ),
        row: address,
      }));
      if (approximate) {
        combo.title.textContent = data.results.length > 1 ? text.titleApproximateMany : text.titleApproximateOne;
        combo.note.textContent = fill(text.noteApproximate, {street: street(), postal_code: postalCode()});
      } else {
        combo.title.textContent = text.titlePrefix;
        combo.note.textContent = text.notePrefix;
      }
      combo.more.textContent = text.more;
      combo.more.hidden = data.results.length <= MAX_OPTIONS;
      combo.popup.dataset.kind = approximate ? "approximate" : "prefix";
      const key = (approximate ? "announceApproximate" : "announcePrefix") + (shown.length === 1 ? "One" : "Many");
      open(combo, shown, fill(text[key], {count: shown.length}));
    };

    const showLocalities = (combo, rows, heading) => {
      const shown = rows.slice(0, MAX_OPTIONS).map((row, number) => ({
        element: buildOption(
          combo, number, `${row.postal_code} ${row.locality}`.trim(), "", () => chooseLocality(combo, row)
        ),
        row,
      }));
      combo.title.textContent = heading;
      combo.note.textContent = "";
      combo.popup.dataset.kind = "locality";
      open(combo, shown, fill(text[shown.length === 1 ? "announceLocalityOne" : "announceLocalityMany"], {count: shown.length}));
    };

    // --- The street suggestions ---
    const suggest = async () => {
      if (!isSwiss() || street().length < MIN_CHARS) {
        hide(combos.street);
        return;
      }
      const answer = await send("suggest");
      if (!answer) return;
      const data = answer.data;
      noteDirectory(data);
      if (answer.ok && data && Array.isArray(data.results) && data.results.length) showAddresses(data);
      else hide(combos.street);
    };

    // --- The town of a postal code and the postal code of a town ---
    const setAutomatically = (name, value, message) => {
      fields[name].value = value;
      autoFilled[name] = true;
      say(message);
    };

    const resolve = async (source) => {
      const combo = combos[source];
      if (!isSwiss()) {
        hide(combo);
        return;
      }
      // A postal code that the script filled in itself is no restriction of the town the person types now.
      const given = autoFilled.postal_code ? "" : postalCode();
      const answer = await send("locality", source === "postal_code" ? {locality: ""} : {postal_code: given});
      if (!answer) return;
      const data = answer.data;
      noteDirectory(data);
      if (!answer.ok || !data || !Array.isArray(data.results) || !data.results.length) {
        hide(combo);
        return;
      }
      const rows = data.results;
      if (source === "postal_code") {
        // Towns of the postal code: the one town goes into an empty field, several are offered as a choice.
        const known = rows.some((row) => same(row.locality, locality()));
        if (known) hide(combo);
        else if (rows.length === 1 && (locality() === "" || autoFilled.locality)) {
          hide(combo);
          setAutomatically("locality", rows[0].locality, fill(text.filledLocality, {town: rows[0].locality}));
        } else showLocalities(combo, rows, text.titleLocality);
        return;
      }
      // Postal codes of the town (or, with a complete postal code, the towns of it that start like the text).
      const complete = new RegExp(`^[0-9]{${POSTAL_CODE_LENGTH}}$`).test(given);
      const known = rows.some((row) => same(row.locality, locality()));
      if (complete && known) {
        hide(combo);
        return;
      }
      if (!complete && known && rows.length === 1) {
        hide(combo);
        if (given === "") {
          setAutomatically("postal_code", rows[0].postal_code, fill(text.filledPostalCode, {postal_code: rows[0].postal_code}));
        }
        return;
      }
      const onlyOneTown = rows.every((row) => same(row.locality, rows[0].locality));
      showLocalities(combo, rows, onlyOneTown ? text.titlePostalCode : text.titleLocality);
    };

    const complete = () => isSwiss() && street() !== "" && hasPostalCode() && locality() !== "";

    // ``atLoad``: the form was just opened (the person clicked "Edit"). Then an address that is not
    // found also fills the panel, so that the suggestions are ready next to the notice. Only the
    // not-found notice is shown to everybody; the state of the directory only to staff (D-066, D-111).
    const check = async (atLoad) => {
      if (!complete()) return;
      const answer = await send("check");
      if (!answer || !answer.ok) return;
      const data = answer.data;
      noteDirectory(data);
      if (data && data.status === "available" && data.match === false && data.warning) {
        showNotice("warning", data.warning);
        if (atLoad) suggest();
      }
    };

    // --- Events ---
    const onInput = (name) => () => {
      // Whatever was asked before describes the old values.
      invalidate();
      cancelTimers();
      hideNotice();
      if (name === "postal_code") autoFilled.postal_code = false;
      if (name === "locality") autoFilled.locality = false;
      if (!isSwiss()) hideAll();
      checkTimer = setTimeout(() => check(false), CHECK_DELAY);
      // The street asks for suggestions; the house number only narrows a list that is open. The postal code asks
      // when it is complete, the town from three characters. A panel stays until the answer arrives, so it does
      // not flicker.
      if (name === "street") {
        if (!isSwiss() || street().length < MIN_CHARS) hide(combos.street);
        else timers.street = setTimeout(suggest, SUGGEST_DELAY);
      } else if (name === "house_number") {
        if (isOpen(combos.street)) timers.street = setTimeout(suggest, SUGGEST_DELAY);
      } else if (name === "postal_code") {
        if (isSwiss() && hasPostalCode()) timers.postal_code = setTimeout(() => resolve("postal_code"), SUGGEST_DELAY);
        else hide(combos.postal_code);
      } else if (name === "locality") {
        if (isSwiss() && locality().length >= MIN_CHARS) timers.locality = setTimeout(() => resolve("locality"), SUGGEST_DELAY);
        else hide(combos.locality);
      }
    };
    Object.entries(fields).forEach(([name, field]) => {
      if (field) field.addEventListener("input", onInput(name));
    });

    // Keyboard: the focus stays in the input; the arrow keys move the active option (aria-activedescendant).
    const hasModifier = (event) => event.altKey || event.ctrlKey || event.metaKey || event.shiftKey;
    Object.values(combos).forEach((combo) => {
      combo.input.addEventListener("keydown", (event) => {
        if (hasModifier(event)) return;
        const opened = isOpen(combo);
        if (event.key === "ArrowDown") {
          if (opened) {
            event.preventDefault();
            setActive(combo, Math.min(combo.active + 1, combo.options.length - 1));
          } else if (combo.name === "street" && isSwiss() && street().length >= MIN_CHARS) {
            event.preventDefault();
            suggest();
          }
        } else if (event.key === "ArrowUp") {
          if (opened) {
            event.preventDefault();
            setActive(combo, combo.active - 1);
          }
        } else if (event.key === "Enter") {
          // Only an active option is taken; without one the form is sent as always.
          if (opened && combo.active >= 0) {
            event.preventDefault();
            combo.options[combo.active].element.click();
          }
        } else if (event.key === "Escape") {
          if (!combo.popup.hidden) {
            event.preventDefault();
            hide(combo);
          }
        } else if (event.key === "Tab") {
          hide(combo);
        }
      });
    });

    // Never intercept form submission: directory failure cannot block saving.
    check(true);
  });
})();
