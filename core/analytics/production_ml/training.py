"""Temporal training/evaluation for production advisory absence models."""

from __future__ import annotations

from copy import deepcopy

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
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
from sklearn.model_selection import GridSearchCV
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.tree import DecisionTreeClassifier

from .constants import (
    CATEGORICAL_FEATURES,
    EXPECTED_FEATURES,
    MODEL_DISPLAY_NAMES,
    NUMERICAL_FEATURES,
    RANDOM_SEED,
)


class ProductionTrainingBlocked(RuntimeError):
    def __init__(self, readiness):
        self.readiness = readiness
        super().__init__("Production ML readiness gate did not pass.")


def _categorical_pipeline():
    return Pipeline(
        [
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
        ]
    )


def build_preprocessor(*, scale_numeric):
    numerical_steps = [("impute", SimpleImputer(strategy="median"))]
    if scale_numeric:
        numerical_steps.append(("scale", StandardScaler()))
    return ColumnTransformer(
        [
            ("numerical", Pipeline(numerical_steps), list(NUMERICAL_FEATURES)),
            ("categorical", _categorical_pipeline(), list(CATEGORICAL_FEATURES)),
        ],
        remainder="drop",
        verbose_feature_names_out=False,
    )


def build_candidate_pipelines(seed=RANDOM_SEED):
    tree_preprocessor = build_preprocessor(scale_numeric=False)
    return {
        "logistic_regression": Pipeline(
            [
                ("preprocess", build_preprocessor(scale_numeric=True)),
                ("model", LogisticRegression(max_iter=2000, random_state=seed)),
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
                ("model", RandomForestClassifier(random_state=seed, n_jobs=1)),
            ]
        ),
        "gradient_boosting": Pipeline(
            [
                ("preprocess", deepcopy(tree_preprocessor)),
                ("model", GradientBoostingClassifier(random_state=seed)),
            ]
        ),
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


def calculate_metrics(y_true, scores, threshold):
    y_true = np.asarray(y_true, dtype=int)
    scores = np.asarray(scores, dtype=float)
    predictions = (scores >= float(threshold)).astype(int)
    roc_auc = None
    if len(np.unique(y_true)) == 2:
        roc_auc = float(roc_auc_score(y_true, scores))
    return {
        "accuracy": float(accuracy_score(y_true, predictions)),
        "precision": float(precision_score(y_true, predictions, zero_division=0)),
        "recall": float(recall_score(y_true, predictions, zero_division=0)),
        "f1": float(f1_score(y_true, predictions, zero_division=0)),
        "average_precision": float(average_precision_score(y_true, scores)),
        "roc_auc": roc_auc,
        "confusion_matrix": confusion_matrix(y_true, predictions, labels=[0, 1]).tolist(),
    }


def choose_threshold(y_true, scores):
    candidates = np.linspace(0.20, 0.80, 25)
    evaluated = [(float(value), calculate_metrics(y_true, scores, value)) for value in candidates]
    best_recall = max(metrics["recall"] for _, metrics in evaluated)
    recall_candidates = [item for item in evaluated if item[1]["recall"] >= best_recall - 0.02]
    best_f1 = max(item[1]["f1"] for item in recall_candidates)
    f1_candidates = [item for item in recall_candidates if item[1]["f1"] >= best_f1 - 0.01]
    return max(
        f1_candidates,
        key=lambda item: (item[1]["average_precision"], item[1]["precision"], item[0]),
    )[0]


def _aggregate_metrics(per_fold):
    result = {"folds": per_fold}
    for metric in ("accuracy", "precision", "recall", "f1", "average_precision", "roc_auc"):
        values = [fold[metric] for fold in per_fold if fold.get(metric) is not None]
        result[f"mean_{metric}"] = float(np.mean(values)) if values else None
        result[f"std_{metric}"] = float(np.std(values, ddof=0)) if values else None
    return result


def _global_explanation(pipeline):
    preprocessor = pipeline.named_steps["preprocess"]
    model = pipeline.named_steps["model"]
    names = [str(name) for name in preprocessor.get_feature_names_out()]
    if hasattr(model, "coef_"):
        values = np.abs(np.asarray(model.coef_)[0])
        kind = "absolute_logistic_coefficient"
    elif hasattr(model, "feature_importances_"):
        values = np.asarray(model.feature_importances_)
        kind = "global_feature_importance"
    else:
        return {"kind": "unavailable", "features": []}
    ranked = sorted(zip(names, values), key=lambda item: float(item[1]), reverse=True)
    return {
        "kind": kind,
        "features": [{"feature": name, "importance": float(value)} for name, value in ranked[:12]],
    }


def train_and_evaluate_candidates(dataset_bundle):
    readiness = dataset_bundle["readiness"]
    if not readiness["ready"]:
        raise ProductionTrainingBlocked(readiness)

    observations = list(dataset_bundle["eligible_observations"])
    X = pd.DataFrame([row["features"] for row in observations], columns=list(EXPECTED_FEATURES))
    y = np.asarray([int(row["target"]) for row in observations], dtype=int)
    holdout = readiness["split_plan"]["final_holdout"]
    final_train_global = list(holdout["train_indices"])
    final_test_global = list(holdout["test_indices"])
    local_index = {global_index: local for local, global_index in enumerate(final_train_global)}
    cv_splits = [
        (
            np.asarray([local_index[index] for index in fold["train_indices"]], dtype=int),
            np.asarray([local_index[index] for index in fold["validation_indices"]], dtype=int),
        )
        for fold in readiness["split_plan"]["validation_folds"]
    ]
    X_train = X.iloc[final_train_global].reset_index(drop=True)
    y_train = y[final_train_global]
    X_test = X.iloc[final_test_global]
    y_test = y[final_test_global]

    candidates = {}
    for model_key, pipeline in build_candidate_pipelines().items():
        search = GridSearchCV(
            pipeline,
            HYPERPARAMETER_GRIDS[model_key],
            scoring="average_precision",
            cv=cv_splits,
            refit=True,
            n_jobs=1,
            error_score="raise",
        )
        search.fit(X_train, y_train)

        validation_truth = []
        validation_scores = []
        raw_fold_scores = []
        for train_indices, validation_indices in cv_splits:
            fold_model = clone(search.best_estimator_)
            fold_model.fit(X_train.iloc[train_indices], y_train[train_indices])
            scores = fold_model.predict_proba(X_train.iloc[validation_indices])[:, 1]
            validation_truth.extend(y_train[validation_indices].tolist())
            validation_scores.extend(scores.tolist())
            raw_fold_scores.append((y_train[validation_indices], scores))

        threshold = choose_threshold(validation_truth, validation_scores)
        fold_metrics = [calculate_metrics(truth, scores, threshold) for truth, scores in raw_fold_scores]
        validation = _aggregate_metrics(fold_metrics)
        final_scores = search.best_estimator_.predict_proba(X_test)[:, 1]
        final_evaluation = calculate_metrics(y_test, final_scores, threshold)
        candidates[model_key] = {
            "model_key": model_key,
            "model_name": MODEL_DISPLAY_NAMES[model_key],
            "pipeline": search.best_estimator_,
            "threshold": float(threshold),
            "best_parameters": search.best_params_,
            "validation": validation,
            "final_evaluation": final_evaluation,
            "explanation": _global_explanation(search.best_estimator_),
        }

    selection = select_production_model(candidates)
    return {
        "candidates": candidates,
        "selection": selection,
        "validation_metadata": {
            "strategy": "expanding temporal validation within earlier periods; latest completed target period held out",
            "validation_fold_count": len(cv_splits),
            "training_target_periods": [str(value) for value in holdout["training_periods"]],
            "final_evaluation_target_period": str(holdout["evaluation_period"]),
            "random_row_split_used": False,
            "threshold_selected_without_final_holdout": True,
        },
    }


def select_production_model(candidates):
    """Apply Recall -> F1 -> PR-AUC priorities and allow no robust winner."""

    results = list(candidates.values())
    best_recall = max(item["validation"]["mean_recall"] for item in results)
    recall_candidates = [
        item for item in results if item["validation"]["mean_recall"] >= best_recall - 0.02
    ]
    best_f1 = max(item["validation"]["mean_f1"] for item in recall_candidates)
    f1_candidates = [
        item for item in recall_candidates if item["validation"]["mean_f1"] >= best_f1 - 0.01
    ]
    ranked = sorted(
        f1_candidates,
        key=lambda item: (
            item["validation"]["mean_average_precision"],
            -(item["validation"]["std_recall"] or 0),
            item["model_key"],
        ),
        reverse=True,
    )
    selected = ranked[0]
    all_ranked = sorted(
        results,
        key=lambda item: (
            item["validation"]["mean_recall"],
            item["validation"]["mean_f1"],
            item["validation"]["mean_average_precision"],
        ),
        reverse=True,
    )
    runner_up = next((item for item in all_ranked if item["model_key"] != selected["model_key"]), None)
    recall_margin = (
        selected["validation"]["mean_recall"] - runner_up["validation"]["mean_recall"]
        if runner_up
        else 1.0
    )
    variability = selected["validation"]["std_recall"] or 0.0
    robust = len(f1_candidates) == 1 and recall_margin > max(0.02, variability)
    return {
        "status": "SELECTED" if robust else "NO_ROBUST_WINNER",
        "selected_model_key": selected["model_key"] if robust else None,
        "leading_candidate_key": selected["model_key"],
        "recall_margin_over_runner_up": float(recall_margin),
        "selected_recall_std": float(variability),
        "policy": "Retain models within 0.02 mean Recall, then within 0.01 mean F1; rank by mean PR-AUC and stability. Require a unique candidate whose Recall margin exceeds its fold variability.",
    }
