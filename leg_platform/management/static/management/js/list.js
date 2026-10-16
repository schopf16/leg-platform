/* Progressive enhancement of the lists of the management area (D-078). Everything works without
   this script (links, a GET form and POST buttons); the script only makes it faster:

   - a column can be resized with the handle at its right edge (mouse, touch or the arrow keys); the
     width is saved for the account (POST to data-widths-url, protected by the CSRF header);
   - a field with data-autosubmit sends its form when it changes; the search field a short moment
     after typing;
    - a row with data-expand-id opens and closes by a click on the row or on its arrow; the state is
     saved for the account (POST to data-expand-url). A click on a link or a button inside the row
     does not toggle it (the link to the detail page never opens or closes the row);
   - the strip of the quick filters (.mgmt-chips) scrolls so that the active filter stands at the left.

   Nothing is read from the page except these attributes; no network except the two addresses above;
   no dynamic code. */
(() => {
  "use strict";

  const list = document.querySelector("[data-list]");

  const post = (url, body) => {
    if (!list || !url) {
      return Promise.resolve();
    }
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": list.dataset.csrf || "" },
      body: JSON.stringify(body),
    }).catch(() => {});
  };

  // ---- Column widths ----
  if (list) {
    const minimum = parseInt(list.dataset.minWidth || "90", 10);
    const table = list.querySelector("table.mgmt-table");
    const save = () => {
      const widths = {};
      table.querySelectorAll("col[data-col]").forEach((col) => {
        const width = parseInt(col.getAttribute("width") || "", 10);
        if (width) {
          widths[col.dataset.col] = width;
        }
      });
      post(list.dataset.widthsUrl, { widths });
    };
    const setWidth = (cell, width) => {
      const col = table.querySelector('col[data-col="' + cell.dataset.col + '"]');
      if (col) {
        col.setAttribute("width", String(Math.max(minimum, Math.round(width))));
      }
    };

    if (table) {
      table.querySelectorAll(".mgmt-grip").forEach((grip) => {
        const cell = grip.closest("th");
        let startX = 0;
        let startWidth = 0;
        grip.addEventListener("pointerdown", (event) => {
          event.preventDefault();
          startX = event.clientX;
          startWidth = cell.getBoundingClientRect().width;
          grip.setPointerCapture(event.pointerId);
          grip.classList.add("is-dragging");
        });
        grip.addEventListener("pointermove", (event) => {
          if (grip.hasPointerCapture(event.pointerId)) {
            setWidth(cell, startWidth + event.clientX - startX);
          }
        });
        const end = (event) => {
          if (grip.hasPointerCapture(event.pointerId)) {
            grip.releasePointerCapture(event.pointerId);
            grip.classList.remove("is-dragging");
            save();
          }
        };
        grip.addEventListener("pointerup", end);
        grip.addEventListener("pointercancel", end);
        grip.addEventListener("keydown", (event) => {
          const step = event.shiftKey ? 40 : 10;
          if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
            event.preventDefault();
            setWidth(cell, cell.getBoundingClientRect().width + (event.key === "ArrowRight" ? step : -step));
            save();
          }
        });
      });
    }
  }

  // ---- Forms that send themselves ----
  document.querySelectorAll("[data-autosubmit]").forEach((field) => {
    field.addEventListener("change", () => {
      (field.form || document.getElementById("list-form")).requestSubmit();
    });
  });
  const form = document.querySelector("[data-autosubmit-form]");
  const search = form && form.querySelector("[data-search]");
  if (search) {
    let timer = 0;
    search.addEventListener("input", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        // The page is built again: ask it to give the search field the focus back.
        let focus = form.querySelector('input[name="focus"]');
        if (!focus) {
          focus = document.createElement("input");
          focus.type = "hidden";
          focus.name = "focus";
          focus.value = "q";
          form.appendChild(focus);
        }
        form.requestSubmit();
      }, 500);
    });
    if (search.hasAttribute("autofocus")) {
      const end = search.value.length;
      search.setSelectionRange(end, end);
    }
  }

  // ---- The quick filters on a phone: the active one stands at the left of the strip ----
  const strip = document.querySelector(".mgmt-chips");
  const active = strip && strip.querySelector(".mgmt-chip.is-on");
  if (active && strip.scrollWidth > strip.clientWidth) {
    strip.scrollLeft = active.offsetLeft - strip.offsetLeft - 16;
  }

  // ---- Rows that open ----
  const open = (id, state) => {
    document.querySelectorAll('[data-expand-id="' + id + '"]').forEach((row) => {
      if (row.hasAttribute("aria-expanded")) {
        row.setAttribute("aria-expanded", state ? "true" : "false");
      }
    });
    document.querySelectorAll('[data-expand-for="' + id + '"]').forEach((part) => {
      part.hidden = !state;
    });
  };
  const rows = () => Array.from(document.querySelectorAll("tr[data-expand-id][aria-expanded]"));
  const label = (button, allOpen) => {
    button.textContent = allOpen ? button.dataset.labelOpen : button.dataset.labelClosed;
    const hidden = button.form && button.form.querySelector('input[name="open"]');
    if (hidden) {
      hidden.value = allOpen ? "0" : "1";
    }
  };
  const refreshAll = () => {
    const button = document.querySelector("[data-expand-all]");
    if (button) {
      label(button, rows().length > 0 && rows().every((row) => row.getAttribute("aria-expanded") === "true"));
    }
  };

  const toggle = (id) => {
    const row = document.querySelector('tr[data-expand-id="' + id + '"][aria-expanded], article[data-expand-id="' + id + '"][aria-expanded]');
    if (!row) {
      return;
    }
    const state = row.getAttribute("aria-expanded") !== "true";
    open(id, state);
    post(list && list.dataset.expandUrl, { ids: [parseInt(id, 10)], open: state });
    refreshAll();
  };

  document.addEventListener("click", (event) => {
    const caret = event.target.closest("[data-expand-toggle]");
    if (caret) {
      event.preventDefault();
      toggle(caret.closest("[data-expand-id]").dataset.expandId);
      return;
    }
    const row = event.target.closest("tr.mgmt-site.is-expandable");
    if (row && !event.target.closest("a, button, input, select, textarea, label")) {
      toggle(row.dataset.expandId);
    }
    const all = event.target.closest("[data-expand-all]");
    if (all) {
      event.preventDefault();
      const state = all.form.querySelector('input[name="open"]').value === "1";
      const ids = rows().map((item) => item.dataset.expandId);
      ids.forEach((id) => open(id, state));
      post(list && list.dataset.expandUrl, { ids: ids.map((id) => parseInt(id, 10)), open: state });
      refreshAll();
    }
  });
})();
