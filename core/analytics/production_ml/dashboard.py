"""Aggregate production-ML state for the operational analytics UI."""

from collections import Counter, defaultdict

from core.models import MLLifecycleEvent

from .artifacts import MLArtifactError, load_registry_artifact
from .prediction import get_active_registry, predict_absenteeism_risk


def _inactive_payload(reason):
    latest = (
        MLLifecycleEvent.objects.filter(event_type__in=["DATASET_BUILT", "READINESS_FAILED"])
        .order_by("-created_at")
        .first()
    )
    metadata = latest.metadata if latest else {}
    readiness = metadata.get("readiness", {}) if isinstance(metadata, dict) else {}
    summary = readiness.get("summary", {}) if isinstance(readiness, dict) else {}
    return {
        "active": False,
        "status": "NOT_READY" if readiness and not readiness.get("ready") else "NO_ACTIVE_MODEL",
        "message": reason or "No active production ML model.",
        "candidate_observations": summary.get("raw_observations", 0),
        "eligible_observations": summary.get("eligible_observations", 0),
        "excluded_observations": summary.get("excluded_observations", 0),
        "eligible_employees": summary.get("unique_eligible_employees", 0),
        "positive_count": summary.get("eligible_target_1", 0),
        "negative_count": summary.get("eligible_target_0", 0),
        "represented_target_periods": readiness.get("represented_target_periods", 0),
        "temporal_validation_folds": readiness.get("temporal_validation_folds", 0),
        "final_holdout_available": readiness.get("final_holdout_available", False),
        "readiness_reason_codes": readiness.get("reason_codes", []),
        "readiness_reasons": readiness.get("reason_messages", []),
        "last_assessed_at": latest.created_at.isoformat() if latest else None,
        "predictions": {},
        "advisory_disclaimer": (
            "ML predictions are advisory only and do not automatically change attendance records, "
            "payroll, leave decisions, disciplinary actions, or employment status."
        ),
    }


def build_workforce_ml_payload(profiles, *, as_of_date):
    registry, reason = get_active_registry()
    if registry is None:
        return _inactive_payload(reason)
    try:
        artifact = load_registry_artifact(registry)
    except MLArtifactError as exc:
        return _inactive_payload(str(exc))

    predictions = {}
    bands = Counter()
    probabilities = []
    departments = defaultdict(list)
    for profile in profiles:
        result = predict_absenteeism_risk(
            profile,
            as_of_date=as_of_date,
            registry=registry,
            artifact=artifact,
        )
        predictions[str(profile.pk)] = result
        if result.get("available"):
            bands[result["risk_level"].lower()] += 1
            probabilities.append(float(result["probability"]))
            departments[str(getattr(profile, "department", "") or "Unassigned")].append(
                float(result["probability"])
            )
    department_aggregates = [
        {
            "department": name,
            "employee_count": len(values),
            "average_probability": sum(values) / len(values),
        }
        for name, values in sorted(departments.items())
        if len(values) >= 3
    ]
    validation = registry.metrics.get("validation", {}) if isinstance(registry.metrics, dict) else {}
    return {
        "active": True,
        "status": "ACTIVE",
        "message": "Advisory production ML model is active.",
        "model_name": registry.model_name,
        "model_version": registry.model_version,
        "trained_at": registry.trained_at.isoformat() if registry.trained_at else None,
        "training_row_count": registry.training_row_count,
        "validation_recall": validation.get("mean_recall"),
        "validation_f1": validation.get("mean_f1"),
        "validation_average_precision": validation.get("mean_average_precision"),
        "risk_buckets": {"low": bands["low"], "medium": bands["medium"], "high": bands["high"]},
        "average_probability": (
            sum(probabilities) / len(probabilities) if probabilities else None
        ),
        "department_aggregates": department_aggregates,
        "predictions": predictions,
        "advisory_disclaimer": (
            "ML predictions are advisory only and do not automatically change attendance records, "
            "payroll, leave decisions, disciplinary actions, or employment status."
        ),
    }
