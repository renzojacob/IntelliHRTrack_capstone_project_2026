"""Production advisory ML for IntelliHRTrack historical employee-period data.

This package is separate from ``core.analytics.research``.  It never uses UCI
rows, research metrics, or research model artifacts.
"""

from .constants import FEATURE_SCHEMA_VERSION, RANDOM_SEED, TARGET_DEFINITION

__all__ = ["FEATURE_SCHEMA_VERSION", "RANDOM_SEED", "TARGET_DEFINITION"]
