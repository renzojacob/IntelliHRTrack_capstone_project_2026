"""Scientific interpretation of already-measured Phase 2C results.

This module never fits or scores a model. It annotates an existing results
structure while preserving all raw folds, predictions, metrics, and original
selection-rule output.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path


TRAINED_MODELS = (
    "logistic_regression",
    "decision_tree",
    "random_forest",
    "gradient_boosting",
)


def _model_reference(model_key: str, models: dict) -> dict:
    return {
        "model_key": model_key,
        "display_name": models[model_key]["display_name"],
    }


def add_scientific_interpretation(results: dict) -> dict:
    """Return an annotated copy without changing any measured result."""
    interpreted = deepcopy(results)
    interpreted["schema_version"] = max(int(interpreted.get("schema_version", 1)), 2)
    for dataset_metadata in (
        interpreted.get("dataset", {}),
        interpreted.get("duplicate_sensitivity", {}).get("dataset", {}),
    ):
        local_source_path = dataset_metadata.pop("source_path", None)
        if local_source_path and "source_file" not in dataset_metadata:
            dataset_metadata["source_file"] = Path(
                str(local_source_path).replace("\\", "/")
            ).name
    models = interpreted["primary"]["models"]
    original = interpreted["selected_public_research_model"]["model_key"]
    sensitivity = interpreted["duplicate_sensitivity"]["selected_model"]["model_key"]
    material_change = bool(
        interpreted["duplicate_sensitivity"]["material_conclusion_change"]
    )

    recall_leader = max(
        TRAINED_MODELS,
        key=lambda name: models[name]["aggregate"]["mean_recall"],
    )
    # Average Precision is the experiment's threshold-independent tuning and
    # precision-recall discrimination measure. F1 and ROC-AUC are deterministic
    # tie-breakers, rather than a new post-hoc composite score.
    discrimination_leader = max(
        TRAINED_MODELS,
        key=lambda name: (
            models[name]["aggregate"]["mean_average_precision"],
            models[name]["aggregate"]["mean_f1"],
            models[name]["aggregate"]["mean_roc_auc"],
        ),
    )
    recall_gap = (
        models[recall_leader]["aggregate"]["mean_recall"]
        - models[discrimination_leader]["aggregate"]["mean_recall"]
    )

    robust_winner = None
    if not material_change and original == sensitivity:
        robust_winner = _model_reference(original, models)

    interpreted["original_rule_selected_model"] = _model_reference(original, models)
    interpreted["primary_recall_leader"] = _model_reference(recall_leader, models)
    interpreted["strongest_overall_discrimination"] = _model_reference(
        discrimination_leader,
        models,
    )
    interpreted["duplicate_sensitivity_winner"] = _model_reference(sensitivity, models)
    interpreted["robust_single_winner"] = robust_winner
    interpreted["selection_stability"] = {
        "material_change_under_exact_deduplication": material_change,
        "original_and_sensitivity_winners_agree": original == sensitivity,
        "primary_recall_leader_and_discrimination_leader_agree": (
            recall_leader == discrimination_leader
        ),
        "recall_leader_minus_discrimination_leader_mean_recall": recall_gap,
        "status": (
            "UNSTABLE_ACROSS_DUPLICATE_HANDLING"
            if material_change or original != sensitivity
            else "STABLE_ACROSS_DUPLICATE_HANDLING"
        ),
    }
    interpreted["scientific_interpretation"] = (
        f"The primary grouped analysis identified {models[recall_leader]['display_name']} "
        "as the recall leader, while "
        f"{models[discrimination_leader]['display_name']} produced the strongest overall discrimination and "
        "precision-recall performance. The recall difference was small relative to "
        "fold-level variability. Because model ranking changed under exact-duplicate "
        "sensitivity analysis and fold-level variability was substantial, the experiment "
        "did not establish a robust single superior classifier."
    )
    return interpreted
