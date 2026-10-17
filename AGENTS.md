# Shared Development Instructions

## 1. Way of Working

Read before starting work (the fixed core, always, and nothing more by default):

* `CONDUCT.md` (rules of conduct)
* this file (sections 2 and 3 apply to all work; section 12 to every self-improvement)
* `docs/CODEMAP.md` (where things are, where to change what, conventions, test map)

Then read only the documents and sections the task needs, chosen from this topic index. Open the section, not the whole document (the headings tell where); do not read the rest "to be safe".

| Topic | Where |
|---|---|
| Scope, terms, what is not included, registration, account states, access levels, phase 2 (management area) | `docs/PROJECT.md` (by heading) |
| Security policy: 3 authentication, 4 authorization, 5 data protection, 6 web security, 7 secrets, 8 Microsoft Graph, 9 database and operations, 10 security logging, 11 required tests | `docs/SECURITY.md` (by section) |
| Architecture: stack, users and roles, login identifiers, account lifecycle, email, management area and community domain, data storage, environments | `docs/ARCHITECTURE.md` (by heading) |
| Binding decisions by topic: 1 stack, 2 language and translation, 3 instances, configuration, hosting, 4 roles and access, 5 accounts and lifecycle, 6 authentication, second factor, links, 7 security log and sessions, 8 mail, 9 web security baseline, 10 address directory, 11 design language and UI, 12 portal settings, 13 management area, 14 operations, local setup, tests, 15 open points, 16 agent workflow and conduct, 17 superseded IDs | `docs/DECISIONS.md` (the sections of the task; an ID such as D-086 in a code comment is found by searching for it) |
| User interface: colours, typography, navigation, layout, print, Tabler | `docs/DESIGN.md` (only for UI changes) |
| Acceptance criteria (1 setup, 2 login, 3 two-factor, 4 profile, 6 roles, 8 email, 9 security log, 12 design, 13 language) | `docs/ACCEPTANCE.md` (the sections of the task) |
| Local environment, start and stop, test accounts, test runner and areas (section 13), troubleshooting | `docs/STARTUP.md` (by section) |
| Deployment, installation, configuration variables, operation of an instance | `docs/DEPLOYMENT.md`, `docs/INSTALLATION.md`, `docs/OPERATIONS.md` (operations tasks only) |
| Third-party services that receive personal data; licences | `docs/SERVICES.md`, `docs/THIRD_PARTY_LICENSES.md` (only when adding or changing a service or dependency) |
| Current status and next steps | `README.md` |

A task that touches authentication, authorization, accounts, sessions, email, logging, secrets or web security reads the matching sections of `docs/SECURITY.md` and the decisions of the matching sections of `docs/DECISIONS.md` in any case; this reduction of the reading never lowers a security rule.

When making changes, also read all directly affected files and tests (find them through `docs/CODEMAP.md`, not by searching the whole code). A change that adds, moves, renames or removes a module, class or entry point named in `docs/CODEMAP.md` updates the map in the same change.

Implement in small, verifiable steps. Do not build extensive functionality in advance that is not part of the agreed scope.

If a requirement is unclear:

* Do not make assumptions about requirements, the project owner's intentions or domain rules. Ask.
* Low-risk technical details may only be decided without asking if the chosen solution is documented as the standard or recommendation in the official documentation of the technology concerned. Record the decision together with its source.
* Always ask if a decision has significant impact on security, data protection, data migration or external services.
* Do not claim a requirement is fulfilled unless it has been verified.

The binding rules on language (section 2) and fact-based work (section 3) apply to all work without exception.

## 2. Language: Software in English, User Interface Localized

### 2.1 Principle

All software and all project documentation are written in English without exception. Only the texts of the user interface are language-dependent. The default user interface language is Swiss German (`de-ch`). Additional languages must be possible later without restructuring.

### 2.2 Communication with the Project Owner

Communication between the project owner and the AI (questions, answers, reports, proposals) is in German.

Instructions the project owner gives in German that affect code, the database, configuration, commits, documentation or any other part of the repository are translated into English when they are written into the repository. German wording from conversations is never copied into the repository, except for user interface texts in the Swiss German translation files.

### 2.3 Always English

* Identifiers in source code: variables, functions, classes, modules, packages, Django apps, constants
* Database: table, column, index and constraint names as well as stored key values (e.g. choice values for statuses)
* Django models, fields, migrations and migration names
* Permission codenames
* Comments and docstrings
* Commit messages, branch names, pull request titles and descriptions, tags
* Log messages and internal error and exception messages
* Test names, test descriptions and test data identifiers
* File and directory names
* Configuration keys, environment variables and comments in configuration files (e.g. `.env.example`)
* URL paths, URL names and API field names
* Names and output of management commands and scripts
* Project documentation (`README.md`, `AGENTS.md`, `CLAUDE.md`, `docs/`, `.claude/agents/`) and technical documentation in code

German terms are not used in identifiers, not even partially (no mixing such as `get_teilnehmer`).

### 2.4 Language-Dependent (Default Swiss German)

Only texts that end users see are language-dependent:

* Texts in templates and forms, labels, help texts, validation messages
* Messages to users (e.g. Django messages)
* Labels in the Django admin (`verbose_name`, `verbose_name_plural`, choice labels)
* Content of emails sent to users
* Error pages for users

Data entered at runtime (e.g. names, addresses, role names created in the admin) is stored as entered and is not covered by these rules.

### 2.5 Implementation

* User-facing texts are marked with Django's translation mechanism: in Python with `gettext` or `gettext_lazy`, in templates with `{% load i18n %}`, `{% translate %}` and `{% blocktranslate %}`.
* Source texts (message IDs) in code are English. The Swiss German texts are kept in the project translation files under `LOCALE_PATHS` (`locale/de_CH/LC_MESSAGES/django.po`).
* User-facing texts are never hard-coded without being marked as translatable.
* Every user-facing text requires a Swiss German translation. Missing translations are defects, because English texts would otherwise appear in the user interface.
* Swiss German spelling applies: the character "ß" is never used; "ss" is used instead.
* Django's own `de_CH` locale only provides date and number formats, not texts. Texts therefore fall back to Django's `de` translations, which contain "ß" (e.g. "Schließen" in the admin, "Datum außerhalb des zulässigen Bereichs"). The project translation files must override every such text that can appear in the user interface, including texts from Django, its contrib apps and any third-party package. An automated check ensures that no displayed text contains "ß".

Sources: Django documentation "Translation" (https://docs.djangoproject.com/en/stable/topics/i18n/translation/), settings `LANGUAGE_CODE` and `LOCALE_PATHS` (https://docs.djangoproject.com/en/stable/ref/settings/), Django source `django/conf/locale/de_CH/` and `django/conf/locale/de/` (https://github.com/django/django, branch `main`, checked on 2026-10-09; must be re-checked against the installed Django version).

## 3. Fact-Based Work: No Assumptions

### 3.1 Principle

Do not make assumptions. Every statement, decision and implementation must be based on verified facts. If something cannot be reliably verified, ask.

### 3.2 What Must Be Verified

* Existing code, configuration and documentation: read instead of guessing.
* Behaviour of libraries, frameworks and APIs: look it up in the official documentation of the version in use.
* Versions, compatibility and support periods: check against official release and support information and verify in the actual environment (e.g. query the installed version).
* Legal and regulatory requirements (e.g. data protection, electricity law): only on the basis of official government sources.
* Results of tests and checks: report only what was actually executed and observed.

Knowledge from an AI model's training may be outdated or wrong. It does not replace verification.

### 3.3 Permitted Sources

Only official, fact-based sources are permitted, for example:

* official documentation, release notes and security advisories of vendors and projects (e.g. docs.djangoproject.com, docs.python.org, dev.mysql.com, learn.microsoft.com)
* official source code repositories and package indexes of the projects used
* official legal texts and publications of authorities (e.g. fedlex.admin.ch and the responsible federal offices)
* recognised standards and expert bodies (e.g. OWASP, IETF RFCs, W3C)
* documents and statements provided by the project owner

Tabloid and news portals, blogs, forums, Q&A sites, social media, SEO guides, content of unclear origin and AI-generated texts are not a permitted basis. Such content may at most serve as a hint that is subsequently verified against a permitted source.

### 3.4 Handling Uncertainty

* If something cannot be confirmed against a permitted source or the actual state of the project, it is considered unverified.
* Unverified points are not presented as facts and are not implemented silently.
* When in doubt: ask before acting.
* If sources or project documents contradict each other, name the contradiction and ask.

### 3.5 Evidence

* Decisions in `docs/DECISIONS.md` state their sources.
* Reports distinguish between "verified (with source or verification step)" and "unverified".

## 4. Security Keeper

The AI takes responsibility for actively identifying and raising security risks with every change.

`docs/SECURITY.md` is binding. A suggestion from the project owner or another agent does not automatically override these rules.

If a requested change would weaken protective measures:

1. Explain the concrete risk.
2. Propose a more secure alternative.
3. Do not implement the insecure variant silently.
4. Document approved, justifiable deviations including risk and rationale.

Legal obligations and indispensable protective measures must not be overridden by a documented exception.

## 5. Scope of the First Phase

Implement exclusively the first phase described in `docs/PROJECT.md`.

No billing logic, no productive financial processes and no bank file processing.

The data structure may allow later extensions but must not be overloaded with speculative future functionality.

## 6. Roles and Access Control

The role model is defined in `docs/DECISIONS.md` (D-012). In short:

* Three access levels are fixed: the **system administrator** (responsible for the instance, implemented as Django `is_superuser` with `is_staff`, has all permissions), the **staff user** (`is_staff` without `is_superuser`, only the permissions assigned to it) and the **authenticated user** (a logged-in user without additional permissions).
* Only system administrators change `is_staff`, `is_superuser`, group memberships, permissions and roles. This is enforced technically, including for staff users with permissions to change users (`docs/SECURITY.md`, section 4).
* The terminology is defined in `docs/PROJECT.md`, section "Terminology".
* All further roles are created, renamed and linked to permissions at runtime in the admin as Django groups. They are data, not code.
* No role is hard-coded. No code, migration or fixture creates, names or checks specific roles.
* Authorization checks use permissions only, never group names.
* **One approved exception (D-086, owner decision of 2026-10-17): the management area (`/management/`) is open to every active staff user with a completed second factor; it has no permissions of its own.** Every staff account can therefore see and change all customer data there (accepted risk, mitigations in D-086). The Django admin and everything outside the management area keep the permission rules above.

New users are authenticated users without group memberships, without additional permissions, without `is_staff` and without `is_superuser`. Any promotion beyond the authenticated user always requires an explicit action by an authorized superior. Nobody can promote their own account.

Access rights must be checked on the server side. Hiding menu items is not sufficient.

Authenticated users may only read and edit their own profile. Test unauthorized access via direct URLs and endpoints.

Superuser status (`is_superuser`) is reserved for system administrators and is not used as a general role model: no other role is ever implemented by granting `is_superuser`.

### Instances

The software is called LEG-Platform (initiated by the LEG Ittigen, developed neutrally; D-014). Each municipality runs its own, independent instance of the application with its own database, configuration and users (`docs/DECISIONS.md`, D-013). The application is not multi-tenant.

* Never hard-code instance-specific values (e.g. the name of the community or municipality, domains, sender addresses, contact details) in code, templates or migrations. They belong to the instance configuration.
* A new instance must be set up from the same code base using only the documented installation steps and its own configuration.

## 7. Quality

* Prefer established, maintained packages over custom developments, especially for security functions. Do not reinvent the wheel.
* Specify only minimum versions: as old as technically possible, but still receiving security updates from their vendor, proven by tests (`docs/DECISIONS.md`, D-015). Never use a package with known security vulnerabilities to support old versions.
* Use the newest possible version: the minimum versions are only declared and tested. Whenever a version is chosen for use (development environment, installation, recommendations), the newest compatible release is used; for components with long-term support tracks, the newest release of that track (D-015).
* Prefer Django's security features.
* Write tests for new functionality and security-critical cases.
* Run appropriate tests and available security checks. Run the tests of the area that a change touches (scenario run, `.\scripts\test.ps1 -Changed` or `-Area <name>`; `docs/STARTUP.md`, section 13; D-071), not the full suite after every change. The full suite is required for core changes (the runner widens the run by itself), before a deployment gate, in continuous integration and once per commit block by the main session.
* Document failed or skipped tests.
* Never present a dummy implementation as a finished security feature.
* Do not change existing architecture or dependencies unnecessarily.
* Follow the language rules in section 2.

## 8. Secrets

Never commit real credentials, tokens, passwords, private keys or confidential configuration to Git.

Never output secrets in logs, error messages, screenshots or test output.

Use locally ignored configuration files, and protected environment variables or a suitable secret store for server environments.

Do not read `.env` or other secret files directly; `.env.example` documents the variables. Do not keep production or stage secrets on development machines (`docs/DECISIONS.md`, D-020).

* Never print the process environment (e.g. `env`, `set`, `Get-ChildItem env:`); local configuration and secrets are kept in environment variables (D-027).

Never publish project content (e.g. as a shared page or to an external service) without the project owner's consent.

## 9. Database and Migrations

Data model changes must be made through traceable Django migrations.

Database objects and migrations are named in English (section 2).

Migrations do not create roles (groups) or assign permissions to roles (section 6).

Migrations do not contain instance-specific data (section 6).

Avoid destructive changes without explicit justification, a backup plan and a migration plan.

Never use production data in local tests.

## 10. Completing Each Unit of Work

Report briefly:

* What was changed?
* Which security aspects were considered?
* Which tests were run and with what result? Name the area or labels and the number of tests, and say whether the full suite was run; a scoped run is never reported as a full run.
* Which points were verified, and against which sources or verification steps?
* What is still open?
* Which risks or unverified points remain?

Never claim that a security check succeeded if it was not executed.

## 11. Delegation to Subagents

Agents may delegate clearly bounded, simpler tasks to subagents, e.g. to a less expensive model, to save resources (`docs/DECISIONS.md`, D-019). The following rules apply:

* The delegation prompt states unambiguously: the task, the files and directories that may be read or changed, the expected result, what must not be done, and the documents the subagent must read.
* A subagent does not make decisions about requirements, architecture, security or data protection. If such a question arises, it stops and reports it.
* Security-critical code (authentication, authorization, account lifecycle, secrets, cryptography, logging of security events) is not delegated to a less capable model.
* The delegating agent remains responsible. It must review the subagent's result in full against the task and the project rules before using it, and must not pass on unchecked results as verified.
* The report of the delegating agent states which tasks were delegated and how their results were checked.
* Subagents follow the same rules as all agents, in particular the language rules (section 2), fact-based work (section 3) and the secret rules (section 8).

## 12. Conduct and Self-Improvement of Agents

### 12.1 Conduct ("Don't Be Evil")

The binding rules of conduct for all agents are in `CONDUCT.md`. Every agent reads and follows them. `CONDUCT.md` and its protection in `.claude/settings.json` are never deleted, weakened or bypassed; only the project owner changes them.

### 12.2 Self-Improvement

Agents may improve their own agent files (`.claude/agents/`), the project instructions (`CLAUDE.md`, `AGENTS.md`) and their way of working, and record lessons learned, under the following conditions:

* `CONDUCT.md` always takes precedence. An improvement never weakens security rules, review steps, tool restrictions of other agents or the protection of `CONDUCT.md`.
* Improvements follow the language rules (section 2) and fact-based work (section 3).
* Every change is reported to the main session and the project owner with file and reason. Changes that amount to a decision are recorded in `docs/DECISIONS.md`.
* The following items are never removed or weakened by self-improvement:
  * the references to `CONDUCT.md` in `CLAUDE.md`, in section 1 and section 12.1 of this file and in the reading list of every agent file;
  * section 12 of this file;
  * the `tools` list of `.claude/agents/security-reviewer.md`;
  * the separation of reviews: the main session starts reviews, and no agent starts or instructs the review of its own work (`.claude/agents/developer.md`, D-019).
* The main session checks changes to `.claude/**`, `CLAUDE.md` and `AGENTS.md` before the next review run, in particular the items above (D-019).
* After a change to an agent file, the session is restarted before the agent is relied on (D-019).
* Changes to `CONDUCT.md` and `.claude/settings.json` are only proposed; the project owner applies them.
