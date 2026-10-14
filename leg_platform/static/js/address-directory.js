/* Local-only suggestions. No external calls, HTML injection or automatic edits. */
(() => {
  "use strict";
  document.querySelectorAll("[data-address-directory]").forEach((widget) => {
    const form = widget.closest("form");
    const status = widget.querySelector("[data-address-status]");
    const results = widget.querySelector("[data-address-results]");
    const fields = {};
    ["street", "house_number", "postal_code", "locality", "country"].forEach((name) => {
      const key = name.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase()) + "Id";
      fields[name] = document.getElementById(widget.dataset[key]);
    });
    if (!form || Object.values(fields).some((field) => !field)) return;
    const endpoint = new URL(widget.dataset.endpoint, location.href);
    if (endpoint.origin !== location.origin) return;
    let timer;
    let controller;
    let sequence = 0;
    const clear = () => results.replaceChildren();
    const lookup = async (mode) => {
      const version = ++sequence;
      if (controller) controller.abort();
      controller = new AbortController();
      clear();
      if (fields.country.value !== "CH" || !fields.street.value.trim()) {
        status.textContent = "";
        return;
      }
      const body = new URLSearchParams({mode});
      Object.entries(fields).forEach(([name, field]) => body.set(name, field.value));
      try {
        const token = form.querySelector("[name=csrfmiddlewaretoken]");
        if (!token) throw new Error("Missing CSRF token");
        const response = await fetch(endpoint.href, {
          method: "POST", credentials: "same-origin", mode: "same-origin",
          headers: {"X-CSRFToken": token.value, "Content-Type": "application/x-www-form-urlencoded"},
          body, signal: controller.signal, cache: "no-store",
        });
        const data = await response.json();
        if (version !== sequence) return;
        status.textContent = data.warning || "";
        if (!response.ok) return;
        if (data.results && data.results.length) {
          const list = document.createElement("ul");
          list.setAttribute("aria-label", widget.dataset.suggestions);
          data.results.slice(0, 10).forEach((address) => {
            const item = document.createElement("li");
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = `${address.street} ${address.house_number}, ${address.postal_code} ${address.locality}`;
            button.addEventListener("click", () => {
              // A deliberate selection is the only operation that changes fields.
              ["street", "house_number", "postal_code", "locality"].forEach((name) => {
                fields[name].value = address[name];
              });
              clear();
              lookup("check");
              fields.street.focus();
            });
            item.append(button);
            list.append(item);
          });
          results.append(list);
        }
      } catch (error) {
        if (error.name !== "AbortError" && version === sequence) {
          status.textContent = widget.dataset.unavailable;
        }
      }
    };
    Object.values(fields).forEach((field) => {
      field.addEventListener("input", () => {
        // Invalidate in-flight results immediately, not after the debounce delay.
        ++sequence;
        if (controller) controller.abort();
        clear();
        clearTimeout(timer);
        timer = setTimeout(() => lookup("suggest"), 300);
      });
      field.addEventListener("change", () => {
        clearTimeout(timer);
        lookup("check");
      });
    });
    // Never intercept form submission: directory failure cannot block saving.
    lookup("check");
  });
})();
