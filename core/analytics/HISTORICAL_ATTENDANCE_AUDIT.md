# Historical attendance audit

## Existing architecture

The admin biometrics page accepts CSV, XLS, XLSX, and Hikvision HTML-as-XLS
files. It maps `Person ID`/`Employee ID`, `Time`, optional status, and optional
display fields, validates the upload, caches mapped rows in the session, and
only writes after a separate Import action. Hikvision connector endpoints also
create the same `AttendanceRecord` rows, scoped to the connector's branch. A
client-specific August workbook command exists and defaults to dry-run.

`AttendanceRecord` (`core_attendancerecord`) is the authoritative raw input to
`_build_dtr_and_summary()`. Its relevant fields are `employee_id`, `branch`,
`timestamp`, `attendance_status`, `raw_row`, and `created_at`. The employee link
is intentionally not a foreign key: DTR generation exactly matches
`AttendanceRecord.employee_id` to `UserProfile.biometric_employee_id`, then
requires the same branch. The database uniqueness rule is employee ID +
timestamp + status + branch. With `USE_TZ=True`, naive import timestamps use
the configured `Asia/Manila` timezone; offset-aware values retain their instant.
Hikvision DTR grouping prefers an offset-bearing `raw_row.time` when available.

## Audit format and safety

The audit accepts the same generic CSV/XLS/XLSX reader and column aliases as the
web importer:

- required employee identifier: `Person ID`, `Employee ID`, or `ID`
- required timestamp: `Time`, `Date Time`, or `Timestamp`
- optional: `Attendance Status`/`Status`/`Event Type`, and `Branch`

ISO timestamps such as `2026-08-01 08:00:00` or explicit-offset ISO timestamps
are preferred. A slash date is accepted only if month/day versus day/month has
one possible interpretation. Naive timestamps use the project timezone.

Matching is exact after trimming whitespace and Excel's integer `.0` suffix.
Names are never used. Zero target-branch profiles is unmatched; multiple exact
profiles in the target branch is ambiguous. Neither classification is accepted.

Exact repeated source rows are detected using all normalized source columns.
An existing database duplicate uses the model's unique tuple. The same employee,
timestamp, and branch with a different status is a potential conflict.

Run an aggregate-only audit with:

```text
python manage.py audit_historical_attendance --file local_data/history.csv --branch 1
```

The command has no commit option. `build_dry_run_plan()` is the explicit future
import boundary and currently delegates only to the read-only audit. Any later
write path must be a separate explicit operation and must reject every row not
reported as accepted by a fresh dry run.
