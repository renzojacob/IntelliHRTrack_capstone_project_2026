"""Explainable, non-ML analytics rules.

Risk and data quality are intentionally separate.  A poor or incomplete DTR
does not become evidence that an employee is likely to be absent.
"""


def _bounded_score(value):
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        value = 0
    return max(0, min(100, value))


def calculate_attendance_risk(
    *,
    absences=0,
    late_days=0,
    incomplete_dtr_records=0,
    monday_friday_absences=0,
    holiday_adjacent_absences=0,
):
    """Return the corrected explainable attendance-risk baseline.

    This is not ML, a probability, or a disciplinary determination.  It keeps
    the existing analytics weights for unprotected absences, lateness,
    incomplete DTRs, Monday/Friday patterns, and holiday-adjacent absences.
    Approved leave, Official Business, Official Travel, and non-working dates
    are deliberately not inputs.
    """

    absences = max(0, int(absences or 0))
    late_days = max(0, int(late_days or 0))
    incomplete = max(0, int(incomplete_dtr_records or 0))
    monday_friday = max(0, int(monday_friday_absences or 0))
    holiday_adjacent = max(0, int(holiday_adjacent_absences or 0))

    score = 0
    reasons = []

    if absences:
        contribution = min(45, absences * 15)
        score += contribution
        reasons.append(
            f"{absences} unprotected Absent day(s) contributed {contribution} point(s)."
        )

    if late_days >= 2:
        contribution = min(25, late_days * 5)
        score += contribution
        reasons.append(
            f"{late_days} late day(s) contributed {contribution} point(s)."
        )

    if incomplete:
        contribution = min(20, incomplete * 8)
        score += contribution
        reasons.append(
            f"{incomplete} incomplete DTR record(s) contributed {contribution} point(s)."
        )

    if monday_friday:
        contribution = min(15, monday_friday * 5)
        score += contribution
        reasons.append(
            f"{monday_friday} Monday/Friday absence(s) contributed {contribution} point(s)."
        )

    if holiday_adjacent:
        contribution = min(12, holiday_adjacent * 4)
        score += contribution
        reasons.append(
            f"{holiday_adjacent} verified-holiday-adjacent absence(s) contributed "
            f"{contribution} point(s)."
        )

    score = _bounded_score(score)
    if score >= 70:
        category = "High"
    elif score >= 40:
        category = "Medium"
    else:
        category = "Low"

    if not reasons:
        reasons.append("No rule-based attendance-risk indicators were found.")

    return {"score": score, "category": category, "reasons": reasons}


def assess_data_quality(
    *,
    total_applicable_days=0,
    incomplete_dtr_records=0,
    usable_attendance_records=0,
    unavailable_features=None,
    historical_period_count=None,
):
    """Assess input completeness without changing the employee risk score."""

    total_days = max(0, int(total_applicable_days or 0))
    incomplete = max(0, int(incomplete_dtr_records or 0))
    usable_records = max(0, int(usable_attendance_records or 0))
    unavailable = sorted(set(unavailable_features or []))

    penalty = 0
    reasons = []

    if total_days:
        incomplete_rate = incomplete / total_days
        if incomplete_rate:
            deduction = min(40, int(round(incomplete_rate * 40)))
            penalty += deduction
            reasons.append(
                f"Incomplete DTRs affect {incomplete_rate:.1%} of applicable days."
            )
    elif incomplete:
        penalty += min(40, incomplete * 8)
        reasons.append("Incomplete DTRs exist but no applicable-day denominator is available.")

    if usable_records <= 0:
        penalty += 35
        reasons.append("No usable biometric attendance records were found for the period.")

    if unavailable:
        penalty += min(25, len(unavailable) * 5)
        reasons.append("Unavailable features: " + ", ".join(unavailable) + ".")

    if historical_period_count is not None and int(historical_period_count or 0) < 2:
        penalty += 20
        reasons.append("Fewer than two historical periods are available.")

    score = _bounded_score(100 - penalty)
    if score >= 85:
        status = "Good"
    elif score >= 60:
        status = "Review"
    else:
        status = "Poor"

    if not reasons:
        reasons.append("No material data-quality issues were detected.")

    return {"score": score, "status": status, "reasons": reasons}
