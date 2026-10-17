# Design Language – LEG-Platform

Binding for every page, so that later pages look and behave like the existing ones (D-024). Where this document and the owner's design package (`LEG-Design-Vorgaben/`, `VORGABEN.md`, reference pages; D-056) differ, the package wins; the decisions K1 to K22 of D-056 fill the points the package leaves open. Implementation details (templates, translations, Content Security Policy) are decided in the project.

## 1. Scope

* All application views built with Tabler (D-003): login, registration, profile, member and management views.
* The Django admin keeps its own appearance, plus one small stylesheet (`static/css/admin-portal.css`: field widths, the box "Erste Schritte", help about the rights) that uses the admin's colour variables and changes nothing else (D-061). Whether later functions are admin or application views: `docs/ARCHITECTURE.md`, "Deliberately Open".
* User-facing texts follow `AGENTS.md`, section 2.

## 2. Principle: One Foundation, Two Registers

All pages share colours, text typeface, icons, controls (buttons, fields, status indicators), spacing and navigation. On top of it each page or area uses one of two registers.

| Register | Used for | Character |
| -------- | -------- | --------- |
| **Clear** | Entering, confirming and managing data: login, registration, password and two-factor functions, profile, onboarding, all forms, administration views | Plain and unambiguous. No illustrations or decoration, lines instead of shadows, small radius, headings in the text typeface, identifiers (metering point) in a monospace typeface. |
| **Warm** | Informing and exploring: overview pages, energy data, messages, information about the LEG | Friendly. Display typeface for headings, soft cards with a light shadow, light tint of the accent, restrained illustrations allowed. |

**Rule for new pages:** if the person has to enter, confirm or manage something, the register is Clear; otherwise Warm. On mixed pages the area in which data is entered is Clear (the login page: a small Warm welcome area and a Clear form). Controls never change between registers.

### Density

Clear for members and the public: base font size 16 px, controls at least 48 px high. Clear for administration views (staff, system administrators): compact, 14 px. Warm: 16 px.

## 3. Colours

### 3.1 Neutral Colours and Tokens

Tokens of the design package, defined in `portal.css` (light on `:root`, dark in `:root[data-theme="dark"]`); Tabler's `--tblr-*` variables are mapped onto them. Dark applies only when the person chooses it with the button (section 7).

| Token | Light | Dark | Use |
| ----- | ----- | ---- | --- |
| `--bg` | `#F6F7F8` | `#111315` | Page background, right panel of the entry pages |
| `--panel` | `#FFFFFF` | `#191C1F` | Inputs, corner buttons, header and card surfaces |
| `--fg` | `#1B1D20` | `#E6E9EC` | Text |
| `--muted` | `#5A6068` | `#9AA2AB` | Secondary text, borders of inputs and buttons, footer |
| `--line` | `#DEE2E7` | `#2A2F35` | Dividers and borders |
| `--accent` | `#005BA2` | `#6AA9E0` | Primary button, links, focus, selected language, active navigation |
| `--on-accent` | `#FFFFFF` | `#0B1520` | Text on the accent |
| `--yellow` | `#DDDD22` | `#DDDD22` | The dots of the benefit list only; never text (R9) |
| `--bad` | `#B42318` | `#F0948A` | Errors and status "problem" |
| `--ok` | `#2F8F5B` | `#5DBB86` | Status "done", success (always with a word; 4.04:1 on white, so a dot or icon, not text) |
| `--warn` | `#8A5A00` | `#E8B34A` | Status "open" and "not set up", warning alerts (K16) |
| `--warm` | accent 9 % over `--bg` | same | Left panel of the entry pages, hover of menu entries |
| `--logo-plate` | transparent | foreground 8 % over `--warm` | Plate behind the logo in dark mode (radius 6 px, padding 4 px 8 px) |

### 3.2 Instance Branding

The platform is neutral; each instance configures its own branding, never hard-coded (`AGENTS.md`, section 6): logo (positive for light, negative for dark backgrounds), accent colour for light and dark mode each with its text colour, and an optional highlight colour (`--yellow`, only the dot of the benefit list, never with text on it, R9). Platform defaults are the values of the design package (D-056): accent `#005BA2` / `#6AA9E0`, text on it `#FFFFFF` / `#0B1520`, highlight `#DDDD22`. Without a logo the instance name is shown as text.

**Where branding is changed (D-073).** Name, website, the two logos (PNG or SVG, both or none), accent and text colours, highlight colour, privacy and imprint addresses are maintained in the administration, "Inhalte", "Portal-Einstellungen" (an ordinary right of a role). Precedence per field: administration, then environment (`INSTANCE_*` variables: `docs/INSTALLATION.md`; the imprint address has none), then platform default. Email texts: "Mailtexte" (D-039); page texts: `locale/de_CH/LC_MESSAGES/django.po`; server settings are environment only (`docs/OPERATIONS.md`, section 5a). Under every field with a server value the page shows "Serverkonfiguration: <value>". The preview changes colours while typing (CSS variables through the CSSOM by an external script; the policy is not widened, so an unsaved logo has no preview). Removal and reset are checkboxes; an empty text field resets that field.

Rules for instance values:

* Text on the accent colour, and the accent as text on panel and page background, reach at least 4.5:1 (WCAG 2.2, 1.4.3). Checked at start against `#FFFFFF`, `#F6F7F8` (light) and `#191C1F`, `#111315` (dark), and again when saving; an empty text colour next to an administration accent is chosen automatically (white or `#0B1520`).
* The accent must not be confusable with the error or energy colours (section 3.4); red tones are not permitted (manual check by the operator, installation guide). The highlight colour is never text or chart colour on a light background; the accent is for buttons, links, active navigation and focus, never charts.
* Validation (`docs/SECURITY.md`, section 6): colours `^#[0-9A-Fa-f]{6}$`; configured URLs absolute `https://`; environment logos inside the instance static files with an allowlisted extension; uploaded logos PNG or SVG checked as described there (D-073); validated at start, on save and on read; invalid values are never used. Values reach CSS variables without `'unsafe-inline'`: a stylesheet generated from the validated values, served as a file (`instance.css`, ETag, `no-cache`).

### 3.3 Initiating Instance: LEG Ittigen

The accent, highlight and logo values of the LEG Ittigen (logo files `LEG-Ittigen_pos_rgb.svg` and `LEG-Ittigen_neg_sw.svg`, provided by the owner) are the platform defaults since D-056. The logo files are instance data, not stored in the repository, and are used unchanged: never recoloured, redrawn, cropped or stretched. Sizes: 360 px wide on the entry pages, 40 px high in the phone top bar, 48 px in the header after the login, 32 px in the phone menu. No further logo guidelines are known.

### 3.4 Energy Colours

Fixed meaning, independent of the instance accent; used by the later energy pages. Their contrast on `--panel` was not recalculated and is verified when first used (open).

| Meaning | Light | Dark |
| ------- | ----- | ---- |
| Production | `#B7791F` | `#E8B34A` |
| Used within the LEG | `#2F8F5B` | `#5DBB86` |
| Grid electricity | `#8A817B` | `#8A817B` |
| Consumption | `#7B4FA8` | `#B394E0` |

### 3.5 Status Indicators

A status is always a dot plus a word, never colour alone (WCAG 2.2, 1.4.1). Dots: active or completed `--ok`, open, pending or not set up `--warn` (K16), problem `--bad`, inactive `--muted`, new the accent.

### 3.6 Verified Contrast Ratios

Required (WCAG 2.2): 4.5:1 for text, 3:1 for graphical objects and borders of fields and buttons (1.4.3, 1.4.11), computed by `leg_platform.branding.contrast`; `test_design_assets.py` checks the token pairs against `portal.css`. Known low pairs, never to be used for text: `--ok` on white 4.04 (dot or icon only) and the yellow dot on the warm panel 1.18 (decoration only). Contrast alone does not make a page conformant; keyboard operation, screen reader support and zoom are verified during implementation.

## 4. Typography

| Role | Typeface | Licence |
| ---- | -------- | ------- |
| All text, controls, headings, customer number | Source Sans 3 (400, 600, 700) | SIL OFL 1.1, Reserved Font Name "Source" |
| Title in the warm panel of the entry pages only | Bricolage Grotesque (700) | SIL OFL 1.1, no Reserved Font Name |
| Recovery codes and the manual key of the authenticator app only | IBM Plex Mono | SIL OFL 1.1, Reserved Font Name "Plex" |

* Fonts are served by the instance itself in fixed versions, never from a CDN (`docs/SECURITY.md`, section 6; `docs/SERVICES.md`), unmodified, each with copyright notice and licence text (OFL 1.1, condition 2); a modified version (e.g. a subset) must not use the Reserved Font Name (condition 3).
* **Font sizes (K6):** 11, 12, 13 px (micro labels only: menu group names, "später", the bottom bar), 14 px (hints, footer row), 16 px (text, labels, controls), 18, 20, 22, 24, 26 (heading "Anmelden"), 32 and 40 px (title of the warm panel; 30 px on a phone). Controls are at least 44 px high on a desktop and 48 px on a phone (R5, K5).

## 5. Shapes, Spacing and Icons

* Corner radius: buttons and fields 8 px, cards and tiles 12 px, logo plate 6 px.
* No shadows and no gradients (R7); the one exception is the soft shadow of the floating language menu. Lines (1 px `--line`, 1.5 px `--muted` for fields and buttons) separate surfaces.
* Spacing only from 4, 8, 12, 16, 24, 32 and 48 px (R3).
* Focus is always visible: 2 px accent outline with 2 px offset; a field in focus has a 1.5 px accent border and a 3 px ring of 30 % accent.
* Icons: line style, stroke 1.8 px, round ends, 20 px (Tabler Icons, MIT, or own SVG), served by the instance. Icons accompany text; an icon without visible text has an accessible name. No emoji (R15).

## 6. Navigation and Layout

* **Pages without a session (`entry_base.html`):** login, registration, password reset, second step of the login, lockout and result pages. Two equal columns above 760 px: the warm panel (`--warm`, padding 48 px, logo 360 px wide, title in Bricolage Grotesque 40 px, lead 18 px, two points with a yellow dot) and the form (at most 400 px wide) separated by a 1 px line. At 760 px and below the panel shrinks to a top bar of at least 64 px with the logo (40 px high), the text is hidden and the login fits one screen. The footer row "Datenschutz | Impressum | host of the website" sits under the form (D-056).
* **Corner buttons on every page** (also error pages): language menu (DE, FR, IT, EN) and light/dark button, round, 40 px, fixed top right (in the header after the login, next to the name).
* **After the login (K1, D-057):** fixed header 64 px (logo 48 px high, name as text, corner buttons); on a desktop a fixed left menu 232 px wide (MEIN BEREICH: Start, Profil, for staff Verwaltung; DEMNÄCHST: Standorte, Rechnungen, Energie, Mitteilungen with the mark "später", no links; below a line "Abmelden" as a POST form, Datenschutz, Impressum); on a phone a bottom bar 72 px (Start, Energie, Rechnungen, Standorte) and the same menu in a drawer 290 px from the left with an overlay (Escape and the close button close it, focus held inside and returned to the menu button). Content at most 960 px wide, padding 32/24 px desktop and 20/16 px phone, page title first. Menu and header measures: the image measures of D-057. The start page has the greeting, the customer number with the address and the task tiles (hiding is final and stored on the server) or an empty state (panel with 1 px line, 12 px radius, at least 360 px high). An account that must change its password, or whose mandatory second factor is incomplete, gets the header and "Abmelden" only.
* **Registration (D-058):** three steps with a 4 px step bar (classes `step-fill-1` to `-3` because of the CSP) and the muted label "Schritt N von 3" (14 px) above the heading. The details step shows the address as plain text with the status "Bestätigt" (K7: no box); street and house number (96 px), postal code (128 px) and town side by side also on the phone. Hints under a field are 14 px; a field with an error has a red 2 px border and one message; the error summary uses short field names and is focused.
* **Profile (D-059, `navigation/VORGABEN.md` section 3.3):** "MEIN KONTO / Profil" with five cards on the page background (1 px line, 12 px radius, no shadow): Persönliche Angaben, E-Mail-Adressen, Passwort, Zwei-Faktor-Anmeldung (2FA), and the red start card Konto löschen. A card shows its values with a button "Ändern" that opens the form **in place** (server-rendered state `?edit=<card>`, works without a script, one card open at a time, errors re-render the same card with one message per field); extra fields are under "Weitere Angaben (optional)". Recovery codes are shown once with "Kopieren", a checkbox and "Fertig". Deletion: a red outlined button, then the confirmation with the proof, a box never ticked in advance and a red button.
* **One proof per confirmation (D-062).** A confirmation asks for the second-factor code if one is active, otherwise the password, never both (rules: `docs/SECURITY.md`, section 3). The proof field takes the place and style of the password field (48 px high, radius 8 px, semibold 16 px label, 8 px between label and field). With the code, a 16 px lead hint (`.profile-text`) stands between the label "2FA-Authentifizierungscode" and the field; the code field is a plain text field (no `inputmode`, `maxlength` or `pattern`); screen reader description order: lead, error, note. "Handy verloren?" is a text link under the code field of "2FA deaktivieren" that opens a state of the same card (also without a script) with the password and the recovery code, a text for people with neither (they go through the operator) and the red outlined button "2FA deaktivieren". All password fields are hidden by default with the eye button. The way back is the secondary button "Abbrechen" in every card. After five wrong entries the session ends and the login page says so (D-062). Images of the owner's package with "Aktuelles Passwort" are valid only for an account **without** a second factor; the state with a second factor has no image and is described here.
* **Links from emails (D-058, D-067):** an invalid, expired, used or replaced link redirects (302, same answer for every cause) to a fixed page of its flow, as does a confirmation page whose session state is gone. The fixed page carries the message and the next step, is identical for everybody, uses no flash message and is an entry-shell page without header or logout form.
* **Address suggestions and notice (R-04, R-05, R-21; D-065, D-066):** suggestions appear directly under the street field as a bordered list of buttons without shadow, titled "Matching addresses" (or "Did you mean this address?" for an approximate match); never inserted by a blur; replaced or hidden only when an answer arrives. Only where an address is edited (profile edit form, registration details; country fixed to Switzerland), never in read-only views. One non-blocking warning notice after the postal code and town row: "This address was not found. Please check street, house number, postal code and town. You can still save it as entered." Only for a complete address while editing and only when the directory is available; it hides on any input, never takes the focus and never blocks saving. Nothing else is ever said about the directory (no source, no text for an unavailable, old or limited directory). Choosing a suggestion is the only way the script changes fields.
* **Administration views:** dark vertical sidebar (Tabler vertical navbar with `data-bs-navbar-theme="dark"`), breadcrumb, page title with one primary action.
* **Multi-step processes:** one step per page, a 4 px step bar with "Schritt 1 von 3", back and continue buttons.
* One primary button per page (R1). Destructive actions use the outlined error style and require confirmation.

## 7. Colour Modes and Print

* **Light and dark only by the button (R13 changed by the package, D-056).** No automatic mode, no `prefers-color-scheme`. The server always renders light; the person switches with the sun/moon button. The mode is set on `<html>` as `data-theme` (own tokens) and `data-bs-theme` (Tabler), always the same value.
* **Persistence (K3).** The choice is kept in the tab's `sessionStorage` (no cookie, nothing sent to the server; a new tab starts light and German). The scripts are external files (`theme-init.js` in `<head>`, `corner-controls.js`); they read only the allowlisted values `light`, `dark`, `de`, `fr`, `it`, `en`, evaluate no URL parameters and never set branding attributes. Without JavaScript the buttons are hidden and the page stays light and German.
* **Languages.** The menu offers Deutsch, Français, Italiano, English; page-text translations exist for the login page only (static dictionary); every other page stays German and the language button only remembers the choice.
* Tabler's `tabler-theme.js` is not used: it reads URL parameters for all theme keys and persists them in `localStorage`, so a crafted link could override the instance branding persistently (Tabler `core/js/tabler-theme.ts`).
* In dark mode the negative logo is used, on the plate of section 3.1.
* Print: a print stylesheet hides navigation, corner buttons and controls, prints black text on white, and shows the instance name and the print date.

## 8. Formats and Tone

* Numbers with the Swiss thousands separator (`112’400 kWh`), amounts `CHF 61.40`, tariffs `Rp./kWh`, dates `11.10.2026`, customer number in two blocks of three digits (`482 915`, D-021).
* Swiss German texts use the formal address, short and plain sentences. Buttons name the action. Error messages state what is wrong and how to fix it.
* Security notifications (D-068): plain text with the link alone on its line. The first line is "Guten Tag Vorname Nachname" for an account of a person with a first and last name, otherwise the neutral "Guten Tag"; no comma, no Herr/Frau/Familie, no title. Neutral also for a company-only account, a name with a character a name does not have, a name that looks like an address and an address that does not currently belong to the account (former login address, removed second address, deleted account). The start page keeps "Guten Tag, Name".

## 9. Implementation in Tabler

Verified against Tabler's documentation ("Customize Tabler", "Color modes", "Theme base colors"); re-check against the version chosen under D-015. Changing primary colour, base or radius can change contrast and must be re-checked (section 3.2).

| Design element | Tabler mechanism |
| -------------- | ---------------- |
| Accent | `--accent` / `--on-accent` mapped to `--tblr-primary` and `--tblr-primary-fg`; instance values from `instance.css` |
| Neutral colours | tokens of section 3.1 mapped to `--tblr-body-bg`, `--tblr-body-color`, `--tblr-bg-surface`, `--tblr-secondary-color`, `--tblr-border-color`, `--tblr-danger` |
| Text typeface | `--tblr-font-sans-serif` |
| Base font size per register | `--tblr-body-font-size` (may be overridden on any container) |
| Light and dark mode | `data-theme` and `data-bs-theme` on `<html>`, set together; never `prefers-color-scheme` |
| Dark sidebar | `data-bs-navbar-theme="dark"` |
| Registers | one CSS class per register on the page or area container, setting radius, heading typeface, shadow and font size through CSS variables |

## 10. Security Notes

* Logo files are static files of the instance (fallback) or uploads of the administrator (D-073, database, served by the portal). SVG logos are embedded only as images (`<img>`), never inlined, so file content cannot execute in the page (W3C SVG Integration: an SVG referenced through `<img>` runs no scripts, one opened directly can).
* Because a logo file can be opened directly, the logo view serves every file with a fixed content type, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox; default-src 'none'` and `Cross-Origin-Resource-Policy: same-origin`; static SVG logo files are checked for scripts and external references before deployment and served with a restrictive CSP by the web server. Whether an SVG with `style` attributes renders in `<img>` under this policy was **not** checked in a browser (open, D-073).
* Footer legal addresses come from the portal settings or the instance configuration and are checked again before printing (`docs/SECURITY.md`, section 6). No third-party fonts, icons or stylesheets; the colour mode cannot be changed through URL parameters (section 7).

## 11. Open Points

* Final page structure of later member and administration views; this document defines how they look, not which functions they have.
* The contrast of the energy colours on the new surfaces is verified when first used (section 3.4).

## Sources

WCAG 2.2 (1.4.1, 1.4.3, 1.4.11); Tabler documentation and theme script (`tabler/tabler`); W3C SVG Integration; font licences (`google/fonts`); Tabler and Tabler Icons licences (MIT); owner-provided logos, colours and design package `LEG-Design-Vorgaben/` (2026-10-14, D-056).
