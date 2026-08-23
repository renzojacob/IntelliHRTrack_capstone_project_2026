from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import (
    AttendanceRecord,
    Branch,
    EmployeeContribution,
    HolidaySuspension,
    LeaveRequest,
    OvertimeRequest,
    PayrollBatch,
    PayrollItem,
    PayrollPeriod,
    PayrollRule,
    UserProfile,
)
from .payroll_calculations import bir_withholding_tax
from .views import (
    _build_dtr_and_summary,
    _build_payroll_batch_validation,
    _compute_payroll,
)


def dtr_summary(**overrides):
    summary = {
        "employee_id_used": "1001",
        "rows": [],
        "days_present": 0,
        "travel_days": 0,
        "holiday_days": 0,
        "leave_days": 0,
        "official_leave_days": 0,
        "active_workdays": 22,
        "outside_employment_days": 0,
        "absences": 0,
        "missing_logs": 0,
        "late_minutes": 0,
        "undertime_minutes": 0,
        "records_found": 1,
        "records_used": 1,
        "off_branch_records_ignored": 0,
        "issues": [],
    }
    summary.update(overrides)
    return summary


class PayrollCalculationTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Occidental Mindoro")
        self.rules = PayrollRule.objects.create(
            branch=self.branch,
            tax_rate_percent=Decimal("0.00"),
            premium_rate_percent=Decimal("20.00"),
            philhealth_default_value=Decimal("0.00"),
            salary_divisor=Decimal("22.00"),
            grace_minutes_normal=0,
            daily_hours_required=Decimal("8.00"),
            lunch_break_required=True,
            work_start_time=time(8, 0),
            work_end_time=time(17, 0),
        )
        self.monthly_period = PayrollPeriod.objects.create(
            name="May 2026",
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
            pay_mode=PayrollPeriod.PAY_MONTHLY,
        )
        self.semi_period = PayrollPeriod.objects.create(
            name="May 1-15 2026",
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 15),
            pay_mode=PayrollPeriod.PAY_FIRST_HALF,
        )

    def make_profile(self, username, employment_type, monthly="0", daily="0", **kwargs):
        user = User.objects.create_user(username=username, password="test-password")
        profile = UserProfile.objects.create(
            user=user,
            branch=self.branch,
            employment_type=employment_type,
            monthly_salary=Decimal(monthly),
            daily_rate=Decimal(daily),
            biometric_employee_id=f"BIO-{username}",
            is_approved=True,
            **kwargs,
        )
        EmployeeContribution.objects.create(
            profile=profile,
            sss_amount=Decimal("0.00"),
            pagibig_amount=Decimal("0.00"),
            philhealth_mode=EmployeeContribution.PHILHEALTH_PERCENT,
            philhealth_value=Decimal("0.00"),
        )
        return profile

    def test_bir_annex_e_thresholds(self):
        self.assertEqual(
            bir_withholding_tax(Decimal("20833.00"), PayrollPeriod.PAY_MONTHLY),
            Decimal("0.00"),
        )
        self.assertEqual(
            bir_withholding_tax(Decimal("26350.00"), PayrollPeriod.PAY_MONTHLY),
            Decimal("827.55"),
        )
        self.assertEqual(
            bir_withholding_tax(Decimal("13175.00"), PayrollPeriod.PAY_FIRST_HALF),
            Decimal("413.70"),
        )

    @patch("core.views._build_dtr_and_summary")
    def test_daily_jo_matches_actual_days_and_exact_minute_formula(self, build_dtr):
        build_dtr.return_value = dtr_summary(days_present=1, late_minutes=30)
        profile = self.make_profile("jo", UserProfile.EMP_JO, daily="800")

        result = _compute_payroll(profile, self.branch, self.monthly_period, self.rules)
        computed = result["computed_payroll"]

        self.assertEqual(computed["base"], Decimal("800.00"))
        self.assertEqual(computed["late_deduction"], Decimal("50.00"))
        self.assertEqual(computed["net"], Decimal("750.00"))

    @patch("core.views._build_dtr_and_summary")
    def test_fixed_monthly_cos_less_three_absences(self, build_dtr):
        build_dtr.return_value = dtr_summary(absences=3, days_present=19)
        profile = self.make_profile("cos", UserProfile.EMP_COS, monthly="20000")

        result = _compute_payroll(profile, self.branch, self.monthly_period, self.rules)
        computed = result["computed_payroll"]

        self.assertEqual(computed["base"], Decimal("20000.00"))
        self.assertEqual(computed["absence_deduction"], Decimal("2727.27"))
        self.assertEqual(computed["net"], Decimal("17272.73"))

    @patch("core.views._build_dtr_and_summary")
    def test_approved_overtime_is_paid_even_when_late_is_deducted(self, build_dtr):
        build_dtr.return_value = dtr_summary(days_present=1, late_minutes=30)
        profile = self.make_profile("jo-ot", UserProfile.EMP_JO, daily="800")
        OvertimeRequest.objects.create(
            profile=profile,
            date=date(2026, 5, 6),
            hours=Decimal("2.00"),
            approved=True,
        )

        result = _compute_payroll(profile, self.branch, self.monthly_period, self.rules)
        computed = result["computed_payroll"]

        self.assertEqual(computed["ot"], Decimal("250.00"))
        self.assertEqual(computed["late_deduction"], Decimal("50.00"))
        self.assertEqual(computed["net"], Decimal("1000.00"))

    @patch("core.views._build_dtr_and_summary")
    def test_permanent_monthly_statutory_deductions_and_pera_tax_exclusion(self, build_dtr):
        build_dtr.return_value = dtr_summary(days_present=22)
        profile = self.make_profile(
            "permanent",
            UserProfile.EMP_PERMANENT,
            monthly="30000",
            pera_allowance=Decimal("2000.00"),
        )

        result = _compute_payroll(profile, self.branch, self.monthly_period, self.rules)
        computed = result["computed_payroll"]
        gov = result["gov"]

        self.assertEqual(computed["gross"], Decimal("32000.00"))
        self.assertEqual(gov["gsis_employee"], Decimal("2700.00"))
        self.assertEqual(gov["philhealth"], Decimal("750.00"))
        self.assertEqual(gov["pagibig"], Decimal("200.00"))
        self.assertEqual(gov["taxable_compensation"], Decimal("26350.00"))
        self.assertEqual(gov["wtax"], Decimal("827.55"))
        self.assertEqual(gov["employer_contributions_total"], Decimal("4550.00"))
        self.assertEqual(computed["net"], Decimal("27522.45"))

    @patch("core.views._build_dtr_and_summary")
    def test_permanent_semi_monthly_uses_half_month_shares(self, build_dtr):
        build_dtr.return_value = dtr_summary(days_present=11, active_workdays=11)
        profile = self.make_profile(
            "permanent-semi",
            UserProfile.EMP_PERMANENT,
            monthly="30000",
            pera_allowance=Decimal("2000.00"),
        )

        result = _compute_payroll(profile, self.branch, self.semi_period, self.rules)
        computed = result["computed_payroll"]
        gov = result["gov"]

        self.assertEqual(computed["gross"], Decimal("16000.00"))
        self.assertEqual(gov["gsis_employee"], Decimal("1350.00"))
        self.assertEqual(gov["philhealth"], Decimal("375.00"))
        self.assertEqual(gov["pagibig"], Decimal("100.00"))
        self.assertEqual(gov["wtax"], Decimal("413.70"))
        self.assertEqual(computed["net"], Decimal("13761.30"))

    @patch("core.views._build_dtr_and_summary")
    def test_partial_contract_prorates_fixed_pay(self, build_dtr):
        build_dtr.return_value = dtr_summary(active_workdays=10)
        profile = self.make_profile(
            "partial-cos",
            UserProfile.EMP_COS,
            monthly="22000",
            employment_start_date=date(2026, 5, 18),
        )

        result = _compute_payroll(profile, self.branch, self.monthly_period, self.rules)
        self.assertEqual(result["computed_payroll"]["base"], Decimal("10000.00"))

    @patch("core.views._build_dtr_and_summary")
    def test_negative_net_is_not_silently_changed_to_zero(self, build_dtr):
        build_dtr.return_value = dtr_summary(days_present=1)
        profile = self.make_profile(
            "negative-net",
            UserProfile.EMP_JO,
            daily="100",
            manual_deduction_amount=Decimal("200.00"),
        )

        result = _compute_payroll(profile, self.branch, self.monthly_period, self.rules)
        self.assertEqual(result["computed_payroll"]["net"], Decimal("-100.00"))
        self.assertIn("cannot be finalized", result["issues"])


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
            work_start_time=time(8, 0),
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


class BatchValidationTests(TestCase):
    def test_missing_logs_block_batch_finalization(self):
        branch = Branch.objects.create(name="Validation Branch")
        period = PayrollPeriod.objects.create(
            name="Validation Period",
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 15),
            pay_mode=PayrollPeriod.PAY_FIRST_HALF,
        )
        user = User.objects.create_user(username="validation-user", password="test-password")
        profile = UserProfile.objects.create(
            user=user,
            branch=branch,
            employment_type=UserProfile.EMP_JO,
            daily_rate=800,
            biometric_employee_id="V-1",
            is_approved=True,
        )
        EmployeeContribution.objects.create(profile=profile)
        batch = PayrollBatch.objects.create(
            name="Validation",
            branch=branch,
            period=period,
            employee_type_scope=PayrollBatch.SCOPE_JO,
            status=PayrollBatch.STATUS_COMPLETED,
            totals_net=700,
            totals_deductions=100,
        )
        PayrollItem.objects.create(
            batch=batch,
            profile=profile,
            base_pay=800,
            deductions_total=100,
            net_pay=700,
            issues="1 day(s) with missing attendance logs",
            meta={
                "attendance_summary": {"missing_logs": 1},
                "computed_payroll": {"gross": "800.00"},
            },
        )

        validation = _build_payroll_batch_validation(batch)

        self.assertFalse(validation["is_ready"])
        self.assertTrue(any("missing attendance logs" in error.lower() for error in validation["errors"]))
