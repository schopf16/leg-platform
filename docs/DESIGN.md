# Design Language – LEG-Platform

Binding for every page (D-024). If this document and the owner's design package (`LEG-Design-Vorgaben/`, `VORGABEN.md`, reference pages; D-056) differ, the package wins. K1 to K22 of D-056 fill the points the package leaves open.

## 1. Scope

* All application views use Tabler (D-003): login, registration, profile, member and management views.
* The Django admin keeps its own appearance plus `static/css/admin-portal.css`, which uses the admin's colour variables (D-061).
* **Own admin pages (D-097)** follow the admin:
  * `<ol class="breadcrumbs">` (never a `div`); the page title is the one heading.
  * Buttons are the admin's own (`input[type=submit].default`, `.button`, never a bare `<button>`).
  * On touch (`pointer: coarse`) and up to 767 px every button, link and check box is at least 44 px high.
  * A table of files becomes one block per file on a phone. Dates are numeric (`17.10.2026 02:44`, section 8).
* Admin or application view for later functions: `docs/ARCHITECTURE.md`, "Deliberately Open".
* User-facing texts follow `AGENTS.md`, section 2.

## 2. Principle: One Foundation, Two Registers

All pages share colours, text typeface, icons, controls (buttons, fields, status indicators), spacing and navigation. Each page or area uses one of two registers on top.

| Register | Used for | Character |
| -------- | -------- | --------- |
| **Clear** | Entering, confirming and managing data: login, registration, password and two-factor, profile, onboarding, all forms, administration views | Plain. No illustrations or decoration, lines instead of shadows, small radius, headings in the text typeface, identifiers (metering point) in a monospace typeface. |
| **Warm** | Informing and exploring: overview pages, energy data, messages, information about the LEG | Friendly. Display typeface for headings, soft cards with a light shadow, light tint of the accent, restrained illustrations allowed. |

* **Rule for new pages:** if the person enters, confirms or manages something, the register is Clear; otherwise Warm.
* On mixed pages the area where data is entered is Clear (login page: a small Warm welcome area and a Clear form). Controls never change between registers.
* **Density:** Clear for members and the public: 16 px base, controls at least 48 px high. Clear for administration views (staff, system administrators): compact, 14 px. Warm: 16 px.

## 3. Colours

### 3.1 Neutral Colours and Tokens

Tokens of the design package, defined in `portal.css` (light on `:root`, dark in `:root[data-theme="dark"]`). Tabler's `--tblr-*` variables are mapped onto them. Dark applies only when the person chooses it with the button (section 7).

| Token | Light | Dark | Use |
| ----- | ----- | ---- | --- |
| `--bg` | `#F6F7F8` | `#111315` | Page background, right panel of the entry pages |
| `--panel` | `#FFFFFF` | `#191C1F` | Inputs, corner buttons, header and card surfaces |
| `--fg` | `#1B1D20` | `#E6E9EC` | Text |
| `--muted` | `#5A6068` | `#9AA2AB` | Secondary text, borders of inputs and buttons, footer |
| `--line` | `#DEE2E7` | `#2A2F35` | Dividers and borders |
| `--accent` | `#005BA2` | `#6AA9E0` | Primary button, links, focus, selected language, active navigation |
| `--on-accent` | `#FFFFFF` | `#0B1520` | Text on the accent |
| `--yellow` | `#DDDD22` | `#DDDD22` | Dots of the benefit list, dots of PV plants on the map of the page Nachbarschaft (D-123); never text (R9) |
| `--bad` | `#B42318` | `#F0948A` | Errors, status "problem" |
| `--ok` | `#2F8F5B` | `#5DBB86` | Status "done", success (always with a word; 4.04:1 on white, so dot or icon, not text) |
| `--warn` | `#8A5A00` | `#E8B34A` | Status "open" and "not set up", warning alerts (K16) |
| `--warm` | accent 9 % over `--bg` | same | Left panel of the entry pages, hover of menu entries |
| `--logo-plate` | transparent | foreground 8 % over `--warm` | Plate behind the logo in dark mode (radius 6 px, padding 4 px 8 px) |

### 3.2 Instance Branding

* The platform is neutral. Each instance configures its branding, never hard-coded (`AGENTS.md`, section 6): logo (positive for light, negative for dark backgrounds), accent colour for light and dark mode each with its text colour, optional highlight colour (`--yellow`).
* Platform defaults are the values of the design package (D-056): accent `#005BA2` / `#6AA9E0`, text on it `#FFFFFF` / `#0B1520`, highlight `#DDDD22`. Without a logo the instance name is shown as text.
* **Where branding is changed (D-073).**
  * Administration, "Inhalte", "Portal-Einstellungen" (an ordinary right of a role): name, website, the two logos (PNG or SVG, both or none), favicon (PNG or SVG, optional), accent and text colours, highlight colour, privacy and imprint addresses.
  * Precedence per field: administration, then environment (`INSTANCE_*`: `docs/INSTALLATION.md`; the imprint address has none), then platform default. Email texts: "Mailtexte" (D-039). Page texts: `locale/de_CH/LC_MESSAGES/django.po`. Server settings: environment only (`docs/OPERATIONS.md`, section 5a).
  * Under every field with a server value the page shows "Serverkonfiguration: <value>".
  * The preview changes colours while typing (CSS variables through the CSSOM by an external script; the CSP is not widened, so an unsaved logo has no preview). Removal and reset are checkboxes; an empty text field resets that field.
* **Rules for instance values.**
  * Text on the accent, and the accent as text on panel and page background, reach at least 4.5:1 (WCAG 2.2, 1.4.3). Checked at start against `#FFFFFF`, `#F6F7F8` (light) and `#191C1F`, `#111315` (dark), and again when saving. An empty text colour next to an administration accent is chosen automatically (white or `#0B1520`).
  * The accent must not be confusable with the error or energy colours (section 3.4); red tones are not permitted (manual check by the operator).
  * The highlight is never text or chart colour. The accent is for buttons, links, active navigation and focus, never charts.
  * Validation: `docs/SECURITY.md`, section 6 (D-073): colours `^#[0-9A-Fa-f]{6}$`, absolute `https://` URLs, environment logos inside the instance static files, uploaded logos PNG or SVG; validated at start, on save and on read; invalid values are never used.
  * Values reach CSS variables without `'unsafe-inline'`: a stylesheet generated from the validated values, served as a file (`instance.css`, ETag, `no-cache`).

### 3.3 Initiating Instance: LEG Ittigen

* The accent, highlight and logo values of the LEG Ittigen (`LEG-Ittigen_pos_rgb.svg`, `LEG-Ittigen_neg_sw.svg`, from the owner) are the platform defaults (D-056).
* Logo files are instance data, not in the repository. Use them unchanged: never recolour, redraw, crop or stretch.
* Logo sizes: 360 px wide on the entry pages, 40 px high in the phone top bar, 48 px in the header after the login, 32 px in the phone menu.

### 3.4 Energy Colours

Fixed meaning, independent of the instance accent; used by the later energy pages. Open: verify the contrast on `--panel` when first used.

| Meaning | Light | Dark |
| ------- | ----- | ---- |
| Production | `#B7791F` | `#E8B34A` |
| Used within the LEG | `#2F8F5B` | `#5DBB86` |
| Grid electricity | `#8A817B` | `#8A817B` |
| Consumption | `#7B4FA8` | `#B394E0` |

### 3.5 Status Indicators

* A status is always a dot plus a word, never colour alone (WCAG 2.2, 1.4.1). Dots: active or completed `--ok`; open, pending or not set up `--warn` (K16); problem `--bad`; inactive `--muted`; new the accent.
* **In progress (D-097):** a spinner (CSS only, `aria-hidden="true"`) is always followed by words that say what goes on and what the person can do.
  * The box is a polite live region (`role="status"`).
  * A failure is a message with `role="alert"` that names a fixed reason.
  * Under `prefers-reduced-motion: reduce` the spinner stands still (`animation: none`).

### 3.6 Contrast

* Required (WCAG 2.2): 4.5:1 for text, 3:1 for graphical objects and borders of fields and buttons (1.4.3, 1.4.11). Computed by `leg_platform.branding.contrast`; `test_design_assets.py` checks the token pairs against `portal.css`.
* Known low pairs, never for text: `--ok` on white 4.04 (dot or icon only); the yellow dot on the warm panel 1.18 (decoration only).
* Contrast alone does not make a page conformant. Keyboard operation, screen reader support and zoom are verified separately.

## 4. Typography

| Role | Typeface | Licence |
| ---- | -------- | ------- |
| All text, controls, headings, customer number | Source Sans 3 (400, 600, 700) | SIL OFL 1.1, Reserved Font Name "Source" |
| Title in the warm panel of the entry pages only | Bricolage Grotesque (700) | SIL OFL 1.1, no Reserved Font Name |
| Recovery codes and the manual key of the authenticator app only | IBM Plex Mono | SIL OFL 1.1, Reserved Font Name "Plex" |

* Fonts are served by the instance in fixed versions, never from a CDN (`docs/SECURITY.md`, section 6; `docs/SERVICES.md`). Unmodified, each with copyright notice and licence text (OFL 1.1, condition 2). A modified version (e.g. a subset) must not use the Reserved Font Name (condition 3).
* **Font sizes (K6):** 11, 12, 13 px (micro labels only); 14 px (hints, footer row); 16 px (text, labels, controls); 18, 20, 22, 24, 26, 32 and 40 px (title of the warm panel; 30 px on a phone). No other sizes (no 15 px).
* Controls are at least 44 px high on a desktop and 48 px on a phone (R5, K5).

## 5. Shapes, Spacing and Icons

* Corner radius: buttons and fields 8 px, cards and tiles 12 px, logo plate 6 px.
* No shadows and no gradients (R7). Exceptions: the floating language menu and the floating suggestion lists and notice cards (D-097, D-111; `--panel`, 1 px `--line`, radius 8 px; black shadow in dark mode).
* Lines (1 px `--line`, 1.5 px `--muted` for fields and buttons) separate surfaces.
* Spacing only from 4, 8, 12, 16, 24, 32 and 48 px (R3).
* Focus is always visible: 2 px accent outline with 2 px offset. A field in focus has a 1.5 px accent border and a 3 px ring of 30 % accent.
* Icons: line style, stroke 1.8 px, round ends, 20 px (Tabler Icons, MIT, or own SVG), served by the instance. Icons accompany text; an icon without visible text has an accessible name. No emoji (R15).

## 6. Navigation and Layout

* **Login page (D-106).** Keep the left panel, corner controls and legal footer. The Clear right column is at most 400 px wide. Order: heading, email, password, one native "Stay signed in" checkbox, three full-width actions (password primary, passkey outlined, registration outlined), one "Trouble signing in?" link.
  * Spacing: heading to fields 24 px; label to input 8 px; field groups 24 px; password to checkbox 16 px; checkbox to action stack 16 px; actions 12 px apart; help 16 px below; legal footer 24 px below. Buttons at least 48 px high, radius 8 px. Side padding at least 16 px; recovery links have 44 px touch targets.
  * Separate authentication forms. The password form owns `remember_me`; the passkey form references the same checkbox ID and copies its value at submission start into JSON `stay`. Empty password/email fields must not block passkeys. Never reset the preference on errors or cancellation. Passkey submission stays disabled until the self-hosted script detects browser support. An empty live-status region occupies no space. A message above the heading keeps 16 px of space (`.login-card > [role="status"]`).
  * `/login/help/` is anonymous GET-only navigation in the entry shell: heading, question, two full-width outlined choices ("Forgot your password?" to `/password-reset/`, "App or passkey unavailable?" to `/recovery/`), a recovery clarification and a back link. No email field, no account lookup, no mutation.
  * The recovery form asks for the address only. The answer is the same page for every account (D-106); its action is "Send recovery instructions".
* **Pages without a session (`entry_base.html`):** login, registration, password reset, second step of the login, lockout and result pages.
  * Above 760 px two equal columns: the warm panel (`--warm`, padding 48 px, logo 360 px wide, title in Bricolage Grotesque 40 px, lead 18 px, two points with a yellow dot) and the form (at most 400 px wide), separated by a 1 px line.
  * At 760 px and below the panel shrinks to a top bar of at least 64 px with the logo (40 px high). The text is hidden and the login fits one screen.
  * The footer row "Datenschutz | Impressum | host of the website" sits under the form (D-056).
* **Corner buttons on every page** (also error pages): language menu (DE, FR, IT, EN; a form, works without JavaScript) and light/dark button (needs JavaScript), round, 40 px, fixed top right (in the header after the login, next to the name).
* **After the login (K1, D-057).**
  * Fixed header 64 px: logo 48 px high, name as text, corner buttons.
  * Desktop: fixed left menu 232 px wide. Structure by access (D-057, D-105, D-113; `anmeldungprozess/MENU-STANDARD.md`):
    * Every account: MEIN BEREICH: Start, Profil. An account that takes part (staff included) also: Teilnahme (always open: Gebäude, Startdatum, Nachbarschaft), Information (Gesellschaftsvertrag, Frage und Antwort, Veranstaltung, "Kontakt zur LEG" last, only where the instance has a website, with the new-tab sign). DEMNÄCHST: Rechnung, Energie, Mitteilung (singular, mark "später", no links; the staff area uses the plural).
    * Parents are no links. An entry whose page does not exist yet is "später" without a link. The contract has ONE entry, "Gesellschaftsvertrag" under Information (D-108); the contract page shows the current contract in a bordered frame (embedded PDF, hidden below 760 px where the download button is the way to read it), the download below, the versions after it.
    * Below a line: "Ausloggen" as a POST form (msgid "Log out"; "Abmeldung" is the deregistration), Datenschutz, Impressum. This foot is sticky at the bottom of the menu and of the drawer.
    * The current page: `aria-current="page"`, bold accent, `--warm`, 3 px bar. Entries 14 px, 44 px high.
  * Phone: bottom bar 72 px, following the workspace, not `is_staff` (D-113): "My area" Start, Teilnahme, Information, Profil for an account that takes part (`START-SEITE.md` section 7); without a customer number Start, Profil, Menü (no link to a page that answers 404); staff area Mein Bereich (arrow, back), Übersicht, Vorgänge, Menü. The "Menü" tab opens the same drawer as the header button (290 px from the left, with an overlay); the focus returns to the tab. Escape and the close button close the drawer; focus is held inside and returned to the opener.
  * Content at most 960 px wide, padding 32/24 px desktop and 20/16 px phone, page title first. Menu and header measures: D-057.
  * The start page has the greeting, the customer number with the address, and the task tiles (hiding is final and stored on the server) or an empty state (panel with 1 px line, 12 px radius, at least 360 px high from 761 px).
  * An account that must change its password, or whose mandatory second factor is incomplete, gets the header and "Abmelden" only.
* **Two workspaces (D-113).** Staff and system administrators take part like every account and also work in the staff area. The two places are told apart without a new colour, shadow or size.
  * **Rule.** The workspace follows the resolved view name: `management:*` and `admin:*` are the staff area, everything else (start, profile, passkeys, password, `customer:*`, deregistration) is "My area". `context_processors.frame_workspace` gives `"participant"` or `"staff"`; bar, menu, header and title read it, never `is_staff`. The Django administration has its own shell and gets the same signs.
  * **"My area" menu.** For a management user the block FÜR MITARBEITENDE stands directly after Profil and before Teilnahme (the sticky foot would hide it at the bottom): one entry "Mitarbeitenden-Bereich" (14 px; second line "Verwaltung und System" or "Verwaltung", 12 px `--muted`, then the new-tab sign `#i-external`, 16 px, `aria-hidden`), hidden text "(öffnet in neuem Tab)", `target="_blank" rel="noopener noreferrer"`, at least 52 px high; the block ends with a 1 px `--line` and 12 px. On a phone the start page has the full-width secondary button "Mitarbeitenden-Bereich öffnen" (48 px, same target) for a management user, under the greeting and above the banners.
  * The menu and the drawer have `scroll-padding-bottom: 136px` so a focused entry is never hidden by the sticky foot (WCAG 2.4.11); the foot covers that padding strip with the panel colour (`.frame-menu-foot::after`, 24 px, 16 px in the drawer). Accepted: at a window height of 800 px the entry "Systemverwaltung" lies below the foot until scrolled or focused.
  * **Staff area menu.** First "Zurück zu Mein Bereich" (arrow `#i-arrow-left`, second line "Teilnahme, Information und Profil", or "Start und Profil" for an account that does not take part; same tab), separated by a 1 px `--line` and 12 px; then VERWALTUNG (`management.navigation.MENU`), SYSTEM ("Systemverwaltung") only where `system_administration_visible` says so (D-102), then DEMNÄCHST (plural) last. No Start, Profil, Teilnahme or Information.
  * **Sign of the staff area.** `.frame--staff .frame-header`: `--warm` background, border-box, height 64 px; a 4 px `--accent` bar on top as a `::before` pseudo-element, not a border (nothing moves down). Behind the logo the chip "Mitarbeitenden-Bereich" (14 px, 600, `--accent` text, 1 px `--accent` border, radius 8 px, padding 4 px 12 px, no wrap; on a phone it may shrink with an ellipsis and is hidden below 400 px). In the drawer the head holds the logo and the close button only; the chip has its own row below (`.frame-drawer-brand`; readable at 360 and 390 px). The chip is text, not a control; the place is told by word, line and bar, never by colour alone. The tab title is "Mitarbeitenden-Bereich: <page> – <name>" in the staff area (filter `staff_page_title`), unchanged in "My area".
  * **Administration** (`templates/admin/base_site.html`, `admin-portal.css`): the title with the same prefix, the chip `.admin-workspace` in the header (colour and border `--header-color` of the admin, about 4.9:1), the links "Verwaltung" (management overview) and "Mein Bereich" (start page, same tab) instead of "View site"; at `pointer: coarse` and up to 767 px every header link is at least 44 px high and underlined, and the admin theme button is a 44 px target.
* **Registration (D-058):** three steps with a 4 px step bar (classes `step-fill-1` to `-3` because of the CSP) and the muted label "Schritt N von 3" (14 px) above the heading.
  * The details step shows the address as plain text with the status "Bestätigt" (K7: no box). Street and house number (96 px), postal code (128 px) and town sit side by side, also on a phone.
  * Hints under a field are 14 px. A field with an error has a red 2 px border and one message. The error summary uses short field names and is focused.
* **Choice cards, the sign-in method (D-106; Clear register).** One `<fieldset>` whose `<legend>` is the question (visually hidden where the page has the same words as its heading), one 14 px hint and one card per method, always stacked (400 px entry column). One component everywhere (`accounts/_login_method_choice.html`): registration step 2, profile card, flow "Anmeldung ändern", page after a recovery, first login of an administrator. No explainer above the cards, no helper list, no recommendation sentence.
  * A card is a `<div>` with a native radio (24 px), at least 48 px high, 1 px `--line`, radius 12 px, no shadow. The label (title 600 and one sentence) selects the radio; "Empfohlen" and "Aktuell" (with the status dot) are words in the card head. Under the sentence a native `<details>` "Mehr dazu" (44 px high, accent colour, no script) opens, 14 px: what it is, "Sie brauchen:", "Wenn Sie es verlieren:" and, for an existing account, "Was passiert?". The sentence is linked with `aria-describedby` on the radio; "Mehr dazu" only opens and never selects.
  * The selected card has the filled radio and a 2 px accent border (never colour alone); the focus ring is a 2 px accent outline with 2 px offset on the card (`:has(:focus-visible)`). Order is always password only, password and app, passkey; a recommendation does not reorder.
  * The page supplies the form, the error summary and one "Weiter" button; the cards come from `login_methods.cards(user)` (`registering=True` leaves out "Was passiert?"), never from the template.
  * **Passkey pages and wizard (D-106, D-112; `LEG-Design-Vorgaben/profil/umsetzung/KAPITEL-PASSKEY.md` chapters 8 to 11).** Pages in the frame (kicker "Mein Konto · Profil"), Clear register, `step-card`s in the look of the building wizard (`customer.css`): "Weiter" primary, "Zurück", "Abbrechen" red outline at the right (a POST); stacked and full width on a phone.
    * Setup: a pre-step without a bar (the one proof of D-062/D-106), then "Schritt 1 von 3: Name" (no preset value), "Schritt 2 von 3: Gerät" (two choice cards; "Auf diesem Gerät" preselected, but "Ein anderes Gerät" for a further passkey), "Schritt 3 von 3: Bestätigen" (browser ceremony; on failure or cancel the screen "Nicht geklappt. Es wurde nichts geändert." with retry and back), then, for a first passkey and recovery only, the activation confirmation (one red warning), then the result. A further passkey is activated at once after the ceremony (D-112). Every exit leads to the profile.
    * Creation stages the key; a separate completion activates it. One passkey suffices; a second is strongly recommended; no device-statement checkbox. The portal makes no statement about the physical device and no synchronisation claim.
    * Page "Passkeys": h1, one lead sentence, one card per passkey (name, "Erstellt am", "Zuletzt benutzt" or "Noch nie benutzt"; red outline "Entfernen" only with two or more passkeys, otherwise the sentence that it is the only one), buttons "Weiteren Passkey einrichten" (primary) and "Zurück zum Profil". "Entfernen" opens its own confirmation screen that names the consequence and that all sessions end.
    * Profile card "Anmeldung" of a passkey account: status "Passkey", "Passkeys verwalten" (the one primary button), "Weiteren Passkey einrichten" (the one secondary button), a 44 px link "Anmeldung ändern". It shows no password mask and no password form.
    * Dates of a passkey card: "23. Oktober 2026", with time on "Zuletzt benutzt". Quotation marks around a name: «Name» in de_CH and it, “Name” in en, « Name » in fr.
    * Recovery: stays in user detail settings; the confirmation names the irreversible effects; the recipient POST names the removal and uses the destructive style.
    * Open: the confirmation before the activation for a first passkey and recovery, the recommendation text of the result (D-106), removal proof (a fresh passkey, no email confirmation) and renaming (none) are provisional until the owner decides (specification chapter 11). Not yet seen in a real browser with a real passkey provider; independent recheck open.
* **Profile (D-059, D-105, D-106; `navigation/VORGABEN.md` section 3.3, `kundenprofil/PROFIL.md` section 2):** "MEIN KONTO / Profil" with cards on the page background (1 px line, 12 px radius, no shadow): Persönliche Angaben, E-Mail-Adressen, Passwort, Anmeldung (`id="card-two-factor"`; for a password account: heading, status word with a dot, one sentence and the one button "Anmeldung ändern", which opens the flow below; "Neue Codes erzeugen" stays for an account with a number code), and the red start card Konto löschen.
  * A customer has a further card "Bank- oder Postverbindung" directly after Persönliche Angaben: the IBAN in full in IBM Plex Mono groups of four, status "Hinterlegt" / "Nicht hinterlegt" (dot plus word), "Ändern" opens the field with the hint in the same card, no proof field. The hint starts with "Only needed if you feed in solar power."
  * The address line shows "· EGID n" and, after the staff confirmed the building, the grey note "Die Adresse ist bestätigt und kann nicht geändert werden. …" (address fields disabled, grey, the note directly under them). The email card and the personal card say "Ändern" (not "Bearbeiten").
  * **End participation or delete account (D-127).** The card "End participation or delete account" has one sentence and a button (red outline) to the fork `/profile/end/` (a customer who takes part) or straight to `/profile/delete/`. The fork has no red: option A "Deregister metering points only" (primary; the account stays), option B "Delete the whole account" (secondary), "Not sure? Contact the LEG", "Cancel". Error summary on every profile card (field names only). The confirmation of an account that is only deactivated says "Ihr Konto wird deaktiviert. …".
  * A card shows its values with a button "Ändern" that opens the form **in place** (server-rendered state `?edit=<card>`, works without a script, one card open at a time, errors re-render the same card with one message per field). Extra fields are under "Weitere Angaben (optional)".
  * **Flow "Anmeldung ändern" (`LEG-Design-Vorgaben/profil/umsetzung/KAPITEL.md` chapters 1 to 4; password accounts, with or without a number code).** A page of its own in the frame (kicker "Mein Konto · Profil", h1 "Anmeldung ändern", no lead), one `step-card` per screen in the look of the building wizard (bar and "Schritt n von m: …" inside the card; "Weiter" primary, "Zurück", "Abbrechen" red outline right, a POST; stacked and full width on a phone). Screens:
    * 1 "Bitte bestätigen Sie sich": the one proof of D-062 (only the password field, or only "Zahlencode vom Handy", placeholder "000 000"; no recovery code); a 44 px link "Handy verloren?" leads to the phone-lost way.
    * 2 "Wie möchten Sie sich anmelden?": the shared choice cards, the tag "Aktuell" from the server, options from `login_methods.cards`.
    * 3 by choice: "Keine Änderung" (button "Zurück zum Profil"; no bar), "Zahlencode abschalten" (no bar), or the two-screen setup of the number code (Schritt 1 von 2: QR code, 200 px on a tile with 1 rem margin; Schritt 2 von 2: the number). The choice "Passkey" has no screen of its own (D-112): "Weiter" leads into the passkey setup, whose confirmation screen carries the warning about the deleted password.
    * Recovery codes after the number code are a step card of the flow (heading "Wiederherstellungscodes", list, "Codes kopieren", the box "Ich habe die Codes gespeichert.", one "Fertig"), shown once in the answer to the confirmation (nothing is stored, D-059).
    * Errors of the proof and number step show inline at the field (the field is described by it and has the focus), no summary box. "Zurück" on the choice leads to the profile and asks for no new proof (the grant stays until it expires or the flow is started again). After the change the person is back on the profile with the updated card; a flash message only where one existed (turning the number code off).
    * Choice title 18 px bold in step cards; the checked card is lightly tinted; the placeholder is muted.
  * Open (specification chapter 7, "OFFEN"), provisional: no tag "Empfohlen" in this choice (the shared pages `_login_method_choice.html` keep theirs until the owner changes the D-106 recommendation); no brand names in the number-code explanation ("Authenticator-Apps", "App-Store oder Play-Store" only); security levels not shown; card statuses "Nur Passwort", "Passwort und Zahlencode", "Passkey" and the sentence "Sie melden sich mit Ihrem Passwort und einem Zahlencode vom Handy an." are proposals; no confirmation text after a change except "Der Zahlencode ist abgeschaltet.".
  * Deletion (page `/profile/delete/`): danger styling, the sentence that the account is deleted at once and open invoices stay valid, the proof, a box never ticked in advance, a red button "Delete account permanently", the link "Just deregister metering points instead" and "Cancel".
* **One proof per confirmation (D-062).** The second-factor code if one is active, otherwise the password, never both (`docs/SECURITY.md`, section 3).
  * The proof field takes the place and style of the password field (48 px high, radius 8 px, semibold 16 px label, 8 px between label and field).
  * With the code, a 16 px lead hint (`.profile-text`) stands between the label "2FA-Authentifizierungscode" and the field. The code field is a plain text field (no `inputmode`, `maxlength` or `pattern`). Screen reader description order: lead, error, note.
  * "Handy verloren?" is a text link under the code field. It opens a state of the same card (also without a script) with the password and the recovery code, a text for people with neither (they go through the operator) and the red outlined button "2FA deaktivieren".
  * All password fields are hidden by default with the eye button. The way back is the secondary button "Abbrechen" in every card.
  * After five wrong entries the session ends and the login page says so (D-062).
  * Images of the owner's package with "Aktuelles Passwort" are valid only for an account without a second factor; the state with a second factor has no image and is described here.
* **Links from emails (D-058, D-067):** an invalid, expired, used or replaced link redirects (302, same answer for every cause) to a fixed page of its flow, as does a confirmation page whose session state is gone. The fixed page carries the message and the next step, is identical for everybody, uses no flash message and is an entry-shell page without header or logout form.
* **Address suggestions and notice (R-04, R-05, R-21; D-065, D-066, D-111; `static/css/address-suggest.css`, `static/js/address-directory.js`).**
  * Suggestions are a one-line-per-address overlay under the street field: out of the flow, 4 px below the input, over the whole address row (street and house number), `z-index` above the fields below, shadow as in section 5. Opening or closing it never moves a field or a button.
    * One line per address: street and house number bold, then ", postal code town" muted (16 px, 6 px vertical padding, about 33 px high: an approved exception to the 44 px touch rule for text links of this list). Head: one slim 14 px line ("Matching addresses: choose one." or "Similar addresses: choose one."; one line at 320 px in every language). At most 7 rows (`MAX_OPTIONS`), no scrollbar; the foot "More matches: keep typing." only when more addresses match than rows are shown; nothing is loaded further.
    * Never inserted by a blur; replaced or hidden only when an answer arrives. ARIA 1.2 combobox on the input (`aria-activedescendant`; the list is a listbox of options; the active option `--warm` with a 2 px accent ring inset); the focus stays in the input. Arrow down opens and moves, arrow up moves (and back to the text), Enter takes the active option (only then it does not send the form), Escape closes and keeps the text, Tab closes. A polite live region says how many there are.
    * It closes when the focus leaves its field (the street list stays while the focus is in the house number) and on a press outside it. After a click on the card the focus returns to the address field used last.
  * The same rules for the postal code (four digits: the town or towns) and the town (three characters: the postal code or codes): an overlay under the input, titled "Choose a town" or "Choose a postal code", with the same foot; one answer goes into an empty field and is announced ("Town filled in: …"). Text the person typed is never replaced unasked.
  * Everywhere an address is edited (registration details, profile, customer wizard, management persons, locations, building correction, LEG settings, admin change forms for accounts, locations and the settings row): one widget and one script, never in read-only views. Choosing a suggestion fills street, house number, postal code and town; that and the two fills above are the only ways the script changes fields.
  * One non-blocking warning notice after the postal code and town row: "This address was not found. Please check street, house number, postal code and town. You can still save it as entered." Only for a complete address while editing and only when the directory is available. It hides on any input, never takes the focus and never blocks saving.
  * Where a fix can be computed, the notice is the card "Did you mean this address?" (amber, as the warning): the sentence that names what does not fit, once the hint "Click an address to use it", one button per complete address (street and house number bold, "postal code town" below; at most 3), and the link "No, my address is correct". Without a fix the warning gets the link "My address is correct, use it as entered".
    * Above 760 px the card and the notice are an overlay under the row of postal code and town, 4 px under the input (shadow and `z-index` of the lists, black shadow in dark theme, no layout shift). Escape or a press outside closes it WITHOUT recording "No, my address is correct"; it comes back when a field changes and the next check finds a fix. A list or card that would end below the visible part is scrolled into view once. Accepted: until closed, the card covers the buttons below it (Save, Cancel); Enter still sends the form.
    * At 760 px and below (phone) the card stays in the page.
    * The card of the customer wizard after a submit is drawn by the server inside the same notice (`step_building.html`, `widget_notice.html` with `card_template`); the script does not draw a second one on load and clears it at the first input.
    * The check never runs on change or blur (a notice that appears then would shift the Save button).
  * **Status line in step 1 of the customer wizard (D-124).** There the notice is the status line `.address-status` under the postal code and town row, before the action row: in the flow at every width (never an overlay), a minimum height of 48 px so that nothing jumps, `role="status"` and `aria-live="polite"`, always in the page. One state at a time, always a dot and a word (colour is never alone; existing `.dotword .dot`, `tone-ok`, `tone-warn`, `tone-muted`):
    * grey "Checking the address …" only when the answer takes more than 400 ms (the check is debounced by 600 ms; a stale answer is dropped);
    * green "Building found" with "EGID 123" below;
    * the card "Did you mean this address?" in the flow (same content and the link "No, my address is correct");
    * yellow "To be checked by the LEG" with "You can continue. The LEG will check the address." for an address confirmed as typed, a directory that is not available, an address that is not clear and several buildings (the last one with its own title "Several buildings at this address").
    * There is no red state on this page. "Continue" is never inactive. Under the card of step 1 there is about 140 px of empty space so that the street list does not end below the page. On a phone the status line has the full width and "Continue" is above "Cancel".
    * After a submit that found the building or kept the address for review the server goes straight to step 2, which shows once the same state in a short notice (green "Building found" with address and EGID, or yellow "To be checked by the LEG"). The page "Building found" stays only as the fallback for what is not clear (several buildings, a fix to confirm).
  * **Building cards on the page Buildings (D-125).** One card per building; its metering point and plant data are in the card.
    * One button "Ändern" in the card footer (context "building card"), no pencil icon at any field. A click opens the card in place (`?edit=<id>`): the fields that may be changed become inputs where they stand, "Speichern" and "Abbrechen" take the place of "Ändern". Values that cannot be changed stay plain text (no grey, no lock icon, no hint). There is no edit dialog and no second page.
    * Only one card is in edit mode. The "Ändern" button of every other card is a disabled button (greyed, no link) while it is open.
    * **Hide or disable.** What does not apply or is not allowed is hidden (no "Ändern" on an application the LEG is checking, no progress block on a completed registration, no bank row without a feed-in). What is only temporarily unavailable is disabled and keeps its place (the "Ändern" of the other cards).
    * The status stands at the top right of the card (dot and word, green "Teilnehmend" when participating; colour is never alone). A completed registration shows no progress block. A card in edit mode hides the progress block as well.
    * Rows: address, EGID, building name; metering points (number locked, name and, on a feed-in, PV power, battery and wallbox as inputs); "LEG-Name" (or "Noch nicht zugewiesen") and "Trafokreis" (locked, read from the LEG administration and the transformer directory).
    * The head button reads "Add new building" or "Add another building"; there is no page-level deregistration button. The card's own red outline link is "Deregister metering points" (`?building=`). A deregistration shows a heading and one status sentence (no progress bar). The submitted page says only "Thank you. We have received your application." and has one button "To my buildings".
    * **Fields and wording of the wizard (D-128).** A metering point number with fewer characters than the fixed length is an error ("Please enter all n characters."), never padded with zeros; no start date is prefilled (also in edit mode); the neighbourhood page without data says "No data yet".
    * **Several buildings fold.** With two or more cards each card is a `<details class="fold">`: the closed row shows icon, title, address, status (dot and word) and a chevron; the whole row is the button (focus ring 3 px, accent colour). A single card is always open and not foldable. All cards are closed, except the card in edit mode (also after a failed save, so the errors are visible) and the card just saved (one-time, by session). Works without script.
    * **Order.** Not yet participating buildings first, participating ones last; inside each group by the building name, otherwise by the address.
    * **Eight or more cards** (`overview.SEARCH_MIN_CARDS`): above the list a search field (name, street, place, EGID, metering point numbers; spaces ignored; "No buildings match your search." when nothing matches) and the buttons "Expand all" / "Collapse all". Script only (`customer/js/buildings-filter.js`, hidden without it, no inline script); the search text is the `data-search` attribute of the card and holds only what the card shows anyway. Fewer cards: nothing of this is shown.
  * Nothing else is said about the directory (no source, no text for an unavailable, old or limited directory) except to a staff account: a neutral (grey) notice says that there are no suggestions because the   directory is not available or not imported yet.* **Administration views:** dark vertical sidebar (Tabler vertical navbar with `data-bs-navbar-theme="dark"`), breadcrumb, page title with one primary action.
* **Menu order (D-097).** The left menu and the phone menu are one list (one template).
  * MEIN BEREICH (Start, Profil); VERWALTUNG (Übersicht, Vorgänge, Personen, Standorte, LEGs, Messpunkte, Einstellungen, then "Mailtexte" for an account with that right, then Verträge; without that right "Verträge" follows "Einstellungen", D-104); DEMNÄCHST (Rechnungen, Energie, Mitteilungen); last, the group SYSTEM with "Systemverwaltung" (own icon, not the gear of "Einstellungen"). In the staff area this follows "Two workspaces" above.
  * "Systemverwaltung" is shown only to an account that can use the administration: a superuser, or a staff account for which the administration lists at least one thing (D-102).
  * Below the line: "Abmelden", Datenschutz, Impressum. The entries "später" stay in the menu; the bottom bar of a staff account has none.
* **Lists (D-097):** the main text of a row (address, person, number, LEG name) is the link to its page; the small link icon stays.
  * Exception, list "Standorte" (D-110): the whole row (a card on a phone) is the click target (pointer cursor, `--warm` on hover and while the arrow has the focus); the address is plain text, the link icon at the right goes to the detail page (accessible name "Go to the details of the location"); the arrow is the indicator and keyboard button (`aria-expanded`, `aria-controls`). A click on a link, button or field inside the row, or one that ends a text selection, does not toggle.
  * On touch (`pointer: coarse`) and up to 760 px these are at least 44 px high: small buttons (`.btn-xs`), back links, filter chips, pager buttons, text links in hints, fact rows and under a field. A status is the dot with its word, also in phone cards.
* **Next-step banner (D-097):** after a create the new object's page opens with one banner in the neutral colour (`--line` border, `--warm` background; a hint, no warning) that says what happened and offers the next step. No plain "Saved." besides it. The warning colour stays for problems and address corrections.
* **Forms in the management area (D-097).** One form class and one template per subject, wherever used; create and change use the same class.
  * A context (page of a location, of a metering point) may only pre-fill and lock a field. It never adds, removes or reorders a field and never changes a label, hint or button.
  * Everything a person reads lives in the form (`layout`, labels, hints, `optional` marking, empty-choice wording, units, submit label), drawn by `dialog_form.html` and `_dialog_field.html`.
  * *Order and marking:* the deciding field comes first (location before number). Required fields are unmarked; optional ones say "(optional)"; the divider "Optional" separates them. A deciding optional field may come before the divider (the location of a metering point). Titles are "Create X" / "Edit X". The button names the action ("Create location", "Assign person"), never "Save" for a creation.
  * *Drop-down of existing objects:* a native `<select class="form-select">` whose first option is a real sentence ("No location chosen yet", "No LEG chosen yet"), not a dash. The same words for the same thing in every list ("no location"). On a phone a closed select keeps 36 px on the right for the arrow. Archived objects are not offered.
    * A "missing?" link under it opens the creating dialog and comes back with the new object chosen (the cursor goes to the next empty field).
    * The circuit list ends with "+ Create new transformer circuit…", which shows the two circuit fields under the list; circuit and location are created in one transaction.
    * **Consequence line:** where a choice has a consequence (a move changes the transformer circuit) a polite live line under the field names it for the chosen option (`NotedSelect`, `option-note.js`). It is a hint; the server decides.
  * *Locked field:* a read-only row (`--warm`, 44 px high) in the place of the field, not a disabled input. The value comes from the page's object on the server.
  * *Numbers:* a unit stands after the field (`%`, `kWp`, `kWh`, `kW`). The field is text (`inputmode="decimal"`), never `type="number"`, never `maxlength` or `max` (they cut or refuse silently). Digits and one decimal point or comma; a comma becomes a point on leaving the field (`12.5`, section 8). The metering point number (D-102) is text without `inputmode` and `maxlength`; it is made 20 characters long on leaving the field.
  * *Checks while typing:* one message under the field with a red 2 px border plus the text (colour is never the only cue), `aria-invalid` and `aria-describedby` (message, hint). One polite live region announces it a moment after the last key, never an alert per key. The script deletes nothing typed; it only normalises on leaving the field. The server checks again with the same messages and has the last word. Without the script everything works.
  * *Suggestion list (combobox, `combobox.js`; D-097, D-098)* replaces the browser's own list.
    * White panel (`--panel`, 1 px `--line`, radius 8 px, soft shadow) directly under the field, as wide as the field. Rows at least 48 px (two lines: name 600, muted second line). At most ten rows, about 336 px high, scrollable. Names that start with the typed text come first. Active row `--warm` with a 2 px accent ring inset.
    * One row says why nothing was found ("No person found. Only active persons with a customer number can be assigned.") and one says to wait after too many requests.
    * ARIA 1.2 (combobox, listbox, option, `aria-activedescendant`, polite live region with the count). Arrow down opens and moves, arrow up moves, Enter takes the active row (only then it does not send the form), Escape closes and keeps the text, Tab closes.
    * A tap takes a row (the click). After a tap the list stays closed and, on touch, the focus is not put back. Typing reopens it.
    * At least two characters, 120 ms after the last key, an older request is cancelled. After a 429 no request is sent until the time named by the server has passed.
  * *Cursor:* a dialog puts the cursor in its first field (a prefilled one at the end), only with a fine pointer that can hover. A field that is chosen already is skipped. After an error the error summary has the focus. A question without fields focuses "Cancel", never the red button. The lead sentence is the description of the first field.
* **Multi-step processes:** one step per page, a 4 px step bar with "Schritt 1 von 3", back and continue buttons.
* **Customer pages (D-105; owner design `anmeldungprozess/*`).** Existing frame, tokens, Tabler controls (48 px, 8 px radius), Clear register; stylesheet `customer/static/customer/css/customer.css`, one script `customer.js` (progressive enhancement). A state is a dot plus a word (`.dotword`, `.dot.tone-*`); a progress bar is a native `<progress>` (8 px, token colours, no inline style). The texts live in the templates and `django.po`; the rules are:
  * *Page "Gebäude":* label "MEINE TEILNAHME"; header buttons "Anmeldung starten" (blue) and "Abmeldung starten" (red outline, only with an active point), stacked on a phone. One card per building (1 px line, 12 px radius, 24 px padding) with heading, address, status, facts (EGID, Netzbetreiber only if the instance entered a name, Beschreibung; no EGRID: no verified source) and the own metering points in fixed columns (number in IBM Plex Mono; direction with "Seit"; status) that stack on a phone; a running process shows progress under its point. Footer "Messpunkt hinzufügen" (blue), "Messpunkt entfernen" (red outline). Empty: the empty-state card, no header buttons. A draft without a building has the card "Gebäude noch nicht erfasst"; every draft card says "Entwurf, noch nicht vollständig erfasst".
  * *Wizard "Anmeldung":* "Schritt n von m: Name" (the numbers count only the steps the customer sees: a step already settled from before is skipped, the bank step counts only with a feed-in), one card (at most 880 px), buttons under a line: "Weiter" (blue) left, "Abbrechen" (red outline) right; stacked on a phone. "Abbrechen" opens the card "Anmeldung abbrechen?" at the top of the page (red border; focus on "Weiter anmelden", never on the red button). Step 1 is prefilled with the account's address. Fixed beginning of a metering point number: grey, in the frame of the field (`.prefix-input`). Yes/no questions are two radio buttons; the part behind "Ja" is hidden only when the script runs. Hint 14 px; error red bold 14 px with the 2 px red border and the error summary on top (field names only).
  * *Page "Abmeldung" (`ABMELDUNG.md`, D-127):* width and cards of the wizard (880 px, `.step-card`). Title "Deregister metering points" (with `?building=`: "Deregistration: <building>"). Cards in one form: all points of all buildings as boxes grouped by building (a group heading only with several buildings), all ticked at the start; "Registration in progress" with an unticked box "Withdraw registration: <building>"; the date as two radio rows ("Earliest possible" without a date; "Desired date" reveals a select of month ends); then the summary, "The deregistration is binding.", the one proof field, the red outlined button "Deregister metering points (binding)" (inactive until a point or a withdrawal is chosen and the proof is typed; while the proof is missing the hint "Enter your password to finish." shows) and a neutral "Cancel". A read-only card "Already in deregistration" follows below the form. A point that is not active yet looks like the others. The result page says the account stays.
  * *Start page:* the card "Ihr Weg in die LEG" above the tiles: one list for the whole way (before the submission the wizard's steps; after it the five steps of "Ihre Anmeldung ist abgeschickt"); a paused draft carries a persistent note with the day it is deleted; the next step as one sentence and one blue button. Tiles "Gebäude anmelden" (a POST form, straight to step 1 where nothing is on file) and "Einspeisung nachmelden" ("Optional", grey dot) follow `START-SEITE.md`. Without a card, one or two status lines stand under the greeting. One-time banners (`.banner-list`, warning tone, `role="status"`) after a rejection or a lapse, shown once; the staff note is plain text.
  * *Submitted registration:* one red outlined "Anmeldung zurückziehen" and, until the LEG takes it into review, a secondary "Anmeldung ändern"; afterwards a muted note. A registration shows "Angemeldet am", a running point "Seit", a start later no line; an empty description is a grey dash.
  * *Words:* "Startdatum" (not "Aufschaltung"); page label and title say "Anmeldung"; the word "LEG" in a sentence and the entry "Kontakt zur LEG" link to the instance website (only where configured; new tab); the log-out entry is "Ausloggen".
  * Open: the wording of the next-step sentences except the contract, the dots of the steps of a deregistration, the banner "paused" and the placeholder of the deregistration are developer proposals not in the owner's documents. Owner to confirm: "Ändern" on the personal profile card (deviates from `PROFIL.md`).
* **Processes, contracts, reminders in the management area (D-104, D-109; owner design `verwaltung/vorgaenge`, `vertraege`, `SEITEN-TEMPLATE.md`).** Same frame, tokens and list framework as the other management pages.
  * *Overview:* two cards never added up: "Zu erledigen" (queues of the onboarding; each count equals the rows of the filtered list it links to; only a queue with entries has a row) and "Probleme". Drafts never appear. The name of a row is the link (44 px high); the 24 px chain icon beside it is decoration (out of the tab order).
  * *List "Vorgänge":* one table for applications and deregistrations. Status as dot plus word (waiting `--warn`, current `--accent`, confirmed `--ok` ring; a draft or paused draft "Entwurf / nicht eingereicht" / "Pausiert / nicht eingereicht" in the muted tone: read-only page, no button, D-107); progress a native `<progress>` with a text "2 von 5 Schritten"; an unknown value is the grey italic word "offen", never a dash. Default sort: oldest change first. Phone: one card per process. A status word is never cut. The start widths fit the card of a 1280 px screen without scrolling.
  * *Page of an application / deregistration:* back link once at the top, name as title, status at the right, a progress card (the five steps and words of the customer's page), a "Nächster Schritt" card (primary blue for the next step, outlined for others), the five steps one under the other (no wizard), a side column with person, notices and history (equal entries in a row are one line "× 3"); at the end the red outlined "Anmeldung rückabwickeln". Steps are dialogs (normal pages) with an action as title and button; a step that removes the page of the application leads to the list with a message.
  * *Metering points (D-109):* blocks, not a table: first line the number (`.mp-number`, filter `metering_point` on every page that shows a number: IBM Plex Mono, tabular figures, one line, first block of 13 characters then blocks of four, no-break spaces), the direction badge, the link icon and the small outlined "Bearbeiten" (only while the LEG works on the application; accessible name starts with the word and names the number); below, labelled facts "Name" and "Anlagedetails" (definition list, label muted 14 px, tabular figures; an empty figure has no line). A new building has a small outlined "Adresse bearbeiten"; a stored building says its address is changed on the location page. The dialogs follow the dialog rules above (prefilled; number with prefix, check while typing; direction as the segmented control; three plant figures with units under the divider "Anlagedetails"); an address the directory does not know shows the amber banner with the suggestion and "Wie eingegeben speichern".
  * *Dialogs:* "Anmeldung abschliessen" is filled with the customer's wish (never before the earliest date); the confirmation of the addresses is an unticked box "Adressen sind geprüft und werden gesperrt" (only while a building is not confirmed). "Termin bestätigen" is filled with the wish and names the earliest date. "Zur Korrektur zurückgeben" has the required reason (500 characters); "Rückabwickeln" has the reason choice and an optional note (300 characters).
  * *List "Verträge":* icon buttons of 44 px with an accessible name that starts with the visible label and names the file, and the short label as a bubble on hover and keyboard focus (always beside the icon on a phone): download (neutral outline), set as current (accent outline; asks first), delete (red outline; asks first); an action that is not allowed is not drawn. "Neue Fassung hochladen" is a blue text button, top right. No link column.
  * *Settings:* the block "Adressverzeichnis" (time of the daily run, own button) under the main form; the chapter "Erinnerungen und Fristen": one block per deadline (name, trigger, recipient, mail text, effect, days field), its own button; one vocabulary (a draft is paused, then deleted). A change that would delete drafts asks once more (old and new days, number of drafts, customer is emailed, cannot be undone) with the red button "Speichern und n Entwurf löschen".
* One primary button per page (R1). Destructive actions use the outlined error style and require confirmation.

## 7. Colour Modes and Print

* **Light and dark only by the button (R13, D-056).** No automatic mode, no `prefers-color-scheme`. The server always renders light; the person switches with the sun/moon button. The mode is set on `<html>` as `data-theme` (own tokens) and `data-bs-theme` (Tabler), always the same value.
* **Persistence of the colour mode (K3).** The choice is kept in the tab's `sessionStorage` (no cookie, nothing sent to the server; a new tab starts light).
  * The scripts are external files (`theme-init.js` in `<head>`, `corner-controls.js`). They read only the allowlisted values `light`, `dark`, evaluate no URL parameters and never set branding attributes.
  * Without JavaScript the sun/moon button is hidden and the page stays light.
* **Languages (D-056).** The menu offers Deutsch (`de-ch`, the instance default), Français, Italiano, English. Every item is a submit button in a small POST form to `set_language` (`/language/`), which sets the functional language cookie and returns to the page (Referer, checked). Every text follows (templates, forms, messages, administration, JavaScript catalogue). The button shows the active language (DE, FR, IT, EN); the item is marked `aria-current`; `<html lang>` is `de-CH`, `fr`, `it`, `en`.
  * Without JavaScript the menu still opens (hover and focus, CSS) and switches. The script only adds the tap, Escape and the arrow keys.
  * The language comes from the cookie only, never from the browser's `Accept-Language`. A text without a translation is English, never German. The 500 page has no language menu (no request, no CSRF token).
  * The cookie holds only the language code (functional, no consent needed; `docs/SECURITY.md`, section 6).
* Tabler's `tabler-theme.js` is not used: it reads URL parameters for all theme keys and persists them in `localStorage`, so a crafted link could override the instance branding persistently (Tabler `core/js/tabler-theme.ts`).
* In dark mode the negative logo is used, on the plate of section 3.1.
* Print: a print stylesheet hides navigation, corner buttons and controls, prints black text on white, and shows the instance name and the print date.

## 8. Formats and Tone

* Numbers with the Swiss thousands separator (`112’400 kWh`), amounts `CHF 61.40`, tariffs `Rp./kWh`, dates `11.10.2026`, customer number in two blocks of three digits (`482 915`, D-021).
* Swiss German texts use the formal address, short and plain sentences. Buttons name the action. Error messages state what is wrong and how to fix it.
* Security notifications (D-068): plain text with the link alone on its line.
  * First line "Guten Tag Vorname Nachname" for an account of a person with a first and last name, otherwise the neutral "Guten Tag". No comma, no Herr/Frau/Familie, no title.
  * Neutral also for: a company-only account; a name with a character a name does not have; a name that looks like an address; an address that does not currently belong to the account (former login address, removed second address, deleted account).
  * The start page keeps "Guten Tag, Name".

## 9. Implementation in Tabler

Changing primary colour, base or radius can change contrast and must be re-checked (section 3.2). Re-check against the Tabler version chosen under D-015.

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

* Logo files and their serving headers: `docs/SECURITY.md`, section 6 (D-073). SVG logos are embedded only as `<img>`, never inlined (W3C SVG Integration: an SVG in `<img>` runs no scripts, one opened directly can). Static SVG logo files are checked for scripts and external references before deployment and served with a restrictive CSP by the web server.
* Open (D-073): whether an SVG with `style` attributes renders in `<img>` under the sandbox policy is not checked in a browser.
* Footer legal addresses come from the portal settings or the instance configuration and are checked again before printing (`docs/SECURITY.md`, section 6).
* No third-party fonts, icons or stylesheets. The colour mode cannot be changed through URL parameters (section 7).

## 11. Open Points

* Final page structure of later member and administration views. This document defines how they look, not which functions they have.
* Contrast of the energy colours on `--panel` (section 3.4): verify when first used.
* Owner to confirm: "Ändern" on the personal profile card (section 6, customer pages); the recommendation text and provisional defaults of the passkey wizard and the flow "Anmeldung ändern" (section 6).
* Not yet verified in a real browser, on a phone, with a screen reader or at 200 % zoom: see `docs/ACCEPTANCE.md`, sections 3, 12 and 13.

## Sources

WCAG 2.2 (1.4.1, 1.4.3, 1.4.11); Tabler documentation and theme script (`tabler/tabler`); W3C SVG Integration; font licences (`google/fonts`); Tabler and Tabler Icons licences (MIT); owner-provided logos, colours and design package `LEG-Design-Vorgaben/` (2026-10-14, D-056).
