# Service Register – LEG-Platform

Status: 2026-10-14. Created by the developer as required by `LEG-Design-Vorgaben/legal/VORGABEN.md`, section 5a (D-056). The privacy notice of the owner is built from this register. The register is in English; the privacy notice and the imprint are written and maintained by the operator elsewhere and only linked from the portal (D-074).

**Rule.** Whenever the software uses a service that receives or stores personal data, it is recorded here **before** the feature goes live: a backup service, an email or SMS provider, an API that receives user data (address check, payment, bot check), an error tracker, log service or analytics tool, and a font, script or CDN host that a page loads from another server. Each entry gives: service, provider, purpose, data categories, country or region, encryption and key holder, contract basis. The developer adds the entry, the security review checks that it is there, and the owner confirms the entry before the page is published. Every new entry is also named in the change description.

Entries marked **open** are not verified by the developer: the owner or the operator of the instance has to fill them in. The developer did not guess them.

## 1. Services that receive personal data

| # | Service | Provider | Purpose | Data categories | Country or region | Encryption and key holder | Contract basis | State |
| - | ------- | -------- | ------- | --------------- | ----------------- | ------------------------- | -------------- | ----- |
| S-1 | Email delivery (Microsoft Graph, `sendMail`; D-006) | Microsoft (Microsoft Entra ID and Exchange Online of the tenant of the operator) | Verification, password reset and security emails | Recipient address, the text of the email (it contains a personal link) | **open** (depends on the tenant of the operator) | Transport: TLS to Microsoft. The certificate with the private key of the application is stored encrypted in the database (key derived from `SECRET_KEY`, D-006); the key holder of the mailbox is the operator | **open** (agreement of the operator with Microsoft) | Implemented; the production tenant is not yet configured |
| S-2 | Hosting of the instance (D-008) | Infomaniak (VPS) is the owner's preference for the instance of the LEG Ittigen; the owner states that Infomaniak operates stage and production (2026-10-16, D-074) | Runs the application, the database and the logs | All data of the portal | **open** | **open** (disk encryption and key holder are not decided) | **open** | Stage reported as set up by the owner (D-064); not inspected |
| S-3 | Backups | **open** | Restore after a loss | All data of the portal | **open** | **open** | **open** | Not decided. Until it is confirmed, no backup is named in the privacy notice |

## 2. Items that send no personal data of the visitors to a third party (recorded so that nobody has to ask)

| # | Item | How it is used | Why no personal data leaves the server |
| - | ---- | -------------- | -------------------------------------- |
| N-1 | **Fonts**: Source Sans 3, Bricolage Grotesque, IBM Plex Mono | Self-hosted files in `leg_platform/static/vendor/fonts/` with their licences (SIL OFL 1.1); loaded by `@font-face` in `portal.css` | A page makes **no request to Google Fonts or any other font host**. The Content Security Policy (`font-src 'self'`, `style-src 'self'`) forbids it technically. The design previews of the owner load Google Fonts; the product does not. |
| N-2 | **Icons and flags**: Tabler Icons, the four flags of the language menu | Files of the own origin (`static/vendor/tabler-icons/`, `static/img/flags/`); `img-src 'self'` | No external image host |
| N-3 | **Scripts and styles**: Tabler (stylesheet), the own scripts | Own origin only (`script-src 'self'`); no CDN, no analytics, no tag manager | No external script host |
| N-4 | **Logos of the instance** | Static files of the instance (`INSTANCE_STATIC_DIRECTORY`), embedded as images | Own origin |
| N-5 | **Address check** (swisstopo address directory, D-033, D-049, D-052, D-075) | A **local copy** of the public directory in an own SQLite file on the server (not part of any backup); suggestions query only the local copy | Profile addresses are never sent to an external service. The copy is imported by the worker of the instance from the public source of the Federal Office of Topography (public data, no personal data of the visitors) |
| N-6 | **Language and colour mode of the page** | `sessionStorage` of the browser tab (D-056, K3) | Nothing is stored in a cookie and nothing is sent to the server |
| N-7 | **Error tracker, log service, analytics, bot check** | **None are used** in the code of this repository | – |

## 3. Links to other websites (the portal sends no data)

The footer links "Datenschutz", "Impressum" and the host of the website of the instance open the addresses that the administrator maintains (portal settings, D-056) in a new tab with `rel="noopener noreferrer"`. The portal sends no data to these sites; the browser of the visitor connects to them only after a click.

## 4. How to add an entry

1. Add a row to section 1 before the feature goes live, with all columns. Write **open** where a fact is not verified; never guess.
2. Name the entry in the change description and in `docs/DECISIONS.md`.
3. The security review checks that the entry exists; the owner confirms it before the page is published.
