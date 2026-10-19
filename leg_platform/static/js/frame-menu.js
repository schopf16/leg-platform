/* The phone menu of the frame (D-057): opens from the left, closes with the close button, a tap on
   the overlay or Escape. While it is open the focus stays inside it (the rest of the page is
   inert), and the focus returns to the menu button when it closes. Without this script the menu
   button is a link to the anchor of the menu, which the style sheet shows with :target.
   No request, no storage, no eval (CSP: script-src 'self'). */
(() => {
  "use strict";
  const root = document.documentElement;
  const button = document.getElementById("menu-btn");
  // More buttons may open the same menu (the "Menu" tab of the bottom bar of a staff account); the focus goes back to
  // the one that opened it.
  const openers = [button, ...document.querySelectorAll("[data-menu-open]")].filter(Boolean);
  const drawer = document.getElementById("frame-drawer");
  if (!button || !drawer) return;
  let opener = button;
  const panel = drawer.querySelector(".frame-drawer-panel");
  const behind = Array.from(document.querySelectorAll(".frame-header, .frame-main, .frame-bottombar, .corner"));
  const desktop = window.matchMedia("(min-width: 761px)");
  const FOCUSABLE = "a[href], button:not([disabled]), [tabindex]:not([tabindex='-1'])";
  let isOpen = false;

  const focusable = () =>
    Array.from(panel.querySelectorAll(FOCUSABLE)).filter((element) => element.offsetParent !== null);

  const open = () => {
    if (isOpen) return;
    isOpen = true;
    drawer.classList.add("is-open");
    root.classList.add("menu-open");
    openers.forEach((element) => element.setAttribute("aria-expanded", "true"));
    behind.forEach((element) => { element.inert = true; });
    const items = focusable();
    // The focus goes to the close button, the first thing a keyboard user may want (designer review
    // of phase 2); without it, to the first entry.
    const closeButton = panel.querySelector(".frame-drawer-head [data-menu-close]");
    if (closeButton) closeButton.focus();
    else if (items.length) items[0].focus();
  };

  const close = () => {
    if (!isOpen) return;
    isOpen = false;
    drawer.classList.remove("is-open");
    root.classList.remove("menu-open");
    openers.forEach((element) => element.setAttribute("aria-expanded", "false"));
    behind.forEach((element) => { element.inert = false; });
    // A leftover anchor (the page was opened with #frame-drawer) must not keep the menu open.
    if (window.location.hash === "#frame-drawer") {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    opener.focus();
  };

  openers.forEach((element) => {
    element.addEventListener("click", (event) => {
      event.preventDefault();
      opener = element;
      open();
    });
  });
  drawer.querySelectorAll("[data-menu-close]").forEach((element) => {
    element.addEventListener("click", (event) => {
      // The links to "#" are the way to close the menu without this script.
      event.preventDefault();
      close();
    });
  });
  drawer.addEventListener("keydown", (event) => {
    if (!isOpen) return;
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
    } else if (event.key === "Tab") {
      const items = focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
  // The menu is for a phone only: going to a wide window closes it.
  desktop.addEventListener("change", (event) => {
    if (event.matches) close();
  });
  // Opened through the anchor before the script ran: take over.
  if (window.location.hash === "#frame-drawer" && !desktop.matches) open();
})();
