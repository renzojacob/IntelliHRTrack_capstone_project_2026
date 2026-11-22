import ast
import inspect
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.template.loader import render_to_string
from django.urls import reverse

from core import research_views
from core.analytics.research import presentation
from core.analytics.research.presentation import ResearchArtifactError, load_research_evaluation
from core.tests import factories as F


class ResearchArtifactPresentationTests(TestCase):
    def test_committed_artifact_loads_all_expected_models_and_metrics(self):
        result = load_research_evaluation()

        self.assertEqual(
            [model["display_name"] for model in result["models"]],
            [
                "Dummy prior baseline",
                "Logistic Regression",
                "Decision Tree",
                "Random Forest",
                "Gradient Boosting",
            ],
        )
        self.assertTrue(all(len(model["metrics"]) == 6 for model in result["models"]))
        self.assertEqual(result["original_rule_selected_model"]["display_name"], "Random Forest")
        self.assertIsNone(result["robust_single_winner"])

    def test_missing_artifact_raises_safe_domain_error(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-results.json"
            with self.assertRaises(ResearchArtifactError):
                load_research_evaluation(missing)

    def test_malformed_artifact_raises_safe_domain_error(self):
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "results.json"
            malformed.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(ResearchArtifactError):
                load_research_evaluation(malformed)

    def test_presentation_layer_is_isolated_from_training_and_production_models(self):
        imported_modules = set()
        for module in (presentation, research_views):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.add(node.module)

        self.assertFalse(any(name == "core.models" for name in imported_modules))
        self.assertFalse(any("experiment" in name for name in imported_modules))
        self.assertFalse(any(name.startswith("sklearn") for name in imported_modules))


class MLResearchEvaluationPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.branch = F.branch("Research UI Test Branch")
        cls.staff = F.admin("research_staff", cls.branch)
        cls.employee = User.objects.create_user("ordinary_employee", password="x")
        cls.superuser = User.objects.create_superuser("research_superuser", password="x")

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("admin_ml_research"))
        self.assertRedirects(
            response,
            f"{reverse('login_ui')}?next={reverse('admin_ml_research')}",
            fetch_redirect_response=False,
        )

    def test_non_staff_user_receives_forbidden(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("admin_ml_research"))
        self.assertEqual(response.status_code, 403)

    def test_normal_staff_user_receives_forbidden(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("admin_ml_research"))
        self.assertEqual(response.status_code, 403)

    def test_superuser_can_open_research_page(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin_ml_research"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/ml_research_evaluation.html")

    def test_page_has_prominent_research_and_production_warnings(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin_ml_research"))

        self.assertContains(response, "Research-only page")
        self.assertContains(
            response,
            "Results use an external public dataset and are not validated for IntelliHRTrack employees",
        )
        self.assertContains(response, "No public-data model is deployed to IntelliHRTrack")
        self.assertContains(response, "must not be used for payroll")
        self.assertContains(response, "does not represent IntelliHRTrack employees")

    def test_page_displays_every_model_and_required_interpretation(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin_ml_research"))

        for model_name in (
            "Dummy prior baseline",
            "Logistic Regression",
            "Decision Tree",
            "Random Forest",
            "Gradient Boosting",
        ):
            self.assertContains(response, model_name)
        self.assertContains(response, "Original preimplemented selection-rule result")
        self.assertContains(response, "Recall leader")
        self.assertContains(response, "Strongest overall discrimination")
        self.assertContains(response, "Duplicate-sensitivity winner")
        self.assertContains(response, "None established")
        research = response.context["research"]
        self.assertEqual(research["primary_recall_leader"]["display_name"], "Random Forest")
        self.assertEqual(
            research["strongest_overall_discrimination"]["display_name"],
            "Logistic Regression",
        )
        self.assertEqual(research["duplicate_sensitivity_winner"]["display_name"], "Decision Tree")
        self.assertIsNone(research["robust_single_winner"])

    def test_page_displays_artifact_metrics_not_template_constants(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin_ml_research"))

        result = load_research_evaluation()
        random_forest = next(model for model in result["models"] if model["key"] == "random_forest")
        recall = next(metric for metric in random_forest["metrics"] if metric["key"] == "recall")
        self.assertContains(response, recall["display"])

    @patch("core.research_views.load_research_evaluation", side_effect=ResearchArtifactError("private path"))
    def test_missing_or_bad_artifact_fails_without_exposing_details(self, mocked_loader):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin_ml_research"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Research results unavailable")
        self.assertContains(response, "No production attendance or payroll function is affected")
        self.assertNotContains(response, "private path")
        mocked_loader.assert_called_once_with()

    def test_page_has_no_employee_records_or_training_controls(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin_ml_research"))
        html = response.content.decode("utf-8").lower()

        self.assertNotIn("run_uci_absenteeism_experiment", html)
        self.assertNotIn("train model", html)
        self.assertNotIn("employee name", html)
        self.assertNotIn("biometric identifier", html)
        self.assertNotIn("salary", html)

    def test_page_does_not_query_production_hr_tables(self):
        self.client.force_login(self.superuser)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("admin_ml_research"))

        self.assertEqual(response.status_code, 200)
        sql = " ".join(query["sql"].lower() for query in queries.captured_queries)
        for table in (
            "core_attendancerecord",
            "core_userprofile",
            "core_branch",
            "core_payrollperiod",
            "core_payrollbatch",
            "core_payrollitem",
            "core_finalizeddtr",
        ):
            self.assertNotIn(table, sql)

    @patch("core.analytics.research.experiment.run_phase2c_experiment")
    def test_page_request_does_not_run_training(self, mocked_experiment):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin_ml_research"))

        self.assertEqual(response.status_code, 200)
        mocked_experiment.assert_not_called()

    def test_metric_definitions_heading_is_rendered_once(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse("admin_ml_research"))
        self.assertContains(response, "<h2 id=\"definitions-title\">Metric definitions</h2>", count=1)

    def test_normal_administrator_sidebar_has_no_research_link(self):
        request = RequestFactory().get("/admin-ui/")
        request.user = self.staff
        html = render_to_string("admin/_sidebar.html", {"request": request, "current": "dashboard"})

        self.assertNotIn(reverse("admin_ml_research"), html)
        self.assertNotIn("Research Model Evaluation", html)

    def test_non_staff_sidebar_has_no_research_link(self):
        request = RequestFactory().get("/employee-ui/")
        request.user = self.employee
        html = render_to_string("admin/_sidebar.html", {"request": request, "current": "dashboard"})

        self.assertNotIn(reverse("admin_ml_research"), html)
        self.assertNotIn("Research Model Evaluation", html)

    def test_operational_analytics_does_not_advertise_or_render_uci_research(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("admin_analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("admin_ml_research"))
        for research_term in (
            "Research Model Evaluation",
            "UCI Absenteeism",
            "Dummy prior baseline",
            "Logistic Regression",
            "Random Forest",
            "duplicate-sensitivity winner",
        ):
            self.assertNotContains(response, research_term)

    def test_operational_analytics_remains_rule_based_for_normal_staff(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("admin_analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rule-based scores computed from recorded attendance, leave, and payroll")

