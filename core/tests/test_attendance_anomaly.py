from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.analytics.production_ml import anomaly
from core.analytics.production_ml.artifacts import MLArtifactError, load_registry_artifact
from core.analytics.production_ml.registry import activate_validated_model
from core.models import AttendanceRecord, FinalizedDTR, LeaveRequest, MLModelRegistry, PayrollItem, PayrollRule, UserProfile
from core.tests import factories as F
from django.core.management import call_command
from io import StringIO


class WeeklyAnomalyTests(SimpleTestCase):
    def observations(self):
        rows = []
        for week in range(4):
            start = date(2025, 1, 6) + timedelta(weeks=week)
            for employee in range(3):
                values = dict.fromkeys(anomaly.FEATURES, 0)
                values.update(late_minutes=employee + week, absences=employee)
                rows.append(dict(employee=employee, start=start, end=start + timedelta(days=6),
                                 quality="PARTIAL", features=values))
        return rows

    def test_holdout_scores_come_from_fitted_forest(self):
        rows = self.observations()
        model, metadata = anomaly.fit(rows)
        self.assertEqual(metadata["readiness"]["training_observations"], 9)
        self.assertEqual(metadata["diagnostics"]["later"]["count"], 3)
        bundle = dict(pipeline=model, training_metadata=metadata)
        registry = SimpleNamespace(model_version="fixture", trained_at=timezone.now())
        result = anomaly.score(rows[-1], bundle, registry)
        self.assertEqual(result["ml_advisory_score"], round(-model.score_samples(anomaly.frame(rows[-1:]))[0], 4))
        self.assertFalse(anomaly.score(rows[0], bundle, registry)["available"])
        self.assertFalse(set(anomaly.INPUTS) & anomaly.CONTEXT)

    def test_duplicates_and_empty_training_block_training(self):
        rows = self.observations()
        self.assertFalse(anomaly.assess([])[0]["ready"])
        self.assertIn("DUPLICATE_EMPLOYEE_WEEK", anomaly.assess(rows + rows[:1])[0]["reasons"])

    def test_source_classification(self):
        verified = dict(combined_device_event=True, dual_biometric_verified=True,
                        verification_methods=["face", "fingerprint"])
        workbook = dict(importer="import_client_august_attendance_v1", source="August-1-31-2026.xlsx")
        for raw, branch, expected in (
            (verified, "Client", "VERIFIED_DEVICE"),
            (workbook, "Client", "AUDITED_WORKBOOK"),
            (dict(verified, demo_marker="demo"), "Client", "GENERATED_SOURCE"),
            (verified, "Occidental Mindoro - Payroll Verification", "FICTIONAL_DEFENSE"),
            (dict(verified, dual_biometric_verified=False), "Client", "REJECTED_DEVICE"),
            ({}, "Client", "UNKNOWN_SOURCE"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(anomaly.provenance(AttendanceRecord(raw_row=raw), branch), expected)

    def test_dtr_aggregation_preserves_protected_statuses_and_rejects_outside_rows(self):
        start = date(2025, 1, 6)
        profile = SimpleNamespace(pk=1, branch=object())
        statuses = ["Absent", "Present", "Incomplete", "Approved Leave", "Official Travel", "Holiday/Suspension", "Weekend"]
        days = [
            dict(date=start + timedelta(days=i), status=status, total_minutes={1:480, 2:240}.get(i, 0),
                 raw_log_count=int(i in (1, 2)), late=15 if i == 1 else 0, undertime=20 if i == 1 else 0)
            for i, status in enumerate(statuses)
        ]
        dtr = dict(rows=days, leave_days=1, travel_days=1, holiday_days=1)
        with patch.object(anomaly, "_default_dtr_builder", return_value=dtr):
            row = anomaly.observation(profile, start, object())
            expected = dict(attendance_rate=50., absences=1, late_days=1, late_minutes=15, undertime_days=1,
                            undertime_minutes=20, incomplete_dtr_records=1, average_worked_hours=4.,
                            worked_hours_stddev=2., consecutive_absence_count=1, monday_friday_absence_count=1,
                            approved_leave_days=1., travel_days=1, holiday_suspension_days=1)
            self.assertEqual(row["quality"], "PARTIAL")
            for key, value in expected.items():
                self.assertEqual(row["features"][key], value, key)
            self.assertGreater(row["features"]["rule_based_risk_score"], 0)
            days.append(dict(days[0], date=start+timedelta(days=7)))
            self.assertEqual(anomaly.observation(profile, start, object())["quality"], "INVALID_WINDOW")
        self.assertEqual(anomaly.observation(profile, start, None)["quality"], "MISSING_BRANCH_RULES")

    def test_seed_boundary_and_explanation(self):
        rows = self.observations()
        model, meta = anomaly.fit(rows)
        second, _ = anomaly.fit(rows)
        self.assertEqual(model[-1].random_state, anomaly.RANDOM_SEED)
        self.assertEqual(model[-1].contamination, "auto")
        self.assertEqual(model.score_samples(anomaly.frame(rows)).tolist(),
                         second.score_samples(anomaly.frame(rows)).tolist())
        registry = SimpleNamespace(model_version="fixture", trained_at=timezone.now())
        fake = SimpleNamespace(decision_function=lambda _: [-.1], score_samples=lambda _: [-.6])
        bundle = dict(pipeline=fake, training_metadata=dict(meta, width=.05))
        row = dict(rows[-1], features=dict(rows[-1]["features"], late_minutes=99))
        result = anomaly.score(row, bundle, registry)
        self.assertEqual(result["category"], "Unusual attendance pattern")
        self.assertTrue(any("late minutes" in item for item in result["explanation"]))
        self.assertNotIn("probability", result)
        fake.decision_function = lambda _: [0.01]
        fake.score_samples = lambda _: [-.49]
        self.assertEqual(anomaly.score(row, bundle, registry)["category"], "Review recommended")
        fake.decision_function = lambda _: [.1]
        fake.score_samples = lambda _: [-.4]
        normal = anomaly.score(row, bundle, registry)
        self.assertEqual(normal["category"], "Normal attendance pattern")
        self.assertGreater(result["ml_advisory_score"], normal["ml_advisory_score"])
        for row in rows:
            row["features"] = dict.fromkeys(anomaly.FEATURES, 0)
        self.assertIn("INSUFFICIENT_MULTIVARIATE_VARIATION", anomaly.assess(rows)[0]["reasons"])


class AnomalyIntegrationTests(TestCase):
    def test_verified_punches_feed_real_dtr_while_protected_days_remain_distinct(self):
        branch = F.branch("Operational device coverage")
        employee = F.employee("device_employee", branch)
        start = date(2025, 1, 6)
        F.non_working(start + timedelta(days=2), br=branch, kind="holiday")
        for day in (start, start + timedelta(days=1)):
            for value, kind in (("08:00", "in"), ("12:00", "out"), ("13:00", "in"), ("17:00", "out")):
                punch = F.punch(employee, day, value, kind)
                punch.raw_row.update(combined_device_event=True, dual_biometric_verified=True,
                                     verification_methods=["face", "fingerprint"])
                punch.save(update_fields=["raw_row"])
        [row] = anomaly.collect([employee], cutoff=start + timedelta(days=7))
        self.assertEqual(row["quality"], "PARTIAL")
        self.assertEqual(row["source_coverage"]["days_with_usable_attendance_evidence"], 2)
        self.assertEqual(row["features"]["holiday_suspension_days"], 1)
        self.assertEqual(row["features"]["absences"], 2)

    def test_collector_uses_completed_real_record_windows(self):
        branch = F.branch("Window coverage")
        PayrollRule.objects.filter(branch=branch).delete()
        profile = SimpleNamespace(pk=7, branch=branch, branch_id=branch.pk, biometric_employee_id="test-key")
        start = date(2025, 1, 6)
        for offset in (0, 6, 7, 14):
            AttendanceRecord.objects.create(
                branch=branch, employee_id="test-key",
                timestamp=timezone.make_aware(datetime.combine(start+timedelta(days=offset), time(10))),
                raw_row=dict(dual_biometric_verified=True, verification_methods=["face", "fingerprint"]),
            )
        rows = anomaly.collect([profile], cutoff=start+timedelta(days=10))
        self.assertEqual([(r["start"], r["end"], r["quality"]) for r in rows],
                         [(start, start+timedelta(days=6), "MISSING_BRANCH_RULES")])
        self.assertEqual(rows[0]["provenance"], {"VERIFIED_DEVICE": 2})

    def test_fitted_registry_artifact_and_dashboard(self):
        from core.tests.test_production_ml import ProductionArtifactRegistryTests
        from core.analytics.production_ml.artifacts import save_model_artifact

        rows = WeeklyAnomalyTests().observations()
        branch = F.branch("Anomaly UI")
        admin = F.admin("anomaly_admin", branch)
        employee = F.employee("anomaly_employee", branch)
        scoring_row = dict(rows[-1], employee=employee.pk)
        artifact_helper = ProductionArtifactRegistryTests()
        with TemporaryDirectory() as root, override_settings(ML_ARTIFACT_ROOT=root):
            saved = save_model_artifact(candidate=artifact_helper.fitted_candidate(), model_version="supervised",
                                        training_metadata={})
            supervised = artifact_helper.registry(saved, "supervised")
            activate_validated_model(supervised.pk)
            with patch.object(anomaly, "collect", return_value=rows):
                call_command("train_attendance_anomaly_model", stdout=StringIO())
            registry = MLModelRegistry.objects.get(purpose=anomaly.PURPOSE)
            load_registry_artifact(registry)
            for field, value in (("purpose", supervised.purpose), ("feature_schema_version", "wrong")):
                mismatched = MLModelRegistry.objects.get(pk=registry.pk)
                setattr(mismatched, field, value)
                with self.assertRaises(MLArtifactError):
                    load_registry_artifact(mismatched)
            activate_validated_model(registry.pk)
            supervised.refresh_from_db()
            self.assertEqual(supervised.status, "ACTIVE")
            saved = save_model_artifact(candidate=artifact_helper.fitted_candidate(), model_version="supervised-next",
                                        training_metadata={})
            next_supervised = artifact_helper.registry(saved, "supervised-next")
            activate_validated_model(next_supervised.pk)
            registry.refresh_from_db()
            self.assertEqual(registry.status, "ACTIVE")
            path = Path(root) / registry.artifact_relative_path
            content = path.read_bytes()
            path.write_bytes(content+b"corrupt")
            with self.assertRaises(MLArtifactError):
                load_registry_artifact(registry)
            path.write_bytes(content)
            url = reverse("admin_analytics")
            self.client.force_login(employee.user)
            self.assertNotEqual(self.client.get(url).status_code, 200)
            self.client.force_login(admin)
            protected_models = (AttendanceRecord, FinalizedDTR, PayrollItem, UserProfile, LeaveRequest)
            before = {model: list(model.objects.order_by("pk").values()) for model in protected_models}
            with patch.object(anomaly, "collect", return_value=[scoring_row]):
                response = self.client.get(url)
            self.assertContains(response, registry.model_version)
            self.assertContains(response, "ML results are advisory")
            result = anomaly.score(scoring_row, load_registry_artifact(registry), registry)
            self.assertContains(response, result["category"])
            self.assertEqual(response.context["anomaly"]["rows"][0]["ml_advisory_score"],
                             result["ml_advisory_score"])
            self.assertEqual(before, {model: list(model.objects.order_by("pk").values()) for model in protected_models})

    def test_missing_anomaly_model_is_safe(self):
        from core.analytics.production_ml.service import get_employee_ml_analytics

        result = get_employee_ml_analytics([], as_of_date=date(2025, 2, 1))
        self.assertFalse(result["anomaly"]["active"])
        self.assertIn("No active attendance anomaly model", result["anomaly"]["message"])
