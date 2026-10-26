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
| webauthn | BSD-3-Clause | WebAuthn verification (D-106); 3.0.1 metadata and version-tagged source checked 2026-10-22 |
| cryptography | Apache-2.0 OR BSD-3-Clause | Encryption of the Microsoft Graph certificate private key, second factor secrets, keyed hashes (D-006, D-023, D-040) |
| mysqlclient | GPL-2.0-or-later | Database driver of Django's MySQL backend, used for MariaDB (D-030); see the note on MariaDB Connector/C below |
| msal | MIT | Microsoft Graph token acquisition (D-026) |
| segno | BSD-3-Clause (metadata: classifier "BSD License"; the bundled licence text is the 3-clause BSD licence) | QR code for TOTP enrolment |
| django-countries | MIT | Country field and country names of the profile (D-034) |
| django-phonenumber-field | MIT | Validation and E.164 storage of the phone number (D-034) |
| phonenumberslite | Apache-2.0 | Phone number metadata for django-phonenumber-field (D-034) |
| idna | BSD-3-Clause | UTS #46 canonicalisation of internationalised email domains (D-034) |
| reportlab | BSD-3-Clause (metadata: "BSD license"; the bundled `LICENSE` is the 3-clause BSD licence of ReportLab Inc.) | PDF printing of the lists of the management area (D-082, D-083); checked on 2026-10-17 (version 5.0.1, PyPI metadata and the installed `LICENSE`) |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (metadata of 2.5.4) | Pixel arrays of the transformer station scan (D-118; server only, `requirements/transformers.txt`) |
| scipy | BSD-3-Clause (classifier "BSD License"; version 1.18.1) | Dot detection and nearest-point matching of the transformer station scan (D-118; server only) |
| opencv-python-headless | Apache-2.0 (metadata of 5.0.0.93) | Image handling of the transformer station scan (D-118; server only) |
| rapidocr-onnxruntime | Apache-2.0 (metadata of 1.2.3) | Reading the labels of the map in the transformer station scan (D-118; server only) |
| python-stdnum | LGPL-2.1-or-later (metadata: `License: LGPL`, classifier "GNU Lesser General Public License v2 or later (LGPLv2+)"; the bundled `licenses/COPYING` is the LGPL 2.1) | Checksum and country structure of the IBAN of a person (D-087); checked on 2026-10-17 (version 2.2, PyPI metadata and the installed `COPYING`); no dependencies; used unmodified as a library (imported, not copied or changed) |

Transitive dependencies:

| Package | Licence | Required by |
| ------- | ------- | ----------- |
| asgiref | BSD-3-Clause | Django, django-countries |
| sqlparse | BSD-3-Clause (metadata: classifier "BSD License"; the bundled licence text is the 3-clause BSD licence) | Django |
| tzdata | Apache-2.0 | Django (only on Windows) |
| requests | Apache-2.0 | msal |
| PyJWT | MIT | msal |
| urllib3 | MIT | requests |
| charset-normalizer | MIT | requests, reportlab |
| Pillow | MIT-CMU (`License-Expression` of 13.0.0) | reportlab (declared dependency `pillow>=9.0.0`; LEG-Platform draws no image) |
| certifi | MPL-2.0 | requests |
| onnxruntime | MIT (metadata of 1.31.0) | rapidocr-onnxruntime (D-118; server only) |
| cffi | MIT-0 | cryptography |
| pycparser | BSD-3-Clause | cffi |
| typing_extensions | PSF-2.0 | django-countries |
| python-ipware | MIT | django-ipware |
| pyasn1, pyasn1-modules | BSD-2-Clause | webauthn |
| cbor2 | MIT | webauthn |
| pyOpenSSL | Apache-2.0 | webauthn |

Notes:

* `mysqlclient` is licensed under GPL-2.0-or-later. According to the Free Software Foundation's list ("Various Licenses and Comments about Them"), the Apache License 2.0 is "compatible with version 3 of the GNU GPL" but "not compatible with GPL version 2", and the Modified BSD license (the licence of LEG-Platform) is "compatible with the GNU GPL". From these statements it follows that a combined work containing `mysqlclient` (GPL-2.0-or-later) and Apache-2.0 code (`requests`, `tzdata`, `phonenumberslite`, and `cryptography` if not used under its BSD option) can only be distributed under GPL-3.0-or-later. The GPL's conditions apply when such a work is conveyed (distributed) to others: "You may make, run and propagate covered works that you do not convey, without conditions so long as your license otherwise remains in force" (GPLv3, section 2). This repository conveys only the source code of LEG-Platform, not its dependencies.
* The Windows wheels of `mysqlclient` 2.3.0 contain the MariaDB Connector/C client library statically linked into the extension module `MySQLdb/_mysql.cp314-win_amd64.pyd` (its strings name `libmariadb` and the source path `mariadb-connector-src`; checked in `.venv` on 2026-10-11); the wheel's `licenses` directory contains only the `mysqlclient` licence. MariaDB Connector/C is licensed under LGPL-2.1 ("The client library is LGPL licensed", https://github.com/mariadb-corporation/mariadb-connector-c, file `COPYING.LIB`), which the Free Software Foundation lists as "compatible with GPLv2 and GPLv3". On Linux (continuous integration), `mysqlclient` is built from source against the system's MySQL client library (`default-libmysqlclient-dev`), which is not bundled.
* The Free Software Foundation lists the Expat (MIT) licence and the Unlicense as compatible with the GNU GPL; for MPL-2.0, "Section 3.3 provides indirect compatibility" with the GNU GPL (same source).
* `python-stdnum` is licensed under the LGPL 2.1 or later, which the Free Software Foundation lists as compatible with GPLv2 and GPLv3 (same source). LEG-Platform only imports it; it is neither copied into the repository nor modified, so that a user can replace the library by another version (the condition of the LGPL for a work that uses a library). Like the other dependencies it is not distributed with this repository.
* MariaDB Server (GPL-2.0, file `COPYING` in https://github.com/MariaDB/server) is not part of this repository and is not distributed with it. It is installed separately: locally with the Chocolatey package `mariadb.install`, in continuous integration as the official Docker image `mariadb`, and on servers by the operator (D-030).
* The list is updated when a dependency is added or replaced. Licences of new versions are checked when the minimum versions are raised.

## Bundled Assets

The following third-party assets are part of this repository, below `leg_platform/static/vendor/`, each with its licence text next to it (the licences require the copyright notice and licence to be included with copies):

| Asset | Version and source | Licence | Used for |
| ----- | ------------------ | ------- | -------- |
| SimpleWebAuthn Browser (`simplewebauthn/index.umd.min.js`) | `@simplewebauthn/browser` 14.0.0, unmodified UMD bundle; SHA-256 `29bb0699b45bc39a350d992a13bfc1a4d40d89531390581e1f57f1142c6e221d`, matches published package metadata; upstream tag `v14.0.0` | MIT (`simplewebauthn/LICENSE.md`) | Browser WebAuthn JSON conversion and ceremonies (D-106), no CDN at runtime |
| Tabler (`css/tabler.min.css`) | 1.6.1, `@tabler/core` from the npm registry; the tarball matched the registry's SHA-512 integrity value; SHA-256 of the file: `370A2D4E608AE518351D10528FC7DBE19B354AE5062C51B2A9CB294850D65403` | MIT (`tabler/LICENSE`) | Application interface (D-003, D-024) |
| Leaflet (`leaflet/leaflet.js`, `leaflet/leaflet.css`) | 1.9.4, `leaflet` from the npm registry; the tarball matched the registry's SHA-512 integrity value; SHA-256 of `leaflet.js`: `DB49D009C841F5CA34A888C96511AE936FD9F5533E90D8B2C4D57596F4E5641A`, of `leaflet.css`: `A7837102824184820DFA198D1EBCD109FF6D0FF9A2672A074B9A1B4D147D04C6` | BSD 2-Clause (`leaflet/LICENSE.md`) | Map of the page "Nachbarschaft" (D-120); no CDN at runtime |
| Source Sans 3 (`fonts/SourceSans3.ttf`) | repository `google/fonts`, `ofl/sourcesans3`, unmodified | SIL Open Font License 1.1, Reserved Font Name "Source" (`fonts/OFL-SourceSans3.txt`) | Text typeface (D-024) |
| Bricolage Grotesque (`fonts/BricolageGrotesque.ttf`) | repository `google/fonts`, `ofl/bricolagegrotesque`, unmodified | SIL Open Font License 1.1 (`fonts/OFL-BricolageGrotesque.txt`) | Headings in the register "Warm" (D-024) |
| IBM Plex Mono (`fonts/IBMPlexMono-Regular.ttf`) | repository `google/fonts`, `ofl/ibmplexmono`, unmodified | SIL Open Font License 1.1, Reserved Font Name "Plex" (`fonts/OFL-IBMPlexMono.txt`) | Identifiers such as the customer number (D-024) |
| Tabler Icons (`tabler-icons/lock.svg`, inlined in `leg_platform/templates/icons/lock.html`; `tabler-icons/circle-check.svg`, `alert-triangle.svg`, `info-circle.svg`, `alert-circle.svg`, used as CSS masks for the icons of the alerts, D-054; `external-link.svg`, `arrow-left.svg`, inlined as symbols `i-external` and `i-arrow-left` in `leg_platform/templates/_frame_icons.html`, D-113) | v3.49.0, repository `tabler/tabler-icons`, `icons/outline/<name>.svg`, unmodified | MIT (`tabler-icons/LICENSE`) | Icons (D-024); further icons are added with the pages that use them |
| DejaVu Sans Condensed (`leg_platform/management/printing/fonts/DejaVuSansCondensed.ttf` and `DejaVuSansCondensed-Bold.ttf`) | DejaVu fonts 2.37, release archive `dejavu-fonts-ttf-2.37.zip` of https://github.com/dejavu-fonts/dejavu-fonts (SHA-256 of the archive `7576310B219E04159D35FF61DD4A4EC4CDBA4F35C00E002A136F00E96A908B0A`; of the files: `8550CD5CA1ACB65A8FC7877C46939CD0B4D909F8E7BC1E24716873D918C5E549` and `38098D0B8EDD8430A0F53FB2831B5D36037563A998A620D3C87A4D7591766D20`), unmodified | Bitstream Vera Fonts Copyright (permissive; the changes of DejaVu are public domain; `printing/fonts/LICENSE-DejaVu.txt`) | Typeface of the PDF printouts of the lists (D-083); read from the package directory by ReportLab, never served |

The fonts are used unmodified, as `docs/DESIGN.md`, section 4, requires (OFL condition 3 forbids a modified version under a Reserved Font Name). The own files `leg_platform/static/img/select-arrow.svg`, `check.svg` and `radio-dot.svg` replace `data:` images of Tabler's stylesheet (D-037, D-054) and are part of this project's BSD 3-Clause licence. The DejaVu fonts are no web fonts and are not part of the pages' design (D-083); the Bitstream Vera licence allows bundling them with a larger software package, as long as no copy of a typeface is sold by itself, and requires its notice with the copies.

## Sources

* Installed package metadata in `.venv` (Python 3.14.8, newest packages), checked on 2026-10-11 with `importlib.metadata`, and the licence files in the packages' `.dist-info` directories (segno, sqlparse); for django-countries 9.1.0, django-phonenumber-field 8.5.0, phonenumberslite 9.0.41 and typing_extensions checked on 2026-10-12 with `importlib.metadata` and against https://pypi.org/pypi/<package>/json; idna 3.15 and 3.20 metadata checked on 2026-10-12
* https://pypi.org/ (package pages of the packages listed above)
* https://pypi.org/pypi/reportlab/json and https://api.osv.dev/v1/query (package `reportlab`, PyPI ecosystem), read on 2026-10-17: version 5.0.1 (2026-08-20), Python 3.9 to 4 (classifiers 3.10 to 3.14), BSD licence, requires `pillow>=9.0.0` and `charset-normalizer`; four known advisories, all fixed in 3.6.13 or earlier (CVE-2019-17626, CVE-2019-19450, CVE-2020-28463, CVE-2023-33733)
* https://www.gnu.org/licenses/license-list.en.html (checked on 2026-10-11)
* https://www.gnu.org/licenses/gpl-3.0.en.html (section 0, definitions of "propagate" and "convey"; section 2, "Basic Permissions"; checked on 2026-10-11)
* https://github.com/mariadb-corporation/mariadb-connector-c (licence of MariaDB Connector/C, checked on 2026-10-11)
* https://github.com/MariaDB/server (licence of MariaDB Server, `COPYING`, GPL-2.0; checked on 2026-10-11)
* https://spdx.org/licenses/ (licence identifiers)
