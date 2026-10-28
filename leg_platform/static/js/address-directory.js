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
   The panels are overlays (static/css/address-suggest.css): out of the flow, under their input, over the whole row of the
   address (the row gets the class ``address-host``). Opening or closing one moves nothing on the page. A list closes when
   the focus leaves its field (the street list also stays in the house number) and on a press outside it (the card: by Escape, and by a press outside only while it floats as an overlay); the form is
   never intercepted. A list shows at most MAX_OPTIONS rows, never a
   scrollbar; "More matches: keep typing." is shown only when the server has more matches than rows are shown.
   A street field that ends in a house number is understood by the server (the house number field stays empty).

   Plus the notice, after the postal code and town (the "check" request): shown only when the directory is available
   and does not know the complete address. It never takes the focus and never blocks saving. Nothing else is said
   about the directory, except to a staff account (the server gives it the text ``data-directory-missing``): there
   are no suggestions because the directory has no addresses (D-111).
   When the street and house number are in the directory but under another postal code, the notice is the card "Did you
   mean this address?" instead (the server's ``proposals``): a click on its option takes over the address, the link keeps
   the input and the card does not return for these exact values; nothing changes without a click.
   The requests have their own stale-response protection; a 429 pauses them for the time the server names (M-4). The
   check never runs on change or blur: a notice that appears then shifts the Save button between mouse-down and
   mouse-up, and the click is lost. */
(() => {
  "use strict";

  const MIN_CHARS = 3; // street and town; the server asks for the same
  const POSTAL_CODE_LENGTH = 4;
  const MAX_OPTIONS = 7;
  const SUGGEST_DELAY = 200;
  const CHECK_DELAY = 1000;
  const RETRY_DELAY = 800;

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

  // The row of the address: the overlays are as wide as it is (a form without such a row: the field's own box).
  // The sentence above the options for each reason (data-proposal-note-*).
  const NOTES = {
    postal_code: "proposalNotePostalCode", house_number: "proposalNoteHouseNumber", town: "proposalNoteTown",
    street: "proposalNoteStreet",
  };
  // The key of the sentence above the options: a typing mistake in the street names the postal code, else the town.
  const noteKey = (reason, hasPostalCode) =>
    reason === "street" && !hasPostalCode ? "proposalNoteStreetTown" : NOTES[reason] || "proposalNotePostalCode";
  const ROW = ".address-row, .field-pair, .mgmt-dialog-row, .form-row";

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
    // A locked address (the profile of a confirmed building, a fixed row of a dialog) has no suggestions.
    if (fields.street.disabled) return;
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
    // The address field the person used last: after a click on the card the focus goes back there (the card disappears).
    let lastField = null;
    // The inputs for which the person said "No, my address is correct" (the card does not return for them).
    const dismissed = new Set();

    const country = () => (fields.country ? fields.country.value : fixedCountry);
    const isSwiss = () => country() === "CH";
    const street = () => fields.street.value.trim();
    const postalCode = () => fields.postal_code.value.trim();
    const locality = () => fields.locality.value.trim();
    const hasPostalCode = () => new RegExp(`^[0-9]{${POSTAL_CODE_LENGTH}}$`).test(postalCode());
    // The four inputs as one text (case and spaces at the ends do not count).
    const houseNumber = () => fields.house_number.value.trim();
    const signature = () =>
      [street(), houseNumber(), postalCode(), locality()]
        .map((part) => part.toLocaleLowerCase()).join("\n");

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
      const popupMore = make("p", "address-more");
      popupMore.hidden = true;
      popup.append(head, popupList, popupMore);
      // In a wrapper without height, like the street's: the overlay then starts under the input (the static position of
      // an absolutely positioned child of a grid is the top of the grid, not the place after the input).
      const wrapper = make("div", "address-assist");
      wrapper.append(popup);
      input.insertAdjacentElement("afterend", wrapper);
      return {popup, title: popupTitle, note: popupNote, list: popupList, more: popupMore};
    };

    const combobox = (name, input, parts) => {
      input.setAttribute("role", "combobox");
      input.setAttribute("aria-autocomplete", "list");
      input.setAttribute("aria-haspopup", "listbox");
      input.setAttribute("aria-expanded", "false");
      input.setAttribute("aria-controls", parts.list.id);
      // A press on the panel must not move the focus out of the input.
      parts.popup.addEventListener("mousedown", (event) => event.preventDefault());
      // The overlay is placed against the row of the address.
      (input.closest(ROW) || input.parentElement).classList.add("address-host");
      return {name, input, ...parts, options: [], active: -1};
    };
    const combos = {
      street: combobox("street", fields.street, {popup: panel, title, note, list, more}),
    };
    combos.postal_code = combobox("postal_code", fields.postal_code, panelAfter(fields.postal_code, "postal-code"));
    combos.locality = combobox("locality", fields.locality, panelAfter(fields.locality, "town"));

    const hide = (combo) => {
      // A list that closes takes its announcement with it (the live region would keep saying "7 suggestions").
      if (!combo.popup.hidden) announce.textContent = "";
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

    // An option is one line: the main text (bold) and the detail (muted, with its comma), read as "main, detail".
    const buildOption = (combo, number, main, detail, onChoose) => {
      const item = make("li", "address-option");
      item.id = `${combo.list.id}-${number}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", "false");
      item.append(make("span", "address-option-main", main));
      if (detail) item.append(make("span", "address-option-sub", `, ${detail}`));
      item.addEventListener("click", onChoose);
      return item;
    };

    // A list or card that opens below the visible part of the page is scrolled into view once (a scroll is no layout shift).
    const reveal = (element) => {
      if (!element) return;
      const box = element.getBoundingClientRect();
      if (box.bottom > window.innerHeight || box.top < 0) element.scrollIntoView({block: "nearest"});
    };

    const open = (combo, shown, announcement) => {
      combo.options = shown;
      combo.list.replaceChildren(...shown.map((option) => option.element));
      combo.active = -1;
      combo.input.removeAttribute("aria-activedescendant");
      combo.popup.hidden = false;
      combo.input.setAttribute("aria-expanded", "true");
      say(announcement);
      reveal(combo.popup);
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
      combo.title.textContent = approximate ? text.titleApproximate : text.titlePrefix;
      combo.note.textContent = text.notePrefix;
      // "More matches" only when the directory has more than the rows shown (the server reads one row beyond its limit).
      const hasMore = data.more === true || data.results.length > MAX_OPTIONS;
      combo.more.textContent = text.more;
      combo.more.hidden = !hasMore;
      combo.popup.dataset.kind = approximate ? "approximate" : "prefix";
      const key = (approximate ? "announceApproximate" : "announcePrefix") + (shown.length === 1 ? "One" : "Many");
      const announcement = fill(text[key], {count: shown.length});
      open(combo, shown, hasMore ? `${announcement} ${text.more}` : announcement);
    };

    const showLocalities = (combo, rows, heading, more = false) => {
      const shown = rows.slice(0, MAX_OPTIONS).map((row, number) => ({
        element: buildOption(
          combo, number, `${row.postal_code} ${row.locality}`.trim(), "", () => chooseLocality(combo, row)
        ),
        row,
      }));
      combo.title.textContent = heading;
      combo.note.textContent = "";
      combo.popup.dataset.kind = "locality";
      const hasMore = more === true || rows.length > MAX_OPTIONS;
      combo.more.textContent = text.more;
      combo.more.hidden = !hasMore;
      const announcement = fill(text[shown.length === 1 ? "announceLocalityOne" : "announceLocalityMany"], {count: shown.length});
      open(combo, shown, hasMore ? `${announcement} ${text.more}` : announcement);
    };

    // --- The street suggestions ---
    // The street list belongs to the street and the house number (the person narrows it there).
    const streetHasFocus = () => document.activeElement === fields.street || document.activeElement === fields.house_number;
    // Nothing of the street list is under way any more: its timer, its request and the answer that would still arrive.
    const stopStreet = () => {
      clearTimeout(timers.street);
      channels.suggest.version += 1;
      if (channels.suggest.controller) channels.suggest.controller.abort();
    };
    const suggest = async () => {
      if (!isSwiss() || street().length < MIN_CHARS) {
        hide(combos.street);
        return;
      }
      if (!streetHasFocus()) return;
      const answer = await send("suggest");
      if (!answer) return;
      const data = answer.data;
      noteDirectory(data);
      if (!streetHasFocus()) return;
      if (answer.ok && data && Array.isArray(data.results) && data.results.length) showAddresses(data);
      else hide(combos.street);
    };

    // --- The town of a postal code and the postal code of a town ---
    const setAutomatically = (name, value, message) => {
      fields[name].value = value;
      autoFilled[name] = true;
      say(message);
      // The filled value completes the address: it is checked like a typed one, after the usual pause.
      clearTimeout(checkTimer);
      checkTimer = setTimeout(() => check(false), CHECK_DELAY);
    };

    const resolve = async (source) => {
      const combo = combos[source];
      // A list opens only while the focus is still in its field (the answer may come after the person moved on).
      const focused = () => document.activeElement === combo.input;
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
        } else if (focused()) showLocalities(combo, rows, text.titleLocality, data.more);
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
      if (focused()) showLocalities(combo, rows, onlyOneTown ? text.titlePostalCode : text.titleLocality, data.more);
    };

    // The minimum for a check (and so for the card): a street, a house number (an empty one is no mistake yet), and a complete
    // postal code or a town (the other may be missing: the card then names what fits). A partial postal code waits.
    const complete = () =>
      isSwiss() && street().length >= MIN_CHARS && houseNumber() !== "" && (postalCode() === "" || hasPostalCode()) &&
      (hasPostalCode() || locality().length >= MIN_CHARS);
    // The warning "not found" only for the whole address; a part of it gets the card or nothing (no nagging).
    const whole = () => hasPostalCode() && locality() !== "";

    // The card "Did you mean this address?": the street and house number are in the directory, but under another postal
    // code than the one entered. A click on an option takes it over; the link keeps what was entered.
    const takeProposal = (address) => {
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
      (lastField || fields.street).focus();
      check(false);
    };
    const keepEntered = () => {
      dismissed.add(signature());
      hideNotice();
      say(text.proposalKept);
      (lastField || fields.street).focus();
    };
    const showProposal = (reason, proposals) => {
      const first = proposals[0];
      const card = make("div", "address-proposal");
      card.append(
        make("p", "address-proposal-title", proposals.length > 1 ? text.proposalTitleMany : text.proposalTitleOne),
        make("p", "address-proposal-note", fill(text[noteKey(reason, hasPostalCode())], {
          address: `${street()} ${fields.house_number.value.trim()}`.trim(), street: street(),
          house_number: fields.house_number.value.trim(), postal_code: postalCode(), town: first.locality,
        }))
      );
      card.append(make("p", "address-proposal-hint", text.proposalHint));
      proposals.forEach((address) => {
        const option = make("button", "address-proposal-option");
        option.type = "button";
        const detail = make("span", "address-proposal-sub", `${address.postal_code} ${address.locality}`);
        option.append(make("span", "address-proposal-main", `${address.street} ${address.house_number}`.trim()), detail);
        option.addEventListener("click", () => takeProposal(address));
        card.append(option);
      });
      const keep = make("button", "address-proposal-keep", text.proposalKeep);
      keep.type = "button";
      keep.addEventListener("click", keepEntered);
      card.append(keep);
      notice.replaceChildren(card);
      notice.dataset.kind = "proposal";
      describe(true);
      reveal(card);
    };

    // ``atLoad``: the form was just opened (the person clicked "Edit"). Then an address that is not
    // found also fills the panel, so that the suggestions are ready next to the notice. Only the
    // not-found notice is shown to everybody; the state of the directory only to staff (D-066, D-111).
    const check = async (atLoad, retried = false) => {
      if (!complete()) return;
      const answer = await send("check");
      if (!answer || !answer.ok) return;
      const data = answer.data;
      if (data && data.status === "unavailable" && !retried) {
        // Busy for a moment (a slow request at the same time): ask once more, silently.
        clearTimeout(checkTimer);
        checkTimer = setTimeout(() => check(atLoad, true), RETRY_DELAY);
        return;
      }
      noteDirectory(data);
      if (data && data.status === "available" && data.match === false && data.warning) {
        // Said "No, my address is correct" for exactly these inputs: nothing more is said.
        if (dismissed.has(signature())) return;
        const proposals = notice && Array.isArray(data.proposals) ? data.proposals.slice(0, 3) : [];
        if (proposals.length) {
          showProposal(data.reason, proposals);
        } else if (whole()) {
          // After the budget of the approximate search is used up the card falls back to this notice (D17): the search
          // for a typing mistake is the one expensive step and has its own limit (limits.allow_approximate).
          showNotice("warning", data.warning);
          // No fix to offer: the person can still say that the address is right (never a dead end).
          const useEntered = make("button", "address-proposal-keep", text.useEntered);
          useEntered.type = "button";
          useEntered.addEventListener("click", keepEntered);
          // One box (the notice floats as an overlay on a desktop width: its children must not lie over each other).
          const box = make("div", "address-warning");
          box.append(notice.firstElementChild, useEntered);
          notice.replaceChildren(box);
          reveal(box);
          if (atLoad) suggest();
        }
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

    // A list belongs to the field that has the focus. The street list stays while the focus is in the house number (the
    // person narrows it there); anywhere else, and on a press outside the widget, every list closes and the street search
    // that is still under way is stopped (D1, B1). The card and the notice are closed by Escape, and when they float also by a press
    // outside, never recorded as "No, my address is correct" (G1).
    const inWidget = (target) =>
      Object.values(combos).some((combo) => combo.popup.contains(target) || combo.input.contains(target)) ||
      fields.house_number.contains(target);
    const closeAll = () => {
      stopStreet();
      hideAll();
    };
    addressFields.forEach((field) => {
      field.addEventListener("focusin", () => {
        lastField = field;
        if (field !== fields.street && field !== fields.house_number) stopStreet();
        Object.values(combos).forEach((combo) => {
          const keep = combo.input === field || (combo.name === "street" && field === fields.house_number);
          if (!keep) hide(combo);
        });
      });
      // Shift+Tab or a click out of the form: the focus goes to something that is not an address field.
      field.addEventListener("focusout", (event) => {
        const next = event.relatedTarget;
        if (next && (addressFields.includes(next) || inWidget(next))) return;
        closeAll();
      });
    });
    document.addEventListener("pointerdown", (event) => {
      if (!inWidget(event.target)) closeAll();
      // Only a card that floats (position absolute, the overlay of a desktop width) is closed by a press outside. A card in the
      // page (a phone) would shift the button the person presses between the press and the release, and the click would be
      // lost: it closes by Escape and by its two actions only. Decided by the computed position, not by a width.
      const card = notice && notice.firstElementChild;
      if (card && !notice.contains(event.target) && !inWidget(event.target) && getComputedStyle(card).position === "absolute") {
        closeCard();
      }
    });
    // Escape closes the card (and the notice) when no list is open; the form can be sent at any time.
    const closeCard = () => {
      if (!notice || notice.dataset.kind === "directory") return;
      if (notice.firstElementChild) {
        hideNotice();
        (lastField || fields.street).focus({preventScroll: true});
      }
    };
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (Object.values(combos).some((combo) => !combo.popup.hidden)) return;
      // The card (also the one the server drew after a submit, it is in the notice) closes without a record; it comes back
      // when a field changes and the next check finds a fix.
      if (form.contains(document.activeElement) || document.activeElement === document.body) closeCard();
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
            setActive(combo, Math.max(-1, combo.active - 1));
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
            event.stopPropagation();
            hide(combo);
          } else if (notice && notice.firstElementChild && notice.dataset.kind !== "directory") {
            event.preventDefault();
            hideNotice();
          }
        } else if (event.key === "Tab") {
          hide(combo);
        }
      });
    });

    // Never intercept form submission: directory failure cannot block saving.
    // A card the server drew after a submit is already in the notice: no second one from the load-time check.
    if (!notice || !notice.firstElementChild) check(true);
  });
})();
