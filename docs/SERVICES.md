# Service Register – LEG-Platform

The register is in English. The privacy notice and the imprint are maintained by the operator elsewhere and only linked from the portal (D-074). The owner's privacy notice is built from this register (`LEG-Design-Vorgaben/legal/VORGABEN.md`, section 5a, D-056).

**Rule.** Record a service here **before** the feature goes live when it receives or stores personal data. Examples:

* backup service, email or SMS provider
* API that receives user data (address check, payment, bot check)
* error tracker, log service, analytics tool
* font, script or CDN host that a page loads from another server

Each entry gives: service, provider, purpose, data categories, country or region, encryption and key holder, contract basis.

* The developer adds the entry. The security review checks it. The owner confirms it before the page is published.
* Name every new entry in the change description.
* **open** = not verified. The owner or the operator fills it in. Never guess.

## 1. Services that receive personal data

| # | Service | Provider | Purpose | Data categories | Country or region | Encryption and key holder | Contract basis | State |
| - | ------- | -------- | ------- | --------------- | ----------------- | ------------------------- | -------------- | ----- |
| S-1 | Email delivery (Microsoft Graph, `sendMail`; D-006) | Microsoft (Microsoft Entra ID and Exchange Online of the tenant of the operator) | Verification, password reset and security emails | Recipient address, the text of the email (it contains a personal link) | **open** (depends on the tenant of the operator) | Transport: TLS to Microsoft. The certificate with the private key of the application is stored encrypted in the database (key derived from `SECRET_KEY`, D-006); the key holder of the mailbox is the operator | **open** (agreement of the operator with Microsoft) | Implemented; the production tenant is not yet configured |
| S-2 | Hosting of the instance (D-008) | Infomaniak (VPS) is the owner's preference for the instance of the LEG Ittigen; the owner states that Infomaniak operates stage and production (2026-10-16, D-074) | Runs the application, the database and the logs | All data of the portal | **open** | **open** (disk encryption and key holder are not decided) | **open** | Stage reported as set up by the owner (D-064); not inspected |
| S-3 | Backups | **open** | Restore after a loss | All data of the portal | **open** | **open** | **open** | Not decided. Until it is confirmed, no backup is named in the privacy notice |
| S-4 | Building search (find service of the geo.admin.ch API; KONZEPT 3a, D-066) | Federal Office of Topography swisstopo, host `api3.geo.admin.ch` | Determine the federal building identifier (EGID) of an address that the local address directory and the stored answers do not know, and nothing else | **Only the address text entered by a signed-in person:** the postal code, the house number and the street (the town is compared locally and not sent). No name, account, customer number or other data of the person. The server's IP address and the request time reach swisstopo like with any request | Switzerland (federal authority; hosting of the API **open**, not verified) | TLS to the host (certificate verified, no proxy). No key; the API needs none | Public federal geodata service under the terms of use of swisstopo (https://www.swisstopo.admin.ch/en/terms-of-use-free-geodata-and-geoservices); no contract with the operator. **Owner approval 2026-10-19** (KONZEPT 3a) for exactly this purpose. The privacy notice names this request; it is the operator's text (D-022, D-074) and is **open** until the operator has written it | Implemented (`addresses/swisstopo.py`, `addresses/buildings.py`); the owner confirms it before the page that uses it is published |

## 2. Items that send no personal data of the visitors to a third party (recorded so that nobody has to ask)

| # | Item | How it is used | Why no personal data leaves the server |
| - | ---- | -------------- | -------------------------------------- |
| N-1 | **Fonts**: Source Sans 3, Bricolage Grotesque, IBM Plex Mono | Self-hosted files in `leg_platform/static/vendor/fonts/` with their licences (SIL OFL 1.1); loaded by `@font-face` in `portal.css` | A page makes **no request to Google Fonts or any other font host**. The Content Security Policy (`font-src 'self'`, `style-src 'self'`) forbids it technically. The design previews of the owner load Google Fonts; the product does not. |
| N-2 | **Icons and flags**: Tabler Icons, the four flags of the language menu | Files of the own origin (`static/vendor/tabler-icons/`, `static/img/flags/`); `img-src 'self'` | No external image host |
| N-3 | **Scripts and styles**: Tabler (stylesheet), the own scripts | Own origin only (`script-src 'self'`); no CDN, no analytics, no tag manager | No external script host |
| N-4 | **Logos of the instance** | Static files of the instance (`INSTANCE_STATIC_DIRECTORY`), embedded as images | Own origin |
| N-5 | **Address suggestions and check** (swisstopo address directory, D-033, D-049, D-052, D-075) | A **local copy** of the public directory in an own SQLite file on the server (not part of any backup); suggestions query only the local copy | The suggestions and the check of a profile address never send the address to an external service. The copy is imported by the instance (the button, or the daily run of the scheduled tasks) from the public source of the Federal Office of Topography (`data.geo.admin.ch`; public data, no personal data of the visitors). The one request that carries an address is the building search S-4, only for signed-in persons and only for an address the local copy does not know |
| N-6 | **Language and colour mode of the page** | Language: the functional cookie `language` (code only, D-056); colour mode: `sessionStorage` of the browser tab (K3) | The language code is sent to the portal with every request (no personal data); the colour mode is not sent |
| N-7 | **Error tracker, log service, analytics, bot check** | **None are used** in the code of this repository | – |
| N-8 | **Passkeys (D-106)** | The self-hosted SimpleWebAuthn browser helper calls the browser's credential API. Server verification is local (`webauthn`). The portal stores opaque handles, public credentials, counters and backup flags, not private keys or biometrics. | No WebAuthn verification service, CDN or attestation metadata service is contacted. A person may independently choose a synchronising credential provider; the portal neither configures it nor receives its private keys. |
| N-9 | **Transformer stations** (public BKW high-discount map, D-118) | Weekly read of the WMS of the public map server `qgisserver-extern.bkw.ch`, only the map tiles that contain a building of the postal codes of the service area, one request at a time; the cleaned name per building is kept in an own table | Only the map tiles leave the server (the tile coordinates and the names of the map and the layer). No address, person or account is sent; no credentials, no proxy, no `.netrc`. The project owner states verbal permission to use the public map server (recorded 2026-10-25; source: owner statement; no written terms found; D-118) |

## 3. Links to other websites (the portal sends no data)

The footer links "Datenschutz", "Impressum" and the website of the instance open the addresses of the portal settings (D-056) in a new tab with `rel="noopener noreferrer"`. The portal sends no data. The browser connects only after a click.

## 4. How to add an entry

1. Add a row to section 1 before the feature goes live, with all columns. Write **open** where a fact is not verified; never guess.
2. Name the entry in the change description and in `docs/DECISIONS.md`.
3. The security review checks that the entry exists; the owner confirms it before the page is published.
