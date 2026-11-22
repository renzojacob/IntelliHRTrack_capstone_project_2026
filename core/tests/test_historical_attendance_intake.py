from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
import json
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.analytics.historical_intake import (
    audit_historical_file,
    audit_historical_rows,
    build_dry_run_plan,
)
from core.models import AttendanceRecord, Branch, PayrollPeriod, UserProfile


MNL = ZoneInfo("Asia/Manila")


class HistoricalAttendanceIntakeTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Synthetic Main")

    def profile(self, username, biometric_id, *, branch=None, start=None, end=None):
        user = User.objects.create_user(
            username=username,
            password="test-only-password",
            email=f"{username}@invalid.test",
        )
        return UserProfile.objects.create(
            user=user,
            branch=branch or self.branch,
            biometric_employee_id=biometric_id,
            employment_start_date=start,
            employment_end_date=end,
            is_approved=True,
            monthly_salary="12345.67",
        )

    @staticmethod
    def row(employee_id="BIO-101", timestamp="2026-08-10 08:00:00", **extra):
        data = {
            "Person ID": employee_id,
            "Time": timestamp,
            "Attendance Status": "Check In",
        }
        data.update(extra)
        return data

    def test_valid_csv_source_rows_parse_correctly(self):
        self.profile("synthetic_valid", "BIO-101")
        source = BytesIO(
            b"Person ID,Time,Attendance Status\n"
            b"BIO-101,2026-08-10 08:00:00,Check In\n"
        )

        report = audit_historical_file(source, "history.csv", branch=self.branch)

        self.assertEqual(report["total_rows"], 1)
        self.assertEqual(report["rows_accepted"], 1)
        self.assertEqual(report["matched_employee_identifiers"], 1)

    def test_malformed_and_ambiguous_timestamps_are_flagged(self):
        self.profile("synthetic_bad_time", "BIO-101")
        report = audit_historical_rows(
            [
                self.row(timestamp="not-a-time"),
                self.row(timestamp="01/02/2026 08:00"),
            ],
            branch=self.branch,
        )

        self.assertEqual(report["malformed_timestamps"], 1)
        self.assertEqual(report["ambiguous_timestamps"], 1)
        self.assertEqual(report["rows_rejected"], 2)

    def test_missing_identifiers_and_timestamps_are_flagged(self):
        report = audit_historical_rows(
            [self.row(employee_id=""), self.row(timestamp="")],
            branch=self.branch,
        )

        self.assertEqual(report["rows_with_missing_employee_identifiers"], 1)
        self.assertEqual(report["rows_with_missing_timestamps"], 1)

    def test_exact_duplicate_source_rows_are_detected(self):
        self.profile("synthetic_duplicate", "BIO-101")
        row = self.row()

        report = audit_historical_rows([row, dict(row)], branch=self.branch)

        self.assertEqual(report["exact_duplicate_source_rows"], 1)
        self.assertEqual(report["rows_accepted"], 1)
        self.assertEqual(report["rows_rejected"], 1)

    def test_unmatched_employee_identifier_is_reported(self):
        report = audit_historical_rows(
            [self.row(employee_id="NO-SUCH-BIOMETRIC-ID")],
            branch=self.branch,
        )

        self.assertEqual(report["unmatched_employee_identifiers"], 1)
        self.assertEqual(report["unmatched_rows"], 1)
        self.assertEqual(report["rows_accepted"], 0)

    def test_ambiguous_exact_employee_matches_are_rejected(self):
        self.profile("synthetic_ambiguous_one", "DUPLICATED-ID")
        self.profile("synthetic_ambiguous_two", "DUPLICATED-ID")

        report = audit_historical_rows(
            [self.row(employee_id="DUPLICATED-ID")],
            branch=self.branch,
        )

        self.assertEqual(report["ambiguous_employee_identifiers"], 1)
        self.assertEqual(report["ambiguous_employee_rows"], 1)
        self.assertEqual(report["rows_rejected"], 1)

    def test_existing_database_duplicate_and_status_conflict_are_recognized(self):
        profile = self.profile("synthetic_existing", "BIO-101")
        stamp = datetime(2026, 8, 10, 8, 0, tzinfo=MNL)
        AttendanceRecord.objects.create(
            employee_id=profile.biometric_employee_id,
            branch=self.branch,
            timestamp=stamp,
            attendance_status=AttendanceRecord.STATUS_CHECKIN,
        )

        report = audit_historical_rows(
            [
                self.row(),
                self.row(**{"Attendance Status": "Check Out"}),
            ],
            branch=self.branch,
        )

        self.assertEqual(report["rows_already_existing"], 1)
        self.assertEqual(report["overlap_with_existing_attendance"], 1)
        self.assertEqual(report["potential_conflicting_records"], 1)

    def test_dry_run_executes_no_write_queries_and_changes_no_attendance(self):
        self.profile("synthetic_read_only", "BIO-101")
        before = AttendanceRecord.objects.count()

        with CaptureQueriesContext(connection) as queries:
            report = build_dry_run_plan([self.row()], branch=self.branch)

        write_sql = [
            query["sql"]
            for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        ]
        self.assertEqual(write_sql, [])
        self.assertEqual(AttendanceRecord.objects.count(), before)
        self.assertEqual(report["database_writes"], 0)
        self.assertEqual(report["mode"], "DRY_RUN")

    def test_date_range_and_aggregate_employee_counts_are_correct(self):
        self.profile("synthetic_range_one", "BIO-101")
        self.profile("synthetic_range_two", "BIO-202")

        report = audit_historical_rows(
            [
                self.row("BIO-101", "2026-07-31 17:00:00"),
                self.row("BIO-202", "2026-09-01 07:30:00"),
            ],
            branch=self.branch,
        )

        self.assertTrue(report["earliest_timestamp"].startswith("2026-07-31T17:00:00"))
        self.assertTrue(report["latest_timestamp"].startswith("2026-09-01T07:30:00"))
        self.assertEqual(report["unique_source_employee_identifiers"], 2)
        self.assertEqual(report["matched_employee_identifiers"], 2)
        self.assertEqual(report["historical_months"], ["2026-07", "2026-09"])

    def test_names_are_never_used_for_fuzzy_matching(self):
        self.profile("person_with_similar_name", "REAL-STABLE-ID")
        row = self.row(employee_id="WRONG-ID", Name="person_with_similar_name")

        report = audit_historical_rows([row], branch=self.branch)

        self.assertEqual(report["matched_employee_identifiers"], 0)
        self.assertEqual(report["unmatched_employee_identifiers"], 1)

    def test_standard_summary_emits_no_sensitive_profile_or_source_values(self):
        self.profile("private_username", "SECRET-BIO-ID")
        row = self.row(
            employee_id="SECRET-BIO-ID",
            Name="Private Employee Name",
            Email="private@example.invalid",
            Salary="999999.99",
        )

        output = json.dumps(audit_historical_rows([row], branch=self.branch))

        for secret in (
            "SECRET-BIO-ID",
            "Private Employee Name",
            "private_username",
            "private@example.invalid",
            "999999.99",
            "12345.67",
        ):
            self.assertNotIn(secret, output)

    def test_employment_branch_and_coverage_preview_are_aggregate_only(self):
        other_branch = Branch.objects.create(name="Synthetic Other")
        self.profile(
            "synthetic_period",
            "BIO-101",
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
        )
        PayrollPeriod.objects.create(
            name="Synthetic August First Half",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 15),
            pay_mode=PayrollPeriod.PAY_FIRST_HALF,
        )
        PayrollPeriod.objects.create(
            name="Synthetic August Second Half",
            start_date=date(2026, 8, 16),
            end_date=date(2026, 8, 31),
            pay_mode=PayrollPeriod.PAY_SECOND_HALF,
        )

        report = audit_historical_rows(
            [
                self.row(timestamp="2026-07-31 08:00:00"),
                self.row(timestamp="2026-08-01 08:00:00"),
                self.row(timestamp="2026-08-31 17:00:00"),
                self.row(timestamp="2026-08-10 08:00:00", Branch=other_branch.name),
            ],
            branch=self.branch,
            as_of_date=date(2026, 9, 15),
        )

        self.assertEqual(report["records_outside_employment_periods"], 1)
        self.assertEqual(report["records_outside_selected_branch"], 1)
        self.assertEqual(report["completed_payroll_periods_potentially_covered"], 2)
        self.assertEqual(report["completed_periods_with_attendance_evidence"], 2)
        self.assertEqual(report["possible_consecutive_feature_target_windows"], 1)

    def test_unknown_source_branch_is_flagged(self):
        self.profile("synthetic_unknown_branch", "BIO-101")

        report = audit_historical_rows(
            [self.row(Branch="Not A Configured Branch")],
            branch=self.branch,
        )

        self.assertEqual(report["records_outside_known_branches"], 1)
        self.assertEqual(report["rows_rejected"], 1)
