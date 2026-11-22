"""Analytics-only data preparation for explainable workforce insights.

This package may read deterministic DTR output, but it must never influence
DTR classification, payroll calculation, deductions, or finalization.
"""

from .features import (
    build_employee_period_dataset,
    generate_employee_period_features,
    pair_chronological_periods,
)
from .labels import build_unprotected_absence_target
from .quality import (
    MLCoveragePolicy,
    assess_dataset_eligibility,
    assess_ml_readiness,
    assess_observation_eligibility,
    assess_period_coverage,
    summarize_ml_dataset,
)
from .rules import assess_data_quality, calculate_attendance_risk

__all__ = [
    "assess_data_quality",
    "assess_dataset_eligibility",
    "assess_ml_readiness",
    "assess_observation_eligibility",
    "assess_period_coverage",
    "build_employee_period_dataset",
    "build_unprotected_absence_target",
    "calculate_attendance_risk",
    "generate_employee_period_features",
    "MLCoveragePolicy",
    "pair_chronological_periods",
    "summarize_ml_dataset",
]
