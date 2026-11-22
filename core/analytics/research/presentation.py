"""Read-only presentation adapter for the committed Phase 2C research artifact.

This module deliberately has no dependency on the experiment runner, scikit-learn,
or production models.  It validates and reshapes already-measured public-dataset
results for the staff research page; it never trains or scores a model.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from django.conf import settings


ARTIFACT_RELATIVE_PATH = Path(
    "research_results/phase2c_uci_absenteeism/uci_absenteeism_phase2c_results.json"
)
MODEL_ORDER = (
    "baseline",
    "logistic_regression",
    "decision_tree",
    "random_forest",
    "gradient_boosting",
)
METRICS = (
    ("recall", "Recall"),
    ("f1", "F1"),
    ("average_precision", "Average precision"),
    ("precision", "Precision"),
    ("accuracy", "Accuracy"),
    ("roc_auc", "ROC AUC"),
)


class ResearchArtifactError(Exception):
    """Raised when the committed research artifact cannot be presented safely."""


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResearchArtifactError(f"Invalid research artifact field: {field}")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchArtifactError(f"Invalid research artifact field: {field}")
    number = float(value)
    if not math.isfinite(number):
        raise ResearchArtifactError(f"Invalid research artifact field: {field}")
    return number


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchArtifactError(f"Invalid research artifact field: {field}")
    return value.strip()


def _named_model(value: Any, field: str) -> dict[str, str]:
    item = _mapping(value, field)
    return {
        "model_key": _text(item.get("model_key"), f"{field}.model_key"),
        "display_name": _text(item.get("display_name"), f"{field}.display_name"),
    }


def load_research_evaluation(artifact_path: Path | str | None = None) -> dict[str, Any]:
    """Load validated display data from the completed Phase 2C JSON artifact."""

    path = Path(artifact_path) if artifact_path else Path(settings.BASE_DIR) / ARTIFACT_RELATIVE_PATH
    try:
        with path.open("r", encoding="utf-8") as artifact_file:
            raw = json.load(artifact_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchArtifactError("Research artifact is unavailable or malformed.") from exc

    root = _mapping(raw, "root")
    if root.get("research_only") is not True:
        raise ResearchArtifactError("Research-only designation is missing.")

    dataset = _mapping(root.get("dataset"), "dataset")
    evaluation = _mapping(root.get("evaluation"), "evaluation")
    primary = _mapping(root.get("primary"), "primary")
    source_models = _mapping(primary.get("models"), "primary.models")
    if set(source_models) != set(MODEL_ORDER):
        raise ResearchArtifactError("Research artifact model set is invalid.")

    model_rows = []
    for model_key in MODEL_ORDER:
        model = _mapping(source_models.get(model_key), f"primary.models.{model_key}")
        aggregate = _mapping(model.get("aggregate"), f"primary.models.{model_key}.aggregate")
        metric_values = []
        for metric_key, metric_label in METRICS:
            mean = _number(aggregate.get(f"mean_{metric_key}"), f"{model_key}.mean_{metric_key}")
            std = _number(aggregate.get(f"std_{metric_key}"), f"{model_key}.std_{metric_key}")
            metric_values.append(
                {
                    "key": metric_key,
                    "label": metric_label,
                    "mean": mean,
                    "std": std,
                    "display": f"{mean:.3f} ± {std:.3f}",
                    "percent": max(0.0, min(100.0, mean * 100.0)),
                }
            )
        model_rows.append(
            {
                "key": model_key,
                "display_name": _text(model.get("display_name"), f"{model_key}.display_name"),
                "metrics": metric_values,
                "chart_metrics": metric_values[:3],
            }
        )

    sensitivity = _mapping(root.get("duplicate_sensitivity"), "duplicate_sensitivity")
    differences = _mapping(
        sensitivity.get("primary_selected_model_metric_differences"),
        "duplicate_sensitivity.primary_selected_model_metric_differences",
    )
    robust_winner = root.get("robust_single_winner")
    if robust_winner is not None:
        robust_winner = _named_model(robust_winner, "robust_single_winner")

    predictors = root.get("predictors")
    exclusions = root.get("exclusions")
    if not isinstance(predictors, list) or not all(isinstance(item, str) for item in predictors):
        raise ResearchArtifactError("Research artifact predictors are invalid.")
    if not isinstance(exclusions, dict) or not all(
        isinstance(key, str) and isinstance(value, (str, int, float, bool, type(None)))
        for key, value in exclusions.items()
    ):
        raise ResearchArtifactError("Research artifact exclusions are invalid.")

    return {
        "experiment_name": _text(root.get("experiment_name"), "experiment_name"),
        "schema_version": root.get("schema_version"),
        "random_seed": root.get("random_seed"),
        "dataset": {
            "source_file": _text(dataset.get("source_file"), "dataset.source_file"),
            "raw_rows": int(_number(dataset.get("raw_rows"), "dataset.raw_rows")),
            "usable_rows": int(_number(dataset.get("usable_rows"), "dataset.usable_rows")),
            "usable_employee_groups": int(
                _number(dataset.get("usable_employee_groups"), "dataset.usable_employee_groups")
            ),
            "positive_rows": int(_number(dataset.get("positive_rows"), "dataset.positive_rows")),
            "negative_rows": int(_number(dataset.get("negative_rows"), "dataset.negative_rows")),
            "positive_prevalence": _number(
                dataset.get("positive_prevalence"), "dataset.positive_prevalence"
            ),
            "positive_prevalence_percent": _number(
                dataset.get("positive_prevalence"), "dataset.positive_prevalence"
            ) * 100.0,
            "target_definition": _text(dataset.get("target_definition"), "dataset.target_definition"),
            "source_sha256_after": _text(
                dataset.get("source_sha256_after"), "dataset.source_sha256_after"
            ),
        },
        "evaluation": {
            "groups": _text(evaluation.get("groups"), "evaluation.groups"),
            "outer_cv": _text(evaluation.get("outer_cv"), "evaluation.outer_cv"),
            "inner_cv": _text(evaluation.get("inner_cv"), "evaluation.inner_cv"),
            "inner_scoring": _text(evaluation.get("inner_scoring"), "evaluation.inner_scoring"),
            "metric_std": _text(evaluation.get("metric_std"), "evaluation.metric_std"),
        },
        "models": model_rows,
        "chart": {
            "labels": [model["display_name"] for model in model_rows],
            "datasets": [
                {
                    "label": metric_label,
                    "data": [
                        next(metric["mean"] for metric in model["metrics"] if metric["key"] == metric_key)
                        for model in model_rows
                    ],
                }
                for metric_key, metric_label in METRICS[:3]
            ],
        },
        "original_rule_selected_model": _named_model(
            root.get("original_rule_selected_model"), "original_rule_selected_model"
        ),
        "primary_recall_leader": _named_model(root.get("primary_recall_leader"), "primary_recall_leader"),
        "strongest_overall_discrimination": _named_model(
            root.get("strongest_overall_discrimination"), "strongest_overall_discrimination"
        ),
        "duplicate_sensitivity_winner": _named_model(
            root.get("duplicate_sensitivity_winner"), "duplicate_sensitivity_winner"
        ),
        "robust_single_winner": robust_winner,
        "scientific_interpretation": _text(
            root.get("scientific_interpretation"), "scientific_interpretation"
        ),
        "external_validity_disclaimer": _text(
            root.get("external_validity_disclaimer"), "external_validity_disclaimer"
        ),
        "sensitivity": {
            "material_conclusion_change": sensitivity.get("material_conclusion_change") is True,
            "materiality_rule": _text(sensitivity.get("materiality_rule"), "sensitivity.materiality_rule"),
            "mean_recall": _number(differences.get("mean_recall"), "sensitivity.mean_recall"),
            "mean_f1": _number(differences.get("mean_f1"), "sensitivity.mean_f1"),
            "mean_average_precision": _number(
                differences.get("mean_average_precision"), "sensitivity.mean_average_precision"
            ),
        },
        "predictors": predictors,
        "exclusions": [
            {"label": key.replace("_", " ").capitalize(), "value": value}
            for key, value in exclusions.items()
        ],
    }
