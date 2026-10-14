# Design Language – LEG-Platform

This document defines the visual design and interaction principles of the application interface. It is binding for every page of every phase, so that later pages look and behave like the existing ones (D-024).

The design language was developed with the project owner on 2026-10-11 from mockups in three directions and confirmed in its third version. The mockups are not part of the repository; this document is the binding description.

**Update of 2026-10-14 (D-056).** The project owner delivered a design package (`LEG-Design-Vorgaben/`: images, `VORGABEN.md` and reference pages for the login, the registration, the navigation, the profile, the legal pages and the logo) and ordered that the homepage is built according to it. **Where this document and the package differ, the package wins.** Sections 3 (colours), 4 (typography), 5 (shapes and fonts sizes), 6 (navigation and layout) and 7 (colour modes) are changed accordingly; the decisions of the team lead K1 to K22 (`docs/DECISIONS.md`, D-056) fill the points that the package leaves open. The package is a design prototype; the implementation details (Django templates, translations, the Content Security Policy) are decided in the project.

## 1. Scope

* Applies to all application views built with Tabler (D-003): login, registration, profile, later member views and any later administration views built as application views.
* The Django admin keeps its own appearance. Whether later administrative functions are built in the Django admin or as application views is decided per function (`docs/ARCHITECTURE.md`, "Deliberately Open"). If they are built as application views, this document applies.
* User-facing texts follow `AGENTS.md`, section 2 (Swiss German, no "ß", translatable).

## 2. Principle: One Foundation, Two Registers

All pages share one foundation: colours, text typeface, icons, controls (buttons, form fields, status indicators), spacing and navigation. On top of it, each page or page area uses one of two registers.

| Register | Used for | Character |
| -------- | -------- | --------- |
| **Clear** | Entering, confirming and managing data: login, registration, password and two-factor functions, profile editing, onboarding, all forms, administration views | Plain and unambiguous. No illustrations or decoration, lines instead of shadows, small corner radius, headings in the text typeface, identifiers (customer number, metering point) in a monospace typeface. |
| **Warm** | Informing and exploring: overview pages, energy data, messages, information about the LEG | Friendly and inviting. Display typeface for headings, soft cards with a light shadow, a light tint of the accent colour, restrained illustrations are allowed. |

**Rule for new pages:** If the person has to enter, confirm or manage something, the register is Clear. Otherwise it is Warm. On mixed pages, the area in which data is entered uses Clear (example: the login page has a small Warm welcome area and a Clear form).

Controls never change between registers: a button, a form field or a status indicator looks the same everywhere.

### Density

* Clear for members and the public: base font size 16 px, interactive controls at least 48 px high.
* Clear for administration views (staff users and system administrators): compact, base font size 14 px.
* Warm: base font size 16 px.

## 3. Colours

### 3.1 Neutral Colours and Tokens

The tokens of the design package (`login/VORGABEN.md`, section 6). They are defined in `portal.css`: the light values on `:root`, the dark values in `:root[data-theme="dark"]`. The variables of Tabler (`--tblr-*`) are mapped onto them. Dark applies only when the person chooses it with the button (section 7).

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
| `--ok` | `#2F8F5B` | `#5DBB86` | Status "done" and success (always with a word; as text it is 4.04:1 on white in light mode, so it is used as a dot and an icon) |
| `--warn` | `#8A5A00` | `#E8B34A` | Status "open" and "not set up", warning alerts (K16) |
| `--warm` | accent 9 % over `--bg` | accent 9 % over `--bg` | Left panel of the entry pages, hover of menu entries |
| `--logo-plate` | transparent | foreground 8 % over `--warm` | Plate behind the logo in dark mode (radius 6 px, padding 4 px 8 px) |

The sidebar of the administration views (Django admin) keeps its own appearance and is not covered by the tokens.

### 3.2 Instance Branding

The platform itself is neutral. Each instance configures its own branding; these values are part of the instance configuration and are never hard-coded (`AGENTS.md`, section 6):

* logo, positive version (for light backgrounds) and negative version (for dark backgrounds)
* accent colour for light mode and for dark mode, each with the text colour used on it
* optional highlight colour (the token `--yellow`), used only as the dot of the benefit list, never with text on it (rule R9)

Without instance values, the platform defaults apply. They are the values of the design package (D-056): accent `#005BA2` (light) and `#6AA9E0` (dark), text on it `#FFFFFF` and `#0B1520`, highlight `#DDDD22` (the token `--yellow`). Without a logo, the instance name is shown as text.

Rules for instance values:

* Text on the accent colour, and the accent colour used as text on the panel and the page background, reach at least 4.5:1 (WCAG 2.2, success criterion 1.4.3). The application checks this at the start against the surfaces `#FFFFFF`, `#F6F7F8` (light) and `#191C1F`, `#111315` (dark).
* The accent colour must not be confusable with the error colour or the energy colours (section 3.4). Red tones are not permitted as accent. This rule is not checked automatically; it is a manual check by the operator, and the installation guide describes it.
* The highlight colour is never used as text colour or as a chart colour on a light background.
* The accent is used for buttons, links, active navigation and the focus indicator. It is never used in charts.

Validation of instance values (`docs/SECURITY.md`, section 6):

* Colour values must match `^#[0-9A-Fa-f]{6}$`.
* Configured URLs (privacy notice, the instance's website) must be absolute `https://` URLs.
* Logo references must point to a file inside the instance's static files with an allowlisted extension (`.svg`, `.png`); absolute paths, path traversal and external URLs are rejected.
* Values are validated when the application starts (values from the configuration) or when they are saved (values from the database). Invalid values are rejected; they are never used.
* Branding values are passed into CSS variables in a way that the Content Security Policy allows without `'unsafe-inline'` (e.g. a stylesheet generated from the validated values and served as a file). The concrete mechanism is decided at implementation.

### 3.3 Initiating Instance: LEG Ittigen

The values below are derived from the logo of the LEG Ittigen (provided by the project owner; file names `LEG-Ittigen_pos_rgb.svg` and `LEG-Ittigen_neg_sw.svg`). **Since D-056 they are also the platform defaults** (the owner ordered them; this replaces the earlier rule that they are not defaults in code). The logo files are instance data and are not stored in this repository: they are public static files in the instance static directory and are used unchanged.

| Value | Light | Dark |
| ----- | ----- | ---- |
| Accent | `#005BA2` (logo blue), text on it `#FFFFFF` | `#6AA9E0`, text on it `#0B1520` |
| Highlight | `#DDDD22` (logo yellow) | same |
| Logo | positive version | negative version (white), on the plate |

No further logo guidelines (clear space, minimum size) are known to the project owner. The logo is shown unchanged and never recoloured, redrawn, cropped or stretched. Sizes: 360 px wide on the entry pages, 40 px high in the top bar of the phone, 48 px high in the header after the login, 32 px high in the phone menu.

### 3.4 Energy Colours

The meaning of these colours is fixed and independent of the instance accent. They are used by the later energy pages; their contrast on the surfaces of the new tokens (`--panel`) was not recalculated in phase 1 and is verified when they are first used (open point).

| Meaning | Light | Dark |
| ------- | ----- | ---- |
| Production | `#B7791F` | `#E8B34A` |
| Used within the LEG | `#2F8F5B` | `#5DBB86` |
| Grid electricity | `#8A817B` | `#8A817B` |
| Consumption | `#7B4FA8` | `#B394E0` |

### 3.5 Status Indicators

A status is always shown as a dot plus a word, never by colour alone (WCAG 2.2, success criterion 1.4.1). Dot colours: active or completed uses `--ok`, open, pending or not set up uses `--warn` (K16), problem uses `--bad`, inactive uses `--muted`, new uses the accent.

### 3.6 Verified Contrast Ratios

Calculated on 2026-10-14 with the contrast ratio formula of WCAG 2.2 (`leg_platform.branding.contrast`); the tests of `test_design_assets.py` check them against the tokens in `portal.css`. Required: 4.5:1 for text, 3:1 for graphical objects and the borders of fields and buttons (success criteria 1.4.3 and 1.4.11).

| Pair | Light | Dark |
| ---- | ----- | ---- |
| Text on panel | 16.89 | 14.04 |
| Text on page background | 15.75 | 15.28 |
| Muted text and field borders on panel | 6.35 | 6.62 |
| Muted text on page background | 5.92 | 7.21 |
| Muted text on the warm panel | 5.16 | 6.37 |
| Text on the warm panel | 13.74 | 13.49 |
| Error colour on panel | 6.57 | 7.58 |
| "Open" colour (`--warn`) on panel | 5.93 | 8.95 |
| "Done" colour (`--ok`) on panel | 4.04 (graphic and icon, not as text) | 7.27 |
| Text on accent | 6.96 | 7.32 |
| Accent as link on page background / panel | 6.49 / 6.96 | 7.42 / 6.82 |
| Yellow dot on the warm panel | 1.18 (decoration, carries no information and no text) | 11.31 |

Contrast alone does not make a page conformant. Keyboard operation, screen reader support and zoom are verified during implementation.

## 4. Typography

| Role | Typeface | Licence |
| ---- | -------- | ------- |
| All text, all controls, all headings, customer number | Source Sans 3 (weights 400, 600 and 700) | SIL Open Font License 1.1, Reserved Font Name "Source" |
| The title in the warm panel of the entry pages only | Bricolage Grotesque (weight 700) | SIL Open Font License 1.1, no Reserved Font Name |
| Recovery codes and the manual key of the authenticator app only | IBM Plex Mono | SIL Open Font License 1.1, Reserved Font Name "Plex" |

* Fonts are served by the instance itself in fixed versions, never from Google Fonts or another CDN (`docs/SECURITY.md`, section 6; `docs/SERVICES.md`). The design previews of the owner load Google Fonts; the product does not.
* The unmodified font files are used. Each font is shipped with its copyright notice and licence text (OFL 1.1, condition 2). A modified version (e.g. a subset) must not use the Reserved Font Name (condition 3).
* The customer number is set in Source Sans 3, not in the monospace typeface (package; changes the earlier rule).
* **Font sizes (K6):** 11, 12 and 13 px (micro labels only: menu group names, the label "später", the bottom bar), 14 px (hints, the footer row), 16 px (text, labels, controls), 18, 20, 22, 24, 26 (heading "Anmelden"), 32 and 40 px (title of the warm panel; 30 px on a phone). Controls are at least 44 px high on a desktop and 48 px on a phone (R5, K5).

## 5. Shapes, Spacing and Icons

* Corner radius: buttons and fields 8 px, cards and tiles 12 px, the logo plate 6 px.
* No shadows and no gradients (R7). The one exception is the soft shadow of the floating language menu, as in the reference of the owner. Lines (1 px `--line`, 1.5 px `--muted` for fields and buttons) separate surfaces.
* Spacing only from 4, 8, 12, 16, 24, 32 and 48 px (R3).
* Focus is always visible: a 2 px accent outline with a 2 px offset. A field in focus has a 1.5 px accent border and a 3 px ring of 30 % accent.
* Icons: line style, stroke 1.8 px, round ends, 20 px (Tabler Icons, MIT licence, or own SVG), served by the instance itself. Icons accompany text; an icon without visible text has an accessible name. No emoji (R15).

## 6. Navigation and Layout

* **Pages without a session (the entry shell, `entry_base.html`):** login, registration, password reset, the second step of the login, the lockout and result pages. Two equal columns above 760 px: the warm panel (`--warm`, padding 48 px, the logo 360 px wide, the title in Bricolage Grotesque 40 px, the lead 18 px, two points with a yellow dot) and the form with a width of at most 400 px on the page background, separated by a 1 px line. On a phone (760 px and below) the panel shrinks to a top bar of at least 64 px with the logo (40 px high), the text is hidden and the page fits one screen without scrolling (the login). The footer row "Datenschutz | Impressum | host of the website" sits under the form. The panel texts depend on the page type (D-056).
* **Corner buttons on every page:** the language menu (DE, FR, IT, EN) and the light/dark button, round, 40 px, fixed in the top right corner (in the header after the login, next to the name). They are the same on every page, also on the error pages.
* **After the login (team lead decision K1, D-057):** a fixed header of 64 px (the logo 48 px high, the name as text, the corner buttons), on a desktop a fixed left menu of 232 px (MEIN BEREICH: Start, Profil, for staff users Verwaltung; DEMNÄCHST: Standorte, Rechnungen, Energie, Mitteilungen with the mark "später", which are no links; below a line Abmelden as a POST form, Datenschutz and Impressum), on a phone a bottom bar of 72 px (Start, Energie, Rechnungen, Standorte) and the same menu in a drawer of 290 px from the left with an overlay (Escape and the close button close it, the focus is held inside and returns to the menu button). Content: at most 960 px wide, padding 32/24 px on a desktop and 20/16 px on a phone, the page title first. The start page `/` has the greeting, the customer number with the address and the task tiles (hiding is final and stored on the server) or the empty state. An account that must change its password gets the header and "Abmelden" only.
* **Administration views:** dark vertical sidebar (Tabler vertical navbar with `data-bs-navbar-theme="dark"`), breadcrumb, page title with one primary action. The Django admin keeps its own appearance.
* **Multi-step processes:** one step per page, a 4 px step bar with the label "Schritt 1 von 3", back and continue buttons.
* One primary button per page (R1). Destructive actions use the outlined error style and require confirmation.

## 7. Colour Modes and Print

* **Light and dark only by the button (R13 changed by the package, D-056).** There is no automatic mode and no `prefers-color-scheme` rule. The server always renders light; the person switches with the sun/moon button in the top right corner. The mode is set on the `<html>` element as `data-theme` (own tokens) and as `data-bs-theme` (Tabler); both always have the same value.
* **Persistence (K3, the one deviation from the package).** The package starts every page load light and German and stores nothing. In an application of several pages that would reset the choice at every click, so the choice is kept in the `sessionStorage` of the tab: no cookie, nothing is sent to the server, a new tab starts light and German. The scripts are external files (`theme-init.js` in the `<head>`, `corner-controls.js`); they read only the allowlisted values `light`, `dark`, `de`, `fr`, `it` and `en`, evaluate no URL parameters and never set branding attributes (primary colour, base colours, font, radius). Without JavaScript the buttons are hidden and the page stays light and German.
* **Languages.** The menu offers Deutsch, Français, Italiano and English. Translations of the page texts exist for the login page only (a static dictionary; French, Italian and English are drafts that native speakers must review). Every other page stays German until the translations are delivered; there the language button remembers the choice only.
* Tabler's theme script (`tabler-theme.js`) is not used. According to Tabler's `color-modes.mdx` and `core/js/tabler-theme.ts` (branch `main`), it reads URL parameters for all theme keys (including `theme-primary`, `theme-base`, `theme-font`, `theme-radius`, `layout`, `navbar-position`, `navbar-theme`) and persists them in `localStorage`. A crafted link could thereby override the instance branding persistently in the visitor's browser. Whether release 1.6.1 behaves the same way was not verified; the decision does not depend on it.
* In dark mode, the negative logo is used, on the plate of section 3.1.
* Print: a print stylesheet hides navigation, the corner buttons and interactive controls, prints black text on white, and shows the instance name and the print date.

## 8. Formats and Tone

* Numbers with the Swiss thousands separator (`112’400 kWh`), amounts as `CHF 61.40`, tariffs in `Rp./kWh`, dates as `11.10.2026`, customer number in two blocks of three digits (`482 915`, D-021).
* The Swiss German texts use the formal form of address, short and plain sentences. Buttons name the action (e.g. "Save", "Log in" as English source texts). Error messages state what is wrong and how to fix it.

## 9. Implementation in Tabler

Verified against Tabler's documentation in the official repository `tabler/tabler` (branch `main`; pages "Customize Tabler", "Color modes", "Theme base colors"; checked on 2026-10-11; latest release at that time: `@tabler/core` 1.6.1 of 2026-09-28). The mechanisms are re-checked against the version chosen under D-015.

| Design element | Tabler mechanism |
| -------------- | ---------------- |
| Accent | The token `--accent` (and `--on-accent`), mapped to `--tblr-primary` and `--tblr-primary-fg`; the instance values come from the stylesheet `instance.css` |
| Neutral colours | The tokens of section 3.1, mapped to `--tblr-body-bg`, `--tblr-body-color`, `--tblr-bg-surface`, `--tblr-secondary-color`, `--tblr-border-color` and `--tblr-danger` |
| Text typeface | `--tblr-font-sans-serif` |
| Base font size per register | `--tblr-body-font-size`, which the documentation allows to override on any container |
| Light and dark mode | `data-theme` (own tokens) and `data-bs-theme` (Tabler) on `<html>`, set together; never `prefers-color-scheme` |
| Dark sidebar | `data-bs-navbar-theme="dark"` |
| Registers | one CSS class per register on the page or area container, setting radius, heading typeface, shadow and font size through CSS variables |

Tabler's documentation notes that changing the primary colour, base or radius can change contrast and must be re-checked; section 3.2 makes this a rule for instance values.

## 10. Security Notes

* Logo files are provided by the operator as static files of the instance. In phase 1 there is no web upload for logos. SVG logos are embedded only as images (`<img>`), never inlined into the HTML, so that content of the file cannot execute in the page. According to W3C SVG Integration, an SVG document referenced through `<img>` is processed without running scripts, while an SVG file opened directly as a document can run scripts.
* Because a logo file can be opened directly through its URL, static SVG logo files are checked for scripts and external references before deployment and/or served with a restrictive Content Security Policy. The installation guide describes the check; the concrete header is decided at implementation.
* Branding values are validated before use (section 3.2), and the colour mode cannot be changed through URL parameters (section 7).
* The legal addresses in the footer come from the portal settings of the administrator, are validated (`https` only, no credentials) and open in a new tab with `rel="noopener noreferrer"` (D-056).
* No fonts, icons or stylesheets are loaded from third parties (`docs/SECURITY.md`, section 6).

## 11. Open Points

* Whether instance branding values are kept in environment variables, files or the database (`docs/ARCHITECTURE.md`, "Deliberately Open"). The package asks for the administration of the two legal addresses only; they are in the portal settings (D-056).
* Final page structure of later member and administration views; this document defines how they look, not which functions they have.
* The French, Italian and English texts of the login page and the labels of the corner buttons are drafts; native speakers must review them (package, `login/VORGABEN.md`, section 12).
* The contrast of the energy colours on the new surfaces is verified when they are first used (section 3.4).

## Sources

* WCAG 2.2, contrast ratio and success criteria 1.4.1, 1.4.3, 1.4.11: https://www.w3.org/TR/WCAG22/ (checked on 2026-10-11)
* Tabler documentation in the official repository: https://github.com/tabler/tabler, files `docs/content/ui/getting-started/customize.mdx`, `docs/content/ui/getting-started/color-modes.mdx`, `docs/content/ui/plugins/theme-base-colors.mdx` (branch `main`, checked on 2026-10-11)
* Tabler theme script: https://github.com/tabler/tabler, files `docs/content/ui/getting-started/color-modes.mdx` and `core/js/tabler-theme.ts` (branch `main`, checked on 2026-10-11 in the security review; behaviour of release 1.6.1 not verified)
* W3C SVG Integration: https://www.w3.org/TR/svg-integration/ (First Public Working Draft, 2014; cited in the security review of 2026-10-11)
* Font licences: https://github.com/google/fonts, `ofl/sourcesans3/OFL.txt`, `ofl/bricolagegrotesque/OFL.txt`, `ofl/ibmplexmono/OFL.txt` (checked on 2026-10-11)
* Tabler and Tabler Icons licences (MIT): https://github.com/tabler/tabler, https://github.com/tabler/tabler-icons (checked on 2026-10-11)
* Logo files and colours: provided by the project owner (2026-10-11)
* Design package of the project owner: `LEG-Design-Vorgaben/` (2026-10-14), see D-056
