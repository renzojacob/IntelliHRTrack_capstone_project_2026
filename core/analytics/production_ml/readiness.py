"""Strict scientific-readiness gate for production temporal evaluation."""

from collections import Counter, defaultdict
from datetime import date, datetime

from core.analytics.features import FEATURE_NAMES
from core.analytics.quality import summarize_ml_dataset


NO_ELIGIBLE_OBSERVATIONS = "NO_ELIGIBLE_OBSERVATIONS"
SINGLE_TARGET_CLASS = "SINGLE_TARGET_CLASS"
INSUFFICIENT_TARGET_PERIODS = "INSUFFICIENT_TARGET_PERIODS"
INSUFFICIENT_EMPLOYEE_GROUPS = "INSUFFICIENT_EMPLOYEE_GROUPS"
INSUFFICIENT_TEMPORAL_VALIDATION_DATA = "INSUFFICIENT_TEMPORAL_VALIDATION_DATA"
INSUFFICIENT_ATTENDANCE_COVERAGE = "INSUFFICIENT_ATTENDANCE_COVERAGE"
INVALID_PERIOD_CHRONOLOGY = "INVALID_PERIOD_CHRONOLOGY"
EMPLOYEE_PERIOD_LEAKAGE = "EMPLOYEE_PERIOD_LEAKAGE"
TARGET_LEAKAGE = "TARGET_LEAKAGE"

REASON_MESSAGES = {
    NO_ELIGIBLE_OBSERVATIONS: "No employee-period observations pass the source-quality gates.",
    SINGLE_TARGET_CLASS: "Both positive and negative next-period outcomes are required.",
    INSUFFICIENT_TARGET_PERIODS: "At least three completed target periods are required for temporal validation and final evaluation.",
    INSUFFICIENT_EMPLOYEE_GROUPS: "More than one employee group is required in training and evaluation data.",
    INSUFFICIENT_TEMPORAL_VALIDATION_DATA: "Earlier training/validation and later evaluation windows must each contain both target classes.",
    INSUFFICIENT_ATTENDANCE_COVERAGE: "Eligible observations do not provide sufficient attendance-source coverage.",
    INVALID_PERIOD_CHRONOLOGY: "Every target period must start strictly after its paired feature period ends.",
    EMPLOYEE_PERIOD_LEAKAGE: "Duplicate employee-period observations would create leakage.",
    TARGET_LEAKAGE: "The predictor schema contains an identifier, target, or non-allowlisted field.",
}


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def temporal_split_plan(eligible_observations):
    """Return expanding temporal validation folds plus the final holdout."""

    observations = list(eligible_observations or [])
    period_dates = {}
    by_period = defaultdict(list)
    for index, row in enumerate(observations):
        period_id = row.get("target_period_id")
        period_dates[period_id] = _as_date(row.get("target_period_start"))
        by_period[period_id].append(index)
    ordered = sorted(by_period, key=lambda key: (period_dates.get(key) or date.min, str(key)))
    if len(ordered) < 2:
        return {"ordered_target_periods": ordered, "validation_folds": [], "final_holdout": None}

    final_period = ordered[-1]
    training_periods = ordered[:-1]
    folds = []
    for position in range(1, len(training_periods)):
        prior = set(training_periods[:position])
        validation_period = training_periods[position]
        train_indices = [i for period_id in prior for i in by_period[period_id]]
        validation_indices = list(by_period[validation_period])
        train_classes = {int(observations[i]["target"]) for i in train_indices}
        validation_classes = {int(observations[i]["target"]) for i in validation_indices}
        if train_classes == {0, 1} and validation_classes == {0, 1}:
            folds.append(
                {
                    "train_indices": train_indices,
                    "validation_indices": validation_indices,
                    "training_periods": list(training_periods[:position]),
                    "validation_period": validation_period,
                }
            )
    final_train_indices = [i for period_id in training_periods for i in by_period[period_id]]
    final_test_indices = list(by_period[final_period])
    return {
        "ordered_target_periods": ordered,
        "validation_folds": folds,
        "final_holdout": {
            "train_indices": final_train_indices,
            "test_indices": final_test_indices,
            "training_periods": training_periods,
            "evaluation_period": final_period,
        },
    }


def evaluate_production_readiness(assessed_observations):
    assessed = list(assessed_observations or [])
    summary = summarize_ml_dataset(assessed)
    eligible = [row["observation"] for row in assessed if row["ml_eligibility"]["eligible"]]
    reasons = []

    if not eligible:
        reasons.append(NO_ELIGIBLE_OBSERVATIONS)
    if assessed and not eligible:
        reasons.append(INSUFFICIENT_ATTENDANCE_COVERAGE)

    target_counts = Counter(int(row.get("target", -1)) for row in eligible)
    if not target_counts[0] or not target_counts[1]:
        reasons.append(SINGLE_TARGET_CLASS)

    employees = {row.get("employee_group_id") for row in eligible if row.get("employee_group_id") is not None}
    if len(employees) < 2:
        reasons.append(INSUFFICIENT_EMPLOYEE_GROUPS)

    target_periods = {row.get("target_period_id") for row in eligible}
    if len(target_periods) < 3:
        reasons.append(INSUFFICIENT_TARGET_PERIODS)

    chronology_valid = all(
        _as_date(row.get("feature_period_end"))
        and _as_date(row.get("target_period_start"))
        and _as_date(row.get("feature_period_end")) < _as_date(row.get("target_period_start"))
        for row in eligible
    )
    if eligible and not chronology_valid:
        reasons.append(INVALID_PERIOD_CHRONOLOGY)

    observation_keys = [
        (row.get("employee_group_id"), row.get("feature_period_id"), row.get("target_period_id"))
        for row in eligible
    ]
    if len(observation_keys) != len(set(observation_keys)):
        reasons.append(EMPLOYEE_PERIOD_LEAKAGE)

    if any(set(row.get("features", {})) != set(FEATURE_NAMES) for row in eligible):
        reasons.append(TARGET_LEAKAGE)

    plan = temporal_split_plan(eligible)
    holdout = plan["final_holdout"]
    holdout_valid = False
    group_valid = False
    if holdout:
        train_rows = [eligible[i] for i in holdout["train_indices"]]
        test_rows = [eligible[i] for i in holdout["test_indices"]]
        holdout_valid = (
            {int(row["target"]) for row in train_rows} == {0, 1}
            and {int(row["target"]) for row in test_rows} == {0, 1}
        )
        group_valid = (
            len({row["employee_group_id"] for row in train_rows}) > 1
            and len({row["employee_group_id"] for row in test_rows}) > 1
        )
    if not holdout_valid or not plan["validation_folds"]:
        reasons.append(INSUFFICIENT_TEMPORAL_VALIDATION_DATA)
    if holdout and not group_valid:
        reasons.append(INSUFFICIENT_EMPLOYEE_GROUPS)

    reasons = list(dict.fromkeys(reasons))
    return {
        "ready": not reasons,
        "status": "READY" if not reasons else "NOT_READY",
        "reason_codes": reasons,
        "reason_messages": [REASON_MESSAGES[code] for code in reasons],
        "summary": summary,
        "represented_target_periods": len(target_periods),
        "represented_employees": len(employees),
        "temporal_validation_folds": len(plan["validation_folds"]),
        "final_holdout_available": bool(holdout_valid and group_valid),
        "split_plan": plan,
    }
