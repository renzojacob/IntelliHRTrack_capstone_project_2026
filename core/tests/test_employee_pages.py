"""
Employee portal: every page renders on the new design, forms still post,
and the employee's DTR printout is the same document the admin prints.

Run with:  python manage.py test core
"""
import datetime as dt
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.urls import reverse

from core.models import FinalizedDTR, LeaveRequest, PayrollItem
from core.tests import factories as F

D = dt.date
MNL = ZoneInfo("Asia/Manila")


class EmployeePortalTests(TestCase):
    def setUp(self):
        self.br = F.branch("Calapan")
        self.emp = F.employee("Drei22", self.br, "COS", bio_id="22")
        self.other = F.employee("mara", self.br, "JO", bio_id="40")
        self.period = F.period(D(2026, 10, 1), D(2026, 10, 15))
        for day, p in {12: ("10:21", "12:37", "12:37", "16:23"), 13: ("08:29", "12:36", "12:36", "16:16"),
                       14: ("11:07", "12:05", "12:05", "16:12"), 15: ("08:29", "12:04", "12:04", "16:05")}.items():
            F.day_punches(self.emp, D(2026, 10, day), *p)
        self.admin = F.admin("calapan_admin", self.br)

    def process_payroll(self):
        self.client.force_login(self.admin)
        r = self.client.post(reverse("admin_payroll_process_batch"),
                             {"period": self.period.id, "branch": self.br.id, "type": "ALL"})
        self.assertEqual(r.status_code, 200, r.content)
        self.client.force_login(self.emp.user)
        return PayrollItem.objects.get(profile=self.emp, batch__period=self.period)

    # ---------------------------------------------------------------- pages
    def test_every_page_uses_the_admin_design(self):
        self.client.force_login(self.emp.user)
        pages = {
            "employee_dashboard": {},
            "employee_attendance": {"start": "2026-10-12", "end": "2026-10-15"},
            "employee_payroll": {"period": self.period.id},
            "employee_leave": {},
            "employee_profile": {},
        }
        for name, params in pages.items():
            with self.subTest(page=name):
                r = self.client.get(reverse(name), params)
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, 'class="ihrt ad-body em-body"')
                self.assertContains(r, "ad-page-head")
                self.assertContains(r, 'aria-current="page"')
                self.assertContains(r, "css/admin.css")

    def test_attendance_shows_the_shared_computation(self):
        self.client.force_login(self.emp.user)
        r = self.client.get(reverse("employee_attendance"), {"start": "2026-10-12", "end": "2026-10-15"})
        self.assertContains(r, "View computation")
        self.assertContains(r, "Total: 240 + 37 = 277 minutes.")
        self.assertContains(r, "Total: 29 + 44 = 73 minutes.")
        self.assertContains(r, "12 h 02 min")          # deductible for the four days
        self.assertContains(r, "6 h 47 min")           # worked on Oct 13

    def test_payroll_page_reads_the_real_summary_keys(self):
        self.client.force_login(self.emp.user)
        r = self.client.get(reverse("employee_payroll"), {"period": self.period.id})
        summary = r.context["attendance_summary"]
        # The old page read days_present and rates.daily_rate, which do not exist.
        self.assertIn("present_days", summary)
        self.assertContains(r, f'<p class="ad-stat__value">{summary["present_days"]}</p>', html=False)
        self.assertContains(r, f"hourly &#8369;{r.context['rates']['hourly']}")
        self.assertContains(r, "12 h 02 min")

    def test_payroll_page_links_the_saved_payslip(self):
        item = self.process_payroll()
        r = self.client.get(reverse("employee_payroll"), {"period": self.period.id})
        self.assertContains(r, reverse("employee_payslip_print", args=[item.id]))
        self.assertTrue(r.context["using_saved_payroll"])

    # ---------------------------------------------------------------- leave
    def test_leave_submit_draft_and_cancel_still_work(self):
        self.client.force_login(self.emp.user)
        year = dt.date.today().year
        base = {"leave_type": "WELLNESS", "start_date": f"{year}-12-01", "end_date": f"{year}-12-02", "reason": "Rest"}
        self.client.post(reverse("employee_leave"), base)
        self.client.post(reverse("employee_leave"), {**base, "save_draft": "1"})
        statuses = sorted(LeaveRequest.objects.filter(employee=self.emp.user).values_list("status", flat=True))
        self.assertEqual(statuses, sorted([LeaveRequest.STATUS_PENDING, LeaveRequest.STATUS_DRAFT]))
        pending = LeaveRequest.objects.get(employee=self.emp.user, status=LeaveRequest.STATUS_PENDING)
        self.client.post(reverse("employee_leave_cancel", args=[pending.id]))
        pending.refresh_from_db()
        self.assertEqual(pending.status, LeaveRequest.STATUS_CANCELLED)
        page = self.client.get(reverse("employee_leave"))
        self.assertContains(page, 'name="save_draft"')
        self.assertContains(page, 'name="attachments"')
        self.assertContains(page, "Cancelled")

    # ---------------------------------------------------------------- payslip
    def test_payslip_uses_the_shared_document_and_stays_private(self):
        item = self.process_payroll()
        r = self.client.get(reverse("employee_payslip_print", args=[item.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Late and half day")
        self.assertContains(r, "ps-net")
        self.client.force_login(self.other.user)
        self.assertEqual(self.client.get(reverse("employee_payslip_print", args=[item.id])).status_code, 404)

    # ---------------------------------------------------------------- DTR print
    def test_dtr_print_is_the_admin_csf48_with_the_same_numbers(self):
        item = self.process_payroll()
        mine = self.client.get(reverse("employee_dtr_print"), {"start": "2026-10-01", "end": "2026-10-15"})
        self.assertEqual(mine.context["dtr_source_label"], "Processed Payroll Snapshot")
        self.assertContains(mine, "CIVIL SERVICE FORM NO. 48")
        self.client.force_login(self.admin)
        admins = self.client.get(reverse("admin_payroll_item_dtr_print", args=[item.id]))
        self.assertEqual(mine.context["dtr_rows"], admins.context["dtr_rows"])
        self.assertEqual(
            (mine.context["total_undertime_hours"], mine.context["total_undertime_minutes"]),
            (admins.context["total_undertime_hours"], admins.context["total_undertime_minutes"]),
        )
        self.assertEqual((mine.context["total_undertime_hours"], mine.context["total_undertime_minutes"]), (12, 2))

    def test_dtr_print_live_for_unprocessed_dates(self):
        self.client.force_login(self.emp.user)
        r = self.client.get(reverse("employee_dtr_print"), {"start": "2026-10-12", "end": "2026-10-15"})
        self.assertTrue(r.context["dtr_source_label"].startswith("Live calculation"))
        self.assertEqual(r.context["total_deductible_minutes"], 722)
        self.assertEqual([row["undertime_hour"] for row in r.context["dtr_rows"]], [4, 1, 4, 1])

    def test_dtr_print_prefers_a_locked_dtr(self):
        item = self.process_payroll()
        FinalizedDTR.objects.create(profile=self.emp, branch=self.br, period=self.period, payroll_item=item,
                                    rows=item.meta["dtr_rows"], summary={}, is_locked=True)
        r = self.client.get(reverse("employee_dtr_print"), {"start": "2026-10-01", "end": "2026-10-15"})
        self.assertEqual(r.context["dtr_source_label"], "Locked / Finalized DTR")
        self.assertTrue(r.context["dtr_locked"])

    # ---------------------------------------------------------------- dashboard
    def _dashboard_at(self, when):
        self.client.force_login(self.emp.user)
        with mock.patch("core.views.timezone.now", return_value=when):
            return self.client.get(reverse("employee_dashboard"))

    def test_late_today_follows_the_ceremony_rule(self):
        # Monday is the ceremony day: 8:05 is late. Tuesday: 8:05 is inside the allowance.
        F.punch(self.emp, D(2026, 10, 19), "08:05", "in")
        F.punch(self.emp, D(2026, 10, 20), "08:05", "in")
        mon = self._dashboard_at(dt.datetime(2026, 10, 19, 9, 0, tzinfo=MNL))
        tue = self._dashboard_at(dt.datetime(2026, 10, 20, 9, 0, tzinfo=MNL))
        self.assertTrue(mon.context["is_late_today"])
        self.assertEqual(mon.context["today_late_minutes"], 5)
        self.assertFalse(tue.context["is_late_today"])

    def test_late_today_moves_with_a_monday_holiday(self):
        F.non_working(D(2026, 10, 19), name="Holiday")
        F.punch(self.emp, D(2026, 10, 20), "08:05", "in")
        tue = self._dashboard_at(dt.datetime(2026, 10, 20, 9, 0, tzinfo=MNL))
        self.assertTrue(tue.context["is_late_today"])
        self.assertTrue(tue.context["today_is_ceremony_day"])
