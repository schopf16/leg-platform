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
| mysqlclient | GPL-2.0-or-later | Database driver of Django's MySQL backend, used for MariaDB (D-030); see the note on MariaDB Connector/C below |
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

* `mysqlclient` is licensed under GPL-2.0-or-later. According to the Free Software Foundation's list ("Various Licenses and Comments about Them"), the Apache License 2.0 is "compatible with version 3 of the GNU GPL" but "not compatible with GPL version 2", and the Modified BSD license (the licence of LEG-Platform) is "compatible with the GNU GPL". From these statements it follows that a combined work containing `mysqlclient` (GPL-2.0-or-later) and Apache-2.0 code (`requests`, `tzdata`, and `cryptography` if not used under its BSD option) can only be distributed under GPL-3.0-or-later. The GPL's conditions apply when such a work is conveyed (distributed) to others: "You may make, run and propagate covered works that you do not convey, without conditions so long as your license otherwise remains in force" (GPLv3, section 2). This repository conveys only the source code of LEG-Platform, not its dependencies.
* The Windows wheels of `mysqlclient` 2.3.0 contain the MariaDB Connector/C client library statically linked into the extension module `MySQLdb/_mysql.cp314-win_amd64.pyd` (its strings name `libmariadb` and the source path `mariadb-connector-src`; checked in `.venv` on 2026-10-11); the wheel's `licenses` directory contains only the `mysqlclient` licence. MariaDB Connector/C is licensed under LGPL-2.1 ("The client library is LGPL licensed", https://github.com/mariadb-corporation/mariadb-connector-c, file `COPYING.LIB`), which the Free Software Foundation lists as "compatible with GPLv2 and GPLv3". On Linux (continuous integration), `mysqlclient` is built from source against the system's MySQL client library (`default-libmysqlclient-dev`), which is not bundled.
* The Free Software Foundation lists the Expat (MIT) licence and the Unlicense as compatible with the GNU GPL; for MPL-2.0, "Section 3.3 provides indirect compatibility" with the GNU GPL (same source).
* MariaDB Server (GPL-2.0, file `COPYING` in https://github.com/MariaDB/server) is not part of this repository and is not distributed with it. It is installed separately: locally with the Chocolatey package `mariadb.install`, in continuous integration as the official Docker image `mariadb`, and on servers by the operator (D-030).
* The list is updated when a dependency is added or replaced. Licences of new versions are checked when the minimum versions are raised.

## Bundled Assets

No third-party assets are bundled yet. Tabler and Tabler Icons (MIT) and the fonts (SIL Open Font License 1.1) are planned to be served by the instance itself (D-024, `docs/SECURITY.md`, section 6). When they are added to the repository, their licence texts (and those of any library they bundle) are added next to them and listed here, because both licences require the copyright notice and licence to be included with copies.

## Sources

* Installed package metadata in `.venv` (Python 3.14.8, newest packages), checked on 2026-10-11 with `importlib.metadata`, and the licence files in the packages' `.dist-info` directories (segno, sqlparse)
* https://pypi.org/ (package pages of the packages listed above)
* https://www.gnu.org/licenses/license-list.en.html (checked on 2026-10-11)
* https://www.gnu.org/licenses/gpl-3.0.en.html (section 0, definitions of "propagate" and "convey"; section 2, "Basic Permissions"; checked on 2026-10-11)
* https://github.com/mariadb-corporation/mariadb-connector-c (licence of MariaDB Connector/C, checked on 2026-10-11)
* https://github.com/MariaDB/server (licence of MariaDB Server, `COPYING`, GPL-2.0; checked on 2026-10-11)
* https://spdx.org/licenses/ (licence identifiers)
