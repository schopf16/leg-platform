/* The two corner buttons: language menu and light/dark button (D-056).

   They are the same on every page. The choice is kept in the sessionStorage of the tab only
   (team lead decision K3): nothing is stored in a cookie or sent to the server, and a new tab
   starts light and German. Only the pages that load login-texts.js have translated texts; on
   every other page the language button stores the choice but the page stays German.
   No request, no inline script, no eval (CSP: script-src 'self'). */
(() => {
  "use strict";
  const root = document.documentElement;
  const wrap = document.getElementById("lang");
  const languageButton = document.getElementById("lang-btn");
  const themeButton = document.getElementById("theme-btn");
  if (!wrap || !languageButton || !themeButton) return;

  const texts = window.LEG_PAGE_TEXTS || null;
  const LANGUAGES = ["de", "fr", "it", "en"];
  const NAMES = { de: "Deutsch", fr: "Français", it: "Italiano", en: "English" };
  const THEMES = ["light", "dark"];
  const items = Array.from(wrap.querySelectorAll("[data-lang]"));
  let currentLanguage = "de";

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
  const applyTheme = (theme) => {
    root.setAttribute("data-theme", theme);
    root.setAttribute("data-bs-theme", theme);
    themeButton.dataset.state = theme;
    const dictionary = texts && texts[currentLanguage];
    const label = dictionary
      ? (theme === "dark" ? dictionary.themeLabelDark : dictionary.themeLabelLight)
      : (theme === "dark" ? themeButton.dataset.labelDark : themeButton.dataset.labelLight);
    themeButton.setAttribute("aria-label", label);
    themeButton.title = label;
  };

  /* ---- Language ---- */
  const translate = (code) => {
    const dictionary = texts && texts[code];
    if (!dictionary) return;
    root.lang = dictionary.lang;
    document.querySelectorAll("[data-i18n]").forEach((element) => {
      const text = dictionary[element.getAttribute("data-i18n")];
      if (typeof text === "string") element.textContent = text;
    });
    document.dispatchEvent(new CustomEvent("leg:language", { detail: dictionary }));
  };
  const applyLanguage = (code, announce) => {
    currentLanguage = code;
    languageButton.textContent = code.toUpperCase();
    const dictionary = texts && texts[code];
    languageButton.setAttribute(
      "aria-label",
      dictionary ? dictionary.langLabel : `${languageButton.dataset.labelPrefix}: ${NAMES[code]}`
    );
    items.forEach((item) => item.setAttribute("aria-current", String(item.dataset.lang === code)));
    if (announce || code !== "de") translate(code);
    applyTheme(themeButton.dataset.state);
  };

  /* ---- Menu: opens on hover, on focus and on a tap; Escape closes it ---- */
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
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
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
  items.forEach((item) => {
    item.addEventListener("click", () => {
      const code = item.dataset.lang;
      if (!LANGUAGES.includes(code)) return;
      write("leg-lang", code);
      applyLanguage(code, true);
      close();
      languageButton.focus();
    });
  });

  themeButton.addEventListener("click", () => {
    const next = themeButton.dataset.state === "dark" ? "light" : "dark";
    write("leg-theme", next);
    applyTheme(next);
  });

  /* ---- Start: the stored choice of this tab, otherwise light and German ---- */
  themeButton.dataset.state = read("leg-theme", THEMES, "light");
  applyLanguage(read("leg-lang", LANGUAGES, "de"), false);
  sync();
})();
