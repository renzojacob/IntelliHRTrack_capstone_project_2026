"""Read-only audit of historical attendance files before any import.

This module deliberately has no create/update/delete code.  It reads the same
flat CSV/Excel shape as the existing biometrics upload and compares normalized
rows with existing configuration and attendance records.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
import json
from pathlib import Path
from typing import Any, Iterable

from django.conf import settings
from django.utils import timezone

from core.models import AttendanceRecord, Branch, PayrollPeriod, UserProfile

from .features import pair_chronological_periods


ID_COLUMNS = ("person id", "personid", "employee id", "employeeid", "id")
TIMESTAMP_COLUMNS = ("time", "date time", "timestamp")
STATUS_COLUMNS = ("attendance status", "status", "event type")
BRANCH_COLUMNS = ("branch", "branch name")


def _key(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_row(row: dict[str, Any]) -> dict[str, Any]:
    return {_key(key): value for key, value in (row or {}).items()}


def _first(row: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return ""


def normalize_employee_id(value: Any) -> str:
    """Use the project's stable biometric ID without guessing identity."""
    text = _text(value).lstrip("'").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def normalize_status(value: Any) -> str:
    normalized = _key(value).replace("-", "")
    if normalized in {"in", "checkin", "check in", "time in", "clock in", "entry"}:
        return AttendanceRecord.STATUS_CHECKIN
    if normalized in {"out", "checkout", "check out", "time out", "clock out", "exit"}:
        return AttendanceRecord.STATUS_CHECKOUT
    return AttendanceRecord.STATUS_UNKNOWN


@dataclass(frozen=True)
class TimestampResult:
    value: datetime | None
    error: str | None = None


def parse_audit_timestamp(value: Any) -> TimestampResult:
    """Parse only timestamps whose calendar interpretation is unambiguous.

    Naive values are interpreted in Django's configured current timezone.  An
    explicit offset is preserved.  Numeric slash dates are accepted only when
    exactly one of month/day and day/month is possible (year-first is always
    unambiguous).
    """
    if value in (None, ""):
        return TimestampResult(None, "missing")

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        source = _text(value)
        if not source:
            return TimestampResult(None, "missing")

        iso_source = source[:-1] + "+00:00" if source.endswith(("Z", "z")) else source
        parsed = None
        try:
            # ISO year-first dates are never confused with day/month dates.
            if len(source) >= 10 and source[4:5] in {"-", "/"}:
                parsed = datetime.fromisoformat(iso_source.replace("/", "-"))
        except ValueError:
            parsed = None

        if parsed is None:
            candidates = []
            for fmt in (
                "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %H:%M",
                "%d/%m/%Y %H:%M:%S",
                "%d/%m/%Y %H:%M",
            ):
                try:
                    candidates.append(datetime.strptime(source, fmt))
                except ValueError:
                    continue
            distinct = {candidate for candidate in candidates}
            if len(distinct) > 1:
                return TimestampResult(None, "ambiguous")
            if len(distinct) == 1:
                parsed = distinct.pop()

        if parsed is None:
            return TimestampResult(None, "malformed")

    if settings.USE_TZ and timezone.is_naive(parsed):
        try:
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        except (ValueError, TypeError):
            return TimestampResult(None, "malformed")
    return TimestampResult(parsed)


def _source_fingerprint(row: dict[str, Any]) -> str:
    cleaned = {
        _key(key): _text(value)
        for key, value in (row or {}).items()
    }
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), default=str)


def read_historical_file(file_obj, filename: str) -> list[dict[str, Any]]:
    """Reuse the established web import readers without changing import code."""
    suffix = Path(filename or "").suffix.lower()
    # Local import avoids making the analytics package depend on views at app
    # startup.  These are the readers already used by the biometrics UI.
    from core.views import _read_csv, _read_excel

    if suffix == ".csv":
        return _read_csv(file_obj)
    if suffix in {".xls", ".xlsx"}:
        return _read_excel(file_obj, filename)
    raise ValueError("Unsupported file type. Use .csv, .xls, or .xlsx.")


def _local_date(value: datetime) -> date:
    if timezone.is_aware(value):
        return timezone.localtime(value).date()
    return value.date()


def _iso_local(value: datetime | None) -> str | None:
    if value is None:
        return None
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.isoformat()


def audit_historical_rows(
    rows: Iterable[dict[str, Any]],
    *,
    branch: Branch,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """Return an aggregate-only dry-run report and perform no database writes."""
    source_rows = list(rows)
    as_of = as_of_date or timezone.localdate()
    known_branches = {
        name.casefold(): branch_id
        for branch_id, name in Branch.objects.values_list("id", "name")
    }

    profile_rows = list(
        UserProfile.objects.filter(biometric_employee_id__gt="")
        .values(
            "id",
            "branch_id",
            "biometric_employee_id",
            "employment_start_date",
            "employment_end_date",
        )
    )
    profiles_by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for profile in profile_rows:
        profiles_by_id[normalize_employee_id(profile["biometric_employee_id"])].append(profile)

    fingerprints_seen: Counter[str] = Counter()
    parsed_rows: list[dict[str, Any]] = []
    all_ids: set[str] = set()
    matched_ids: set[str] = set()
    unmatched_ids: set[str] = set()
    ambiguous_ids: set[str] = set()
    counters = Counter()

    for source_row_number, raw in enumerate(source_rows, start=2):
        row = _normalized_row(raw)
        employee_id = normalize_employee_id(_first(row, ID_COLUMNS))
        timestamp_result = parse_audit_timestamp(_first(row, TIMESTAMP_COLUMNS))
        status = normalize_status(_first(row, STATUS_COLUMNS))
        source_branch = _text(_first(row, BRANCH_COLUMNS))
        fingerprint = _source_fingerprint(raw)
        fingerprints_seen[fingerprint] += 1
        exact_duplicate = fingerprints_seen[fingerprint] > 1

        reasons: list[str] = []
        if not employee_id:
            counters["missing_employee_identifiers"] += 1
            reasons.append("missing_employee_identifier")
            match = "unmatched"
            candidates = []
        else:
            all_ids.add(employee_id)
            candidates = [
                profile
                for profile in profiles_by_id.get(employee_id, [])
                if profile["branch_id"] == branch.id
            ]
            if len(candidates) == 1:
                match = "matched"
                matched_ids.add(employee_id)
            elif len(candidates) > 1:
                match = "ambiguous"
                ambiguous_ids.add(employee_id)
                reasons.append("ambiguous_employee")
                counters["ambiguous_employee_rows"] += 1
            else:
                match = "unmatched"
                unmatched_ids.add(employee_id)
                reasons.append("unmatched_employee")
                counters["unmatched_employee_rows"] += 1

        if timestamp_result.error == "missing":
            counters["missing_timestamps"] += 1
            reasons.append("missing_timestamp")
        elif timestamp_result.error == "ambiguous":
            counters["ambiguous_timestamps"] += 1
            reasons.append("ambiguous_timestamp")
        elif timestamp_result.error:
            counters["malformed_timestamps"] += 1
            reasons.append("malformed_timestamp")

        if exact_duplicate:
            counters["exact_duplicate_source_rows"] += 1
            reasons.append("exact_source_duplicate")

        branch_issue = False
        if source_branch:
            source_branch_id = known_branches.get(source_branch.casefold())
            if source_branch_id is None:
                counters["records_outside_known_branches"] += 1
                reasons.append("unknown_source_branch")
                branch_issue = True
            elif source_branch_id != branch.id:
                counters["records_outside_selected_branch"] += 1
                reasons.append("source_branch_conflict")
                branch_issue = True

        profile = candidates[0] if match == "matched" else None
        outside_employment = False
        if profile and timestamp_result.value:
            attendance_date = _local_date(timestamp_result.value)
            start = profile["employment_start_date"]
            end = profile["employment_end_date"]
            outside_employment = bool((start and attendance_date < start) or (end and attendance_date > end))
            if outside_employment:
                counters["records_outside_employment_periods"] += 1
                reasons.append("outside_employment_period")

        parsed_rows.append(
            {
                "source_row": source_row_number,
                "employee_id": employee_id,
                "timestamp": timestamp_result.value,
                "status": status,
                "match": match,
                "profile_id": profile["id"] if profile else None,
                "reasons": reasons,
                "branch_issue": branch_issue,
                "outside_employment": outside_employment,
            }
        )

    comparable = [row for row in parsed_rows if row["employee_id"] and row["timestamp"]]
    employee_ids = {row["employee_id"] for row in comparable}
    earliest = min((row["timestamp"] for row in comparable), default=None)
    latest = max((row["timestamp"] for row in comparable), default=None)

    existing_exact: set[tuple[Any, ...]] = set()
    existing_identity: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    if employee_ids and earliest is not None and latest is not None:
        existing_rows = AttendanceRecord.objects.filter(
            branch=branch,
            employee_id__in=employee_ids,
            timestamp__gte=earliest,
            timestamp__lte=latest,
        ).values_list("employee_id", "timestamp", "attendance_status")
        for employee_id, stamp, status in existing_rows:
            existing_exact.add((normalize_employee_id(employee_id), stamp, status))
            existing_identity[(normalize_employee_id(employee_id), stamp)].add(status)

    evidence_by_profile: dict[int, set[date]] = defaultdict(set)
    accepted = rejected = already_existing = conflicts = 0
    for row in parsed_rows:
        stamp = row["timestamp"]
        exact_key = (row["employee_id"], stamp, row["status"])
        identity_key = (row["employee_id"], stamp)
        is_existing = stamp is not None and exact_key in existing_exact
        is_conflict = (
            stamp is not None
            and identity_key in existing_identity
            and row["status"] not in existing_identity[identity_key]
        )
        if is_existing:
            already_existing += 1
        if is_conflict:
            conflicts += 1
            row["reasons"].append("database_status_conflict")

        if is_existing:
            outcome = "already_existing"
        elif row["reasons"]:
            outcome = "rejected"
            rejected += 1
        else:
            outcome = "accepted"
            accepted += 1
        row["outcome"] = outcome

        if row["profile_id"] and stamp is not None and not row["branch_issue"] and not row["outside_employment"]:
            evidence_by_profile[row["profile_id"]].add(_local_date(stamp))

    valid_dates = sorted({day for days in evidence_by_profile.values() for day in days})
    periods = list(PayrollPeriod.objects.order_by("start_date", "end_date", "id"))
    completed_periods = [period for period in periods if period.end_date <= as_of]
    fully_covered = [
        period for period in completed_periods
        if valid_dates and valid_dates[0] <= period.start_date and valid_dates[-1] >= period.end_date
    ]
    all_evidence_period_ids: set[int] = set()
    completed_evidence_period_ids: set[int] = set()
    employee_periods: dict[int, set[int]] = defaultdict(set)
    for period in periods:
        for profile_id, days in evidence_by_profile.items():
            if any(period.start_date <= day <= period.end_date for day in days):
                all_evidence_period_ids.add(period.id)
                if period.end_date <= as_of:
                    completed_evidence_period_ids.add(period.id)
                    employee_periods[profile_id].add(period.id)

    chronological_pairs = pair_chronological_periods(completed_periods)
    possible_windows = sum(
        1
        for period_ids in employee_periods.values()
        for feature, target in chronological_pairs
        if feature.id in period_ids and target.id in period_ids
    )

    parsed_timestamps = [row["timestamp"] for row in parsed_rows if row["timestamp"] is not None]
    months = sorted({_local_date(stamp).strftime("%Y-%m") for stamp in parsed_timestamps})

    # This dictionary is safe for normal command/API output: it contains no
    # names, usernames, emails, salaries, or source biometric identifiers.
    return {
        "mode": "DRY_RUN",
        "database_writes": 0,
        "total_rows": len(source_rows),
        "earliest_timestamp": _iso_local(min(parsed_timestamps, default=None)),
        "latest_timestamp": _iso_local(max(parsed_timestamps, default=None)),
        "unique_source_employee_identifiers": len(all_ids),
        "rows_with_missing_employee_identifiers": counters["missing_employee_identifiers"],
        "rows_with_missing_timestamps": counters["missing_timestamps"],
        "malformed_timestamps": counters["malformed_timestamps"],
        "ambiguous_timestamps": counters["ambiguous_timestamps"],
        "exact_duplicate_source_rows": counters["exact_duplicate_source_rows"],
        "matched_employee_identifiers": len(matched_ids),
        "unmatched_employee_identifiers": len(unmatched_ids),
        "ambiguous_employee_identifiers": len(ambiguous_ids),
        "matched_rows": sum(row["match"] == "matched" for row in parsed_rows),
        "unmatched_rows": counters["unmatched_employee_rows"],
        "ambiguous_employee_rows": counters["ambiguous_employee_rows"],
        "records_outside_employment_periods": counters["records_outside_employment_periods"],
        "records_outside_known_branches": counters["records_outside_known_branches"],
        "records_outside_selected_branch": counters["records_outside_selected_branch"],
        "rows_accepted": accepted,
        "rows_rejected": rejected,
        "rows_already_existing": already_existing,
        "potential_conflicting_records": conflicts,
        "historical_months": months,
        "employees_with_usable_attendance_evidence": len(evidence_by_profile),
        "completed_payroll_periods_potentially_covered": len(fully_covered),
        "payroll_periods_with_attendance_evidence": len(all_evidence_period_ids),
        "completed_periods_with_attendance_evidence": len(completed_evidence_period_ids),
        "possible_consecutive_feature_target_windows": possible_windows,
        "overlap_with_existing_attendance": already_existing,
        "eligibility_note": (
            "Preview only; no observation is ML-eligible until a future controlled import "
            "passes the Phase 1.6 eligibility gate."
        ),
    }


def audit_historical_file(file_obj, filename: str, *, branch: Branch, as_of_date: date | None = None):
    rows = read_historical_file(file_obj, filename)
    return audit_historical_rows(rows, branch=branch, as_of_date=as_of_date)


def build_dry_run_plan(rows, *, branch: Branch, as_of_date: date | None = None):
    """Named future-import boundary: today it can only return a zero-write plan."""
    return audit_historical_rows(rows, branch=branch, as_of_date=as_of_date)
