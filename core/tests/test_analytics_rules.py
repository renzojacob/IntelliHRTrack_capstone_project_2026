from django.test import SimpleTestCase

from core.analytics.rules import assess_data_quality, calculate_attendance_risk


class AttendanceRiskRuleTests(SimpleTestCase):
    def test_score_is_bounded_and_explained(self):
        result = calculate_attendance_risk(
            absences=50,
            late_days=50,
            incomplete_dtr_records=50,
            monday_friday_absences=50,
            holiday_adjacent_absences=50,
        )
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["category"], "High")
        self.assertTrue(result["reasons"])

    def test_baseline_weights_remain_explainable(self):
        result = calculate_attendance_risk(
            absences=1,
            late_days=2,
            incomplete_dtr_records=1,
            monday_friday_absences=1,
            holiday_adjacent_absences=1,
        )
        self.assertEqual(result["score"], 42)
        self.assertEqual(result["category"], "Medium")
        self.assertEqual(len(result["reasons"]), 5)

    def test_no_absence_indicators_including_leave_means_zero_risk(self):
        # Approved leave is intentionally not an input to the risk function.
        result = calculate_attendance_risk()
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["category"], "Low")
        self.assertTrue(result["reasons"])


class DataQualityTests(SimpleTestCase):
    def test_data_quality_is_a_separate_result_from_risk(self):
        risk = calculate_attendance_risk()
        quality = assess_data_quality(
            total_applicable_days=10,
            incomplete_dtr_records=5,
            usable_attendance_records=0,
            unavailable_features=["example_feature"],
            historical_period_count=1,
        )
        self.assertEqual(set(risk), {"score", "category", "reasons"})
        self.assertEqual(set(quality), {"score", "status", "reasons"})
        self.assertEqual(risk["score"], 0)
        self.assertLess(quality["score"], 100)
        self.assertIn(quality["status"], {"Good", "Review", "Poor"})
