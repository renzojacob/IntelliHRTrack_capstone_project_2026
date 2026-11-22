"""Deterministic JSON and Markdown reporting for the Phase 2C experiment."""

from __future__ import annotations

import json
from pathlib import Path
from copy import deepcopy

from .experiment import MODEL_DISPLAY_NAMES
from .interpretation import add_scientific_interpretation


def canonical_json(results: dict) -> str:
    return json.dumps(results, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def _metric(value) -> str:
    return "n/a" if value is None else f"{float(value):.6f}"


def _comparison_table(models: dict) -> list[str]:
    lines = [
        "| Model | Recall mean±std | F1 mean±std | AP mean±std | Precision mean±std | Accuracy mean±std | ROC-AUC mean±std |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model_name in MODEL_DISPLAY_NAMES:
        aggregate = models[model_name]["aggregate"]
        cells = []
        for metric in ("recall", "f1", "average_precision", "precision", "accuracy", "roc_auc"):
            cells.append(
                f"{_metric(aggregate[f'mean_{metric}'])} ± {_metric(aggregate[f'std_{metric}'])}"
            )
        lines.append(f"| {MODEL_DISPLAY_NAMES[model_name]} | " + " | ".join(cells) + " |")
    return lines


def render_markdown(results: dict) -> str:
    results = add_scientific_interpretation(results)
    dataset = results["dataset"]
    primary = results["primary"]
    selection = results["selected_public_research_model"]
    sensitivity = results["duplicate_sensitivity"]
    selected_model = primary["models"][selection["model_key"]]

    lines = [
        "# Phase 2C: UCI Absenteeism Public Research Experiment",
        "",
        "## Objective",
        "",
        "Compare a prior-probability baseline with Logistic Regression, Decision Tree, Random Forest, and Gradient Boosting using employee-group-aware nested cross-validation.",
        "",
        "## Dataset",
        "",
        f"- Source SHA-256: `{dataset['source_sha256_before']}`",
        f"- Raw rows: {dataset['raw_rows']}",
        f"- Invalid-month exclusions: {dataset['invalid_month_rows_excluded']}",
        f"- Primary usable rows: {dataset['usable_rows']}",
        f"- Primary employee groups: {dataset['usable_employee_groups']}",
        f"- Positive class: {dataset['positive_rows']} ({dataset['positive_prevalence']:.6f})",
        f"- Negative class: {dataset['negative_rows']}",
        "- Exact duplicates: retained in the primary analysis",
        "",
        "## Target and predictors",
        "",
        f"Target: `{results['target']['name']}` — {results['target']['definition']}.",
        "",
        "Predictors:",
        "",
        *[f"- `{column}`" for column in results["predictors"]],
        "",
        "`ID` was used only for grouping. Raw absenteeism hours, reason, disciplinary failure, and every non-allowlisted field were excluded from the feature matrix.",
        "",
        "## Evaluation methodology",
        "",
        f"- Outer CV: {results['evaluation']['outer_cv']}",
        f"- Inner CV: {results['evaluation']['inner_cv']}",
        f"- Inner selection metric: {results['evaluation']['inner_scoring']}",
        f"- Random seed: {results['random_seed']}",
        "- Every fold was checked for zero employee-ID overlap.",
        "- All preprocessing and hyperparameter selection occurred inside training folds.",
        "",
        "## Fold verification",
        "",
        "| Fold | Train rows | Test rows | Train employees | Test employees | Train prevalence | Test prevalence | Group overlap |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for fold in primary["folds"]:
        lines.append(
            f"| {fold['fold']} | {fold['train_observations']} | {fold['test_observations']} | "
            f"{fold['train_employee_groups']} | {fold['test_employee_groups']} | "
            f"{fold['train_positive_prevalence']:.6f} | {fold['test_positive_prevalence']:.6f} | "
            f"{fold['group_overlap_count']} |"
        )

    lines.extend(
        [
            "",
            "## Model comparison",
            "",
            *_comparison_table(primary["models"]),
            "",
        "## Scientific interpretation",
            "",
        f"- Original preimplemented rule selection: **{results['original_rule_selected_model']['display_name']}**",
        f"- Primary recall leader: **{results['primary_recall_leader']['display_name']}**",
        f"- Strongest overall discrimination: **{results['strongest_overall_discrimination']['display_name']}**",
        f"- Exact-duplicate sensitivity winner: **{results['duplicate_sensitivity_winner']['display_name']}**",
        "- Robust single winner: **None**" if results["robust_single_winner"] is None else f"- Robust single winner: **{results['robust_single_winner']['display_name']}**",
        "",
        results["scientific_interpretation"],
        "",
        f"The original {selection['display_name']} selection is retained as the output of the preimplemented rule; it is not presented as proof of universal scientific superiority.",
        "",
        "## Pooled original-rule-selected-model performance",
        "",
        f"Original rule selected model: **{selection['display_name']}**.",
            "",
            f"Selection rule: {selection['rule']}",
            "",
            f"- Recall: {_metric(selected_model['pooled']['recall'])}",
            f"- Precision: {_metric(selected_model['pooled']['precision'])}",
            f"- F1: {_metric(selected_model['pooled']['f1'])}",
            f"- Average Precision: {_metric(selected_model['pooled']['average_precision'])}",
            f"- Accuracy: {_metric(selected_model['pooled']['accuracy'])}",
            f"- ROC-AUC: {_metric(selected_model['pooled']['roc_auc'])}",
            f"- Confusion matrix `[[TN, FP], [FN, TP]]`: `{selected_model['pooled']['confusion_matrix']}`",
            "",
            "## Duplicate sensitivity",
            "",
            f"- Deduplicated usable rows: {sensitivity['dataset']['usable_rows']}",
            f"- Deduplicated selected model: {sensitivity['selected_model']['display_name']}",
            f"- Material conclusion change: {str(sensitivity['material_conclusion_change']).lower()}",
            f"- Materiality rule: {sensitivity['materiality_rule']}",
            "- Differences for the primary selected model (deduplicated minus primary): "
            + ", ".join(
                f"{name}={value:+.6f}"
                for name, value in sensitivity["primary_selected_model_metric_differences"].items()
            ),
            "",
            "## Limitations",
            "",
            "- Genuine chronology cannot be reconstructed from the source CSV.",
            "- Employee group sizes are uneven.",
            "- Exact duplicate rows cannot be distinguished from genuine repeated events because exact dates are absent.",
            "- This is a small external research dataset; performance uncertainty across employee groups must be retained.",
            "",
            "## External-validity disclaimer",
            "",
            results["external_validity_disclaimer"],
            "",
            "This experiment is not connected to IntelliHRTrack payroll, DTR, attendance ingestion, production labels, or production predictions.",
            "",
        ]
    )
    return "\n".join(lines)


def write_research_artifacts(results: dict, output_directory: str | Path) -> dict:
    results = add_scientific_interpretation(results)
    directory = Path(output_directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "uci_absenteeism_phase2c_results.json"
    markdown_path = directory / "uci_absenteeism_phase2c_report.md"
    json_path.write_text(canonical_json(results), encoding="utf-8", newline="\n")
    markdown_path.write_text(render_markdown(results), encoding="utf-8", newline="\n")
    return {
        "json": str(json_path),
        "markdown": str(markdown_path),
    }


def regenerate_research_artifacts(
    results_json: str | Path,
    output_directory: str | Path | None = None,
) -> tuple[dict, dict]:
    """Regenerate interpretation/report artifacts without model fitting."""
    source_path = Path(results_json).expanduser().resolve()
    with source_path.open("r", encoding="utf-8") as source:
        original = json.load(source)

    measured_before = deepcopy(
        {
            "primary_models": original["primary"]["models"],
            "sensitivity_models": original["duplicate_sensitivity"]["evaluation"]["models"],
        }
    )
    interpreted = add_scientific_interpretation(original)
    measured_after = {
        "primary_models": interpreted["primary"]["models"],
        "sensitivity_models": interpreted["duplicate_sensitivity"]["evaluation"]["models"],
    }
    if measured_after != measured_before:
        raise AssertionError("Interpretation regeneration changed measured model results.")

    target_directory = (
        Path(output_directory).expanduser().resolve()
        if output_directory is not None
        else source_path.parent
    )
    artifacts = write_research_artifacts(interpreted, target_directory)
    return interpreted, artifacts
