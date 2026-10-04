"""Branch-scoped administrator overtime authorization."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import OvertimeAuthorizationForm
from .models import FinalizedDTR, OvertimeRequest, PayrollBatch, PayrollItem, UserProfile


def _overtime_profiles_for_admin(user):
    if not (user.is_staff or user.is_superuser):
        raise PermissionDenied
    profiles = UserProfile.objects.select_related("user", "branch").filter(
        is_approved=True, user__is_staff=False, user__is_superuser=False,
        branch__isnull=False,
    )
    if not user.is_superuser:
        admin = UserProfile.objects.filter(user=user).first()
        if admin is None or admin.branch_id is None:
            return profiles.none()
        profiles = profiles.filter(branch_id=admin.branch_id)
    return profiles.order_by("user__username")


def overtime_authorization_context(user):
    profiles = _overtime_profiles_for_admin(user)
    return {
        "overtime_form": OvertimeAuthorizationForm(profiles=profiles),
        "overtime_requests": OvertimeRequest.objects.select_related(
            "profile__user", "profile__branch", "approved_by",
        ).filter(profile__in=profiles).order_by("-date", "-created_at")[:50],
    }


def _overtime_is_protected(profile, day):
    if FinalizedDTR.objects.filter(
        profile=profile, is_locked=True,
        period__start_date__lte=day, period__end_date__gte=day,
    ).exists():
        return True
    finalized_batches = PayrollBatch.objects.filter(
        branch_id=profile.branch_id, status=PayrollBatch.STATUS_FINALIZED,
        period__start_date__lte=day, period__end_date__gte=day,
    )
    return finalized_batches.filter(
        Q(employee_type_scope=PayrollBatch.SCOPE_ALL)
        | Q(employee_type_scope=profile.employment_type)
    ).exists() or PayrollItem.objects.filter(
        profile=profile, batch__in=finalized_batches,
    ).exists()


@login_required
@require_POST
def admin_overtime_authorize(request):
    profiles = _overtime_profiles_for_admin(request.user)
    form = OvertimeAuthorizationForm(request.POST, profiles=profiles)
    if not form.is_valid():
        for field, errors in form.errors.items():
            label = form.fields[field].label if field in form.fields else "Overtime"
            for error in errors:
                messages.error(request, f"{label}: {error}")
        return redirect("admin_biometrics_attendance")

    with transaction.atomic():
        profile = get_object_or_404(
            profiles.select_for_update(), pk=form.cleaned_data["profile"].pk,
        )
        day = form.cleaned_data["date"]
        if _overtime_is_protected(profile, day):
            messages.error(request, "Reopen the finalized payroll batch and unlock the affected DTR before changing overtime authorization.")
            return redirect("admin_biometrics_attendance")
        OvertimeRequest.objects.update_or_create(
            profile=profile, date=day,
            defaults={
                "hours": form.cleaned_data["hours"],
                "reason": form.cleaned_data["reason"],
                "approved": True, "approved_by": request.user,
            },
        )
    messages.success(request, "Overtime authorized. Only eligible hours actually rendered are payable, up to the authorized limit. Reprocess any existing draft/completed payroll for these dates to update its saved amounts.")
    return redirect("admin_biometrics_attendance")


@login_required
@require_POST
def admin_overtime_revoke(request, overtime_id):
    profiles = _overtime_profiles_for_admin(request.user)
    reason = str(request.POST.get("revoke_reason") or "").strip()
    with transaction.atomic():
        overtime = get_object_or_404(
            OvertimeRequest.objects.select_for_update(), pk=overtime_id,
            profile__in=profiles,
        )
        profile = get_object_or_404(profiles.select_for_update(), pk=overtime.profile_id)
        if not 5 <= len(reason) <= 80:
            messages.error(request, "Enter a revocation reason between 5 and 80 characters.")
            return redirect("admin_biometrics_attendance")
        if _overtime_is_protected(profile, overtime.date):
            messages.error(request, "Reopen the finalized payroll batch and unlock the affected DTR before revoking overtime.")
            return redirect("admin_biometrics_attendance")
        if not overtime.approved:
            messages.info(request, "This overtime entry is already not authorized.")
            return redirect("admin_biometrics_attendance")
        note = f"Revoked {timezone.localtime():%Y-%m-%d %H:%M} by {request.user.username[:30]}: {reason}"
        overtime.approved = False
        overtime.reason = f"{overtime.reason[:100]} | {note}"[:255]
        overtime.save(update_fields=["approved", "reason"])
    messages.success(request, "Overtime authorization revoked. Reprocess any existing draft/completed payroll for these dates to remove the saved overtime pay.")
    return redirect("admin_biometrics_attendance")
