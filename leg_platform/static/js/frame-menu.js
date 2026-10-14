/* The phone menu of the frame (D-057): opens from the left, closes with the close button, a tap on
   the overlay or Escape. While it is open the focus stays inside it (the rest of the page is
   inert), and the focus returns to the menu button when it closes. Without this script the menu
   button is a link to the anchor of the menu, which the style sheet shows with :target.
   No request, no storage, no eval (CSP: script-src 'self'). */
(() => {
  "use strict";
  const root = document.documentElement;
  const button = document.getElementById("menu-btn");
  const drawer = document.getElementById("frame-drawer");
  if (!button || !drawer) return;
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
    button.setAttribute("aria-expanded", "true");
    behind.forEach((element) => { element.inert = true; });
    const items = focusable();
    if (items.length) items[0].focus();
  };

  const close = () => {
    if (!isOpen) return;
    isOpen = false;
    drawer.classList.remove("is-open");
    root.classList.remove("menu-open");
    button.setAttribute("aria-expanded", "false");
    behind.forEach((element) => { element.inert = false; });
    // A leftover anchor (the page was opened with #frame-drawer) must not keep the menu open.
    if (window.location.hash === "#frame-drawer") {
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    button.focus();
  };

  button.addEventListener("click", (event) => {
    event.preventDefault();
    open();
  });
  drawer.querySelectorAll("[data-menu-close]").forEach((element) => {
    element.addEventListener("click", close);
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
