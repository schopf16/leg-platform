# Design Language – LEG-Platform

This document defines the visual design and interaction principles of the application interface. It is binding for every page of every phase, so that later pages look and behave like the existing ones (D-024).

The design language was developed with the project owner on 2026-10-11 from mockups in three directions and confirmed in its third version. The mockups are not part of the repository; this document is the binding description.

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

### 3.1 Neutral Colours

The neutral colours are Tabler's "stone" grey palette (`--tblr-gray-50` … `--tblr-gray-950`; values in Tabler's documentation, "Theme base colors").

| Token | Light | Dark |
| ----- | ----- | ---- |
| Page background | `#F5F5F4` (stone 100) | `#0C0A09` (stone 950) |
| Surface (cards, forms) | `#FFFFFF` | `#1C1917` (stone 900) |
| Text | `#1C1917` (stone 900) | `#F5F5F4` (stone 100) |
| Secondary text | `#57534E` (stone 600) | `#A8A29E` (stone 400) |
| Divider lines | `#E7E5E4` (stone 200) | `#44403C` (stone 700) |
| Form field border | `#78716C` (stone 500) | `#8A817B` |
| Dark sidebar (administration views) | `#1C1917` | `#0C0A09` |
| Error | `#B42318` | `#F0948A` |

### 3.2 Instance Branding

The platform itself is neutral. Each instance configures its own branding; these values are part of the instance configuration and are never hard-coded (`AGENTS.md`, section 6):

* logo, positive version (for light backgrounds) and negative version (for dark backgrounds)
* accent colour for light mode and for dark mode, each with the text colour used on it
* optional highlight colour, used only as a background with dark text

Without instance values, the platform defaults apply: accent `#2F6B4F` (light) and `#74C59A` (dark), no highlight colour, the instance name as text instead of a logo.

Rules for instance values:

* Text on the accent colour, and the accent colour used as text on the surface and page background, reach at least 4.5:1 (WCAG 2.2, success criterion 1.4.3).
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

Values derived from the logo of the LEG Ittigen (provided by the project owner; file names `LEG-Ittigen_pos_rgb.svg` and `LEG-Ittigen_neg_sw.svg`). The logo files are instance data and are not stored in this repository.

| Value | Light | Dark |
| ----- | ----- | ---- |
| Accent | `#005BA2` (logo blue), text on it `#FFFFFF` | `#6AA9E0`, text on it `#0C0A09` |
| Highlight | `#DDDD22` (logo yellow), text on it `#1C1917` | same |
| Logo | positive version | negative version (white) |

No further logo guidelines (clear space, minimum size) are known to the project owner. The logo is shown unchanged.

These values are entered only in the configuration of the Ittigen instance; they are not used as defaults in code, templates, fixtures, migrations or `.env.example` (D-013, D-014).

### 3.4 Energy Colours

The meaning of these colours is fixed and independent of the instance accent.

| Meaning | Light | Dark |
| ------- | ----- | ---- |
| Production | `#B7791F` | `#E8B34A` |
| Used within the LEG | `#2F8F5B` | `#5DBB86` |
| Grid electricity | `#8A817B` | `#8A817B` |
| Consumption | `#7B4FA8` | `#B394E0` |

### 3.5 Status Indicators

A status is always shown as a dot plus a word, never by colour alone (WCAG 2.2, success criterion 1.4.1). Dot colours: active or completed uses "used within the LEG", pending or in review uses "production", problem uses the error colour, inactive uses "grid electricity", new uses the accent.

### 3.6 Verified Contrast Ratios

Calculated on 2026-10-11 with the contrast ratio formula of WCAG 2.2. Required: 4.5:1 for text, 3:1 for graphical objects and form field borders (success criteria 1.4.3 and 1.4.11).

| Pair | Light | Dark |
| ---- | ----- | ---- |
| Text on surface | 17.49 | 16.03 |
| Text on page background | 16.03 | 18.11 |
| Secondary text on surface | 7.63 | 6.93 |
| Secondary text on page background | 6.99 | 7.83 |
| Error text on surface | 6.57 | 7.75 |
| Form field border on surface | 4.80 | 4.59 |
| Sidebar text `#E7E5E4` on sidebar | 13.93 | – |
| Platform default accent: text on accent | 6.29 | 9.59 |
| Ittigen accent: text on accent | 6.96 | 7.87 |
| Ittigen accent as link (on page background / surface) | 6.38 | 6.97 |
| Text on Ittigen highlight | 12.03 | 12.03 |
| Ittigen highlight on white | 1.45 (not permitted as text or chart colour) | – |
| Production on surface | 3.64 | 9.14 |
| Used within the LEG on surface | 4.04 | 7.43 |
| Grid electricity on surface | 3.81 | 4.59 |
| Consumption on surface | 5.96 | 6.85 |

Contrast alone does not make a page conformant. Keyboard operation, screen reader support and zoom are verified during implementation.

## 4. Typography

| Role | Typeface | Licence |
| ---- | -------- | ------- |
| Text, all controls, headings in Clear | Source Sans 3 | SIL Open Font License 1.1, Reserved Font Name "Source" |
| Headings and key figures in Warm | Bricolage Grotesque | SIL Open Font License 1.1, no Reserved Font Name |
| Identifiers and table figures in Clear | IBM Plex Mono | SIL Open Font License 1.1, Reserved Font Name "Plex" |

* Fonts are served by the instance itself in fixed versions, never from Google Fonts or another CDN (`docs/SECURITY.md`, section 6).
* The unmodified font files are used. Each font is shipped with its copyright notice and licence text (OFL 1.1, condition 2). A modified version (e.g. a subset) must not use the Reserved Font Name (condition 3).
* Figures that are compared in columns use tabular figures.

## 5. Shapes, Spacing and Icons

* Corner radius: buttons 8 px, form fields 6 px, cards 6 px in Clear and 16 px in Warm.
* Shadows only on Warm cards. Clear uses lines.
* Spacing uses Tabler's spacing utilities; no own spacing scale.
* Icons: Tabler Icons (MIT licence), served by the instance itself. Icons accompany text; an icon without visible text has an accessible name.

## 6. Navigation and Layout

* **Members on mobile phones:** a menu button at the top left opens all further pages, including pages added in later phases. A tab bar at the bottom shows the most important pages (at most four) without opening the menu.
* **Members on larger screens:** the same structure, with the tab bar pages in the top navigation.
* **Administration views:** dark vertical sidebar (Tabler vertical navbar with `data-bs-navbar-theme="dark"`), breadcrumb, page title with one primary action.
* **Login page:** entry point of the portal. Small Warm welcome area with the instance logo, Clear form with login, link to password reset, button to register, and links to the privacy notice and the instance's website (D-025).
* **Multi-step processes** (e.g. later onboarding): one step per page, numbered step indicator with labels, back and continue buttons.
* One primary action per area. Destructive actions use the outlined error style and require confirmation.

## 7. Colour Modes and Print

* Light and dark mode. The default follows the device setting (`prefers-color-scheme`); the person can switch. The mode is set on the `<html>` element via Tabler's `data-bs-theme` attribute.
* The `data-bs-theme` attribute is rendered by the server.
* If switching in the browser is needed, a small own script served as an external file (no inline script) is used. It reads only the key `theme`, accepts only the values `light`, `dark` and `auto` (allowlist), evaluates no URL parameters and never sets branding attributes (e.g. primary colour, base colours, font, radius).
* Tabler's theme script (`tabler-theme.js`) is not used. According to Tabler's `color-modes.mdx` and `core/js/tabler-theme.ts` (branch `main`), it reads URL parameters for all theme keys (including `theme-primary`, `theme-base`, `theme-font`, `theme-radius`, `layout`, `navbar-position`, `navbar-theme`) and persists them in `localStorage`. A crafted link could thereby override the instance branding persistently in the visitor's browser. Whether release 1.6.1 behaves the same way was not verified; the decision does not depend on it.
* In dark mode, the negative logo is used.
* Print: a print stylesheet hides navigation and interactive controls, prints black text on white, and shows the instance name and the print date.

## 8. Formats and Tone

* Numbers with the Swiss thousands separator (`112’400 kWh`), amounts as `CHF 61.40`, tariffs in `Rp./kWh`, dates as `11.10.2026`, customer number in two blocks of three digits (`482 915`, D-021).
* The Swiss German texts use the formal form of address, short and plain sentences. Buttons name the action (e.g. "Save", "Log in" as English source texts). Error messages state what is wrong and how to fix it.

## 9. Implementation in Tabler

Verified against Tabler's documentation in the official repository `tabler/tabler` (branch `main`; pages "Customize Tabler", "Color modes", "Theme base colors"; checked on 2026-10-11; latest release at that time: `@tabler/core` 1.6.1 of 2026-09-28). The mechanisms are re-checked against the version chosen under D-015.

| Design element | Tabler mechanism |
| -------------- | ---------------- |
| Accent | `--tblr-primary` and `--tblr-primary-fg`, from the instance configuration |
| Neutral colours | `--tblr-gray-50` … `--tblr-gray-950` with the stone values, or `data-bs-theme-base="stone"` with the `tabler-themes` stylesheet |
| Text typeface | `--tblr-font-sans-serif` |
| Base font size per register | `--tblr-body-font-size`, which the documentation allows to override on any container |
| Light, dark and automatic mode | `data-bs-theme` on `<html>` |
| Dark sidebar | `data-bs-navbar-theme="dark"` |
| Registers | one CSS class per register on the page or area container, setting radius, heading typeface, shadow and font size through CSS variables |

Tabler's documentation notes that changing the primary colour, base or radius can change contrast and must be re-checked; section 3.2 makes this a rule for instance values.

## 10. Security Notes

* Logo files are provided by the operator as static files of the instance. In phase 1 there is no web upload for logos. SVG logos are embedded only as images (`<img>`), never inlined into the HTML, so that content of the file cannot execute in the page. According to W3C SVG Integration, an SVG document referenced through `<img>` is processed without running scripts, while an SVG file opened directly as a document can run scripts.
* Because a logo file can be opened directly through its URL, static SVG logo files are checked for scripts and external references before deployment and/or served with a restrictive Content Security Policy. The installation guide describes the check; the concrete header is decided at implementation.
* Branding values are validated before use (section 3.2), and the colour mode cannot be changed through URL parameters (section 7).
* No fonts, icons or stylesheets are loaded from third parties (`docs/SECURITY.md`, section 6).

## 11. Open Points

* Whether instance branding values are kept in environment variables, files or the database (`docs/ARCHITECTURE.md`, "Deliberately Open").
* Final page structure of later member and administration views; this document defines how they look, not which functions they have.

## Sources

* WCAG 2.2, contrast ratio and success criteria 1.4.1, 1.4.3, 1.4.11: https://www.w3.org/TR/WCAG22/ (checked on 2026-10-11)
* Tabler documentation in the official repository: https://github.com/tabler/tabler, files `docs/content/ui/getting-started/customize.mdx`, `docs/content/ui/getting-started/color-modes.mdx`, `docs/content/ui/plugins/theme-base-colors.mdx` (branch `main`, checked on 2026-10-11)
* Tabler theme script: https://github.com/tabler/tabler, files `docs/content/ui/getting-started/color-modes.mdx` and `core/js/tabler-theme.ts` (branch `main`, checked on 2026-10-11 in the security review; behaviour of release 1.6.1 not verified)
* W3C SVG Integration: https://www.w3.org/TR/svg-integration/ (First Public Working Draft, 2014; cited in the security review of 2026-10-11)
* Font licences: https://github.com/google/fonts, `ofl/sourcesans3/OFL.txt`, `ofl/bricolagegrotesque/OFL.txt`, `ofl/ibmplexmono/OFL.txt` (checked on 2026-10-11)
* Tabler and Tabler Icons licences (MIT): https://github.com/tabler/tabler, https://github.com/tabler/tabler-icons (checked on 2026-10-11)
* Logo files and colours: provided by the project owner (2026-10-11)
