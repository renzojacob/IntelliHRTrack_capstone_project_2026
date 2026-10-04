import calendar
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import Branch, PayrollPeriod, UserProfile


def _payroll_period_branch_for_admin(request):
    """Return only the branch this administrator is allowed to manage."""
    if request.user.is_superuser:
        branch_id = str(request.POST.get("branch") or "").strip()
        if not branch_id.isdigit():
            return None
        return get_object_or_404(Branch, pk=int(branch_id))

    try:
        return request.user.profile.branch
    except UserProfile.DoesNotExist:
        return None


@login_required
@require_POST
def admin_payroll_period_create(request):
    """Create one monthly period or both semimonthly periods."""
    if not (request.user.is_staff or request.user.is_superuser):
        raise PermissionDenied

    branch = _payroll_period_branch_for_admin(request)
    if branch is None:
        messages.error(request, "No valid branch selected or assigned.")
        return redirect("admin_dashboard")

    month_text = str(request.POST.get("month") or "").strip()
    schedule = str(request.POST.get("schedule") or "").strip().upper()
    payroll_url = f"{reverse('admin_payroll')}?branch={branch.id}"

    try:
        month_start = datetime.strptime(month_text, "%Y-%m").date().replace(day=1)
    except ValueError:
        messages.error(request, "Select a valid payroll month.")
        return redirect(payroll_url)

    if schedule not in {"MONTHLY", "SEMIMONTHLY"}:
        messages.error(request, "Select Monthly or Semimonthly.")
        return redirect(payroll_url)

    last_day = calendar.monthrange(month_start.year, month_start.month)[1]
    month_end = month_start.replace(day=last_day)
    month_label = month_start.strftime("%B %Y")
    existing_for_month = PayrollPeriod.objects.filter(
        start_date__lte=month_end,
        end_date__gte=month_start,
    )

    if schedule == "MONTHLY":
        conflicting = existing_for_month.exclude(
            pay_mode=PayrollPeriod.PAY_MONTHLY,
        ).exists()
        definitions = [
            (
                f"{month_label} Monthly",
                month_start,
                month_end,
                PayrollPeriod.PAY_MONTHLY,
            ),
        ]
    else:
        conflicting = existing_for_month.filter(
            pay_mode=PayrollPeriod.PAY_MONTHLY,
        ).exists()
        definitions = [
            (
                f"{month_label} - 1st Half",
                month_start,
                month_start.replace(day=15),
                PayrollPeriod.PAY_FIRST_HALF,
            ),
            (
                f"{month_label} - 2nd Half",
                month_start.replace(day=16),
                month_end,
                PayrollPeriod.PAY_SECOND_HALF,
            ),
        ]

    if conflicting:
        messages.error(
            request,
            f"{month_label} already uses a different payroll schedule. "
            "Use the existing periods instead.",
        )
        return redirect(payroll_url)

    created_periods = []
    existing_periods = []

    with transaction.atomic():
        for name, start_date, end_date, pay_mode in definitions:
            period, created = PayrollPeriod.objects.get_or_create(
                start_date=start_date,
                end_date=end_date,
                pay_mode=pay_mode,
                defaults={"name": name},
            )
            target = created_periods if created else existing_periods
            target.append(period)

    selected_period = (created_periods or existing_periods)[0]

    if created_periods:
        messages.success(
            request,
            f"Created {len(created_periods)} payroll period(s) for {month_label}.",
        )
    else:
        messages.info(request, f"The {month_label} payroll period already exists.")

    return redirect(
        f"{reverse('admin_payroll')}?branch={branch.id}"
        f"&period={selected_period.id}"
    )
