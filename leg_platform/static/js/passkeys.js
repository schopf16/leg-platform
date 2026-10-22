/* D-106. Self-hosted SimpleWebAuthn handles the browser's binary/JSON conversion.
 * No credential, challenge, assertion or browser exception is logged.
 */
(() => {
  "use strict";
  if (typeof PublicKeyCredential !== "undefined" && typeof SimpleWebAuthnBrowser !== "undefined") {
    document.querySelectorAll("[data-passkey-login] [data-passkey-submit]").forEach(button => {
      button.disabled = false;
    });
  }
  async function post(url, data, form) {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": form.querySelector("[name=csrfmiddlewaretoken]").value
      },
      body: JSON.stringify(data)
    });
    if (!response.ok) throw new Error("Passkey request refused");
    return response.json();
  }
  document.addEventListener("submit", async event => {
    const form = event.target;
    const proof = form.querySelector("[data-passkey-proof]");
    const register = form.matches("[data-passkey-register]");
    const login = form.matches("[data-passkey-login]");
    if (!proof && !register && !login) return;
    const input = proof && document.getElementById(proof.dataset.field);
    if (input && input.value) return;
    event.preventDefault();
    const control = proof || form;
    const status = control.querySelector("[data-passkey-status]");
    const button = event.submitter;
    const mode = register ? button?.value : null;
    if (form.dataset.passkeyBusy) return;
    // One native password-form checkbox is authoritative for both independent methods.
    // Snapshot before any asynchronous prompt so changes during the ceremony cannot alter it.
    const stay = login && document.getElementById(form.dataset.rememberMe)?.checked === true;
    form.dataset.passkeyBusy = "true";
    if (button) button.disabled = true;
    if (register && status) {
      status.setAttribute("role", "status");
      status.textContent = control.dataset.pending || "";
    }
    try {
      const purpose = proof ? "proof" : register ? "register" : "login";
      const request = {
        purpose,
        action: proof ? new URL(form.action, location.href).pathname : ""
      };
      // No implicit platform fallback: registration must name an explicit action.
      if (register) {
        if (mode !== "this_device" && mode !== "another_device") {
          throw new Error("Choose a registration mode");
        }
        request.mode = mode;
      }
      const options = await post(control.dataset.options, request, form);
      const credential = await SimpleWebAuthnBrowser[
        register ? "startRegistration" : "startAuthentication"
      ]({optionsJSON: options.options});
      // UX guard only: attachment is browser-reported, not signed device evidence.
      // Older clients may omit it. Never infer physical independence from its absence.
      if (register && mode === "another_device" && credential.authenticatorAttachment === "platform") {
        if (status) {
          status.setAttribute("role", "alert");
          status.textContent = control.dataset.deviceError;
        }
        return;
      }
      const data = {ceremony: options.ceremony, credential};
      if (proof) {
        input.value = JSON.stringify(data);
        if (button) button.disabled = false;
        delete form.dataset.passkeyBusy;
        form.requestSubmit(button || undefined);
      } else {
        if (register) data.name = form.elements.namedItem("name").value;
        if (login) data.stay = stay;
        const result = await post(form.action || location.href, data, form);
        location.assign(result.redirect);
      }
    } catch (_) {
      if (status) {
        status.setAttribute("role", "alert");
        status.textContent = control.dataset.error;
      }
      if (input) input.value = "";
    } finally {
      if (button) button.disabled = false;
      delete form.dataset.passkeyBusy;
    }
  });
})();
