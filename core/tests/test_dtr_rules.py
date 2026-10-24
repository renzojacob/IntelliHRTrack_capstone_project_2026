"""
Unit tests for core/dtr_rules.py (no database).

Expected values come from the client's handwritten examples and the
corrections they gave for the production rows.  Examples whose policy is not
confirmed yet are skipped with the reason, not asserted.
"""
from datetime import date, time
from types import SimpleNamespace as N
from unittest import skip

from django.test import SimpleTestCase

from core import dtr_rules as R

# Default PayrollRule values: 8:00-5:00, lunch 12:00-1:00 unpaid, 8 hours, 15-minute grace, ceremony 8:00.
RULES = N(work_start_time=time(8, 0), lunch_start_time=time(12, 0), lunch_end_time=time(13, 0),
          work_end_time=time(17, 0), earliest_creditable_time=time(7, 0), lunch_break_required=True,
          daily_hours_required=8, grace_minutes_normal=15, flag_ceremony_cutoff_time=time(8, 0))
S = R.Schedule.from_rules(RULES)


def t(s):
    if s is None:
        return None
    h, m = map(int, s.split(":"))
    return time(h, m)


def day(am_in, am_out, pm_in, pm_out, ceremony=False, leave=None):
    return R.evaluate_day(S, t(am_in), t(am_out), t(pm_in), t(pm_out), is_ceremony_day=ceremony, half_day_leave=leave)


class ClientExamples(SimpleTestCase):
    def test_ceremony_day_801_to_500_is_one_minute(self):
        r = day("08:01", "12:00", "13:00", "17:00", ceremony=True)
        self.assertEqual((r.recorded_late, r.deductible, r.status), (1, 1, "Present"))

    def test_ordinary_815_to_500_is_15_short(self):
        r = day("08:15", "12:00", "13:00", "17:00")
        self.assertEqual((r.recorded_late, r.deductible), (0, 15))

    def test_ordinary_815_to_515_is_zero(self):
        r = day("08:15", "12:00", "13:00", "17:15")
        self.assertEqual((r.recorded_late, r.deductible, r.required_departure), (0, 0, "5:15 PM"))

    def test_handwritten_half_day_1001_to_423_is_277(self):
        r = day("10:01", "12:00", "13:00", "16:23", ceremony=True)
        self.assertEqual((r.status, r.half_day_penalty, r.departure_shortfall, r.deductible), ("Half Day", 240, 37, 277))
        self.assertIn("240 + 37 = 277", r.formula)

    def test_eight_to_eleven_is_absent(self):
        r = day("08:00", "11:00", None, None)
        self.assertEqual((r.status, r.below_minimum, r.deductible), ("Absent", True, 0))

    def test_eight_to_1215_is_half_day_240(self):
        r = day("08:00", "12:15", None, None)
        self.assertEqual((r.status, r.deductible), ("Half Day", 240))


class ProductionRowsAsClientCorrected(SimpleTestCase):
    """The four Drei22 rows, with the totals the client gave in the conversation."""

    def test_oct12_monday_1021_to_423_is_277(self):
        r = day("10:21", "12:37", "12:37", "16:23", ceremony=True)
        self.assertEqual((r.half_day_penalty, r.departure_shortfall, r.deductible), (240, 37, 277))

    def test_oct13_829_to_416_is_73(self):
        r = day("08:29", "12:36", "12:36", "16:16")
        self.assertEqual((r.recorded_late, r.arrival_shortfall, r.departure_shortfall, r.deductible), (14, 29, 44, 73))
        self.assertEqual(r.required_departure, "5:29 PM")

    def test_oct14_1107_to_412_is_288(self):
        r = day("11:07", "12:05", "12:05", "16:12")
        self.assertEqual((r.half_day_penalty, r.departure_shortfall, r.deductible), (240, 48, 288))

    def test_oct15_829_to_405_is_84(self):
        r = day("08:29", "12:04", "12:04", "16:05")
        self.assertEqual((r.arrival_shortfall, r.departure_shortfall, r.deductible), (29, 55, 84))

    def test_period_total_is_722_minutes(self):
        total = sum(day(*row).deductible for row in [
            ("10:21", "12:37", "12:37", "16:23", True), ("08:29", "12:36", "12:36", "16:16"),
            ("11:07", "12:05", "12:05", "16:12"), ("08:29", "12:04", "12:04", "16:05")])
        self.assertEqual(total, 722)
        self.assertEqual(R.hm(total), "12 h 02 min")


class Boundaries(SimpleTestCase):
    def test_grace_before_at_after(self):
        self.assertEqual(day("08:14", "12:00", "13:00", "17:00").recorded_late, 0)
        self.assertEqual(day("08:15", "12:00", "13:00", "17:00").recorded_late, 0)
        self.assertEqual(day("08:16", "12:00", "13:00", "17:00").recorded_late, 1)

    def test_ceremony_has_no_allowance(self):
        self.assertEqual(day("08:00", "12:00", "13:00", "17:00", ceremony=True).recorded_late, 0)
        self.assertEqual(day("08:15", "12:00", "13:00", "17:00", ceremony=True).recorded_late, 15)

    def test_half_day_cutoff_before_and_after(self):
        self.assertEqual(day("09:59", "12:00", "13:00", "17:00").status, "Present")
        self.assertEqual(day("10:01", "12:00", "13:00", "17:00").status, "Half Day")

    def test_half_day_cutoff_exactly_10(self):
        # Client: "lampas ng ten" (past ten) is a half day, so 10:00 itself is not.
        self.assertEqual(day("10:00", "12:00", "13:00", "17:00").status, "Present")

    def test_minimum_attendance_239_vs_240(self):
        self.assertEqual(day("08:01", "12:00", None, None).status, "Absent")      # 239 minutes
        self.assertEqual(day("08:00", "12:00", None, None).status, "Half Day")    # 240 minutes

    def test_afternoon_only_full_afternoon_is_half_day_240(self):
        r = day(None, None, "13:00", "17:00")
        self.assertEqual((r.status, r.deductible), ("Half Day", 240))


class LunchAndPunches(SimpleTestCase):
    def test_lunch_hour_is_never_credited(self):
        # Same punch for AM out and PM in (as in production): the 12:37 to 1:00 gap is not paid work.
        r = day("08:00", "12:37", "12:37", "17:00")
        self.assertEqual((r.actual_minutes, r.deductible), (480, 0))

    def test_missing_punch_is_flagged_not_invented(self):
        r = day("08:00", None, "13:00", "17:00")
        self.assertEqual((r.status, r.needs_review, r.deductible), ("Incomplete", True, 0))
        self.assertIn("AM departure", r.remarks)

    def test_unreadable_logs_are_flagged(self):
        r = day(None, None, None, None)
        self.assertEqual((r.status, r.needs_review), ("Incomplete", True))

    def test_staying_late_is_not_overtime(self):
        r = day("08:00", "12:00", "13:00", "19:00")
        self.assertEqual((r.deductible, r.credited_minutes, r.actual_minutes), (0, 480, 600))


class NoDoubleDeduction(SimpleTestCase):
    def test_lateness_inside_the_shortfall_is_not_added_again(self):
        r = day("08:29", "12:00", "13:00", "16:16")
        self.assertEqual(r.deductible, 73)                      # not 73 + 14
        self.assertEqual(r.arrival_shortfall + r.departure_shortfall, r.deductible)

    def test_absent_day_adds_no_minutes(self):
        self.assertEqual(day("08:00", "10:00", None, None).deductible, 0)

    def test_half_day_leave_is_not_a_half_day_penalty(self):
        r = day(None, None, "13:00", "17:00", leave="AM")
        self.assertEqual((r.status, r.deductible, r.half_day_penalty), ("Present", 0, 0))

    def test_half_day_leave_pm_with_late_morning(self):
        r = day("08:29", "12:00", None, None, leave="PM")
        self.assertEqual(r.deductible, 29)


class PendingPolicy(SimpleTestCase):
    @skip("Pending client: 8:29 AM arrival with 5:20 PM (or 5:28 PM) departure. Current rules give 9 (or 1) minutes; confirm.")
    def test_829_to_520(self):
        pass



def perm(am_in, am_out, pm_in, pm_out, ceremony=False):
    sched = S.for_employee("PERMANENT", ceremony)
    return R.evaluate_day(sched, t(am_in), t(am_out), t(pm_in), t(pm_out), is_ceremony_day=ceremony)


class PermanentSchedule(SimpleTestCase):
    """Client notes: Permanent, Tuesday to Friday 9:15 - 6:15; Monday 8:00 - 5:00."""

    def test_915_to_615_is_complete(self):
        r = perm("09:15", "12:00", "13:00", "18:15")
        self.assertEqual((r.recorded_late, r.deductible), (0, 0))

    def test_800_to_500_is_also_complete(self):
        r = perm("08:00", "12:00", "13:00", "17:00")
        self.assertEqual((r.recorded_late, r.deductible), (0, 0))

    def test_after_915_is_late(self):
        r = perm("09:30", "12:00", "13:00", "18:15")
        self.assertEqual((r.recorded_late, r.deductible, r.arrival_shortfall), (15, 15, 15))

    def test_leaving_early_is_shown_on_the_departure_side(self):
        r = perm("09:00", "12:00", "13:00", "17:30")
        self.assertEqual((r.deductible, r.arrival_shortfall, r.departure_shortfall, r.required_departure),
                         (30, 0, 30, "6:00 PM"))

    def test_ceremony_day_is_800_with_no_allowance(self):
        r = perm("08:10", "12:00", "13:00", "17:00", ceremony=True)
        self.assertEqual((r.recorded_late, r.deductible), (10, 10))

    def test_cos_is_unchanged(self):
        self.assertEqual(day("09:15", "12:00", "13:00", "18:15").recorded_late, 60)


class CeremonyDay(SimpleTestCase):
    MON = date(2026, 10, 12)

    def rec(self, kind, name="X"):
        return N(type=kind, name=name)

    def test_ordinary_monday(self):
        self.assertEqual(R.ceremony_day_for_week(self.MON + R.timedelta(days=3), {})[0], self.MON)

    def test_monday_holiday_moves_to_tuesday(self):
        cd, skipped = R.ceremony_day_for_week(self.MON, {self.MON: self.rec("holiday")})
        self.assertEqual(cd, date(2026, 10, 13))
        self.assertEqual(len(skipped), 1)

    def test_monday_suspension_moves_to_tuesday(self):
        self.assertEqual(R.ceremony_day_for_week(self.MON, {self.MON: self.rec("suspension")})[0], date(2026, 10, 13))

    def test_monday_and_tuesday_off_moves_to_wednesday(self):
        nw = {self.MON: self.rec("holiday"), date(2026, 10, 13): self.rec("suspension")}
        cd, skipped = R.ceremony_day_for_week(date(2026, 10, 16), nw)
        self.assertEqual(cd, date(2026, 10, 14))
        self.assertIn("Wednesday is this week's first working day because Monday", R.ceremony_reason(cd, skipped))

    def test_four_days_off_moves_to_friday(self):
        nw = {self.MON + R.timedelta(days=i): self.rec("holiday") for i in range(4)}
        self.assertEqual(R.ceremony_day_for_week(self.MON, nw)[0], date(2026, 10, 16))

    def test_whole_week_off_has_no_ceremony_and_no_carryover(self):
        nw = {self.MON + R.timedelta(days=i): self.rec("holiday") for i in range(5)}
        self.assertIsNone(R.ceremony_day_for_week(self.MON, nw)[0])
        self.assertEqual(R.ceremony_day_for_week(date(2026, 10, 19), nw)[0], date(2026, 10, 19))

    def test_midweek_date_uses_its_own_week(self):
        nw = {self.MON: self.rec("holiday")}
        self.assertEqual(R.ceremony_day_for_week(date(2026, 10, 15), nw)[0], date(2026, 10, 13))
