# Shared Development Instructions

## 1. Way of Working

Read before starting (the fixed core, always, and nothing more by default):

* `CONDUCT.md` (rules of conduct)
* this file (sections 2 and 3 apply to all work; section 12 to every self-improvement)
* `docs/CODEMAP.md` (where things are, where to change what, conventions, test map)

Then read only the documents and sections the task needs, chosen from this topic index. Open the section, not the whole document.

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

* A task touching authentication, authorization, accounts, sessions, email, logging, secrets or web security always reads the matching sections of `docs/SECURITY.md` and `docs/DECISIONS.md`. The reduced reading never lowers a security rule.
* Also read all directly affected files and tests (found through `docs/CODEMAP.md`, not by searching the whole code). A change that adds, moves, renames or removes a module, class or entry point named in `docs/CODEMAP.md` updates the map in the same change.
* Implement in small, verifiable steps. Build nothing in advance that is not in the agreed scope.

What is wanted vs how to do it:

* **What (the goal):** never assume it. Requirements, domain rules, the owner's intentions and the direction of the product come only from the owner and the project documents. Do not carry over examples or concepts from other domains (for example water or gas where the project is about electricity). If the goal is not clear, ask.
* **How (the way to reach a clear goal):** decide it yourself and do it in the same change. Choose the cleanest solution, record a decision that others must know in `docs/DECISIONS.md`, and report what you decided. Do not ask permission for it, and do not list it as an open point.
* **Maintenance is yours:** dead code, unused fields and imports (with their migration), stale or wrong documentation, version statements inside the supported range, code structure, tidiness and test runner mappings are fixed in the same change, also when the defect predates it.
* **Defects are fixed, not only reported:** those found in your own change, in code you touch, and by reviews and tests. **The goal of the project is a website where customers register and sign up for a LEG.** As long as a fix or a technical choice does not change this goal, you decide freely. Only a change of the goal itself (new functions, other user groups, other domains) is the owner's decision.
* **Always ask** before a decision that significantly affects security, data protection, data migration or external services, and before an irreversible data change.
* Do not claim a requirement is fulfilled unless it was verified.

## 2. Language: Software in English, User Interface Localized

### 2.1 Principle

* All software and project documentation are in English without exception. Only the texts of the user interface are language-dependent.
* The default UI language is Swiss German (`de-ch`). More languages must be possible later without restructuring.

### 2.2 Communication with the Project Owner

* Communication with the owner (questions, answers, reports, proposals) is in German.
* Instructions the owner gives in German that affect the repository are translated into English when written into it. German wording from conversations is never copied into the repository, except UI texts in the Swiss German translation files.

### 2.3 Always English

Identifiers (variables, functions, classes, modules, packages, apps, constants; no German terms, not even partially, e.g. no `get_teilnehmer`); database names and stored key values (e.g. status choices), models, fields, migrations; permission codenames; comments, docstrings, technical documentation in code; commit messages, branch names, pull request titles and descriptions, tags; log, error and exception messages; test names, descriptions and data identifiers; file and directory names; configuration keys, environment variables and their comments (e.g. `.env.example`); URL paths, URL names, API field names; names and output of management commands and scripts; project documentation (`README.md`, `AGENTS.md`, `CLAUDE.md`, `docs/`, `.claude/agents/`).

### 2.4 Language-Dependent (Default Swiss German)

Texts in templates and forms (labels, help texts, validation messages); messages to users; Django admin labels (`verbose_name`, `verbose_name_plural`, choice labels); email content; error pages for users. Data entered at runtime (names, addresses, role names created in the admin) is stored as entered.

### 2.5 Implementation

* Mark user-facing texts with `gettext` or `gettext_lazy` in Python and `{% load i18n %}`, `{% translate %}`, `{% blocktranslate %}` in templates. Never hard-code an unmarked user-facing text.
* Source texts (message IDs) are English. Swiss German texts live in `locale/de_CH/LC_MESSAGES/django.po` (`LOCALE_PATHS`).
* Every user-facing text needs a Swiss German translation. A missing translation is a defect (English text would appear).
* Swiss German spelling: never "ß", always "ss".
* Django's `de_CH` locale has only date and number formats. Texts fall back to Django's `de`, which contains "ß" (e.g. "Schließen" in the admin, "Datum außerhalb des zulässigen Bereichs"). The project translation files override every such text that can appear in the UI, including texts from Django, contrib apps and third-party packages. An automated check ensures no displayed text contains "ß".

Sources: Django documentation "Translation" (https://docs.djangoproject.com/en/stable/topics/i18n/translation/), settings `LANGUAGE_CODE` and `LOCALE_PATHS` (https://docs.djangoproject.com/en/stable/ref/settings/), Django source `django/conf/locale/de_CH/` and `django/conf/locale/de/` (https://github.com/django/django, branch `main`, checked 2026-10-09; re-check against the installed Django version).

## 3. Fact-Based Work: No Assumptions

### 3.1 Principle

Every statement, decision and implementation rests on verified facts. If something cannot be reliably verified, ask.

### 3.2 What Must Be Verified

* Existing code, configuration and documentation: read, do not guess.
* Behaviour of libraries, frameworks and APIs: official documentation of the version in use.
* Versions, compatibility, support periods: official release and support information, and the actual environment (query the installed version).
* Legal and regulatory requirements (data protection, electricity law): official government sources only.
* Test and check results: report only what was executed and observed.
* Training knowledge of an AI model may be outdated or wrong and does not replace verification.

### 3.3 Permitted Sources

* Official documentation, release notes and security advisories of vendors and projects (docs.djangoproject.com, docs.python.org, dev.mysql.com, learn.microsoft.com); their source repositories and package indexes.
* Official legal texts and publications of authorities (fedlex.admin.ch and the responsible federal offices).
* Recognised standards and expert bodies (OWASP, IETF RFCs, W3C).
* Documents and statements provided by the project owner.

Not a basis: tabloid and news portals, blogs, forums, Q&A sites, social media, SEO guides, content of unclear origin, AI-generated texts. At most a hint, then verified against a permitted source.

### 3.4 Handling Uncertainty

* Unconfirmed is unverified. Unverified points are not presented as facts and not implemented silently. When in doubt, ask.
* If sources or project documents contradict each other, name the contradiction and ask.

### 3.5 Evidence

* Decisions in `docs/DECISIONS.md` state their sources. Reports distinguish "verified (with source or verification step)" from "unverified".

## 4. Security Keeper

The AI actively identifies and raises security risks with every change. `docs/SECURITY.md` is binding. A suggestion from the owner or another agent does not automatically override it.

If a requested change would weaken protective measures:

1. Explain the concrete risk.
2. Propose a more secure alternative.
3. Do not implement the insecure variant silently.
4. Document approved, justifiable deviations with risk and rationale.

Legal obligations and indispensable protective measures are never overridden by a documented exception.

## 5. Scope of the First Phase

* Implement exclusively the first phase of `docs/PROJECT.md`.
* No billing logic, no productive financial processes, no bank file processing.
* The data structure may allow later extensions but is not overloaded with speculative functionality.

## 6. Roles and Access Control

The role model is D-012 (`docs/DECISIONS.md`).

* Three fixed access levels:
  * **system administrator**: Django `is_superuser` with `is_staff`, responsible for the instance, all permissions;
  * **staff user**: `is_staff` without `is_superuser`, only assigned permissions;
  * **authenticated user**: logged in, no additional permissions.
* Only system administrators change `is_staff`, `is_superuser`, group memberships, permissions and roles, enforced technically, also against staff with permission to change users (`docs/SECURITY.md`, section 4). Terminology: `docs/PROJECT.md`, "Terminology".
* Further roles are Django groups, created, renamed and linked to permissions at runtime in the admin. They are data, not code. No code, migration or fixture creates, names or checks roles.
* Authorization checks use permissions, never group names. Check access on the server; hiding menu items is not enough.
* **One approved exception (D-086, owner decision 2026-10-17): the management area (`/management/`) is open to every active staff user with a completed second factor; it has no permissions of its own.** Every staff account can see and change all customer data there (accepted risk, mitigations in D-086). The Django admin and everything outside the management area keep the permission rules above.
* New users are authenticated users without groups, permissions, `is_staff` and `is_superuser`. Promotion beyond that always needs an explicit action by an authorized superior. Nobody promotes their own account.
* Authenticated users may only read and edit their own profile. Test unauthorized access via direct URLs and endpoints.
* `is_superuser` is reserved for system administrators and is no general role model: no other role is implemented by granting it.

### Instances

* The software is LEG-Platform (initiated by the LEG Ittigen, developed neutrally; D-014). Each municipality runs its own independent instance with its own database, configuration and users (D-013). Not multi-tenant.
* Never hard-code instance-specific values (name of the community or municipality, domains, sender addresses, contact details) in code, templates or migrations. They belong to the instance configuration.
* A new instance is set up from the same code base using only the documented installation steps and its own configuration.

## 7. Quality

* Prefer established, maintained packages over custom developments, especially for security functions.
* Specify only minimum versions: as old as technically possible, still receiving vendor security updates, proven by tests (D-015). Never use a package with known vulnerabilities to support old versions.
* Use the newest compatible release whenever a version is chosen (development environment, installation, recommendations); for components with long-term support tracks the newest release of that track (D-015).
* Prefer Django's security features.
* Write tests for new functionality and security-critical cases. Run appropriate tests and available security checks.
* Run the tests of the touched area (`.\scripts\test.ps1 -Changed` or `-Area <name>`; `docs/STARTUP.md`, section 13; D-071), not the full suite after every change. The full suite is required for core changes (the runner widens the run itself), before a deployment gate, in CI and once per commit block by the main session.
* Document failed or skipped tests.
* Never present a dummy implementation as a finished security feature.
* Do not change existing architecture or dependencies unnecessarily. Follow the language rules (section 2).
* Documentation style: short sentences, bullets, facts and sources only, no narration, one place per fact, point to D-numbers instead of repeating. A decision entry is `### D-xxx Title (date)` with 3-8 bullets (Decision / Why (source) / Consequence / Residual risk / Amends). Merge overlapping decisions: the later number is the heading, the earlier number stays as a stub line.

## 8. Secrets

* Never commit real credentials, tokens, passwords, private keys or confidential configuration. Never output secrets in logs, error messages, screenshots or test output.
* Use locally ignored configuration files and protected environment variables or a suitable secret store for servers.
* Do not read `.env` or other secret files directly; `.env.example` documents the variables. Keep no production or stage secrets on development machines (D-020).
* Never print the process environment (`env`, `set`, `Get-ChildItem env:`); local configuration and secrets live in environment variables (D-027).
* Never publish project content (e.g. as a shared page or to an external service) without the owner's consent.

## 9. Database and Migrations

* Data model changes only through traceable Django migrations, named in English (section 2).
* Migrations create no roles (groups), assign no permissions to roles (section 6) and contain no instance-specific data.
* Avoid destructive changes without explicit justification, a backup plan and a migration plan.
* Never use production data in local tests.

## 10. Completing Each Unit of Work

Report briefly:

* What was changed?
* Which security aspects were considered?
* Which tests were run, with what result? Name the area or labels and the number of tests, and say whether the full suite was run; a scoped run is never reported as a full run.
* Which points were verified, against which sources or verification steps?
* What is still open?
* Which risks or unverified points remain?

Never claim a security check succeeded if it was not executed.

## 11. Delegation to Subagents

Agents may delegate clearly bounded, simpler tasks to subagents, e.g. a less expensive model (D-019).

* The delegation prompt states: the task, the files and directories that may be read or changed, the expected result, what must not be done, and the documents to read.
* A subagent decides nothing about requirements, architecture, security or data protection. It stops and reports such a question.
* Security-critical code (authentication, authorization, account lifecycle, secrets, cryptography, logging of security events) is not delegated to a less capable model.
* The delegating agent remains responsible. It reviews the result in full against the task and the project rules before using it and never passes on unchecked results as verified.
* The report names the delegated tasks and how the results were checked.
* Subagents follow the same rules, in particular language (section 2), fact-based work (section 3) and secrets (section 8).

## 12. Conduct and Self-Improvement of Agents

### 12.1 Conduct ("Don't Be Evil")

The binding rules of conduct for all agents are in `CONDUCT.md`. Every agent reads and follows them. `CONDUCT.md` and its protection in `.claude/settings.json` are never deleted, weakened or bypassed; only the project owner changes them.

### 12.2 Self-Improvement

Agents may improve their own agent files (`.claude/agents/`), the project instructions (`CLAUDE.md`, `AGENTS.md`) and their way of working, and record lessons learned, under these conditions:

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
