# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

IntelliHRTrack is a Django 5.1 HR attendance and payroll system: biometric attendance, DTRs (daily time records), leave, and payroll for branches with COS, JO, and permanent employees. Server-rendered templates, one app (`core`), one project (`config`). There is no linter or formatter configured.

## Commands

Run from the repo root. On this Windows machine, use `.venv/Scripts/python.exe` (or activate `.venv`).

```sh
python manage.py runserver                # needs local MySQL (see Database)
python manage.py check
python manage.py makemigrations --check --dry-run --settings=config.test_settings

# Tests: always pass test_settings (in-memory SQLite, no MySQL needed)
python manage.py test core.tests --settings=config.test_settings
python manage.py test core.tests.test_dtr_rules --settings=config.test_settings
python manage.py test core.tests.test_dtr_rules.ClientExamples --settings=config.test_settings

npm run build:css    # Tailwind: core/static/src/input.css -> static/css/output.css
npm run watch:css
```

Test gotchas:

- Use the label `core.tests`. `manage.py test core`, the command `INSTALL.txt` gives, crashes during discovery with an `ImportError`, because `core/tests.py` and the `core/tests/` package both exist.
- The package shadows `core/tests.py`, so the payroll calculation and batch validation tests in that file **do not run**. Add new tests inside `core/tests/`. Helpers for creating model rows are in `core/tests/factories.py`.
- The one skipped test in `test_dtr_rules` is intentional. It marks an open client policy question (see `PENDING_POLICY` in `core/dtr_rules.py`).

## Architecture

### DTR and payroll calculation

The calculation is layered, and every screen and printout must get its numbers through it. Do not recompute lateness, deductions, or money elsewhere.

1. `core/dtr_rules.py`: `evaluate_day()` holds the daily attendance rules. It is pure, with no database access, and works in whole minutes after midnight. Its docstring lists the client's confirmed rules and worked examples, which the tests encode. Read it before changing anything about lateness, half days, or the flag ceremony.
2. `core/payroll_calculations.py`: pure money rules using `Decimal`, shared by previews, batches, payslips, and tests.
3. `core/views.py`: `_build_dtr_and_summary(profile, branch, period, rules)` is the database-facing layer. It collects `AttendanceRecord` rows, matches `AttendanceRecord.employee_id` to `UserProfile.biometric_employee_id`, and calls `evaluate_day()`.

### Saved snapshots

Processed payroll is stored, not recomputed. `FinalizedDTR` holds a locked DTR snapshot per employee and period, and `PayrollBatch` moves through draft, completed, and finalized.

Rule changes therefore do not alter saved periods:

- When daily rules change, bump `RULES_VERSION` in `dtr_rules.py` and update `row_is_current()`, which decides per employment type whether a saved row is stale. Printouts warn when a snapshot is stale.
- To correct a processed period, a user unlocks its DTRs, reopens the batch, and processes it again.

### Access control

There is no shared permission decorator. Each admin view combines `@login_required` with an inline `is_staff or is_superuser` check, and must scope its data through helpers such as `_scoped_branch_queryset_for_admin` and `_scoped_profiles_for_admin`: superusers see all branches, staff admins only their own. New admin views need both the check and the scoping. Non-staff users get the employee portal.

### Biometric attendance

Hikvision devices sit on branch networks that the deployed app cannot reach, so connector scripts run on a branch PC and talk to the server over HTTP:

- `hikvision_connector.py` polls `/api/biometrics/connector/next/` for sync requests and posts results back. It runs `django.setup()` and imports `core.hikvision_sync`, so it needs the full project. `run_hikvision_connector.ps1` launches it.
- `hikvision_auto_connector.py` is standalone (`requests` is its only third-party dependency) and pushes events to `/api/biometrics/live/` (`core/biometric_live_views.py`). It keeps its state under `%LOCALAPPDATA%\IntelliHRTrack\automatic-sync`.

Both read `INTELLIHRTRACK_BASE_URL` and `INTELLIHRTRACK_CONNECTOR_TOKEN`. They authenticate with a bearer token checked against `BranchConnector`, and the branch always comes from the token. `core/hikvision_sync.py` pairs separate face and fingerprint events into one punch when they fall within a 2-minute window, using the later timestamp.

### Code layout

- `core/views.py` is over 10,000 lines and holds most views and helpers. Search it rather than reading it whole. Newer features are in separate modules (`overtime_views.py`, `payroll_period_views.py`, `dtr_print_views.py`), all routed through `core/urls.py`.
- Custom UI lives under `/admin-ui/`. `/admin/` is the stock Django admin.
- Live templates are in `templates/admin/`, `templates/employee/`, and `templates/auth/`. `templates/oldadmin/` and `templates/adminv1/` are old copies that no Python code references, so do not edit them by mistake.
- Admin and employee pages load `output.css` (Tailwind), then the hand-written `static/css/ihrt.css` and `admin.css`. `output.css` is committed, so rebuild it when templates need new Tailwind classes.
- `core/management/commands/` contains one-off scripts for client data imports, demo data, and accuracy checks. It is not part of normal app operation.
- `INSTALL.txt` and `views.py.changes.diff` are leftovers from a past patch handoff, not current instructions.

## Database and deployment

- **Database:** MySQL, through PyMySQL (`pymysql.install_as_MySQLdb()` in `config/__init__.py`). Connection settings come from the `MYSQL*` environment variables and default to local `root` on `intellihrtrack_payroll`. The `db.sqlite3` in the repo root is not used by the app.
- **Deployment:** Railway, on push. `railway.json` runs `collectstatic`, then `migrate`, then gunicorn. Migrations therefore run automatically against production.
- **Static and media:** `config/urls.py` has an explicit `/static/` route ahead of `core.urls`, serving from `static/`. Media is only routed when `DEBUG` is on.
- **Email:** sent by `core/email_backend.py` through the Brevo HTTPS API (`BREVO_API_KEY`), not SMTP.
- **Time zone:** `Asia/Manila`, with `USE_TZ` on. DTR code reads punch times from `raw_row['time']` to avoid time zone mismatches, so keep that in mind when changing attendance grouping.
