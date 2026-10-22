# Design Language – LEG-Platform

Binding for every page, so that later pages look and behave like the existing ones (D-024). If this document and the owner's design package (`LEG-Design-Vorgaben/`, `VORGABEN.md`, reference pages; D-056) differ, the package wins. The decisions K1 to K22 of D-056 fill the points the package leaves open. Templates, translations and the Content Security Policy are decided in the project.

## 1. Scope

* All application views use Tabler (D-003): login, registration, profile, member and management views.
* The Django admin keeps its own appearance plus one small stylesheet (`static/css/admin-portal.css`: field widths, the box "Erste Schritte", help about the rights, the import and log pages) that uses the admin's colour variables (D-061).
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
| `--yellow` | `#DDDD22` | `#DDDD22` | Dots of the benefit list only; never text (R9) |
| `--bad` | `#B42318` | `#F0948A` | Errors, status "problem" |
| `--ok` | `#2F8F5B` | `#5DBB86` | Status "done", success (always with a word; 4.04:1 on white, so dot or icon, not text) |
| `--warn` | `#8A5A00` | `#E8B34A` | Status "open" and "not set up", warning alerts (K16) |
| `--warm` | accent 9 % over `--bg` | same | Left panel of the entry pages, hover of menu entries |
| `--logo-plate` | transparent | foreground 8 % over `--warm` | Plate behind the logo in dark mode (radius 6 px, padding 4 px 8 px) |

### 3.2 Instance Branding

* The platform is neutral. Each instance configures its own branding, never hard-coded (`AGENTS.md`, section 6): logo (positive for light, negative for dark backgrounds), accent colour for light and dark mode each with its text colour, and an optional highlight colour (`--yellow`: only the dot of the benefit list, never with text on it, R9).
* Platform defaults are the values of the design package (D-056): accent `#005BA2` / `#6AA9E0`, text on it `#FFFFFF` / `#0B1520`, highlight `#DDDD22`. Without a logo the instance name is shown as text.
* **Where branding is changed (D-073).**
  * Administration, "Inhalte", "Portal-Einstellungen" (an ordinary right of a role): name, website, the two logos (PNG or SVG, both or none), the favicon (PNG or SVG, optional), accent and text colours, highlight colour, privacy and imprint addresses.
  * Precedence per field: administration, then environment (`INSTANCE_*`: `docs/INSTALLATION.md`; the imprint address has none), then platform default. Email texts: "Mailtexte" (D-039). Page texts: `locale/de_CH/LC_MESSAGES/django.po`. Server settings: environment only (`docs/OPERATIONS.md`, section 5a).
  * Under every field with a server value the page shows "Serverkonfiguration: <value>".
  * The preview changes colours while typing (CSS variables through the CSSOM by an external script; the policy is not widened, so an unsaved logo has no preview). Removal and reset are checkboxes; an empty text field resets that field.
* **Rules for instance values.**
  * Text on the accent, and the accent as text on panel and page background, reach at least 4.5:1 (WCAG 2.2, 1.4.3). Checked at start against `#FFFFFF`, `#F6F7F8` (light) and `#191C1F`, `#111315` (dark), and again when saving. An empty text colour next to an administration accent is chosen automatically (white or `#0B1520`).
  * The accent must not be confusable with the error or energy colours (section 3.4); red tones are not permitted (manual check by the operator, installation guide).
  * The highlight is never text or chart colour on a light background. The accent is for buttons, links, active navigation and focus, never charts.
  * Validation: `docs/SECURITY.md`, section 6 (D-073): colours `^#[0-9A-Fa-f]{6}$`, absolute `https://` URLs, environment logos inside the instance static files, uploaded logos PNG or SVG; validated at start, on save and on read; invalid values are never used.
  * Values reach CSS variables without `'unsafe-inline'`: a stylesheet generated from the validated values, served as a file (`instance.css`, ETag, `no-cache`).

### 3.3 Initiating Instance: LEG Ittigen

* Since D-056 the accent, highlight and logo values of the LEG Ittigen (logo files `LEG-Ittigen_pos_rgb.svg` and `LEG-Ittigen_neg_sw.svg`, provided by the owner) are the platform defaults.
* The logo files are instance data, not stored in the repository, and are used unchanged: never recoloured, redrawn, cropped or stretched.
* Sizes: 360 px wide on the entry pages, 40 px high in the phone top bar, 48 px in the header after the login, 32 px in the phone menu. No further logo guidelines are known.

### 3.4 Energy Colours

Fixed meaning, independent of the instance accent; used by the later energy pages. Contrast on `--panel` was not recalculated; verify when first used (open).

| Meaning | Light | Dark |
| ------- | ----- | ---- |
| Production | `#B7791F` | `#E8B34A` |
| Used within the LEG | `#2F8F5B` | `#5DBB86` |
| Grid electricity | `#8A817B` | `#8A817B` |
| Consumption | `#7B4FA8` | `#B394E0` |

### 3.5 Status Indicators

* A status is always a dot plus a word, never colour alone (WCAG 2.2, 1.4.1). Dots: active or completed `--ok`; open, pending or not set up `--warn` (K16); problem `--bad`; inactive `--muted`; new the accent.
* **In progress (D-097):** a spinner (CSS only, `aria-hidden="true"`) is always followed by words that say what goes on and what the person can do ("Update running since 17:45. This can take a few minutes. You can leave this page; the update continues.").
  * The box is a polite live region (`role="status"`).
  * A failure is a message with `role="alert"` that names a fixed reason (on the import page the reason element carries it).
  * Under `prefers-reduced-motion: reduce` the spinner stands still (`animation: none`); the words carry the meaning.

### 3.6 Verified Contrast Ratios

* Required (WCAG 2.2): 4.5:1 for text, 3:1 for graphical objects and borders of fields and buttons (1.4.3, 1.4.11). Computed by `leg_platform.branding.contrast`; `test_design_assets.py` checks the token pairs against `portal.css`.
* Known low pairs, never for text: `--ok` on white 4.04 (dot or icon only); the yellow dot on the warm panel 1.18 (decoration only).
* Contrast alone does not make a page conformant. Keyboard operation, screen reader support and zoom are verified during implementation.

## 4. Typography

| Role | Typeface | Licence |
| ---- | -------- | ------- |
| All text, controls, headings, customer number | Source Sans 3 (400, 600, 700) | SIL OFL 1.1, Reserved Font Name "Source" |
| Title in the warm panel of the entry pages only | Bricolage Grotesque (700) | SIL OFL 1.1, no Reserved Font Name |
| Recovery codes and the manual key of the authenticator app only | IBM Plex Mono | SIL OFL 1.1, Reserved Font Name "Plex" |

* Fonts are served by the instance in fixed versions, never from a CDN (`docs/SECURITY.md`, section 6; `docs/SERVICES.md`). Unmodified, each with copyright notice and licence text (OFL 1.1, condition 2). A modified version (e.g. a subset) must not use the Reserved Font Name (condition 3).
* **Font sizes (K6):** 11, 12, 13 px (micro labels only: menu group names, "später", the bottom bar); 14 px (hints, footer row); 16 px (text, labels, controls); 18, 20, 22, 24, 26 (heading "Anmelden"), 32 and 40 px (title of the warm panel; 30 px on a phone).
* Controls are at least 44 px high on a desktop and 48 px on a phone (R5, K5).

## 5. Shapes, Spacing and Icons

* Corner radius: buttons and fields 8 px, cards and tiles 12 px, logo plate 6 px.
* No shadows and no gradients (R7). Exceptions: the soft shadow of the floating language menu and, with the same shadow, the floating suggestion list under a field (D-097; `--panel`, 1 px `--line`, radius 8 px).
* Lines (1 px `--line`, 1.5 px `--muted` for fields and buttons) separate surfaces.
* Spacing only from 4, 8, 12, 16, 24, 32 and 48 px (R3).
* Focus is always visible: 2 px accent outline with 2 px offset. A field in focus has a 1.5 px accent border and a 3 px ring of 30 % accent.
* Icons: line style, stroke 1.8 px, round ends, 20 px (Tabler Icons, MIT, or own SVG), served by the instance. Icons accompany text; an icon without visible text has an accessible name. No emoji (R15).

## 6. Navigation and Layout

* **Login cleanup (D-106, accepted designer specification, 2026-10-22).** Keep the left panel, corner controls and legal footer unchanged. The Clear right column remains at most 400 px wide. Order: heading, email, password, one native "Stay signed in" checkbox, three consecutive full-width actions (password primary, passkey outlined, registration outlined), then one "Trouble signing in?" link. Remove the account-question copy and both old recovery links.
  * Login-scoped spacing only: heading to fields 24 px; label to input 8 px; field groups 24 px; password to checkbox 16 px; checkbox to action stack 16 px; actions 12 px apart; help 16 px below; legal footer 24 px below. Buttons minimum 48 px high, radius 8 px. Preserve mobile shell and at least 16 px side padding, wrapping, scrolling and visible focus. Recovery links have minimum 44 px touch targets.
  * Keep separate authentication forms. The password form owns `remember_me`; the passkey form references that same stable checkbox ID and snapshots its value at submission start into JSON `stay`. Empty password/email fields must not block passkeys. Do not reset the preference on errors/cancellation, store it separately or change persistence policy. Passkey submission stays disabled until the self-hosted script detects browser support. An empty live-status region occupies no visible spacer.
  * `/login/help/` is anonymous GET-only navigation using the entry shell: heading, question, two full-width outlined choices ("Forgot your password?" to existing `/password-reset/`, "App or passkey unavailable?" to existing `/recovery/`), precise recovery clarification and a back link. No email field, account lookup or mutation. Recovery explains that a second passkey signs in directly, while an app recovery code follows the password at the next step. The existing recovery form requests instructions, not an automatic reset link; its action is "Send recovery instructions". English source and de_CH/fr/it wording follow the accepted designer text.

* **Pages without a session (`entry_base.html`):** login, registration, password reset, second step of the login, lockout and result pages.
  * Above 760 px two equal columns: the warm panel (`--warm`, padding 48 px, logo 360 px wide, title in Bricolage Grotesque 40 px, lead 18 px, two points with a yellow dot) and the form (at most 400 px wide), separated by a 1 px line.
  * At 760 px and below the panel shrinks to a top bar of at least 64 px with the logo (40 px high). The text is hidden and the login fits one screen.
  * The footer row "Datenschutz | Impressum | host of the website" sits under the form (D-056).
* **Corner buttons on every page** (also error pages): language menu (DE, FR, IT, EN; a form, works without JavaScript) and light/dark button (needs JavaScript), round, 40 px, fixed top right (in the header after the login, next to the name).
* **After the login (K1, D-057).**
  * Fixed header 64 px: logo 48 px high, name as text, corner buttons.
  * Desktop: fixed left menu 232 px wide. MEIN BEREICH: Start, Profil, for staff Verwaltung. DEMNÄCHST: Standorte, Rechnungen, Energie, Mitteilungen with the mark "später", no links. Below a line: "Ausloggen" as a POST form (the msgid "Log out"; "Abmeldung" stays the deregistration), Datenschutz, Impressum; the foot is sticky at the bottom of the menu and of the drawer. A customer finds "Kontakt zur LEG" (where the instance has a website) as the last entry of the Information group. **A customer (not staff, D-105; `anmeldungprozess/MENU-STANDARD.md`)** sees instead: MEIN BEREICH: Start, Profil, Teilnahme (always open: Gebäude, Startdatum, Nachbarschaft), Information (Gesellschaftsvertrag, Frage und Antwort, Veranstaltung); DEMNÄCHST: Rechnung, Energie, Mitteilung (singular). Parents are no links; the contract page has ONE entry, "Gesellschaftsvertrag" under Information (D-108: the entry "Vertrag" under Teilnahme led to the same page and is removed; the address is unchanged); the contract page shows the current contract in a bordered frame (an embedded PDF, hidden below 760 px where the download button is the way to read it) with the download below and the versions after it; an entry whose page does not exist yet is "später" without a link; the current page has `aria-current="page"`, bold accent, `--warm`, the 3 px bar; entries are 14 px (the scale has no 15 px), 44 px high.
  * Phone: bottom bar 72 px (**staff: Start, Übersicht, Vorgänge, Menü**, walkthrough W5-11: the "Menü" tab opens the same drawer as the button in the header, the focus returns to the tab; **a customer: Start, Teilnahme, Information, Profil**, START-SEITE.md section 7) and the same menu in a drawer 290 px from the left with an overlay. Escape and the close button close it; focus is held inside and returned to the button that opened it.
  * Content at most 960 px wide, padding 32/24 px desktop and 20/16 px phone, page title first. Menu and header measures: the image measures of D-057.
  * The start page has the greeting, the customer number with the address, and the task tiles (hiding is final and stored on the server) or an empty state (panel with 1 px line, 12 px radius, at least 360 px high).
  * An account that must change its password, or whose mandatory second factor is incomplete, gets the header and "Abmelden" only.
* **Registration (D-058):** three steps with a 4 px step bar (classes `step-fill-1` to `-3` because of the CSP) and the muted label "Schritt N von 3" (14 px) above the heading.
  * The details step shows the address as plain text with the status "Bestätigt" (K7: no box). Street and house number (96 px), postal code (128 px) and town sit side by side, also on the phone.
  * Hints under a field are 14 px. A field with an error has a red 2 px border and one message. The error summary uses short field names and is focused.
* **Choice cards, the sign-in method (D-106 "Guidance"; Clear register).** One `<fieldset>` whose `<legend>` is the question ("Wie möchten Sie sich anmelden?"), directly under it a closed native `<details class="help-details">` explainer (no script), the recommendation line ("Unsicher? Wir empfehlen: …"), the static helper "Was passt zu mir?" and then one card per method, **always stacked** (the entry column is 400 px wide).
  * A card is one `<label>` around a native radio (24 px), at least 48 px high, 1 px `--line`, radius 12 px, no shadow. Inside, in this order: title (600), benefit sentence, two 14 px lines "Sie brauchen:" and "Wenn Sie es verlieren:", then, where it applies, the consequence line. "Empfohlen" and "Aktuell" (with the status dot) are words in the card head. The texts are linked with `aria-describedby` on the radio.
  * The selected card has the filled radio and a 2 px accent border (never colour alone); the focus ring is a 2 px accent outline with 2 px offset on the card (`:has(:focus-visible)`). Order is always password only, password and app, passkey; the recommendation does not reorder.
  * The component is two includes: `accounts/_login_method_choice.html` (fieldset, cards, helper) and `accounts/_login_method_help.html` (the explainer, also usable alone). The page supplies the form, the error summary and one "Weiter" button; the options come from `login_methods.available(user)`, never from the template. The same component serves registration follow-up, profile, recovery and first administrator login. The tile on the start page is "Anmeldemethode wählen", status "Optional", button "Wählen" (a link).
  * Passkey pages use the Clear register and established profile cards/forms. Management shows the chosen label, creation, last use and synchronization support, never a claimed physical-device identity. Add/remove are explicit; removal states the minimum and that all sessions end. Recovery stays in user detail settings; the confirmation names the irreversible effects. Browser/design review remains pending.
  * Accepted walkthrough corrections UX-002–008: setup copy distinguishes switching, confirmed recovery and adding a key. Creation stages the key; a separate completion saves/activates it. Only an administrator requiring the device statement sees its checkbox; ordinary users see a non-interactive second-key recommendation. The passkey-mode password card shows no fictitious password mask or dead-end password form and links directly to the shared method chooser. Recovery headings and subjects describe whole-method recovery; recipient POST explicitly names removal and uses destructive treatment. Recovery-only admin has one native heading, formatted customer number/name (name only when no number exists), and a native `.button` of at least 44 px. The follow-up tile names all three methods; new password setup reuses the established password-rules list and association. These corrections await independent recheck.
  * Promotion of an existing ordinary passkey user (owner-approved, 2026-10-22) uses the existing administration permissions section: an authorized system administrator explicitly states that the person confirmed setup on two devices. The unchecked native checkbox is not an ordinary-user setup option and does not identify physical devices. Form errors explain a missing second active passkey or confirmation; no new screen or menu.
* **Profile (D-059, `navigation/VORGABEN.md` section 3.3; D-105):** "MEIN KONTO / Profil" with five cards on the page background (1 px line, 12 px radius, no shadow): Persönliche Angaben, E-Mail-Adressen, Passwort, Anmeldemethode (formerly Zwei-Faktor-Anmeldung (2FA); D-106 "Guidance"; the card keeps `id="card-two-factor"`, its choice is the state `?edit=two_factor&step=choose`; the view state names the current method with the dot "Aktiv" and the button "Ändern", the other buttons ("Neue Codes erzeugen", "2FA deaktivieren") stay), and the red start card Konto löschen. **A customer has a sixth card "Bank- oder Postverbindung" directly after Persönliche Angaben** (`kundenprofil/PROFIL.md` section 2): the IBAN in full in IBM Plex Mono groups of four, the status "Hinterlegt" / "Nicht hinterlegt" (dot plus word), "Ändern" opens the field with the hint in the same card, no proof field. The address line shows "· EGID n" and, after the staff confirmed the building, the grey note "Die Adresse ist bestätigt und kann nicht geändert werden. …" (the address fields are then disabled). The email card and the personal card say "Ändern" (not "Bearbeiten"; the personal card deviates from PROFIL.md, owner to confirm). The delete card of a customer with running metering points has no form: the red button leads to the deregistration; the confirmation of an account that is only deactivated carries the notice "Ihr Konto wird deaktiviert. …".
  * A card shows its values with a button "Ändern" that opens the form **in place** (server-rendered state `?edit=<card>`, works without a script, one card open at a time, errors re-render the same card with one message per field). Extra fields are under "Weitere Angaben (optional)".
  * Recovery codes are shown once with "Kopieren", a checkbox and "Fertig".
  * Deletion: a red outlined button, then the confirmation with the proof, a box never ticked in advance and a red button.
* **One proof per confirmation (D-062).** The second-factor code if one is active, otherwise the password, never both (rules: `docs/SECURITY.md`, section 3).
  * The proof field takes the place and style of the password field (48 px high, radius 8 px, semibold 16 px label, 8 px between label and field).
  * With the code, a 16 px lead hint (`.profile-text`) stands between the label "2FA-Authentifizierungscode" and the field. The code field is a plain text field (no `inputmode`, `maxlength` or `pattern`). Screen reader description order: lead, error, note.
  * "Handy verloren?" is a text link under the code field of "2FA deaktivieren". It opens a state of the same card (also without a script) with the password and the recovery code, a text for people with neither (they go through the operator) and the red outlined button "2FA deaktivieren".
  * All password fields are hidden by default with the eye button. The way back is the secondary button "Abbrechen" in every card.
  * After five wrong entries the session ends and the login page says so (D-062).
  * Images of the owner's package with "Aktuelles Passwort" are valid only for an account **without** a second factor. The state with a second factor has no image and is described here.
* **Links from emails (D-058, D-067):** an invalid, expired, used or replaced link redirects (302, same answer for every cause) to a fixed page of its flow, as does a confirmation page whose session state is gone. The fixed page carries the message and the next step, is identical for everybody, uses no flash message and is an entry-shell page without header or logout form.
* **Address suggestions and notice (R-04, R-05, R-21; D-065, D-066, D-111).**
  * Suggestions appear directly under the street field as a bordered list without shadow, titled "Matching addresses" (or "Did you mean this address?" for an approximate match). Never inserted by a blur; replaced or hidden only when an answer arrives. The list is a listbox of options (ARIA 1.2 combobox on the input: `aria-activedescendant`, the active option `--warm` with a 2 px accent ring inset), the focus stays in the input; arrow down opens and moves, arrow up moves (and back to the text), Enter takes the active option (only then it does not send the form), Escape closes and keeps the text, Tab closes. A polite live region says how many there are. The panel is in the flow of the page (a card never clips it) and nothing closes it by a mouse press elsewhere (the button below would move).
  * The same for the postal code (four digits: the town or the towns) and the town (three characters: the postal code or the codes): a panel directly under the input, titled "Choose a town" or "Choose a postal code"; one answer goes into an empty field and is announced ("Town filled in: …"); text the person typed is never replaced unasked.
  * Everywhere an address is edited (registration details, profile, customer wizard, management persons, locations, correction of a building, settings of the LEG; country fixed to Switzerland or the country field), one widget and one script, never in read-only views. Choosing a suggestion fills street, house number, postal code and town; that and the two fills above are the only ways the script changes fields.
  * One non-blocking warning notice after the postal code and town row: "This address was not found. Please check street, house number, postal code and town. You can still save it as entered." Only for a complete address while editing and only when the directory is available. It hides on any input, never takes the focus and never blocks saving.
  * Nothing else is said about the directory (no source, no text for an unavailable, old or limited directory) **except to a staff account**: a neutral (grey) notice says that there are no suggestions because the directory is not available or not imported yet.
* **Administration views:** dark vertical sidebar (Tabler vertical navbar with `data-bs-navbar-theme="dark"`), breadcrumb, page title with one primary action.
* **Menu order (D-097).** The left menu and the phone menu are one list (one template).
  * MEIN BEREICH (Start, Profil); VERWALTUNG (Übersicht, Vorgänge, Personen, Standorte, LEGs, Messpunkte, Einstellungen, then "Mailtexte" for an account with that right, then Verträge; without the right "Verträge" follows "Einstellungen", D-104); DEMNÄCHST (Rechnungen, Energie, Mitteilungen); last, the group SYSTEM with "Systemverwaltung" (own icon, not the gear of "Einstellungen").
  * "Systemverwaltung" is shown only to an account that can use the administration: a superuser, or a staff account for which the administration lists at least one thing (D-102).
  * Below the line: "Abmelden", Datenschutz, Impressum. The entries "später" stay in the menu; the bottom bar of a staff account has none (W5-11).
* **Lists (D-097):** the main text of a row (address, person, number, LEG name) is the link to its page; the small link icon stays. **Exception, the list "Standorte" (D-110):** its rows open and close, so the whole row (a card on a phone) is the click target (pointer cursor, `--warm` background on hover and while the arrow has the focus); the address is plain text, the way to the detail page is the link icon at the right (accessible name "Go to the details of the location"); the arrow stays as the indicator and as the keyboard button (`aria-expanded`, `aria-controls`). A click on a link, button or field inside the row, or one that ends a text selection, does not toggle. On touch (`pointer: coarse`) and up to 760 px these are at least 44 px high: small buttons (`.btn-xs`), back links, filter chips, pager buttons, text links in hints, fact rows and under a field. A status is the dot with its word, also in phone cards.
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
* **Customer pages (D-105; owner design `anmeldungprozess/*`).** Existing frame, tokens, Tabler controls (48 px, 8 px radius) and the Clear register; stylesheet `customer/static/customer/css/customer.css`, one script `customer.js` (progressive enhancement). A state is a dot plus a word (`.dotword`, `.dot.tone-*`); a progress bar is a native `<progress>` (8 px, token colours, no inline style).
  * *Page "Gebäude":* label "MEINE TEILNAHME", title, one sentence; at the right "Anmeldung starten" (blue) then "Abmeldung starten" (red outline, only with an active point), stacked on a phone. One card per building (1 px line, 12 px radius, 24 px padding): heading (own name, else the street) with the pencil, line (the whole address with a name, else postal code and town), status; facts EGID, Netzbetreiber (only if the instance entered a name), Beschreibung with the pencil (no EGRID: no verified source); the own metering points in fixed columns (number in IBM Plex Mono with the name grey below; direction with "Seit"; status) that become number, then direction and status on a phone; under a point of a running process the progress (count, bar, "Details anzeigen" with the steps, "Weiter anmelden" for a draft, "Anmeldung zurückziehen" before the registration at the grid operator); footer "Messpunkt hinzufügen" (blue) and "Messpunkt entfernen" (red outline) or the sentence "Abmeldung noch nicht möglich, bis der Messpunkt aktiv ist.". Empty: the empty-state card with "Anmeldung starten" and no header buttons.
  * *Page "Abmeldung" (ABMELDUNG.md; D-105):* the width and the cards of the wizard (880 px, `.step-card`); kicker "MEINE TEILNAHME · ABMELDUNG", title "Abmeldung" (with the building's name when only one building is listed), one sentence. Three cards in one form: the metering points as boxes (name or direction, then number in IBM Plex Mono, direction and "Seit", grouped by building with a legend when there are several), the date as two radio rows ("Nächstmöglicher Termin" chosen with "Frühestens am <Datum>" next to it; "Wunschtermin (Monatsende)" reveals a date field labelled "Wunschdatum (letzter Tag eines Monats)"; the earliest date deviates from ABMELDUNG.md section 3) with the notice sentence, and "Bestätigung": the summary sentence (count follows the boxes), the one proof field, "Eine Bestätigung geht an Ihre E-Mail-Adressen.", the red outlined "Verbindlich kündigen" (inactive until a point is chosen and the proof is typed) and the neutral "Abbrechen" at the right. A feed-in without an IBAN shows a card with the way to the profile. Result page: progress, "So geht es weiter", the submitted details, and (from "Konto löschen") the card with the way to the deletion. The card footer of a building with a submitted registration says "Messpunkt entfernen" (withdraws the registration).
  * *Wizard:* label "MEINE TEILNAHME · ANMELDUNG", bar and "Schritt n von m: Name" (the bank step counts only with a feed-in), heading as a question, one sentence, one card (at most 880 px), buttons under a line: "Weiter" (blue) left, "Abbrechen" (red outline) right; on a phone one under the other. "Abbrechen" opens the card "Anmeldung abbrechen?" at the top of the same page (red border; focus on "Weiter anmelden", never on the red button). Fixed beginning of a number: grey, in the frame of the field (`.prefix-input`). Yes/no questions are two radio buttons; the part behind "Ja" is hidden only when the script runs. A hint under a field is 14 px; an error is red bold 14 px with the 2 px red border and the error summary on top.
  * *Start page:* the card "Ihr Weg in die LEG" (**one list for the whole way, W1-01:** before the submission the steps of the wizard with its names, "n von m erledigt" counted like the wizard (no extra final step; the bank step only with a feed-in); after it the five steps of the page "Ihre Anmeldung ist abgeschickt" with their words; a returned draft carries the persistent note "Die LEG hat Ihre Anmeldung zur Korrektur zurückgegeben: …" with the escaped reason, a paused one the day it is deleted; the next step as one sentence and one blue button) above the tiles; the tiles "Gebäude anmelden" (blue button; a POST form) and "Einspeisung nachmelden" ("Optional", grey dot) follow the tile rules of `START-SEITE.md`. **Where no card is shown, one or two status lines stand under the greeting** (dot plus sentence, "Details" to Gebäude: "Sie nehmen seit dem … an der LEG teil.", "Ihre Teilnahme beginnt am …", "Ihre Abmeldung ist bestätigt/eingereicht …"; W1-18). **One-time banners** (`.banner-list`, warning tone, role status) above the card after a rejection or a lapse, shown once on the start page or the page Gebäude; the staff note is plain text.
  * *Walkthrough 3 (D-107, customer part):* the card of a submitted registration has a secondary button "Anmeldung ändern" next to the red "Anmeldung zurückziehen" until the LEG takes it into review, then a muted note instead ("Die LEG prüft Ihre Anmeldung. Sie können sie hier nicht mehr ändern. ..."); the confirmation page has the same card layout as the withdrawal page with the primary button on the right; a draft without a building has a card ("Gebäude noch nicht erfasst") and every draft card is labelled "Entwurf, noch nicht vollständig erfasst".
  * *Walkthrough changes (W1 to W4, team lead decisions; `docs/DECISIONS.md` D-105):* words "Startdatum" (not "Aufschaltung"), the page label and title say "Anmeldung"; the word "LEG" in a sentence and the menu entry "Kontakt zur LEG" are links to the instance website (only where it is configured; the link opens in a new tab); the log-out entry is "Ausloggen"; the error summary names the fields only (the message stays under the field); step 2 shows "Schritt 2: Messpunkte" (no total) with the explanation of the metering point, the configured prefix, the feed-in heading and the note that the figures are optional; step 1 is prefilled with the account's address; the card of a submitted registration has one red outlined "Anmeldung zurückziehen"; a registration shows "Angemeldet am", a running point "Seit", a start later no line; an empty description is a grey dash; the deregistration page shows "Frühestens am <Datum>" next to "Nächstmöglicher Termin" (deviates from ABMELDUNG.md section 3, owner to confirm) and says that the notice cannot be withdrawn by the customer; the profile card "Persönliche Angaben" says "Ändern" (deviates from PROFIL.md, owner to confirm), its lock note stands directly under the address fields and locked fields are grey.
  * *Developer proposals (not in the owner's documents):* the wording of the next-step sentences except the contract, the dots of the steps of a deregistration, the banners "paused" and "returned by the LEG", the contract page "Gesellschaftsvertrag" with the accepted versions, the placeholder of the deregistration.
* **Processes, contracts, reminders in the management area (D-104; owner design `verwaltung/vorgaenge`, `vertraege`, `SEITEN-TEMPLATE.md`).** Same frame, tokens and list framework as the other management pages.
  * *Overview:* two cards that are never added up. "Zu erledigen" (the queues of the onboarding: a count that equals the rows of the filtered list it links to, the five oldest entries, the notices, a missing contract; only a queue with entries has a row) and "Probleme" (unchanged: the button "Probleme filtern (n)" and a row per list with a problem). Drafts never appear.
  * *List "Vorgänge":* one table for applications and deregistrations (columns Person, Art, Gebäude, Messpunkte, Status, Fortschritt, Datum, Letzte Änderung, link column). Person name with the customer number below; building with the EGID below; status as dot plus word (waiting `--warn`, current `--accent`, confirmed `--ok` ring; a draft or a paused draft "Entwurf / nicht eingereicht" / "Pausiert / nicht eingereicht" in the muted tone, D-107: read-only page, no button); progress is a native `<progress>` (token colours, with a text "2 von 5 Schritten"); a value that is not known yet is the grey italic word "offen", never a dash. Default sort: oldest change first. Phone: one card per process.
  * *Page of an application / deregistration:* back link (once, at the top), kicker "Verwaltung · Anmeldung", name as title, status at the right, a progress card with the way (dot, word, date per step; the five steps and the words of the customer's page, W1-01), a "Nächster Schritt" card with the buttons of the state (primary blue for the next step, outlined for others; the deregistration has the same card with "Termin bestätigen"), the five steps one under the other (no wizard), the side column with the person (label above the value, so an email is not broken), notices and history (equal entries in a row are one line "× 3"); at the end the red outlined "Anmeldung rückabwickeln". Steps are dialogs (normal pages) with an action as title and button ("Start bei … erfassen", "Start erfassen"); a step that removes the page of the application (return, roll back) leads to the list with a message.
  * *Metering points, plant details and corrections (D-109):* the metering points of an application and of a deregistration are blocks, not a table: first line the number (IBM Plex Mono, tabular figures, one line, never wrapped; first block of 13 characters, then blocks of four, no-break spaces: `.mp-number`, the filter `metering_point` on every page that shows a number), the direction badge and at the right the link icon and the small outlined "Bearbeiten" (only while the LEG works on the application; its accessible name starts with the word and names the number); below, labelled facts: "Name", and "Anlagedetails" as stacked lines of a definition list (label in the muted 14 px, value in tabular figures: "PV-Leistung 9.50 kWp", "Batterie", "Wallbox"; an empty figure has no line). A new building has a small outlined "Adresse bearbeiten" below its facts; a stored building has the sentence that its address is changed on the page of the location. The two dialogs follow the dialog rules above (prefilled; the number as in "Messpunkt erfassen" with the prefix, the check while typing and the complete number grouped; the direction as the segmented control; the plant figures as three fields with units under the divider "Anlagedetails"); an address that the directory does not know shows the amber banner with the suggestion and the button "Wie eingegeben speichern".
  * *Dialogs (W5-05, W5-07):* "Anmeldung abschliessen" is filled with the customer's wish (never before the earliest date), says the wish in the lead and the earliest date in the hint; the confirmation of the addresses is a tick "Adressen sind geprüft und werden gesperrt" in a framed box (off by default; only while a building is not confirmed). "Termin bestätigen" is filled with the wish and names the earliest date. "Zur Korrektur zurückgeben" has the required reason field (a text area, 500 characters); "Rückabwickeln" has the reason choice and the optional note (300 characters).
  * *List "Verträge":* icon buttons of 44 px with an accessible name that starts with the visible label and names the file ("Als aktuell festlegen: contract-….pdf"), and the short text label as a bubble on hover and keyboard focus (always beside the icon on a phone): download (neutral outline), set as current (accent outline; asks first like the deletion: "Fassung vom … als aktuell festlegen?"), delete (red outline; asks first); an action that is not allowed is not drawn. "Neue Fassung hochladen" is a text button, blue, top right. No link column.
  * *Settings:* the small block "Adressverzeichnis" (the time of the daily run, own button) under the main form; the chapter "Erinnerungen und Fristen": one block per deadline (name, trigger, recipient, mail text, one sentence of effect, the days field) with one vocabulary (a draft is paused, then deleted; "Vertrag nicht zugestimmt"), its own button. A change that would delete drafts asks once more: "Frist „…“: Sie ändern sie von 14 auf 5 Tage. Dadurch wird 1 Entwurf endgültig gelöscht. Der Kunde erhält eine E-Mail. Das lässt sich nicht rückgängig machen." with the red button "Speichern und 1 Entwurf löschen".
  * *Overview (W5-02):* the name of a row is the link (44 px high); the 24 px chain icon beside it is decoration (out of the tab order); the notices row shows the kind and person of the oldest notices; wording "Wunschtermin …", "wartet auf die Terminbestätigung"; quotes „…“.
  * *Lists:* the start widths of "Vorgänge" fit the card of a 1280 px screen without scrolling (the card is about 967 px wide; narrower cell padding, 8 px a side); a status word is never cut (a longer word breaks).
* One primary button per page (R1). Destructive actions use the outlined error style and require confirmation.

## 7. Colour Modes and Print

* **Light and dark only by the button (R13 changed by the package, D-056).** No automatic mode, no `prefers-color-scheme`. The server always renders light; the person switches with the sun/moon button. The mode is set on `<html>` as `data-theme` (own tokens) and `data-bs-theme` (Tabler), always the same value.
* **Persistence of the colour mode (K3).** The choice is kept in the tab's `sessionStorage` (no cookie, nothing sent to the server; a new tab starts light).
  * The scripts are external files (`theme-init.js` in `<head>`, `corner-controls.js`). They read only the allowlisted values `light`, `dark`, evaluate no URL parameters and never set branding attributes.
  * Without JavaScript the sun/moon button is hidden and the page stays light.
* **Languages (D-056, changed 2026-10-19).** The menu offers Deutsch (`de-ch`, the instance default), Français, Italiano, English. The language is real and server-side: every item is a submit button in a small POST form to `set_language` (`/language/`), which sets the functional language cookie and returns to the page (Referer, checked). Every text of the page follows (templates, forms, messages, administration, JavaScript catalogue). The button shows the active language (DE, FR, IT, EN) and the item is marked `aria-current`; `<html lang>` is `de-CH`, `fr`, `it`, `en`.
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

Verified against Tabler's documentation ("Customize Tabler", "Color modes", "Theme base colors"). Re-check against the version chosen under D-015. Changing primary colour, base or radius can change contrast and must be re-checked (section 3.2).

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
* Open (D-073): whether an SVG with `style` attributes renders in `<img>` under the sandbox policy was **not** checked in a browser.
* Footer legal addresses come from the portal settings or the instance configuration and are checked again before printing (`docs/SECURITY.md`, section 6).
* No third-party fonts, icons or stylesheets. The colour mode cannot be changed through URL parameters (section 7).

## 11. Open Points

* Final page structure of later member and administration views. This document defines how they look, not which functions they have.
* The contrast of the energy colours on the new surfaces is verified when first used (section 3.4).

## Sources

WCAG 2.2 (1.4.1, 1.4.3, 1.4.11); Tabler documentation and theme script (`tabler/tabler`); W3C SVG Integration; font licences (`google/fonts`); Tabler and Tabler Icons licences (MIT); owner-provided logos, colours and design package `LEG-Design-Vorgaben/` (2026-10-14, D-056).
