"""Checks of the repository content (docs/DECISIONS.md, D-028).

Automated tests and examples use only email addresses under domains reserved for
documentation and testing (RFC 2606: example.com, example.net, example.org and their
subdomains, and the top-level domains .test and .invalid). No real personal email address is
written into the repository.
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
RESERVED_TOP_LEVEL_DOMAINS = ("test", "invalid")

# Text files of the repository; generated, local and third-party directories are skipped.
# Secret files such as .env have no suffix in this list and are never read (AGENTS.md,
# section 8); .env.example is scanned.
SCANNED_SUFFIXES = {
    ".py", ".md", ".txt", ".toml", ".yml", ".yaml", ".ps1", ".html", ".po", ".cfg", ".ini",
    ".example", ".json",
}
SCANNED_NAMES = {"LICENSE"}
SKIPPED_DIRECTORIES = {
    ".git", ".venv", "venv", "node_modules", "staticfiles", "__pycache__", "build", "dist",
}


def is_reserved_domain(domain):
    domain = domain.lower().rstrip(".")
    if domain.rsplit(".", 1)[-1] in RESERVED_TOP_LEVEL_DOMAINS:
        return True
    return any(domain == reserved or domain.endswith("." + reserved) for reserved in RESERVED_DOMAINS)


def unreserved_addresses(text):
    return [match.group(0) for match in EMAIL_ADDRESS.finditer(text) if not is_reserved_domain(match.group(1))]


def repository_files(root):
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIPPED_DIRECTORIES or part.endswith(".egg-info") for part in relative.parts):
            continue
        if path.is_file() and (path.suffix in SCANNED_SUFFIXES or path.name in SCANNED_NAMES):
            yield path


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
                self.assertEqual(unreserved_addresses(address), [])

    def test_other_domains_are_reported(self):
        # Built at runtime, so that this file itself contains no such address.
        for domain in ("example.org.attacker", "notexample.org", "example.ch", "mail.example"):
            address = "someone" + "@" + domain
            with self.subTest(address=address):
                self.assertEqual(unreserved_addresses(address), [address])

    def test_repository_uses_only_reserved_email_domains(self):
        root = Path(settings.BASE_DIR)
        findings = []
        scanned = 0
        for path in repository_files(root):
            scanned += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            for address in unreserved_addresses(text):
                findings.append(f"{path.relative_to(root)}: {address}")
        self.assertGreater(scanned, 10)
        self.assertEqual(findings, [])
