from datetime import date
from types import SimpleNamespace

from django.test import SimpleTestCase

from core.analytics.quality import (
    CURRENT_PROFILE_CATEGORICAL_VALUES_NOT_HISTORICAL,
    IDENTIFIER_IN_PREDICTORS,
    INCOMPLETE_TARGET_PERIOD,
    INSUFFICIENT_FEATURE_COVERAGE,
    INSUFFICIENT_TARGET_COVERAGE,
    MLCoveragePolicy,
    NON_CHRONOLOGICAL_PAIR,
    NO_FEATURE_ATTENDANCE_COVERAGE,
    NO_TARGET_ATTENDANCE_COVERAGE,
    UNSUPPORTED_CADENCE,
    assess_dataset_eligibility,
    assess_ml_readiness,
    assess_observation_eligibility,
    assess_period_coverage,
    summarize_ml_dataset,
)


def period(pk, start, end, mode="MONTHLY"):
    return SimpleNamespace(
        pk=pk,
        id=pk,
        start_date=start,
        end_date=end,
        pay_mode=mode,
    )


def dtr(*statuses, covered_indexes=()):
    covered_indexes = set(covered_indexes)
    rows = [
        {
            "date": date(2026, 1, index + 1).isoformat(),
            "status": status,
            "raw_log_count": 2 if index in covered_indexes else 0,
        }
        for index, status in enumerate(statuses)
    ]
    return {
        "rows": rows,
        "records_used": sum(row["raw_log_count"] for row in rows),
    }


class MLEligibilityTests(SimpleTestCase):
    def setUp(self):
        self.feature_period = period(
            1, date(2026, 1, 1), date(2026, 1, 31)
        )
        self.target_period = period(
            2, date(2026, 2, 1), date(2026, 2, 28)
        )
        self.observation = {
            "employee_group_id": 17,
            "feature_period_id": 1,
            "feature_period_start": date(2026, 1, 1),
            "feature_period_end": date(2026, 1, 31),
            "target_period_id": 2,
            "target_period_start": date(2026, 2, 1),
            "target_period_end": date(2026, 2, 28),
            "features": {"previous_absences": 0},
            "target": 0,
        }
        self.covered_feature = dtr("Present", "Absent", covered_indexes={0})
        self.covered_target_negative = dtr("Present", covered_indexes={0})

    def assess(self, **overrides):
        kwargs = {
            "feature_period": self.feature_period,
            "target_period": self.target_period,
            "feature_dtr": self.covered_feature,
            "target_dtr": self.covered_target_negative,
            "as_of_date": date(2026, 3, 1),
        }
        kwargs.update(overrides)
        return assess_observation_eligibility(self.observation, **kwargs)

    def test_zero_feature_source_coverage_is_excluded(self):
        result = self.assess(feature_dtr=dtr("Absent", "Absent"))
        self.assertFalse(result["eligible"])
        self.assertIn(NO_FEATURE_ATTENDANCE_COVERAGE, result["exclusion_reasons"])

    def test_zero_target_source_coverage_is_excluded(self):
        result = self.assess(target_dtr=dtr("Absent", "Weekend"))
        self.assertFalse(result["eligible"])
        self.assertIn(NO_TARGET_ATTENDANCE_COVERAGE, result["exclusion_reasons"])

    def test_incomplete_target_period_is_excluded(self):
        result = self.assess(as_of_date=date(2026, 2, 10))
        self.assertFalse(result["target_period_complete"])
        self.assertIn(INCOMPLETE_TARGET_PERIOD, result["exclusion_reasons"])

    def test_missing_as_of_boundary_is_conservatively_incomplete(self):
        result = self.assess(as_of_date=None)
        self.assertIn(INCOMPLETE_TARGET_PERIOD, result["exclusion_reasons"])

    def test_unsupported_cadence_is_excluded(self):
        unsupported = period(
            1, date(2026, 1, 1), date(2026, 1, 15), "KINSENAS"
        )
        result = self.assess(feature_period=unsupported)
        self.assertIn(UNSUPPORTED_CADENCE, result["exclusion_reasons"])

    def test_non_chronological_pair_is_excluded(self):
        overlapping = period(
            2, date(2026, 1, 15), date(2026, 2, 15), "MONTHLY"
        )
        result = self.assess(target_period=overlapping)
        self.assertIn(NON_CHRONOLOGICAL_PAIR, result["exclusion_reasons"])

    def test_approved_leave_does_not_create_positive_label(self):
        result = self.assess(
            target_dtr=dtr("Present", "Approved Leave", covered_indexes={0})
        )
        self.assertTrue(result["eligible"])

    def test_official_business_and_travel_do_not_create_positive_label(self):
        for status in ("Official Business", "Official Travel"):
            with self.subTest(status=status):
                result = self.assess(
                    target_dtr=dtr("Present", status, covered_indexes={0})
                )
                self.assertTrue(result["eligible"])

    def test_holiday_suspension_does_not_create_positive_label(self):
        result = self.assess(
            target_dtr=dtr("Present", "Holiday/Suspension", covered_indexes={0})
        )
        self.assertTrue(result["eligible"])

    def test_legitimate_absence_in_covered_period_remains_valid_positive(self):
        positive_observation = {**self.observation, "target": 1}
        target_dtr = dtr("Present", "Absent", covered_indexes={0})
        result = assess_observation_eligibility(
            positive_observation,
            feature_period=self.feature_period,
            target_period=self.target_period,
            feature_dtr=self.covered_feature,
            target_dtr=target_dtr,
            as_of_date=date(2026, 3, 1),
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["exclusion_reasons"], [])

    def test_covered_negative_remains_target_zero_and_eligible(self):
        result = self.assess()
        self.assertTrue(result["eligible"])
        self.assertEqual(self.observation["target"], 0)

    def test_multiple_exclusion_reasons_are_reported(self):
        result = self.assess(
            feature_dtr=dtr("Absent"),
            target_dtr=dtr("Absent"),
            as_of_date=date(2026, 2, 1),
        )
        self.assertEqual(
            set(result["exclusion_reasons"]),
            {
                NO_FEATURE_ATTENDANCE_COVERAGE,
                NO_TARGET_ATTENDANCE_COVERAGE,
                INCOMPLETE_TARGET_PERIOD,
                # Observation says zero while deterministic target is positive.
                "INVALID_TARGET_LABEL",
            },
        )

    def test_optional_ratio_policy_reports_insufficient_coverage(self):
        result = self.assess(
            feature_dtr=dtr("Present", "Absent", covered_indexes={0}),
            target_dtr=dtr("Present", "Absent", covered_indexes={0}),
            policy=MLCoveragePolicy(minimum_coverage_ratio=0.75),
        )
        self.assertIn(INSUFFICIENT_FEATURE_COVERAGE, result["exclusion_reasons"])
        self.assertIn(INSUFFICIENT_TARGET_COVERAGE, result["exclusion_reasons"])

    def test_future_minimum_day_policy_uses_insufficient_reason(self):
        result = self.assess(
            policy=MLCoveragePolicy(minimum_attendance_days=2)
        )
        self.assertNotIn(
            NO_FEATURE_ATTENDANCE_COVERAGE, result["exclusion_reasons"]
        )
        self.assertNotIn(
            NO_TARGET_ATTENDANCE_COVERAGE, result["exclusion_reasons"]
        )
        self.assertIn(INSUFFICIENT_FEATURE_COVERAGE, result["exclusion_reasons"])
        self.assertIn(INSUFFICIENT_TARGET_COVERAGE, result["exclusion_reasons"])

    def test_period_coverage_reports_aggregate_evidence(self):
        coverage = assess_period_coverage(
            dtr("Present", "Absent", "Weekend", covered_indexes={0})
        )
        self.assertEqual(coverage["applicable_working_days"], 2)
        self.assertEqual(coverage["days_with_usable_attendance_evidence"], 1)
        self.assertEqual(coverage["days_without_usable_attendance_evidence"], 1)
        self.assertEqual(coverage["coverage_ratio"], 0.5)

    def test_identifier_in_predictors_is_excluded(self):
        observation = {
            **self.observation,
            "features": {"previous_absences": 0, "employee_id": 17},
        }
        result = assess_observation_eligibility(
            observation,
            feature_period=self.feature_period,
            target_period=self.target_period,
            feature_dtr=self.covered_feature,
            target_dtr=self.covered_target_negative,
            as_of_date=date(2026, 3, 1),
        )
        self.assertIn(IDENTIFIER_IN_PREDICTORS, result["exclusion_reasons"])

    def test_current_profile_dimensions_are_reported_as_limitation(self):
        result = self.assess()
        self.assertIn(
            CURRENT_PROFILE_CATEGORICAL_VALUES_NOT_HISTORICAL,
            result["known_limitations"],
        )


class MLDatasetSummaryAndReadinessTests(SimpleTestCase):
    def eligible_row(self, target, target_period_id=2, target_start=date(2026, 2, 1)):
        return {
            "observation": {
                "employee_group_id": target + 1,
                "feature_period_id": target_period_id - 1,
                "target_period_id": target_period_id,
                "target_period_start": target_start,
                "target": target,
                "features": {"previous_absences": 0},
            },
            "ml_eligibility": {"eligible": True, "exclusion_reasons": []},
        }

    def test_dataset_assessment_and_summary_count_raw_vs_eligible(self):
        feature = period(1, date(2026, 1, 1), date(2026, 1, 31))
        target = period(2, date(2026, 2, 1), date(2026, 2, 28))
        base = {
            "employee_group_id": 1,
            "feature_period_id": 1,
            "target_period_id": 2,
            "target_period_start": date(2026, 2, 1),
            "features": {"previous_absences": 0},
            "target": 0,
        }
        records = [
            {
                "observation": base,
                "feature_period": feature,
                "target_period": target,
                "feature_dtr": dtr("Present", covered_indexes={0}),
                "target_dtr": dtr("Present", covered_indexes={0}),
            },
            {
                "observation": {**base, "employee_group_id": 2},
                "feature_period": feature,
                "target_period": target,
                "feature_dtr": dtr("Absent"),
                "target_dtr": dtr("Present", covered_indexes={0}),
            },
        ]
        assessed = assess_dataset_eligibility(
            records, as_of_date=date(2026, 3, 1)
        )
        summary = summarize_ml_dataset(assessed)
        self.assertEqual(summary["raw_observations"], 2)
        self.assertEqual(summary["eligible_observations"], 1)
        self.assertEqual(summary["excluded_observations"], 1)
        self.assertEqual(summary["exclusion_counts"], {NO_FEATURE_ATTENDANCE_COVERAGE: 1})
        self.assertEqual(summary["eligible_target_0"], 1)

    def test_readiness_is_insufficient_with_no_eligible_observations(self):
        assessed = [
            {
                "observation": {"target": 1},
                "ml_eligibility": {
                    "eligible": False,
                    "exclusion_reasons": [NO_TARGET_ATTENDANCE_COVERAGE],
                },
            }
        ]
        self.assertEqual(
            assess_ml_readiness(assessed)["status"], "INSUFFICIENT_DATA"
        )

    def test_readiness_is_not_ready_with_only_one_target_class(self):
        assessed = [
            self.eligible_row(1, 2, date(2026, 2, 1)),
            self.eligible_row(1, 3, date(2026, 3, 1)),
        ]
        self.assertEqual(
            assess_ml_readiness(assessed)["status"], "INSUFFICIENT_DATA"
        )

    def test_readiness_is_not_ready_without_temporal_holdout(self):
        assessed = [self.eligible_row(0), self.eligible_row(1)]
        result = assess_ml_readiness(assessed)
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertFalse(result["chronological_holdout_possible"])

