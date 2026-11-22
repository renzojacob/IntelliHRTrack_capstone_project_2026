"""Supervised target definitions for future analytics model training."""


PROTECTED_DTR_STATUSES = frozenset(
    {
        "Approved Leave",
        "Official Business",
        "Official Travel",
        "Holiday/Suspension",
        "Weekend",
        "Outside Employment Period",
    }
)


def is_unprotected_absence_row(row):
    """Return whether an existing deterministic DTR row is an Absent target.

    ``Absent`` is used only after the existing DTR builder has applied leave,
    travel, verified holiday/suspension, weekend, and employment-period rules.
    It is an operational analytics label, not a finding of misconduct and not
    a formally adjudicated "unexcused absence".
    """

    if not isinstance(row, dict):
        return False
    status = str(row.get("status") or "").strip()
    return status == "Absent" and status not in PROTECTED_DTR_STATUSES


def build_unprotected_absence_target(dtr_output):
    """Return 1 when the deterministic next-period DTR has an Absent row."""

    if isinstance(dtr_output, dict):
        rows = dtr_output.get("rows", [])
    else:
        rows = dtr_output or []
    return int(any(is_unprotected_absence_row(row) for row in rows))
