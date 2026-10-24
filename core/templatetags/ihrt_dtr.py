"""
Small display filters for DTR pages.

    {% load ihrt_dtr %}
    {{ row.total_minutes|hm }}     ->  "6 h 47 min"   (minutes // 60, minutes % 60)
    {{ row.status|status_tone }}   ->  "ok" / "warn" / "bad" / "info" / "neutral"
"""
from django import template

register = template.Library()


@register.filter
def hm(minutes):
    """Whole minutes as hours and minutes. Blank or bad values show 0 h 00 min."""
    try:
        m = max(0, int(minutes or 0))
    except (TypeError, ValueError):
        m = 0
    return f"{m // 60} h {m % 60:02d} min"


@register.filter
def status_tone(status):
    """Badge colour for a DTR status, matching the admin DTR dialog."""
    s = str(status or "").lower()
    if "incomplete" in s or "late" in s or "review" in s:
        return "warn"
    if "absent" in s or "missing" in s:
        return "bad"
    if any(word in s for word in ("travel", "holiday", "suspension", "leave", "business")):
        return "info"
    if "weekend" in s or "outside" in s or "no record" in s:
        return "neutral"
    return "ok"
