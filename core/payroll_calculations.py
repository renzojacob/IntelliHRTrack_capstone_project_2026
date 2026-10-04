"""Pure payroll calculation helpers.

The view layer owns database access and attendance collection.  This module
contains the money rules that must remain identical for previews, batches,
payslips, and tests.
"""

from calendar import monthrange
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CENT = Decimal("0.01")


def as_decimal(value, default="0.00"):
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def money(value):
    return as_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def is_monthly_period(pay_mode):
    return str(pay_mode or "").upper() == "MONTHLY"


def period_fraction(pay_mode):
    """Return the portion of a monthly amount due in one payroll period."""
    return Decimal("1.00") if is_monthly_period(pay_mode) else Decimal("0.50")


def recurring_amount_for_period(monthly_amount, pay_mode):
    return money(as_decimal(monthly_amount) * period_fraction(pay_mode))

def optional_monthly_deduction_for_period(
    configured_monthly_amount,
    minimum_monthly_amount,
    pay_mode,
):
    """
    Compute an optional JO/COS monthly deduction.

    Rules:
    - Zero means the employee did not choose/apply the deduction.
    - If a positive amount is configured below the allowed minimum,
      the minimum monthly amount is used.
    - Monthly amounts are prorated by payroll period.
    """

    configured = max(
        Decimal("0.00"),
        as_decimal(configured_monthly_amount),
    )

    minimum = max(
        Decimal("0.00"),
        as_decimal(minimum_monthly_amount),
    )

    if configured <= 0:
        return Decimal("0.00")

    effective_monthly_amount = max(
        configured,
        minimum,
    )

    return recurring_amount_for_period(
        effective_monthly_amount,
        pay_mode,
    )


def contractual_earned_compensation(
    gross_before_attendance,
    attendance_deduction,
):
    """
    JO/COS compensation actually earned after attendance deductions.

    This is useful as the base for client-configured percentage
    deductions such as PhilHealth and tax.
    """

    return money(
        max(
            Decimal("0.00"),
            as_decimal(gross_before_attendance)
            - as_decimal(attendance_deduction),
        )
    )

def _clock_minutes(value):
    """
    Convert a datetime.time value into minutes after midnight.
    """
    if value is None:
        return None

    return (
        (value.hour * 60)
        + value.minute
    )


def _work_segment_minutes(start_time, end_time):
    """
    Return positive minutes between two same-day clock times.
    Invalid/reversed segments return zero.
    """
    start_minutes = _clock_minutes(start_time)
    end_minutes = _clock_minutes(end_time)

    if start_minutes is None or end_minutes is None:
        return 0

    if end_minutes <= start_minutes:
        return 0

    return end_minutes - start_minutes


def credited_work_minutes(
    am_in,
    am_out,
    pm_in,
    pm_out,
    *,
    earliest_creditable_time,
    lunch_start_time,
    lunch_end_time,
    lunch_break_required=True,
):
    """
    Calculate ordinary creditable working minutes from the
    four Civil Service DTR punches:

        AM Arrival
        AM Departure
        PM Arrival
        PM Departure

    Rules:
    - Actual punches are not changed.
    - Ordinary work before earliest_creditable_time is not credited.
    - When lunch_break_required=True:
        * AM credit stops at lunch_start_time.
        * PM credit cannot begin before lunch_end_time.
    - AM and PM are calculated separately.
    - Time between AM Departure and PM Arrival is never
      automatically counted as working time.
    - Extra time does not erase separately calculated tardiness.
    """

    total_minutes = 0

    # -------------------------
    # AM work segment
    # -------------------------
    if am_in is not None and am_out is not None:
        credited_am_start = max(
            am_in,
            earliest_creditable_time,
        )

        credited_am_end = am_out

        if lunch_break_required:
            credited_am_end = min(
                credited_am_end,
                lunch_start_time,
            )

        total_minutes += _work_segment_minutes(
            credited_am_start,
            credited_am_end,
        )

    # -------------------------
    # PM work segment
    # -------------------------
    if pm_in is not None and pm_out is not None:
        credited_pm_start = pm_in

        if lunch_break_required:
            credited_pm_start = max(
                credited_pm_start,
                lunch_end_time,
            )

        total_minutes += _work_segment_minutes(
            credited_pm_start,
            pm_out,
        )

    return max(0, int(total_minutes))

def overtime_eligibility(
    approved_hours,
    current_dtr_row,
    previous_workday_row=None,
    *,
    required_minutes=480,
):
    """
    Determine how many approved OT minutes are actually payable.

    Client rules:
    - OT must be approved before this function is called.
    - Employee must actually work beyond the required daily hours.
    - Employee who is late that day cannot receive OT.
    - Employee absent on the previous applicable workday cannot receive OT.
    - Employee on Official Travel on the previous applicable workday
      cannot receive OT on the next applicable workday.

    The payable OT is also capped to the actual overtime proven by DTR.
    """

    approved_hours = max(
        Decimal("0.00"),
        as_decimal(approved_hours),
    )

    approved_minutes = int(
        (
            approved_hours
            * Decimal("60")
        ).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )

    current_row = (
        current_dtr_row
        if isinstance(current_dtr_row, dict)
        else {}
    )

    previous_row = (
        previous_workday_row
        if isinstance(previous_workday_row, dict)
        else {}
    )

    blockers = []
    notes = []

    current_status = str(
        current_row.get("status", "")
    ).strip()

    try:
        late_minutes = int(
            current_row.get("late", 0) or 0
        )
    except (TypeError, ValueError):
        late_minutes = 0

    try:
        worked_minutes = int(
            current_row.get("total_minutes", 0) or 0
        )
    except (TypeError, ValueError):
        worked_minutes = 0

    try:
        required_minutes = int(required_minutes or 480)
    except (TypeError, ValueError):
        required_minutes = 480

    previous_status = str(
        previous_row.get("status", "")
    ).strip()

    # -----------------------------------------
    # Same-day attendance requirement
    # -----------------------------------------
    if current_status != "Present":
        blockers.append(
            "OT date does not have a complete Present DTR"
        )

    # -----------------------------------------
    # Client rule: late employee cannot OT
    # -----------------------------------------
    if late_minutes > 0:
        blockers.append(
            f"Employee was late by {late_minutes} minute(s) on the OT date"
        )

    # -----------------------------------------
    # Previous applicable workday restrictions
    # -----------------------------------------
    if previous_status == "Absent":
        blockers.append(
            "Employee was absent on the previous applicable workday"
        )

    if previous_status == "Official Travel":
        blockers.append(
            "Employee was on Official Travel on the previous applicable workday"
        )

    # -----------------------------------------
    # OT must actually be beyond required hours
    # -----------------------------------------
    actual_overtime_minutes = max(
        0,
        worked_minutes - required_minutes,
    )

    if actual_overtime_minutes <= 0:
        blockers.append(
            "DTR does not show work beyond the required daily hours"
        )

    # -----------------------------------------
    # Final payable OT
    # -----------------------------------------
    if blockers:
        eligible_minutes = 0

    else:
        eligible_minutes = min(
            approved_minutes,
            actual_overtime_minutes,
        )

        if approved_minutes > actual_overtime_minutes:
            notes.append(
                "Approved OT was capped to the actual overtime proven by DTR"
            )

    return {
        "approved_minutes": int(approved_minutes),
        "actual_overtime_minutes": int(
            actual_overtime_minutes
        ),
        "eligible_minutes": int(eligible_minutes),
        "blockers": blockers,
        "notes": notes,
        "is_eligible": (
            eligible_minutes > 0
            and not blockers
        ),
    }


def _tax_from_brackets(taxable_compensation, brackets):
    taxable = max(Decimal("0.00"), as_decimal(taxable_compensation))

    for lower, upper, base_tax, rate in brackets:
        if upper is None or taxable <= upper:
            if taxable < lower:
                return Decimal("0.00")
            return money(base_tax + ((taxable - lower) * rate))

    return Decimal("0.00")


MONTHLY_WITHHOLDING_BRACKETS = (
    (Decimal("0"), Decimal("20833"), Decimal("0"), Decimal("0")),
    (Decimal("20833"), Decimal("33332.99"), Decimal("0"), Decimal("0.15")),
    (Decimal("33333"), Decimal("66666.99"), Decimal("1875.00"), Decimal("0.20")),
    (Decimal("66667"), Decimal("166666.99"), Decimal("8541.80"), Decimal("0.25")),
    (Decimal("166667"), Decimal("666666.99"), Decimal("33541.80"), Decimal("0.30")),
    (Decimal("666667"), None, Decimal("183541.80"), Decimal("0.35")),
)


SEMI_MONTHLY_WITHHOLDING_BRACKETS = (
    (Decimal("0"), Decimal("10417"), Decimal("0"), Decimal("0")),
    (Decimal("10417"), Decimal("16666.99"), Decimal("0"), Decimal("0.15")),
    (Decimal("16667"), Decimal("33332.99"), Decimal("937.50"), Decimal("0.20")),
    (Decimal("33333"), Decimal("83332.99"), Decimal("4270.70"), Decimal("0.25")),
    (Decimal("83333"), Decimal("333332.99"), Decimal("16770.70"), Decimal("0.30")),
    (Decimal("333333"), None, Decimal("91770.70"), Decimal("0.35")),
)


def bir_withholding_tax(taxable_compensation, pay_mode):
    """BIR Annex E withholding table effective 1 January 2023 onward."""
    brackets = (
        MONTHLY_WITHHOLDING_BRACKETS
        if is_monthly_period(pay_mode)
        else SEMI_MONTHLY_WITHHOLDING_BRACKETS
    )
    return _tax_from_brackets(taxable_compensation, brackets)


def permanent_statutory_contributions(monthly_basic_salary, pay_mode):
    """Compute employee/employer shares from monthly basic salary.

    PhilHealth uses the 5% premium with a P10,000 floor and P100,000
    ceiling; the premium is divided equally between employee and employer.
    Pag-IBIG uses 2% up to the P10,000 maximum fund salary.  GSIS uses the
    current standard 9% member and 12% employer shares.
    """
    basic = max(Decimal("0.00"), as_decimal(monthly_basic_salary))
    factor = period_fraction(pay_mode)

    philhealth_base = min(max(basic, Decimal("10000.00")), Decimal("100000.00"))
    philhealth_total = philhealth_base * Decimal("0.05")
    philhealth_employee = philhealth_total / Decimal("2")
    philhealth_employer = philhealth_total / Decimal("2")

    pagibig_base = min(basic, Decimal("10000.00"))
    pagibig_employee = pagibig_base * Decimal("0.02")
    pagibig_employer = pagibig_base * Decimal("0.02")

    gsis_employee = basic * Decimal("0.09")
    gsis_employer = basic * Decimal("0.12")

    return {
        "philhealth_employee": money(philhealth_employee * factor),
        "philhealth_employer": money(philhealth_employer * factor),
        "pagibig_employee": money(pagibig_employee * factor),
        "pagibig_employer": money(pagibig_employer * factor),
        "gsis_employee": money(gsis_employee * factor),
        "gsis_employer": money(gsis_employer * factor),
    }


def payroll_period_validation_error(start_date, end_date, pay_mode):
    """Validate calendar-month salary schedules before selection or processing."""
    if start_date > end_date:
        return "The start date cannot be later than the end date."
    if (start_date.year, start_date.month) != (end_date.year, end_date.month):
        return "Payroll dates must be within the same calendar month."
    last_day = monthrange(start_date.year, start_date.month)[1]
    expected = {
        "MONTHLY": (1, last_day, "Monthly requires the first through the last day of the month."),
        "FIRST_HALF": (1, 15, "1st half requires the 1st through the 15th of the month."),
        "SECOND_HALF": (16, last_day, "2nd half requires the 16th through the last day of the month."),
    }
    schedule = expected.get(str(pay_mode or "").upper())
    if schedule is None:
        return "Select a valid salary schedule."
    first, last, error = schedule
    if (start_date.day, end_date.day) != (first, last):
        return error
    return ""
