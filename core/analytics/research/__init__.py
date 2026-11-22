"""External public-data research experiments.

Nothing in this package is part of IntelliHRTrack's production feature, label,
eligibility, attendance, DTR, or payroll pipeline.
"""

from .uci_absenteeism import (
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    NUMERICAL_FEATURES,
    RANDOM_SEED,
    TARGET_NAME,
    TARGET_THRESHOLD_HOURS,
    load_uci_absenteeism,
)

__all__ = [
    "CATEGORICAL_FEATURES",
    "FEATURE_COLUMNS",
    "NUMERICAL_FEATURES",
    "RANDOM_SEED",
    "TARGET_NAME",
    "TARGET_THRESHOLD_HOURS",
    "load_uci_absenteeism",
]
