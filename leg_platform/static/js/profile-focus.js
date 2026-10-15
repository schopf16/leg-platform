/* Focus after an action on the profile (designer acceptance of phase 4, D5). Every action loads the
   page again, so without this the focus would be on the page and a person using the keyboard or a
   screen reader would lose the place. The page works without this script.

   The order: an error summary has the focus already (autofocus); otherwise the message of the
   action, then the first field of the card that is open, then the heading of the card that the
   address names (#card-...). No request, no storage (CSP: script-src 'self'). */
(() => {
  "use strict";

  const focusLater = (element) => {
    if (!element) {
      return false;
    }
    if (!element.hasAttribute("tabindex") && !/^(INPUT|SELECT|TEXTAREA|BUTTON|A)$/.test(element.tagName)) {
      element.setAttribute("tabindex", "-1");
    }
    element.focus({ preventScroll: false });
    return document.activeElement === element;
  };

  const run = () => {
    if (document.querySelector(".error-summary")) {
      return;
    }
    if (focusLater(document.querySelector("[data-profile-message]"))) {
      return;
    }
    const open = document.querySelector('.profile-card[data-state="edit"]');
    if (open) {
      const field = open.querySelector(
        "input:not([type=hidden]):not([type=checkbox]):not([disabled]), select:not([disabled]), textarea:not([disabled])"
      );
      if (focusLater(field)) {
        return;
      }
      if (focusLater(open.querySelector("h2"))) {
        return;
      }
    }
    if (window.location.hash.startsWith("#card-")) {
      const card = document.getElementById(window.location.hash.slice(1));
      if (card) {
        focusLater(card.querySelector("h2"));
      }
    }
  };

  // After the load event and one task later: other scripts of the page and the jump to the anchor
  // of the address have run by then and would otherwise take the focus away again.
  if (document.readyState === "complete") {
    window.setTimeout(run, 0);
  } else {
    window.addEventListener("load", () => window.setTimeout(run, 0));
  }
})();
