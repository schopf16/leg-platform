# Code Map

What is where, and where to change what. Read this instead of searching the code; it lists entry points, not every function. Paths are relative to `leg_platform/` (the package) unless they start with `docs/`, `scripts/`, `locale/` or `.claude/`. `D-nnn` are decision IDs in `docs/DECISIONS.md` (its contents line names the sections). A wrong pointer is a defect of this file: fix it in the same change.

## 1. Architecture

One Django project (`settings.py`, `ROOT_URLCONF = "leg_platform.urls"`), one instance per municipality, MariaDB (`default`) plus a read-only SQLite file for the address directory (alias `addresses`). Apps in `INSTALLED_APPS` order: `accounts` (user model, login, registration, second factor, profile, mail queue, security log, administration), `addresses` (address directory and import), `community` (domain of the management area: models, rules, services; no pages), `management` (pages under `/management/`: lists, dialogs, PDF printing), `admin_site.PortalAdminConfig` (replaces `django.contrib.admin`). Third party: `axes` (login lockout), `django_otp` (device functions only), `django_countries`, `phonenumber_field`; Tabler is self-hosted in `static/vendor/`.

Request flow, `MIDDLEWARE` in order: `SecurityMiddleware`, `ContentSecurityPolicyMiddleware`, `middleware.NoStoreForManagementMiddleware`, `middleware.RequestBodyLimitMiddleware`, sessions, common, CSRF, authentication, `OTPMiddleware`, messages, `accounts.session_timeout.SessionTimeoutMiddleware`, `accounts.second_factor.SecondFactorMiddleware` (a privileged account without a second factor can only set it up or log out), `XFrameOptionsMiddleware`, `AxesMiddleware`. `urls.py`: `instance.css` and `branding/logo/<variant>/` (`presentation`), `addresses/`, `management/`, `""` (`accounts.views.HomeView`, name `home`), `""` (`accounts.urls`, namespace `accounts`), `admin/`. Templates: `templates/` (shells `base.html`, `entry_base.html`, `error_base.html`, error pages, `_frame_menu.html`, `admin/` overrides) plus `<app>/templates/`; global context from `context_processors.instance`. Static: `static/` and `management/static/management/`. The CSP allows only the own origin: no inline script or style.

Background work (separate processes, never in a request): `manage.py process_mail_queue --loop` (outbox `MailJob`, D-037); `manage.py process_address_imports` (queue `addresses.ImportRequest`, D-075; this worker marks itself as the writer of the SQLite file with `storage.mark_writer`); `manage.py run_scheduled_tasks` (every cleanup command once; scheduled externally). Email: `EMAIL_MODE` console / allowlist / graph; Graph sender `mail.GraphEmailBackend` (configuration in the model `GraphConfiguration`); texts are a default template plus an optional administrator text (`EmailText`). Operations: `docs/STARTUP.md`, `docs/DEPLOYMENT.md`, `docs/INSTALLATION.md`, `docs/OPERATIONS.md`.

## 2. Project package

| Module | Contains / entry points |
|---|---|
| `settings.py` | All settings from `ENVIRONMENT = load_environment(os.environ)`; `DATABASES` + `DATABASES[DIRECTORY_ALIAS]`, `DATABASE_ROUTERS`, Axes, `SECURE_CSP`, `SESSION_*_TIMEOUT`, `STAY_SIGNED_IN_DURATION`, `LOGGING`, `INSTANCE_*`. Core file. |
| `environment.py` | Environment to validated config: `EnvironmentConfig`, `load_environment`, one `parse_*` per variable group, `security_settings`, `email_backend`, `silenced_system_checks`; `APP_ENV` local / stage / production. Core file. |
| `test_settings.py`, `urls.py` | Test settings (MD5 hasher, temporary address directory); root URLs. Core files. |
| `middleware.py` | `NoStoreForManagementMiddleware`; `RequestBodyLimitMiddleware` with `BODY_LIMITS` / `body_limit` (add a prefix to limit an open endpoint). Core file. |
| `context_processors.py` | `instance` (portal name, legal links, logos, `frame_restricted`, `frame_profile_active`, `print_date`), `PROFILE_VIEWS`. |
| `presentation.py`, `branding.py` | `instance_stylesheet`, `instance_logo`, `LogoVariantConverter`; environment colours `load_branding`, `Branding`, `COLOUR_DEFAULTS`, `colour_problems`, `contrast`, `instance_static_directory`. |
| `mail.py` | `GraphEmailBackend` (Microsoft Graph `sendMail`), `GraphEmailError`. |
| `admin_site.py` | `PortalAdminSite` (`each_context`, `index`, `group_of`), `PortalAdminConfig`, `ADMIN_GROUPS` (start-page groups by model name; unnamed models go to System). |
| `debug.py`, `log_filters.py` | `ExceptionReporterFilter`; `TokenRedactionFilter`, `redact_tokens`. |
| `tests/` | Project-wide tests (section 10); `tests/urls.py` is a test URL configuration. |

## 3. `accounts/`

Models (`accounts/models/`, a package; `__init__.py` re-exports every public name: import from `leg_platform.accounts.models`):

| Module | Contains |
|---|---|
| `user.py`, `user_manager.py` | `User` with the central account logic (`set_status`, `delete_or_deactivate`, `reactivate`, `set_privileges`, `set_access_relations`, `change_email`, `set_secondary_email`, `assign_customer_number`, `set_retention_obligation`, `complete_registration`, `end_all_sessions`; guards `_change`, `_check_against_stored`, `_check_privilege_grant`); `UserQuerySet.PROTECTED_FIELDS`, `UserManager` (`create_user`, `create_invited_user`, `create_superuser`). |
| `common.py`, `customer_numbers.py` | `EXACT_COLLATION`, `AccountIntegrityError`, validators (`validate_account_email`, `validate_https_address`, `validate_hex_colour`); `IssuedCustomerNumber`. |
| `security.py` | `SecurityEvent` (`Event`, `LONG_RETENTION`, append-only manager `record`, `delete_expired`), `LoginIpAttempt`, `SessionCodeFailure`, `ConfirmationFailure`, `RateLimitBucket`, `RateLimitEvent`. |
| `tokens.py`, `mail.py` | `VerificationToken` (digests only), `PendingEmailChange`; `MailJob` (`Kind`, `NOTIFICATION_KINDS`), `EmailText` (`Key`), `EmailTextVersion`, `GraphConfiguration`. |
| `portal.py`, `second_factor.py` | `PortalSettings` (one row), `HiddenTask`; `AuthenticatorDevice`, `RecoveryCodeDevice`, `RecoveryCode`. |

Logic (`accounts/*.py`):

| Module | Contains / entry points |
|---|---|
| `views.py` | Entry-page views: `HomeView`, `PortalLoginView`, `SecondFactorLoginView`, registration (`RegistrationView`, `RegistrationVerifyView`, `RegistrationPasswordView`, `RegistrationCompleteView`), password reset, email change and two-factor reset token pages, `ResendView`, `EmailSentView`, `LinkNoLongerValidView`; mixins `NeverCacheMixin`, `TokenPageMixin`. |
| `profile_views.py` | Logged-in profile: `ProfileView`, `ProfileEditView`, `EmailChangeRequestView`, `PasswordChangeView`, `TwoFactor*View`, `RecoveryCodes*View`, `AccountDeleteView`, `ProfileCardView`. |
| `urls.py` | Account URLs, namespace `accounts`. Core file. |
| `forms.py` | All portal forms: `PortalFormMixin`, `ProfileForm`, `RegistrationForm`, `LoginForm`, `CodeForm`, `OneProofMixin`, `ReauthenticationForm`, `NewPasswordForm`, `AccountDeleteForm`, `ConfirmForm`. |
| `registration.py`, `resend.py`, `bot_check.py`, `normalization.py` | `process_registration`, `delete_expired_registrations`; resend cooldown; `BotCheckMixin`; `normalize_email_address`. |
| `password_reset.py`, `email_change.py`, `invitations.py` | `process_reset_request`, `complete_reset`; `request_changes`, `confirm_change`, `cancel`, `stop_all`; `queue`, `process_invitation`, `reissue_link`. |
| `second_factor.py`, `totp.py`, `second_factor_reset.py` | Middleware, `UNRESTRICTED_VIEWS` / `PASSWORD_CHANGE_VIEWS` / `SESSION_FREE_VIEWS`, `requires_second_factor`, `is_second_factor_complete`, login steps, per-session wrong-code counters; `start_enrolment`, `confirm_enrolment`, `verify_code`, `renew_recovery_codes`, code budget; `request_link`, `confirm_link`, `remove_second_factor`, `reset_privileged`. |
| `mail_queue.py`, `rate_limits.py` | `submit_mail_request`, `enqueue`, `process_pending`, `HANDLERS` (kind to handler), `ACCOUNT_HANDLERS`; `consume`, `exhausted`, `*_LIMITS`, `SCOPE_*`. |
| `emails.py`, `email_texts.py`, `greeting.py` | One `send_*` per email, `_send`, `preview`, `send_test`, `absolute_url`; `Placeholder`, `Definition`, `DEFINITIONS`, `compose`, `render`, `validate_body`; `clean_name`, `render`. |
| `security_log.py` | `record(event, account=, actor=, long_retention=, detail=)`, `MESSAGES`. |
| `login_protection.py`, `axes_handler.py`, `backends.py`, `session_timeout.py` | Lockout and IP counter (`axes_username`, `axes_client_ip`, `LOGIN_FAILURE_LIMIT`); `LoginProtectionHandler`; `EmailModelBackend`; `SessionTimeoutMiddleware`, `timeouts_for`. |
| `encryption.py`, `digests.py` | `encrypt`, `decrypt`, `reencrypt` (key derived from `SECRET_KEY`, fallbacks for rotation); `keyed_digest`. |
| `portal_settings.py`, `portal_settings_form.py`, `logo_images.py` | `portal_values`, `legal_links`, `effective_colours`; `PortalSettingsForm`; `validate_logo`. |
| `permission_names.py`, `tasks.py`, `initial_administrator.py` | `permission_label`, `assignable_permissions_filter`, `rights_help`; start-page tiles `TASKS`, `open_tasks` (not scheduled tasks); `create_initial_administrator`. |
| `templatetags/` | `instance_branding.py`, `admin_display.py`. |

Administration (`accounts/admin/`, a package; `__init__.py` registers every model): `users.py` (`UserAdmin`, `UserAdminForm._clean_privileges`: rights only with a second factor, D-042), `groups.py` (`RestrictedGroupAdmin`), `email_texts.py` (`EmailTextAdmin`, `EMAIL_TEXT_PROCESSES` = grouping and order), `graph.py` (`GraphConfigurationAdmin`), `portal.py` (`PortalSettingsAdmin`), `security.py` (`SecurityEventAdmin`, read-only Axes admins).

Other: templates `accounts/templates/accounts/` (partials start with `_`, `profile/_*.html` cards, `email/*.txt` default email texts) and `accounts/templates/admin/accounts/<model>/`; commands in `accounts/management/commands/`: `process_mail_queue`, `run_scheduled_tasks` (its `TASKS` tuple names the cleanup commands: `delete_expired_registrations`, `cleanup_mail_requests`, `cleanup_login_ip_attempts`, `cleanup_security_log`, `cleanup_confirmation_failures`, `clearsessions`), `setup_instance`, `setup_initial_administrator`, `emergency_access`, `createsuperuser` (replaces Django's), `seed_design_accounts`; migrations 0001 to 0030.

## 4. `addresses/`

| Module | Contains |
|---|---|
| `models.py`, `router.py`, `storage.py`, `sqlite/base.py` | `Directory`, `Address`, `ImportRequest` (row `pk=1`); `AddressRouter`; `DIRECTORY_ALIAS`, `directory_database`, `mark_writer` (connections are read-only unless marked); SQLite `DatabaseWrapper`. |
| `services.py`, `keys.py`, `limits.py`, `views.py`, `urls.py` | `lookup(values, mode=)`, `directory_status`, `approximate_matches`; `fold`, `key`, `prefix_range`; `allow_request`; `address_lookup` at `addresses/lookup/`. |
| `importer.py`, `jobs.py` | `import_directory`, `ImportFailure`, limits; `request_import`, `process_one`, `directory_is_empty`. |
| `admin.py` | `ImportRequestAdmin` (right `addresses.request_directory_import`). |
| commands | `process_address_imports`, `import_address_directory`, `seed_local_address_directory`, `migrate` (overrides Django's; marks the writer). |
| templates, script | `addresses/widget.html`, `widget_notice.html`, `admin/addresses/directory.html`; `static/js/address-directory.js`. |

## 5. `community/` (no pages, no Django permissions: D-077, D-086)

| Module | Contains / entry points |
|---|---|
| `models.py` | `Location`, `MeteringPoint`, `TransformerCircuit`, `Leg`, `PersonAssignment`, `LegAssignment`, `PersonRecord` (staff-only person data), `ShareChange`, `HistoryEntry`, `ManagementSettings` (one row), `GridOperator` (one row), `ListPreference`; all `default_permissions = ()`; only `GridOperator` is registered in the administration. |
| `locations.py`, `transformer_circuits.py`, `addressing.py` | `create_location`, `update_address` (also merges), `set_circuit`, `confirm`, `set_archived`, `delete`, suggestion functions; circuits `create`, `update`; `address_digest`, `suggest_correction`, `check_address`. |
| `metering_points.py`, `numbers.py`, `status.py` | `create_metering_point`, steps `mark_notified`, `record_confirmation`, `record_start_date`, `terminate`, `set_archived`, `delete`; number validators; derived `derive_status`, `status_filter` (status is never stored). |
| `assignments.py`, `leg_switch.py` | `assign`, `end`, `delete` (one transaction, row locks, sets the retention flag); `switch_leg`. |
| `legs.py`, `leg_services.py` | `summaries`, `quota_problems`, `DiscountTier`, `LegProblem`; `create_leg`, `update_quota`, `update_master_data`. |
| `persons.py`, `person_search.py`, `iban.py` | `create_person`, `resend_invitation`, `update_customer_data`, `update_payment_data`, `set_note`, `add_share_change`, `may_change_customer_data`, `PersonError`; `search`, `resolve`; `validate`, `encrypt_iban`, `decrypt_iban`. |
| `location_list.py`, `metering_point_list.py`, `leg_list.py`, `person_list.py`, `problems.py` | Pure query code per list: `base_queryset`, `apply_search`, `apply_sort`, `apply_<filter>`, row builders; problem detection `problem_counts`, `LocationProblem`, `MeteringPointProblem`. |
| `history.py` | `record(subject, event, actor=, detail=)`, `entries_for`, `HistoryEvent` (the column has no choices: no migration), `PROTECTED_DETAIL_KEYS`. |
| `settings_service.py`, `preferences.py`, `grid_operator.py`, `archived.py`, `admin.py` | `update` (settings with history); `LIST_KEYS`, `save_column_widths`; `name()`, `text()`; `ensure_open`, `Archived` (D-085); `GridOperatorAdmin` (superusers only). |

Also `community/management/commands/reencrypt_ibans.py`; migrations 0001 to 0006.

## 6. `management/`

| Module | Contains / entry points |
|---|---|
| `access.py` | The one access check: `ManagementAccessMixin`, `management_view`, `check_access`, `is_management_user`, `is_protected`. |
| `urls.py` | All routes under `/management/`, namespace `management`; URL names start with the menu key (`persons`, `persons_detail`, ...). |
| `navigation.py`, `templatetags/management_ui.py` | `MENU` (`MenuEntry(key, label, icon)`), `menu_entries`; tags `management_menu`, `status_tone`, `status_label`, `bound`. Icon ids: `templates/_frame_icons.html`. |
| `lists.py`, `returns.py` | List framework: `Column`, `ListState`, `parse`, `paginate`, `list_context` (the address of a list is its state: `q`, filters, `sort`, `dir`, `per`, `page`, `problems`); `return_url`, `with_next` (way back after a dialog). |
| `views.py`, `forms.py` | `OverviewView`, `SettingsView`, `SettingsFieldCheckView`; `ManagementSettingsForm`. |
| `person_views.py`, `person_forms.py` | `PersonListView`, `PersonDetailView`, dialogs on `PersonDialog` / `CustomerDataDialog`, `PersonCreateView`, `PersonInvitationView`; `NoteForm`, `PaymentForm`, `PersonDataForm`, `SharesForm`. |
| `location_views.py`, `location_forms.py` | `LocationListView`, `LocationDetailView`, `ListWidthsView`, circuit dialogs; `DialogView` (base of every dialog: `form_class`, `title`, `lead`, `submit_label`, `default_back`, `save`), `LocationObjectDialog`, `LocationPostView`; `AddressForm`, `CircuitForm`. |
| `metering_point_views.py`, `metering_point_forms.py` | `MeteringPointListView`, `MeteringPointDetailView`, step dialogs on `StepDialog`, assignment dialogs on `AssignmentDialog`, `MeteringPointNewView`, `SuggestView`; forms on `DialogForm`. |
| `leg_views.py`, `leg_forms.py` | `LegListView`, `LegDetailView`, dialogs on `LegObjectDialog`, `LegCreateView`; `LegCreateForm`, `QuotaForm`, `MasterDataForm`. |
| `printing/` | `areas.py` (`Area` per list, registry `AREAS`, limits `MAX_*`), `views.py` (`PrintView`, one subclass per list), `pdf.py` (`render`, ReportLab, fonts in `printing/fonts/`), `document.py` (`PrintDocument`, `Table`). |
| templates `management/templates/management/` | `page.html` (list/detail shell), `dialog.html` / `dialog_form.html` (form laid out by `form.layout`: list of `(row class, [field names])`), `overview.html`, `settings.html`, `print_dialog.html`, partials `_list_head.html`, `_list_form_start.html`, `_list_foot.html`, `_dialog_field.html`, `_status.html`, `_goto.html`; per area `legs/`, `locations/`, `metering_points/`, `persons/` (`list.html`, `detail.html`, `dialog_*.html`, `_tip_*.html`). |
| static `management/static/management/` | `css/management.css` (colours only through the tokens of `static/css/portal.css`), `js/list.js`, `table-fit.js`, `lookup.js`, `info-bubble.js`, `leg-quota.js`, `settings-validate.js` (progressive enhancement only). |

Shared static: `static/css/` (`portal.css` tokens and entry pages, `frame.css`, `admin-portal.css`), `static/js/` (one script per page feature), `static/vendor/` (Tabler, icons, fonts, licences, `CHECKSUMS.txt`). Translations: `locale/de_CH/LC_MESSAGES/django.po` (edited by hand) and `django.mo` (committed).

## 7. Where do I change X?

| Task | Steps |
|---|---|
| Add a field to a model | Model (`accounts/models/<topic>.py`, `community/models.py`); `manage.py makemigrations <app>`; form, admin, template, translation; a `User` field that is protected changes only through `User` methods (D-034); tests. A migration selects the `migrations` area (fresh database, `makemigrations --check`). |
| Add a management list column | Column in the view's column tuple (`COLUMNS`, `list_columns()`, `ALL_COLUMNS` in `management/<area>_views.py`); sort in `community/<area>_list.py` `apply_sort`; cell in `management/templates/management/<area>/list.html`; print column in `management/printing/areas.py` (`<Area>` `widths`, `prepare`); translation; test in `management/tests/test_<area>_pages.py`. |
| Add a management list filter | `apply_<filter>` in `community/<area>_list.py`; `filters={name: (allowed, default)}` in the view's `build_list` (unknown values fall back to the default); control in the list template; print line `Area.option_line`; counts must equal the rows the filter shows. |
| Add a dialog | Form with `layout` in `management/<area>_forms.py`; view on `DialogView` (or an object dialog base) implementing `save`; the rule as a service in `community/` (one transaction, history entry, checks again); route in `management/urls.py`; button with `with_next(...)` in the page template; template `management/dialog_form.html`. |
| Add a menu entry | One `MenuEntry` in `management/navigation.MENU`; URL names `management:<key>` and `management:<key>_...`; icon id in `templates/_frame_icons.html`. |
| Add a security-log event | `SecurityEvent.Event` in `accounts/models/security.py` (key up to 40 characters; add to `LONG_RETENTION` if kept long); `MESSAGES` in `accounts/security_log.py` (a test needs a message per event); `makemigrations accounts` (choices are in the migration); `security_log.record(...)` in the transaction of the change; translate the label; new class of event: `docs/SECURITY.md` section 10. |
| Add a History event | `HistoryEvent` in `community/history.py` (no migration) and `history.record(...)` in the service; `detail` holds ids, dates, field names only; label in the page's history list; translate. |
| Add or change an email text | `EmailText.Key` in `accounts/models/mail.py` (+ migration); `Definition` in `accounts/email_texts.py` `DEFINITIONS`; default text `accounts/templates/accounts/email/<name>.txt`; `send_<name>` in `accounts/emails.py`; background mail: `MailJob.Kind` (+ `NOTIFICATION_KINDS` for a notice about an account) and `mail_queue.HANDLERS`; `EMAIL_TEXT_PROCESSES` in `accounts/admin/email_texts.py`; translations. Links only through `absolute_url`. |
| Add a permission or access rule | Management area: nothing to add, no permission (D-086, section 8). Administration: `ModelAdmin` with explicit `has_*_permission` methods (see `accounts/admin/*.py`, `addresses/admin.py`), registered in `accounts/admin/__init__.py` or the app's `admin.py`, named in `admin_site.ADMIN_GROUPS`; rights offered to roles: `accounts/permission_names.py`. English codenames; check permissions, never groups. |
| Add a URL outside the management area | App `urls.py`; reachable before login or without the second factor: `second_factor.UNRESTRICTED_VIEWS` (forced password page: `PASSWORD_CHANGE_VIEWS`); token pages: `TokenPageMixin`, `NeverCacheMixin`; negative test in `accounts/tests/`. |
| Add a translated text | Mark it (`gettext`, `gettext_lazy`, `{% translate %}`, `{% blocktranslate %}`, English source); add msgid and Swiss German msgstr to `locale/de_CH/LC_MESSAGES/django.po` (no "ß"; override Django or third-party texts that contain it); `manage.py compilemessages`; commit `django.mo`; check with `msgfmt --check` (`docs/STARTUP.md`, section 13). `accounts/tests/test_translations.py` fails on a missing translation or the forbidden letter. |
| Add a setting or environment variable | Operator value: `parse_*` in `environment.py`, field of `EnvironmentConfig`, call in `load_environment`, use in `settings.py`, document in `.env.example` and `docs/INSTALLATION.md`, test in `tests/test_environment.py` (both are core files: full suite). Administrator value: field on `PortalSettings`, `portal_settings.PortalValues` / `portal_values`, `portal_settings_form.py`, `accounts/admin/portal.py`. Staff value: field on `ManagementSettings`, `management/forms.ManagementSettingsForm`, `community/settings_service.update`, `settings.html`. |
| Add a test and map it | `tests/test_<topic>.py` of the app (`accounts/tests/`, `community/tests/`, `management/tests/`, `addresses/tests/`, `tests/`); add the label to the matching `AREAS` entries in `scripts/test_areas.py` (a new source file that no area maps: add its path to `paths`); `tests/test_areas.py` fails otherwise; run `.\scripts\test.ps1 -Area <name>` or `-Changed`. |
| Add a PDF print area | `Area` subclass in `management/printing/areas.py` (`key`, `filename`, `widths`, `name`, `prepare`, `details`) and entry in `AREAS`; `PrintView` subclass with `area_key` in `management/printing/views.py`; route `<area>/print/` in `management/urls.py`; `management/_print_button.html` and `print_url=state.url_to(...)` in the list view. Printing is logged by `PrintView` (`LIST_PRINTED`). |
| Add a scheduled cleanup | Command `accounts/management/commands/cleanup_<name>.py`; name in `TASKS` of `run_scheduled_tasks.py`; map the file in `scripts/test_areas.py`. |

## 8. Conventions a change must follow

* English everywhere in the repository; every user-facing text is translatable (English source) with a Swiss German translation in `django.po`; never "ß" (`AGENTS.md`, section 2).
* No instance-specific value in code, templates or migrations; email links only from `INSTANCE_BASE_URL` (`emails.absolute_url`); never read `Host` from `request.META`.
* Roles (D-012): three fixed access levels; never create, name or check a group in code; permissions with English codenames; only system administrators change `is_staff`, `is_superuser`, groups, permissions (enforced in `UserAdmin` / `UserAdminForm`).
* Management area (D-086, the approved exception to D-012): no Django permission; every `/management/` URL is served by a view with `ManagementAccessMixin` or `@management_view`; never show or hide a page, column, filter, count, print scope or menu entry by permission; `management/tests/test_access.py` walks the URL configuration and fails for an unprotected route; no `csrf_exempt`, no `put` / `patch` / `delete` handler; `test_acceptance_matrix.py` (`test_every_route_has_a_case`) needs a case per route.
* Guarded `User` writes (D-034): status, `is_active`, email, `retention_obligation`, `is_staff`, `is_superuser`, `customer_number`, password and `session_version` change only through `User` methods; `UserQuerySet.update`, `delete`, `bulk_create` refuse them (`AccountIntegrityError`).
* Two logs: the security log (`security_log.record`) holds security events with internal numbers and no personal data (D-018); the History (`community.history.record`) holds who changed which management object, with ids and field names, never note, IBAN, operator customer number, email or password (D-077, D-087).
* Security: checks only on the server; never trust ids or roles from the browser; no secret in code, logs or error responses; no inline script or style; responses with customer data are `no-store`.
* Schema changes only through migrations; exact identifier columns use `EXACT_COLLATION`. UI changes follow `docs/DESIGN.md`.
* Tests (D-071, `docs/DECISIONS.md` section 14): tests for new code incl. negative authorization; class data in `setUpTestData`; `TransactionTestCase` only where needed; threaded tests only on MariaDB (tag `mariadb`); messages are compared with the `de-ch` translation; one test process at a time; run the area, not the full suite.

## 9. Test map

Run: `.\scripts\test.ps1 -List` (areas and core files), `-Changed`, `-Area <name>[,<name>]`, `-Since <rev>`, `-DryRun`; `-Full` only when the main session orders it (`docs/STARTUP.md`, section 13). Exit code 3: another run is active (wait, never stop it). `scripts/test_areas.py` (`AREAS`, `CORE_PATHS`) maps source paths to test labels; a core file (`settings.py`, `environment.py`, `urls.py`, `middleware.py`, `accounts/models/`, `accounts/urls.py`, `accounts/backends.py`, `accounts/apps.py`, `manage.py`, dependencies, shared test helpers) or an unmapped file runs the full suite. `tests/test_repository.py` (tracked files use only reserved email domains) runs with every scoped run. `A` = `accounts/tests/`, `C` = `community/tests/`, `M` = `management/tests/`, `T` = `tests/`.

| Area | Source paths | Tests |
|---|---|---|
| `docs` | `docs/`, `README.md`, `AGENTS.md`, `CLAUDE.md`, `.claude/` | `T.test_repository` only |
| `packaging`, `testing` | `.github/workflows/`, `scripts/check_minimum_constraints.py`; `scripts/test_areas.py`, `scripts/test.ps1` | `T.test_minimum_constraints`, `T.test_production_settings`; `T.test_areas` |
| `translations` | `locale/` | `A.test_translations`, `A.test_usability`, `A.test_acceptance_final`, `A.test_acceptance_owner_decisions`, `A.test_admin_acceptance`, `A.test_portal_branding`, `T.test_error_pages`, `T.test_presentation` |
| `templates`, `static` | `*/templates/`, `*/templatetags/`; `static/`, `*/static/` | entry shell, entry pages, frame, profile, admin spot checks, `T.test_sizes`, `T.test_design_assets`, `M.test_access`, `M.test_settings_page`, `M.test_components` |
| `branding` | `branding.py`, `presentation.py`, `context_processors.py`, `accounts/portal_settings*.py`, `accounts/logo_images.py` | `T.test_presentation`, `T.test_instance_configuration`, `A.test_portal_settings`, `A.test_logo_images`, `A.test_portal_branding` |
| `environment` | `debug.py`, `log_filters.py`, `wsgi.py`, `asgi.py`, `.env.example` | `T.test_environment`, `T.test_settings`, `T.test_production_settings`, `T.test_responses`, `T.test_request_body_limit`, `T.test_database`, `T.test_error_pages` |
| `address-directory` | `addresses/` | `addresses.tests`, `A.test_address_notice`, `C.test_address_check` |
| `community`, `management` | `community/`, `accounts/permission_names.py`; `management/` | all of `C` and `M`; `management` also `C.test_rights` (route walk `M.test_access`, matrix `M.test_acceptance_matrix`, `M.test_acceptance_flows`) |
| `registration` | `registration.py`, `resend.py`, `invitations.py`, `bot_check.py`, `normalization.py`, `views.py`, `forms.py` | `A.test_registration*`, `A.test_entry_pages`, `A.test_invalid_link_redirect`, `A.test_token_page_forms`, `A.test_browser_origin`, `A.test_invitations` |
| `login` | `login_protection.py`, `axes_handler.py`, `session_timeout.py`, `password_reset.py`, `security_log.py`, `views.py`, `forms.py` | `A.test_portal_views`, `A.test_login_protection`, `A.test_session_timeout`, `A.test_stay_signed_in`, `A.test_password_reset`, `A.test_security_log` |
| `second-factor` | `second_factor.py`, `second_factor_reset.py`, `totp.py`, `encryption.py`, `digests.py`, setup and emergency commands | `A.test_second_factor*`, `A.test_lost_phone`, `A.test_one_proof*`, `A.test_recent_authentication`, `A.test_session_code_limit`, `A.test_session_limit_review`, `A.test_setup_instance` |
| `profile` | `profile_views.py`, `email_change.py` | `A.test_profile*`, `A.test_account_lifecycle`, `A.test_phase4_review` |
| `admin` | `accounts/admin/`, `admin_site.py`, `addresses/admin.py`, admin templates | `A.test_admin*`, `A.test_portal_settings`, `A.test_graph_test_email_limits`, `A.test_email_texts`, `T.test_mail_graph`, `C.test_rights` |
| `mail` | `mail.py`, `mail_queue.py`, `emails.py`, `email_texts.py`, `greeting.py`, `rate_limits.py`, `tasks.py`, email templates, mail commands | `A.test_mail_queue`, `A.test_email_texts`, `A.test_greeting`, `A.test_scheduled_tasks`, `T.test_mail_graph` |
| `migrations` | `*/migrations/` (fresh database) | `A.test_models`, `A.test_model_rules`, `A.test_account_lifecycle`, `A.test_mail_queue`, `T.test_database`, `C.test_models`, `C.test_migration_0004`, `C.test_iban`, `addresses.tests` |
| `accounts-support` | `accounts/management/` (rarely changed files) | `A.test_models`, `A.test_model_rules`, `A.test_scheduled_tasks` |

Test helpers: `accounts/tests/helpers.py` (`create_active_user`, `VerifiedClient`, `complete_second_factor`, `run_mail_queue`), `community/tests/helpers.py` (`staff_user`, `make_location`, `make_metering_point`, `make_leg`, `assign_leg`, `assign_person`), `management/tests/test_acceptance_matrix.py` (`MatrixBase`). A test class that touches the address directory declares `databases = {"default", "addresses"}`.
