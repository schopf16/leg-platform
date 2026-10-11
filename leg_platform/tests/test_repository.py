"""Checks of the repository content (docs/DECISIONS.md, D-028).

Automated tests and examples use only email addresses under domains reserved for
documentation and testing (RFC 2606: example.com, example.net, example.org and their
subdomains, and the top-level domains .test and .invalid). No real personal email address is
written into the repository.

Only files tracked by Git are read (``git ls-files``), never untracked or ignored files such
as ``.env`` (AGENTS.md, section 8). Findings name only the file and line, not the address.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
RESERVED_TOP_LEVEL_DOMAINS = ("test", "invalid")


def is_reserved_domain(domain):
    domain = domain.lower().rstrip(".")
    if domain.rsplit(".", 1)[-1] in RESERVED_TOP_LEVEL_DOMAINS:
        return True
    return any(domain == reserved or domain.endswith("." + reserved) for reserved in RESERVED_DOMAINS)


def unreserved_address_lines(text):
    """Return the line numbers (1-based) that contain an address outside the reserved domains."""
    lines = []
    for number, line in enumerate(text.splitlines(), 1):
        if any(not is_reserved_domain(match.group(1)) for match in EMAIL_ADDRESS.finditer(line)):
            lines.append(number)
    return lines


def decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # Latin-1 maps every byte, so that an address is still found in other encodings.
        return data.decode("latin-1")


def tracked_files(root):
    """Return the files tracked by Git, or None if Git is not available."""
    git = shutil.which("git")
    if git is None:
        return None
    result = subprocess.run(
        [git, "ls-files", "-z"], cwd=root, capture_output=True, check=False, timeout=60
    )
    if result.returncode != 0:
        return None
    names = [name for name in result.stdout.decode("utf-8").split("\0") if name]
    return [root / name for name in names]


class ReservedEmailDomainTests(SimpleTestCase):
    def test_reserved_domains_are_recognised(self):
        for address in (
            "user@example.org",
            "user@portal.example.com",
            "User@Example.NET",
            "user@mail.test",
            "user@host.invalid",
        ):
            with self.subTest(address=address):
                self.assertEqual(unreserved_address_lines(address), [])

    def test_other_domains_are_reported(self):
        # Built at runtime, so that this file itself contains no such address.
        for domain in ("example.org.attacker", "notexample.org", "example.ch", "mail.example"):
            text = "first line\nsomeone" + "@" + domain + "\n"
            with self.subTest(domain=domain):
                self.assertEqual(unreserved_address_lines(text), [2])

    def test_decode_falls_back_for_other_encodings(self):
        self.assertEqual(decode(b"caf\xe9 user@example.org"), "caf\xe9 user@example.org")

    def test_repository_uses_only_reserved_email_domains(self):
        root = Path(settings.BASE_DIR)
        files = tracked_files(root)
        if files is None:
            # Without Git, tracked and ignored files cannot be told apart; ignored files (e.g.
            # .env) must not be read. In continuous integration the check must run, so it
            # fails there; elsewhere it is skipped.
            message = "Git is not available; the repository scan needs 'git ls-files'."
            if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
                self.fail(message)
            self.skipTest(message)
        findings = []
        scanned = 0
        for path in files:
            if not path.is_file():
                continue
            data = path.read_bytes()
            if b"\0" in data:
                continue  # binary file
            scanned += 1
            for number in unreserved_address_lines(decode(data)):
                findings.append(f"{path.relative_to(root).as_posix()}:{number}")
        self.assertGreater(scanned, 10)
        self.assertEqual(findings, [], "Email addresses outside the reserved domains")
