# core/views.py

import csv
import io
import re
from decimal import Decimal
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.db.models import Q

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .forms import AttendanceImportForm, AttendanceRecordForm
from .models import AttendanceRecord, UserProfile, Branch
from .models import LeaveRequest, LeaveAttachment

from .models import (
    Branch,
    AttendanceRecord,
    UserProfile,
    PayrollPeriod,
    PayrollRule,
    EmployeeContribution,
    HolidaySuspension,
    PayrollBatch,
    PayrollItem,
)


# =========================
# Small helpers
# =========================
def _get_admin_branch(request):
    """
    For staff admins, returns their assigned branch (or None).
    For superuser, returns None (meaning "all branches").
    """
    if request.user.is_superuser:
        return None
    try:
        return request.user.profile.branch
    except UserProfile.DoesNotExist:
        return None


def _scoped_branch_queryset_for_admin(request):
    """
    Superuser -> all branches
    Staff admin -> only their branch
    """
    if request.user.is_superuser:
        return Branch.objects.all().order_by("name")
    b = _get_admin_branch(request)
    if b:
        return Branch.objects.filter(id=b.id).order_by("name")
    return Branch.objects.none()


def _apply_branch_choices_to_form(form, branches_qs):
    """
    ✅ CRITICAL FIX:
    If form.branch is ModelChoiceField -> set queryset
    Else -> set choices to branch IDs
    """
    if not form or "branch" not in getattr(form, "fields", {}):
        return

    field = form.fields["branch"]

    # ModelChoiceField / ModelMultipleChoiceField
    if hasattr(field, "queryset"):
        field.queryset = branches_qs
        return

    # Plain ChoiceField fallback
    try:
        field.choices = [("", "Select branch")] + [(str(b.id), b.name) for b in branches_qs]
    except Exception:
        field.choices = [("", "Select branch")]


def _get_branch_from_post(request, branches_qs):
    """
    Get branch from POST safely:
    - supports 'branch' value being ID or name
    - ensures it exists inside the allowed branches_qs
    """
    raw = (request.POST.get("branch") or "").strip()
    if not raw:
        return None

    if raw.isdigit():
        return branches_qs.filter(id=int(raw)).first()

    return branches_qs.filter(name__iexact=raw).first()


# =========================
# Auth UI pages
# =========================
def login_ui(request):
    # Already logged in? Send to proper dashboard
    if request.user.is_authenticated:
        if request.user.is_staff or request.user.is_superuser:
            return redirect("admin_dashboard")
        return redirect("employee_dashboard")

    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""

        user = authenticate(request, username=username, password=password)
        if user is None:
            messages.error(request, "Invalid username or password.")
            return render(request, "auth/login.html")

        # ✅ Block employees if not approved
        if not (user.is_staff or user.is_superuser):
            try:
                if not user.profile.is_approved:
                    messages.error(
                        request,
                        "Your account is pending approval by your branch admin.",
                    )
                    return render(request, "auth/login.html")
            except UserProfile.DoesNotExist:
                messages.error(request, "Account profile missing. Contact admin.")
                return render(request, "auth/login.html")

        login(request, user)

        next_url = request.GET.get("next") or request.POST.get("next")
        if next_url:
            return redirect(next_url)

        if user.is_staff or user.is_superuser:
            return redirect("admin_dashboard")
        return redirect("employee_dashboard")

    return render(request, "auth/login.html")


def logout_ui(request):
    logout(request)
    return redirect("login_ui")


def signup_ui(request):
    """
    Employee signup:
    - user chooses branch (Branch FK)
    - user chooses employment type (COS / JO)
    - account created as PENDING (UserProfile.is_approved=False)
    - admin approves inside Employee Management
    """
    if request.user.is_authenticated:
        if request.user.is_staff or request.user.is_superuser:
            return redirect("admin_dashboard")
        return redirect("employee_dashboard")

    branches = Branch.objects.all().order_by("name")

    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        email = (request.POST.get("email") or "").strip()
        branch_id = (request.POST.get("branch") or "").strip()
        employment_type = (request.POST.get("employment_type") or "").strip().upper()

        password = request.POST.get("password") or ""
        password2 = request.POST.get("password2") or ""

        if not username:
            messages.error(request, "Username is required.")
            return render(request, "auth/signup.html", {"branches": branches})

        if not branch_id:
            messages.error(request, "Please select your branch.")
            return render(request, "auth/signup.html", {"branches": branches})

        if not employment_type:
            messages.error(request, "Please select your employment type.")
            return render(request, "auth/signup.html", {"branches": branches})

        if employment_type not in ("COS", "JO"):
            messages.error(request, "Invalid employment type selected.")
            return render(request, "auth/signup.html", {"branches": branches})

        if not password:
            messages.error(request, "Password is required.")
            return render(request, "auth/signup.html", {"branches": branches})

        if password != password2:
            messages.error(request, "Passwords do not match.")
            return render(request, "auth/signup.html", {"branches": branches})

        if len(password) < 8:
            messages.error(request, "Password must be at least 8 characters.")
            return render(request, "auth/signup.html", {"branches": branches})

        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already exists.")
            return render(request, "auth/signup.html", {"branches": branches})

        try:
            branch = Branch.objects.get(id=branch_id)

            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
            )

            # NOTE: assumes your real UserProfile has employment_type field.
            UserProfile.objects.create(
                user=user,
                branch=branch,
                employment_type=employment_type,
                is_approved=False,
            )

        except Branch.DoesNotExist:
            messages.error(request, "Invalid branch selected.")
            return render(request, "auth/signup.html", {"branches": branches})
        except Exception as e:
            messages.error(request, f"Signup failed: {e}")
            return render(request, "auth/signup.html", {"branches": branches})

        messages.success(
            request,
            "Account created! Please wait for your branch admin to approve your account.",
        )
        return redirect("login_ui")

    return render(request, "auth/signup.html", {"branches": branches})


# =========================
# Admin Dashboard UI pages
# =========================
@login_required
def admin_dashboard(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")
    return render(request, "admin/dashboard.html", {"current": "dashboard"})


@login_required
def admin_analytics(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")
    return render(request, "admin/analytics.html", {"current": "analytics"})


def _scoped_profiles_for_admin(request):
    """
    Admin visibility rule:
    - superuser: all branches
    - staff admin: only their branch
    """
    qs = UserProfile.objects.select_related("user", "branch").order_by("-created_at")
    if request.user.is_superuser:
        return qs
    try:
        admin_branch = request.user.profile.branch
        return qs.filter(branch=admin_branch)
    except UserProfile.DoesNotExist:
        return qs.none()


@login_required
def admin_employee_management(request):
    """
    SAME PAGE:
    - Pending profiles for approval
    - Approved profiles list
    - ✅ Employee Profiles CRUD table on the same page
    """
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    qs = _scoped_profiles_for_admin(request)

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        def _get_allowed_branch_from_post():
            if request.user.is_superuser:
                bid = (request.POST.get("branch_id") or "").strip()
                if not bid:
                    return None
                return Branch.objects.filter(id=bid).first()
            try:
                return request.user.profile.branch
            except UserProfile.DoesNotExist:
                return None

        if action == "create_employee":
            username = (request.POST.get("username") or "").strip()
            email = (request.POST.get("email") or "").strip()
            password = request.POST.get("password") or ""
            department = (request.POST.get("department") or "").strip()
            employment_type = (request.POST.get("employment_type") or "").strip().upper()
            branch = _get_allowed_branch_from_post()

            if not username:
                messages.error(request, "Username is required.")
                return redirect("admin_employees")

            if not branch:
                messages.error(request, "Branch is required.")
                return redirect("admin_employees")

            if employment_type not in ("COS", "JO"):
                messages.error(request, "Employment type must be COS or JO.")
                return redirect("admin_employees")

            if not password or len(password) < 8:
                messages.error(request, "Password is required (min 8 chars).")
                return redirect("admin_employees")

            if User.objects.filter(username=username).exists():
                messages.error(request, "Username already exists.")
                return redirect("admin_employees")

            try:
                with transaction.atomic():
                    user = User.objects.create_user(
                        username=username,
                        email=email,
                        password=password,
                    )
                    UserProfile.objects.create(
                        user=user,
                        branch=branch,
                        department=department,
                        employment_type=employment_type,
                        is_approved=True,
                    )
                messages.success(request, f"Employee created: {username}")
            except Exception as e:
                messages.error(request, f"Create failed: {e}")

            return redirect("admin_employees")

        elif action == "update_employee":
            profile_id = request.POST.get("profile_id")
            prof = get_object_or_404(
                UserProfile.objects.select_related("user", "branch"),
                id=profile_id,
            )

            if not request.user.is_superuser:
                try:
                    if prof.branch != request.user.profile.branch:
                        messages.error(request, "You can only edit employees in your branch.")
                        return redirect("admin_employees")
                except UserProfile.DoesNotExist:
                    messages.error(request, "Admin profile missing.")
                    return redirect("admin_employees")

            department = (request.POST.get("department") or "").strip()
            employment_type = (request.POST.get("employment_type") or "").strip().upper()
            email = (request.POST.get("email") or "").strip()

            if employment_type not in ("COS", "JO"):
                messages.error(request, "Employment type must be COS or JO.")
                return redirect("admin_employees")

            if request.user.is_superuser:
                bid = (request.POST.get("branch_id") or "").strip()
                if bid:
                    b = Branch.objects.filter(id=bid).first()
                    if b:
                        prof.branch = b

            prof.department = department
            prof.employment_type = employment_type
            prof.save()

            if email != prof.user.email:
                prof.user.email = email
                prof.user.save(update_fields=["email"])

            messages.success(request, f"Updated: {prof.user.username}")
            return redirect("admin_employees")

        elif action == "delete_employee":
            profile_id = request.POST.get("profile_id")
            prof = get_object_or_404(
                UserProfile.objects.select_related("user", "branch"),
                id=profile_id,
            )

            if not request.user.is_superuser:
                try:
                    if prof.branch != request.user.profile.branch:
                        messages.error(request, "You can only delete employees in your branch.")
                        return redirect("admin_employees")
                except UserProfile.DoesNotExist:
                    messages.error(request, "Admin profile missing.")
                    return redirect("admin_employees")

            username = prof.user.username
            prof.user.delete()
            messages.success(request, f"Deleted employee: {username}")
            return redirect("admin_employees")

        else:
            messages.error(request, "Invalid action.")
            return redirect("admin_employees")

    pending_profiles = qs.filter(is_approved=False)
    approved_profiles = qs.filter(is_approved=True)
    employee_profiles = qs.filter(
        user__is_staff=False,
        user__is_superuser=False,
    ).order_by("user__username")

    branches = Branch.objects.all().order_by("name")

    return render(
        request,
        "admin/employee_management.html",
        {
            "current": "employees",
            "pending_profiles": pending_profiles,
            "approved_profiles": approved_profiles,
            "employee_profiles": employee_profiles,
            "branches": branches,
        },
    )


@login_required
@require_POST
def approve_user(request, profile_id):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    prof = get_object_or_404(UserProfile, id=profile_id)

    if not request.user.is_superuser:
        try:
            if prof.branch != request.user.profile.branch:
                messages.error(request, "You can only approve accounts in your branch.")
                return redirect("admin_employees")
        except UserProfile.DoesNotExist:
            messages.error(request, "Admin profile missing.")
            return redirect("admin_employees")

    prof.is_approved = True
    prof.save()

    messages.success(request, f"Approved: {prof.user.username} ({prof.branch.name})")
    return redirect("admin_employees")


@login_required
@require_POST
def reject_user(request, profile_id):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    prof = get_object_or_404(UserProfile, id=profile_id)

    if not request.user.is_superuser:
        try:
            if prof.branch != request.user.profile.branch:
                messages.error(request, "You can only reject accounts in your branch.")
                return redirect("admin_employees")
        except UserProfile.DoesNotExist:
            messages.error(request, "Admin profile missing.")
            return redirect("admin_employees")

    username = prof.user.username
    branch_name = prof.branch.name if prof.branch else "—"
    prof.user.delete()
    messages.success(request, f"Rejected: {username} ({branch_name})")
    return redirect("admin_employees")


# =========================
# Leave Approval (Admin)
# =========================
@login_required
def admin_leave_approval(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    qs = LeaveRequest.objects.select_related(
        "employee", "branch", "reviewed_by"
    ).order_by("-created_at")

    if request.user.is_superuser:
        pass
    else:
        try:
            admin_profile = request.user.profile
        except UserProfile.DoesNotExist:
            messages.error(request, "Admin profile missing. Please contact superuser.")
            qs = qs.none()
        else:
            if not admin_profile.branch_id:
                messages.error(request, "Admin has no branch assigned. Please assign a branch.")
                qs = qs.none()
            else:
                qs = qs.filter(branch_id=admin_profile.branch_id)

    status_filter = (request.GET.get("status") or "").strip().upper()
    if status_filter:
        qs = qs.filter(status=status_filter)

    total_count = qs.count()
    approved_count = qs.filter(status=LeaveRequest.STATUS_APPROVED).count()
    rejected_count = qs.filter(status=LeaveRequest.STATUS_REJECTED).count()
    pending_count = qs.filter(status=LeaveRequest.STATUS_PENDING).count()
    draft_count = qs.filter(status=LeaveRequest.STATUS_DRAFT).count()
    cancelled_count = qs.filter(status=LeaveRequest.STATUS_CANCELLED).count()

    year = timezone.now().year

    def _days_within_year(start_date, end_date, year):
        from datetime import date as _date
        yr_start = _date(year, 1, 1)
        yr_end = _date(year, 12, 31)
        s = max(start_date, yr_start)
        e = min(end_date, yr_end)
        if e < s:
            return 0
        return (e - s).days + 1

    total_leave_used = 0.0
    for lr in qs.filter(status=LeaveRequest.STATUS_APPROVED):
        days = _days_within_year(lr.start_date, lr.end_date, year)
        if lr.duration in (LeaveRequest.DURATION_HALF_AM, LeaveRequest.DURATION_HALF_PM):
            total_leave_used += 0.5 * days
        else:
            total_leave_used += days

    pending_days = 0.0
    for lr in qs.filter(status=LeaveRequest.STATUS_PENDING):
        days = _days_within_year(lr.start_date, lr.end_date, year)
        if lr.duration in (LeaveRequest.DURATION_HALF_AM, LeaveRequest.DURATION_HALF_PM):
            pending_days += 0.5 * days
        else:
            pending_days += days

    notifications = []

    reviewed_qs = qs.filter(reviewed_at__isnull=False).order_by("-reviewed_at")[:3]
    for lr in reviewed_qs:
        emp = lr.employee.get_full_name() or lr.employee.username
        if lr.status == LeaveRequest.STATUS_APPROVED:
            notifications.append(
                f"✅ Approved {emp}'s {lr.get_leave_type_display()} ({lr.start_date.strftime('%b %d')} - {lr.end_date.strftime('%b %d')})"
            )
        elif lr.status == LeaveRequest.STATUS_REJECTED:
            notifications.append(
                f"❌ Rejected {emp}'s {lr.get_leave_type_display()} ({lr.start_date.strftime('%b %d')} - {lr.end_date.strftime('%b %d')})"
            )

    new_pending_qs = qs.filter(status=LeaveRequest.STATUS_PENDING, reviewed_at__isnull=True).order_by("-created_at")[:3]
    for lr in new_pending_qs:
        emp = lr.employee.get_full_name() or lr.employee.username
        notifications.insert(
            0,
            f"📋 New request from {emp}: {lr.get_leave_type_display()} ({lr.start_date.strftime('%b %d')} - {lr.end_date.strftime('%b %d')})"
        )
    notifications = notifications[:5]

    calendar_events = []
    for lr in qs.filter(status__in=[LeaveRequest.STATUS_APPROVED, LeaveRequest.STATUS_PENDING]):
        emp = lr.employee.username
        calendar_events.append({
            "start": lr.start_date.strftime("%b %d"),
            "title": f"{emp}: {lr.get_leave_type_display()[:3]}",
            "status": lr.status,
        })

    return render(
        request,
        "admin/leave_approval.html",
        {
            "current": "leave",
            "leave_requests": qs,
            "status_filter": status_filter,
            "total_count": total_count,
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "pending_count": pending_count,
            "draft_count": draft_count,
            "cancelled_count": cancelled_count,
            "total_leave_used": int(total_leave_used),
            "pending_days": int(pending_days),
            "notifications": notifications,
            "calendar_events": calendar_events,
        },
    )


@login_required
@require_POST
def admin_leave_approve(request, leave_id):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    lr = get_object_or_404(LeaveRequest, id=leave_id)

    if not request.user.is_superuser:
        try:
            admin_branch_id = request.user.profile.branch_id
        except UserProfile.DoesNotExist:
            messages.error(request, "Admin profile missing.")
            return redirect("admin_leave")
        if not admin_branch_id:
            messages.error(request, "Admin has no branch assigned.")
            return redirect("admin_leave")
        if lr.branch_id != admin_branch_id:
            messages.error(request, "You can only approve requests in your branch.")
            return redirect("admin_leave")

    if lr.status != LeaveRequest.STATUS_PENDING:
        messages.error(request, "Only pending requests can be approved.")
        return redirect("admin_leave")

    lr.status = LeaveRequest.STATUS_APPROVED
    lr.reviewed_by = request.user
    lr.reviewed_at = timezone.now()
    lr.admin_note = (request.POST.get("admin_note") or "").strip()
    lr.save()

    messages.success(request, f"Approved leave request of {lr.employee.username}.")
    return redirect("admin_leave")


@login_required
@require_POST
def admin_leave_reject(request, leave_id):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    lr = get_object_or_404(LeaveRequest, id=leave_id)

    if not request.user.is_superuser:
        try:
            admin_branch_id = request.user.profile.branch_id
        except UserProfile.DoesNotExist:
            messages.error(request, "Admin profile missing.")
            return redirect("admin_leave")
        if not admin_branch_id:
            messages.error(request, "Admin has no branch assigned.")
            return redirect("admin_leave")
        if lr.branch_id != admin_branch_id:
            messages.error(request, "You can only reject requests in your branch.")
            return redirect("admin_leave")

    if lr.status != LeaveRequest.STATUS_PENDING:
        messages.error(request, "Only pending requests can be rejected.")
        return redirect("admin_leave")

    lr.status = LeaveRequest.STATUS_REJECTED
    lr.reviewed_by = request.user
    lr.reviewed_at = timezone.now()
    lr.admin_note = (request.POST.get("admin_note") or "").strip()
    lr.save()

    messages.success(request, f"Rejected leave request of {lr.employee.username}.")
    return redirect("admin_leave")


# =========================
# Admin pages (simple renders)
# =========================
@login_required
def admin_payroll(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")
    return render(request, "admin/payroll.html", {"current": "payroll"})


@login_required
def admin_reports(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")
    return render(request, "admin/reports.html", {"current": "reports"})


@login_required
def admin_shift_scheduling(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")
    return render(request, "admin/shift_scheduling.html", {"current": "scheduling"})


@login_required
def admin_system_administration(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")
    return render(request, "admin/system_administration.html", {"current": "system"})


# =========================
# Employee pages (simple renders)
# =========================
@login_required
def employee_dashboard(request):
    return render(request, "employee/dashboard.html", {"current": "dashboard"})


@login_required
def employee_attendance(request):
    return render(request, "employee/attendance.html", {"current": "attendance"})


@login_required
def employee_schedule(request):
    return render(request, "employee/schedule.html", {"current": "schedule"})


@login_required
def employee_leave(request):
    try:
        if not (request.user.is_staff or request.user.is_superuser):
            if not request.user.profile.is_approved:
                messages.error(request, "Your account is pending approval by your branch admin.")
                return redirect("employee_dashboard")
    except UserProfile.DoesNotExist:
        messages.error(request, "Account profile missing. Contact admin.")
        return redirect("employee_dashboard")

    try:
        emp_branch = request.user.profile.branch
    except UserProfile.DoesNotExist:
        messages.error(request, "Profile/Branch missing. Contact admin.")
        return redirect("employee_dashboard")

    if request.method == "POST":
        leave_type = (request.POST.get("leave_type") or "").strip()
        start_date = (request.POST.get("start_date") or "").strip()
        end_date = (request.POST.get("end_date") or "").strip()
        duration = (request.POST.get("duration") or LeaveRequest.DURATION_FULL).strip()
        reason = (request.POST.get("reason") or "").strip()

        is_draft = bool(request.POST.get("save_draft"))

        if not leave_type:
            messages.error(request, "Please select a leave type.")
            return redirect("employee_leave")

        if not start_date or not end_date:
            messages.error(request, "Please select start and end date.")
            return redirect("employee_leave")

        if not reason:
            messages.error(request, "Please enter a reason.")
            return redirect("employee_leave")

        try:
            s = datetime.strptime(start_date, "%Y-%m-%d").date()
            e = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Invalid date format.")
            return redirect("employee_leave")

        if e < s:
            messages.error(request, "End date cannot be before start date.")
            return redirect("employee_leave")

        status = LeaveRequest.STATUS_DRAFT if is_draft else LeaveRequest.STATUS_PENDING

        lr = LeaveRequest.objects.create(
            employee=request.user,
            branch=emp_branch,
            leave_type=leave_type,
            start_date=s,
            end_date=e,
            duration=duration,
            reason=reason,
            status=status,
        )

        files = request.FILES.getlist("attachments")
        for f in files:
            LeaveAttachment.objects.create(leave_request=lr, file=f)

        messages.success(request, "Saved as draft." if is_draft else "Leave request submitted! Awaiting approval.")
        return redirect("employee_leave")

    leave_requests = (
        LeaveRequest.objects
        .filter(employee=request.user)
        .select_related("reviewed_by", "branch")
        .prefetch_related("attachments")
        .order_by("-created_at")
    )

    year = timezone.now().year

    total_count = leave_requests.count()
    approved_count = leave_requests.filter(status=LeaveRequest.STATUS_APPROVED).count()
    rejected_count = leave_requests.filter(status=LeaveRequest.STATUS_REJECTED).count()
    pending_count = leave_requests.filter(status=LeaveRequest.STATUS_PENDING).count()
    draft_count = leave_requests.filter(status=LeaveRequest.STATUS_DRAFT).count()
    cancelled_count = leave_requests.filter(status=LeaveRequest.STATUS_CANCELLED).count()

    def _days_within_year(start_date, end_date, year):
        from datetime import date as _date
        yr_start = _date(year, 1, 1)
        yr_end = _date(year, 12, 31)
        s = max(start_date, yr_start)
        e = min(end_date, yr_end)
        if e < s:
            return 0
        return (e - s).days + 1

    total_leave_used = 0.0
    for lr in leave_requests.filter(status=LeaveRequest.STATUS_APPROVED):
        days = _days_within_year(lr.start_date, lr.end_date, year)
        if lr.duration in (LeaveRequest.DURATION_HALF_AM, LeaveRequest.DURATION_HALF_PM):
            total_leave_used += 0.5 * days
        else:
            total_leave_used += days

    pending_days = 0.0
    for lr in leave_requests.filter(status=LeaveRequest.STATUS_PENDING):
        days = _days_within_year(lr.start_date, lr.end_date, year)
        if lr.duration in (LeaveRequest.DURATION_HALF_AM, LeaveRequest.DURATION_HALF_PM):
            pending_days += 0.5 * days
        else:
            pending_days += days

    remaining_leave = max(0, 5 - int(total_leave_used + pending_days))

    leave_notifications = []
    for lr in leave_requests.order_by("-reviewed_at"):
        if lr.status == LeaveRequest.STATUS_APPROVED and lr.reviewed_at:
            leave_notifications.append(
                f"✅ Your {lr.get_leave_type_display()} ({lr.start_date.strftime('%b %d')} - {lr.end_date.strftime('%b %d')}) was approved."
            )
        elif lr.status == LeaveRequest.STATUS_REJECTED and lr.reviewed_at:
            note = f" Note: {lr.admin_note}" if lr.admin_note else ""
            leave_notifications.append(
                f"❌ Your {lr.get_leave_type_display()} ({lr.start_date.strftime('%b %d')} - {lr.end_date.strftime('%b %d')}) was rejected.{note}"
            )
        elif lr.status == LeaveRequest.STATUS_PENDING:
            leave_notifications.append(
                f"⏳ Your {lr.get_leave_type_display()} ({lr.start_date.strftime('%b %d')} - {lr.end_date.strftime('%b %d')}) is awaiting approval."
            )
    leave_notifications = leave_notifications[:5]

    calendar_events = []
    for lr in leave_requests.filter(status__in=[LeaveRequest.STATUS_APPROVED, LeaveRequest.STATUS_PENDING]):
        calendar_events.append({
            "start": lr.start_date.strftime("%b %d"),
            "title": f"{lr.get_leave_type_display()}",
            "status": lr.status,
        })

    return render(
        request,
        "employee/leave.html",
        {
            "current": "leave",
            "leave_requests": leave_requests,
            "total_leave_used": int(total_leave_used),
            "pending_leave_count": pending_count,
            "remaining_leave": remaining_leave,
            "total_requests_count": total_count,
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "draft_count": draft_count,
            "cancelled_count": cancelled_count,
            "leave_notifications": leave_notifications,
            "calendar_events": calendar_events,
        },
    )


@login_required
@require_POST
def employee_leave_cancel(request, leave_id):
    lr = get_object_or_404(LeaveRequest, id=leave_id)

    if lr.employee_id != request.user.id:
        messages.error(request, "You are not allowed to cancel this request.")
        return redirect("employee_leave")

    if lr.status != LeaveRequest.STATUS_PENDING:
        messages.error(request, "Only pending requests can be cancelled.")
        return redirect("employee_leave")

    lr.status = LeaveRequest.STATUS_CANCELLED
    lr.reviewed_by = None
    lr.reviewed_at = None
    lr.save()

    messages.success(request, "Leave request cancelled.")
    return redirect("employee_leave")


@login_required
def employee_payroll(request):
    return render(request, "employee/payroll.html", {"current": "payroll"})


@login_required
def employee_analytics(request):
    return render(request, "employee/analytics.html", {"current": "analytics"})


@login_required
def employee_notifications(request):
    return render(request, "employee/notification.html", {"current": "notification"})


@login_required
def employee_profile(request):
    return render(request, "employee/setting_&_profile.html", {"current": "profile"})


# =========================
# Biometrics helpers (Normalization)
# =========================
def _norm_key(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\ufeff", "")
    s = s.replace("\xa0", " ")
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _norm_val(v) -> str:
    if v is None:
        return ""
    return str(v).replace("\xa0", " ").strip()


def _row_norm_dict(row: dict) -> dict:
    out = {}
    for k, v in (row or {}).items():
        out[_norm_key(k)] = _norm_val(v)
    return out


def _normalize_status(value: str) -> str:
    if not value:
        return AttendanceRecord.STATUS_UNKNOWN

    v = str(value).strip().upper()

    checkin = {"IN", "CHECKIN", "CHECK-IN", "CHECK IN", "TIME IN", "CLOCK IN", "ENTRY", "CHECK IN "}
    checkout = {"OUT", "CHECKOUT", "CHECK-OUT", "CHECK OUT", "TIME OUT", "CLOCK OUT", "EXIT", "CHECK OUT "}
    noneish = {"NONE", "N/A", "NA", "NULL", "-", "UNKNOWN"}

    if v in checkin:
        return AttendanceRecord.STATUS_CHECKIN
    if v in checkout:
        return AttendanceRecord.STATUS_CHECKOUT
    if v in noneish:
        return AttendanceRecord.STATUS_UNKNOWN

    if "CHECK" in v and "IN" in v:
        return AttendanceRecord.STATUS_CHECKIN
    if "CHECK" in v and "OUT" in v:
        return AttendanceRecord.STATUS_CHECKOUT

    return AttendanceRecord.STATUS_UNKNOWN


def _ensure_aware(dt: datetime) -> datetime:
    """
    ✅ Payroll fix: ensure timezone-awareness consistently when USE_TZ=True.
    """
    if not dt:
        return dt
    if settings.USE_TZ and timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _parse_timestamp(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return _ensure_aware(value)

    s = str(value).strip()

    dt = parse_datetime(s)
    if dt:
        return _ensure_aware(dt)

    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ]
    for f in fmts:
        try:
            return _ensure_aware(datetime.strptime(s, f))
        except ValueError:
            continue

    return None


def _status_label(value: str) -> str:
    return dict(AttendanceRecord.ATTENDANCE_STATUS_CHOICES).get(value, value)


def _read_csv(file_obj):
    raw = file_obj.read()
    encodings = ["utf-8-sig", "utf-8", "cp1252", "latin-1", "iso-8859-1"]
    text = None

    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue

    if text is None:
        raise ValueError("Could not decode CSV file. Try saving as UTF-8.")

    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        cleaned = {}
        for k, v in (row or {}).items():
            key = (k or "").strip()
            val = v.strip() if isinstance(v, str) else v
            cleaned[key] = val
        if any(str(x).strip() for x in cleaned.values() if x is not None):
            rows.append(cleaned)

    return rows


def _df_to_rows(df):
    df = df.fillna("")
    rows = []
    for _, r in df.iterrows():
        row = {}
        for k in df.columns:
            row[str(k).strip()] = str(r[k]).strip() if str(r[k]).strip() else ""
        if any(str(v).strip() for v in row.values()):
            rows.append(row)
    return rows


def _find_header_row_in_df(df):
    for i in range(len(df.index)):
        row_vals = [str(x).strip().lower() for x in df.iloc[i].tolist()]
        if "person id" in row_vals:
            return i
    return None


def _read_excel(file_obj, filename: str):
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("Install deps: pip install pandas openpyxl xlrd==2.0.1 lxml")

    file_obj.seek(0)
    raw = file_obj.read()
    head = raw[:500].lstrip().lower()

    # HTML-as-XLS detection (Hikvision exports sometimes)
    if (
        head.startswith(b"<html")
        or head.startswith(b"<!doctype")
        or head.startswith(b"<table")
        or b"<html" in head
    ):
        text = None
        for enc in ["utf-8-sig", "utf-8", "cp1252", "latin-1"]:
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("File looks like HTML but could not decode it.")

        try:
            header_tables = pd.read_html(io.StringIO(text), attrs={"class": "Detail1"}, header=None)
            data_tables = pd.read_html(io.StringIO(text), attrs={"class": "Detail2"}, header=None)

            if header_tables and data_tables:
                df_h = header_tables[0].fillna("")
                header_row_idx = _find_header_row_in_df(df_h)
                if header_row_idx is None:
                    raise ValueError("Could not find header row in Detail1 table.")

                headers = [str(x).strip() for x in df_h.iloc[header_row_idx].tolist()]
                headers = [h if h else f"col_{i}" for i, h in enumerate(headers)]

                df_d = pd.concat([d.fillna("") for d in data_tables], ignore_index=True)
                headers = headers[: len(df_d.columns)]
                df_d.columns = headers

                first_col = headers[0] if headers else None
                if first_col:
                    df_d = df_d[df_d[first_col].astype(str).str.strip().str.lower() != "person id"]

                df_d = df_d.replace("", None).dropna(how="all").fillna("")
                return _df_to_rows(df_d)
        except Exception:
            pass

        try:
            tables = pd.read_html(io.StringIO(text), header=None)
            if not tables:
                raise ValueError("No tables found in HTML file.")

            candidates = [t for t in tables if t.shape[1] >= 5]
            df_best = max(candidates, key=lambda d: d.shape[0]) if candidates else max(tables, key=lambda d: d.shape[0])

            row0 = [str(x).strip().lower() for x in df_best.iloc[0].tolist()]
            if "person id" in row0 and "time" in row0:
                df_best.columns = [str(x).strip() for x in df_best.iloc[0].tolist()]
                df_best = df_best.iloc[1:].reset_index(drop=True)

            df_best = df_best.fillna("")
            return _df_to_rows(df_best)
        except Exception as e:
            raise ValueError(f"HTML-as-Excel detected but failed to parse tables: {e}")

    # Real Excel
    ext = filename.lower().split(".")[-1]
    engine = "openpyxl" if ext == "xlsx" else "xlrd" if ext == "xls" else None

    try:
        import pandas as pd
        import io as _io

        df = pd.read_excel(_io.BytesIO(raw), sheet_name=0, engine=engine)
        return _df_to_rows(df)
    except Exception as e:
        raise ValueError(f"Error reading Excel file: {e}")


def _map_row(row: dict, branch_obj: Branch) -> dict:
    r = _row_norm_dict(row)

    employee_id = (
        r.get("person id")
        or r.get("personid")
        or r.get("employee id")
        or r.get("employeeid")
        or r.get("id")
        or ""
    )
    employee_id = str(employee_id).strip().lstrip("'").strip()

    full_name = (r.get("name") or r.get("full name") or r.get("fullname") or "").strip()
    department = (r.get("department") or r.get("dept") or "").strip()

    ts_raw = (r.get("time") or r.get("date time") or r.get("timestamp") or "").strip()
    ts = _parse_timestamp(ts_raw)

    status_raw = (r.get("attendance status") or r.get("status") or r.get("event type") or "").strip()
    status = _normalize_status(status_raw)

    return {
        "employee_id": employee_id,
        "full_name": full_name,
        "department": department,
        "branch": branch_obj,
        "timestamp": ts,
        "attendance_status": status,
        "raw_row": row,
    }


SESSION_KEY = "attendance_import_cache_v2"


def _save_import_cache(request, branch_obj: Branch, skip_duplicates: bool, rows: list):
    cached = {
        "branch_id": branch_obj.id if branch_obj else None,
        "skip_duplicates": bool(skip_duplicates),
        "rows": [],
    }

    for row in rows:
        mapped = _map_row(row, branch_obj=branch_obj)
        cached["rows"].append(
            {
                "employee_id": mapped["employee_id"],
                "full_name": mapped["full_name"],
                "department": mapped["department"],
                "branch_id": branch_obj.id if branch_obj else None,
                "timestamp": mapped["timestamp"].isoformat(sep=" ") if mapped["timestamp"] else "",
                "attendance_status": mapped["attendance_status"],
                "raw_row": mapped.get("raw_row", {}),
            }
        )

    request.session[SESSION_KEY] = cached
    request.session.modified = True


def _load_import_cache(request):
    return request.session.get(SESSION_KEY)


def _clear_import_cache(request):
    if SESSION_KEY in request.session:
        del request.session[SESSION_KEY]
        request.session.modified = True


# =========================
# Biometrics page
# =========================
@login_required
def admin_biometrics_attendance(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    branches_qs = _scoped_branch_queryset_for_admin(request)

    records_qs = AttendanceRecord.objects.select_related("branch").all()
    admin_branch = _get_admin_branch(request)
    if admin_branch:
        records_qs = records_qs.filter(branch=admin_branch)

    records = records_qs.order_by("-timestamp")[:100]

    kpi = {
        "present": records_qs.filter(attendance_status=AttendanceRecord.STATUS_CHECKIN).count(),
        "late": 0,
        "absent": 0,
        "last_sync": records_qs.order_by("-created_at").values_list("created_at", flat=True).first(),
    }

    cache = _load_import_cache(request)

    form = AttendanceImportForm()
    _apply_branch_choices_to_form(form, branches_qs)

    context = {
        "current": "biometrics",
        "records": records,
        "kpi": kpi,
        "preview_rows": [],
        "import_errors": [],
        "import_summary": "",
        "branches": branches_qs.values_list("id", "name"),
        "can_import": bool(cache and cache.get("rows")),
        "form": form,
    }
    return render(request, "admin/biometrics_attendance.html", context)


# =========================
# Biometrics import (Validate + Import)
# =========================
@login_required
@require_http_methods(["POST"])
def admin_biometrics_import(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    branches_qs = _scoped_branch_queryset_for_admin(request)

    form = AttendanceImportForm(request.POST, request.FILES)
    _apply_branch_choices_to_form(form, branches_qs)

    records_qs = AttendanceRecord.objects.select_related("branch").all()
    admin_branch = _get_admin_branch(request)
    if admin_branch:
        records_qs = records_qs.filter(branch=admin_branch)

    records = records_qs.order_by("-timestamp")[:100]
    kpi = {
        "present": records_qs.filter(attendance_status=AttendanceRecord.STATUS_CHECKIN).count(),
        "late": 0,
        "absent": 0,
        "last_sync": records_qs.order_by("-created_at").values_list("created_at", flat=True).first(),
    }

    context = {
        "current": "biometrics",
        "records": records,
        "kpi": kpi,
        "preview_rows": [],
        "import_errors": [],
        "import_summary": "",
        "branches": branches_qs.values_list("id", "name"),
        "can_import": False,
        "form": form,
    }

    action = (request.POST.get("action") or "validate").strip().lower()

    if not form.is_valid():
        context["import_errors"] = []
        for field, errs in form.errors.items():
            for e in errs:
                context["import_errors"].append(f"{field}: {e}")
        if not context["import_errors"]:
            context["import_errors"] = ["File upload failed. Please choose a valid CSV/Excel file."]
        return render(request, "admin/biometrics_attendance.html", context)

    upload = form.cleaned_data.get("file")
    skip_duplicates = form.cleaned_data.get("skip_duplicates", True)

    branch_obj = form.cleaned_data.get("branch")

    if branch_obj and not isinstance(branch_obj, Branch):
        branch_obj = _get_branch_from_post(request, branches_qs)

    if not branch_obj:
        context["import_errors"] = ["Invalid branch selected. Please choose a valid branch."]
        return render(request, "admin/biometrics_attendance.html", context)

    rows = None
    is_cached_mapped = False

    if action == "import" and not upload:
        cache = _load_import_cache(request)
        if not cache or not cache.get("rows"):
            context["import_errors"] = ["No validated data found. Please upload and Validate first."]
            return render(request, "admin/biometrics_attendance.html", context)

        cached_branch_id = cache.get("branch_id")
        if cached_branch_id:
            cached_branch = branches_qs.filter(id=cached_branch_id).first()
            if not cached_branch:
                context["import_errors"] = ["Cached branch is not allowed. Please validate again."]
                _clear_import_cache(request)
                return render(request, "admin/biometrics_attendance.html", context)
            branch_obj = cached_branch

        skip_duplicates = cache.get("skip_duplicates", skip_duplicates)
        rows = cache["rows"]
        is_cached_mapped = True
    else:
        if not upload:
            context["import_errors"] = ["No file received. Please select a file to upload."]
            return render(request, "admin/biometrics_attendance.html", context)

        filename = (upload.name or "").lower().strip()

        try:
            upload.seek(0)
            if filename.endswith(".csv"):
                rows = _read_csv(upload)
            elif filename.endswith(".xls") or filename.endswith(".xlsx"):
                rows = _read_excel(upload, filename)
            else:
                raise ValueError("Unsupported file type. Upload .csv, .xls, or .xlsx")
        except Exception as e:
            context["import_errors"] = [str(e)]
            return render(request, "admin/biometrics_attendance.html", context)

        if not rows:
            context["import_errors"] = ["File is empty or has no data rows."]
            return render(request, "admin/biometrics_attendance.html", context)

    preview = []
    validation_errors = []

    if is_cached_mapped:
        for idx, r in enumerate(rows[:20], start=2):
            employee_id = (r.get("employee_id") or "").strip()
            ts = _parse_timestamp(r.get("timestamp", ""))

            row_errors = []
            if not employee_id:
                row_errors.append("Missing Person ID")
            if not ts:
                row_errors.append("Invalid/missing Time")

            is_valid = len(row_errors) == 0

            preview.append(
                {
                    "employee_id": employee_id or "—",
                    "full_name": (r.get("full_name") or "").strip() or "—",
                    "department": (r.get("department") or "").strip() or "—",
                    "branch": branch_obj.name if branch_obj else "—",
                    "timestamp": ts or "—",
                    "attendance_status": _status_label(r.get("attendance_status")),
                    "status": "valid" if is_valid else "invalid",
                    "errors": ", ".join(row_errors) if row_errors else "",
                }
            )
    else:
        meaningful = 0
        for idx, row in enumerate(rows, start=2):
            mapped = _map_row(row, branch_obj=branch_obj)

            if (
                not mapped["employee_id"]
                and not mapped["timestamp"]
                and not mapped["full_name"]
                and not mapped["department"]
            ):
                continue

            row_errors = []
            if not mapped["employee_id"]:
                row_errors.append("Missing Person ID")
            if not mapped["timestamp"]:
                row_errors.append("Invalid/missing Time")

            is_valid = len(row_errors) == 0

            if meaningful < 20:
                preview.append(
                    {
                        "employee_id": mapped["employee_id"] or "—",
                        "full_name": mapped["full_name"] or "—",
                        "department": mapped["department"] or "—",
                        "branch": branch_obj.name if branch_obj else "—",
                        "timestamp": mapped["timestamp"] or "—",
                        "attendance_status": _status_label(mapped["attendance_status"]),
                        "status": "valid" if is_valid else "invalid",
                        "errors": ", ".join(row_errors) if row_errors else "",
                    }
                )
                meaningful += 1

            if not is_valid:
                validation_errors.append(f"Row {idx}: {', '.join(row_errors)}")

            if meaningful >= 20:
                break

    context["preview_rows"] = preview

    if action == "validate":
        if validation_errors:
            context["import_errors"] = validation_errors[:10]
            context["import_summary"] = f"Validation detected {len(validation_errors)} error(s). Fix and try again."
            _clear_import_cache(request)
            context["can_import"] = False
            return render(request, "admin/biometrics_attendance.html", context)

        _save_import_cache(request, branch_obj=branch_obj, skip_duplicates=skip_duplicates, rows=rows)
        context["import_summary"] = f"✓ Validation passed! {len(rows)} row(s) ready to import."
        context["can_import"] = True
        return render(request, "admin/biometrics_attendance.html", context)

    if action != "import":
        context["import_errors"] = ["Invalid action."]
        return render(request, "admin/biometrics_attendance.html", context)

    created = 0
    skipped = 0
    failed = 0
    import_errors = []

    try:
        with transaction.atomic():
            if is_cached_mapped:
                for idx, r in enumerate(rows, start=2):
                    employee_id = (r.get("employee_id") or "").strip()
                    ts = _parse_timestamp(r.get("timestamp", ""))

                    if not employee_id or not ts:
                        failed += 1
                        continue

                    attendance_status = r.get("attendance_status") or AttendanceRecord.STATUS_UNKNOWN

                    try:
                        AttendanceRecord.objects.create(
                            employee_id=employee_id,
                            full_name=(r.get("full_name") or "").strip(),
                            department=(r.get("department") or "").strip(),
                            branch=branch_obj,
                            timestamp=ts,
                            attendance_status=attendance_status,
                            raw_row=r.get("raw_row", r),
                        )
                        created += 1
                    except IntegrityError:
                        if skip_duplicates:
                            skipped += 1
                        else:
                            failed += 1
                            import_errors.append(f"Row {idx}: duplicate record")
                    except Exception as e:
                        failed += 1
                        import_errors.append(f"Row {idx}: {e}")
            else:
                for idx, row in enumerate(rows, start=2):
                    mapped = _map_row(row, branch_obj=branch_obj)

                    if (
                        not mapped["employee_id"]
                        and not mapped["timestamp"]
                        and not mapped["full_name"]
                        and not mapped["department"]
                    ):
                        continue

                    if not mapped["employee_id"] or not mapped["timestamp"]:
                        failed += 1
                        continue

                    try:
                        AttendanceRecord.objects.create(**mapped)
                        created += 1
                    except IntegrityError:
                        if skip_duplicates:
                            skipped += 1
                        else:
                            failed += 1
                            import_errors.append(f"Row {idx}: duplicate record")
                    except Exception as e:
                        failed += 1
                        import_errors.append(f"Row {idx}: {e}")

    except Exception as e:
        context["import_errors"] = [f"Import failed: {e}"]
        return render(request, "admin/biometrics_attendance.html", context)

    _clear_import_cache(request)

    context["import_summary"] = f"✓ Import complete: {created} created | {skipped} skipped | {failed} failed"
    if import_errors:
        context["import_errors"] = import_errors[:10]

    records_qs2 = AttendanceRecord.objects.select_related("branch").all()
    if admin_branch:
        records_qs2 = records_qs2.filter(branch=admin_branch)

    context["records"] = records_qs2.order_by("-timestamp")[:100]
    context["kpi"]["present"] = records_qs2.filter(attendance_status=AttendanceRecord.STATUS_CHECKIN).count()
    context["kpi"]["last_sync"] = records_qs2.order_by("-created_at").values_list("created_at", flat=True).first()
    context["can_import"] = False

    return render(request, "admin/biometrics_attendance.html", context)


# =========================
# Export endpoints
# =========================
@login_required
def admin_biometrics_template(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="attendance_template.csv"'
    writer = csv.writer(response)
    writer.writerow(["Person ID", "Name", "Department", "Time", "Attendance Status"])
    writer.writerow(["1", "Juan Dela Cruz", "Kitchen", "2026-02-04 08:00:00", "Check-in"])
    return response


@login_required
def admin_biometrics_export(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    qs = AttendanceRecord.objects.select_related("branch").all().order_by("-timestamp")

    employee_id = request.GET.get("employee_id", "").strip()
    branch = request.GET.get("branch", "").strip()

    if employee_id:
        qs = qs.filter(employee_id__icontains=employee_id)

    if branch:
        if str(branch).isdigit():
            qs = qs.filter(branch_id=int(branch))
        else:
            qs = qs.filter(branch__name__icontains=branch)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="attendance_export.csv"'

    writer = csv.writer(response)
    writer.writerow(["Person ID", "Name", "Department", "Branch", "Time", "Attendance Status", "Created At"])

    for rec in qs:
        writer.writerow(
            [
                rec.employee_id,
                rec.full_name,
                rec.department,
                rec.branch.name if rec.branch else "",
                rec.timestamp.strftime("%Y-%m-%d %H:%M:%S") if rec.timestamp else "",
                _status_label(rec.attendance_status),
                rec.created_at.strftime("%Y-%m-%d %H:%M:%S") if rec.created_at else "",
            ]
        )

    return response


# =========================
# CRUD Operations
# =========================
@login_required
def attendance_list(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    records = AttendanceRecord.objects.select_related("branch").all().order_by("-timestamp")

    page = int(request.GET.get("page", 1))
    per_page = 50
    start = (page - 1) * per_page
    end = start + per_page

    total = records.count()
    paginated = records[start:end]

    context = {
        "current": "biometrics",
        "records": paginated,
        "page": page,
        "total": total,
        "per_page": per_page,
    }
    return render(request, "admin/attendance_list.html", context)


@login_required
def attendance_detail(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    obj = get_object_or_404(AttendanceRecord.objects.select_related("branch"), pk=pk)
    return render(request, "admin/attendance_detail.html", {"current": "biometrics", "obj": obj})


@login_required
def attendance_create(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    branches_qs = _scoped_branch_queryset_for_admin(request)

    if request.method == "POST":
        form = AttendanceRecordForm(request.POST)
        _apply_branch_choices_to_form(form, branches_qs)

        if form.is_valid():
            obj = form.save(commit=False)

            admin_branch = _get_admin_branch(request)
            if admin_branch:
                obj.branch = admin_branch

            obj.save()
            messages.success(request, "Record created successfully!")
            return redirect("admin_biometrics")
    else:
        form = AttendanceRecordForm()
        _apply_branch_choices_to_form(form, branches_qs)

    return render(
        request,
        "admin/attendance_form.html",
        {
            "current": "biometrics",
            "form": form,
            "mode": "create",
            "title": "Create Attendance Record",
        },
    )


@login_required
def attendance_update(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    branches_qs = _scoped_branch_queryset_for_admin(request)

    obj = get_object_or_404(AttendanceRecord.objects.select_related("branch"), pk=pk)

    admin_branch = _get_admin_branch(request)
    if admin_branch and obj.branch_id != admin_branch.id:
        messages.error(request, "You can only edit attendance records in your branch.")
        return redirect("admin_biometrics")

    if request.method == "POST":
        form = AttendanceRecordForm(request.POST, instance=obj)
        _apply_branch_choices_to_form(form, branches_qs)

        if form.is_valid():
            edited = form.save(commit=False)

            if admin_branch:
                edited.branch = admin_branch

            edited.save()
            messages.success(request, "Record updated successfully!")
            return redirect("admin_biometrics")
    else:
        form = AttendanceRecordForm(instance=obj)
        _apply_branch_choices_to_form(form, branches_qs)

    return render(
        request,
        "admin/attendance_form.html",
        {
            "current": "biometrics",
            "form": form,
            "mode": "edit",
            "obj": obj,
            "title": f"Edit Record: {obj.employee_id}",
        },
    )


@login_required
def attendance_delete(request, pk):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    obj = get_object_or_404(AttendanceRecord.objects.select_related("branch"), pk=pk)

    admin_branch = _get_admin_branch(request)
    if admin_branch and obj.branch_id != admin_branch.id:
        messages.error(request, "You can only delete attendance records in your branch.")
        return redirect("admin_biometrics")

    if request.method == "POST":
        obj.delete()
        messages.success(request, "Record deleted successfully!")
        return redirect("admin_biometrics")

    return render(
        request,
        "admin/attendance_delete.html",
        {
            "current": "biometrics",
            "obj": obj,
        },
    )


#==============================================================
# -------------------------
# Payroll helpers
# -------------------------
def _time_to_dt(d: date, t: time):
    return datetime.combine(d, t)


def _daterange(d1: date, d2: date):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


def _get_or_create_rules(branch: Branch) -> PayrollRule:
    rules, _ = PayrollRule.objects.get_or_create(branch=branch)
    return rules


def _get_or_create_contrib(profile: UserProfile) -> EmployeeContribution:
    contrib, _ = EmployeeContribution.objects.get_or_create(profile=profile)
    return contrib


def _scoped_branch_for_admin_or_404(request, branch_id: int | None):
    """
    staff admin -> only their branch
    superuser -> any branch
    """
    if request.user.is_superuser:
        if branch_id:
            return Branch.objects.filter(id=branch_id).first()
        return None

    try:
        b = request.user.profile.branch
    except UserProfile.DoesNotExist:
        return None

    if branch_id and b and int(branch_id) != b.id:
        return None
    return b


def _attendance_summary_for_period(employee_id: str, branch: Branch, period: PayrollPeriod, rules: PayrollRule):
    """
    ✅ FIXED: avoids naive vs aware datetime comparisons.
    """
    start, end = period.start_date, period.end_date

    qs = AttendanceRecord.objects.filter(
        branch=branch,
        employee_id=employee_id,
        timestamp__date__gte=start,
        timestamp__date__lte=end,
    ).order_by("timestamp")

    by_day = {}
    for rec in qs:
        d = rec.timestamp.date()
        by_day.setdefault(d, {"ins": [], "outs": []})
        if rec.attendance_status == AttendanceRecord.STATUS_CHECKIN:
            by_day[d]["ins"].append(rec.timestamp)
        elif rec.attendance_status == AttendanceRecord.STATUS_CHECKOUT:
            by_day[d]["outs"].append(rec.timestamp)

    days_present = 0
    days_absent = 0
    late_minutes_total = 0
    undertime_minutes_total = 0
    has_missing_logs = False

    grace = int(rules.grace_minutes_normal or 0)

    # cutoff time (work_start + grace)
    start_limit_time = (datetime.combine(date.today(), rules.work_start_time) + timedelta(minutes=grace)).time()

    tz = timezone.get_current_timezone() if settings.USE_TZ else None

    for d in _daterange(start, end):
        logs = by_day.get(d)
        if not logs or (not logs["ins"] and not logs["outs"]):
            days_absent += 1
            continue

        if logs["ins"]:
            days_present += 1
        else:
            has_missing_logs = True

        if logs["ins"] and not logs["outs"]:
            has_missing_logs = True
        if logs["outs"] and not logs["ins"]:
            has_missing_logs = True

        # Late calc
        if logs["ins"]:
            first_in = min(logs["ins"])  # may be aware
            cutoff_dt = datetime.combine(d, start_limit_time)
            if settings.USE_TZ:
                cutoff_dt = timezone.make_aware(cutoff_dt, tz) if timezone.is_naive(cutoff_dt) else cutoff_dt
                first_in = _ensure_aware(first_in)

            if first_in > cutoff_dt:
                late_minutes_total += int((first_in - cutoff_dt).total_seconds() // 60)

        # Undertime calc
        if logs["outs"]:
            last_out = max(logs["outs"])
            end_dt = datetime.combine(d, rules.work_end_time)
            if settings.USE_TZ:
                end_dt = timezone.make_aware(end_dt, tz) if timezone.is_naive(end_dt) else end_dt
                last_out = _ensure_aware(last_out)

            if last_out < end_dt:
                undertime_minutes_total += int((end_dt - last_out).total_seconds() // 60)

    return {
        "days_present": days_present,
        "days_absent": days_absent,
        "total_late_minutes": late_minutes_total,
        "total_undertime_minutes": undertime_minutes_total,
        "has_missing_logs": has_missing_logs,
        "by_day": by_day,
    }


def _is_holiday_for(branch: Branch, d: date):
    """
    ✅ FIXED: uses Q() not models.Q()
    """
    qs = HolidaySuspension.objects.filter(date=d).filter(
        Q(scope=HolidaySuspension.SCOPE_NATIONWIDE)
        | Q(scope=HolidaySuspension.SCOPE_REGION)
        | Q(scope=HolidaySuspension.SCOPE_BRANCH, branch=branch)
    )
    return qs.first()


def _compute_payroll(profile: UserProfile, branch: Branch, period: PayrollPeriod, rules: PayrollRule):
    contrib = _get_or_create_contrib(profile)

    candidate_ids = [str(profile.user.username), str(profile.user.id)]
    picked_id = candidate_ids[0]
    if not AttendanceRecord.objects.filter(branch=branch, employee_id=picked_id).exists():
        picked_id = candidate_ids[1]

    summary = _attendance_summary_for_period(picked_id, branch, period, rules)

    days_present = summary["days_present"]
    days_absent = summary["days_absent"]
    late_minutes = summary["total_late_minutes"]
    undertime_minutes = summary["total_undertime_minutes"]
    has_missing_logs = summary["has_missing_logs"]

    base = Decimal("0.00")
    absences = days_absent

    if profile.employment_type == UserProfile.EMP_JO:
        base = (Decimal(profile.daily_rate or 0) * Decimal(days_present))
    else:
        ms = Decimal(profile.monthly_salary or 0)
        if period.pay_mode == PayrollPeriod.PAY_MONTHLY:
            base = ms
        else:
            base = (ms / Decimal("2.0"))

    premium = Decimal("0.00")
    if profile.has_premium and base > 0:
        premium = (base * (Decimal(rules.premium_rate_percent or 0) / Decimal("100")))

    overtime_pay = Decimal("0.00")

    late_pen = (Decimal(rules.late_penalty_per_minute or 0) * Decimal(late_minutes))
    under_pen = (Decimal(rules.undertime_penalty_per_minute or 0) * Decimal(undertime_minutes))

    sss = Decimal(contrib.sss_amount or 0)
    pagibig = Decimal(contrib.pagibig_amount or 0)
    if contrib.philhealth_mode == EmployeeContribution.PHILHEALTH_FIXED:
        philhealth = Decimal(contrib.philhealth_value or 0)
    else:
        philhealth = (base * (Decimal(contrib.philhealth_value or 0) / Decimal("100")))

    gov_total = sss + pagibig + philhealth

    gross = base + premium + overtime_pay

    tax = Decimal("0.00")
    if gross > 0:
        tax = gross * (Decimal(rules.tax_rate_percent or 0) / Decimal("100"))

    deductions_total = late_pen + under_pen + gov_total + tax

    net = gross - deductions_total
    if net < 0:
        net = Decimal("0.00")

    issues = []
    if has_missing_logs:
        issues.append("Missing logs")
    if rules.lunch_break_required:
        issues.append("Lunch logs not tracked")

    return {
        "attendance_summary": summary,
        "computed_payroll": {
            "base": base,
            "premium": premium,
            "ot": overtime_pay,
            "late_minutes": late_minutes,
            "undertime_minutes": undertime_minutes,
            "absences": absences,
            "deductions": deductions_total,
            "net": net,
        },
        "gov": {
            "sss": sss,
            "pagibig": pagibig,
            "philhealth": philhealth,
            "tax": tax,
            "gov_total": gov_total,
        },
        "issues": ", ".join([x for x in issues if x]),
        "picked_employee_id": picked_id,
    }


# -------------------------
# Admin payroll page (REAL context)
# -------------------------
@login_required
def admin_payroll(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    selected_branch = request.GET.get("branch")
    branch_obj = _scoped_branch_for_admin_or_404(request, selected_branch)
    if not request.user.is_superuser and not branch_obj:
        messages.error(request, "Admin has no branch assigned.")
        return redirect("admin_dashboard")

    if request.user.is_superuser:
        branches = Branch.objects.all().order_by("name")
    else:
        branches = Branch.objects.filter(id=branch_obj.id).order_by("name")

    payroll_periods = PayrollPeriod.objects.all().order_by("-start_date")[:24]
    selected_period = request.GET.get("period")
    period_obj = None
    if selected_period and str(selected_period).isdigit():
        period_obj = PayrollPeriod.objects.filter(id=int(selected_period)).first()
    if not period_obj:
        period_obj = payroll_periods.first()

    if request.user.is_superuser and not branch_obj:
        branch_obj = branches.first()

    payroll_rules = _get_or_create_rules(branch_obj) if branch_obj else None

    holidays = HolidaySuspension.objects.all().order_by("-date")[:50]
    if branch_obj:
        holidays = HolidaySuspension.objects.filter(
            Q(scope=HolidaySuspension.SCOPE_NATIONWIDE)
            | Q(scope=HolidaySuspension.SCOPE_REGION)
            | Q(scope=HolidaySuspension.SCOPE_BRANCH, branch=branch_obj)
        ).order_by("-date")[:50]

    payroll_batches = PayrollBatch.objects.all().order_by("-created_at")[:20]
    if branch_obj:
        payroll_batches = PayrollBatch.objects.filter(branch=branch_obj).order_by("-created_at")[:20]

    prof_qs = UserProfile.objects.select_related("user", "branch").filter(is_approved=True)
    if branch_obj:
        prof_qs = prof_qs.filter(branch=branch_obj)

    type_filter = (request.GET.get("type") or "ALL").upper()
    if type_filter in ("JO", "COS"):
        prof_qs = prof_qs.filter(employment_type=type_filter)

    employees = []
    total_payroll = Decimal("0.00")
    attendance_deductions = Decimal("0.00")
    gov_contributions = Decimal("0.00")

    if branch_obj and period_obj and payroll_rules:
        for prof in prof_qs.order_by("user__username"):
            result = _compute_payroll(prof, branch_obj, period_obj, payroll_rules)

            total_payroll += result["computed_payroll"]["net"]
            attendance_deductions += (
                Decimal(payroll_rules.late_penalty_per_minute or 0) * Decimal(result["computed_payroll"]["late_minutes"])
                + Decimal(payroll_rules.undertime_penalty_per_minute or 0) * Decimal(result["computed_payroll"]["undertime_minutes"])
            )
            gov_contributions += result["gov"]["gov_total"]

            employees.append({
                "id": prof.id,
                "name": (prof.user.get_full_name() or prof.user.username),
                "position": prof.position or "—",
                "employment_type": prof.employment_type,
                "branch_name": prof.branch.name if prof.branch else "—",
                "attendance_summary": {
                    "days_present": result["attendance_summary"]["days_present"],
                    "days_absent": result["attendance_summary"]["days_absent"],
                    "total_late_minutes": result["attendance_summary"]["total_late_minutes"],
                    "total_undertime_minutes": result["attendance_summary"]["total_undertime_minutes"],
                    "has_missing_logs": result["attendance_summary"]["has_missing_logs"],
                },
                "computed_payroll": {
                    "net": f"{result['computed_payroll']['net']:.2f}",
                },
                "sss_amount": f"{result['gov']['sss']:.2f}",
                "pagibig_amount": f"{result['gov']['pagibig']:.2f}",
                "philhealth_value": f"{result['gov']['philhealth']:.2f}",
                "philhealth_mode": "fixed",
                "has_premium": prof.has_premium,
            })

    pending_approval = Decimal("0.00")
    if branch_obj and period_obj:
        draft = PayrollBatch.objects.filter(branch=branch_obj, period=period_obj, status=PayrollBatch.STATUS_DRAFT).first()
        if draft:
            pending_approval = draft.totals_net or 0

    context = {
        "current": "payroll",
        "branches": branches,
        "selected_branch": branch_obj.id if branch_obj else None,
        "payroll_periods": payroll_periods,
        "selected_period": period_obj.id if period_obj else None,
        "payroll_rules": payroll_rules,

        "employees": employees,

        "holidays": holidays,
        "payroll_batches": payroll_batches,

        "total_payroll": total_payroll,
        "pending_approval": pending_approval,
        "attendance_deductions": attendance_deductions,
        "gov_contributions": gov_contributions,
    }
    return render(request, "admin/payroll.html", context)


# -------------------------
# Payroll Preview API (JSON)
# -------------------------
@login_required
def admin_payroll_preview_api(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    branch_id = request.GET.get("branch")
    period_id = request.GET.get("period")
    emp_type = (request.GET.get("type") or "ALL").upper()

    branch_obj = _scoped_branch_for_admin_or_404(request, branch_id)
    if not branch_obj:
        return JsonResponse({"ok": False, "error": "Invalid/unauthorized branch"}, status=400)

    period = PayrollPeriod.objects.filter(id=period_id).first() if str(period_id).isdigit() else PayrollPeriod.objects.first()
    if not period:
        return JsonResponse({"ok": False, "error": "No payroll period found"}, status=400)

    rules = _get_or_create_rules(branch_obj)

    prof_qs = UserProfile.objects.select_related("user", "branch").filter(is_approved=True, branch=branch_obj)
    if emp_type in ("JO", "COS"):
        prof_qs = prof_qs.filter(employment_type=emp_type)

    rows = []
    total_net = Decimal("0.00")

    for prof in prof_qs.order_by("user__username"):
        res = _compute_payroll(prof, branch_obj, period, rules)
        p = res["computed_payroll"]

        row = {
            "name": prof.user.get_full_name() or prof.user.username,
            "type": prof.employment_type,
            "base": float(p["base"]),
            "premium": float(p["premium"]),
            "ot": float(p["ot"]),
            "late": int(p["late_minutes"]),
            "undertime": int(p["undertime_minutes"]),
            "absences": int(p["absences"]),
            "deductions": float(p["deductions"]),
            "net": float(p["net"]),
            "issues": res["issues"],
        }
        total_net += p["net"]
        rows.append(row)

    return JsonResponse({
        "ok": True,
        "period": {"id": period.id, "name": period.name},
        "branch": {"id": branch_obj.id, "name": branch_obj.name},
        "employees": rows,
        "total_employees": len(rows),
        "total_net": float(total_net),
    })


# -------------------------
# DTR API (JSON)
# -------------------------
@login_required
def admin_employee_dtr_api(request, profile_id: int):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    period_id = request.GET.get("period")
    period = PayrollPeriod.objects.filter(id=period_id).first() if str(period_id).isdigit() else PayrollPeriod.objects.first()
    if not period:
        return JsonResponse({"ok": False, "error": "No payroll period found"}, status=400)

    prof = UserProfile.objects.select_related("user", "branch").filter(id=profile_id).first()
    if not prof or not prof.branch:
        return JsonResponse({"ok": False, "error": "Profile not found"}, status=404)

    branch_obj = _scoped_branch_for_admin_or_404(request, prof.branch_id)
    if not branch_obj:
        return JsonResponse({"ok": False, "error": "Unauthorized branch"}, status=403)

    rules = _get_or_create_rules(branch_obj)

    candidate_ids = [str(prof.user.username), str(prof.user.id)]
    picked_id = candidate_ids[0]
    if not AttendanceRecord.objects.filter(branch=branch_obj, employee_id=picked_id).exists():
        picked_id = candidate_ids[1]

    summ = _attendance_summary_for_period(picked_id, branch_obj, period, rules)
    by_day = summ["by_day"]

    tz = timezone.get_current_timezone() if settings.USE_TZ else None

    out = []
    for d in _daterange(period.start_date, period.end_date):
        logs = by_day.get(d, {"ins": [], "outs": []})
        time_in = min(logs["ins"]).strftime("%H:%M") if logs["ins"] else ""
        time_out = max(logs["outs"]).strftime("%H:%M") if logs["outs"] else ""

        late = 0
        undertime = 0
        status = "Absent"

        if logs["ins"] or logs["outs"]:
            status = "Present"
        if logs["ins"] and not logs["outs"]:
            status = "Missing Out"
        if logs["outs"] and not logs["ins"]:
            status = "Missing In"

        grace = int(rules.grace_minutes_normal or 0)
        cutoff_time = (datetime.combine(date.today(), rules.work_start_time) + timedelta(minutes=grace)).time()
        cutoff_dt = datetime.combine(d, cutoff_time)
        if settings.USE_TZ:
            cutoff_dt = timezone.make_aware(cutoff_dt, tz) if timezone.is_naive(cutoff_dt) else cutoff_dt

        if logs["ins"]:
            first_in = _ensure_aware(min(logs["ins"]))
            if first_in > cutoff_dt:
                late = int((first_in - cutoff_dt).total_seconds() // 60)

        end_dt = datetime.combine(d, rules.work_end_time)
        if settings.USE_TZ:
            end_dt = timezone.make_aware(end_dt, tz) if timezone.is_naive(end_dt) else end_dt

        if logs["outs"]:
            last_out = _ensure_aware(max(logs["outs"]))
            if last_out < end_dt:
                undertime = int((end_dt - last_out).total_seconds() // 60)

        total_hours = ""
        if logs["ins"] and logs["outs"]:
            delta = (_ensure_aware(max(logs["outs"])) - _ensure_aware(min(logs["ins"])))
            total_hours = round(delta.total_seconds() / 3600, 2)

        out.append({
            "date": d.strftime("%Y-%m-%d"),
            "day": d.strftime("%a"),
            "timeIn": time_in,
            "timeOut": time_out,
            "lunchIn": "",
            "lunchOut": "",
            "totalHours": total_hours,
            "late": late,
            "undertime": undertime,
            "status": status,
        })

    return JsonResponse({
        "ok": True,
        "employee": {"id": prof.id, "name": prof.user.get_full_name() or prof.user.username},
        "branch": {"id": branch_obj.id, "name": branch_obj.name},
        "period": {"id": period.id, "name": period.name},
        "rows": out,
    })


# -------------------------
# Process / Create Payroll Batch (Approve & Process)
# -------------------------
@login_required
@require_POST
def admin_payroll_process_batch(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    branch_id = request.POST.get("branch")
    period_id = request.POST.get("period")
    emp_type = (request.POST.get("type") or "ALL").upper()

    branch_obj = _scoped_branch_for_admin_or_404(request, branch_id)
    if not branch_obj:
        return JsonResponse({"ok": False, "error": "Invalid/unauthorized branch"}, status=400)

    period = PayrollPeriod.objects.filter(id=period_id).first() if str(period_id).isdigit() else None
    if not period:
        return JsonResponse({"ok": False, "error": "Invalid payroll period"}, status=400)

    rules = _get_or_create_rules(branch_obj)

    prof_qs = UserProfile.objects.select_related("user", "branch").filter(is_approved=True, branch=branch_obj)
    if emp_type in ("JO", "COS"):
        prof_qs = prof_qs.filter(employment_type=emp_type)

    batch, created = PayrollBatch.objects.get_or_create(
        branch=branch_obj,
        period=period,
        defaults={
            "name": f"Payroll {branch_obj.name} - {period.name}",
            "status": PayrollBatch.STATUS_DRAFT,
        }
    )

    PayrollItem.objects.filter(batch=batch).delete()

    totals_net = Decimal("0.00")
    totals_deductions = Decimal("0.00")

    with transaction.atomic():
        for prof in prof_qs.order_by("user__username"):
            res = _compute_payroll(prof, branch_obj, period, rules)
            p = res["computed_payroll"]
            gov_total = res["gov"]["gov_total"]
            tax_total = res["gov"]["tax"]

            item = PayrollItem.objects.create(
                batch=batch,
                profile=prof,
                base_pay=p["base"],
                premium_pay=p["premium"],
                overtime_pay=p["ot"],
                late_minutes=p["late_minutes"],
                undertime_minutes=p["undertime_minutes"],
                absences=p["absences"],
                deductions_total=p["deductions"],
                gov_contributions_total=gov_total,
                tax_total=tax_total,
                net_pay=p["net"],
                issues=res["issues"],
                meta={
                    "period": {"start": str(period.start_date), "end": str(period.end_date)},
                    "employee_id_used": res["picked_employee_id"],
                }
            )

            totals_net += item.net_pay
            totals_deductions += item.deductions_total

        batch.totals_net = totals_net
        batch.totals_deductions = totals_deductions
        batch.status = PayrollBatch.STATUS_COMPLETED
        batch.processed_by = request.user
        batch.processed_at = timezone.now()
        batch.save()

    return JsonResponse({
        "ok": True,
        "batch": {"id": batch.id, "name": batch.name, "status": batch.status},
        "totals": {"net": float(totals_net), "deductions": float(totals_deductions)},
    })
