"""
Integration tests on real models: punches -> DTR rows -> payroll -> print.

Run with:  python manage.py test core
"""
import datetime as dt

from django.test import TestCase
from django.urls import reverse

from core import dtr_rules, views
from core.models import FinalizedDTR, PayrollItem, PayrollRule
from core.tests import factories as F

D = dt.date
MON, TUE, WED, THU, FRI = D(2026, 10, 12), D(2026, 10, 13), D(2026, 10, 14), D(2026, 10, 15), D(2026, 10, 16)


def rows_by_date(dtr):
    return {r["date"]: r for r in dtr["rows"]}


class CeremonyWithRealRecordsTests(TestCase):
    def setUp(self):
        self.br = F.branch("Calapan")
        self.other = F.branch("Puerto Princesa")
        self.emp = F.employee("ana", self.br, "COS", bio_id="31")
        self.rules = PayrollRule.objects.get(branch=self.br)

    def dtr(self, start, end):
        return views._build_dtr_and_summary(self.emp, self.br, F.period(start, end), self.rules)

    def test_ceremony_moves_to_wednesday_even_when_period_starts_wednesday(self):
        # Monday and Tuesday fall before the period but still decide the week.
        F.non_working(MON, kind="holiday", name="Regular holiday")
        F.non_working(TUE, kind="suspension", name="Work suspension")
        F.day_punches(self.emp, WED, "08:05", "12:00", "13:00", "17:00")
        F.day_punches(self.emp, THU, "08:05", "12:00", "13:00", "17:00")
        rows = rows_by_date(self.dtr(WED, THU))
        wed, thu = rows[WED.isoformat()], rows[THU.isoformat()]
        self.assertTrue(wed["is_ceremony_day"])
        self.assertIn("Monday", wed["ceremony_note"])
        self.assertIn("Tuesday", wed["ceremony_note"])
        self.assertEqual(wed["late"], 5)            # no allowance on the ceremony day
        self.assertEqual(wed["deductible_minutes"], 5)
        self.assertFalse(thu["is_ceremony_day"])
        self.assertEqual(thu["late"], 0)            # 8:05 is inside the normal allowance
        self.assertEqual(thu["deductible_minutes"], 5)  # but 5 minutes of the 8 hours are still missing

    def test_staying_later_makes_up_the_time_but_keeps_the_late_record(self):
        F.day_punches(self.emp, MON, "08:05", "12:00", "13:00", "17:05")
        mon = rows_by_date(self.dtr(MON, TUE))[MON.isoformat()]
        self.assertEqual((mon["late"], mon["deductible_minutes"]), (5, 0))

    def test_other_branch_holiday_has_no_effect(self):
        F.non_working(MON, br=self.other, name="Other branch holiday")
        F.day_punches(self.emp, MON, "08:05", "12:00", "13:00", "17:05")
        mon = rows_by_date(self.dtr(MON, TUE))[MON.isoformat()]
        self.assertTrue(mon["is_ceremony_day"])
        self.assertEqual(mon["late"], 5)

    def test_unverified_holiday_is_ignored(self):
        F.non_working(MON, verified=False, name="Unverified")
        F.day_punches(self.emp, MON, "08:05", "12:00", "13:00", "17:05")
        mon = rows_by_date(self.dtr(MON, TUE))[MON.isoformat()]
        self.assertTrue(mon["is_ceremony_day"])

    def test_employee_absence_does_not_move_the_ceremony(self):
        # No punches on Monday; Tuesday is an ordinary day for this employee.
        F.day_punches(self.emp, TUE, "08:05", "12:00", "13:00", "17:05")
        rows = rows_by_date(self.dtr(MON, TUE))
        self.assertTrue(rows[MON.isoformat()]["is_ceremony_day"])
        self.assertFalse(rows[TUE.isoformat()]["is_ceremony_day"])
        self.assertEqual(rows[TUE.isoformat()]["late"], 0)

    def test_full_non_working_week_has_no_ceremony_and_nothing_carries_over(self):
        for d in (MON, TUE, WED, THU, FRI):
            F.non_working(d, name="Week off")
        next_mon, next_tue = D(2026, 10, 19), D(2026, 10, 20)
        F.day_punches(self.emp, next_mon, "08:05", "12:00", "13:00", "17:05")
        rows = rows_by_date(self.dtr(MON, next_tue))
        self.assertFalse(any(rows[d.isoformat()]["is_ceremony_day"] for d in (MON, TUE, WED, THU, FRI)))
        self.assertTrue(rows[next_mon.isoformat()]["is_ceremony_day"])
        self.assertFalse(rows[next_tue.isoformat()]["is_ceremony_day"])


class ProductionExampleTests(TestCase):
    """The four production days from the screenshots, end to end."""

    def setUp(self):
        self.br = F.branch()
        self.emp = F.employee("Drei22", self.br, "COS", bio_id="22")
        for day, p in {12: ("10:21", "12:37", "12:37", "16:23"), 13: ("08:29", "12:36", "12:36", "16:16"),
                       14: ("11:07", "12:05", "12:05", "16:12"), 15: ("08:29", "12:04", "12:04", "16:05")}.items():
            F.day_punches(self.emp, D(2026, 10, day), *p)
        self.period = F.period(D(2026, 10, 1), D(2026, 10, 15))
        self.rules = PayrollRule.objects.get(branch=self.br)

    def test_rows_payroll_and_print_agree(self):
        dtr = views._build_dtr_and_summary(self.emp, self.br, self.period, self.rules)
        rows = rows_by_date(dtr)
        self.assertEqual([rows[f"2026-10-{d}"]["deductible_minutes"] for d in (12, 13, 14, 15)], [277, 73, 288, 84])
        self.assertEqual(dtr["deductible_minutes"], 722)

        pay = views._compute_payroll(self.emp, self.br, self.period, self.rules)["computed_payroll"]
        self.assertEqual(pay["late_minutes"] + pay["undertime_minutes"], 722)   # what payroll deducts
        self.assertEqual(pay["deductible_minutes"], 722)

        totals = views._dtr_totals(views._normalize_dtr_rows_for_print(dtr["rows"]))
        self.assertEqual(totals["total_minutes"], 722)                            # what the DTR prints
        self.assertEqual((totals["total_undertime_hours"], totals["total_undertime_minutes"]), (12, 2))

    def test_each_row_explains_itself(self):
        dtr = views._build_dtr_and_summary(self.emp, self.br, self.period, self.rules)
        r = rows_by_date(dtr)["2026-10-13"]
        self.assertEqual(r["rules_version"], dtr_rules.RULES_VERSION)
        self.assertEqual(r["computation"]["formula"], "Total: 29 + 44 = 73 minutes.")
        self.assertEqual(r["required_departure"], "5:29 PM")
        labels = [label for label, _ in r["computation"]["lines"]]
        self.assertTrue(any("lunch" in label.lower() for label in labels))

    def test_raw_punches_are_not_changed(self):
        before = list(self.emp.user.username for _ in [0])
        from core.models import AttendanceRecord
        snapshot = list(AttendanceRecord.objects.order_by("id").values_list("timestamp", "raw_row"))
        views._build_dtr_and_summary(self.emp, self.br, self.period, self.rules)
        views._compute_payroll(self.emp, self.br, self.period, self.rules)
        self.assertEqual(snapshot, list(AttendanceRecord.objects.order_by("id").values_list("timestamp", "raw_row")))
        self.assertEqual(before, ["Drei22"])


class PrintAllDtrsTests(TestCase):
    def setUp(self):
        self.br = F.branch("Calapan")
        self.other = F.branch("Puerto Princesa")
        self.period = F.period(D(2026, 10, 1), D(2026, 10, 15))
        self.ana = F.employee("ana", self.br, "COS", bio_id="31")
        self.ben = F.employee("ben", self.br, "JO", bio_id="32")         # no punches at all
        self.cara = F.employee("cara", self.other, "COS", bio_id="33")
        F.day_punches(self.ana, MON, "08:05", "12:00", "13:00", "17:05")
        self.branch_admin = F.admin("calapan_admin", self.br)
        self.superuser = F.admin("root", superuser=True)
        self.url = reverse("admin_payroll_dtr_print_all")

    def get(self, user, **params):
        self.client.force_login(user)
        return self.client.get(self.url, {"period": self.period.id, **params})

    def names(self, response):
        return [d["employee_name"] for d in response.context["dtrs"]]

    def test_prints_every_matching_employee_including_no_punches(self):
        r = self.get(self.branch_admin, branch=self.br.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.names(r), ["ana", "ben"])          # stable order, other branch excluded
        self.assertEqual(r.context["matched_count"], 2)
        ben = r.context["dtrs"][1]
        self.assertEqual(len(ben["dtr_rows"]), 15)                 # every day of the period, even with no punches
        self.assertContains(r, "pa-sheet", count=None)

    def test_employment_type_filter(self):
        r = self.get(self.branch_admin, branch=self.br.id, type="JO")
        self.assertEqual(self.names(r), ["ben"])

    def test_branch_admin_cannot_print_another_branch(self):
        r = self.get(self.branch_admin, branch=self.other.id)
        self.assertEqual(r.status_code, 403)

    def test_superuser_can_choose_any_branch(self):
        r = self.get(self.superuser, branch=self.other.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.names(r), ["cara"])

    def test_empty_state(self):
        r = self.get(self.branch_admin, branch=self.br.id, type="PERMANENT")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["matched_count"], 0)
        self.assertContains(r, "No employees match")

    def test_period_is_required(self):
        self.client.force_login(self.branch_admin)
        self.assertEqual(self.client.get(self.url, {"branch": self.br.id}).status_code, 404)

    def test_non_staff_is_redirected(self):
        self.client.force_login(self.ana.user)
        self.assertEqual(self.client.get(self.url, {"period": self.period.id}).status_code, 302)

    def test_does_not_query_once_per_employee(self):
        for i in range(8):
            F.employee(f"extra{i}", self.br, "COS", bio_id=f"9{i}")
        self.client.force_login(self.branch_admin)
        # Live calculation still reads punches per employee; this guards the lookups the
        # view itself adds (locked DTRs and saved payroll items) from growing per employee.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(self.url, {"period": self.period.id, "branch": self.br.id})
        lookups = [q["sql"] for q in ctx.captured_queries if '"core_finalizeddtr"' in q["sql"] or '"core_payrollitem"' in q["sql"]]
        self.assertLessEqual(len(lookups), 2)


class SnapshotTests(TestCase):
    """Bulk print uses the same saved snapshot as the individual Print DTR button."""

    def setUp(self):
        self.br = F.branch()
        self.period = F.period(D(2026, 10, 1), D(2026, 10, 15))
        self.emp = F.employee("Drei22", self.br, "COS", bio_id="22")
        F.day_punches(self.emp, D(2026, 10, 13), "08:29", "12:36", "12:36", "16:16")
        self.admin = F.admin("calapan_admin", self.br)
        self.client.force_login(self.admin)
        r = self.client.post(reverse("admin_payroll_process_batch"),
                             {"period": self.period.id, "branch": self.br.id, "type": "ALL"})
        self.assertEqual(r.status_code, 200, r.content)
        self.item = PayrollItem.objects.get(profile=self.emp, batch__period=self.period)

    def bulk(self):
        return self.client.get(reverse("admin_payroll_dtr_print_all"), {"period": self.period.id, "branch": self.br.id})

    def test_bulk_and_individual_print_the_same_rows(self):
        single = self.client.get(reverse("admin_payroll_item_dtr_print", args=[self.item.id]))
        bulk = self.bulk()
        d = bulk.context["dtrs"][0]
        self.assertEqual(d["source_label"], "Processed Payroll Snapshot")
        self.assertEqual(d["dtr_rows"], single.context["dtr_rows"])
        self.assertEqual(d["total_undertime_minutes"], single.context["total_undertime_minutes"])
        self.assertEqual(self.item.late_minutes + self.item.undertime_minutes, 73)
        self.assertTrue(single.context["snapshot_is_current"])

    def test_old_snapshot_is_flagged_not_rewritten(self):
        meta = self.item.meta
        for row in meta["dtr_rows"]:
            row.pop("rules_version", None)
            row.pop("deductible_minutes", None)
        PayrollItem.objects.filter(id=self.item.id).update(meta=meta)
        bulk = self.bulk()
        self.assertEqual(bulk.context["outdated"], ["Drei22"])
        single = self.client.get(reverse("admin_payroll_item_dtr_print", args=[self.item.id]))
        self.assertFalse(single.context["snapshot_is_current"])
        self.assertContains(single, "saved before")

    def test_relabelled_pages_render(self):
        batch = self.item.batch
        detail = self.client.get(reverse("admin_payroll_batch_detail", args=[batch.id]))
        self.assertContains(detail, "Late / half-day min")
        slip = self.client.get(reverse("admin_payslip", args=[self.emp.id, self.period.id]))
        self.assertContains(slip, "Late and half day")
        page = self.client.get(reverse("admin_payroll"), {"branch": self.br.id, "period": self.period.id})
        self.assertContains(page, "Deductible time")
        self.assertContains(page, "1 h 13 min")
        self.assertContains(page, "Late record 14 min")

    def test_employee_sees_the_same_computation(self):
        self.client.force_login(self.emp.user)
        r = self.client.get(reverse("employee_attendance"), {"start": "2026-10-13", "end": "2026-10-13"})
        self.assertContains(r, "View computation")
        self.assertContains(r, "Total: 29 + 44 = 73 minutes.")
        self.assertContains(r, "1 h 13 min")

    def test_locked_dtr_wins(self):
        FinalizedDTR.objects.create(profile=self.emp, branch=self.br, period=self.period, payroll_item=self.item,
                                    rows=self.item.meta["dtr_rows"], summary={}, is_locked=True)
        d = self.bulk().context["dtrs"][0]
        self.assertEqual(d["source_label"], "Locked / Finalized DTR")
        self.assertEqual(self.bulk().context["counts"]["locked"], 1)
