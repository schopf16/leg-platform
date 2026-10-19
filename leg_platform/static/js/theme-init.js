/* Colour mode at the start of a page (D-056).

   Loaded in the <head> without "defer", so that the page is painted in the right colours at once.
   Light mode is the default. The choice of the person is kept only in the sessionStorage of this
   tab (team lead decision K3): no cookie, nothing is sent to the server. A new tab starts light
   again. The language is not handled here: the server renders it (leg_platform/language.py). Only
   values of an allowlist are read; nothing comes from the URL (docs/DESIGN.md, section 7). No
   inline script, no eval (CSP: script-src 'self'). */
(() => {
  "use strict";
  const root = document.documentElement;
  const THEMES = ["light", "dark"];
  let theme = "light";
  try {
    const stored = window.sessionStorage.getItem("leg-theme");
    if (THEMES.includes(stored)) theme = stored;
  } catch (error) {
    // Storage is not available (private mode, blocked): the default stays.
  }
  root.setAttribute("data-theme", theme);
  // Tabler styles its components by data-bs-theme; both attributes always have the same value.
  root.setAttribute("data-bs-theme", theme);
  // Marks a page whose buttons can work (the light/dark button is hidden without it).
  root.classList.add("js");
})();
