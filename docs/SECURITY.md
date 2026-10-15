# Security Policy – Security Keeper

## 1. Binding Principle

Security by design and security by default apply to all architecture, development, configuration and operational decisions.

The AI takes on the role of Security Keeper. It actively identifies risks, explains them clearly and proposes secure alternatives.

Security must never be silently weakened in favour of convenience or development speed.

The software must be securely operable by municipalities without in-depth IT knowledge: secure defaults, few manual security steps, clear instructions.

## 2. Security Decisions

For security-relevant changes:

1. Identify protection needs and possible attacks.
2. Determine appropriate protective measures.
3. Choose a secure implementation, preferably an established, maintained solution instead of a custom development.
4. Add automated tests.
5. Document the checks actually performed.
6. Openly name remaining risks.

In case of significant uncertainty, ask before implementing.

Security decisions are not based on assumptions. Protective measures, configurations and statements about threats are verified against official sources, in particular the vendors' security documentation (e.g. Django, Microsoft), recognised standards (e.g. OWASP) and official government requirements. Blogs, forums, news and tabloid content are not a basis. The rules are defined in `AGENTS.md`, section 3.

## 3. Authentication

* Use Django's password functions and established security mechanisms.
* Do not develop custom password hashing schemes. Passwords are hashed with Django's default hasher and the default work factor of the installed version, never below the OWASP recommendation (D-023).
* Login is possible only with the email address (D-021). The customer number is not a login identifier, because counting failed attempts per account across two identifiers would reveal which email address and customer number belong to the same account.
  * Email addresses are looked up only through the normalised unique column of accounts that are not deactivated, so that exactly one account is found. The normalisation (at least removal of surrounding whitespace and conversion to lower case) is defined in one place and used for storage and for all lookups and per-address counters (login, registration, password reset, email change, counting of failed attempts and rate limits), so that variants in upper and lower case do not bypass the abuse protection.
  * A login with an email address that does not exist returns the same response and shows the same lockout behaviour as a login with a wrong password. The authentication backend keeps Django's protection against timing differences for nonexistent users (it is based on `ModelBackend` or reproduces this behaviour).
* Password rules use Django's password validators: minimum length 15 characters for all accounts, maximum length at least 64 characters, all characters allowed, no composition rules; passphrases are recommended to users. Source: OWASP Authentication Cheat Sheet (checked on 2026-10-09: minimum 15 characters without MFA, 8 with MFA; two-factor authentication is optional for users and can be removed, so 15 applies to all accounts; D-004).
* An account can only log in once its email address is verified and a password is set. Blocked and deactivated accounts cannot log in.
* Registration:
  * The password is set only after the email address has been verified, on the page reached through the verification link (step 2 of 3). Verification therefore never activates a password that the person clicking the link did not choose. The password is checked there and kept **only as a hash**, in the session of the browser, bound to the digest of the verification link (a hash held for another link is refused; opening another link drops it); the account is **activated only by the last step** (name, address, consent), which writes status, customer number and password in one transaction under the locks of the account and of the link (D-058). The consent to the privacy notice is stored with its time and the address that was shown.
  * The page after the request names the address that this browser entered (from its own session, never from the URL) and offers "send again" only after a pause of 60 seconds that the server enforces (`resend.py`); the limits per address, per network and per instance apply to every resend, and the address cannot be chosen in the resend request (D-058).
  * Unverified accounts and their data are deleted automatically when the verification link expires.
  * A new registration with the address of an unverified account replaces the pending registration; it never leads to a dead end.
  * Registration ignores any submitted fields other than the email address, and the later steps ignore any fields other than the password (step 2) and the profile fields of the package (name, street, house number, postal code, town; the country is Switzerland) with the consent (step 3), e.g. `is_staff`, `is_superuser`, groups, permissions, customer number, retention flag, status, the email address. New accounts have no rights.
  * Registration and email change respond in a comparable time regardless of whether the address is already in use (e.g. by sending emails asynchronously).
  * Completing the registration logs the person in (owner decision of 2026-10-14, D-055): the link was consumed by the successful submission, the session ID is renewed, the account has no rights, and no failed attempt is recorded. A used, replaced or expired link cannot log anyone in. The registration link stays valid until the last step is completed (D-058: scanners, back button), opening it rotates the session key, and the session keys of the registration (the pending request, the password hash, the privacy address) are removed after the completion and when the link is invalid.
* Two-factor authentication with an authenticator app (TOTP) is mandatory for staff users and system administrators and optional for users (D-004).
  * `is_staff` and `is_superuser` are granted only to an account that has already set up two-factor authentication (D-042); otherwise whoever knows its password could set up their own authenticator app at the next login. An account with these rights but without a second factor (the initial system administrator, or after a reset of its second factor) must set it up at its next login before it can use any function.
  * Sessions of staff users and system administrators without completed two-factor authentication have no privileges.
  * Whether the second factor has been completed is checked in one central place, independent of the method, so that a later switch to AGOV does not change this check (`docs/PROJECT.md`, "AGOV and Passkeys"). A switch of the method must keep at least the current level of security.
* Recovery codes are issued when two-factor authentication is set up. Each code can be used only once. Users can generate new codes after re-authentication; the previous codes become invalid.
* Wrong codes at the second-factor step have a budget per account in addition to the lockout of the login protection (D-040): a window of 24 hours from the first wrong code; at 10 wrong codes the owner is notified by email without a link (once per window); at 15 code entry is paused, also for a correct code, until the window ends or the password is reset, which leaves at most 10 counted failures (once per window). Only wrong codes that reach the check count (not attempts refused by a lockout, not wrong passwords), so that someone who knows only the email address can neither pause the account nor trigger the email. A successful login does not clear the budget. The pending login between password and code ends when the password changes.
* Lost second factor (no recovery code available):
  * Users: a system administrator may start the removal of the second factor. The application then sends a time-limited, single-use confirmation link to the account's registered email address; the second factor is removed only after the link has been confirmed. The sender address of an incoming email is not accepted as proof, because it can be forged (RFC 7489).
  * Staff users and system administrators: a system administrator resets the second factor only after verifying the person's identity in person or by calling back a phone number that is known independently of the account (not taken from the account's editable profile). The second factor of the last system administrator is reset only via a management command on the server.
  * Every reset is logged, and the account is notified by email.
* A password reset never removes or bypasses the second factor.
* Re-authentication (for changing the email address, deleting the account, removing two-factor authentication and generating new recovery codes) requires the current password and, if set up, the second factor (OWASP Authentication Cheat Sheet). **Setting up** two-factor authentication is the one exception, by decision of the project owner of 2026-10-14 (D-055): it does not ask for the password again if the session authenticated at most five minutes ago (`second_factor.recently_authenticated`). The time is written by the server into the session when the session logs in (after the password and the second factor, if there is one, and when the registration is completed), is never taken from the client, ends with the session, and is not extended by activity, a password change or a password reset. After the five minutes the password is asked for as before. The device is still confirmed only by a valid code of the app, the account is notified by email, the event is logged, and other sessions end. Risk: a hijacked fresh session could add its own authenticator within five minutes.
* Login and recovery are protected against automated abuse in the application itself with an established package (D-017). The login path uses Axes for account lockouts and a separate, transactional IP counter because Axes account resets also delete records that contribute to its IP bucket. The limits of registration and password reset are described in the last bullet of this list (D-038).
  * Failed attempts are limited per account and per IP address.
  * Lockouts are temporary (cool-off period), so that attackers cannot lock out other users permanently. A password reset remains possible during a temporary lockout (OWASP Authentication Cheat Sheet). A successful password reset ends only the temporary lockout of that account; failed-attempt counters and lockouts per IP address remain unchanged, so that an attacker cannot lift the lockout of their IP address by resetting the password of their own account. How this is achieved with the package is verified against its documentation before implementation. Remaining risk: attackers who know an email address can repeatedly cause temporary lockouts of that account and use up the per-address email limit; the account owner can still use links already sent to their mailbox, and new emails are possible after the time window (D-017).
  * Behind reverse proxies, the exact trusted proxy count is configured per instance with `AXES_IPWARE_PROXY_COUNT`. The default is `0`, which uses `REMOTE_ADDR` and ignores forwarded headers. A positive count enables strict IPware proxy-chain validation; a mismatch yields no client IP and login is refused. Client IP headers are otherwise client-controlled and forgeable (django-axes and django-ipware documentation).
  * Axes stores only a keyed digest of the normalised email address. Its attempt rows do not store the raw IP; the separate IP lockout table stores an HMAC-SHA256 digest with a purpose-specific key derived from `SECRET_KEY` via HKDF (D-017, D-023). Password and configured OTP fields are masked in attempt data. Axes access logs are disabled.
  * Registration, password reset and email address changes are also rate-limited, both per IP address and per normalised target email address, so that the functions cannot be used to flood a mailbox or to repeatedly replace another person's pending registration.
    * The per-address limit restricts the number of emails sent to an address within a time window. A request above the limit is not processed: no email is sent, and a pending registration or pending email change is not replaced. Above the limit, the most recent verification link and the password reset links already sent stay valid, so that third parties cannot prevent the account owner from using a link already in their mailbox. Below the limit, a new registration still replaces the pending one; its new link goes to the same mailbox. Likewise, a new password reset request below the limit makes the earlier reset links of the account invalid, so that only the most recent link works; it goes to the same mailbox (D-042).
    * Exceeding a limit changes neither the response nor the response time (no hint about the existence of accounts).
    * The counters of the per-address limits contain addresses of persons who may have no account. They are kept only for the duration of the time window and, where the package allows it, stored as a keyed hash (HMAC-SHA256 with a key derived from `SECRET_KEY` via HKDF; D-023) instead of the address.
    * Implementation (D-038): the request stores only a counter entry and one job in a database outbox and makes no account lookup; a background worker looks up the address, decides and sends. The three budgets (per address, per client IP address with IPv6 counted by /64, instance wide) count a request only if all of them have room. The budgets of the client address and of the instance are read first and without storing anything: a request over one of them gets HTTP 429 and "Zu viele Anfragen" and creates no job, no counter row and no session; the budget of an address (12 per hour, 3 per 10 minutes, 24 per day) is never shown, so no answer tells whether an address has an account. A request for the address that this browser entered less than the pause ago creates no job. **Bot check, stage 0 (D-058):** a hidden field and a signed time stamp (at least 2 seconds) on step 1 and the password reset request; a hit gets the same page without a job. A proof of work is only a recommendation (not built). The information email to an existing address is sent at most once per address per 24 hours. Jobs and counters store keyed digests, except that a job that is not processed yet holds the address for at most one hour. Tokens are never stored in a job.
* Email texts that administrators can edit (D-039) are plain text with fixed `{placeholder}` markers. They are never evaluated by the template engine; a text is accepted only if all placeholders are valid for that email, the personal link of the email is present, and no web or email address is typed in (a deny-list of the usual forms, also for the subject; the permission is the protection against a person who wants to bypass it). Changing a text needs the permission "change email text" (assigned to a role at runtime) and is recorded in the administration log without the text; the history is visible to system administrators only. A test email goes only to the address of the logged-in account; earlier versions of a text are kept and can be restored after validation.
* Error responses must not give unnecessary hints about the existence of accounts, including blocked and deactivated accounts. Registration and email address changes use generic responses (`docs/PROJECT.md`, "Registration" and "Changing the Email Address").
* Email address changes require re-authentication and take effect only after the new address has been verified via a time-limited (24 hours), single-use link. The old address remains valid until then and is notified after the change. Only the most recent pending change can be confirmed; earlier links become invalid (D-059). The account can also have a **second address** (own confirmation by link, own status): it is a recipient of every notification, **never an identifier** (not for the login, not for the password reset, not for any lookup), and can be removed at once after re-authentication. A request for a new address stores a pending row for every target address, whatever it belongs to, so the answer does not tell whether it is in use; the person can cancel a pending request.
* **Stop link (D-059).** A request for a new login or second address sends a notice with a link "stop it at once" to the old login address and the confirmed second address. The link works without a login and follows the two-step pattern (a page, then a POST with the CSRF token), so a mail scanner stops nothing; it cancels all pending changes of the account, deletes all confirmation and stop tokens, ends every session of the account and logs the event. It is the one link in a notification that acts, and it can only make the account safer (it never confirms, sets or opens anything).
* Password reset links must be random, single-use, time-limited and protected against abuse. Password reset is not possible for unverified, blocked and deactivated accounts; the response is identical.
* Changes of email address, password and two-factor authentication are notified by email to **every address of the account** (for an email address change, also to the old address). This also applies when a system administrator changes the password or email address of another account. Except for the stop link above, the notification contains no link that could undo or confirm anything and tells the person (in the default text; an administrator can rewrite it, D-039) to contact the operator immediately if they did not request the change; it does not invite a reply, because automatic emails come from a noreply address by default.
* After such a report, the procedure described in the installation guide applies: block the account, verify the person's identity, restore the email address or password, log the incident.
* Sessions, cookies and CSRF protection are configured securely:
  * The session ID is renewed at login, after the second factor has been completed, and after password and privilege changes (OWASP Session Management Cheat Sheet).
  * All other sessions of an account are ended after a password change or reset, an email address change, a reset of the second factor, blocking, deactivation and removal of rights.
  * Idle and absolute session timeouts are enforced on the server (D-041): users 30 minutes idle and 8 hours absolute; staff users and system administrators 15 minutes idle and 4 hours absolute. The values are within the ranges of the OWASP Session Management Cheat Sheet (idle 15-30 minutes for low-risk applications, absolute 4-8 hours) and were set by the project owner as best practice (2026-10-13). A session without the stored start time is ended (fails closed).
  * **Stay signed in (D-056, decision of the project owner of 2026-10-14; the duration is a proposal that the security review may change).** The login page has the box "Angemeldet bleiben", **off by default**. If it is ticked, a user gets a persistent session of 14 days (`STAY_SIGNED_IN_DURATION`): the cookie and the server-side session end at an absolute time fixed at the login (Django stores a time, not a number of idle seconds, so activity does not extend it), and the idle and the absolute timeout of the server check are both this long. **Staff users and system administrators are excluded**: the box is ignored for them and their timeouts above stay. The second factor is asked **once, at the login**, as for every login; the persistent session is not created before the code was accepted, and the wish waits only in the pending login (never as a session value the client could influence). **Sensitive actions still ask for the current password and, where one is set up, the code** (changing the email address, the password, the second factor and deleting the account), independent of the length of the session; the window of five minutes for setting up the second factor counts from the login of the session (D-055), so a session that is days old always asks for the password. Logging out ends the persistent session. A longer-lived cookie on a device that is not the person's own is the residual risk; the page says "Angemeldet bleiben" without more words, and the person decides per login.
  * Session and CSRF cookies are `Secure` in stage and production and `HttpOnly` where possible.
  * `SESSION_COOKIE_DOMAIN` and `CSRF_COOKIE_DOMAIN` are not set, so that the cookies are host-only and are not sent to the instance's website or other subdomains (D-025).
  * `CSRF_TRUSTED_ORIGINS` never contains the instance's website or a wildcard for its parent domain.
  * In stage and production, the names of the session and CSRF cookies use the `__Host-` prefix. Supporting browsers accept such a cookie only if it is `Secure`, has `Path=/` and no `Domain` attribute, so that a cookie set by the website for the parent domain (cookie tossing) cannot take the place of the application's cookies (D-025). The prefix is defined in draft-ietf-httpbis-rfc6265bis, an Internet-Draft; browser support and compatibility with Django's cookie settings are verified at implementation.
  * After login, a redirect target (e.g. the `next` parameter) is followed only if it points to the application's own host; any other target is ignored.
* Security settings are secure by default and are only relaxed explicitly for `APP_ENV=local`. A missing or unknown `APP_ENV` prevents the application from starting. `DEBUG` can only be enabled with `APP_ENV=local`.
* Password reset requests respond in a comparable time regardless of whether an account exists (e.g. by sending emails asynchronously).
* Confirming an email address change or reactivating an account deletes a pending unverified registration with the same address, because the confirmation proves control of the address. A pending registration can therefore not block an email change or a reactivation.
* The initial system administrator must change the initial password and set up two-factor authentication at the first login (D-005). Its email address is entered by the operator on the server and counts as verified. The setup command treats any account with `is_superuser`, including blocked and deactivated ones, as an existing system administrator. Until the password is changed, the session can only change the password and log out; the new password must differ from the initial one; after that, the second factor must be set up before anything else (D-041).
* Reset of a lost second factor (D-004, D-041): only system administrators, never for their own account. For users, the administrator starts the removal and the owner confirms it with a time-limited (24 hours, best practice as decided by the project owner), single-use link sent to the registered address; opening the link changes nothing, only the confirmation does. Staff users and system administrators are reset at once, after the administrator confirmed in the administration that the identity was verified in person or by calling back a phone number known independently of the account. The emergency command on the server (`manage.py emergency_access`) resets any account, including the last system administrator. Every reset ends all sessions of the account, notifies the owner by email without a link and is logged.
* Introduce AGOV and passkeys only after their integration and security requirements have been reviewed.
* Account linking must reliably confirm the identity.

## 4. Authorization

* Permissions are checked on the server side.
* New users are authenticated users without group memberships, additional permissions, `is_staff` or `is_superuser`.
* Authenticated users can only read and edit their own profile.
* Authorization checks use permissions, never role (group) names. Roles are runtime data and may be renamed (`docs/DECISIONS.md`, D-012).
* Only system administrators can create and change roles and their permissions, assign roles, grant or remove direct permissions, and grant or remove `is_staff` and `is_superuser`. This is enforced technically, including for staff users who have permissions to add or change users. Reason: "If you give a non-superuser the ability to edit users, this is ultimately the same as giving them superuser status because they will be able to elevate permissions of users including themselves!" (Django documentation, "Using the Django authentication system").
* Staff users can never change, block, deactivate or delete the accounts of staff users or system administrators, including their own account in the admin, and never change the password, email address or second factor of another account.
* The customer number, the retention flag (which can only be set by application functions, never unset) and the normalised unique email column cannot be changed in the admin by anyone, including system administrators. The customer number and the retention flag are shown read-only; the normalised column is not shown, because it repeats the email address (D-035).
* Every path that deletes, deactivates, reactivates, blocks, unblocks, promotes or demotes accounts (`is_staff`, `is_superuser`, groups, permissions), changes the email address or the normalised email column, or changes the retention flag (single objects, admin bulk actions, `list_editable`, `QuerySet.update()`/`delete()`, management commands) goes through the central account logic. Django's bulk action "delete selected objects" uses `QuerySet.delete()` and does not call the model's `delete()` method; it is therefore disabled for the user model or replaced by an implementation that uses the central logic (Django documentation, "Admin actions").
* Changes by a system administrator to the password or email address of another account are logged and notified to the affected account.
* Any promotion beyond the authenticated user requires an explicit action by an authorized superior. Nobody can promote their own account; `is_superuser` is only granted by another system administrator. Every promotion is logged.
* `is_staff` and `is_superuser` are granted only to accounts that have already set up two-factor authentication (a confirmed authenticator app; an unconfirmed app or recovery codes alone do not count). The administration refuses the change with an explanation, and the rule is checked again on the locked account row when the change is saved (D-042). The only account that receives these rights without a second factor is the initial system administrator created on the server (D-005), which must set it up at its first login. Because the rule proves only that a confirmed authenticator app exists, not that the owner controls it, the administrator confirms with the person before granting rights that they set up the second factor themselves. Removing rights is always possible.
* System administrators can block other system administrators and remove their rights. At least one active, not blocked system administrator always remains. Emergency access is restored only via a management command on the server.
* Delegating role assignment to roles other than system administrator requires a separate, reviewed decision, because it enables privilege escalation.
* The number of system administrator accounts is kept to a minimum. System administrators (`is_superuser`) bypass all permission checks; their administrative actions are logged.
* Hidden menus or buttons do not count as access control.
* Direct URLs, manipulated IDs, API endpoints and exports must be covered by tests.
* Superuser status is reserved for system administrators and is not used as a general role model.
* `ALLOWED_HOSTS` lists only the host names of the application (not the instance's website; D-025). Code never reads the `Host` header directly from `request.META`. Links in emails are built from the canonical base URL in the instance configuration, not from the request.

## 5. Data Protection

* Collect only necessary data. The purpose of each profile field is documented (`docs/PROJECT.md`, "Profile Data and Purpose"); the phone number is optional.
* Persons are informed about the collection of their data through the instance's privacy notice, which is linked at registration and on all pages accessible without login (DSG Art. 19; D-022).
* Account deletion removes all data of the account unless a retention obligation is set; in that case the account is deactivated (D-016). Security log entries are kept without personal reference until they expire (D-018).
* The retention obligation flag can never be unset once it has been set.
* Do not write personal data to logs unnecessarily.
* Backups are the responsibility of the operator. The documentation states that deleted data can remain in backups until they expire.
* Assess legal data protection and retention requirements only against official sources (e.g. legal texts on fedlex.admin.ch). Legally unclear points are not assumed but clarified with the project owner.
* Error pages and responses must not disclose secrets, stack traces or internal configuration details.
* Development and test environments do not use real production personal data.

## 6. Web Security

Consider at least:

* CSRF
* XSS
* SQL injection
* insecure direct object references
* session and cookie security
* secure HTTP headers: HSTS in stage and production, Content Security Policy, `X-Content-Type-Options`, `Referrer-Policy: same-origin` or stricter (Django's default `SECURE_REFERRER_POLICY` is `'same-origin'`), protection against framing
* no inline scripts and no inline styles that would require `'unsafe-inline'` in the Content Security Policy; the policy is set with Django's built-in `ContentSecurityPolicyMiddleware` and `SECURE_CSP` (at least `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`) and is never relaxed globally (D-028)
* pages whose URL contains a token (email verification, password reset and other links sent by email) remove the token from the URL before rendering, following the approach of Django's password reset view; the exact mechanism is verified against the installed Django version
* static assets, including Tabler, fonts and icons, are served by the instance itself in fixed versions, not loaded from third-party CDNs
* the application runs on its own host name, never under a path of another website, so that it does not share an origin with it (D-025); on a subdomain of the website's registrable domain it is still the same site, so the cookie rules in section 3 apply and the installation guide states the residual risk
* logo files are static files provided by the operator and are embedded as images, never inlined into the HTML; static SVG logo files are checked for scripts and external references before deployment and/or served with a restrictive Content Security Policy (`docs/DESIGN.md`, section 10)
* instance branding values are validated: colour values match `^#[0-9A-Fa-f]{6}$`, configured URLs (privacy notice, website) are absolute `https://` URLs, logo references point to a file inside the instance's static files with an allowlisted extension (no absolute paths, path traversal or external URLs); values are validated at startup (configuration) or when saved (database), and invalid values are rejected (`docs/DESIGN.md`, section 3.2)
* the colour mode and the language are chosen in the browser and kept in the `sessionStorage` of the tab only (no cookie, nothing sent to the server); the scripts are external files, read only allowlisted values and evaluate no URL parameters for theme or branding settings (`docs/DESIGN.md`, section 7; D-056)
* secure uploads, if introduced later; this includes any later web upload of logo files
* rate limiting and abuse protection
* secure error handling
* protection against accidental disclosure of files and configuration

Django's security features must be configured correctly and must not be disabled without a documented, reviewed justification.

Sources: https://docs.djangoproject.com/en/6.0/ref/settings/ (`SECURE_REFERRER_POLICY`), https://docs.djangoproject.com/en/6.0/ref/csrf/ ("Limitations"), https://docs.djangoproject.com/en/6.0/ref/csp/ and https://docs.djangoproject.com/en/6.0/howto/csp/ (Content Security Policy), https://www.w3.org/TR/CSP3/ (directive names; Working Draft of 16 September 2026, checked on 2026-10-11), draft-ietf-httpbis-rfc6265bis (cookie name prefixes), https://www.w3.org/TR/svg-integration/ (as cited in the security review of 2026-10-11; re-checked against the installed versions at implementation)

## 7. Secrets

* No secrets in Git.
* No secrets in source code, documentation, logs or error messages.
* Local secrets in ignored files or environment variables.
* Server secrets via protected environment variables or a suitable secret store.
* The TOTP key of the second factor is encrypted in the same way (own HKDF label, Fernet, fallback keys tried and the key encrypted again when used); recovery codes are stored only as keyed digests, also checked against the digests of the fallback keys (D-040). No security value is displayed again.
* Exception by decision D-006: the private key of the Microsoft Graph certificate is written in the Django admin and stored encrypted in the database; it is never displayed again:
  * The encryption key is derived from `SECRET_KEY` with HKDF and a fixed, purpose-specific label; encryption uses Fernet. Both come from the established `cryptography` library; no custom cryptographic primitives are developed.
  * Keys derived from `SECRET_KEY_FALLBACKS` are tried for decryption; the secret is then encrypted again with the current key.
* Never display secrets in plain text again.
* Keep credentials for local, stage and production separate, and separate per instance.
* The initial system administrator password is entered interactively and never stored in `.env` or other files (D-005).
* In case of accidental disclosure, the affected secrets must be revoked or rotated. If `SECRET_KEY` is changed without keeping the old key in `SECRET_KEY_FALLBACKS`, the Microsoft Graph certificate and its private key must be uploaded again.
* No production or stage secrets on development machines. Read deny rules in `.claude/settings.json` additionally keep AI tools away from secret files; they do not apply to scripts that open files themselves and are therefore not a complete protection (D-020).

## 8. Microsoft Graph

* Use only the required permissions.
* By default, the application permission `Mail.Send` allows sending mail as any user of the Microsoft organisation. The application must therefore be restricted to the sender mailbox with "RBAC for Applications" in Exchange Online, and no organisation-wide `Mail.Send` permission may remain in Microsoft Entra ID.
* The installation guide explains these steps for operators without in-depth IT knowledge.
* Authentication uses a certificate, not a client secret: Microsoft recommends a certificate for production (D-006, Microsoft Learn, checked 2026-10-14).
* The private key is write-only in the admin and stored encrypted; only the public certificate and its expiry date are displayed.
* Take expiry and renewal of credentials into account; the admin warns before expiry.
* The configuration is accessible only to system administrators; every change is logged.
* The test send never displays secrets.
* Email logs must not contain tokens, passwords or complete security links.
* Local and stage delivery must be controlled by default. Locally, output to the console (the default) and delivery via Microsoft Graph restricted to allowed recipients are possible; stage allows only delivery restricted to allowed recipients. Unrestricted delivery is only possible in production, and in production only unrestricted delivery via Microsoft Graph is allowed, so that security links never end up in console or server logs (D-028).
* Local delivery via Microsoft Graph uses a dedicated development app registration and sender mailbox whose credentials are separate from stage and production (section 7; D-020). The allowed local recipient addresses are kept only in the environment variable `EMAIL_ALLOWED_RECIPIENTS` on the developer machine, never in the repository. `allowlist` without allowed recipients refuses to start; each entry must be one complete address. Debug pages and error reports mask the allowed recipients.
* Automated tests never send real email (Django's test runner replaces the email backend with its in-memory backend) and use only email addresses under domains reserved for testing and documentation (RFC 2606: `example.com`, `example.net`, `example.org`, `.test`, `.invalid`).
* Automatic emails do not invite a reply; a mailbox that receives replies anyway can contain personal data.

Source: https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac

## 9. Database and Operations

* Database access with the minimum required privileges.
* No publicly accessible database administration.
* Encrypted transport in stage and production.
* Keep migrations traceable and reviewable.
* Test backups and restore before going into production.
* Regularly check security updates and dependencies. Only versions that still receive security updates from their vendor are declared as minimum versions (D-015).
* Check the production configuration with the available Django checks.

## 10. Security Logging

* Logged events include at least: failed logins and lockouts, password changes and resets, email address changes, setup, removal and reset of two-factor authentication, use of recovery codes, role, permission, `is_staff` and `is_superuser` changes, blocking, unblocking, deletion, deactivation and reactivation of accounts, setting of the retention obligation, use of emergency management commands, and changes to the Microsoft Graph configuration and to the portal settings (the addresses of the privacy notice and the legal notice, D-056; the log line names only the host names of the old and the new address).
* Logs never contain passwords, tokens, secrets or complete security links, and only as much personal data as necessary.
* Only system administrators can view security logs. The security log is append-only within the application: nobody, including system administrators, can create, change or delete entries through the admin or other application functions. Entries are deleted only by the automatic retention. This also applies to the failed-attempt and lockout records of the abuse protection package: if its admin allows changing or deleting them, the admin is restricted accordingly (to be verified against the installed version), or failed logins and lockouts are additionally written to the application's own append-only log.
* Log entries of deleted accounts are kept without personal reference; they use an internal number, not the customer number, because the customer number appears on invoices. The Django admin's log entries of accounts store only a neutral text with the internal number; while the account exists, a system administrator can map an entry to it through the internal number shown on the change form; after deletion this mapping is intentionally lost, as long as no other kept record (backups, a future security log, invoices) maps the internal number to a person (D-036). The admin history of accounts, which shows the acting account's email address, is available only to system administrators.
* Retention (D-018): configured per instance in the server configuration (environment: `SECURITY_LOG_RETENTION_DAYS` and `SECURITY_LOG_LONG_RETENTION_DAYS`, a lower value than the default prevents the start), not in the admin, with minimum values that cannot be undercut. Default: 1 year for changes of roles, permissions, `is_staff` and `is_superuser`, resets of the second factor of staff users and system administrators, blocking and unblocking of staff users and system administrators, changes by system administrators to the password or email address of another account, deletion, deactivation and reactivation of accounts by system administrators, use of emergency management commands and changes to the Microsoft Graph configuration and to the portal settings; 30 days for all other log entries, including the failed-attempt records of the abuse protection package. Expired entries are deleted automatically.
* The OWASP Logging Cheat Sheet is used as the basis and verified before implementation.

## 11. Tests

Security-critical functions require positive and negative tests. This list is the only list of security-relevant acceptance criteria; `docs/ACCEPTANCE.md` refers to it and does not repeat it.

At least:

Accounts and authentication:

* Accounts without a verified email address or without a password cannot log in.
* Login works with the email address, regardless of upper and lower case and surrounding whitespace; failed attempts are counted per normalised email address. The customer number is not accepted as a login identifier.
* Login with an email address that also belongs to deactivated accounts resolves exactly the account that is not deactivated.
* Login with a nonexistent email address returns the same response and the same lockout behaviour as a wrong password.
* Unverified accounts cannot reset their password; the response is identical.
* The password is set only after verification; unverified accounts and their data are deleted when the verification link expires; a new registration with the address of an unverified account replaces the pending registration.
* Registration ignores submitted fields other than the email address, and completing it ignores fields other than the profile fields and the password; new accounts have no group memberships, permissions, `is_staff` or `is_superuser`.
* All other sessions of an account are ended after a password change or reset, an email address change, a reset of the second factor, blocking, deactivation and removal of rights.
* Idle and absolute session timeouts apply; staff users and system administrators have a shorter idle timeout. An expired session cannot be used again, and a request after the timeout changes nothing.
* Profile cards and the second address (D-059, `test_profile.py`): every card action needs a login and the CSRF token (negative tests anonymous and without token); the cards change the account of the request only; a change of the login or second address needs the password (and the code where a second factor exists) and creates a pending row for an address in use by another account exactly as for a free one, with the same response; the second address never logs in and never receives a reset; the stop link works without a login, is a two-step page, cancels pending changes and ends the sessions; the recovery codes are in the answer of the creating POST only and "Fertig" without the tick shows none; every notification goes to both addresses; the deletion result page is rendered directly and the mail goes to both addresses; `Cache-Control: no-store` on every profile page.
* Bot check and limits of the mail forms (D-058, `test_mail_queue.py`, `test_registration.py`): a request over the limit of the client address or of the instance creates no job, no bucket and no session and gets HTTP 429, a request over the limit of an address gets the same page as any other and no 429; the honeypot or a missing, forged or too fresh stamp gets the same page without a job; within the pause the same address creates no new job.
* Stay signed in (D-056): off by default; ticked, a user gets a session of 14 days with an absolute end that does not slide with activity (cookie and server-side row); staff users and system administrators never get it; with a second factor the persistent session starts only after the code, and an abandoned second step leaves nothing behind for a later login of the same browser; a login without the box ends the persistence of the same session; sensitive actions still ask for the password (`test_stay_signed_in.py`).
* The reset of a lost second factor is possible only for system administrators and never for their own account; for a user, nothing changes before the owner confirms the link (single use, expiry, replaced by a newer link, invalid after a promotion); a staff user or system administrator is reset only with the identity confirmation; every reset ends all sessions, notifies the owner and is logged.
* The initial administrator command refuses while any system administrator exists (also a blocked or deactivated one), reads the password only interactively, prints no secret and logs; the first login can only change the password (to a different one) and log out, then only set up the second factor, and the administration stays closed.
* The security log is append-only (no change or deletion through the application or the administration, only for system administrators to view) and deletes entries only by the retention.
* Session and CSRF cookies are `Secure` in stage and production.
* Session and CSRF cookies have no `Domain` attribute; in stage and production their names use the `__Host-` prefix.
* `CSRF_TRUSTED_ORIGINS` does not contain the instance's website or a wildcard for its parent domain; a POST request with an `Origin` header of the website's host is rejected.
* After login, a redirect target is accepted only on the application's own host; an external `next` parameter is ignored.
* Password validators enforce a minimum length of 15 and accept at least 64 characters.
* Logout ends the session; the session ID is renewed at login, after the second factor has been completed, and after password and privilege changes.
* Blocked and deactivated accounts cannot log in; they cannot reset their password; responses do not reveal their existence.
* Repeated failed login and recovery attempts are throttled per account and per IP address; a temporary lockout expires; a password reset is possible during a temporary lockout; a successful reset ends the lockout of that account but not the counters and lockouts of IP addresses.
* Wrong codes use up the budget of the account: an email at the notice, a pause at the limit even for a correct code, a password reset ends the pause and leaves at most the notice value (once per window); attempts refused by a lockout and wrong passwords do not count; a successful login does not reset the budget; parallel wrong codes cannot exceed the limit.
* The client IP address is not taken from client-controlled headers unless they are set by a configured trusted proxy.
* Registration, password reset and email address change requests are rate-limited per IP address and per normalised target email address; variants in upper and lower case do not bypass the limit; exceeding a limit changes neither the response nor the response time.
* Above the per-address limit, no email is sent and no pending registration or pending email change is replaced; a password reset or verification link sent earlier still works. Below the limit, a new password reset request makes the earlier reset links of the account invalid.
* An attacker's IP address stays locked after the attacker successfully resets the password of their own account.
* Login, registration, recovery and email change responses do not reveal whether an account exists.
* Registration with an address of an active, temporarily locked or blocked account returns the same response, creates no account and sends an information email to that address.
* Verification, email change, password reset and second-factor removal links are time-limited and cannot be reused.
* Confirming an email address change or reactivating an account deletes a pending unverified registration with the same address.
* An email address change without re-authentication is rejected; before verification of the new address, the account still uses the old address; after verification, the old address is replaced and notified.
* An email address change to an address of an active, temporarily locked or blocked account is not performed and returns the same response as a successful request; the check is repeated when the link is confirmed.
* Only the most recent pending email address change can be confirmed.
* Email addresses are unique among accounts that are not deactivated, enforced by the database, including for concurrent requests and differences in upper and lower case.
* Customer numbers are assigned only when the registration is completed (password form submitted successfully via the verification link); an account that opened the link but did not set a password has neither a password nor a customer number and is deleted when the link expires. Customer numbers are unique and never reused, including numbers of deleted accounts. Confirming an email address change and reactivation keep the existing number; the initial system administrator receives a number at setup.
* Changes of email address, password and two-factor authentication send a notification without an undo or confirmation link.

Two-factor authentication:

* Staff users and system administrators cannot use any function, including the admin, without completed two-factor authentication; such an account without a second factor must set it up at its next login. `is_staff` and `is_superuser` cannot be granted to an account without a confirmed second factor, also not when the second factor is removed between the check of the form and the save.
* Re-authentication requires the second factor if it is set up; removing two-factor authentication or generating new recovery codes without re-authentication is rejected. Setting up two-factor authentication needs the current password only if the session did not authenticate within the last five minutes: within that window it works without the password, after it the password is required; the window is measured by a server-side timestamp that activity, a password change and a password reset do not extend (D-055).
* Completing the registration logs the person in, once, with no rights; a used or replaced link cannot log anyone in; the start page, the hide button of a task and the start of the setup need a login and a CSRF token, and opening the setup page changes nothing.
* Registration in three steps (D-058): the password step does not activate the account and keeps only a hash that is bound to the link; the details step without the password step, with a held hash of another link, or without the consent creates nothing; a link that is used, expired or replaced leads to the page "Link nicht mehr gültig" at every step; the resend sends nothing during the pause, never to an address from the request, and is limited like every request (`test_registration.py`, `test_entry_pages.py`).
* The frame after the login (D-057): an account that must replace its password and an account with a mandatory but incomplete second factor reach nothing but the password change or the setup and see only the header and "Abmelden"; for the first, `GET /`, `POST /tasks/<key>/hide/` and `/admin/` lead to the password change and no `HiddenTask` row is written; staff without a second factor posting the hide button is sent to the setup; every page of the frame has `Cache-Control: no-store`; the hide URL answers GET, HEAD, OPTIONS, PUT, PATCH and DELETE with 405 and writes nothing, an unknown key is a 404 and is checked before anything is written, and without a login the person goes to the login page without `?next` (`test_frame.py`).
* The addresses of the legal links come from the portal settings or the instance configuration and are checked again before they are printed: only a complete `https://` address without credentials, white space, control, format or line and paragraph separator characters, otherwise no link (`test_portal_settings.py`, `test_instance_configuration.py`).
* A password reset does not remove or bypass the second factor.
* Recovery codes can be used only once; generating new codes invalidates the previous ones.
* Resetting a second factor is possible only for system administrators, is logged and is notified; the last system administrator's second factor can only be reset via the management command.
* For users, the second factor is removed only after the confirmation link sent to the registered email address has been confirmed.
* Every protected function relies on the central check of the completed second factor; no function checks TOTP devices directly.

Authorization:

* Authenticated user A cannot read or modify the profile of authenticated user B.
* Users without the corresponding permission cannot manage roles, permissions or role assignments.
* Users cannot grant themselves group memberships, permissions, `is_staff` or `is_superuser`.
* A staff user with permissions to add or change users can change neither `is_staff`, `is_superuser`, group memberships nor permissions, cannot change, block, deactivate or delete accounts of staff users or system administrators (including its own account in the admin), and cannot change another account's password, email address or second factor.
* Customer number, retention flag and the normalised unique email column cannot be changed in the admin, including by system administrators; the normalised column is not shown in the admin user form.
* Changes by a system administrator to the password or email address of another account are logged and notified.
* Unauthorized users cannot perform administrative actions.
* Direct URL calls and manipulated IDs do not bypass any authorization check.
* Authorization still works correctly after a role (group) has been renamed.
* `is_superuser` can only be granted by another system administrator; nobody can promote their own account.

Account lifecycle:

* The initial setup command does not run if a system administrator exists and never overwrites an account; it reads the password only interactively; there is no setup function on the website; the initial password does not appear in source code, files or logs.
* The initial system administrator must change the password and set up two-factor authentication at the first login.
* Account deletion requires re-authentication; without a retention obligation, all account data is removed; security log entries are kept without personal reference.
* With a retention obligation, deletion deactivates the account (shown as "Deleted (retention obligation)" in the administration); the flag cannot be unset, including in the admin. The person sees the same page and the same message as without a retention obligation (D-055).
* A deactivated account cannot be reactivated by the user; a system administrator cannot reactivate it while an active, temporarily locked or blocked account with the same email address exists.
* The last active, not blocked system administrator can neither be deleted, deactivated or blocked nor lose superuser status.
* Bulk actions and bulk edits in the admin respect the retention obligation and the last-administrator rule.

Email and Microsoft Graph:

* The Microsoft Graph configuration is accessible only to system administrators; every change is logged; the private key is never displayed after saving and is stored encrypted.
* The private key can still be decrypted with a key from `SECRET_KEY_FALLBACKS`; without a matching key, the admin clearly asks for the certificate and private key to be uploaded again.
* Local delivery is restricted to console output or to allowed recipients; stage delivery is restricted to allowed recipients; unrestricted delivery is refused outside production; any mode other than unrestricted Microsoft Graph delivery is refused in production.
* Automated tests send no real email, and the repository contains email addresses only under reserved domains (RFC 2606).
* A missing or unknown `APP_ENV` prevents the application from starting.
* The test send never displays secrets.
* Links in emails use the canonical base URL of the instance, not the `Host` header of the request.
* A request with a `Host` header not listed in `ALLOWED_HOSTS` is rejected.

Logging, configuration and repository:

* Security-relevant events are logged; logs contain no passwords, tokens, secrets or complete security links.
* Only system administrators can view security logs; nobody, including system administrators, can create, change or delete entries through the application, including the failed-attempt and lockout records of the abuse protection package.
* `DEBUG` cannot be enabled unless `APP_ENV=local`.
* Expired log entries are deleted according to the configured retention; the retention cannot be set below the minimum values.
* Error pages do not reveal stack traces or internal details.
* Secure HTTP headers are set; static assets are served by the instance itself.
* `Referrer-Policy` is `same-origin` or stricter.
* No template inlines the content of an SVG logo; logos are embedded with `<img>`.
* The scripts of the colour mode and the language ignore URL parameters and accept only the allowlisted values (`light`, `dark`; `de`, `fr`, `it`, `en`) from the `sessionStorage` of the tab (D-056).
* Invalid instance branding values (colour values not matching `^#[0-9A-Fa-f]{6}$`, URLs that are not absolute `https://` URLs, logo references outside the instance's static files, with path traversal or with a non-allowlisted extension) are rejected at startup or when saved.
* Pages reached through verification, password reset, email change and second-factor removal links redirect to a URL without the token before rendering; the rendered page contains no token in its URL.
* Secrets are not contained in the Git repository.
* Relevant Django security checks (including the deployment checks) are run.

Tests create the groups and permissions they need themselves and do not rely on predefined roles.

## 12. Handling Insecure Requests

If a requirement would significantly weaken security, the AI must not implement it silently.

It explains the risk, proposes a secure alternative and asks in case of significant conflicting goals.

Deviations must be documented traceably. Documentation alone does not make an insecure solution acceptable.

## 13. No Unsubstantiated Security Claims

The AI must distinguish between:

* implemented,
* automatically tested,
* manually checked,
* still open,
* not verified.

Every security statement names the verification step or official source it is based on. Anything not substantiated is considered not verified.

A passing test suite does not prove that the application is completely secure. Critical risks must be reported accordingly.

The subagents defined in `.claude/agents/` do not guarantee an independent security review (D-019). Critical changes additionally require a human review.
