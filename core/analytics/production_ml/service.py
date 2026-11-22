"""Read-only operational ML for the employees visible to the requesting administrator."""
import logging
from collections import Counter

from django.utils import timezone

from core.models import MLModelRegistry
from . import anomaly, dashboard
from .artifacts import load_registry_artifact


def attendance_patterns(profiles, as_of_date):
    registry = MLModelRegistry.objects.get(purpose=anomaly.PURPOSE, status="ACTIVE")
    bundle = load_registry_artifact(registry)
    rows = anomaly.collect(profiles, cutoff=as_of_date, latest_only=True)
    names = {p.pk: p.user.get_full_name() or p.user.username for p in profiles}
    results = [dict(anomaly.score(row, bundle, registry), employee=names[row["employee"]]) for row in rows]
    counts = Counter(row["category"] for row in results if row["available"])
    meta = bundle["training_metadata"]
    return dict(active=True, version=registry.model_version, trained=registry.trained_at,
                start=meta["training_start"], end=meta["training_end"], employees=registry.employee_group_count,
                observations=registry.training_row_count, rows=results, normal=counts["Normal attendance pattern"],
                review=counts["Review recommended"], unusual=counts["Unusual attendance pattern"])


def get_employee_ml_analytics(profiles, *, as_of_date):
    try:
        result = dashboard.build_workforce_ml_payload(profiles, as_of_date=as_of_date)
    except Exception:
        logging.getLogger(__name__).exception("Supervised analytics unavailable")
        result = dict(active=False, status="UNAVAILABLE", predictions={})
    result["feature_type"] = "ABSENTEEISM_SUPERVISED"
    try:
        patterns = attendance_patterns(profiles, as_of_date)
    except MLModelRegistry.DoesNotExist:
        patterns = dict(active=False, message="No active attendance anomaly model.")
    except Exception:
        logging.getLogger(__name__).exception("Attendance anomaly analytics unavailable")
        patterns = dict(active=False, message="Attendance anomaly scoring unavailable.")
    result["anomaly"] = dict(patterns, feature_type=anomaly.PURPOSE, generated_timestamp=timezone.now().isoformat())
    return result
