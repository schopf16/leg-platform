# Third-Party Licences

LEG-Platform itself is licensed under the BSD 3-Clause License (`LICENSE`, `docs/DECISIONS.md`, D-029).

This document lists the licences of the packages LEG-Platform depends on. It is not legal advice and was not reviewed by a lawyer.

## Python Packages

The dependencies are not part of this repository and are not distributed with its source code. They are installed separately by each installation from the Python Package Index (`pyproject.toml`). Their licences apply to them as published by their authors.

Licences as declared in the package metadata (`License-Expression`, `License` or the licence classifier) of the versions installed on the development machine on 2026-10-11 (also checked against PyPI by the main session on the same day); `idna` versions 3.15 and 3.20 were checked against PyPI on 2026-10-12:

| Package | Licence | Used for |
| ------- | ------- | -------- |
| Django | BSD-3-Clause | Web framework |
| django-axes | MIT | Login abuse protection (D-017) |
| django-ipware | MIT | Trusted-proxy-aware client IP resolution for login abuse protection (D-017) |
| django-otp | Unlicense | Two-factor authentication (D-004) |
| cryptography | Apache-2.0 OR BSD-3-Clause | Encryption of the Microsoft Graph certificate private key, second factor secrets, keyed hashes (D-006, D-023, D-040) |
| mysqlclient | GPL-2.0-or-later | Database driver of Django's MySQL backend, used for MariaDB (D-030); see the note on MariaDB Connector/C below |
| msal | MIT | Microsoft Graph token acquisition (D-026) |
| segno | BSD-3-Clause (metadata: classifier "BSD License"; the bundled licence text is the 3-clause BSD licence) | QR code for TOTP enrolment |
| django-countries | MIT | Country field and country names of the profile (D-034) |
| django-phonenumber-field | MIT | Validation and E.164 storage of the phone number (D-034) |
| phonenumberslite | Apache-2.0 | Phone number metadata for django-phonenumber-field (D-034) |
| idna | BSD-3-Clause | UTS #46 canonicalisation of internationalised email domains (D-034) |

Transitive dependencies:

| Package | Licence | Required by |
| ------- | ------- | ----------- |
| asgiref | BSD-3-Clause | Django, django-countries |
| sqlparse | BSD-3-Clause (metadata: classifier "BSD License"; the bundled licence text is the 3-clause BSD licence) | Django |
| tzdata | Apache-2.0 | Django (only on Windows) |
| requests | Apache-2.0 | msal |
| PyJWT | MIT | msal |
| urllib3 | MIT | requests |
| charset-normalizer | MIT | requests |
| certifi | MPL-2.0 | requests |
| cffi | MIT-0 | cryptography |
| pycparser | BSD-3-Clause | cffi |
| typing_extensions | PSF-2.0 | django-countries |
| python-ipware | MIT | django-ipware |

Notes:

* `mysqlclient` is licensed under GPL-2.0-or-later. According to the Free Software Foundation's list ("Various Licenses and Comments about Them"), the Apache License 2.0 is "compatible with version 3 of the GNU GPL" but "not compatible with GPL version 2", and the Modified BSD license (the licence of LEG-Platform) is "compatible with the GNU GPL". From these statements it follows that a combined work containing `mysqlclient` (GPL-2.0-or-later) and Apache-2.0 code (`requests`, `tzdata`, `phonenumberslite`, and `cryptography` if not used under its BSD option) can only be distributed under GPL-3.0-or-later. The GPL's conditions apply when such a work is conveyed (distributed) to others: "You may make, run and propagate covered works that you do not convey, without conditions so long as your license otherwise remains in force" (GPLv3, section 2). This repository conveys only the source code of LEG-Platform, not its dependencies.
* The Windows wheels of `mysqlclient` 2.3.0 contain the MariaDB Connector/C client library statically linked into the extension module `MySQLdb/_mysql.cp314-win_amd64.pyd` (its strings name `libmariadb` and the source path `mariadb-connector-src`; checked in `.venv` on 2026-10-11); the wheel's `licenses` directory contains only the `mysqlclient` licence. MariaDB Connector/C is licensed under LGPL-2.1 ("The client library is LGPL licensed", https://github.com/mariadb-corporation/mariadb-connector-c, file `COPYING.LIB`), which the Free Software Foundation lists as "compatible with GPLv2 and GPLv3". On Linux (continuous integration), `mysqlclient` is built from source against the system's MySQL client library (`default-libmysqlclient-dev`), which is not bundled.
* The Free Software Foundation lists the Expat (MIT) licence and the Unlicense as compatible with the GNU GPL; for MPL-2.0, "Section 3.3 provides indirect compatibility" with the GNU GPL (same source).
* MariaDB Server (GPL-2.0, file `COPYING` in https://github.com/MariaDB/server) is not part of this repository and is not distributed with it. It is installed separately: locally with the Chocolatey package `mariadb.install`, in continuous integration as the official Docker image `mariadb`, and on servers by the operator (D-030).
* The list is updated when a dependency is added or replaced. Licences of new versions are checked when the minimum versions are raised.

## Bundled Assets

The following third-party assets are part of this repository, below `leg_platform/static/vendor/`, each with its licence text next to it (the licences require the copyright notice and licence to be included with copies):

| Asset | Version and source | Licence | Used for |
| ----- | ------------------ | ------- | -------- |
| Tabler (`css/tabler.min.css`) | 1.6.1, `@tabler/core` from the npm registry; the tarball matched the registry's SHA-512 integrity value; SHA-256 of the file: `370A2D4E608AE518351D10528FC7DBE19B354AE5062C51B2A9CB294850D65403` | MIT (`tabler/LICENSE`) | Application interface (D-003, D-024) |
| Source Sans 3 (`fonts/SourceSans3.ttf`) | repository `google/fonts`, `ofl/sourcesans3`, unmodified | SIL Open Font License 1.1, Reserved Font Name "Source" (`fonts/OFL-SourceSans3.txt`) | Text typeface (D-024) |
| Bricolage Grotesque (`fonts/BricolageGrotesque.ttf`) | repository `google/fonts`, `ofl/bricolagegrotesque`, unmodified | SIL Open Font License 1.1 (`fonts/OFL-BricolageGrotesque.txt`) | Headings in the register "Warm" (D-024) |
| IBM Plex Mono (`fonts/IBMPlexMono-Regular.ttf`) | repository `google/fonts`, `ofl/ibmplexmono`, unmodified | SIL Open Font License 1.1, Reserved Font Name "Plex" (`fonts/OFL-IBMPlexMono.txt`) | Identifiers such as the customer number (D-024) |
| Tabler Icons (`tabler-icons/lock.svg`, inlined in `leg_platform/templates/icons/lock.html`) | v3.49.0, repository `tabler/tabler-icons`, `icons/outline/lock.svg` | MIT (`tabler-icons/LICENSE`) | Icons (D-024); further icons are added with the pages that use them |

The fonts are used unmodified, as `docs/DESIGN.md`, section 4, requires (OFL condition 3 forbids a modified version under a Reserved Font Name). The own file `leg_platform/static/img/select-arrow.svg` replaces a `data:` image of Tabler's stylesheet (D-037) and is part of this project's BSD 3-Clause licence.

## Sources

* Installed package metadata in `.venv` (Python 3.14.8, newest packages), checked on 2026-10-11 with `importlib.metadata`, and the licence files in the packages' `.dist-info` directories (segno, sqlparse); for django-countries 9.1.0, django-phonenumber-field 8.5.0, phonenumberslite 9.0.41 and typing_extensions checked on 2026-10-12 with `importlib.metadata` and against https://pypi.org/pypi/<package>/json; idna 3.15 and 3.20 metadata checked on 2026-10-12
* https://pypi.org/ (package pages of the packages listed above)
* https://www.gnu.org/licenses/license-list.en.html (checked on 2026-10-11)
* https://www.gnu.org/licenses/gpl-3.0.en.html (section 0, definitions of "propagate" and "convey"; section 2, "Basic Permissions"; checked on 2026-10-11)
* https://github.com/mariadb-corporation/mariadb-connector-c (licence of MariaDB Connector/C, checked on 2026-10-11)
* https://github.com/MariaDB/server (licence of MariaDB Server, `COPYING`, GPL-2.0; checked on 2026-10-11)
* https://spdx.org/licenses/ (licence identifiers)
