from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from core.analytics.features import FEATURE_NAMES


RANDOM_SEED = 20261122
DATASET_VERSION = "INTELLIHRTRACK_EMPLOYEE_PERIOD_V1"
FEATURE_SCHEMA_VERSION = "INTELLIHRTRACK_ABSENCE_FEATURES_V1"
MODEL_PURPOSE = "NEXT_PERIOD_QUALIFYING_ABSENCE"
TARGET_DEFINITION = (
    "1 when an employee has at least one qualifying, unprotected exact Absent "
    "DTR day in the immediately following eligible payroll period; otherwise 0."
)

NUMERICAL_FEATURES = tuple(name for name in FEATURE_NAMES if name not in {"employment_type", "department"})
CATEGORICAL_FEATURES = ("employment_type", "department")
EXPECTED_FEATURES = tuple(FEATURE_NAMES)

MODEL_DISPLAY_NAMES = {
    "logistic_regression": "Logistic Regression",
    "decision_tree": "Decision Tree",
    "random_forest": "Random Forest",
    "gradient_boosting": "Gradient Boosting",
}

RISK_BANDS = {
    "low_upper_exclusive": 0.40,
    "medium_upper_exclusive": 0.70,
}


def private_ml_root() -> Path:
    configured = getattr(settings, "ML_ARTIFACT_ROOT", None)
    root = (Path(configured) if configured else Path(settings.BASE_DIR) / "private_ml_artifacts").resolve()
    for setting_name in ("MEDIA_ROOT", "STATIC_ROOT"):
        public_root = getattr(settings, setting_name, None)
        if not public_root:
            continue
        public_root = Path(public_root).resolve()
        if root == public_root or public_root in root.parents:
            raise ImproperlyConfigured(f"ML_ARTIFACT_ROOT must not be inside {setting_name}.")
    return root
