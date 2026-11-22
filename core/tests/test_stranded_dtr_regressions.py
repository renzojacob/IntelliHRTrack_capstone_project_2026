"""DTR regressions migrated from the shadowed ``core/tests.py`` module."""

from datetime import date, datetime, time

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import (
    AttendanceRecord,
    Branch,
    HolidaySuspension,
    LeaveRequest,
    PayrollPeriod,
    PayrollRule,
    UserProfile,
)
from core.views import _build_dtr_and_summary


class DTRIntegrityTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Main")
        self.other_branch = Branch.objects.create(name="Other")
        self.rules = PayrollRule.objects.create(
            branch=self.branch,
            tax_rate_percent=0,
            grace_minutes_normal=0,
            lunch_break_required=True,
            daily_hours_required=8,
            flag_ceremony_cutoff_time=time(8, 0),
            earliest_creditable_time=time(7, 0),
            work_start_time=time(8, 0),
            lunch_start_time=time(12, 0),
            lunch_end_time=time(13, 0),
            work_end_time=time(17, 0),
        )
        self.period = PayrollPeriod.objects.create(
            name="May 6",
            start_date=date(2026, 5, 6),
            end_date=date(2026, 5, 6),
            pay_mode=PayrollPeriod.PAY_FIRST_HALF,
        )
        self.user = User.objects.create_user(username="employee", password="test-password")
        self.profile = UserProfile.objects.create(
            user=self.user,
            branch=self.branch,
            employment_type=UserProfile.EMP_JO,
            daily_rate=800,
            biometric_employee_id="1001",
            is_approved=True,
        )

    def make_log(self, branch, hour, status):
        timestamp = timezone.make_aware(datetime(2026, 5, 6, hour, 0))
        return AttendanceRecord.objects.create(
            employee_id="1001",
            full_name="Employee",
            branch=branch,
            timestamp=timestamp,
            attendance_status=status,
        )

    def test_incomplete_day_is_not_counted_as_paid_present_day(self):
        self.make_log(self.branch, 8, AttendanceRecord.STATUS_CHECKIN)
        result = _build_dtr_and_summary(self.profile, self.branch, self.period, self.rules)
        self.assertEqual(result["days_present"], 0)
        self.assertEqual(result["missing_logs"], 1)
        self.assertEqual(result["rows"][0]["status"], "Incomplete")

    def test_off_branch_attendance_is_ignored(self):
        self.make_log(self.other_branch, 8, AttendanceRecord.STATUS_CHECKIN)
        self.make_log(self.other_branch, 17, AttendanceRecord.STATUS_CHECKOUT)
        result = _build_dtr_and_summary(self.profile, self.branch, self.period, self.rules)
        self.assertEqual(result["days_present"], 0)
        self.assertEqual(result["records_used"], 0)
        self.assertEqual(result["off_branch_records_ignored"], 2)

    def test_approved_full_day_leave_is_not_an_absence(self):
        LeaveRequest.objects.create(
            employee=self.user,
            branch=self.branch,
            leave_type=LeaveRequest.TYPE_VACATION,
            start_date=self.period.start_date,
            end_date=self.period.end_date,
            duration=LeaveRequest.DURATION_FULL,
            reason="Approved leave",
            status=LeaveRequest.STATUS_APPROVED,
        )
        result = _build_dtr_and_summary(self.profile, self.branch, self.period, self.rules)
        self.assertEqual(result["leave_days"], 1.0)
        self.assertEqual(result["absences"], 0)
        self.assertEqual(result["rows"][0]["status"], "Approved Leave")

    def test_unverified_holiday_is_ignored_until_source_is_confirmed(self):
        holiday = HolidaySuspension.objects.create(
            date=self.period.start_date,
            name="Test holiday",
            type=HolidaySuspension.TYPE_HOLIDAY,
            scope=HolidaySuspension.SCOPE_NATIONWIDE,
            is_payroll_verified=False,
        )
        unverified = _build_dtr_and_summary(self.profile, self.branch, self.period, self.rules)
        self.assertEqual(unverified["holiday_days"], 0)
        self.assertIn("unverified holiday", " ".join(unverified["issues"]).lower())

        holiday.is_payroll_verified = True
        holiday.source_reference = "Official proclamation"
        holiday.save()
        verified = _build_dtr_and_summary(self.profile, self.branch, self.period, self.rules)
        self.assertEqual(verified["holiday_days"], 1)
        self.assertEqual(verified["absences"], 0)
