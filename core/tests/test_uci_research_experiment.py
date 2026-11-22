from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from core.analytics.research.experiment import (
    MODEL_DISPLAY_NAMES,
    build_model_pipelines,
    build_preprocessor,
    calculate_metrics,
    make_outer_folds,
    run_phase2c_experiment,
)
from core.analytics.research.interpretation import add_scientific_interpretation
from core.analytics.research.reporting import (
    canonical_json,
    regenerate_research_artifacts,
    render_markdown,
)
from core.analytics.research.uci_absenteeism import (
    EXPECTED_COLUMNS,
    FEATURE_COLUMNS,
    FORBIDDEN_FEATURES,
    TARGET_NAME,
    assert_feature_allowlist,
    construct_target,
    load_uci_absenteeism,
    sha256_file,
)


class UCIResearchExperimentTests(SimpleTestCase):
    def measured_interpretation_fixture(self):
        measured = {
            "baseline": ("Dummy prior baseline", 0.0, 0.0, 0.368942, 0.5),
            "logistic_regression": (
                "Logistic Regression",
                0.541420,
                0.459423,
                0.533053,
                0.642113,
            ),
            "decision_tree": ("Decision Tree", 0.323370, 0.373113, 0.425899, 0.536997),
            "random_forest": ("Random Forest", 0.566920, 0.449987, 0.444415, 0.547277),
            "gradient_boosting": (
                "Gradient Boosting",
                0.355030,
                0.353344,
                0.468219,
                0.599201,
            ),
        }
        models = {}
        for key, (display, recall, f1, average_precision, roc_auc) in measured.items():
            models[key] = {
                "display_name": display,
                "aggregate": {
                    "mean_recall": recall,
                    "std_recall": 0.1,
                    "mean_f1": f1,
                    "std_f1": 0.1,
                    "mean_average_precision": average_precision,
                    "std_average_precision": 0.1,
                    "mean_precision": f1,
                    "std_precision": 0.1,
                    "mean_accuracy": 0.6,
                    "std_accuracy": 0.1,
                    "mean_roc_auc": roc_auc,
                    "std_roc_auc": 0.1,
                },
                "pooled": {
                    "recall": recall,
                    "precision": f1,
                    "f1": f1,
                    "average_precision": average_precision,
                    "accuracy": 0.6,
                    "roc_auc": roc_auc,
                    "confusion_matrix": [[10, 2], [3, 8]],
                },
            }
        return {
            "dataset": {
                "source_path": "C:\\Users\\private\\local_data\\synthetic.csv",
                "source_sha256_before": "SYNTHETIC",
                "raw_rows": 40,
                "invalid_month_rows_excluded": 0,
                "usable_rows": 40,
                "usable_employee_groups": 10,
                "positive_rows": 20,
                "negative_rows": 20,
                "positive_prevalence": 0.5,
            },
            "target": {"name": TARGET_NAME, "definition": "synthetic boundary"},
            "predictors": list(FEATURE_COLUMNS),
            "random_seed": 20261122,
            "evaluation": {
                "outer_cv": "synthetic grouped outer CV",
                "inner_cv": "synthetic grouped inner CV",
                "inner_scoring": "average_precision",
            },
            "primary": {"folds": [], "models": models},
            "selected_public_research_model": {
                "model_key": "random_forest",
                "display_name": "Random Forest",
                "rule": "original synthetic selection rule",
            },
            "duplicate_sensitivity": {
                "dataset": {
                    "source_path": "C:\\Users\\private\\local_data\\synthetic.csv",
                    "usable_rows": 36,
                },
                "evaluation": {"models": deepcopy(models)},
                "selected_model": {
                    "model_key": "decision_tree",
                    "display_name": "Decision Tree",
                },
                "material_conclusion_change": True,
                "materiality_rule": "synthetic materiality rule",
                "primary_selected_model_metric_differences": {
                    "mean_recall": -0.14,
                    "mean_f1": -0.03,
                    "mean_average_precision": 0.02,
                },
            },
            "external_validity_disclaimer": "Synthetic external-validity disclaimer.",
        }

    def synthetic_frame(self, *, groups=10, rows_per_group=4):
        rows = []
        outcomes = (0, 7, 8, 16)
        for group_id in range(1, groups + 1):
            for offset in range(rows_per_group):
                values = {
                    "ID": group_id,
                    "Reason for absence": 0,
                    "Month of absence": (group_id % 12) + 1,
                    "Day of the week": (offset % 5) + 2,
                    "Seasons": (group_id % 4) + 1,
                    "Transportation expense": 100 + group_id,
                    "Distance from Residence to Work": 5 + group_id,
                    "Service time": 1 + (group_id % 20),
                    "Age": 25 + group_id,
                    "Work load Average/day ": 250.0,
                    "Hit target": 95,
                    "Disciplinary failure": 0,
                    "Education": 1,
                    "Son": 0,
                    "Social drinker": 0,
                    "Social smoker": 0,
                    "Pet": 0,
                    "Weight": 70,
                    "Height": 170,
                    "Body mass index": 24,
                    "Absenteeism time in hours": outcomes[offset % len(outcomes)],
                }
                rows.append(values)
        return pd.DataFrame(rows, columns=EXPECTED_COLUMNS)

    def write_source(self, directory: str, frame=None) -> Path:
        path = Path(directory) / "synthetic_absenteeism.csv"
        (frame if frame is not None else self.synthetic_frame()).to_csv(
            path,
            sep=";",
            index=False,
            encoding="utf-8",
        )
        return path

    def test_semicolon_source_parsing(self):
        with TemporaryDirectory() as directory:
            dataset = load_uci_absenteeism(self.write_source(directory))
        self.assertEqual(dataset.metadata["delimiter"], ";")
        self.assertEqual(dataset.metadata["raw_rows"], 40)
        self.assertEqual(tuple(dataset.X.columns), FEATURE_COLUMNS)

    def test_invalid_month_zero_is_excluded_exactly(self):
        frame = self.synthetic_frame()
        frame.loc[0, "Month of absence"] = 0
        with TemporaryDirectory() as directory:
            dataset = load_uci_absenteeism(self.write_source(directory, frame))
        self.assertEqual(dataset.metadata["invalid_month_rows_excluded"], 1)
        self.assertEqual(dataset.metadata["usable_rows"], 39)
        self.assertNotIn(0, dataset.X["Month of absence"].tolist())

    def test_target_construction_boundary(self):
        target = construct_target(pd.Series([0, 7, 8, 16]))
        self.assertEqual(target.name, TARGET_NAME)
        self.assertEqual(target.tolist(), [0, 0, 1, 1])

    def test_feature_matrix_is_exact_allowlist(self):
        frame = pd.DataFrame([[1, 2, 100, 10, 5]], columns=FEATURE_COLUMNS)
        assert_feature_allowlist(frame)
        self.assertEqual(tuple(frame.columns), FEATURE_COLUMNS)

    def test_id_is_excluded_from_predictors(self):
        self.assertIn("ID", FORBIDDEN_FEATURES)
        leaked = pd.DataFrame(columns=FEATURE_COLUMNS + ("ID",))
        with self.assertRaises(AssertionError):
            assert_feature_allowlist(leaked)

    def test_raw_hours_are_excluded_from_predictors(self):
        self.assertIn("Absenteeism time in hours", FORBIDDEN_FEATURES)
        self.assertNotIn("Absenteeism time in hours", FEATURE_COLUMNS)

    def test_reason_is_excluded_from_predictors(self):
        self.assertIn("Reason for absence", FORBIDDEN_FEATURES)
        self.assertNotIn("Reason for absence", FEATURE_COLUMNS)

    def test_disciplinary_failure_is_excluded_from_predictors(self):
        self.assertIn("Disciplinary failure", FORBIDDEN_FEATURES)
        self.assertNotIn("Disciplinary failure", FEATURE_COLUMNS)

    def test_group_splits_have_zero_employee_overlap(self):
        with TemporaryDirectory() as directory:
            dataset = load_uci_absenteeism(self.write_source(directory))
            folds = make_outer_folds(dataset)
        for fold in folds:
            train_groups = set(dataset.groups.iloc[fold["train_indices"]])
            test_groups = set(dataset.groups.iloc[fold["test_indices"]])
            self.assertFalse(train_groups.intersection(test_groups))
            self.assertTrue(fold["summary"]["zero_group_overlap"])

    def test_fold_generation_is_deterministic(self):
        with TemporaryDirectory() as directory:
            dataset = load_uci_absenteeism(self.write_source(directory))
            first = make_outer_folds(dataset)
            second = make_outer_folds(dataset)
        self.assertEqual(
            [fold["summary"]["test_group_ids"] for fold in first],
            [fold["summary"]["test_group_ids"] for fold in second],
        )

    def test_preprocessing_fits_successfully(self):
        frame = self.synthetic_frame()
        X = frame.loc[:, FEATURE_COLUMNS]
        transformed = build_preprocessor(scale_numeric=True).fit_transform(X)
        self.assertEqual(transformed.shape[0], len(frame))
        self.assertGreater(transformed.shape[1], len(FEATURE_COLUMNS))

    def test_all_expected_models_fit_synthetic_grouped_fixture(self):
        frame = self.synthetic_frame()
        X = frame.loc[:, FEATURE_COLUMNS]
        y = construct_target(frame["Absenteeism time in hours"])
        pipelines = build_model_pipelines()
        self.assertEqual(set(pipelines), set(MODEL_DISPLAY_NAMES))
        for pipeline in pipelines.values():
            pipeline.fit(X, y)
            predictions = pipeline.predict(X)
            self.assertEqual(len(predictions), len(frame))

    def test_metrics_and_json_schema_are_deterministic(self):
        y = np.asarray([0, 0, 1, 1])
        predictions = np.asarray([0, 1, 1, 1])
        scores = np.asarray([0.1, 0.6, 0.7, 0.9])
        first = calculate_metrics(y, predictions, scores)
        second = calculate_metrics(y, predictions, scores)
        self.assertEqual(first, second)
        self.assertEqual(first["confusion_matrix"], [[1, 1], [0, 2]])
        self.assertEqual(canonical_json({"metrics": first}), canonical_json({"metrics": second}))

    def test_experiment_orchestration_does_not_modify_source(self):
        fake_models = {}
        for index, name in enumerate(MODEL_DISPLAY_NAMES):
            value = 0.4 + (index * 0.01)
            fake_models[name] = {
                "display_name": MODEL_DISPLAY_NAMES[name],
                "aggregate": {
                    "mean_recall": value,
                    "std_recall": 0.01,
                    "mean_f1": value,
                    "std_f1": 0.01,
                    "mean_average_precision": value,
                    "std_average_precision": 0.01,
                    "mean_roc_auc": value,
                }
            }
        fake_evaluation = {
            "folds": [],
            "fixed_test_groups": [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]],
            "models": fake_models,
        }
        with TemporaryDirectory() as directory:
            path = self.write_source(directory)
            before = sha256_file(path)
            with patch(
                "core.analytics.research.experiment.evaluate_bundle",
                return_value=fake_evaluation,
            ):
                results = run_phase2c_experiment(path)
            after = sha256_file(path)
        self.assertEqual(before, after)
        self.assertTrue(results["source_unchanged"])

    def test_interpretation_fields_preserve_original_and_sensitivity_winners(self):
        interpreted = add_scientific_interpretation(self.measured_interpretation_fixture())
        self.assertEqual(interpreted["original_rule_selected_model"]["model_key"], "random_forest")
        self.assertEqual(interpreted["duplicate_sensitivity_winner"]["model_key"], "decision_tree")
        self.assertIn("selection_stability", interpreted)
        self.assertIn("scientific_interpretation", interpreted)

    def test_random_forest_is_primary_recall_leader(self):
        interpreted = add_scientific_interpretation(self.measured_interpretation_fixture())
        self.assertEqual(interpreted["primary_recall_leader"]["model_key"], "random_forest")

    def test_logistic_regression_is_strongest_overall_discrimination(self):
        interpreted = add_scientific_interpretation(self.measured_interpretation_fixture())
        self.assertEqual(
            interpreted["strongest_overall_discrimination"]["model_key"],
            "logistic_regression",
        )

    def test_material_sensitivity_change_means_no_robust_single_winner(self):
        interpreted = add_scientific_interpretation(self.measured_interpretation_fixture())
        self.assertIsNone(interpreted["robust_single_winner"])
        self.assertEqual(
            interpreted["selection_stability"]["status"],
            "UNSTABLE_ACROSS_DUPLICATE_HANDLING",
        )

    def test_utf8_markdown_has_correct_symbols_without_mojibake(self):
        markdown = render_markdown(self.measured_interpretation_fixture())
        self.assertIn("\u2014", markdown)
        self.assertIn("\u00b1", markdown)
        self.assertNotIn("\u00c3", markdown)
        self.assertNotIn("\u00c2", markdown)

    def test_interpretation_does_not_change_raw_metrics(self):
        source = self.measured_interpretation_fixture()
        primary_before = deepcopy(source["primary"]["models"])
        sensitivity_before = deepcopy(source["duplicate_sensitivity"]["evaluation"]["models"])
        interpreted = add_scientific_interpretation(source)
        self.assertEqual(interpreted["primary"]["models"], primary_before)
        self.assertEqual(
            interpreted["duplicate_sensitivity"]["evaluation"]["models"],
            sensitivity_before,
        )

    def test_json_regeneration_preserves_metrics_and_writes_utf8(self):
        source = self.measured_interpretation_fixture()
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "uci_absenteeism_phase2c_results.json"
            source_path.write_text(canonical_json(source), encoding="utf-8")
            interpreted, artifacts = regenerate_research_artifacts(source_path)
            regenerated = json.loads(Path(artifacts["json"]).read_text(encoding="utf-8"))
            markdown = Path(artifacts["markdown"]).read_text(encoding="utf-8")
        self.assertEqual(regenerated["primary"]["models"], source["primary"]["models"])
        self.assertEqual(regenerated["robust_single_winner"], None)
        self.assertNotIn("source_path", regenerated["dataset"])
        self.assertEqual(regenerated["dataset"]["source_file"], "synthetic.csv")
        self.assertNotIn("source_path", regenerated["duplicate_sensitivity"]["dataset"])
        self.assertEqual(interpreted["primary_recall_leader"]["model_key"], "random_forest")
        self.assertIn("\u2014", markdown)
        self.assertIn("\u00b1", markdown)
