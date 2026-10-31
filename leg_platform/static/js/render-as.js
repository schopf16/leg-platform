/*
 * Support view "View as" (D-131): progressive enhancement only. The server refuses every write and every page that is not
 * on its list (leg_platform.render_as); this script makes the page say so before the click: what would be refused stays
 * visible but is disabled (aria-disabled, hint "Not possible in this view").
 * The bar has data-allowed (the paths of the allowed pages) and data-hint (the translated hint). Elements with data-render-as-keep (the bar, the log out
 * button) are left alone.
 */
(function () {
  "use strict";
  var bar = document.querySelector(".render-as-bar");
  if (!bar) { return; }
  var allowed = (bar.getAttribute("data-allowed") || "").split(" ").filter(Boolean);
  var hint = bar.getAttribute("data-hint") || "";
  var scope = document.querySelector(".frame-main") || document.body;

  function kept(el) { return !!el.closest("[data-render-as-keep]"); }

  function block(event) {
    event.preventDefault();
    event.stopPropagation();
  }

  function disable(el) {
    el.setAttribute("aria-disabled", "true");
    el.classList.add("is-view-as-disabled");
    if (!el.getAttribute("title")) { el.setAttribute("title", hint); }
    el.addEventListener("click", block, true);
  }

  scope.querySelectorAll("a[href]").forEach(function (link) {
    if (kept(link)) { return; }
    var raw = link.getAttribute("href");
    if (raw.charAt(0) === "#") { return; }
    var url;
    try { url = new URL(raw, window.location.href); } catch (e) { return; }
    if (url.origin !== window.location.origin) { return; }
    if (url.search === "" && allowed.indexOf(url.pathname) !== -1) { return; }
    link.removeAttribute("href");
    link.setAttribute("role", "link");
    disable(link);
  });

  scope.querySelectorAll("form").forEach(function (form) {
    if (kept(form)) { return; }
    form.addEventListener("submit", block, true);
    form.querySelectorAll("button, input[type=submit], input[type=button]").forEach(function (button) {
      button.setAttribute("aria-disabled", "true");
      button.classList.add("is-view-as-disabled");
      if (!button.getAttribute("title")) { button.setAttribute("title", hint); }
      button.addEventListener("click", block, true);
    });
  });
})();
