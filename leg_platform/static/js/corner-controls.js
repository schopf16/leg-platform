/* The two corner buttons: language menu and light/dark button (D-056).

   They are the same on every page. The language is chosen on the server: every language of the
   menu is a submit button of a form that posts to the view set_language, which sets the language
   cookie and renders the page again (no JavaScript is needed for that, and this script stores or
   sends nothing about the language). This script only adds the behaviour of the menu (a tap opens
   it, Escape closes it, the arrow keys move) and the light/dark button. The colour mode is kept in
   the sessionStorage of the tab only (team lead decision K3): no cookie, nothing sent to the
   server, a new tab starts light.
   No request, no inline script, no eval (CSP: script-src 'self'). */
(() => {
  "use strict";
  const root = document.documentElement;
  const wrap = document.getElementById("lang");
  const languageButton = document.getElementById("lang-btn");
  const themeButton = document.getElementById("theme-btn");
  const THEMES = ["light", "dark"];

  const read = (key, allowed, fallback) => {
    try {
      const value = window.sessionStorage.getItem(key);
      return allowed.includes(value) ? value : fallback;
    } catch (error) {
      return fallback;
    }
  };
  const write = (key, value) => {
    try {
      window.sessionStorage.setItem(key, value);
    } catch (error) {
      // Storage is not available: the choice holds until the page changes.
    }
  };

  /* ---- Theme ---- */
  if (themeButton) {
    const applyTheme = (theme) => {
      root.setAttribute("data-theme", theme);
      root.setAttribute("data-bs-theme", theme);
      themeButton.dataset.state = theme;
      const label = theme === "dark" ? themeButton.dataset.labelDark : themeButton.dataset.labelLight;
      themeButton.setAttribute("aria-label", label);
      themeButton.title = label;
    };
    themeButton.addEventListener("click", () => {
      const next = themeButton.dataset.state === "dark" ? "light" : "dark";
      write("leg-theme", next);
      applyTheme(next);
    });
    applyTheme(read("leg-theme", THEMES, "light"));
  }

  /* ---- Language menu: opens on hover, on focus and on a tap; Escape closes it ---- */
  if (!wrap || !languageButton) return;
  const items = Array.from(wrap.querySelectorAll(".lang-menu button"));
  const isOpen = () =>
    !wrap.classList.contains("dismissed") &&
    (wrap.matches(":hover") || wrap.matches(":focus-within") || wrap.classList.contains("open"));
  const sync = () => languageButton.setAttribute("aria-expanded", String(isOpen()));
  const close = () => {
    wrap.classList.remove("open");
    wrap.classList.add("dismissed");
    sync();
  };
  const reset = () => {
    wrap.classList.remove("open", "dismissed");
    sync();
  };

  wrap.addEventListener("mouseenter", sync);
  wrap.addEventListener("mouseleave", reset);
  wrap.addEventListener("focusin", sync);
  wrap.addEventListener("focusout", () => {
    // The focus moves after this event: look at where it went.
    window.setTimeout(() => {
      if (!wrap.contains(document.activeElement)) reset();
      else sync();
    }, 0);
  });
  languageButton.addEventListener("click", () => {
    // A tap also makes the browser "hover" the button, so the state cannot be read from the
    // hover: the menu closes only if the click itself opened it before.
    if (wrap.classList.contains("open")) {
      close();
    } else {
      wrap.classList.remove("dismissed");
      wrap.classList.add("open");
      sync();
    }
  });
  document.addEventListener("click", (event) => {
    if (!wrap.contains(event.target)) reset();
  });
  wrap.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isOpen()) {
      event.stopPropagation();
      close();
      languageButton.focus();
    } else if ((event.key === "ArrowDown" || event.key === "ArrowUp") && items.length > 0) {
      event.preventDefault();
      wrap.classList.remove("dismissed");
      const position = items.indexOf(document.activeElement);
      const step = event.key === "ArrowDown" ? 1 : -1;
      const next = position === -1
        ? (step === 1 ? 0 : items.length - 1)
        : (position + step + items.length) % items.length;
      items[next].focus();
      sync();
    }
  });
  sync();
})();
