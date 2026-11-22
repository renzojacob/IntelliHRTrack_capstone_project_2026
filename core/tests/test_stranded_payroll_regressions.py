"""Payroll regressions migrated from the shadowed ``core/tests.py`` module.

The original file is intentionally retained.  These active tests preserve the
original invariants while reconciling obsolete amounts with current documented
deterministic policy.
"""

from datetime import date, time
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import (
    Branch,
    EmployeeContribution,
    FinalizedDTR,
    OvertimeRequest,
    PayrollBatch,
    PayrollItem,
    PayrollPeriod,
    PayrollRule,
    UserProfile,
)
from core.payroll_calculations import bir_withholding_tax
from core.views import (
    _build_payroll_batch_validation,
    _compute_payroll,
    _get_payroll_batch_finalization_errors,
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
        self.assertEqual(result["rates"]["per_minute"], Decimal("1.67"))
        self.assertEqual(computed["late_deduction"], Decimal("50.10"))
        self.assertEqual(result["gov"]["philhealth"], Decimal("500.00"))
        self.assertEqual(computed["net"], Decimal("249.90"))

    @patch("core.views._build_dtr_and_summary")
    def test_fixed_monthly_cos_less_three_absences(self, build_dtr):
        build_dtr.return_value = dtr_summary(absences=3, days_present=19)
        profile = self.make_profile("cos", UserProfile.EMP_COS, monthly="20000")
        result = _compute_payroll(profile, self.branch, self.monthly_period, self.rules)
        computed = result["computed_payroll"]
        self.assertEqual(computed["base"], Decimal("20000.00"))
        self.assertEqual(computed["absence_deduction"], Decimal("2727.27"))
        self.assertEqual(result["gov"]["philhealth"], Decimal("863.64"))
        self.assertEqual(computed["net"], Decimal("16409.09"))

    @patch("core.views._build_dtr_and_summary")
    def test_late_day_overtime_is_not_paid_even_when_approved(self, build_dtr):
        build_dtr.return_value = dtr_summary(
            days_present=1,
            late_minutes=30,
            rows=[
                {
                    "date": "2026-05-05",
                    "status": "Present",
                    "late": 0,
                    "total_minutes": 480,
                },
                {
                    "date": "2026-05-06",
                    "status": "Present",
                    "late": 30,
                    "total_minutes": 600,
                },
            ],
        )
        profile = self.make_profile("jo-ot", UserProfile.EMP_JO, daily="800")
        approver = User.objects.create_user(
            username="overtime-approver",
            password="test-password",
            is_staff=True,
        )
        OvertimeRequest.objects.create(
            profile=profile,
            date=date(2026, 5, 6),
            hours=Decimal("2.00"),
            approved=True,
            approved_by=approver,
        )
        result = _compute_payroll(profile, self.branch, self.monthly_period, self.rules)
        computed = result["computed_payroll"]
        self.assertEqual(computed["overtime_approved_hours"], Decimal("2.00"))
        self.assertEqual(computed["overtime_eligible_minutes"], 0)
        self.assertEqual(computed["overtime_excluded_minutes"], 120)
        self.assertEqual(computed["ot"], Decimal("0.00"))
        self.assertTrue(
            any(
                "late by 30 minute" in blocker
                for blocker in computed["overtime_details"][0]["blockers"]
            )
        )

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
        computed = result["computed_payroll"]
        self.assertEqual(result["gov"]["philhealth"], Decimal("500.00"))
        self.assertEqual(computed["net"], Decimal("-600.00"))
        self.assertLess(computed["net"], Decimal("0.00"))
        self.assertNotEqual(computed["net"], Decimal("0.00"))
        self.assertIn("cannot be finalized", result["issues"])

        batch = PayrollBatch.objects.create(
            name="Negative net validation",
            branch=self.branch,
            period=self.monthly_period,
            employee_type_scope=PayrollBatch.SCOPE_JO,
            status=PayrollBatch.STATUS_COMPLETED,
            totals_net=Decimal("-600.00"),
            totals_deductions=Decimal("700.00"),
        )
        PayrollItem.objects.create(
            batch=batch,
            profile=profile,
            base_pay=Decimal("100.00"),
            gov_contributions_total=Decimal("500.00"),
            manual_deduction=Decimal("200.00"),
            deductions_total=Decimal("700.00"),
            net_pay=Decimal("-600.00"),
            issues=result["issues"],
            meta={
                "policy_blockers": [],
                "attendance_summary": {"missing_logs": 0},
                "computed_payroll": {
                    "gross": "100.00",
                    "attendance_deduction": "0.00",
                    "loan_deduction": "0.00",
                    "other_deduction": "0.00",
                },
            },
        )
        finalization_errors = _get_payroll_batch_finalization_errors(batch)
        self.assertTrue(
            any("deductions exceed gross pay" in error.lower() for error in finalization_errors)
        )


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
        self.assertTrue(
            any("missing attendance logs" in error.lower() for error in validation["errors"])
        )
        finalization_errors = _get_payroll_batch_finalization_errors(batch)
        self.assertTrue(finalization_errors)
        self.assertTrue(
            any("missing attendance logs" in error.lower() for error in finalization_errors)
        )

    def test_valid_batch_reaches_complete_validation_path(self):
        branch = Branch.objects.create(name="Ready Validation Branch")
        period = PayrollPeriod.objects.create(
            name="Ready Validation Period",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 15),
            pay_mode=PayrollPeriod.PAY_FIRST_HALF,
        )
        user = User.objects.create_user(username="ready-user", password="test-password")
        profile = UserProfile.objects.create(
            user=user,
            branch=branch,
            employment_type=UserProfile.EMP_JO,
            daily_rate=Decimal("800.00"),
            biometric_employee_id="READY-1",
            is_approved=True,
        )
        EmployeeContribution.objects.create(profile=profile)
        batch = PayrollBatch.objects.create(
            name="Ready Validation",
            branch=branch,
            period=period,
            employee_type_scope=PayrollBatch.SCOPE_JO,
            status=PayrollBatch.STATUS_COMPLETED,
            totals_net=Decimal("700.00"),
            totals_deductions=Decimal("100.00"),
        )
        item = PayrollItem.objects.create(
            batch=batch,
            profile=profile,
            base_pay=Decimal("800.00"),
            deductions_total=Decimal("100.00"),
            net_pay=Decimal("700.00"),
            issues="",
            meta={
                "policy_blockers": [],
                "attendance_summary": {"missing_logs": 0},
                "computed_payroll": {
                    "gross": "800.00",
                    "attendance_deduction": "100.00",
                    "loan_deduction": "0.00",
                    "other_deduction": "0.00",
                },
            },
        )
        FinalizedDTR.objects.create(
            profile=profile,
            branch=branch,
            period=period,
            payroll_item=item,
            rows=[],
            summary={"missing_logs": 0},
            is_locked=True,
        )

        validation = _build_payroll_batch_validation(batch)
        self.assertTrue(validation["is_ready"], validation["errors"])
        self.assertEqual(validation["errors"], [])
        self.assertTrue(
            any(
                check["label"] == "No missing attendance logs" and check["passed"]
                for check in validation["checks"]
            )
        )
        self.assertEqual(_get_payroll_batch_finalization_errors(batch), [])
