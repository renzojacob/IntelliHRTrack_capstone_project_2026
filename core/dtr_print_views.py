"""
Print All DTRs: one browser-printable document with every matching
employee's Civil Service Form No. 48.

Each employee's DTR comes from the same source the individual Print DTR
button uses, so the two always match:
  1. a locked FinalizedDTR for the period, otherwise
  2. the DTR saved in the employee's latest PayrollItem for the period
     (the same item the payroll page links to), otherwise
  3. a live calculation with the current rules, clearly labelled, for
     employees whose payroll has not been processed yet.
"""

from types import SimpleNamespace
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import views
from .models import FinalizedDTR, PayrollItem, PayrollPeriod, UserProfile

VALID_TYPES = {"ALL", UserProfile.EMP_JO, UserProfile.EMP_COS, UserProfile.EMP_PERMANENT}
TYPE_LABELS = dict(UserProfile.EMPLOYMENT_TYPE_CHOICES)


def _matching_profiles(branch, emp_type):
    """Same employee selection as the payroll page (views.admin_payroll)."""
    qs = UserProfile.objects.select_related("user", "branch").filter(
        is_approved=True,
        branch=branch,
        user__is_staff=False,
        user__is_superuser=False,
    )
    if emp_type != "ALL":
        qs = qs.filter(employment_type=emp_type)
    # Stable order: the payroll page's order, with the id as a tie-breaker.
    return qs.order_by("user__username", "id")


@login_required
def admin_payroll_dtr_print_all(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    # Branch permission is enforced here, not only in the button's link:
    # a branch administrator asking for another branch gets 403.
    branch = views._scoped_branch_for_admin_or_404(request, request.GET.get("branch"))
    if branch is None:
        raise PermissionDenied("You are not allowed to print DTRs for that branch.")

    period_id = request.GET.get("period")
    if not str(period_id or "").isdigit():
        raise Http404("Choose a payroll period first.")
    period = get_object_or_404(PayrollPeriod, id=int(period_id))

    emp_type = str(request.GET.get("type") or "ALL").upper()
    if emp_type not in VALID_TYPES:
        emp_type = "ALL"

    profiles = list(_matching_profiles(branch, emp_type))
    profile_ids = [p.id for p in profiles]

    # Three queries for everyone instead of three per employee.
    locked = {
        d.profile_id: d
        for d in FinalizedDTR.objects.filter(profile_id__in=profile_ids, period=period, is_locked=True)
    }
    latest_item = {}
    for item in (
        PayrollItem.objects.select_related("batch")
        .filter(batch__branch=branch, batch__period=period, profile_id__in=profile_ids)
        .order_by("profile_id", "-batch__processed_at", "-id")
    ):
        latest_item.setdefault(item.profile_id, item)

    rules = views._get_or_create_rules(branch)
    month_label = f"{period.start_date.strftime('%B %d, %Y')} - {period.end_date.strftime('%B %d, %Y')}"

    dtrs = []
    counts = {"locked": 0, "saved": 0, "live": 0}
    outdated = []

    for profile in profiles:
        item = latest_item.get(profile.id)
        meta = (item.meta or {}) if item else {}
        employee_meta = meta.get("employee", {}) if isinstance(meta.get("employee", {}), dict) else {}

        if profile.id in locked:
            rows = views._normalize_dtr_rows_for_print(locked[profile.id].rows)
            source, batch = "Locked / Finalized DTR", item.batch if item else SimpleNamespace(name="—")
            counts["locked"] += 1
        elif item:
            rows = views._normalize_dtr_rows_for_print(views._get_item_dtr_rows(item))
            source, batch = "Processed Payroll Snapshot", item.batch
            counts["saved"] += 1
        else:
            live = views._build_dtr_and_summary(profile, branch, period, rules)
            rows = views._normalize_dtr_rows_for_print(live.get("rows", []))
            source, batch = "Live calculation (payroll not processed)", SimpleNamespace(name="Not processed")
            counts["live"] += 1

        name = employee_meta.get("full_name") or profile.user.get_full_name() or profile.user.username
        current = views._rows_use_current_rules(rows)
        if not current:
            outdated.append(name)
        totals = views._dtr_totals(rows)

        dtrs.append({
            "employee_name": name,
            "biometric_employee_id": employee_meta.get("biometric_employee_id") or profile.biometric_employee_id or "",
            "division": employee_meta.get("department") or profile.department or "—",
            "employment_type": profile.employment_type,
            "month_label": month_label,
            "dtr_rows": rows,
            "total_undertime_hours": totals["total_undertime_hours"],
            "total_undertime_minutes": totals["total_undertime_minutes"],
            "batch": batch,
            "source_label": source,
            "current_rules": current,
        })

    back_url = f"{reverse('admin_payroll')}?{urlencode({'branch': branch.id, 'period': period.id, 'type': emp_type})}"

    return render(request, "admin/payroll_dtr_print_all.html", {
        "period": period,
        "branch": branch,
        "branch_label": branch.name,
        "employment_type": emp_type,
        "employment_type_label": TYPE_LABELS.get(emp_type, "All employment types"),
        "dtrs": dtrs,
        "matched_count": len(profiles),
        "counts": counts,
        "outdated": outdated,
        "back_url": back_url,
        "printed_by": request.user.get_full_name() or request.user.username,
        "printed_at": timezone.localtime(timezone.now()),
    })
