import json
import inspect
import tempfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.analytics.features import FEATURE_NAMES
from core.analytics.production_ml.artifacts import (
    MLArtifactError,
    load_registry_artifact,
    save_model_artifact,
)
from core.analytics.production_ml.constants import (
    DATASET_VERSION,
    FEATURE_SCHEMA_VERSION,
    RANDOM_SEED,
    RISK_BANDS,
    TARGET_DEFINITION,
)
from core.analytics.production_ml.dataset import build_production_ml_dataset, private_dataset_document
from core.analytics.production_ml.prediction import predict_absenteeism_risk, probability_risk_level
from core.analytics.production_ml.readiness import (
    INSUFFICIENT_TARGET_PERIODS,
    SINGLE_TARGET_CLASS,
    temporal_split_plan,
)
from core.analytics.production_ml.registry import MLActivationError, activate_validated_model
from core.analytics.production_ml.training import (
    build_candidate_pipelines,
    calculate_metrics,
    select_production_model,
)
from core.analytics.production_ml import training as production_training
from core.models import MLLifecycleEvent, MLModelRegistry
from core.tests import factories as F


def period(pk, month):
    starts = {1: date(2026, 1, 1), 2: date(2026, 2, 1), 3: date(2026, 3, 1), 4: date(2026, 4, 1)}
    ends = {1: date(2026, 1, 31), 2: date(2026, 2, 28), 3: date(2026, 3, 31), 4: date(2026, 4, 30)}
    return SimpleNamespace(pk=pk, id=pk, start_date=starts[month], end_date=ends[month], pay_mode="MONTHLY")


def profile(pk, department="Operations"):
    return SimpleNamespace(pk=pk, id=pk, branch=object(), employment_type="COS", department=department)


def synthetic_dtr_builder(employee, branch, selected_period, rules):
    status = "Absent" if employee.pk % 2 else "Present"
    return {
        "rows": [
            {
                "date": selected_period.start_date.isoformat(),
                "status": status,
                "raw_log_count": 2,
                "total_minutes": 480 if status == "Present" else 0,
            }
        ],
        "records_used": 2,
    }


class ProductionDatasetAndReadinessTests(SimpleTestCase):
    def build(self, profiles=None, periods=None):
        return build_production_ml_dataset(
            as_of_timestamp=timezone.make_aware(datetime(2026, 5, 1)),
            profiles=profiles or [profile(1), profile(2)],
            periods=periods or [period(i, i) for i in range(1, 5)],
            rules_resolver=lambda employee: object(),
            dtr_builder=synthetic_dtr_builder,
        )

    def test_adjacent_period_dataset_is_temporal_and_ready(self):
        bundle = self.build()
        self.assertEqual(bundle["metadata"]["candidate_observation_count"], 6)
        self.assertEqual(bundle["metadata"]["eligible_observation_count"], 6)
        self.assertTrue(bundle["readiness"]["ready"])
        self.assertTrue(
            all(
                row["feature_period_end"] < row["target_period_start"]
                for row in bundle["eligible_observations"]
            )
        )

    def test_exact_feature_allowlist_and_no_identifiers(self):
        bundle = self.build()
        for row in bundle["eligible_observations"]:
            self.assertEqual(set(row["features"]), set(FEATURE_NAMES))
            self.assertNotIn("employee_group_id", row["features"])
            self.assertNotIn("salary", row["features"])
            self.assertNotIn("biometric_employee_id", row["features"])

    def test_private_export_pseudonymizes_employee_group(self):
        document = private_dataset_document(self.build())
        self.assertNotIn("employee_group_id", json.dumps(document))
        self.assertTrue(all(len(row["employee_group"]) == 64 for row in document["observations"]))

    def test_insufficient_periods_block_readiness_with_code(self):
        bundle = self.build(periods=[period(1, 1), period(2, 2)])
        self.assertFalse(bundle["readiness"]["ready"])
        self.assertIn(INSUFFICIENT_TARGET_PERIODS, bundle["readiness"]["reason_codes"])

    def test_single_class_blocks_readiness(self):
        bundle = self.build(profiles=[profile(2), profile(4)])
        self.assertFalse(bundle["readiness"]["ready"])
        self.assertIn(SINGLE_TARGET_CLASS, bundle["readiness"]["reason_codes"])

    def test_temporal_plan_never_places_future_period_in_training(self):
        eligible = self.build()["eligible_observations"]
        plan = temporal_split_plan(eligible)
        for fold in plan["validation_folds"]:
            self.assertTrue(all(period_id < fold["validation_period"] for period_id in fold["training_periods"]))
        self.assertTrue(
            all(
                period_id < plan["final_holdout"]["evaluation_period"]
                for period_id in plan["final_holdout"]["training_periods"]
            )
        )


class ProductionModelPipelineTests(SimpleTestCase):
    def fixture(self):
        rows = []
        targets = []
        for index in range(12):
            row = {name: float(index % 4) for name in FEATURE_NAMES if name not in {"employment_type", "department"}}
            row["employment_type"] = "COS" if index % 2 else "JO"
            row["department"] = "Field" if index % 3 else "Operations"
            rows.append(row)
            targets.append(index % 2)
        return pd.DataFrame(rows, columns=list(FEATURE_NAMES)), np.asarray(targets)

    def test_all_four_candidate_pipelines_fit_and_return_probabilities(self):
        X, y = self.fixture()
        pipelines = build_candidate_pipelines()
        self.assertEqual(
            set(pipelines),
            {"logistic_regression", "decision_tree", "random_forest", "gradient_boosting"},
        )
        for key, pipeline in pipelines.items():
            with self.subTest(model=key):
                pipeline.fit(X, y)
                probabilities = pipeline.predict_proba(X)[:, 1]
                self.assertTrue(np.all(probabilities >= 0))
                self.assertTrue(np.all(probabilities <= 1))

    def test_metrics_and_confusion_matrix_are_calculated(self):
        result = calculate_metrics([0, 0, 1, 1], [0.1, 0.8, 0.7, 0.9], 0.5)
        self.assertEqual(result["confusion_matrix"], [[1, 1], [0, 2]])
        self.assertEqual(result["recall"], 1.0)
        self.assertGreater(result["average_precision"], 0)

    def test_selection_can_return_no_robust_winner_deterministically(self):
        def candidate(key, recall, f1, ap, std):
            return {
                "model_key": key,
                "validation": {
                    "mean_recall": recall,
                    "mean_f1": f1,
                    "mean_average_precision": ap,
                    "std_recall": std,
                },
            }

        candidates = {
            "a": candidate("a", 0.70, 0.60, 0.65, 0.08),
            "b": candidate("b", 0.69, 0.61, 0.66, 0.07),
        }
        first = select_production_model(candidates)
        second = select_production_model(candidates)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "NO_ROBUST_WINNER")
        self.assertIsNone(first["selected_model_key"])

    def test_probability_risk_bands_are_distinct_from_model_threshold(self):
        self.assertEqual(probability_risk_level(0.2, RISK_BANDS), "Low")
        self.assertEqual(probability_risk_level(0.5, RISK_BANDS), "Medium")
        self.assertEqual(probability_risk_level(0.8, RISK_BANDS), "High")

    def test_training_module_has_no_random_row_split_fallback(self):
        source = inspect.getsource(production_training)
        self.assertNotIn("train_test_split", source)
        self.assertIn("expanding temporal validation", source)


class ProductionArtifactRegistryTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.override = override_settings(ML_ARTIFACT_ROOT=self.temporary.name)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        self.temporary.cleanup()

    def fitted_candidate(self, key="logistic_regression"):
        X, y = ProductionModelPipelineTests().fixture()
        pipeline = build_candidate_pipelines()[key]
        pipeline.fit(X, y)
        return {
            "model_key": key,
            "model_name": "Logistic Regression",
            "pipeline": pipeline,
            "threshold": 0.5,
            "explanation": {"kind": "test", "features": []},
        }

    def registry(self, saved, version, status=MLModelRegistry.STATUS_VALIDATED):
        return MLModelRegistry.objects.create(
            model_key="logistic_regression",
            model_name="Logistic Regression",
            model_version=version,
            status=status,
            artifact_relative_path=saved["relative_path"],
            artifact_sha256=saved["sha256"],
            dataset_version=DATASET_VERSION,
            dataset_generated_at=timezone.now(),
            trained_at=timezone.now(),
            training_row_count=12,
            employee_group_count=4,
            positive_count=6,
            negative_count=6,
            target_definition=TARGET_DEFINITION,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            random_seed=RANDOM_SEED,
            selected_threshold=0.5,
            validation_metadata={
                "readiness": {"ready": True},
                "selection": {"selected_model_key": "logistic_regression"},
            },
        )

    def test_save_load_and_checksum_validation(self):
        saved = save_model_artifact(candidate=self.fitted_candidate(), model_version="v1", training_metadata={})
        registry = self.registry(saved, "v1")
        payload = load_registry_artifact(registry)
        self.assertEqual(payload["model_version"], "v1")

        path = Path(self.temporary.name) / saved["relative_path"]
        path.write_bytes(path.read_bytes() + b"corrupt")
        with self.assertRaises(MLArtifactError):
            load_registry_artifact(registry)

    def test_missing_artifact_is_rejected_safely(self):
        saved = save_model_artifact(candidate=self.fitted_candidate(), model_version="missing", training_metadata={})
        registry = self.registry(saved, "missing")
        (Path(self.temporary.name) / saved["relative_path"]).unlink()
        with self.assertRaises(MLArtifactError):
            load_registry_artifact(registry)

    def test_activation_retires_previous_active_model(self):
        old_saved = save_model_artifact(candidate=self.fitted_candidate(), model_version="old", training_metadata={})
        old = self.registry(old_saved, "old", status=MLModelRegistry.STATUS_ACTIVE)
        new_saved = save_model_artifact(candidate=self.fitted_candidate(), model_version="new", training_metadata={})
        new = self.registry(new_saved, "new")

        activated = activate_validated_model(new.pk, actor_label="unit-test")
        old.refresh_from_db()
        self.assertEqual(activated.status, MLModelRegistry.STATUS_ACTIVE)
        self.assertEqual(old.status, MLModelRegistry.STATUS_RETIRED)
        self.assertEqual(MLModelRegistry.objects.filter(status=MLModelRegistry.STATUS_ACTIVE).count(), 1)
        self.assertTrue(MLLifecycleEvent.objects.filter(event_type="MODEL_RETIRED").exists())

    def test_activation_refuses_inactive_or_unvalidated_entry(self):
        saved = save_model_artifact(candidate=self.fitted_candidate(), model_version="candidate", training_metadata={})
        registry = self.registry(saved, "candidate", status=MLModelRegistry.STATUS_CANDIDATE)
        with self.assertRaises(MLActivationError):
            activate_validated_model(registry.pk)


class FixedProbabilityPipeline:
    def predict_proba(self, frame):
        return np.asarray([[0.25, 0.75] for _ in range(len(frame))])


class ProductionPredictionTests(SimpleTestCase):
    def test_structured_advisory_prediction_uses_injected_active_artifact(self):
        registry = SimpleNamespace(
            status=MLModelRegistry.STATUS_ACTIVE,
            model_name="Logistic Regression",
            model_version="test-v1",
        )
        artifact = {
            "pipeline": FixedProbabilityPipeline(),
            "threshold": 0.6,
            "risk_bands": RISK_BANDS,
            "explanation": {"kind": "global_feature_importance", "features": []},
        }
        result = predict_absenteeism_risk(
            profile(2),
            as_of_date=date(2026, 2, 28),
            registry=registry,
            artifact=artifact,
            periods=[period(1, 1), period(2, 2)],
            rules=object(),
            dtr_builder=synthetic_dtr_builder,
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["predicted_class"], 1)
        self.assertEqual(result["probability"], 0.75)
        self.assertEqual(result["risk_level"], "High")
        self.assertEqual(result["model_version"], "test-v1")
        self.assertIn("does not automatically change", result["advisory_disclaimer"])

    def test_inactive_model_is_never_used(self):
        registry = SimpleNamespace(
            status=MLModelRegistry.STATUS_RETIRED,
            model_name="Retired",
            model_version="old",
        )
        result = predict_absenteeism_risk(
            profile(2),
            registry=registry,
            artifact={"pipeline": FixedProbabilityPipeline()},
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["status"], "MODEL_INACTIVE")

    def test_missing_completed_feature_period_is_safe(self):
        registry = SimpleNamespace(
            status=MLModelRegistry.STATUS_ACTIVE,
            model_name="Logistic Regression",
            model_version="test-v1",
        )
        artifact = {
            "pipeline": FixedProbabilityPipeline(),
            "threshold": 0.6,
            "risk_bands": RISK_BANDS,
            "explanation": {"features": []},
        }
        result = predict_absenteeism_risk(
            profile(2),
            registry=registry,
            artifact=artifact,
            periods=[],
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["status"], "INSUFFICIENT_CURRENT_FEATURE_DATA")


class ProductionMLAnalyticsUITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = F.branch("Production ML UI")
        cls.admin = F.admin("production_ml_admin", cls.branch)

    def test_workforce_analytics_renders_real_unavailable_state_and_rule_scores(self):
        MLLifecycleEvent.objects.create(
            event_type="READINESS_FAILED",
            succeeded=False,
            message="Production ML dataset assessment completed.",
            metadata={
                "readiness": {
                    "ready": False,
                    "reason_codes": [
                        "INSUFFICIENT_TARGET_PERIODS",
                        "INSUFFICIENT_TEMPORAL_VALIDATION_DATA",
                        "INSUFFICIENT_EMPLOYEE_GROUPS",
                    ],
                    "reason_messages": [
                        "Additional completed target periods are required.",
                        "Temporal validation data are insufficient.",
                        "Additional employee groups are required.",
                    ],
                    "represented_target_periods": 2,
                    "temporal_validation_folds": 0,
                    "final_holdout_available": False,
                    "summary": {
                        "raw_observations": 202,
                        "eligible_observations": 4,
                        "excluded_observations": 198,
                        "unique_eligible_employees": 4,
                        "eligible_target_1": 2,
                        "eligible_target_0": 2,
                    },
                }
            },
        )
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_analytics"))
        payload = json.loads(response.context["chart_payload_json"])
        ml = payload["production_ml"]
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ml["active"])
        self.assertEqual(ml["candidate_observations"], 202)
        self.assertEqual(ml["eligible_observations"], 4)
        self.assertEqual(ml["represented_target_periods"], 2)
        self.assertNotIn("validation_recall", ml)
        self.assertEqual(ml["predictions"], {})
        self.assertContains(response, "ML absenteeism risk — decision support only")
        self.assertContains(response, "Why ML is unavailable")
        self.assertContains(response, "Historical data coverage:")
        self.assertContains(
            response,
            "ML predictions are advisory only and do not automatically change attendance records, payroll, leave decisions, disciplinary actions, or employment status.",
        )
        self.assertContains(response, "Rule-based risk")
        self.assertContains(response, "ML unavailable")
        self.assertContains(response, "Rule-based scores computed")
        self.assertNotContains(response, "UCI Absenteeism")
        self.assertNotContains(response, "Dummy prior baseline")

    @patch("core.analytics.production_ml.dashboard.build_workforce_ml_payload")
    def test_active_model_payload_is_additive_and_advisory(self, mocked_payload):
        mocked_payload.return_value = {
            "active": True,
            "status": "ACTIVE",
            "model_name": "Logistic Regression",
            "model_version": "synthetic-v1",
            "training_row_count": 40,
            "validation_recall": 0.8,
            "validation_f1": 0.7,
            "validation_average_precision": 0.75,
            "risk_buckets": {"low": 1, "medium": 0, "high": 0},
            "average_probability": 0.2,
            "predictions": {},
            "advisory_disclaimer": "Decision support only; no operational records are changed.",
        }
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_analytics"))
        payload = json.loads(response.context["chart_payload_json"])
        self.assertTrue(payload["production_ml"]["active"])
        self.assertEqual(payload["production_ml"]["model_version"], "synthetic-v1")
        self.assertContains(response, "Rule-based scores computed")
