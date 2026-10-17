/* Fits the tables of the management area into their card on a medium screen (D-084; the lists between 768 and 1279 px).

   The start widths of the columns are the ones of the templates (or the ones the account saved). When they do not
   fit, the columns that may shrink (all but the ones marked data-keep, such as the metering point number, which is
   never cut) are scaled down together, but never below the minimum of 90 px. If the table still does not fit, the
   card of the table scrolls sideways (the class is-overflowing), never the whole page. Without this script the
   card scrolls from 1279 px downwards (management.css).

   Only widths are set (CSSOM, no style attribute in the markup); nothing is read from the page except the
   attributes of the table, no network, no dynamic code. */
(() => {
  "use strict";

  document.documentElement.classList.add("mgmt-js");
  const MINIMUM = 90;
  // A column is narrowed to 60 % of its width at most: a text that is cut to one letter helps nobody, the card
  // scrolls instead.
  const FLOOR = 0.6;
  const LAST_MINIMUM = 150;

  const fit = (view) => {
    const table = view.querySelector("table.mgmt-table-fixed");
    if (!table) {
      view.classList.toggle("is-overflowing", view.scrollWidth > view.clientWidth + 1);
      return;
    }
    const cols = Array.from(table.querySelectorAll("col"));
    cols.forEach((col) => {
      col.style.removeProperty("width");
    });
    const width = (col) => parseInt(col.getAttribute("width") || "0", 10);
    const available = view.clientWidth;
    if (!available) {
      return;
    }
    const fixed = cols.filter((col) => !col.dataset.col);
    const data = cols.filter((col) => col.dataset.col);
    const keep = data.filter((col) => col.hasAttribute("data-keep") && width(col));
    const shrink = data.filter((col) => !col.hasAttribute("data-keep") && width(col));
    // A column without a width takes what is left; it needs at least this much.
    const rest = data.filter((col) => !width(col)).length * LAST_MINIMUM;
    const sum = (list) => list.reduce((total, col) => total + width(col), 0);
    const needed = sum(fixed) + sum(keep) + sum(shrink) + rest;
    if (needed > available && shrink.length) {
      const room = available - sum(fixed) - sum(keep) - rest;
      const factor = Math.min(Math.max(room / sum(shrink), FLOOR), 1);
      shrink.forEach((col) => {
        const head = table.querySelector('th[data-col="' + col.dataset.col + '"]');
        const label = head
          ? Array.from(head.children)
              .filter((child) => !child.classList.contains("mgmt-grip"))
              .reduce((total, child) => total + child.getBoundingClientRect().width + 6, 24)
          : 0;
        col.style.width = Math.max(MINIMUM, label, Math.floor(width(col) * factor)) + "px";
      });
    }
    view.classList.toggle("is-overflowing", view.scrollWidth > available + 1);
  };

  const all = () => document.querySelectorAll(".mgmt-table-view");
  const run = () => all().forEach(fit);
  run();
  let timer = 0;
  window.addEventListener("resize", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(run, 100);
  });
  // The width of a column that was dragged: fit again after the handle was released.
  document.addEventListener("pointerup", () => window.setTimeout(run, 0));
  document.addEventListener("keyup", (event) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      window.setTimeout(run, 0);
    }
  });
})();
