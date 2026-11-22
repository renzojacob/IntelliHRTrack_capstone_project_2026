"""Read-only advisory prediction service for an ACTIVE production model."""

from __future__ import annotations

import pandas as pd
from django.utils import timezone

from core.analytics.features import (
    _period_cadence,
    _resolve_rules,
    aggregate_dtr_features,
)
from core.analytics.quality import assess_period_coverage
from core.models import MLModelRegistry, PayrollPeriod

from .artifacts import MLArtifactError, load_registry_artifact
from .constants import EXPECTED_FEATURES, MODEL_PURPOSE
from .dataset import authoritative_dtr_builder


def probability_risk_level(probability, bands):
    value = float(probability)
    if value < float(bands["low_upper_exclusive"]):
        return "Low"
    if value < float(bands["medium_upper_exclusive"]):
        return "Medium"
    return "High"


def get_active_registry():
    active = list(
        MLModelRegistry.objects.filter(
            purpose=MODEL_PURPOSE,
            status=MLModelRegistry.STATUS_ACTIVE,
        ).order_by("-activated_at", "-pk")[:2]
    )
    if len(active) != 1:
        return None, (
            "No active production ML model."
            if not active
            else "Production ML registry conflict: multiple active models."
        )
    return active[0], None


def _latest_feature_period(as_of_date, periods=None):
    periods = list(periods if periods is not None else PayrollPeriod.objects.all())
    completed = [
        period
        for period in periods
        if getattr(period, "end_date", None)
        and period.end_date <= as_of_date
        and _period_cadence(period)
    ]
    if not completed:
        return None, None
    feature_period = max(completed, key=lambda item: (item.end_date, item.start_date, item.pk or 0))
    cadence = _period_cadence(feature_period)
    later = sorted(
        [
            period
            for period in periods
            if _period_cadence(period) == cadence
            and getattr(period, "start_date", None)
            and period.start_date > feature_period.end_date
        ],
        key=lambda item: (item.start_date, item.end_date, item.pk or 0),
    )
    return feature_period, later[0] if later else None


def _relevant_indicators(features, explanation):
    indicators = []
    for item in (explanation or {}).get("features", [])[:5]:
        transformed_name = str(item.get("feature") or "")
        source_name = next(
            (name for name in EXPECTED_FEATURES if transformed_name == name or transformed_name.startswith(f"{name}_")),
            transformed_name,
        )
        indicators.append(
            {
                "indicator": source_name,
                "value": features.get(source_name),
                "global_importance": item.get("importance"),
            }
        )
    return indicators


def predict_absenteeism_risk(
    profile,
    *,
    as_of_date=None,
    registry=None,
    artifact=None,
    periods=None,
    rules=None,
    dtr_builder=None,
):
    """Return one advisory prediction without writing operational data."""

    as_of_date = as_of_date or timezone.localdate()
    if registry is None:
        registry, unavailable_reason = get_active_registry()
        if registry is None:
            return {"available": False, "status": "NO_ACTIVE_MODEL", "reason": unavailable_reason}
    if registry.status != MLModelRegistry.STATUS_ACTIVE:
        return {"available": False, "status": "MODEL_INACTIVE", "reason": "The selected model is not ACTIVE."}
    try:
        artifact = artifact or load_registry_artifact(registry)
    except MLArtifactError as exc:
        return {"available": False, "status": "INVALID_ARTIFACT", "reason": str(exc)}

    feature_period, target_period = _latest_feature_period(as_of_date, periods=periods)
    if feature_period is None:
        return {
            "available": False,
            "status": "INSUFFICIENT_CURRENT_FEATURE_DATA",
            "reason": "No completed eligible payroll period is available for a feature snapshot.",
        }
    branch = getattr(profile, "branch", None)
    resolved_rules = rules or _resolve_rules(profile)
    if branch is None or resolved_rules is None:
        return {
            "available": False,
            "status": "INSUFFICIENT_CURRENT_FEATURE_DATA",
            "reason": "Employee branch or payroll rules are unavailable.",
        }
    builder = dtr_builder or authoritative_dtr_builder
    try:
        dtr_output = builder(profile, branch, feature_period, resolved_rules)
        feature_result = aggregate_dtr_features(profile, dtr_output)
    except (TypeError, ValueError, LookupError) as exc:
        return {
            "available": False,
            "status": "INSUFFICIENT_CURRENT_FEATURE_DATA",
            "reason": f"Feature snapshot is unavailable: {exc}",
        }
    coverage = assess_period_coverage(dtr_output)
    if coverage["days_with_usable_attendance_evidence"] <= 0:
        return {
            "available": False,
            "status": "INSUFFICIENT_CURRENT_FEATURE_DATA",
            "reason": "The latest completed period has no usable attendance evidence.",
            "data_quality_warnings": ["NO_FEATURE_ATTENDANCE_COVERAGE"],
        }

    features = feature_result["features"]
    frame = pd.DataFrame([[features[name] for name in EXPECTED_FEATURES]], columns=list(EXPECTED_FEATURES))
    probability = float(artifact["pipeline"].predict_proba(frame)[0, 1])
    threshold = float(artifact["threshold"])
    quality = feature_result.get("data_quality", {})
    warnings = list(quality.get("reasons", [])) if quality.get("status") != "Good" else []
    return {
        "available": True,
        "status": "AVAILABLE",
        "predicted_class": int(probability >= threshold),
        "probability": probability,
        "risk_level": probability_risk_level(probability, artifact["risk_bands"]),
        "model_name": registry.model_name,
        "model_version": registry.model_version,
        "threshold": threshold,
        "prediction_timestamp": timezone.now().isoformat(),
        "feature_period": {
            "id": feature_period.pk,
            "start": feature_period.start_date.isoformat(),
            "end": feature_period.end_date.isoformat(),
        },
        "target_period": (
            {
                "id": target_period.pk,
                "start": target_period.start_date.isoformat(),
                "end": target_period.end_date.isoformat(),
            }
            if target_period
            else None
        ),
        "feature_snapshot": features,
        "relevant_historical_indicators": _relevant_indicators(features, artifact.get("explanation")),
        "explanation_scope": "Global model importance plus this employee's historical feature values; not a causal explanation.",
        "data_quality_warnings": warnings,
        "advisory_disclaimer": (
            "This prediction is an advisory workforce analytics indicator. It does not automatically "
            "change attendance records, payroll, leave decisions, disciplinary actions, or employment status."
        ),
    }
