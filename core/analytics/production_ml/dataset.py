"""Build private production ML datasets from IntelliHRTrack history only."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from core.analytics.features import (
    _default_dtr_builder,
    _resolve_rules,
    aggregate_dtr_features,
    pair_chronological_periods,
)
from core.analytics.labels import build_unprotected_absence_target
from core.analytics.quality import assess_dataset_eligibility
from core.models import FinalizedDTR, PayrollPeriod, UserProfile

from .constants import (
    DATASET_VERSION,
    EXPECTED_FEATURES,
    FEATURE_SCHEMA_VERSION,
    RANDOM_SEED,
    TARGET_DEFINITION,
    private_ml_root,
)
from .readiness import evaluate_production_readiness


def _source_version():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=settings.BASE_DIR,
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _group_token(identifier) -> str:
    secret = str(settings.SECRET_KEY).encode("utf-8")
    return hmac.new(secret, f"ml-employee-group:{identifier}".encode("utf-8"), hashlib.sha256).hexdigest()


def _empty_features(profile):
    values = {name: 0 for name in EXPECTED_FEATURES}
    values["employment_type"] = str(getattr(profile, "employment_type", "") or "")
    values["department"] = str(getattr(profile, "department", "") or "Unassigned")
    return values


def authoritative_dtr_builder(profile, branch, period, rules):
    """Prefer an immutable locked DTR snapshot, otherwise classify history."""

    locked = FinalizedDTR.objects.filter(
        profile=profile,
        branch=branch,
        period=period,
        is_locked=True,
    ).first()
    if locked is None:
        return _default_dtr_builder(profile, branch, period, rules)
    rows = list(locked.rows or [])
    summary = dict(locked.summary or {})
    records_used = summary.get("records_used")
    if records_used is None:
        records_used = sum(int(row.get("raw_log_count") or 0) for row in rows if isinstance(row, dict))
    return {
        **summary,
        "rows": rows,
        "records_used": records_used,
        "source": "locked_finalized_dtr",
    }


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def build_production_ml_dataset(
    *,
    as_of_timestamp=None,
    profiles=None,
    periods=None,
    rules_resolver=None,
    dtr_builder=None,
    policy=None,
):
    """Construct and quality-assess t -> t+1 employee-period observations.

    This function is read-only.  It does not create attendance, DTR, payroll,
    leave, or employee records.
    """

    as_of_timestamp = as_of_timestamp or timezone.now()
    as_of_date = as_of_timestamp.date() if isinstance(as_of_timestamp, datetime) else as_of_timestamp
    generated_at = timezone.now()
    if profiles is None:
        profiles = UserProfile.objects.select_related("user", "branch").filter(
            is_approved=True,
            user__is_staff=False,
            user__is_superuser=False,
        )
    if periods is None:
        periods = PayrollPeriod.objects.all()

    profiles = list(profiles)
    pairs = pair_chronological_periods(list(periods))
    builder = dtr_builder or authoritative_dtr_builder
    raw_records = []

    for profile in profiles:
        branch = getattr(profile, "branch", None)
        rules = _resolve_rules(profile, rules_resolver=rules_resolver)
        for feature_period, target_period in pairs:
            feature_dtr = {}
            target_dtr = {}
            feature_result = None
            if branch is not None and rules is not None:
                try:
                    feature_dtr = builder(profile, branch, feature_period, rules) or {}
                    target_dtr = builder(profile, branch, target_period, rules) or {}
                    feature_result = aggregate_dtr_features(profile, feature_dtr)
                except (TypeError, ValueError, LookupError):
                    feature_dtr = {}
                    target_dtr = {}

            features = (
                dict(feature_result["features"])
                if feature_result is not None
                else _empty_features(profile)
            )
            observation = {
                "employee_group_id": getattr(profile, "pk", None) or getattr(profile, "id", None),
                "feature_period_id": getattr(feature_period, "pk", None) or getattr(feature_period, "id", None),
                "feature_period_start": feature_period.start_date,
                "feature_period_end": feature_period.end_date,
                "target_period_id": getattr(target_period, "pk", None) or getattr(target_period, "id", None),
                "target_period_start": target_period.start_date,
                "target_period_end": target_period.end_date,
                "features": features,
                "target": build_unprotected_absence_target(target_dtr),
            }
            raw_records.append(
                {
                    "observation": observation,
                    "feature_period": feature_period,
                    "target_period": target_period,
                    "feature_dtr": feature_dtr,
                    "target_dtr": target_dtr,
                }
            )

    assessed = assess_dataset_eligibility(raw_records, as_of_date=as_of_date, policy=policy)
    readiness = evaluate_production_readiness(assessed)
    eligible = [row["observation"] for row in assessed if row["ml_eligibility"]["eligible"]]
    feature_periods = sorted({str(row["feature_period_id"]) for row in eligible})
    target_periods = sorted({str(row["target_period_id"]) for row in eligible})
    exclusions = Counter(
        reason for row in assessed for reason in row["ml_eligibility"]["exclusion_reasons"]
    )
    metadata = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "generated_at": generated_at.isoformat(),
        "as_of_timestamp": (
            as_of_timestamp.isoformat()
            if hasattr(as_of_timestamp, "isoformat")
            else str(as_of_timestamp)
        ),
        "random_seed": RANDOM_SEED,
        "employee_count": len({row["employee_group_id"] for row in eligible}),
        "candidate_observation_count": len(assessed),
        "eligible_observation_count": len(eligible),
        "excluded_observation_count": len(assessed) - len(eligible),
        "positive_count": sum(int(row["target"]) == 1 for row in eligible),
        "negative_count": sum(int(row["target"]) == 0 for row in eligible),
        "feature_names": list(EXPECTED_FEATURES),
        "target_name": "NEXT_PERIOD_QUALIFYING_ABSENCE",
        "target_definition": TARGET_DEFINITION,
        "feature_periods": feature_periods,
        "target_periods": target_periods,
        "exclusion_reason_counts": dict(sorted(exclusions.items())),
        "data_quality_limitations": readiness["summary"].get("known_limitations", []),
        "source_code_version": _source_version(),
        "contains_uci_data": False,
    }
    return {
        "metadata": metadata,
        "assessed_observations": assessed,
        "eligible_observations": eligible,
        "readiness": readiness,
    }


def private_dataset_document(dataset_bundle):
    """Return a JSON-safe private export with pseudonymous group tokens."""

    rows = []
    for assessed in dataset_bundle["assessed_observations"]:
        observation = assessed["observation"]
        rows.append(
            {
                "employee_group": _group_token(observation.get("employee_group_id")),
                "feature_period_id": observation.get("feature_period_id"),
                "feature_period_start": _json_value(observation.get("feature_period_start")),
                "feature_period_end": _json_value(observation.get("feature_period_end")),
                "target_period_id": observation.get("target_period_id"),
                "target_period_start": _json_value(observation.get("target_period_start")),
                "target_period_end": _json_value(observation.get("target_period_end")),
                "features": observation.get("features"),
                "target": observation.get("target"),
                "eligible": assessed["ml_eligibility"]["eligible"],
                "exclusion_reasons": assessed["ml_eligibility"]["exclusion_reasons"],
            }
        )
    readiness = dict(dataset_bundle["readiness"])
    readiness.pop("split_plan", None)
    return {"metadata": dataset_bundle["metadata"], "readiness": readiness, "observations": rows}


def export_private_dataset(dataset_bundle, output_path: str | Path | None = None):
    root = private_ml_root().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        stamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
        path = root / "datasets" / f"intellihrtrack_ml_dataset_{stamp}.json"
    else:
        path = Path(output_path)
        if not path.is_absolute():
            path = root / path
    path = path.resolve()
    if path != root and root not in path.parents:
        raise ValueError("Production ML datasets must remain inside ML_ARTIFACT_ROOT.")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = private_dataset_document(dataset_bundle)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    return path
