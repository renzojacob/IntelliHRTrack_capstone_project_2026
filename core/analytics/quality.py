"""ML-only source-quality gates for employee-period observations.

Operational DTR output remains the deterministic HR/payroll source of truth.
This module only decides whether an already-constructed supervised observation
has enough historical source evidence to be used in a scientific ML dataset.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from .labels import build_unprotected_absence_target


NO_FEATURE_ATTENDANCE_COVERAGE = "NO_FEATURE_ATTENDANCE_COVERAGE"
NO_TARGET_ATTENDANCE_COVERAGE = "NO_TARGET_ATTENDANCE_COVERAGE"
INCOMPLETE_TARGET_PERIOD = "INCOMPLETE_TARGET_PERIOD"
UNSUPPORTED_CADENCE = "UNSUPPORTED_CADENCE"
NON_CHRONOLOGICAL_PAIR = "NON_CHRONOLOGICAL_PAIR"
INSUFFICIENT_FEATURE_COVERAGE = "INSUFFICIENT_FEATURE_COVERAGE"
INSUFFICIENT_TARGET_COVERAGE = "INSUFFICIENT_TARGET_COVERAGE"
INVALID_TARGET_LABEL = "INVALID_TARGET_LABEL"
IDENTIFIER_IN_PREDICTORS = "IDENTIFIER_IN_PREDICTORS"

CURRENT_PROFILE_CATEGORICAL_VALUES_NOT_HISTORICAL = (
    "CURRENT_PROFILE_CATEGORICAL_VALUES_NOT_HISTORICAL"
)

SUPPORTED_MONTHLY_MODES = frozenset({"MONTHLY"})
SUPPORTED_SEMI_MONTHLY_MODES = frozenset({"FIRST_HALF", "SECOND_HALF"})
NON_APPLICABLE_STATUSES = frozenset(
    {"Weekend", "Holiday/Suspension", "Outside Employment Period"}
)
FORBIDDEN_PREDICTOR_IDENTIFIERS = frozenset(
    {
        "employee_group_id",
        "employee_id",
        "profile_id",
        "user_id",
        "username",
        "name",
        "email",
    }
)


@dataclass(frozen=True)
class MLCoveragePolicy:
    """Explicit ML coverage policy.

    One usable attendance day is only an existence gate: it separates a wholly
    absent source history from a period containing some source evidence.  No
    minimum ratio is assumed because the project has not established a
    defensible policy threshold.  A future approved threshold can be supplied
    without changing DTR or payroll behavior.
    """

    minimum_attendance_days: int = 1
    minimum_coverage_ratio: float | None = None

    def __post_init__(self):
        if self.minimum_attendance_days < 1:
            raise ValueError("minimum_attendance_days must be at least 1.")
        if self.minimum_coverage_ratio is not None and not (
            0 <= self.minimum_coverage_ratio <= 1
        ):
            raise ValueError("minimum_coverage_ratio must be between 0 and 1.")


def _as_nonnegative_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _period_cadence(period):
    mode = str(getattr(period, "pay_mode", "") or "").upper()
    if mode in SUPPORTED_MONTHLY_MODES:
        return "MONTHLY"
    if mode in SUPPORTED_SEMI_MONTHLY_MODES:
        return "SEMI_MONTHLY"
    return None


def assess_period_coverage(dtr_output):
    """Return aggregate, PII-free attendance evidence for one DTR period."""

    dtr_output = dtr_output if isinstance(dtr_output, dict) else {}
    rows = [row for row in dtr_output.get("rows", []) if isinstance(row, dict)]
    applicable_rows = [
        row
        for row in rows
        if str(row.get("status") or "").strip() not in NON_APPLICABLE_STATUSES
    ]
    attendance_days = sum(
        1 for row in applicable_rows if _as_nonnegative_int(row.get("raw_log_count")) > 0
    )
    records_used = _as_nonnegative_int(dtr_output.get("records_used"))
    evidence_basis = "daily_raw_log_count"

    # Older/test DTR payloads may expose only a period-level record count.
    # Treat it as evidence of at least one covered day without fabricating a
    # daily distribution or coverage percentage.
    if attendance_days == 0 and records_used > 0:
        attendance_days = 1
        evidence_basis = "period_records_used"

    applicable_days = len(applicable_rows)
    coverage_ratio = (
        attendance_days / applicable_days if applicable_days else None
    )
    return {
        "applicable_working_days": applicable_days,
        "days_with_usable_attendance_evidence": attendance_days,
        "days_without_usable_attendance_evidence": max(
            0, applicable_days - attendance_days
        ),
        "coverage_ratio": coverage_ratio,
        "records_used": records_used,
        "evidence_basis": evidence_basis,
    }


def assess_observation_eligibility(
    observation,
    *,
    feature_period,
    target_period,
    feature_dtr,
    target_dtr,
    as_of_date,
    policy=None,
):
    """Assess one raw supervised observation without changing or deleting it."""

    policy = policy or MLCoveragePolicy()
    feature_coverage = assess_period_coverage(feature_dtr)
    target_coverage = assess_period_coverage(target_dtr)
    reasons = []

    feature_cadence = _period_cadence(feature_period)
    target_cadence = _period_cadence(target_period)
    if not feature_cadence or feature_cadence != target_cadence:
        reasons.append(UNSUPPORTED_CADENCE)

    feature_end = _as_date(getattr(feature_period, "end_date", None))
    target_start = _as_date(getattr(target_period, "start_date", None))
    target_end = _as_date(getattr(target_period, "end_date", None))
    if not feature_end or not target_start or target_start <= feature_end:
        reasons.append(NON_CHRONOLOGICAL_PAIR)

    as_of = _as_date(as_of_date)
    period_complete = bool(as_of and target_end and target_end <= as_of)
    if not period_complete:
        reasons.append(INCOMPLETE_TARGET_PERIOD)

    feature_days = feature_coverage["days_with_usable_attendance_evidence"]
    target_days = target_coverage["days_with_usable_attendance_evidence"]
    if feature_days == 0:
        reasons.append(NO_FEATURE_ATTENDANCE_COVERAGE)
    elif feature_days < policy.minimum_attendance_days:
        reasons.append(INSUFFICIENT_FEATURE_COVERAGE)
    if target_days == 0:
        reasons.append(NO_TARGET_ATTENDANCE_COVERAGE)
    elif target_days < policy.minimum_attendance_days:
        reasons.append(INSUFFICIENT_TARGET_COVERAGE)

    threshold = policy.minimum_coverage_ratio
    if threshold is not None:
        feature_ratio = feature_coverage["coverage_ratio"]
        target_ratio = target_coverage["coverage_ratio"]
        if feature_days and feature_ratio is not None and feature_ratio < threshold:
            reasons.append(INSUFFICIENT_FEATURE_COVERAGE)
        if target_days and target_ratio is not None and target_ratio < threshold:
            reasons.append(INSUFFICIENT_TARGET_COVERAGE)

    expected_target = build_unprotected_absence_target(target_dtr)
    try:
        actual_target = int(observation.get("target"))
    except (TypeError, ValueError):
        actual_target = None
    if actual_target not in {0, 1} or actual_target != expected_target:
        reasons.append(INVALID_TARGET_LABEL)

    features = observation.get("features") or {}
    if FORBIDDEN_PREDICTOR_IDENTIFIERS.intersection(features):
        reasons.append(IDENTIFIER_IN_PREDICTORS)

    # Preserve stable order while allowing multiple simultaneous exclusions.
    reasons = list(dict.fromkeys(reasons))
    return {
        "eligible": not reasons,
        "exclusion_reasons": reasons,
        "feature_coverage": feature_coverage,
        "target_coverage": target_coverage,
        "target_period_complete": period_complete,
        "as_of_date": as_of,
        "known_limitations": [
            CURRENT_PROFILE_CATEGORICAL_VALUES_NOT_HISTORICAL
        ],
    }


def assess_dataset_eligibility(observation_records, *, as_of_date, policy=None):
    """Annotate raw observations supplied with their period/DTR evidence.

    Each input item contains ``observation``, ``feature_period``,
    ``target_period``, ``feature_dtr``, and ``target_dtr``.  The original raw
    observation is retained under ``observation`` for auditability.
    """

    assessed = []
    for record in observation_records or []:
        observation = record["observation"]
        eligibility = assess_observation_eligibility(
            observation,
            feature_period=record["feature_period"],
            target_period=record["target_period"],
            feature_dtr=record["feature_dtr"],
            target_dtr=record["target_dtr"],
            as_of_date=as_of_date,
            policy=policy,
        )
        assessed.append({"observation": observation, "ml_eligibility": eligibility})
    return assessed


def summarize_ml_dataset(assessed_observations):
    """Return aggregate-only raw/eligible dataset counts."""

    assessed = list(assessed_observations or [])
    eligible = [row for row in assessed if row["ml_eligibility"]["eligible"]]
    exclusions = Counter(
        reason
        for row in assessed
        for reason in row["ml_eligibility"]["exclusion_reasons"]
    )
    eligible_observations = [row["observation"] for row in eligible]
    target_counts = Counter(
        int(observation["target"]) for observation in eligible_observations
    )

    return {
        "raw_observations": len(assessed),
        "eligible_observations": len(eligible),
        "excluded_observations": len(assessed) - len(eligible),
        "exclusion_counts": dict(sorted(exclusions.items())),
        "unique_eligible_employees": len(
            {
                observation.get("employee_group_id")
                for observation in eligible_observations
            }
        ),
        "unique_eligible_feature_periods": len(
            {
                observation.get("feature_period_id")
                for observation in eligible_observations
            }
        ),
        "unique_eligible_target_periods": len(
            {
                observation.get("target_period_id")
                for observation in eligible_observations
            }
        ),
        "eligible_target_0": target_counts[0],
        "eligible_target_1": target_counts[1],
        "known_limitations": [
            CURRENT_PROFILE_CATEGORICAL_VALUES_NOT_HISTORICAL
        ],
    }


def assess_ml_readiness(assessed_observations):
    """Conservatively assess whether chronological ML evaluation is feasible."""

    assessed = list(assessed_observations or [])
    summary = summarize_ml_dataset(assessed)
    eligible = [
        row["observation"]
        for row in assessed
        if row["ml_eligibility"]["eligible"]
    ]
    reasons = []

    if not eligible:
        reasons.append("No observations pass the ML source-quality gate.")

    target_counts = Counter(int(row["target"]) for row in eligible)
    if not target_counts[0] or not target_counts[1]:
        reasons.append("Both target classes are not represented in eligible data.")

    targets_by_period = defaultdict(Counter)
    target_dates = {}
    for row in eligible:
        period_id = row.get("target_period_id")
        targets_by_period[period_id][int(row["target"])] += 1
        target_dates[period_id] = _as_date(row.get("target_period_start"))

    ordered_periods = sorted(
        targets_by_period,
        key=lambda period_id: (target_dates.get(period_id) or date.min, str(period_id)),
    )
    chronological_holdout_possible = False
    time_aware_validation_possible = False
    evaluable_splits = 0
    for index in range(1, len(ordered_periods)):
        training = Counter()
        for period_id in ordered_periods[:index]:
            training.update(targets_by_period[period_id])
        validation = targets_by_period[ordered_periods[index]]
        if training[0] and training[1] and validation[0] and validation[1]:
            evaluable_splits += 1

    chronological_holdout_possible = evaluable_splits >= 1
    time_aware_validation_possible = evaluable_splits >= 2
    if not chronological_holdout_possible:
        reasons.append(
            "No chronological split has both classes in prior training data and "
            "the later evaluation period."
        )
    if not time_aware_validation_possible:
        reasons.append("Multiple time-aware validation windows are not available.")

    if not eligible or not target_counts[0] or not target_counts[1]:
        status = "INSUFFICIENT_DATA"
    elif not chronological_holdout_possible:
        status = "INSUFFICIENT_DATA"
    elif not time_aware_validation_possible:
        status = "PARTIALLY_READY"
    else:
        status = "READY"

    return {
        "status": status,
        "reasons": reasons,
        "summary": summary,
        "eligible_chronological_windows": len(
            {
                (row.get("feature_period_id"), row.get("target_period_id"))
                for row in eligible
            }
        ),
        "target_periods_with_both_classes": sum(
            bool(counts[0] and counts[1]) for counts in targets_by_period.values()
        ),
        "chronological_holdout_possible": chronological_holdout_possible,
        "time_aware_validation_possible": time_aware_validation_possible,
        "evaluable_time_splits": evaluable_splits,
    }
