"""Completed employee-week observations and out-of-time Isolation Forest scoring."""
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, date
from statistics import pstdev
from types import SimpleNamespace

import numpy as np
import pandas as pd
from django.utils import timezone
from sklearn.ensemble import IsolationForest
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.pipeline import Pipeline
from sklearn.utils.validation import check_is_fitted

from core.analytics.features import _default_dtr_builder, _resolve_rules, aggregate_dtr_features
from core.analytics.quality import assess_period_coverage
from core.models import AttendanceRecord, UserProfile
from .constants import NUMERICAL_FEATURES, RANDOM_SEED

PURPOSE = "ATTENDANCE_ANOMALY"
SCHEMA = "employee-week-v1"
FEATURES = tuple(key.removeprefix("previous_") for key in NUMERICAL_FEATURES) + ("worked_hours_stddev",)
CONTEXT = {"approved_leave_days", "travel_days", "holiday_suspension_days"}
INPUTS = tuple(key for key in FEATURES if key not in CONTEXT)
ACCEPTED_PROVENANCE = {"AUDITED_WORKBOOK", "VERIFIED_DEVICE"}


def provenance(record, branch_name):
    # Reuse the DTR rejection rule and the connector's proof contract.
    from core.views import _is_rejected_hikvision_record

    raw = record.raw_row
    if branch_name.casefold() == "occidental mindoro - payroll verification":
        return "FICTIONAL_DEFENSE"
    if not isinstance(raw, dict):
        return "UNKNOWN_SOURCE"
    if raw.get("demo_marker") or raw.get("synthetic") or raw.get("generated") or raw.get("fixture"):
        return "GENERATED_SOURCE"
    if _is_rejected_hikvision_record(record):
        return "REJECTED_DEVICE"
    methods = raw.get("verification_methods")
    if (raw.get("dual_biometric_verified") is True and isinstance(methods, list)
            and "face" in methods and "fingerprint" in methods):
        return "VERIFIED_DEVICE"
    if (raw.get("importer") == "import_client_august_attendance_v1"
            and raw.get("source") == "August-1-31-2026.xlsx"):
        return "AUDITED_WORKBOOK"
    return "UNKNOWN_SOURCE"


def week_start(day):
    return day - timedelta(days=day.weekday())


def observation(profile, start, rules):
    end = start + timedelta(days=6)
    row = dict(employee=profile.pk, start=start, end=end, quality="NO_ATTENDANCE_DATA")
    if not rules:
        return dict(row, quality="MISSING_BRANCH_RULES")
    dtr = _default_dtr_builder(profile, profile.branch, SimpleNamespace(start_date=start, end_date=end), rules)
    if any(not start <= date.fromisoformat(str(day["date"])) <= end for day in dtr["rows"]):
        return dict(row, quality="INVALID_WINDOW")
    coverage = assess_period_coverage(dtr)
    row["source_coverage"] = coverage
    days = coverage["days_with_usable_attendance_evidence"]
    if days < 2:
        return dict(row, quality="INSUFFICIENT_COVERAGE" if days else "NO_ATTENDANCE_DATA")
    previous = aggregate_dtr_features(profile, dtr)["features"]
    hours = [float(day["total_minutes"]) / 60 if day.get("total_minutes") is not None else float(day["total_hours"])
             for day in dtr["rows"] if day["status"] in {"Present", "Half Day", "Incomplete"}
             and (day.get("total_minutes") is not None or day.get("total_hours") is not None)]
    features = {key.removeprefix("previous_"): previous[key] for key in NUMERICAL_FEATURES}
    features["worked_hours_stddev"] = pstdev(hours) if len(hours) >= 2 else np.nan
    return dict(row, quality="COMPLETE" if coverage["coverage_ratio"] == 1 else "PARTIAL", features=features)


def collect(profiles=None, cutoff=None, latest_only=False):
    from core.views import _record_local_datetime

    cutoff = min(cutoff or timezone.localdate(), timezone.localdate())
    if profiles is None:
        profiles = UserProfile.objects.select_related("branch").filter(is_approved=True, user__is_staff=False, user__is_superuser=False)
    rows = []
    for profile in profiles:
        if not profile.biometric_employee_id or not profile.branch_id:
            continue
        weeks = defaultdict(Counter)
        punches = AttendanceRecord.objects.filter(branch_id=profile.branch_id, employee_id=profile.biometric_employee_id,
                                                   timestamp__lt=timezone.make_aware(datetime.combine(cutoff, time.min)))
        for punch in punches.only("timestamp", "raw_row").iterator():
            start = week_start(timezone.localtime(punch.timestamp).date())
            if start + timedelta(days=6) < cutoff:
                source = provenance(punch, profile.branch.name)
                if isinstance(punch.raw_row, dict) and week_start(_record_local_datetime(punch).date()) != start:
                    source = "INCONSISTENT_PUNCH_DATE"
                weeks[start][source] += 1
        rules = _resolve_rules(profile)
        starts = sorted(weeks)
        for start in (starts[-1:] if latest_only else starts):
            rejected_sources = sorted(weeks[start].keys() - ACCEPTED_PROVENANCE)
            if rejected_sources:
                quality = rejected_sources[0] if len(rejected_sources) == 1 else "MIXED_UNVERIFIED_SOURCE"
                row = dict(employee=profile.pk, start=start, end=start + timedelta(days=6), quality=quality)
            else:
                row = observation(profile, start, rules)
            rows.append(dict(row, provenance=dict(weeks[start])))
    return rows


def frame(rows):
    return pd.DataFrame([r["features"] for r in rows], columns=INPUTS)


def assess(rows):
    eligible = [r for r in rows if "features" in r]
    # Reserve the latest observed week for an independently scored diagnostic.
    latest = max((r["start"] for r in eligible), default=None)
    train = [r for r in eligible if r["end"] < latest]
    variable = frame(train).nunique()
    reasons = []
    if len({r["employee"] for r in train}) < 2:
        reasons.append("INSUFFICIENT_TRAINING_EMPLOYEES")
    if len({r["start"] for r in train}) < 2:
        reasons.append("INSUFFICIENT_TRAINING_WEEKS")
    if sum(variable > 1) < 2 or len(frame(train).drop_duplicates()) < 3:
        reasons.append("INSUFFICIENT_MULTIVARIATE_VARIATION")
    if len({(r["employee"], r["start"]) for r in rows}) != len(rows):
        reasons.append("DUPLICATE_EMPLOYEE_WEEK")
    if any(set(r["features"]) != set(FEATURES) or r["start"].weekday() or r["end"] != r["start"] + timedelta(days=6) for r in eligible):
        reasons.append("INVALID_SCHEMA_OR_WINDOW")
    return dict(ready=not reasons, reasons=reasons, candidates=len(rows), eligible=len(eligible),
                excluded=len(rows)-len(eligible), employees=len({r["employee"] for r in eligible}),
                windows=len({r["start"] for r in eligible}), training_observations=len(train),
                training_employees=len({r["employee"] for r in train}), unique_values=variable.to_dict(),
                exclusions=dict(Counter(r["quality"] for r in rows if "features" not in r)),
                start=min((r["start"].isoformat() for r in eligible), default=None),
                end=max((r["end"].isoformat() for r in eligible), default=None)), train


def fit(rows):
    audit, train = assess(rows)
    if not audit["ready"]:
        raise ValueError(", ".join(audit["reasons"]))
    x = frame(train)
    pipeline = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True),
                             IsolationForest(random_state=RANDOM_SEED, contamination="auto"))
    pipeline.fit(x)
    decisions = pipeline.decision_function(x)
    width = float(np.median(abs(decisions - np.median(decisions))) / 2)
    reference = {key: [float(v) for v in x[key].quantile([.25, .5, .75])] for key in INPUTS if x[key].count()}
    meta = dict(readiness=audit, training_start=min(r["start"] for r in train).isoformat(),
                training_end=max(r["end"] for r in train).isoformat(), width=width, reference=reference,
                features=list(INPUTS))
    later = [r for r in rows if "features" in r and r["start"].isoformat() > meta["training_end"]]
    def diagnostics(values):
        return dict(count=len(values), flagged=int(sum(values < 0)), flagged_fraction=float(np.mean(values < 0)),
                    decision_quartiles=np.quantile(values, [0,.25,.5,.75,1]).tolist())
    meta["diagnostics"] = dict(training=diagnostics(decisions), later=diagnostics(pipeline.decision_function(frame(later))))
    return pipeline, meta


def score(row, bundle, registry):
    meta = bundle["training_metadata"]
    result = dict(available=False, feature_type=PURPOSE, model_type="Isolation Forest", model_name="Isolation Forest",
                  model_version=registry.model_version,
                  training_timestamp=registry.trained_at.isoformat(), source_data_start=row["start"].isoformat(),
                  source_data_end=row["end"].isoformat(), data_quality_status=row["quality"])
    if "features" not in row or result["source_data_start"] <= meta["training_end"]:
        return result
    pipeline = bundle["pipeline"]
    decision = float(pipeline.decision_function(frame([row]))[0])
    category = "Unusual attendance pattern" if decision < 0 else "Normal attendance pattern"
    if meta["width"] > 0 and abs(decision) <= meta["width"]:
        category = "Review recommended"
    observed = []
    for key, (low, median, high) in meta["reference"].items():
        value = row["features"][key]
        if value < low or value > high:
            observed.append(f"{key.replace('_', ' ')}: {value:.2f} (historical middle half {low:.2f}–{high:.2f})")
    return dict(result, available=True, ml_advisory_score=round(-float(pipeline.score_samples(frame([row]))[0]), 4),
                category=category, explanation=observed)


def validate_bundle(bundle, registry):
    from .artifacts import MLArtifactError

    try:
        model = bundle["pipeline"]
        metadata = bundle["training_metadata"]
        valid = (
            bundle["purpose"] == registry.purpose == PURPOSE
            and bundle["model_version"] == registry.model_version
            and bundle["model_key"] == registry.model_key == "isolation_forest"
            and bundle["model_name"] == registry.model_name == "Isolation Forest"
            and bundle["feature_schema_version"] == registry.feature_schema_version == SCHEMA
            and bundle["feature_names"] == metadata["features"] == list(INPUTS)
            and metadata == registry.validation_metadata
            and metadata["readiness"]["ready"] is True
            and metadata["readiness"]["training_observations"] == registry.training_row_count
            and metadata["readiness"]["training_employees"] == registry.employee_group_count
            and metadata["diagnostics"]["later"]["count"] > 0
            and np.isfinite(metadata["width"]) and metadata["width"] >= 0
            and isinstance(model, Pipeline) and isinstance(model[-1], IsolationForest)
            and model[-1].random_state == registry.random_seed == RANDOM_SEED
            and list(model.feature_names_in_) == list(INPUTS)
        )
        check_is_fitted(model[-1])
    except (ValueError, KeyError, AttributeError, TypeError, IndexError) as exc:
        raise MLArtifactError("Invalid attendance anomaly artifact.") from exc
    if not valid:
        raise MLArtifactError("Attendance anomaly artifact does not match its registry.")
