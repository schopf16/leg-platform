# Third-Party Licences

LEG-Platform itself is licensed under the BSD 3-Clause License (`LICENSE`, `docs/DECISIONS.md`, D-029).

This document lists the licences of the packages LEG-Platform depends on. It is not legal advice and was not reviewed by a lawyer.

## Python Packages

The dependencies are not part of this repository and are not distributed with its source code. They are installed separately by each installation from the Python Package Index (`pyproject.toml`). Their licences apply to them as published by their authors.

Licences as declared in the package metadata (`License-Expression`, `License` or the licence classifier) of the versions installed on the development machine on 2026-10-11 (also checked against PyPI by the main session on the same day):

| Package | Licence | Used for |
| ------- | ------- | -------- |
| Django | BSD-3-Clause | Web framework |
| django-axes | MIT | Login abuse protection (D-017) |
| django-otp | Unlicense | Two-factor authentication (D-004) |
| cryptography | Apache-2.0 OR BSD-3-Clause | Encryption of the Microsoft Graph client secret, keyed hashes (D-006, D-023) |
| mysqlclient | GPL-2.0-or-later | MySQL database driver |
| msal | MIT | Microsoft Graph token acquisition (D-026) |
| segno | BSD-3-Clause (metadata: classifier "BSD License"; the bundled licence text is the 3-clause BSD licence) | QR code for TOTP enrolment |

Transitive dependencies:

| Package | Licence | Required by |
| ------- | ------- | ----------- |
| asgiref | BSD-3-Clause | Django |
| sqlparse | BSD-3-Clause (metadata: classifier "BSD License"; the bundled licence text is the 3-clause BSD licence) | Django |
| tzdata | Apache-2.0 | Django (only on Windows) |
| requests | Apache-2.0 | msal |
| PyJWT | MIT | msal |
| urllib3 | MIT | requests |
| charset-normalizer | MIT | requests |
| idna | BSD-3-Clause | requests |
| certifi | MPL-2.0 | requests |
| cffi | MIT-0 | cryptography |
| pycparser | BSD-3-Clause | cffi |

Notes:

* `mysqlclient` is licensed under GPL-2.0-or-later. An installation that combines LEG-Platform with `mysqlclient` and the Apache-2.0 components uses `mysqlclient` under the terms of GPL-3.0 or later: the Apache License 2.0 is "compatible with version 3 of the GNU GPL" but "not compatible with GPL version 2" (Free Software Foundation, "Various Licenses and Comments about Them"). The BSD 3-Clause License of LEG-Platform is "compatible with the GNU GPL" (same source, "Modified BSD license").
* The Free Software Foundation lists the Expat (MIT) licence and the Unlicense as compatible with the GNU GPL; for MPL-2.0, "Section 3.3 provides indirect compatibility" with the GNU GPL (same source).
* The list is updated when a dependency is added or replaced. Licences of new versions are checked when the minimum versions are raised.

## Bundled Assets

No third-party assets are bundled yet. Tabler and Tabler Icons (MIT) and the fonts (SIL Open Font License 1.1) are planned to be served by the instance itself (D-024, `docs/SECURITY.md`, section 6). When they are added to the repository, their licence texts (and those of any library they bundle) are added next to them and listed here, because both licences require the copyright notice and licence to be included with copies.

## Sources

* Installed package metadata in `.venv` (Python 3.14.8, newest packages), checked on 2026-10-11 with `importlib.metadata`, and the licence files in the packages' `.dist-info` directories (segno, sqlparse)
* https://pypi.org/ (package pages of the packages listed above)
* https://www.gnu.org/licenses/license-list.en.html (checked on 2026-10-11)
* https://spdx.org/licenses/ (licence identifiers)
