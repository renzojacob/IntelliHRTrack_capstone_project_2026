"""Validated loading for the external UCI Absenteeism at Work dataset."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

import pandas as pd


TARGET_NAME = "UCI_EIGHT_PLUS_ABSENCE_HOURS"
TARGET_THRESHOLD_HOURS = 8
RANDOM_SEED = 20261122
PHASE2B_SOURCE_SHA256 = "41930631AA5B14F91FDE29AE595CEFAD2BEAC464DDF837BF8150487E40038320"

CATEGORICAL_FEATURES = (
    "Month of absence",
    "Day of the week",
)
NUMERICAL_FEATURES = (
    "Transportation expense",
    "Distance from Residence to Work",
    "Service time",
)
FEATURE_COLUMNS = CATEGORICAL_FEATURES + NUMERICAL_FEATURES

GROUP_COLUMN = "ID"
RAW_TARGET_COLUMN = "Absenteeism time in hours"

EXPECTED_COLUMNS = (
    "ID",
    "Reason for absence",
    "Month of absence",
    "Day of the week",
    "Seasons",
    "Transportation expense",
    "Distance from Residence to Work",
    "Service time",
    "Age",
    "Work load Average/day ",
    "Hit target",
    "Disciplinary failure",
    "Education",
    "Son",
    "Social drinker",
    "Social smoker",
    "Pet",
    "Weight",
    "Height",
    "Body mass index",
    "Absenteeism time in hours",
)

FORBIDDEN_FEATURES = frozenset(
    {
        GROUP_COLUMN,
        "Reason for absence",
        "Seasons",
        "Age",
        "Work load Average/day ",
        "Hit target",
        "Disciplinary failure",
        "Education",
        "Son",
        "Social drinker",
        "Social smoker",
        "Pet",
        "Weight",
        "Height",
        "Body mass index",
        RAW_TARGET_COLUMN,
        TARGET_NAME,
    }
)


@dataclass(frozen=True)
class UCIAbsenteeismDataset:
    X: pd.DataFrame
    y: pd.Series
    groups: pd.Series
    source_row_numbers: pd.Series
    metadata: dict


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def assert_feature_allowlist(X: pd.DataFrame) -> None:
    actual = tuple(X.columns)
    if actual != FEATURE_COLUMNS:
        raise AssertionError(
            f"Research feature matrix must be exactly {FEATURE_COLUMNS!r}; got {actual!r}."
        )
    leaked = FORBIDDEN_FEATURES.intersection(actual)
    if leaked:
        raise AssertionError(f"Forbidden research predictors detected: {sorted(leaked)!r}")


def construct_target(hours: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(hours, errors="raise")
    if numeric.isna().any():
        raise ValueError("Raw absenteeism hours cannot be missing.")
    if (numeric < 0).any():
        raise ValueError("Raw absenteeism hours cannot be negative.")
    return (numeric >= TARGET_THRESHOLD_HOURS).astype("int8").rename(TARGET_NAME)


def _validate_columns(frame: pd.DataFrame) -> None:
    actual = tuple(frame.columns)
    if actual != EXPECTED_COLUMNS:
        missing = [column for column in EXPECTED_COLUMNS if column not in frame.columns]
        unexpected = [column for column in frame.columns if column not in EXPECTED_COLUMNS]
        raise ValueError(
            "UCI source schema mismatch. "
            f"Missing={missing!r}; unexpected={unexpected!r}; exact_order_match=False."
        )


def load_uci_absenteeism(
    path: str | Path,
    *,
    drop_exact_duplicates: bool = False,
) -> UCIAbsenteeismDataset:
    """Read, validate, and isolate the public research feature matrix.

    The source is hashed before and after reading. It is never written.
    """
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"UCI source file not found: {source_path}")

    hash_before = sha256_file(source_path)
    raw = pd.read_csv(source_path, sep=";", encoding="utf-8")
    _validate_columns(raw)

    for column in EXPECTED_COLUMNS:
        raw[column] = pd.to_numeric(raw[column], errors="raise")

    raw_rows = int(len(raw))
    duplicate_mask = raw.duplicated(keep="first")
    duplicate_rows_beyond_first = int(duplicate_mask.sum())
    duplicate_rows_participating = int(raw.duplicated(keep=False).sum())
    duplicate_patterns = int(raw.loc[raw.duplicated(keep=False)].drop_duplicates().shape[0])

    working = raw.drop_duplicates(keep="first").copy() if drop_exact_duplicates else raw.copy()
    invalid_month_mask = working["Month of absence"].eq(0)
    invalid_month_count = int(invalid_month_mask.sum())
    other_invalid_months = ~working["Month of absence"].between(0, 12)
    if other_invalid_months.any():
        values = sorted(working.loc[other_invalid_months, "Month of absence"].unique().tolist())
        raise ValueError(f"Unexpected month values outside 0..12: {values!r}")

    usable = working.loc[~invalid_month_mask].copy()
    if usable.empty:
        raise ValueError("No usable observations remain after invalid-month exclusion.")
    required_non_null = list(FEATURE_COLUMNS) + [GROUP_COLUMN, RAW_TARGET_COLUMN]
    if usable[required_non_null].isna().any().any():
        raise ValueError("Missing values are not permitted in the Phase 2C fields.")

    y = construct_target(usable[RAW_TARGET_COLUMN]).reset_index(drop=True)
    groups = usable[GROUP_COLUMN].astype("int64").rename(GROUP_COLUMN).reset_index(drop=True)
    X = usable.loc[:, FEATURE_COLUMNS].copy().reset_index(drop=True)
    categorical_columns = list(CATEGORICAL_FEATURES)
    X.loc[:, categorical_columns] = X.loc[:, categorical_columns].astype("int64")
    source_row_numbers = pd.Series(
        usable.index.to_numpy() + 2,
        name="source_csv_row_number",
        dtype="int64",
    ).reset_index(drop=True)
    assert_feature_allowlist(X)

    hash_after = sha256_file(source_path)
    if hash_after != hash_before:
        raise RuntimeError("Source UCI CSV changed while it was being read; experiment aborted.")

    positive = int(y.sum())
    usable_rows = int(len(usable))
    metadata = {
        "source_file": source_path.name,
        "source_sha256_before": hash_before,
        "source_sha256_after": hash_after,
        "source_unchanged": True,
        "delimiter": ";",
        "encoding": "utf-8",
        "raw_rows": raw_rows,
        "source_columns": len(raw.columns),
        "raw_employee_groups": int(raw[GROUP_COLUMN].nunique()),
        "invalid_month_rows_excluded": invalid_month_count,
        "drop_exact_duplicates": bool(drop_exact_duplicates),
        "duplicate_patterns": duplicate_patterns,
        "duplicate_rows_participating": duplicate_rows_participating,
        "duplicate_rows_beyond_first": duplicate_rows_beyond_first,
        "usable_rows": usable_rows,
        "usable_employee_groups": int(groups.nunique()),
        "positive_rows": positive,
        "negative_rows": usable_rows - positive,
        "positive_prevalence": positive / usable_rows,
        "target_name": TARGET_NAME,
        "target_definition": f"1 if {RAW_TARGET_COLUMN} >= {TARGET_THRESHOLD_HOURS}; else 0",
        "feature_columns": list(FEATURE_COLUMNS),
        "group_column": GROUP_COLUMN,
    }
    return UCIAbsenteeismDataset(
        X=X,
        y=y,
        groups=groups,
        source_row_numbers=source_row_numbers,
        metadata=metadata,
    )
