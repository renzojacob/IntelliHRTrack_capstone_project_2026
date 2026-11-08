"""
November client review: missing AM arrival, combined Late, Half-Day &
Undertime line, monthly PhilHealth and the client's monthly tax cutoff.

Reference case: Drei22, JO, Calapan, daily rate P829.59, 20% premium,
optional SSS P760 and Pag-IBIG P400 a month.
"""
import datetime as dt
from decimal import Decimal as Dec

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core import dtr_rules as R
from core import payroll_calculations as P
from core import views
from core.models import EmployeeContribution, PayrollBatch, PayrollItem, PayrollRule
from core.tests import factories as F
from core.tests.test_dtr_rules import day

D = dt.date

OCT_12_16 = {
    12: ("10:21", "12:37", "12:37", "16:23"),
    13: ("08:29", "12:36", "12:36", "16:16"),
    14: ("11:07", "12:05", "12:05", "16:12"),
    15: ("08:29", "12:04", "12:04", "16:05"),
    16: ("08:46", "12:09", "12:09", "16:10"),
}
OCT_19 = (None, "12:03", "12:03", "16:08")


# =========================================================================
# Daily rules
# =========================================================================

class MissingAmArrivalTests(SimpleTestCase):
    def test_oct19_is_absent_with_52_minute_reference(self):
        r = day(*OCT_19, ceremony=True)
        self.assertEqual(r.status, "Absent")
        self.assertTrue(r.below_minimum)
        self.assertTrue(r.needs_review)
        self.assertEqual(r.actual_minutes, 188)            # 1:00 PM - 4:08 PM; no morning credit
        self.assertEqual(r.deductible, 0)                  # the absence covers the day; nothing added
        self.assertEqual(r.reference_shortfall, 52)        # 5:00 PM - 4:08 PM
        self.assertEqual(r.half_day_penalty, 0)            # never 240 + 52 = 292
        self.assertIn("AM arrival missing", r.remarks)

    def test_full_afternoon_without_am_arrival_is_the_existing_half_day(self):
        r = day(None, "12:00", "13:00", "17:00")
        self.assertEqual((r.status, r.half_day_penalty, r.departure_shortfall, r.deductible), ("Half Day", 240, 0, 240))
        self.assertTrue(r.needs_review)
        self.assertEqual(r.reference_shortfall, 0)

    def test_no_am_punch_is_ever_invented(self):
        r = day(*OCT_19)
        labels = dict(r.lines)
        self.assertIn("No time is assumed", labels["Missing punch"])
        self.assertTrue(labels["Actual eligible work"].startswith("0 + 188"))

    def test_other_partial_punches_stay_incomplete(self):
        self.assertEqual(day("08:00", None, "13:00", "17:00").status, "Incomplete")
        self.assertEqual(day(None, "12:03", "12:03", None).status, "Incomplete")

    def test_existing_october_rows_unchanged(self):
        results = [day(*p, ceremony=(d == 12)).deductible for d, p in OCT_12_16.items()]
        self.assertEqual(results, [277, 73, 288, 84, 96])
        self.assertEqual(R.hm(sum(results)), "13 h 38 min")

    def test_complete_day_undertime_is_correct(self):
        r = day("08:00", "12:00", "13:00", "16:08")
        self.assertEqual((r.status, r.deductible, r.reference_shortfall), ("Present", 52, 0))


class SnapshotVersionTests(SimpleTestCase):
    def row(self, version, status, am_in="", employment="Job Order (JO)"):
        return {"rules_version": version, "status": status, "am_in": am_in, "am_out": "12:03 PM",
                "pm_in": "12:03 PM", "pm_out": "04:08 PM", "computation": {"employment_type": employment}}

    def test_old_missing_am_row_is_stale(self):
        self.assertFalse(R.row_is_current(self.row(3, "Incomplete")))

    def test_old_ordinary_rows_stay_current(self):
        self.assertTrue(R.row_is_current(self.row(3, "Present", am_in="08:29 AM")))
        self.assertTrue(R.row_is_current(self.row(2, "Half Day", am_in="10:21 AM")))

    def test_new_rows_are_current(self):
        self.assertTrue(R.row_is_current(self.row(R.RULES_VERSION, "Absent")))


# =========================================================================
# Pure money rules
# =========================================================================

class AttendanceMoneyTests(SimpleTestCase):
    def test_client_worksheet_1248_25(self):
        self.assertEqual(P.attendance_time_deduction(722, "829.59", "1.73"), Dec("1248.25"))

    def test_day_boundaries(self):
        self.assertEqual(P.attendance_time_deduction(479, "829.59", "1.73"), Dec("828.67"))
        self.assertEqual(P.attendance_time_deduction(480, "829.59", "1.73"), Dec("829.59"))
        self.assertEqual(P.attendance_time_deduction(481, "829.59", "1.73"), Dec("831.32"))
        self.assertEqual(P.attendance_time_deduction(0, "829.59", "1.73"), Dec("0.00"))

    def test_parts_add_up_to_the_combined_amount(self):
        late, ut = P.split_attendance_deduction(Dec("1248.25"), 538, 184)
        self.assertEqual(late + ut, Dec("1248.25"))
        self.assertEqual((late, ut), (Dec("930.14"), Dec("318.11")))
        self.assertEqual(P.split_attendance_deduction(Dec("0.00"), 0, 0), (Dec("0.00"), Dec("0.00")))


class PhilHealthTests(SimpleTestCase):
    def test_floor(self):
        for base in ("0", "2222.50", "9999.99", "10000.00", "10000.01"):
            self.assertEqual(P.jo_cos_philhealth_monthly(base), Dec("500.00"), base)

    def test_above_floor_and_ceiling(self):
        self.assertEqual(P.jo_cos_philhealth_monthly("20000.00"), Dec("1000.00"))
        self.assertEqual(P.jo_cos_philhealth_monthly("100000.00"), Dec("5000.00"))
        self.assertEqual(P.jo_cos_philhealth_monthly("150000.00"), Dec("5000.00"))

    def test_collection_timing(self):
        self.assertFalse(P.collects_monthly_items("FIRST_HALF"))
        self.assertTrue(P.collects_monthly_items("SECOND_HALF"))
        self.assertTrue(P.collects_monthly_items("MONTHLY"))


class TaxCutoffTests(SimpleTestCase):
    def test_below_cutoff_is_zero(self):
        self.assertEqual(P.jo_cos_monthly_tax("20833.32", 5), (Dec("0.00"), None))

    def test_at_and_above_cutoff_is_pending(self):
        for base in ("20833.33", "20833.34"):
            amount, pending = P.jo_cos_monthly_tax(base, 5)
            self.assertEqual(amount, Dec("0.00"))
            self.assertEqual(pending, P.TAX_PENDING_REASON)

    def test_method_switches_once_the_client_decides(self):
        self.assertEqual(P.jo_cos_monthly_tax("20833.33", 5, method="whole"), (Dec("1041.67"), None))
        self.assertEqual(P.jo_cos_monthly_tax("20853.33", 5, method="excess"), (Dec("1.00"), None))
        self.assertEqual(P.jo_cos_monthly_tax("20833.32", 5, method="whole"), (Dec("0.00"), None))


class PeriodBoundaryTests(SimpleTestCase):
    def test_last_day_of_month_is_actual(self):
        err = P.payroll_period_validation_error
        self.assertEqual(err(D(2028, 2, 16), D(2028, 2, 29), "SECOND_HALF"), "")
        self.assertTrue(err(D(2028, 2, 16), D(2028, 2, 28), "SECOND_HALF"))
        self.assertEqual(err(D(2026, 2, 16), D(2026, 2, 28), "SECOND_HALF"), "")
        self.assertEqual(err(D(2026, 11, 16), D(2026, 11, 30), "SECOND_HALF"), "")
        self.assertTrue(err(D(2026, 10, 16), D(2026, 10, 30), "SECOND_HALF"))
        self.assertEqual(err(D(2026, 10, 1), D(2026, 10, 31), "MONTHLY"), "")


# =========================================================================
# End to end on real models
# =========================================================================

class Drei22Base(TestCase):
    def setUp(self):
        self.br = F.branch("Calapan")
        self.rules = PayrollRule.objects.get(branch=self.br)
        self.emp = F.employee("Drei22", self.br, "JO", bio_id="5", monthly="0")
        self.emp.daily_rate = Dec("829.59")
        self.emp.has_premium = True
        self.emp.save()
        EmployeeContribution.objects.create(profile=self.emp, sss_amount=Dec("760"), pagibig_amount=Dec("400"))
        for d, p in OCT_12_16.items():
            F.day_punches(self.emp, D(2026, 10, d), *p)
        F.day_punches(self.emp, D(2026, 10, 19), *OCT_19)
        self.admin = F.admin("admin_calapan", self.br)
        self.client.force_login(self.admin)

    def process(self, period, scope="ALL"):
        r = self.client.post(reverse("admin_payroll_process_batch"),
                             {"period": period.id, "branch": self.br.id, "type": scope})
        self.assertEqual(r.status_code, 200, r.content)
        return PayrollItem.objects.get(profile=self.emp, batch_id=r.json()["batch_id"])


class MonthlyDtrOct19Tests(Drei22Base):
    def test_row_19_and_totals(self):
        period = F.period(D(2026, 10, 1), D(2026, 10, 31), "MONTHLY")
        dtr = views._build_dtr_and_summary(self.emp, self.br, period, self.rules)
        row = {r["date"]: r for r in dtr["rows"]}["2026-10-19"]
        self.assertEqual((row["am_in"], row["am_out"], row["pm_in"], row["pm_out"]), ("", "12:03 PM", "12:03 PM", "04:08 PM"))
        self.assertEqual(row["raw_log_count"], 3)          # one CHECK_OUT and one CHECK_IN at 12:03, separate records
        self.assertEqual((row["status"], row["deductible_minutes"], row["reference_minutes"]), ("Absent", 0, 52))
        self.assertTrue(row["needs_review"])
        self.assertEqual(dtr["deductible_minutes"], 818)    # 13 h 38 min, as before
        self.assertEqual(dtr["pending_review_days"], 1)
        self.assertTrue(any("2026-10-19" in i for i in dtr["issues"]))

    def test_printed_dtr_marks_the_reference_and_keeps_the_total(self):
        period = F.period(D(2026, 10, 1), D(2026, 10, 31), "MONTHLY")
        item = self.process(period)
        r = self.client.get(reverse("admin_payroll_item_dtr_print", args=[item.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "52*")
        self.assertContains(r, "Afternoon shortfall of 52 min shown for reference only")
        self.assertEqual((r.context["total_undertime_hours"], r.context["total_undertime_minutes"]), (13, 38))


class FirstHalfPayslipTests(Drei22Base):
    def setUp(self):
        super().setUp()
        self.period = F.period(D(2026, 10, 1), D(2026, 10, 15), "FIRST_HALF")
        self.item = self.process(self.period)
        self.cp = self.item.meta["computed_payroll"]

    def test_reconciliation_case(self):
        self.assertEqual(self.item.base_pay, Dec("3318.36"))
        self.assertEqual(self.item.premium_pay, Dec("663.67"))
        self.assertEqual(Dec(self.cp["gross"]), Dec("3982.03"))
        self.assertEqual(self.cp["attendance_time_minutes"], 722)
        self.assertEqual(Dec(self.cp["attendance_time_deduction"]), Dec("1248.25"))
        self.assertEqual(self.item.absences, 7)
        self.assertEqual(Dec(self.cp["absence_deduction"]), Dec("0.00"))   # JO base already excludes absent days
        self.assertEqual(Dec(self.item.meta["sss_amount"]), Dec("380.00"))
        self.assertEqual(Dec(self.item.meta["pagibig_amount"]), Dec("200.00"))
        self.assertEqual(Dec(self.item.meta["philhealth_amount"]), Dec("0.00"))
        self.assertEqual(self.item.tax_total, Dec("0.00"))
        self.assertEqual(self.item.deductions_total, Dec("1828.25"))
        self.assertEqual(self.item.net_pay, Dec("2153.78"))
        self.assertTrue(views.payroll_item_reconciles(self.item))

    def test_half_days_are_not_charged_twice(self):
        dtr = views._build_dtr_and_summary(self.emp, self.br, self.period, self.rules)
        self.assertEqual(dtr["half_days"], 2)
        self.assertEqual(self.cp["attendance_time_minutes"], dtr["deductible_minutes"])
        self.assertEqual(Dec(self.cp["late_deduction"]) + Dec(self.cp["undertime_deduction"]),
                         Dec(self.cp["attendance_time_deduction"]))

    def test_payslip_pages_show_one_combined_line(self):
        admin_slip = self.client.get(reverse("admin_payslip", args=[self.emp.id, self.period.id]))
        self.assertContains(admin_slip, "Late, Half-Day &amp; Undertime (722 min, 12h 02m)")
        self.assertContains(admin_slip, "1248.25")
        self.assertContains(admin_slip, "Absences (7)")
        self.assertNotContains(admin_slip, "Undertime (184 min)")
        self.assertContains(admin_slip, "PhilHealth (collected in 2nd half)")
        self.client.force_login(self.emp.user)
        emp_slip = self.client.get(reverse("employee_payslip_print", args=[self.item.id]))
        self.assertContains(emp_slip, "Late, Half-Day &amp; Undertime (722 min, 12h 02m)")

    def test_old_snapshot_shows_saved_parts_added_not_recalculated(self):
        meta = self.item.meta
        for key in ("attendance_time_deduction",):
            meta.pop(key, None)
            meta["computed_payroll"].pop(key, None)
        meta["late_deduction"], meta["undertime_deduction"] = "930.74", "318.32"
        PayrollItem.objects.filter(id=self.item.id).update(meta=meta)
        slip = self.client.get(reverse("admin_payslip", args=[self.emp.id, self.period.id]))
        self.assertContains(slip, "1249.06")


class MonthlyCollectionTests(Drei22Base):
    def setUp(self):
        super().setUp()
        self.h1 = F.period(D(2026, 10, 1), D(2026, 10, 15), "FIRST_HALF")
        self.h2 = F.period(D(2026, 10, 16), D(2026, 10, 31), "SECOND_HALF")

    def test_second_half_combines_both_halves_and_applies_floor(self):
        first = self.process(self.h1)
        second = self.process(self.h2)
        cp = second.meta["computed_payroll"]
        self.assertEqual(Dec(first.meta["computed_payroll"]["contribution_base"]), Dec("2733.78"))
        self.assertEqual(Dec(cp["monthly_contribution_base"]),
                         Dec("2733.78") + Dec(cp["contribution_base"]))
        self.assertEqual(Dec(second.meta["philhealth_amount"]), Dec("500.00"))   # 5% is below P500
        self.assertEqual(second.tax_total, Dec("0.00"))                          # below the cutoff
        self.assertEqual(second.meta["policy_blockers"], [])
        self.assertTrue(views.payroll_item_reconciles(second))

    def test_missing_first_half_is_reported(self):
        second = self.process(self.h2)
        self.assertIn("No processed 1st-half payroll found for October 2026", second.issues)
        self.assertEqual(Dec(second.meta["philhealth_amount"]), Dec("500.00"))

    def test_monthly_run_after_second_half_does_not_collect_again(self):
        self.process(self.h1)
        self.process(self.h2)
        monthly = self.process(F.period(D(2026, 10, 1), D(2026, 10, 31), "MONTHLY"))
        self.assertEqual(Dec(monthly.meta["philhealth_amount"]), Dec("0.00"))
        self.assertTrue(any("already handled" in b for b in monthly.meta["policy_blockers"]))
        errors = views._get_payroll_batch_finalization_errors(monthly.batch)
        self.assertTrue(any("already handled" in e for e in errors))

    def test_duplicate_first_half_items_prefer_finalized_and_block_when_ambiguous(self):
        all_item = self.process(self.h1, "ALL")
        jo_item = self.process(self.h1, "JO")
        meta = jo_item.meta
        meta["computed_payroll"]["contribution_base"] = "1000.00"
        PayrollItem.objects.filter(id=jo_item.id).update(meta=meta)

        second = self.process(self.h2)
        self.assertTrue(any("disagree" in b for b in second.meta["policy_blockers"]))

        PayrollBatch.objects.filter(id=all_item.batch_id).update(status=PayrollBatch.STATUS_FINALIZED)
        second = self.process(self.h2)
        self.assertEqual(second.meta["policy_blockers"], [])
        self.assertEqual(Dec(second.meta["computed_payroll"]["monthly_contribution_base"]),
                         Dec("2733.78") + Dec(second.meta["computed_payroll"]["contribution_base"]))


class TaxAboveCutoffTests(TestCase):
    def test_at_or_above_cutoff_blocks_finalization(self):
        br = F.branch("Calapan")
        emp = F.employee("high", br, "COS", bio_id="77", monthly="50000.00")
        for d in range(1, 32):
            day_ = D(2026, 10, d)
            if day_.weekday() < 5:
                F.day_punches(emp, day_, "08:00", "12:00", "13:00", "17:00")
        admin = F.admin("admin_calapan", br)
        self.client.force_login(admin)
        period = F.period(D(2026, 10, 1), D(2026, 10, 31), "MONTHLY")
        r = self.client.post(reverse("admin_payroll_process_batch"), {"period": period.id, "branch": br.id, "type": "ALL"})
        item = PayrollItem.objects.get(batch_id=r.json()["batch_id"], profile=emp)
        self.assertEqual(item.tax_total, Dec("0.00"))
        self.assertIn(P.TAX_PENDING_REASON, item.meta["policy_blockers"])
        self.assertTrue(views.payroll_item_reconciles(item))
        errors = views._get_payroll_batch_finalization_errors(item.batch)
        self.assertTrue(any("Tax pending client decision" in e for e in errors))


class PermanentReconcilesTests(TestCase):
    def test_permanent_item_reconciles(self):
        br = F.branch("Calapan")
        emp = F.employee("perm", br, "PERMANENT", bio_id="88", monthly="30000.00")
        F.day_punches(emp, D(2026, 10, 13), "08:00", "12:00", "13:00", "17:00")
        self.client.force_login(F.admin("admin_calapan", br))
        period = F.period(D(2026, 10, 1), D(2026, 10, 15), "FIRST_HALF")
        r = self.client.post(reverse("admin_payroll_process_batch"), {"period": period.id, "branch": br.id, "type": "ALL"})
        item = PayrollItem.objects.get(batch_id=r.json()["batch_id"], profile=emp)
        self.assertTrue(views.payroll_item_reconciles(item))
        self.assertEqual(item.meta["policy_blockers"], [])
