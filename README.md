# LEG-Platform

Web application for local electricity communities (German: "lokale Elektrizitätsgemeinschaft", LEG) in Switzerland.

The project was initiated by the LEG Ittigen. The software is developed neutrally, so that other interested municipalities can run their own, independent instance from the same code base with their own configuration (`docs/DECISIONS.md`, D-013 and D-014). References to Ittigen in the documentation refer to the initiating instance.

## Project Goal

The first phase creates a secure, extensible foundation. LEG domain functions, billing and financial functions are explicitly not part of this phase.

## Technology Direction

* Backend: Python and Django
* Database: MySQL
* User interface: Tabler, responsive for desktop computers, tablets and mobile phones; design language in `docs/DESIGN.md`
* System administration: Django admin
* Outgoing email: Microsoft Graph, configured in the Django admin
* Login: email address with password
* Two-factor authentication: authenticator app (TOTP), mandatory for staff users and system administrators; a later switch to AGOV as the second factor is prepared (approval requested, answer pending)
* Development: local, then stage, later production
* Hosting preference for the Ittigen instance: Infomaniak; the final choice is open
* Deployment model: one independent instance per municipality
* Supported versions: minimum versions only, as old as technically possible while still receiving vendor security updates (D-015)
* Language of software and documentation: English (code, database, comments, commits, documents, etc.)
* User interface language: Swiss German (`de-ch`, no "ß") by default, prepared for additional languages

## Binding Rules

1. `CONDUCT.md` contains the protected rules of conduct for all AI agents ("don't be evil").
2. `AGENTS.md` contains the shared development rules, including the language rules and the obligation to work fact-based without assumptions.
3. `CLAUDE.md` is the entry point for Claude Code.
4. `docs/SECURITY.md` contains the binding security policy.
5. `docs/PROJECT.md` defines the scope.
6. `docs/ACCEPTANCE.md` defines the acceptance criteria.
7. `docs/DECISIONS.md` records all decisions with their sources.
8. `docs/DESIGN.md` defines the binding design language of the user interface (D-024).

Read the relevant rules before every larger change.

## First Phase

* Login page as the entry point; no public home page, information about the LEG is provided by its own website (D-025)
* Registration and email verification
* Login with email address, and logout, protected against automated attacks
* Two-factor authentication with an authenticator app (mandatory for staff users and system administrators, optional for users)
* Password change and password reset
* Personal user profile: viewing, editing and deleting one's own account
* All user functions fully usable on mobile phones
* Account lifecycle including deactivation for later retention obligations
* User management, role management and role assignment
* Microsoft Graph email configuration in the Django admin, including a test send
* Security logging
* Instance configuration outside the code, so that further instances can be set up
* Secure baseline configuration and automated tests
* Preparation for AGOV as the second factor: the completed second factor is checked centrally and independently of the method; this is intended to allow a switch from TOTP to AGOV without changing the architecture or weakening security, and a switch requires a new security review (`docs/PROJECT.md`, "AGOV and Passkeys")

## Not Part of the First Phase

* LEGs as entities and all domain functions
* Billing logic and invoicing
* Calculation of electricity costs or energy distribution
* Productive financial processes
* Bank file processing
* Productive payout processes
* AGOV integration (only prepared) and passkeys
* Multi-tenancy (several municipalities in one instance)
* Additional user interface languages and language selection
* Public home page and publication of aggregated figures (D-025)

## Principles

Security, data protection and server-side access control are part of development from the start. A function is not considered secure merely because it works.

Established, maintained solutions are preferred over custom developments, especially for security functions.

The software is written in English without exception. Only the user interface is language-dependent.

No assumptions are made. Decisions are based on verified facts from official sources. When in doubt, ask.

## Current Status

The project foundation and security requirements are defined. They were revised on 2026-10-09 after a consistency check and a security review, and revised again on the same day after a second consistency check (login with email address only, customer number assigned at verification, AGOV preparation, rules of conduct for agents); open points are listed in `docs/ARCHITECTURE.md` ("Deliberately Open") and in the decisions. Git 2.55.0 is installed on the development machine. On 2026-10-11 the design language was defined (`docs/DESIGN.md`, D-024), and the public home page was replaced by the login page as entry point on its own host name (D-025).

On 2026-10-11 the Git repository was initialised (branch `main`) and the ignore rules for secret files were verified with `git check-ignore`. The minimum versions and packages were determined from official sources and proposed in D-026.

On 2026-10-11 the project owner confirmed D-026 and decided D-027 (local MySQL 9.7 LTS, public repository, continuous integration). The Django project skeleton was then created: dependency declaration (`pyproject.toml`, `requirements/minimum-constraints.txt`), the project package `leg_platform` with validated, secure-by-default settings read from environment variables (D-028), tests of the settings validation, and a GitHub Actions workflow that tests the minimum versions (Python 3.11, Django 5.2, MySQL 8.4) and the newest versions (Python 3.14, Django 6.1, MySQL 9.7). Local development is described in `docs/INSTALLATION.md`. The repository is public on GitHub (D-027); secret scanning with push protection, Dependabot alerts and private vulnerability reporting are enabled, and the default workflow token is read-only. The first run of continuous integration on GitHub passed both jobs. No application functions exist yet: the code consists of the settings, their validation, a placeholder email backend and tests.

On 2026-10-11 a security review of the skeleton was carried out. Its findings were corrected: email delivery modes per environment, HSTS `includeSubDomains` configurable by the operator (D-028), stricter validation of numeric values, `HttpOnly` CSRF cookie, response-level tests with production settings, a hardened local setup script (D-027, `docs/INSTALLATION.md`), service images pinned by digest and Dependabot for actions and Python packages. The corrections are awaiting a new review.

Deployment gates: no stage or production instance is deployed before

1. a Content Security Policy is in place (decision pending with the project owner; D-028);
2. the Microsoft Graph email backend is implemented (D-006); until then, the placeholder backend raises an error on every send, so that a function sending email would answer with an error page and could reveal whether an account exists;
3. two-factor authentication and `django-axes` protect the admin and the login (D-004, D-017);
4. the custom user model exists before any persistent database is migrated (D-021);
5. tokens in URLs are redacted from request logs: Django's `django.request` and `django.security.csrf` loggers write the request path of every response with status 400 or higher (`log_response` in the installed Django source), so a token in a path would end up in the logs. A redaction filter and a test are required when the first URL with a token is introduced (`docs/SECURITY.md`, section 10).

Next steps:

1. Confirmation of D-028 by the project owner and a review of the corrections.
2. First run of the hardened setup script on the development machine and a local test run against MySQL with the restricted application account.
3. Custom user model (D-021), before any persistent database is migrated.
