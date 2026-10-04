from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
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
    """Create or select the exact payroll date range chosen by an admin."""
    if not (request.user.is_staff or request.user.is_superuser):
        raise PermissionDenied

    branch = _payroll_period_branch_for_admin(request)
    if branch is None:
        messages.error(request, "No valid branch selected or assigned.")
        return redirect("admin_dashboard")

    start_text = str(request.POST.get("start_date") or "").strip()
    end_text = str(request.POST.get("end_date") or "").strip()
    pay_mode = str(request.POST.get("pay_mode") or "").strip().upper()
    payroll_url = f"{reverse('admin_payroll')}?branch={branch.id}"

    try:
        start_date = datetime.strptime(start_text, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_text, "%Y-%m-%d").date()
    except ValueError:
        messages.error(request, "Select a valid payroll start date and end date.")
        return redirect(payroll_url)

    if start_date > end_date:
        messages.error(request, "The start date cannot be later than the end date.")
        return redirect(payroll_url)

    valid_modes = {
        PayrollPeriod.PAY_MONTHLY,
        PayrollPeriod.PAY_FIRST_HALF,
        PayrollPeriod.PAY_SECOND_HALF,
    }
    if pay_mode not in valid_modes:
        messages.error(request, "Select a valid salary schedule.")
        return redirect(payroll_url)

    mode_labels = {
        PayrollPeriod.PAY_MONTHLY: "Monthly",
        PayrollPeriod.PAY_FIRST_HALF: "Semimonthly - 1st Half",
        PayrollPeriod.PAY_SECOND_HALF: "Semimonthly - 2nd Half",
    }
    period_name = (
        f"{start_date.strftime('%b %d, %Y')} - "
        f"{end_date.strftime('%b %d, %Y')} ({mode_labels[pay_mode]})"
    )

    selected_period = PayrollPeriod.objects.filter(
        start_date=start_date,
        end_date=end_date,
        pay_mode=pay_mode,
    ).order_by("id").first()

    created = selected_period is None
    if created:
        selected_period = PayrollPeriod.objects.create(
            name=period_name,
            start_date=start_date,
            end_date=end_date,
            pay_mode=pay_mode,
        )
        messages.success(
            request,
            f"Payroll dates set to {start_date:%b %d, %Y} through "
            f"{end_date:%b %d, %Y}.",
        )
    else:
        messages.info(
            request,
            f"Using the existing payroll dates {start_date:%b %d, %Y} through "
            f"{end_date:%b %d, %Y}.",
        )

    return redirect(
        f"{reverse('admin_payroll')}?branch={branch.id}"
        f"&period={selected_period.id}"
    )
