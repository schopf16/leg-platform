/* A combobox for a field with data-suggest-url (D-094): the suggestions appear in a list under the field, a white
   panel in the colours of the page (not the grey box of the browser), at most ten rows, scrollable. It replaces
   the browser's own <datalist> list. Everything works without this script: the server resolves the typed text
   itself; the script only shows the suggestions.

   ARIA 1.2: the input is a combobox (role, aria-expanded, aria-controls, aria-activedescendant), the list a
   listbox of options; a polite live region says how many suggestions there are.
   Keyboard: arrow down opens the list and moves down, arrow up moves up, Enter takes the active row (only then
   it does not send the form), Escape closes the list and keeps the typed text, Tab closes it and moves on.
   Pointer and touch: a tap on a row takes it (the click, so that scrolling the list with a finger takes nothing);
   the list stays open while a row is pressed. The list is never turned above the field.

   Speed: at least two characters (the server answers nothing below that), 120 ms after the last key, and an
   answer for an older text is dropped: the request for it is cancelled (AbortController), so fast typing does not
   pile up requests. The answers are shown as text (textContent); nothing is built from markup. */
(() => {
  "use strict";

  const DELAY = 120;
  const MINIMUM = 2;
  const MAXIMUM = 10;
  const SEPARATOR = " · ";

  const make = (tag, className, content) => {
    const element = document.createElement(tag);
    if (className) {
      element.className = className;
    }
    if (content !== undefined) {
      element.textContent = content;
    }
    return element;
  };

  document.querySelectorAll("input[data-suggest-url]").forEach((input, index) => {
    const url = input.dataset.suggestUrl;
    const box = make("div", "mgmt-combobox");
    input.parentNode.insertBefore(box, input);
    box.appendChild(input);
    const list = make("ul", "mgmt-combobox-list");
    list.id = "combobox-list-" + index;
    list.setAttribute("role", "listbox");
    list.hidden = true;
    box.appendChild(list);
    const status = make("div", "visually-hidden");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    box.appendChild(status);

    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-haspopup", "listbox");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", list.id);

    let options = [];
    let active = -1;
    let timer = 0;
    let controller = null;
    let last = "";
    let pressing = false;
    let chosen = "";
    let pointerType = "mouse";
    let pausedUntil = 0;

    const close = () => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    };

    const setActive = (position) => {
      options.forEach((option, number) => {
        const on = number === position;
        option.element.setAttribute("aria-selected", on ? "true" : "false");
        option.element.classList.toggle("is-active", on);
      });
      active = position;
      if (position >= 0) {
        input.setAttribute("aria-activedescendant", options[position].element.id);
        options[position].element.scrollIntoView({ block: "nearest" });
      } else {
        input.removeAttribute("aria-activedescendant");
      }
    };

    const choose = (position) => {
      input.value = options[position].value;
      last = input.value.trim();
      // A row that was taken is not offered again when the field gets the focus back (UX-001).
      chosen = last;
      close();
      // On a touch screen the focus stays where the tap put it: the keyboard would come back and cover the form.
      if (pointerType === "mouse" && document.activeElement !== input) {
        input.focus();
      }
    };

    const open = () => {
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      // On a phone the keyboard must not hide the list.
      list.scrollIntoView({ block: "nearest" });
    };

    // A row that cannot be chosen: nothing found, or a pause after too many requests.
    const note = (text) => {
      list.replaceChildren();
      options = [];
      active = -1;
      input.removeAttribute("aria-activedescendant");
      const empty = make("li", "mgmt-combobox-empty", text);
      empty.setAttribute("role", "option");
      empty.setAttribute("aria-disabled", "true");
      empty.id = list.id + "-empty";
      list.appendChild(empty);
      status.textContent = text;
      open();
    };

    const render = (values) => {
      if (!values.length) {
        note(input.dataset.messageNone || "");
        return;
      }
      list.replaceChildren();
      options = [];
      active = -1;
      input.removeAttribute("aria-activedescendant");
      values.slice(0, MAXIMUM).forEach((value, number) => {
        const cut = value.lastIndexOf(SEPARATOR);
        const title = cut > 0 ? value.slice(0, cut) : value;
        const detail = cut > 0 ? value.slice(cut + SEPARATOR.length) : "";
        const element = make("li", "mgmt-combobox-option");
        element.setAttribute("role", "option");
        element.setAttribute("aria-selected", "false");
        element.id = list.id + "-" + number;
        element.appendChild(make("span", "mgmt-combobox-main", title));
        if (detail) {
          element.appendChild(make("span", "mgmt-combobox-sub", (input.dataset.detailLabel || "") + " " + detail));
        }
        element.addEventListener("click", () => choose(number));
        list.appendChild(element);
        options.push({ element, value });
      });
      const count = options.length;
      status.textContent = (count === 1 ? input.dataset.messageOne : input.dataset.messageMany || "").replace(
        "%(count)s",
        String(count)
      );
      open();
    };

    const search = (text) => {
      if (controller) {
        controller.abort();
      }
      if (Date.now() < pausedUntil) {
        // The server said "too many requests": no request until the time it named has passed (M-3, M-4). The text is
        // forgotten, so the same text is searched again once the pause is over (L-D).
        last = "";
        note(input.dataset.messageLimited || "");
        return;
      }
      controller = new AbortController();
      fetch(url + "?" + new URLSearchParams({ q: text }).toString(), {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      })
        .then((response) => {
          if (response.status === 429) {
            const seconds = Number(response.headers.get("Retry-After"));
            pausedUntil = Date.now() + (seconds > 0 && seconds <= 120 ? seconds : 30) * 1000;
            last = ""; // L-D: the same text is searched again after the pause
            if (input.value.trim() === text) {
              note(input.dataset.messageLimited || "");
            }
            return null;
          }
          return response.ok ? response.json() : null;
        })
        .then((data) => {
          if (data && input.value.trim() === text) {
            render(Array.isArray(data.results) ? data.results.filter((value) => typeof value === "string") : []);
          }
        })
        .catch(() => {});
    };

    input.addEventListener("input", () => {
      window.clearTimeout(timer);
      chosen = "";
      const text = input.value.trim();
      if (text.length < MINIMUM) {
        if (controller) {
          controller.abort();
        }
        close();
        last = "";
        return;
      }
      if (text === last) {
        return;
      }
      timer = window.setTimeout(() => {
        last = text;
        search(text);
      }, DELAY);
    });

    input.addEventListener("keydown", (event) => {
      const isOpen = !list.hidden;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (!isOpen && options.length) {
          open();
          setActive(0);
        } else if (isOpen && options.length) {
          setActive(Math.min(active + 1, options.length - 1));
        }
      } else if (event.key === "ArrowUp") {
        if (isOpen && options.length) {
          event.preventDefault();
          setActive(Math.max(active - 1, 0));
        }
      } else if (event.key === "Enter") {
        if (isOpen && active >= 0) {
          event.preventDefault();
          choose(active);
        }
      } else if (event.key === "Escape") {
        if (isOpen) {
          event.preventDefault();
          close();
        }
      } else if (event.key === "Tab") {
        close();
      }
    });

    // The list stays open while a row is pressed or touched (the click that takes the row comes after the blur).
    list.addEventListener("pointerdown", (event) => {
      pressing = true;
      pointerType = event.pointerType || "mouse";
      if (event.pointerType === "mouse") {
        event.preventDefault();
      }
    });
    const release = () => {
      window.setTimeout(() => {
        pressing = false;
      }, 300);
    };
    list.addEventListener("pointerup", release);
    list.addEventListener("pointercancel", release);

    input.addEventListener("blur", () => {
      window.setTimeout(() => {
        if (!pressing && document.activeElement !== input) {
          close();
        }
      }, 150);
    });
    input.addEventListener("focus", () => {
      if (options.length && input.value.trim() === last && last.length >= MINIMUM && chosen !== last) {
        open();
      }
    });
  });
})();
