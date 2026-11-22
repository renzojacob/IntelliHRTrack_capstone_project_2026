"""Employee-period feature and supervised-dataset construction.

Observation definition
----------------------
For one employee and historical payroll period t, X(t) contains only values
derived from t.  The target y(t) is computed from the deterministic DTR rows
of the next non-overlapping chronological period t+1 in the same cadence:

* MONTHLY periods pair only with MONTHLY periods.
* FIRST_HALF and SECOND_HALF periods form one SEMI_MONTHLY cadence.
* Adjacent entries that overlap are not paired.

Employee identifiers are retained only as grouping/audit metadata and are
never inserted into the predictor feature dictionary.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from core.models import PayrollPeriod, PayrollRule, UserProfile

from .labels import build_unprotected_absence_target
from .rules import assess_data_quality, calculate_attendance_risk


NON_WORKING_STATUSES = frozenset(
    {"Weekend", "Holiday/Suspension", "Outside Employment Period"}
)
PRESENT_EQUIVALENT_STATUSES = frozenset(
    {"Present", "Half Day", "Official Business", "Official Travel"}
)
ACTUAL_WORK_STATUSES = frozenset({"Present", "Half Day", "Absent", "Incomplete"})

FEATURE_NAMES = (
    "previous_attendance_rate",
    "previous_absences",
    "previous_late_days",
    "previous_late_minutes",
    "previous_undertime_days",
    "previous_undertime_minutes",
    "previous_incomplete_dtr_records",
    "previous_consecutive_absence_count",
    "previous_monday_friday_absence_count",
    "previous_average_worked_hours",
    "previous_approved_leave_days",
    "previous_travel_days",
    "previous_holiday_suspension_days",
    "previous_rule_based_risk_score",
    "employment_type",
    "department",
)


class AnalyticsDataUnavailable(ValueError):
    """Raised when deterministic DTR features cannot be produced safely."""


def _as_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _as_decimal(value):
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _row_date(row):
    value = row.get("date") if isinstance(row, dict) else None
    if hasattr(value, "year"):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _period_cadence(period):
    mode = str(getattr(period, "pay_mode", "") or "").upper()
    if mode == PayrollPeriod.PAY_MONTHLY:
        return "MONTHLY"
    if mode in {PayrollPeriod.PAY_FIRST_HALF, PayrollPeriod.PAY_SECOND_HALF}:
        return "SEMI_MONTHLY"
    return None


def pair_chronological_periods(periods):
    """Pair adjacent, strictly non-overlapping periods in the same cadence."""

    grouped = defaultdict(list)
    for period in periods or []:
        cadence = _period_cadence(period)
        if cadence and getattr(period, "start_date", None) and getattr(period, "end_date", None):
            grouped[cadence].append(period)

    pairs = []
    for cadence_periods in grouped.values():
        ordered = sorted(
            cadence_periods,
            key=lambda p: (
                p.start_date,
                p.end_date,
                getattr(p, "pk", None) or getattr(p, "id", None) or 0,
            ),
        )
        for feature_period, target_period in zip(ordered, ordered[1:]):
            if target_period.start_date > feature_period.end_date:
                pairs.append((feature_period, target_period))

    return sorted(
        pairs,
        key=lambda pair: (
            pair[0].start_date,
            pair[0].end_date,
            getattr(pair[0], "pk", None) or getattr(pair[0], "id", None) or 0,
        ),
    )


def _maximum_absence_streak(rows):
    """Maximum Absent streak across qualifying working days.

    Weekends/holidays/outside-employment dates are skipped.  Any protected or
    non-absent working-day status (including leave, travel, and official
    business) breaks the streak and can never extend it.
    """

    maximum = 0
    current = 0
    for row in sorted(rows, key=lambda item: _row_date(item) or datetime.min.date()):
        status = str(row.get("status") or "").strip()
        if status in NON_WORKING_STATUSES:
            continue
        if status == "Absent":
            current += 1
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def _verified_holiday_adjacent_absences(rows):
    holiday_dates = {
        day
        for row in rows
        if str(row.get("status") or "").strip() == "Holiday/Suspension"
        for day in [_row_date(row)]
        if day is not None
    }
    count = 0
    for row in rows:
        if str(row.get("status") or "").strip() != "Absent":
            continue
        day = _row_date(row)
        if day and (
            day - timedelta(days=1) in holiday_dates
            or day + timedelta(days=1) in holiday_dates
        ):
            count += 1
    return count


def aggregate_dtr_features(profile, dtr_output):
    """Aggregate one already-classified deterministic DTR into predictors."""

    dtr_output = dtr_output if isinstance(dtr_output, dict) else {}
    rows = [row for row in dtr_output.get("rows", []) if isinstance(row, dict)]

    applicable_rows = [
        row
        for row in rows
        if str(row.get("status") or "").strip() not in NON_WORKING_STATUSES
        and str(row.get("status") or "").strip() != "Approved Leave"
    ]
    present_rows = [
        row
        for row in applicable_rows
        if str(row.get("status") or "").strip() in PRESENT_EQUIVALENT_STATUSES
    ]
    absent_rows = [row for row in rows if str(row.get("status") or "").strip() == "Absent"]
    incomplete_rows = [
        row for row in rows if str(row.get("status") or "").strip() == "Incomplete"
    ]

    late_days = sum(1 for row in rows if _as_int(row.get("late")) > 0)
    late_minutes = sum(_as_int(row.get("late")) for row in rows)
    undertime_days = sum(1 for row in rows if _as_int(row.get("undertime")) > 0)
    undertime_minutes = sum(_as_int(row.get("undertime")) for row in rows)

    monday_friday_absences = sum(
        1
        for row in absent_rows
        for day in [_row_date(row)]
        if day is not None and day.weekday() in {0, 4}
    )

    worked_hour_rows = [
        row
        for row in rows
        if str(row.get("status") or "").strip() in ACTUAL_WORK_STATUSES
    ]
    total_worked_hours = sum(
        (_as_decimal(row.get("total_minutes")) / Decimal("60"))
        if row.get("total_minutes") not in (None, "")
        else _as_decimal(row.get("total_hours"))
        for row in worked_hour_rows
    )
    average_worked_hours = (
        total_worked_hours / Decimal(len(worked_hour_rows))
        if worked_hour_rows
        else Decimal("0")
    )

    attendance_rate = (
        (Decimal(len(present_rows)) / Decimal(len(applicable_rows))) * Decimal("100")
        if applicable_rows
        else Decimal("0")
    )

    holiday_adjacent = _verified_holiday_adjacent_absences(rows)
    risk = calculate_attendance_risk(
        absences=len(absent_rows),
        late_days=late_days,
        incomplete_dtr_records=len(incomplete_rows),
        monday_friday_absences=monday_friday_absences,
        holiday_adjacent_absences=holiday_adjacent,
    )

    features = {
        "previous_attendance_rate": round(float(attendance_rate), 2),
        "previous_absences": len(absent_rows),
        "previous_late_days": late_days,
        "previous_late_minutes": late_minutes,
        "previous_undertime_days": undertime_days,
        "previous_undertime_minutes": undertime_minutes,
        "previous_incomplete_dtr_records": len(incomplete_rows),
        "previous_consecutive_absence_count": _maximum_absence_streak(rows),
        "previous_monday_friday_absence_count": monday_friday_absences,
        "previous_average_worked_hours": round(float(average_worked_hours), 2),
        "previous_approved_leave_days": float(dtr_output.get("leave_days", 0) or 0),
        "previous_travel_days": _as_int(dtr_output.get("travel_days")),
        "previous_holiday_suspension_days": _as_int(dtr_output.get("holiday_days")),
        "previous_rule_based_risk_score": risk["score"],
        "employment_type": str(getattr(profile, "employment_type", "") or ""),
        "department": str(getattr(profile, "department", "") or "Unassigned"),
    }

    quality = assess_data_quality(
        total_applicable_days=len(applicable_rows),
        incomplete_dtr_records=len(incomplete_rows),
        usable_attendance_records=_as_int(dtr_output.get("records_used")),
        unavailable_features=[],
    )

    return {
        "features": features,
        "risk": risk,
        "data_quality": quality,
        "unavailable_features": [],
    }


def _resolve_rules(profile, rules=None, rules_resolver=None):
    if rules is not None:
        return rules
    if rules_resolver is not None:
        return rules_resolver(profile)
    branch = getattr(profile, "branch", None)
    if branch is None:
        return None
    return PayrollRule.objects.filter(branch=branch).first()


def _default_dtr_builder(profile, branch, period, rules):
    # Lazy import avoids making core.views import this package during startup.
    from core.views import _build_dtr_and_summary

    return _build_dtr_and_summary(profile, branch, period, rules)


def generate_employee_period_features(
    profile,
    period,
    *,
    rules=None,
    rules_resolver=None,
    dtr_builder=None,
):
    """Build X(t) from period t only."""

    branch = getattr(profile, "branch", None)
    if branch is None:
        raise AnalyticsDataUnavailable("Employee has no assigned branch.")
    resolved_rules = _resolve_rules(profile, rules=rules, rules_resolver=rules_resolver)
    if resolved_rules is None:
        raise AnalyticsDataUnavailable("No payroll rules exist for the employee branch.")
    builder = dtr_builder or _default_dtr_builder
    dtr_output = builder(profile, branch, period, resolved_rules)
    return aggregate_dtr_features(profile, dtr_output)


def build_employee_period_dataset(
    *,
    profiles=None,
    periods=None,
    rules_resolver=None,
    dtr_builder=None,
):
    """Construct chronological employee t -> t+1 supervised observations."""

    if profiles is None:
        profiles = UserProfile.objects.select_related("user", "branch").filter(
            is_approved=True,
            user__is_staff=False,
            user__is_superuser=False,
        )
    if periods is None:
        periods = PayrollPeriod.objects.all()

    profiles = list(profiles)
    period_pairs = pair_chronological_periods(list(periods))
    if not profiles or not period_pairs:
        return []

    builder = dtr_builder or _default_dtr_builder
    observations = []

    for profile in profiles:
        branch = getattr(profile, "branch", None)
        rules = _resolve_rules(profile, rules_resolver=rules_resolver)
        if branch is None or rules is None:
            continue

        for feature_period, target_period in period_pairs:
            if target_period.start_date <= feature_period.end_date:
                raise ValueError("Target period must begin after the feature period ends.")

            try:
                feature_result = generate_employee_period_features(
                    profile,
                    feature_period,
                    rules=rules,
                    dtr_builder=builder,
                )
                target_dtr = builder(profile, branch, target_period, rules)
            except AnalyticsDataUnavailable:
                continue

            features = dict(feature_result["features"])
            forbidden_identifiers = {
                "employee_group_id",
                "employee_id",
                "profile_id",
                "user_id",
                "username",
                "name",
                "email",
            }
            if forbidden_identifiers.intersection(features):
                raise ValueError("Employee identifiers must not appear in predictor features.")

            observations.append(
                {
                    "employee_group_id": getattr(profile, "pk", None)
                    or getattr(profile, "id", None),
                    "feature_period_id": getattr(feature_period, "pk", None)
                    or getattr(feature_period, "id", None),
                    "feature_period_start": feature_period.start_date,
                    "feature_period_end": feature_period.end_date,
                    "target_period_id": getattr(target_period, "pk", None)
                    or getattr(target_period, "id", None),
                    "target_period_start": target_period.start_date,
                    "target_period_end": target_period.end_date,
                    "features": features,
                    "target": build_unprotected_absence_target(target_dtr),
                    "rule_based_risk": feature_result["risk"],
                    "data_quality": feature_result["data_quality"],
                }
            )

    return observations
