/* Escape closes an open info bubble (WCAG 2.2, 1.4.13 "dismissible"). The bubble itself works
   without a script (hover and focus, see management.css); this only adds the way to dismiss it.
   Nothing is read from the page except the classes below; no network, no dynamic code. */
(() => {
  "use strict";

  const reopen = (event) => {
    const info = event.target.closest && event.target.closest(".mgmt-info");
    if (info) {
      info.classList.remove("is-dismissed");
    }
  };

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") {
      return;
    }
    document.querySelectorAll(".mgmt-info").forEach((info) => {
      if (info.matches(":hover") || info.matches(":focus-within")) {
        info.classList.add("is-dismissed");
      }
    });
  });
  // The bubble comes back when the pointer or the focus enters again.
  document.addEventListener("mouseover", reopen);
  document.addEventListener("focusin", reopen);
})();
