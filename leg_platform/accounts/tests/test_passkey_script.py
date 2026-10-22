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
    def test_modes_reach_native_options_and_cancellation_never_falls_back(self):
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
<form data-passkey-register data-options="/options/" data-error="Cancelled: choose another method">
  <input name="csrfmiddlewaretoken" value="synthetic-csrf">
  <input name="name" value="Synthetic phone">
  <button name="mode" value="another_device">Another device</button>
  <button name="mode" value="this_device">This device</button>
  <p role="status" data-passkey-status></p>
</form><pre id="out"></pre>
<script>
const calls = [], native = [], out = {};
window.fetch = async (url, request) => {
  const data = JSON.parse(request.body);
  calls.push({url, data, csrf: request.headers["X-CSRFToken"], credentials: request.credentials});
  if (url !== "/options/") throw new Error("Unexpected credential submission");
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
Object.defineProperty(navigator.credentials, "create", {value: async ({publicKey}) => {
  native.push({
    hints: publicKey.hints, selection: publicKey.authenticatorSelection,
    binaryChallenge: publicKey.challenge instanceof ArrayBuffer,
    binaryExcludedId: publicKey.excludeCredentials[0].id instanceof ArrayBuffer
  });
  throw new DOMException("Synthetic cancellation", "NotAllowedError");
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
    for (const button of buttons) {
      form.requestSubmit(button);
      await wait();
    }
    buttons[0].value = "untrusted";
    form.requestSubmit(buttons[0]);
    await wait();
    form.dispatchEvent(new SubmitEvent("submit", {bubbles: true, cancelable: true}));
    await wait();
    out.calls = calls;
    out.native = native;
    out.status = form.querySelector("[data-passkey-status]").textContent;
    out.ready = !form.dataset.passkeyBusy && Array.from(buttons).every(b => !b.disabled);
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
        self.assertEqual(out["status"], "Cancelled: choose another method")
        self.assertTrue(out["ready"])
