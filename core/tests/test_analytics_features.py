from datetime import date
from types import SimpleNamespace

from django.test import SimpleTestCase

from core.analytics.features import (
    FEATURE_NAMES,
    aggregate_dtr_features,
    build_employee_period_dataset,
    generate_employee_period_features,
    pair_chronological_periods,
)


def period(pk, start, end, mode):
    return SimpleNamespace(
        pk=pk,
        id=pk,
        start_date=start,
        end_date=end,
        pay_mode=mode,
    )


class EmployeePeriodFeatureTests(SimpleTestCase):
    def setUp(self):
        self.branch = object()
        self.profile = SimpleNamespace(
            pk=17,
            id=17,
            branch=self.branch,
            employment_type="COS",
            department="Operations",
        )

    def test_reusable_generator_reads_only_the_requested_period(self):
        selected_period = period(
            9, date(2026, 1, 1), date(2026, 1, 31), "MONTHLY"
        )
        calls = []

        def builder(profile, branch, requested_period, rules):
            calls.append(requested_period.pk)
            return {
                "rows": [
                    {
                        "date": "2026-01-05",
                        "status": "Present",
                        "late": 4,
                        "total_minutes": 480,
                    }
                ],
                "records_used": 2,
            }

        result = generate_employee_period_features(
            self.profile,
            selected_period,
            rules=object(),
            dtr_builder=builder,
        )
        self.assertEqual(calls, [9])
        self.assertEqual(result["features"]["previous_late_minutes"], 4)

    def test_basic_feature_generation_and_aggregations(self):
        dtr = {
            "rows": [
                {"date": "2026-01-05", "status": "Absent", "late": 0, "undertime": 0, "total_minutes": 0},
                {"date": "2026-01-06", "status": "Absent", "late": 0, "undertime": 0, "total_minutes": 0},
                {"date": "2026-01-07", "status": "Approved Leave", "late": 0, "undertime": 0, "total_minutes": 0},
                {"date": "2026-01-08", "status": "Present", "late": 10, "undertime": 20, "total_minutes": 450},
                {"date": "2026-01-09", "status": "Absent", "late": 0, "undertime": 0, "total_minutes": 0},
                {"date": "2026-01-10", "status": "Weekend", "late": 0, "undertime": 0, "total_minutes": 0},
                {"date": "2026-01-12", "status": "Official Travel", "late": 0, "undertime": 0, "total_minutes": 0},
                {"date": "2026-01-13", "status": "Official Business", "late": 0, "undertime": 0, "total_minutes": 0},
                {"date": "2026-01-14", "status": "Holiday/Suspension", "late": 0, "undertime": 0, "total_minutes": 0},
                {"date": "2026-01-15", "status": "Incomplete", "late": 5, "undertime": 15, "total_minutes": 120},
            ],
            "leave_days": 1.0,
            "travel_days": 1,
            "holiday_days": 1,
            "records_used": 6,
        }

        result = aggregate_dtr_features(self.profile, dtr)
        features = result["features"]

        self.assertEqual(set(features), set(FEATURE_NAMES))
        self.assertEqual(features["previous_absences"], 3)
        self.assertEqual(features["previous_late_days"], 2)
        self.assertEqual(features["previous_late_minutes"], 15)
        self.assertEqual(features["previous_undertime_days"], 2)
        self.assertEqual(features["previous_undertime_minutes"], 35)
        self.assertEqual(features["previous_incomplete_dtr_records"], 1)
        self.assertEqual(features["previous_consecutive_absence_count"], 2)
        self.assertEqual(features["previous_monday_friday_absence_count"], 2)
        self.assertEqual(features["previous_average_worked_hours"], 1.9)
        self.assertEqual(features["previous_approved_leave_days"], 1.0)
        self.assertEqual(features["previous_travel_days"], 1)
        self.assertEqual(features["previous_holiday_suspension_days"], 1)
        self.assertEqual(features["employment_type"], "COS")
        self.assertEqual(features["department"], "Operations")

        # Leave is removed from the denominator; travel and official business
        # are consistently present-equivalent.  3 present-equivalent / 7.
        self.assertEqual(features["previous_attendance_rate"], 42.86)

    def test_approved_leave_does_not_reduce_attendance_or_raise_risk(self):
        result = aggregate_dtr_features(
            self.profile,
            {
                "rows": [
                    {"date": "2026-01-05", "status": "Approved Leave", "total_minutes": 0},
                    {"date": "2026-01-06", "status": "Present", "total_minutes": 480},
                ],
                "leave_days": 1,
                "records_used": 2,
            },
        )
        self.assertEqual(result["features"]["previous_attendance_rate"], 100.0)
        self.assertEqual(result["risk"]["score"], 0)

    def test_official_business_travel_and_half_day_count_as_present(self):
        result = aggregate_dtr_features(
            self.profile,
            {
                "rows": [
                    {"date": "2026-01-05", "status": "Official Business"},
                    {"date": "2026-01-06", "status": "Official Travel"},
                    {"date": "2026-01-07", "status": "Half Day", "total_minutes": 240},
                ],
                "travel_days": 1,
                "records_used": 1,
            },
        )
        self.assertEqual(result["features"]["previous_attendance_rate"], 100.0)
        self.assertEqual(result["features"]["previous_average_worked_hours"], 4.0)


class PeriodPairingAndLeakageTests(SimpleTestCase):
    def setUp(self):
        self.profile = SimpleNamespace(
            pk=23,
            id=23,
            branch=object(),
            employment_type="JO",
            department="Field",
        )
        self.rules = object()

    def test_pairing_uses_cadence_and_strict_non_overlap(self):
        monthly_1 = period(1, date(2026, 1, 1), date(2026, 1, 31), "MONTHLY")
        monthly_2 = period(2, date(2026, 2, 1), date(2026, 2, 28), "MONTHLY")
        half_1 = period(3, date(2026, 1, 1), date(2026, 1, 15), "FIRST_HALF")
        half_2 = period(4, date(2026, 1, 16), date(2026, 1, 31), "SECOND_HALF")
        pairs = pair_chronological_periods([half_2, monthly_2, half_1, monthly_1])
        self.assertIn((monthly_1, monthly_2), pairs)
        self.assertIn((half_1, half_2), pairs)
        self.assertNotIn((half_1, monthly_2), pairs)
        self.assertTrue(all(target.start_date > feature.end_date for feature, target in pairs))

    def test_one_period_and_empty_history_create_no_rows(self):
        only = period(1, date(2026, 1, 1), date(2026, 1, 31), "MONTHLY")
        resolver = lambda profile: self.rules
        self.assertEqual(
            build_employee_period_dataset(
                profiles=[self.profile], periods=[only], rules_resolver=resolver
            ),
            [],
        )
        self.assertEqual(
            build_employee_period_dataset(profiles=[], periods=[], rules_resolver=resolver),
            [],
        )

    def test_t_features_pair_with_t_plus_one_target_without_identifier_feature(self):
        first = period(1, date(2026, 1, 1), date(2026, 1, 31), "MONTHLY")
        second = period(2, date(2026, 2, 1), date(2026, 2, 28), "MONTHLY")

        def builder(profile, branch, selected_period, rules):
            if selected_period.pk == first.pk:
                return {
                    "rows": [
                        {
                            "date": "2026-01-05",
                            "status": "Present",
                            "late": 7,
                            "undertime": 0,
                            "total_minutes": 480,
                        }
                    ],
                    "records_used": 2,
                }
            return {
                "rows": [{"date": "2026-02-02", "status": "Absent"}],
                "records_used": 0,
            }

        rows = build_employee_period_dataset(
            profiles=[self.profile],
            periods=[second, first],
            rules_resolver=lambda profile: self.rules,
            dtr_builder=builder,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["feature_period_id"], 1)
        self.assertEqual(row["target_period_id"], 2)
        self.assertGreater(row["target_period_start"], row["feature_period_end"])
        self.assertEqual(row["features"]["previous_late_minutes"], 7)
        self.assertEqual(row["target"], 1)
        self.assertEqual(row["employee_group_id"], 23)
        self.assertFalse(
            {"employee_group_id", "employee_id", "profile_id", "username", "email"}
            & set(row["features"])
        )

    def test_future_period_changes_target_but_cannot_change_features(self):
        first = period(1, date(2026, 1, 1), date(2026, 1, 31), "MONTHLY")
        second = period(2, date(2026, 2, 1), date(2026, 2, 28), "MONTHLY")
        future_status = {"value": "Present"}

        def builder(profile, branch, selected_period, rules):
            if selected_period.pk == first.pk:
                return {
                    "rows": [
                        {"date": "2026-01-05", "status": "Present", "total_minutes": 480}
                    ],
                    "records_used": 2,
                }
            return {"rows": [{"date": "2026-02-02", "status": future_status["value"]}]}

        kwargs = {
            "profiles": [self.profile],
            "periods": [first, second],
            "rules_resolver": lambda profile: self.rules,
            "dtr_builder": builder,
        }
        before = build_employee_period_dataset(**kwargs)[0]
        future_status["value"] = "Absent"
        after = build_employee_period_dataset(**kwargs)[0]

        self.assertEqual(before["features"], after["features"])
        self.assertEqual(before["target"], 0)
        self.assertEqual(after["target"], 1)
