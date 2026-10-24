"""
Daily attendance rules for the DTR, payroll, and printouts.

Every screen that shows lateness, shortfall, or deductions gets its numbers
from evaluate_day() here, through views._build_dtr_and_summary().  Nothing in
this module reads or writes the database, so it can be tested directly.

All time arithmetic uses whole minutes after midnight (int).  Attendance
timestamps are never changed; this module only reads the four DTR punches.

Confirmed client rules implemented here
---------------------------------------
* Flag ceremony: 8:00 AM, no arrival allowance, on the FIRST WORKING DAY of
  each Monday-to-Friday week.  Verified holidays and work suspensions that
  apply to the branch push it to the next working day.  A fully non-working
  week has no ceremony day, and nothing carries over to the next week.
* Ordinary days: arrival allowance up to work start + grace (8:15 AM with the
  default branch settings).  The allowance does not remove the 8-hour work
  requirement.
* Daily deduction = minutes short of the required work time (8 hours), using
  credited work with the unpaid lunch excluded.  Client examples:
      8:01 AM - 5:00 PM on a ceremony day  ->   1 minute
      8:15 AM - 5:00 PM                    ->  15 minutes
      8:15 AM - 5:15 PM                    ->   0 minutes
      8:29 AM - 4:16 PM                    ->  73 minutes (29 + 44)
      8:29 AM - 4:05 PM                    ->  84 minutes (29 + 55)
* Arrival after the half-day cutoff (10:00 AM): the whole morning session is a
  half-day penalty (240 minutes) plus the afternoon shortfall before 5:00 PM.
      10:01 AM - 4:23 PM  ->  240 + 37 = 277
      11:07 AM - 4:12 PM  ->  240 + 48 = 288
* Permanent employees (client notes): Monday / ceremony day 8:00 AM - 5:00 PM;
  Tuesday to Friday 9:15 AM - 6:15 PM, read as flexible arrival up to 9:15 AM
  with the same 8 hours of work.  Arriving 8:00 and leaving 5:00 is complete.
* Fewer than 4 hours of actual eligible work: absent for the day (handled by
  the existing absence rules, so no minute deduction is added on top).
      8:00 AM - 11:00 AM  ->  absent
      8:00 AM - 12:15 PM  ->  half day, 240 minutes

Still waiting on the client (see PENDING_POLICY): the values below are the
current defaults and are listed on screen as pending where they matter.
"""

from dataclasses import dataclass, field, replace
from datetime import date, time, timedelta

from .payroll_calculations import credited_work_minutes

# -------------------------------------------------------------------------
# Settings that are not stored in PayrollRule yet
# -------------------------------------------------------------------------

# Fewer actual eligible minutes than this makes the day an absence.
MINIMUM_ATTENDANCE_MINUTES = 240

# Arriving AFTER this time triggers the morning half-day penalty.  Client:
# "lampas ng ten" (past ten), so exactly 10:00 AM is not a half day.
# Pending: whether permanent employees use a different cutoff.
HALF_DAY_ARRIVAL_AFTER = time(10, 0)

# Permanent employees on days that are not the flag ceremony day
# (client notes: "Permanent, Tues - Friday 9:15 - 6:15, Monday 8:00 - 5:00").
PERMANENT_LATEST_ARRIVAL = time(9, 15)

# Employment types that attend the flag ceremony.  The client notes give
# permanent employees 8:00 - 5:00 on Monday, so everyone is covered.
FLAG_CEREMONY_EMPLOYMENT_TYPES = frozenset({"JO", "COS", "PERMANENT"})

# Bump this when the rules change, so old saved DTR snapshots can be detected.
#   2: corrected lateness, half day, minimum attendance, first-working-day ceremony
#   3: permanent employees' Tuesday-Friday 9:15 AM flexible arrival
RULES_VERSION = 3


def row_is_current(row):
    """
    True when a saved DTR row already matches the current rules.
    Version 3 changed permanent employees only, so a version 2 row for a
    JO or COS employee is still current.
    """
    try:
        version = int(row.get("rules_version", 1) or 1)
    except (TypeError, ValueError):
        version = 1
    employment = str((row.get("computation") or {}).get("employment_type", "")).lower()
    needed = RULES_VERSION if employment.startswith("perm") else 2
    return version >= needed

PENDING_POLICY = (
    "Whether permanent employees' 9:15 AM is the latest flexible arrival (current reading) or a fixed start, "
    "and their half-day cutoff.",
    "Whether work before 8:00 AM counts toward the 8 hours (currently from the branch's earliest creditable time).",
    "Whether lateness after the allowance is still deducted when the employee makes up the time "
    "(8:29 AM arrival, 5:20 PM or 5:28 PM departure).",
)


# -------------------------------------------------------------------------
# Small helpers
# -------------------------------------------------------------------------

def minutes_of(t):
    """time -> minutes after midnight (seconds ignored); None stays None."""
    if t is None:
        return None
    return t.hour * 60 + t.minute


def as_time(m):
    m = max(0, min(int(m), 24 * 60 - 1))
    return time(m // 60, m % 60)


def clock(t):
    """9:05 AM style label for a time or minutes value."""
    if t is None:
        return "—"
    if isinstance(t, int):
        t = as_time(t)
    hour = t.hour % 12 or 12
    return f"{hour}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def hm(total_minutes):
    """Integer minutes -> '3 h 04 min' using // and %."""
    total_minutes = int(total_minutes)
    return f"{total_minutes // 60} h {total_minutes % 60:02d} min"


def _plural(n, word="minute"):
    return f"{n} {word}{'' if n == 1 else 's'}"


# -------------------------------------------------------------------------
# Flag ceremony day
# -------------------------------------------------------------------------

def week_monday(day):
    return day - timedelta(days=day.weekday())


def ceremony_day_for_week(day, non_working):
    """
    First working day (Monday to Friday) of the week containing `day`.

    non_working: dict {date: HolidaySuspension} of VERIFIED records that apply
    to this branch.  It must cover the whole week, even if the payroll period
    starts midweek.

    Returns (ceremony_date or None, skipped) where skipped is a list of
    (date, record) pairs for the non-working weekdays passed over.  None means
    every weekday is non-working: no ceremony that week, nothing carried over.
    The result does not depend on any employee's attendance.
    """
    monday = week_monday(day)
    skipped = []
    for offset in range(5):
        candidate = monday + timedelta(days=offset)
        record = non_working.get(candidate)
        if record is None:
            return candidate, skipped
        skipped.append((candidate, record))
    return None, skipped


def ceremony_reason(ceremony_date, skipped):
    """One sentence explaining why this is the week's ceremony day."""
    weekday = ceremony_date.strftime("%A")
    if not skipped:
        return f"{weekday} is this week's first working day."
    causes = []
    for d, rec in skipped:
        kind = {"holiday": "was a regular holiday", "suspension": "had a full-day work suspension",
                "special": "was a special non-working day"}.get(getattr(rec, "type", ""), "was a non-working day")
        name = getattr(rec, "name", "")
        causes.append(f"{d.strftime('%A')} ({d:%b %d}) {kind}" + (f" ({name})" if name else ""))
    joined = causes[0] if len(causes) == 1 else ", ".join(causes[:-1]) + " and " + causes[-1]
    return f"{weekday} is this week's first working day because {joined}."


# -------------------------------------------------------------------------
# Schedule for one day
# -------------------------------------------------------------------------

@dataclass(frozen=True)
class Schedule:
    work_start: int            # 8:00 AM
    lunch_start: int           # 12:00 PM
    lunch_end: int             # 1:00 PM
    work_end: int              # 5:00 PM
    earliest_credit: int       # work before this is not credited
    lunch_unpaid: bool
    required: int              # 480
    grace: int                 # 15
    ceremony_cutoff: int       # 8:00 AM
    flex_latest_arrival: int = None   # permanent Tue-Fri: 9:15 AM; None = work_start + grace

    def for_employee(self, employment_type, is_ceremony_day):
        """The schedule that applies to this employee on this day."""
        if str(employment_type or "").upper() == "PERMANENT" and not is_ceremony_day:
            return replace(self, flex_latest_arrival=minutes_of(PERMANENT_LATEST_ARRIVAL))
        return self

    @property
    def morning_session(self):
        return max(0, self.lunch_start - self.work_start)

    @property
    def afternoon_session(self):
        return max(0, self.work_end - self.lunch_end)

    @classmethod
    def from_rules(cls, rules):
        def t(value, default):
            return minutes_of(value) if value is not None else minutes_of(default)
        try:
            required = int(round(float(rules.daily_hours_required or 8) * 60))
        except (TypeError, ValueError):
            required = 480
        grace = rules.grace_minutes_normal if rules.grace_minutes_normal is not None else 15
        return cls(
            work_start=t(rules.work_start_time, time(8, 0)),
            lunch_start=t(rules.lunch_start_time, time(12, 0)),
            lunch_end=t(rules.lunch_end_time, time(13, 0)),
            work_end=t(rules.work_end_time, time(17, 0)),
            earliest_credit=t(rules.earliest_creditable_time, time(7, 0)),
            lunch_unpaid=bool(rules.lunch_break_required),
            required=required,
            grace=int(grace),
            ceremony_cutoff=t(rules.flag_ceremony_cutoff_time, time(8, 0)),
        )


# -------------------------------------------------------------------------
# One day's result
# -------------------------------------------------------------------------

@dataclass
class DayResult:
    status: str                         # Present | Half Day | Absent | Incomplete
    actual_minutes: int = 0             # eligible work actually done
    credited_minutes: int = 0           # counted for pay (required - deductible), capped at required
    recorded_late: int = 0              # tardiness record (AM vs allowance + PM vs 1:00 + grace)
    arrival_shortfall: int = 0          # deductible minutes on the arrival side (includes half-day penalty)
    departure_shortfall: int = 0        # deductible minutes on the departure side
    half_day_penalty: int = 0
    below_minimum: bool = False
    needs_review: bool = False
    required_departure: str = ""
    arrival_threshold: str = ""
    lines: list = field(default_factory=list)   # [(label, value)] for the breakdown
    formula: str = ""
    remarks: str = ""

    @property
    def deductible(self):
        return self.arrival_shortfall + self.departure_shortfall


def _session_credit(start, end, lo, hi):
    """Minutes of [start, end] that fall inside [lo, hi]."""
    if start is None or end is None:
        return 0
    return max(0, min(end, hi) - max(start, lo))


def evaluate_day(sched, am_in, am_out, pm_in, pm_out, *, is_ceremony_day, half_day_leave=None):
    """
    Evaluate one ordinary working day that has attendance punches.

    am_in .. pm_out: datetime.time (seconds already dropped) or None.
    half_day_leave:  None, "AM" or "PM" for an approved half-day leave.
    Weekends, holidays, travel, full-day leave and days with no punches are
    handled by the caller and never reach this function.
    """
    a_in, a_out, p_in, p_out = (minutes_of(x) for x in (am_in, am_out, pm_in, pm_out))
    lines = []

    if a_in is None and a_out is None and p_in is None and p_out is None:
        return DayResult(status="Incomplete", needs_review=True,
                         remarks="Attendance logs found, but none could be read as a check-in or check-out",
                         lines=[("Punches", "Logs exist but their in/out type is unknown. No time is assumed.")],
                         formula="Pending review: unreadable punches.")

    # --- Arrival threshold (recorded lateness only) ----------------------
    if is_ceremony_day:
        threshold = sched.ceremony_cutoff
        lines.append(("Arrival time required", f"{clock(threshold)} (flag ceremony, no allowance)"))
    elif sched.flex_latest_arrival is not None:
        threshold = sched.flex_latest_arrival
        latest_out = sched.lunch_end + sched.required - (sched.lunch_start - threshold)
        lines.append(("Arrival allowance", f"until {clock(threshold)} (permanent, Tuesday to Friday: "
                                           f"{clock(threshold)} – {clock(latest_out)}, 8 hours)"))
    else:
        threshold = sched.work_start + sched.grace
        lines.append(("Arrival allowance", f"until {clock(threshold)} ({clock(sched.work_start)} + {sched.grace} min)"))

    recorded_late = 0
    if a_in is not None and a_in > threshold:
        recorded_late += a_in - threshold
    pm_threshold = sched.lunch_end + sched.grace
    if p_in is not None and p_in > pm_threshold:
        recorded_late += p_in - pm_threshold

    result = DayResult(status="Present", recorded_late=recorded_late, arrival_threshold=clock(threshold))

    # --- Punch completeness: each session is either fully punched or empty
    am_partial = (a_in is None) != (a_out is None)
    pm_partial = (p_in is None) != (p_out is None)
    if am_partial or pm_partial:
        missing = [label for label, v in (("AM arrival", a_in), ("AM departure", a_out),
                                          ("PM arrival", p_in), ("PM departure", p_out)) if v is None]
        result.status = "Incomplete"
        result.needs_review = True
        result.remarks = "Missing required DTR punch(es): " + ", ".join(missing)
        lines.append(("Punches", "Missing " + ", ".join(missing) + ". No time is assumed."))
        lines.append(("Deduction", "Not computed. Correct or confirm the punches, then reprocess payroll."))
        result.lines = lines
        result.formula = "Pending review: missing punch."
        return result

    # --- Actual eligible work (lunch excluded) ---------------------------
    # Uses the same function payroll has always used for credited time.
    credit = dict(earliest_creditable_time=as_time(sched.earliest_credit),
                  lunch_start_time=as_time(sched.lunch_start), lunch_end_time=as_time(sched.lunch_end),
                  lunch_break_required=sched.lunch_unpaid)
    am_credit = credited_work_minutes(am_in, am_out, None, None, **credit)
    pm_credit = credited_work_minutes(None, None, pm_in, pm_out, **credit)
    actual = am_credit + pm_credit
    result.actual_minutes = actual

    lines.append(("Punches", f"AM {clock(a_in)} – {clock(a_out)}, PM {clock(p_in)} – {clock(p_out)}"))
    if sched.lunch_unpaid:
        lines.append(("Unpaid lunch", f"{clock(sched.lunch_start)} – {clock(sched.lunch_end)} is not counted as work"))
    if a_in is not None and a_in < sched.earliest_credit:
        lines.append(("Early arrival", f"Work before {clock(sched.earliest_credit)} is not credited"))
    lines.append(("Actual eligible work", f"{am_credit} + {pm_credit} = {actual} min ({hm(actual)})"))
    lines.append(("Required work", f"{sched.required} min ({hm(sched.required)})"))
    lines.append(("Recorded lateness", _plural(recorded_late) + (" (for the tardiness record; the deduction is below)" if recorded_late else "")))

    # --- Approved half-day leave covers one session ----------------------
    if half_day_leave in ("AM", "PM"):
        covered = sched.morning_session if half_day_leave == "AM" else sched.afternoon_session
        if half_day_leave == "AM":
            worked = _session_credit(p_in, p_out, sched.lunch_end, sched.work_end)
            target = sched.afternoon_session
            short = max(0, target - worked)
            result.departure_shortfall = short
            lines.append(("Approved leave", f"Morning on half-day leave: {covered} min credited"))
            lines.append(("Afternoon shortfall", f"{target} − {worked} = {short} min"))
        else:
            worked = _session_credit(a_in, a_out, sched.work_start, sched.lunch_start)
            target = sched.morning_session
            short = max(0, target - worked)
            result.arrival_shortfall = short
            lines.append(("Approved leave", f"Afternoon on half-day leave: {covered} min credited"))
            lines.append(("Morning shortfall", f"{target} − {worked} = {short} min"))
        result.status = "Present"
        result.remarks = f"Approved half-day leave ({half_day_leave})"
        result.credited_minutes = max(0, sched.required - result.deductible)
        result.formula = f"Total deductible: {result.deductible} min."
        result.lines = lines + [("Deductible", f"{result.deductible} min")]
        return result

    # --- Minimum attendance: fewer than 4 hours is an absence ------------
    if actual < MINIMUM_ATTENDANCE_MINUTES:
        result.status = "Absent"
        result.below_minimum = True
        result.remarks = f"Worked {hm(actual)}, below the {hm(MINIMUM_ATTENDANCE_MINUTES)} minimum"
        lines.append(("Classification", f"Absent: {actual} min is below the {MINIMUM_ATTENDANCE_MINUTES}-minute minimum"))
        lines.append(("Deduction", "Handled as one absence under the existing absence rules (no minutes added)"))
        result.lines = lines
        result.formula = f"{actual} < {MINIMUM_ATTENDANCE_MINUTES}: absent."
        return result

    # --- Morning half-day penalty: no morning, or arrival after cutoff ----
    cutoff = minutes_of(HALF_DAY_ARRIVAL_AFTER)
    if a_in is None or a_in > cutoff:
        penalty = sched.morning_session
        afternoon_done = _session_credit(p_in, p_out, sched.lunch_end, sched.work_end)
        afternoon_short = max(0, sched.afternoon_session - afternoon_done)
        result.status = "Half Day"
        result.half_day_penalty = penalty
        result.arrival_shortfall = penalty
        result.departure_shortfall = afternoon_short
        why = "no morning attendance" if a_in is None else f"arrived {clock(a_in)}, after {clock(cutoff)}"
        lines.append(("Morning half-day penalty", f"{penalty} min ({why})"))
        if p_out is not None and p_out < sched.work_end:
            lines.append(("Afternoon shortfall", f"{clock(sched.work_end)} − {clock(p_out)} = {afternoon_short} min"))
        else:
            lines.append(("Afternoon shortfall", f"{sched.afternoon_session} − {afternoon_done} = {afternoon_short} min"))
        result.required_departure = clock(sched.work_end)
        result.credited_minutes = max(0, sched.required - result.deductible)
        result.remarks = "Half day: late arrival" if a_in is not None else "Half day: afternoon only"
        result.formula = f"Total: {penalty} + {afternoon_short} = {result.deductible} minutes."
        result.lines = lines
        return result

    # --- Ordinary day: deduct what is short of the required work time ----
    deductible = max(0, sched.required - actual)
    credit_start = max(a_in, sched.earliest_credit)
    # Which part of the shortfall is shown as the arrival side: minutes after
    # 8:00 AM (client: "plus 29" for 8:29 AM), or after 9:15 AM for permanent
    # employees' flexible arrival.  The total is the same either way.
    arrival_ref = sched.flex_latest_arrival if sched.flex_latest_arrival is not None else sched.work_start
    arrival_offset = max(0, credit_start - arrival_ref)
    result.arrival_shortfall = min(deductible, arrival_offset)
    result.departure_shortfall = deductible - result.arrival_shortfall
    result.credited_minutes = sched.required - deductible

    if p_in is None:
        result.status = "Half Day"
        result.remarks = "Half day: morning only"
        result.required_departure = clock(sched.work_end)
        lines.append(("Classification", "Half day: no afternoon attendance"))
    else:
        needed_after_lunch = max(0, sched.required - am_credit)
        target = (sched.lunch_end if sched.lunch_unpaid else (a_out or sched.lunch_end)) + needed_after_lunch
        result.required_departure = clock(target)
        lines.append(("Departure needed for 8 hours", f"{clock(target)} (left {clock(p_out)})"))

    lines.append(("Shortfall", f"{sched.required} − {actual} = {deductible} min"))
    if deductible:
        parts = []
        if result.arrival_shortfall:
            parts.append(f"arrival {clock(credit_start)} vs {clock(arrival_ref)} = {result.arrival_shortfall}")
        if result.departure_shortfall:
            parts.append(f"end of day = {result.departure_shortfall}")
        lines.append(("Made up of", "; ".join(parts)))
        if result.arrival_shortfall and result.departure_shortfall:
            result.formula = f"Total: {result.arrival_shortfall} + {result.departure_shortfall} = {deductible} minutes."
        else:
            result.formula = f"Total: {deductible} minutes."
    else:
        result.formula = "Total: 0 minutes. The required work time was completed."
    result.lines = lines
    return result
