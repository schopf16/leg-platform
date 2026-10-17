"""The single definition of email address normalisation (D-021, D-031, D-034).

Used for the canonical stored address (D-035), the normalised unique column and every lookup and
per-address counter. The normalised value is compared exactly (collation utf8mb4_bin). Account
addresses have an ASCII local part (D-035).
"""

import unicodedata

import idna


def normalize_email_address(address):
    """Return an NFKC/lower-case address with an IDNA ASCII domain.

    NFKC is what Django applies to usernames (folds compatibility variants, composes accents, keeps
    accents). The domain uses UTS #46 mapping, so Unicode and Punycode spellings compare equally.
    Invalid addresses remain normalised Unicode for validation to reject. None for None or an
    address that is empty after stripping.
    """
    if address is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(address)).strip().lower()
    if not normalized:
        return None
    if normalized.count("@") == 1:
        local_part, domain = normalized.rsplit("@", 1)
        if len(domain) > 253:
            return normalized
        try:
            domain = idna.encode(domain, uts46=True, std3_rules=True, transitional=False).decode(
                "ascii"
            )
        except idna.IDNAError:
            pass
        else:
            normalized = f"{local_part}@{domain.lower()}"
    return normalized
