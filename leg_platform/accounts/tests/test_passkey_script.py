"""Browser-helper compatibility with stubbed credentials; no real authenticator is used."""

import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class PasskeyRegistrationScriptTests(SimpleTestCase):
    def test_modes_reach_native_options_without_fallback_and_platform_result_is_not_submitted(self):
        chrome = next((
            found for name in ("chrome", "google-chrome", "chromium", "chromium-browser")
            if (found := shutil.which(name))
        ), None)
        if chrome is None:
            chrome = next((
                path for path in (
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                ) if Path(path).exists()
            ), None)
        if chrome is None:
            self.skipTest("no Chrome or Chromium found")
        static = Path(settings.BASE_DIR) / "leg_platform" / "static"
        # Inline scripts belong only to this disposable, offline test harness, not the portal.
        page = """
<!doctype html><meta charset="utf-8">
<input name="name" form="passkey-create" required>
<form id="passkey-create" data-passkey-register data-options="/options/" data-error="Cancelled: choose another method"
  data-pending="Follow the browser prompt"
  data-device-error="Local creation was not saved in the portal">
  <input name="csrfmiddlewaretoken" value="synthetic-csrf">
  <button name="mode" value="another_device">Another device</button>
  <button name="mode" value="this_device">This device</button>
  <p role="status" data-passkey-status></p>
</form><pre id="out"></pre>
<script>
const calls = [], native = [], submissions = [], out = {}, pending = [];
let cancel = true, attachment = null;
window.fetch = async (url, request) => {
  const data = JSON.parse(request.body);
  calls.push({url, data, csrf: request.headers["X-CSRFToken"], credentials: request.credentials});
  if (url !== "/options/") {
    submissions.push(data);
    return {ok: true, json: async () => ({redirect: "#saved"})};
  }
  const other = data.mode === "another_device";
  return {ok: true, json: async () => ({ceremony: "synthetic", options: {
    challenge: "AQIDBA", rp: {id: "localhost", name: "Test"},
    user: {id: "AQIDBA", name: "test@example.org", displayName: "Test"},
    pubKeyCredParams: [{type: "public-key", alg: -7}],
    excludeCredentials: [{type: "public-key", id: "AQIDBA"}],
    authenticatorSelection: {authenticatorAttachment: other ? "cross-platform" : "platform",
      residentKey: "required", requireResidentKey: true, userVerification: "required"},
    hints: [other ? "hybrid" : "client-device"]
  }})};
};
Object.defineProperty(navigator.credentials, "create", {value: async ({publicKey, mediation}) => {
  const status = document.querySelector("[data-passkey-status]");
  pending.push({role: status.getAttribute("role"), text: status.textContent});
  native.push({
    hints: publicKey.hints, selection: publicKey.authenticatorSelection,
    binaryChallenge: publicKey.challenge instanceof ArrayBuffer,
    binaryExcludedId: publicKey.excludeCredentials[0].id instanceof ArrayBuffer,
    challenge: Array.from(new Uint8Array(publicKey.challenge)),
    userId: Array.from(new Uint8Array(publicKey.user.id)),
    excludedId: Array.from(new Uint8Array(publicKey.excludeCredentials[0].id)),
    rp: publicKey.rp, algorithms: publicKey.pubKeyCredParams,
    conditional: mediation === "conditional"
  });
  if (cancel) throw new DOMException("Synthetic cancellation", "NotAllowedError");
  return {
    id: "AQIDBA", rawId: new Uint8Array([1, 2, 3, 4]).buffer, type: "public-key",
    response: {attestationObject: new ArrayBuffer(0), clientDataJSON: new ArrayBuffer(0)},
    getClientExtensionResults: () => ({}), authenticatorAttachment: attachment
  };
}});
</script>
"""
        page += (
            f'<script src="{(static / "vendor/simplewebauthn/index.umd.min.js").as_uri()}"></script>'
            f'<script src="{(static / "js/passkeys.js").as_uri()}"></script>'
        )
        page += """
<script>
(async () => {
  try {
    const form = document.querySelector("form"), buttons = form.querySelectorAll("button");
    const wait = () => new Promise(resolve => setTimeout(resolve, 20));
    const name = form.elements.namedItem("name");
    out.externalNameIsAssociated = name.form === form;
    out.invalidNameRejected = !form.checkValidity();
    form.requestSubmit(buttons[0]);
    await wait();
    out.invalidSubmissionCalls = calls.length;
    out.invalidSubmissionPrompts = native.length;
    name.value = "Synthetic phone";
    for (const button of buttons) {
      form.requestSubmit(button);
      await wait();
    }
    buttons[0].value = "untrusted";
    form.requestSubmit(buttons[0]);
    await wait();
    form.dispatchEvent(new SubmitEvent("submit", {bubbles: true, cancelable: true}));
    await wait();
    out.calls = calls.slice();
    out.native = native.slice();
    out.status = form.querySelector("[data-passkey-status]").textContent;
    out.ready = !form.dataset.passkeyBusy && Array.from(buttons).every(b => !b.disabled);
    out.errorRole = form.querySelector("[data-passkey-status]").getAttribute("role");
    cancel = false;
    out.results = [];
    for (const [mode, reported] of [
      ["another_device", "platform"], ["another_device", "cross-platform"],
      ["another_device", null], ["another_device", "future-value"], ["this_device", "platform"]
    ]) {
      attachment = reported;
      buttons[0].value = mode;
      form.querySelector("[data-passkey-status]").textContent = "";
      const before = submissions.length, prompts = native.length;
      form.requestSubmit(buttons[0]);
      await wait();
      out.results.push({
        mode, reported, submitted: submissions.length - before, prompts: native.length - prompts,
        status: form.querySelector("[data-passkey-status]").textContent,
        role: form.querySelector("[data-passkey-status]").getAttribute("role"),
        ready: !form.dataset.passkeyBusy && Array.from(buttons).every(b => !b.disabled)
      });
    }
    out.submissions = submissions;
    out.pending = pending;
    out.name = form.elements.namedItem("name").value;
  } catch (error) { out.failure = error.name; }
  document.getElementById("out").textContent = "RESULT" + JSON.stringify(out) + "END";
})();
</script>
"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "passkeys.html"
            path.write_text(page, encoding="utf-8")
            result = subprocess.run(
                [chrome, "--headless=new", "--disable-gpu", f"--user-data-dir={folder}/profile",
                 "--allow-file-access-from-files", "--virtual-time-budget=10000", "--dump-dom", path.as_uri()],
                capture_output=True, timeout=60, check=False,
            )
        match = re.search(r'<pre id="out">RESULT(.*?)END</pre>', result.stdout.decode("utf-8", "replace"), re.S)
        self.assertIsNotNone(match, f"No browser result (exit {result.returncode}).")
        out = json.loads(html.unescape(match.group(1)))
        self.assertNotIn("failure", out)
        self.assertTrue(out["externalNameIsAssociated"])
        self.assertTrue(out["invalidNameRejected"])
        self.assertEqual(out["invalidSubmissionCalls"], 0)
        self.assertEqual(out["invalidSubmissionPrompts"], 0)
        self.assertEqual([call["data"]["mode"] for call in out["calls"]], ["another_device", "this_device"])
        self.assertTrue(all(call["csrf"] == "synthetic-csrf" for call in out["calls"]))
        self.assertTrue(all(call["credentials"] == "same-origin" for call in out["calls"]))
        self.assertEqual([item["hints"] for item in out["native"]], [["hybrid"], ["client-device"]])
        self.assertEqual(
            [item["selection"]["authenticatorAttachment"] for item in out["native"]],
            ["cross-platform", "platform"],
        )
        for item in out["native"]:
            self.assertEqual(item["selection"]["residentKey"], "required")
            self.assertEqual(item["selection"]["userVerification"], "required")
            self.assertTrue(item["binaryChallenge"])
            self.assertTrue(item["binaryExcludedId"])
            self.assertEqual(item["challenge"], [1, 2, 3, 4])
            self.assertEqual(item["userId"], [1, 2, 3, 4])
            self.assertEqual(item["excludedId"], [1, 2, 3, 4])
            self.assertEqual(item["rp"], {"id": "localhost", "name": "Test"})
            self.assertEqual(item["algorithms"], [{"type": "public-key", "alg": -7}])
            self.assertFalse(item["conditional"])
        self.assertEqual(out["status"], "Cancelled: choose another method")
        self.assertTrue(out["ready"])
        self.assertEqual(out["errorRole"], "alert")
        self.assertEqual(out["name"], "Synthetic phone")
        self.assertTrue(all(item == {"role": "status", "text": "Follow the browser prompt"}
                            for item in out["pending"]))
        self.assertEqual([item["submitted"] for item in out["results"]], [0, 1, 1, 1, 1])
        self.assertEqual(out["results"][0]["status"], "Local creation was not saved in the portal")
        self.assertEqual(out["results"][0]["role"], "alert")
        self.assertTrue(all(item["prompts"] == 1 and item["ready"] for item in out["results"]))
        self.assertTrue(all(item["status"] == "Follow the browser prompt" for item in out["results"][1:]))
        self.assertEqual(len(out["submissions"]), 4)
        self.assertEqual(
            [item["credential"].get("authenticatorAttachment") for item in out["submissions"]],
            ["cross-platform", None, None, "platform"],
        )
        self.assertTrue(all(item["ceremony"] == "synthetic" for item in out["submissions"]))
        self.assertTrue(all(item["name"] == "Synthetic phone" for item in out["submissions"]))
