"""Nested, employee-group-aware Phase 2C public research experiment."""

from __future__ import annotations

from copy import deepcopy
from importlib import metadata as package_metadata
import platform
from pathlib import Path
from typing import Iterable

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import GridSearchCV, StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.tree import DecisionTreeClassifier

from .uci_absenteeism import (
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    NUMERICAL_FEATURES,
    PHASE2B_SOURCE_SHA256,
    RANDOM_SEED,
    TARGET_NAME,
    UCIAbsenteeismDataset,
    assert_feature_allowlist,
    load_uci_absenteeism,
    sha256_file,
)
from .interpretation import add_scientific_interpretation


OUTER_SPLITS = 5
INNER_SPLITS = 4
INNER_SCORING = "average_precision"

MODEL_DISPLAY_NAMES = {
    "baseline": "Dummy prior baseline",
    "logistic_regression": "Logistic Regression",
    "decision_tree": "Decision Tree",
    "random_forest": "Random Forest",
    "gradient_boosting": "Gradient Boosting",
}

HYPERPARAMETER_GRIDS = {
    "logistic_regression": {
        "model__C": [0.1, 1.0, 10.0],
        "model__class_weight": [None, "balanced"],
    },
    "decision_tree": {
        "model__max_depth": [3, 5, None],
        "model__min_samples_leaf": [1, 5],
        "model__class_weight": [None, "balanced"],
    },
    "random_forest": {
        "model__n_estimators": [100, 200],
        "model__max_depth": [5, None],
        "model__min_samples_leaf": [1, 5],
        "model__class_weight": [None, "balanced"],
    },
    "gradient_boosting": {
        "model__n_estimators": [50, 100],
        "model__learning_rate": [0.05, 0.1],
        "model__max_depth": [1, 3],
    },
}

METRIC_NAMES = (
    "recall",
    "f1",
    "average_precision",
    "precision",
    "accuracy",
    "roc_auc",
)


def _categorical_transformer() -> OneHotEncoder:
    return OneHotEncoder(handle_unknown="ignore", sparse_output=False)


def build_preprocessor(*, scale_numeric: bool) -> ColumnTransformer:
    numeric_transformer = StandardScaler() if scale_numeric else "passthrough"
    return ColumnTransformer(
        transformers=[
            ("categorical", _categorical_transformer(), list(CATEGORICAL_FEATURES)),
            ("numerical", numeric_transformer, list(NUMERICAL_FEATURES)),
        ],
        remainder="drop",
        verbose_feature_names_out=True,
    )


def build_model_pipelines(seed: int = RANDOM_SEED) -> dict[str, Pipeline]:
    tree_preprocessor = build_preprocessor(scale_numeric=False)
    results = {
        "baseline": Pipeline(
            [
                ("preprocess", deepcopy(tree_preprocessor)),
                ("model", DummyClassifier(strategy="prior")),
            ]
        ),
        "logistic_regression": Pipeline(
            [
                ("preprocess", build_preprocessor(scale_numeric=True)),
                (
                    "model",
                    LogisticRegression(
                        max_iter=2000,
                        solver="liblinear",
                        random_state=seed,
                    ),
                ),
            ]
        ),
        "decision_tree": Pipeline(
            [
                ("preprocess", deepcopy(tree_preprocessor)),
                ("model", DecisionTreeClassifier(random_state=seed)),
            ]
        ),
        "random_forest": Pipeline(
            [
                ("preprocess", deepcopy(tree_preprocessor)),
                (
                    "model",
                    RandomForestClassifier(random_state=seed, n_jobs=1),
                ),
            ]
        ),
        "gradient_boosting": Pipeline(
            [
                ("preprocess", deepcopy(tree_preprocessor)),
                ("model", GradientBoostingClassifier(random_state=seed)),
            ]
        ),
    }
    return results


def _validate_fold(
    dataset: UCIAbsenteeismDataset,
    train_indices: np.ndarray,
    test_indices: np.ndarray,
    *,
    fold_number: int,
) -> dict:
    train_groups = set(dataset.groups.iloc[train_indices].astype(int).tolist())
    test_groups = set(dataset.groups.iloc[test_indices].astype(int).tolist())
    overlap = train_groups.intersection(test_groups)
    if overlap:
        raise AssertionError(f"Employee group leakage in outer fold {fold_number}: {sorted(overlap)!r}")

    y_train = dataset.y.iloc[train_indices]
    y_test = dataset.y.iloc[test_indices]
    if y_train.nunique() != 2 or y_test.nunique() != 2:
        raise ValueError(f"Both classes are required in train and test for outer fold {fold_number}.")

    return {
        "fold": fold_number,
        "train_observations": int(len(train_indices)),
        "test_observations": int(len(test_indices)),
        "train_employee_groups": len(train_groups),
        "test_employee_groups": len(test_groups),
        "train_positive_prevalence": float(y_train.mean()),
        "test_positive_prevalence": float(y_test.mean()),
        "group_overlap_count": 0,
        "zero_group_overlap": True,
        "test_group_ids": sorted(test_groups),
        "test_source_csv_rows": dataset.source_row_numbers.iloc[test_indices].astype(int).tolist(),
    }


def make_outer_folds(
    dataset: UCIAbsenteeismDataset,
    *,
    seed: int = RANDOM_SEED,
    fixed_test_groups: Iterable[Iterable[int]] | None = None,
) -> list[dict]:
    assert_feature_allowlist(dataset.X)
    folds = []
    if fixed_test_groups is None:
        splitter = StratifiedGroupKFold(
            n_splits=OUTER_SPLITS,
            shuffle=True,
            random_state=seed,
        )
        raw_splits = list(splitter.split(dataset.X, dataset.y, dataset.groups))
    else:
        fixed = [set(int(value) for value in group_ids) for group_ids in fixed_test_groups]
        if len(fixed) != OUTER_SPLITS:
            raise ValueError(f"Expected {OUTER_SPLITS} fixed outer group sets; got {len(fixed)}.")
        all_indices = np.arange(len(dataset.X), dtype=int)
        raw_splits = []
        for test_groups in fixed:
            test_mask = dataset.groups.isin(test_groups).to_numpy()
            raw_splits.append((all_indices[~test_mask], all_indices[test_mask]))

    for fold_number, (train_indices, test_indices) in enumerate(raw_splits, start=1):
        train_array = np.asarray(train_indices, dtype=int)
        test_array = np.asarray(test_indices, dtype=int)
        summary = _validate_fold(
            dataset,
            train_array,
            test_array,
            fold_number=fold_number,
        )
        folds.append(
            {
                "train_indices": train_array,
                "test_indices": test_array,
                "summary": summary,
            }
        )
    return folds


def _make_inner_splits(X, y, groups, *, seed: int, outer_fold: int):
    splitter = StratifiedGroupKFold(
        n_splits=INNER_SPLITS,
        shuffle=True,
        random_state=seed + outer_fold,
    )
    splits = list(splitter.split(X, y, groups))
    for inner_fold, (train_indices, validation_indices) in enumerate(splits, start=1):
        train_groups = set(groups.iloc[train_indices].astype(int).tolist())
        validation_groups = set(groups.iloc[validation_indices].astype(int).tolist())
        overlap = train_groups.intersection(validation_groups)
        if overlap:
            raise AssertionError(
                f"Employee group leakage in outer fold {outer_fold}, inner fold {inner_fold}."
            )
        if y.iloc[train_indices].nunique() != 2 or y.iloc[validation_indices].nunique() != 2:
            raise ValueError(
                f"Both classes are required in outer fold {outer_fold}, inner fold {inner_fold}."
            )
    return splits


def calculate_metrics(y_true, predictions, scores) -> dict:
    matrix = confusion_matrix(y_true, predictions, labels=[0, 1])
    result = {
        "recall": float(recall_score(y_true, predictions, zero_division=0)),
        "f1": float(f1_score(y_true, predictions, zero_division=0)),
        "average_precision": float(average_precision_score(y_true, scores)),
        "precision": float(precision_score(y_true, predictions, zero_division=0)),
        "accuracy": float(accuracy_score(y_true, predictions)),
        "roc_auc": None,
        "confusion_matrix": matrix.astype(int).tolist(),
    }
    if len(np.unique(y_true)) == 2:
        result["roc_auc"] = float(roc_auc_score(y_true, scores))
    return result


def _aggregate_fold_metrics(per_fold: list[dict]) -> dict:
    aggregate = {}
    for metric in METRIC_NAMES:
        values = [fold["metrics"][metric] for fold in per_fold if fold["metrics"][metric] is not None]
        aggregate[f"mean_{metric}"] = float(np.mean(values)) if values else None
        aggregate[f"std_{metric}"] = float(np.std(values, ddof=0)) if values else None
    aggregate["std_definition"] = "population standard deviation across the five outer folds (ddof=0)"
    return aggregate


def evaluate_bundle(
    dataset: UCIAbsenteeismDataset,
    *,
    seed: int = RANDOM_SEED,
    fixed_test_groups: Iterable[Iterable[int]] | None = None,
) -> dict:
    assert_feature_allowlist(dataset.X)
    folds = make_outer_folds(dataset, seed=seed, fixed_test_groups=fixed_test_groups)
    pipelines = build_model_pipelines(seed)
    model_results = {}

    for model_name, pipeline in pipelines.items():
        oof_predictions = np.full(len(dataset.X), -1, dtype=int)
        oof_scores = np.full(len(dataset.X), np.nan, dtype=float)
        per_fold = []

        for fold in folds:
            summary = fold["summary"]
            fold_number = summary["fold"]
            train_indices = fold["train_indices"]
            test_indices = fold["test_indices"]
            X_train = dataset.X.iloc[train_indices]
            X_test = dataset.X.iloc[test_indices]
            y_train = dataset.y.iloc[train_indices]
            y_test = dataset.y.iloc[test_indices]
            groups_train = dataset.groups.iloc[train_indices].reset_index(drop=True)

            if model_name == "baseline":
                fitted = pipeline.fit(X_train, y_train)
                selected_parameters = {}
                inner_best_score = None
            else:
                inner_splits = _make_inner_splits(
                    X_train.reset_index(drop=True),
                    y_train.reset_index(drop=True),
                    groups_train,
                    seed=seed,
                    outer_fold=fold_number,
                )
                search = GridSearchCV(
                    estimator=pipeline,
                    param_grid=HYPERPARAMETER_GRIDS[model_name],
                    scoring=INNER_SCORING,
                    cv=inner_splits,
                    refit=True,
                    n_jobs=1,
                    error_score="raise",
                    return_train_score=False,
                )
                fitted = search.fit(X_train, y_train)
                selected_parameters = {
                    key: value for key, value in sorted(search.best_params_.items())
                }
                inner_best_score = float(search.best_score_)

            predictions = fitted.predict(X_test).astype(int)
            scores = fitted.predict_proba(X_test)[:, 1].astype(float)
            oof_predictions[test_indices] = predictions
            oof_scores[test_indices] = scores
            per_fold.append(
                {
                    **summary,
                    "selected_hyperparameters": selected_parameters,
                    "inner_cv_best_average_precision": inner_best_score,
                    "metrics": calculate_metrics(y_test, predictions, scores),
                }
            )

        if (oof_predictions < 0).any() or np.isnan(oof_scores).any():
            raise AssertionError(f"Incomplete out-of-fold predictions for {model_name}.")
        model_results[model_name] = {
            "display_name": MODEL_DISPLAY_NAMES[model_name],
            "per_fold": per_fold,
            "aggregate": _aggregate_fold_metrics(per_fold),
            "pooled": calculate_metrics(dataset.y, oof_predictions, oof_scores),
        }

    return {
        "folds": [fold["summary"] for fold in folds],
        "fixed_test_groups": [fold["summary"]["test_group_ids"] for fold in folds],
        "models": model_results,
    }


def select_research_model(model_results: dict) -> dict:
    """Select with practical tolerances before AP/variance/complexity tie-breaks."""
    candidates = [name for name in MODEL_DISPLAY_NAMES if name != "baseline"]
    best_recall = max(model_results[name]["aggregate"]["mean_recall"] for name in candidates)
    recall_candidates = [
        name
        for name in candidates
        if model_results[name]["aggregate"]["mean_recall"] >= best_recall - 0.02
    ]
    best_f1 = max(model_results[name]["aggregate"]["mean_f1"] for name in recall_candidates)
    f1_candidates = [
        name
        for name in recall_candidates
        if model_results[name]["aggregate"]["mean_f1"] >= best_f1 - 0.01
    ]
    simplicity = {
        "logistic_regression": 4,
        "decision_tree": 3,
        "gradient_boosting": 2,
        "random_forest": 1,
    }
    selected = max(
        f1_candidates,
        key=lambda name: (
            model_results[name]["aggregate"]["mean_average_precision"],
            -model_results[name]["aggregate"]["std_average_precision"],
            simplicity[name],
        ),
    )
    return {
        "model_key": selected,
        "display_name": MODEL_DISPLAY_NAMES[selected],
        "rule": (
            "Retain models within 0.02 absolute mean recall of the best; then within "
            "0.01 mean F1 of the best retained model; select by mean Average Precision, "
            "then lower AP variance and lower complexity."
        ),
        "best_mean_recall": best_recall,
        "recall_tolerance_candidates": recall_candidates,
        "f1_tolerance_candidates": f1_candidates,
    }


def _installed_version(distribution: str) -> str:
    try:
        return package_metadata.version(distribution)
    except package_metadata.PackageNotFoundError:
        return "NOT_INSTALLED"


def environment_metadata() -> dict:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "Django": _installed_version("Django"),
        "numpy": _installed_version("numpy"),
        "pandas": _installed_version("pandas"),
        "scipy": _installed_version("scipy"),
        "scikit-learn": _installed_version("scikit-learn"),
        "joblib": _installed_version("joblib"),
    }


def run_phase2c_experiment(
    source_path: str | Path,
    *,
    expected_source_sha256: str | None = None,
) -> dict:
    source = Path(source_path).expanduser().resolve()
    original_hash = sha256_file(source)
    if expected_source_sha256 and original_hash != expected_source_sha256.upper():
        raise ValueError(
            "UCI source SHA-256 does not match the verified Phase 2B source; "
            "experiment aborted."
        )
    primary_dataset = load_uci_absenteeism(source, drop_exact_duplicates=False)
    primary = evaluate_bundle(primary_dataset, seed=RANDOM_SEED)
    primary_selection = select_research_model(primary["models"])

    sensitivity_dataset = load_uci_absenteeism(source, drop_exact_duplicates=True)
    sensitivity = evaluate_bundle(
        sensitivity_dataset,
        seed=RANDOM_SEED,
        fixed_test_groups=primary["fixed_test_groups"],
    )
    sensitivity_selection = select_research_model(sensitivity["models"])

    selected_key = primary_selection["model_key"]
    metric_differences = {}
    for metric in ("mean_recall", "mean_f1", "mean_average_precision"):
        primary_value = primary["models"][selected_key]["aggregate"][metric]
        sensitivity_value = sensitivity["models"][selected_key]["aggregate"][metric]
        metric_differences[metric] = sensitivity_value - primary_value
    material_change = (
        primary_selection["model_key"] != sensitivity_selection["model_key"]
        or any(abs(value) > 0.05 for value in metric_differences.values())
    )

    final_hash = sha256_file(source)
    if final_hash != original_hash:
        raise RuntimeError("Source UCI CSV changed during experiment execution; results discarded.")

    return {
        "schema_version": 2,
        "experiment_name": "IntelliHRTrack Phase 2C UCI public research comparison",
        "research_only": True,
        "random_seed": RANDOM_SEED,
        "environment": environment_metadata(),
        "dataset": primary_dataset.metadata,
        "verified_phase2b_source_sha256": expected_source_sha256,
        "target": {
            "name": TARGET_NAME,
            "definition": primary_dataset.metadata["target_definition"],
        },
        "predictors": list(FEATURE_COLUMNS),
        "exclusions": {
            "invalid_month_rule": "Exclude exactly Month of absence == 0.",
            "invalid_month_rows": primary_dataset.metadata["invalid_month_rows_excluded"],
            "group_only": "ID",
            "raw_target_removed": "Absenteeism time in hours",
            "primary_duplicates": "retained",
        },
        "evaluation": {
            "outer_cv": "5-fold StratifiedGroupKFold(shuffle=True)",
            "inner_cv": "4-fold StratifiedGroupKFold(shuffle=True)",
            "groups": "ID",
            "inner_scoring": INNER_SCORING,
            "metric_std": "population standard deviation across outer folds (ddof=0)",
            "hyperparameter_grids": HYPERPARAMETER_GRIDS,
        },
        "primary": primary,
        "selected_public_research_model": primary_selection,
        "duplicate_sensitivity": {
            "dataset": sensitivity_dataset.metadata,
            "evaluation": sensitivity,
            "selected_model": sensitivity_selection,
            "primary_selected_model_metric_differences": metric_differences,
            "material_conclusion_change": material_change,
            "materiality_rule": (
                "Material if selected model changes or absolute mean Recall, F1, or AP "
                "changes by more than 0.05 for the primary selected model."
            ),
        },
        "source_sha256_after_experiment": final_hash,
        "source_unchanged": True,
        "external_validity_disclaimer": (
            "This external UCI research dataset does not represent IntelliHRTrack employees, "
            "branches, payroll periods, attendance devices, or organizational policies. "
            "Results are not validated for IntelliHRTrack production use."
        ),
    }
    return add_scientific_interpretation(results)
