# core/views.py

import csv
import io
import re
import json
from decimal import Decimal
from datetime import date, datetime, time, timedelta

from django.db.models import Count, Sum, Q

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.cache import never_cache
from .models import BiometricDevice
from .hikvision_sync import fetch_hikvision_attendance
from .forms import AttendanceImportForm, AttendanceRecordForm
from .models import (
    Branch,
    AttendanceRecord,
    UserProfile,
    LeaveRequest,
    LeaveAttachment,
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
# Landing Page
# =========================
def index(request):
    return render(request, "index.html")


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


@never_cache
def logout_ui(request):
    logout(request)
    request.session.flush()

    response = redirect("login_ui")
    response.delete_cookie("sessionid")
    response.delete_cookie("csrftoken")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


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
from collections import defaultdict
from datetime import time
from decimal import Decimal
from django.utils import timezone

@login_required
@never_cache
def admin_dashboard(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    today = timezone.localdate()
    branch = _get_admin_branch(request)

    # =========================
    # EMPLOYEES
    # =========================
    profiles = UserProfile.objects.filter(is_approved=True)
    if branch:
        profiles = profiles.filter(branch=branch)

    total_employees = profiles.count()

    # =========================
    # ATTENDANCE
    # =========================
    attendance_qs = AttendanceRecord.objects.all()
    if branch:
        attendance_qs = attendance_qs.filter(branch=branch)

    def _normalize_status(status_value):
        return str(status_value or "").strip().upper().replace("-", "_").replace(" ", "_")

    def _is_checkin(status_value):
        s = _normalize_status(status_value)
        return s in {"CHECK_IN", "CHECKIN", "TIME_IN", "IN"}

    def _is_checkout(status_value):
        s = _normalize_status(status_value)
        return s in {"CHECK_OUT", "CHECKOUT", "TIME_OUT", "OUT"}

    first_checkins = {}
    for rec in attendance_qs.order_by("employee_id", "timestamp"):
        if _is_checkin(rec.attendance_status) and rec.employee_id not in first_checkins:
            first_checkins[rec.employee_id] = rec

    last_checkouts = {}
    for rec in attendance_qs.order_by("employee_id", "-timestamp"):
        if _is_checkout(rec.attendance_status) and rec.employee_id not in last_checkouts:
            last_checkouts[rec.employee_id] = rec

    present = len(first_checkins)

    late_cutoff = time(8, 15)
    late = 0
    for rec in first_checkins.values():
        rec_time = (
            timezone.localtime(rec.timestamp).time()
            if timezone.is_aware(rec.timestamp)
            else rec.timestamp.time()
        )
        if rec_time > late_cutoff:
            late += 1

    absent = max(0, total_employees - present)

    incomplete_dtr_count = sum(
        1 for emp_id in first_checkins if emp_id not in last_checkouts
    )

    # =========================
    # LEAVES
    # =========================
    leave_qs = LeaveRequest.objects.all()
    if branch:
        leave_qs = leave_qs.filter(branch=branch)

    pending_leaves = leave_qs.filter(status=LeaveRequest.STATUS_PENDING).count()
    on_leave = leave_qs.filter(status=LeaveRequest.STATUS_APPROVED).count()

    # =========================
    # DEVICES
    # =========================
    devices_qs = BiometricDevice.objects.all()
    if branch:
        devices_qs = devices_qs.filter(branch=branch)

    devices = [
        {
            "name": d.name,
            "status": "online" if d.is_active else "offline",
            "last": "Active" if d.is_active else "Inactive",
            "battery": "N/A",
        }
        for d in devices_qs
    ]

    devices_online = devices_qs.filter(is_active=True).count()
    devices_total = devices_qs.count()
    offline_devices = max(0, devices_total - devices_online)

    # =========================
    # ALERTS / NOTIFICATIONS
    # =========================
    alerts = []

    if absent > 0:
        alerts.append({
            "lvl": "High",
            "text": f"{absent} employee(s) absent today."
        })

    if late > 0:
        alerts.append({
            "lvl": "Medium",
            "text": f"{late} employee(s) arrived late."
        })

    if pending_leaves > 0:
        alerts.append({
            "lvl": "Medium",
            "text": f"{pending_leaves} leave request(s) pending approval."
        })

    if incomplete_dtr_count > 0:
        alerts.append({
            "lvl": "Medium",
            "text": f"{incomplete_dtr_count} employee(s) have incomplete DTR today."
        })

    if offline_devices > 0:
        alerts.append({
            "lvl": "High",
            "text": f"{offline_devices} biometric device(s) offline."
        })

    recent_leave_notifications = leave_qs.order_by("-created_at")[:3]
    for leave in recent_leave_notifications:
        employee_name = leave.employee.get_full_name() or leave.employee.username
        alerts.append({
            "lvl": "Low",
            "text": f"{employee_name} filed {leave.get_leave_type_display()} ({leave.get_status_display()})."
        })

    if not alerts:
        alerts.append({
            "lvl": "Low",
            "text": "No new notifications for today."
        })

    alerts = alerts[:6]

    anomalies = []
    if incomplete_dtr_count > 0:
        anomalies.append(f"{incomplete_dtr_count} employee(s) have missing time logs.")
    if late > 3:
        anomalies.append(f"Late arrivals are unusually high today ({late}).")
    if offline_devices > 0:
        anomalies.append(f"{offline_devices} device(s) need attention.")
    if not anomalies:
        anomalies = ["No anomaly detected today."]

    # =========================
    # LEAVE OVERVIEW
    # =========================
    leave_overview = [
        {
            "employee": leave.employee.username,
            "type": leave.get_leave_type_display(),
            "dates": f"{leave.start_date} - {leave.end_date}",
            "status": leave.get_status_display(),
        }
        for leave in leave_qs.order_by("-created_at")[:5]
    ]

    # =========================
    # ATTENDANCE BY DEPARTMENT
    # =========================
    dept_map = defaultdict(lambda: {"present": 0, "late": 0})

    for rec in first_checkins.values():
        dept = (rec.department or "").strip() or "Unassigned"
        dept_map[dept]["present"] += 1

        rec_time = (
            timezone.localtime(rec.timestamp).time()
            if timezone.is_aware(rec.timestamp)
            else rec.timestamp.time()
        )
        if rec_time > late_cutoff:
            dept_map[dept]["late"] += 1

    dept_labels = list(dept_map.keys())
    dept_present_data = [dept_map[d]["present"] for d in dept_labels]
    dept_late_data = [dept_map[d]["late"] for d in dept_labels]

    department_attendance = []
    if dept_labels:
        for dept in dept_labels:
            department_attendance.append({
                "department": dept,
                "present": dept_map[dept]["present"],
                "late": dept_map[dept]["late"],
            })

    # =========================
    # PAYROLL SNAPSHOT
    # =========================
    estimated_gross = Decimal("0.00")
    estimated_deductions = Decimal("0.00")

    latest_batch = PayrollBatch.objects.all()
    if branch:
        latest_batch = latest_batch.filter(branch=branch)

    latest_batch = latest_batch.order_by("-created_at").first()

    if latest_batch:
        estimated_gross = latest_batch.totals_net or Decimal("0.00")
        estimated_deductions = latest_batch.totals_deductions or Decimal("0.00")

    payroll_ready = max(0, total_employees - incomplete_dtr_count)

    # =========================
    # AI SUMMARY
    # =========================
    ai_summary = (
        f"{present} present, {late} late, {absent} absent. "
        f"{pending_leaves} pending leaves. "
        f"{incomplete_dtr_count} incomplete DTR."
    )

    print("========== ADMIN DASHBOARD DEBUG ==========")
    print("TODAY:", today)
    print("BRANCH:", branch.name if branch else "All Branches")
    print("TOTAL EMPLOYEES:", total_employees)
    print("ATTENDANCE RECORDS:", attendance_qs.count())
    print("FIRST CHECKINS:", len(first_checkins))
    print("LAST CHECKOUTS:", len(last_checkouts))
    print("DEPARTMENT ATTENDANCE:", department_attendance)
    print("ADMIN ALERTS:", alerts)
    print("ADMIN ANOMALIES:", anomalies)
    print("===========================================")

    dashboard_data = {
        "branch": branch.name if branch else "All Branches",
        "total_employees": total_employees,
        "present": present,
        "late": late,
        "leave": on_leave,
        "absent": absent,
        "pending_leaves": pending_leaves,
        "payroll_ready": payroll_ready,
        "devices_online": devices_online,
        "devices_total": devices_total,
        "estimated_gross": f"{estimated_gross:.2f}",
        "estimated_deductions": f"{estimated_deductions:.2f}",
        "incomplete_dtr_count": incomplete_dtr_count,
        "devices": devices,
        "alerts": alerts,
        "anomalies": anomalies,
        "leave_overview": leave_overview,
        "dept_labels": dept_labels,
        "dept_present_data": dept_present_data,
        "dept_late_data": dept_late_data,
        "department_attendance": department_attendance,
        "ai_summary": ai_summary,
    }

    context = {
        "current": "dashboard",
        "today": today,
        "dashboard_data": dashboard_data,
    }

    return render(request, "admin/dashboard.html", context)


# NOTE: This earlier stub is kept (as you requested not to remove other code),
# but the REAL analytics view is defined later and will override this.
@login_required
@never_cache
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

#button sync for realtime data for attendance== renzo
@login_required
def admin_biometrics_sync_now(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

   

    devices = BiometricDevice.objects.filter(is_active=True)

    if not devices.exists():
        messages.error(request, "No active biometric device found.")
        return redirect("admin_biometrics")

    success_count = 0
    fail_count = 0

    for device in devices:
        try:
            fetch_hikvision_attendance(device)
            success_count += 1
        except Exception as e:
            fail_count += 1
            messages.error(request, f"Sync failed for {device.name}: {e}")

    if success_count:
        messages.success(request, f"Device sync completed. Successful device syncs: {success_count}")
    elif fail_count and not success_count:
        messages.error(request, "All device sync attempts failed.")

    return redirect("admin_biometrics")
#====================


@login_required
@never_cache
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
@never_cache
def admin_leave_approval(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    qs = LeaveRequest.objects.select_related(
        "employee", "branch", "reviewed_by"
    ).prefetch_related("attachments").order_by("-created_at")

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
@never_cache
def admin_reports(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")
    return render(request, "admin/reports.html", {"current": "reports"})


@login_required
@never_cache
def admin_shift_scheduling(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")
    return render(request, "admin/shift_scheduling.html", {"current": "scheduling"})


@login_required
@never_cache
def admin_system_administration(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")
    return render(request, "admin/system_administration.html", {"current": "system"})


# =========================
# Employee pages (simple renders)
# =========================
@login_required
@never_cache
def employee_dashboard(request):
    # -------------------------
    # Approval gate (employee only)
    # -------------------------
    try:
        if not (request.user.is_staff or request.user.is_superuser):
            if not request.user.profile.is_approved:
                messages.error(request, "Your account is pending approval by your branch admin.")
                return redirect("login_ui")
    except UserProfile.DoesNotExist:
        messages.error(request, "Account profile missing. Contact admin.")
        return redirect("login_ui")

    # -------------------------
    # Profile + branch
    # -------------------------
    try:
        profile = request.user.profile
        emp_branch = profile.branch
    except UserProfile.DoesNotExist:
        messages.error(request, "Profile missing. Contact admin.")
        return redirect("login_ui")

    if not emp_branch:
        messages.error(request, "No branch assigned. Contact admin.")
        return redirect("login_ui")

    # -------------------------
    # Attendance identity
    # -------------------------
    employee_id_used = _pick_attendance_employee_id(request.user, emp_branch)

    now = timezone.localtime(timezone.now()) if settings.USE_TZ else datetime.now()
    today = now.date()

    # -------------------------
    # Today's logs
    # -------------------------
    today_qs = AttendanceRecord.objects.filter(
        branch=emp_branch,
        employee_id=employee_id_used,
        timestamp__date=today,
    ).order_by("timestamp")

    ins = [r.timestamp for r in today_qs if r.attendance_status == AttendanceRecord.STATUS_CHECKIN]
    outs = [r.timestamp for r in today_qs if r.attendance_status == AttendanceRecord.STATUS_CHECKOUT]

    first_in = min(ins) if ins else None
    last_out = max(outs) if outs else None

    if first_in and not last_out:
        current_status = "Currently Checked In"
        status_kind = "in"
    elif first_in and last_out:
        current_status = "Checked Out"
        status_kind = "out"
    else:
        current_status = "Not Checked In"
        status_kind = "none"

    work_duration_text = "--"
    if first_in and not last_out:
        diff = now - (timezone.localtime(first_in) if settings.USE_TZ else first_in)
        mins = max(0, int(diff.total_seconds() // 60))
        work_duration_text = f"{mins // 60}h {mins % 60}m"

    late_cutoff = time(8, 15, 0)
    is_late_today = False
    if first_in:
        first_in_time = timezone.localtime(first_in).time() if settings.USE_TZ else first_in.time()
        is_late_today = first_in_time > late_cutoff

    # -------------------------
    # Weekly total hours
    # -------------------------
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_qs = AttendanceRecord.objects.filter(
        branch=emp_branch,
        employee_id=employee_id_used,
        timestamp__date__gte=week_start,
        timestamp__date__lte=today,
    ).order_by("timestamp")

    week_by_day = {}
    for rec in week_qs:
        d = rec.timestamp.date()
        week_by_day.setdefault(d, {"ins": [], "outs": []})
        if rec.attendance_status == AttendanceRecord.STATUS_CHECKIN:
            week_by_day[d]["ins"].append(rec.timestamp)
        elif rec.attendance_status == AttendanceRecord.STATUS_CHECKOUT:
            week_by_day[d]["outs"].append(rec.timestamp)

    total_week_minutes = 0
    for d, logs in week_by_day.items():
        if logs["ins"] and logs["outs"]:
            din = min(logs["ins"])
            dout = max(logs["outs"])
            if dout >= din:
                delta = (
                    timezone.localtime(dout) - timezone.localtime(din)
                    if settings.USE_TZ else
                    dout - din
                )
                total_week_minutes += int(delta.total_seconds() // 60)

    total_week_hours_text = f"{total_week_minutes / 60:.1f} hours"

    # -------------------------
    # Leave summary
    # -------------------------
    current_year = today.year
    used_requests, remaining_leave = _request_counts_for_year(request.user, current_year)

    pending_leave_count = LeaveRequest.objects.filter(
        employee=request.user,
        status=LeaveRequest.STATUS_PENDING,
    ).count()

    approved_leave_count = LeaveRequest.objects.filter(
        employee=request.user,
        status=LeaveRequest.STATUS_APPROVED,
    ).count()

    # -------------------------
    # Latest payroll item
    # -------------------------
    latest_payroll_item = (
        PayrollItem.objects
        .select_related("batch", "batch__period")
        .filter(profile=profile)
        .order_by("-batch__created_at")
        .first()
    )

    latest_net_pay = f"{latest_payroll_item.net_pay:.2f}" if latest_payroll_item else "0.00"
    latest_payroll_period = latest_payroll_item.batch.period.name if latest_payroll_item else "No payroll yet"

    # -------------------------
    # Recent attendance logs
    # -------------------------
    recent_logs = []
    for rec in today_qs.order_by("-timestamp")[:5]:
        recent_logs.append({
            "label": "Check In" if rec.attendance_status == AttendanceRecord.STATUS_CHECKIN else
                     "Check Out" if rec.attendance_status == AttendanceRecord.STATUS_CHECKOUT else
                     "Unknown",
            "time": _fmt_time_ampm(timezone.localtime(rec.timestamp) if settings.USE_TZ else rec.timestamp),
            "department": rec.department or "Unassigned",
        })

    # -------------------------
    # Dashboard context
    # -------------------------
    context = {
        "current": "dashboard",

        "employee_name": request.user.get_full_name() or request.user.username,
        "employee_branch": emp_branch.name,
        "employee_department": profile.department or "Unassigned",
        "employee_position": profile.position or "Not set",

        "current_status": current_status,
        "status_kind": status_kind,
        "today_checkin": _fmt_time_ampm(timezone.localtime(first_in) if (settings.USE_TZ and first_in) else first_in) if first_in else "--",
        "today_checkout": _fmt_time_ampm(timezone.localtime(last_out) if (settings.USE_TZ and last_out) else last_out) if last_out else "--",
        "work_duration": work_duration_text,
        "is_late_today": is_late_today,

        "total_week_hours": total_week_hours_text,

        "leave_year": current_year,
        "used_requests": used_requests,
        "remaining_leave": remaining_leave,
        "pending_leave_count": pending_leave_count,
        "approved_leave_count": approved_leave_count,

        "latest_net_pay": latest_net_pay,
        "latest_payroll_period": latest_payroll_period,

        "recent_logs": recent_logs,
    }

    return render(request, "employee/dashboard.html", context)


@login_required
@never_cache
def employee_attendance(request):
    # -------------------------
    # Approval gate (employee only)
    # -------------------------
    try:
        if not (request.user.is_staff or request.user.is_superuser):
            if not request.user.profile.is_approved:
                messages.error(request, "Your account is pending approval by your branch admin.")
                return redirect("employee_dashboard")
    except UserProfile.DoesNotExist:
        messages.error(request, "Account profile missing. Contact admin.")
        return redirect("employee_dashboard")

    # -------------------------
    # Branch required
    # -------------------------
    try:
        prof = request.user.profile
        emp_branch = prof.branch
    except UserProfile.DoesNotExist:
        messages.error(request, "Profile missing. Contact admin.")
        return redirect("employee_dashboard")

    if not emp_branch:
        messages.error(request, "No branch assigned. Contact admin.")
        return redirect("employee_dashboard")

    # -------------------------
    # Identify the employee_id used in AttendanceRecord
    # -------------------------
    employee_id_used = _pick_attendance_employee_id(request.user, emp_branch)

    # -------------------------
    # Today logs (check-in / check-out)
    # -------------------------
    now = timezone.localtime(timezone.now()) if settings.USE_TZ else datetime.now()
    today = now.date()

    todays_qs = AttendanceRecord.objects.filter(
        branch=emp_branch,
        employee_id=employee_id_used,
        timestamp__date=today,
    ).order_by("timestamp")

    ins = [r.timestamp for r in todays_qs if r.attendance_status == AttendanceRecord.STATUS_CHECKIN]
    outs = [r.timestamp for r in todays_qs if r.attendance_status == AttendanceRecord.STATUS_CHECKOUT]

    first_in = min(ins) if ins else None
    last_out = max(outs) if outs else None

    # Status logic
    if first_in and not last_out:
        current_status = "Checked In"
        status_kind = "in"
    elif first_in and last_out:
        current_status = "Checked Out"
        status_kind = "out"
    else:
        current_status = "Not Checked In"
        status_kind = "none"

    # Work duration (only if checked in and not checked out)
    work_duration_text = "--"
    if first_in and not last_out:
        diff = now - timezone.localtime(first_in) if settings.USE_TZ else (datetime.now() - first_in)
        mins = max(0, int(diff.total_seconds() // 60))
        work_duration_text = f"{mins // 60}h {mins % 60}m"

    # Late (simple rule)
    late_cutoff = time(8, 15, 0)
    is_late_today = bool(first_in and first_in.time() > late_cutoff)

    # -------------------------
    # Attendance History (last 30 days)
    # Build daily summary: earliest IN + latest OUT + total hours + status label
    # -------------------------
    days_back = 30
    start_date = today - timedelta(days=days_back - 1)

    period_qs = AttendanceRecord.objects.filter(
        branch=emp_branch,
        employee_id=employee_id_used,
        timestamp__date__gte=start_date,
        timestamp__date__lte=today,
    ).order_by("timestamp")

    by_day = {}
    for rec in period_qs:
        d = rec.timestamp.date()
        by_day.setdefault(d, {"ins": [], "outs": []})
        if rec.attendance_status == AttendanceRecord.STATUS_CHECKIN:
            by_day[d]["ins"].append(rec.timestamp)
        elif rec.attendance_status == AttendanceRecord.STATUS_CHECKOUT:
            by_day[d]["outs"].append(rec.timestamp)

    history_rows = []
    for d in sorted(by_day.keys(), reverse=True):
        logs = by_day[d]
        din = min(logs["ins"]) if logs["ins"] else None
        dout = max(logs["outs"]) if logs["outs"] else None

        # total hours
        total_hours_text = "-"
        if din and dout and dout >= din:
            delta = (timezone.localtime(dout) - timezone.localtime(din)) if settings.USE_TZ else (dout - din)
            total_mins = int(delta.total_seconds() // 60)
            total_hours_text = f"{total_mins // 60}h {total_mins % 60}m"

        # status badge
        badge = "Present" if din else "Absent"
        badge_style = "green"
        if din and din.time() > late_cutoff:
            badge = "Late"
            badge_style = "amber"
        if din and not dout:
            badge = "Missing Out"
            badge_style = "red"
        if dout and not din:
            badge = "Missing In"
            badge_style = "red"

        history_rows.append({
            "date": d,
            "date_label": "Today" if d == today else d.strftime("%b %d"),
            "check_in": _fmt_time_ampm(timezone.localtime(din) if (settings.USE_TZ and din) else din) if din else "-",
            "check_out": _fmt_time_ampm(timezone.localtime(dout) if (settings.USE_TZ and dout) else dout) if dout else "-",
            "total_hours": total_hours_text,
            "badge": badge,
            "badge_style": badge_style,
            "remarks": "",
        })

    # show latest 20 in table
    history_rows = history_rows[:20]

    # -------------------------
    # Calendar statuses for current month (present/late)
    # -------------------------
    first_of_month = today.replace(day=1)
    next_month = (first_of_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_qs = AttendanceRecord.objects.filter(
        branch=emp_branch,
        employee_id=employee_id_used,
        timestamp__date__gte=first_of_month,
        timestamp__date__lt=next_month,
    ).order_by("timestamp")

    month_by_day_first_in = {}
    for rec in month_qs:
        if rec.attendance_status != AttendanceRecord.STATUS_CHECKIN:
            continue
        d = rec.timestamp.date()
        if d not in month_by_day_first_in:
            month_by_day_first_in[d] = rec.timestamp

    calendar_statuses = {}
    for d, din in month_by_day_first_in.items():
        key = d.strftime("%Y-%m-%d")
        calendar_statuses[key] = "late" if din.time() > late_cutoff else "present"

    calendar_statuses_json = json.dumps(calendar_statuses, default=str)

    context = {
        "current": "attendance",

        # header card (today)
        "current_status": current_status,
        "status_kind": status_kind,          # "in" | "out" | "none"
        "today_checkin": _fmt_time_ampm(timezone.localtime(first_in) if (settings.USE_TZ and first_in) else first_in) if first_in else "",
        "today_checkout": _fmt_time_ampm(timezone.localtime(last_out) if (settings.USE_TZ and last_out) else last_out) if last_out else "",
        "work_duration": work_duration_text,
        "is_late_today": is_late_today,

        # table + calendar
        "history_rows": history_rows,
        "calendar_statuses_json": calendar_statuses_json,
    }
    return render(request, "employee/attendance.html", context)


@login_required
@never_cache
def employee_schedule(request):
    return render(request, "employee/schedule.html", {"current": "schedule"})


LEAVE_MAX_REQUESTS_PER_YEAR = 5


def _request_counts_for_year(employee, year: int):
    """
    Request-based counting (NOT days):
    - Counts: APPROVED + PENDING (slot is consumed once submitted)
    - Ignores: DRAFT, REJECTED, CANCELLED
    - Uses start_date.year as the year basis
    """
    qs = LeaveRequest.objects.filter(
        employee=employee,
        start_date__year=year,
        status__in=[LeaveRequest.STATUS_APPROVED, LeaveRequest.STATUS_PENDING],
    )
    used = qs.count()
    remaining = max(0, LEAVE_MAX_REQUESTS_PER_YEAR - used)
    return used, remaining


@login_required
@never_cache
def employee_leave(request):
    # -------------------------
    # Approval gate (employee only)
    # -------------------------
    try:
        if not (request.user.is_staff or request.user.is_superuser):
            if not request.user.profile.is_approved:
                messages.error(request, "Your account is pending approval by your branch admin.")
                return redirect("employee_dashboard")
    except UserProfile.DoesNotExist:
        messages.error(request, "Account profile missing. Contact admin.")
        return redirect("employee_dashboard")

    # -------------------------
    # Get employee branch
    # -------------------------
    try:
        emp_branch = request.user.profile.branch
    except UserProfile.DoesNotExist:
        messages.error(request, "Profile/Branch missing. Contact admin.")
        return redirect("employee_dashboard")

    # -------------------------
    # Year + request-based remaining
    # -------------------------
    year = timezone.now().year
    used_requests, remaining_leave = _request_counts_for_year(request.user, year)

    # -------------------------
    # Handle POST: Create leave request
    # -------------------------
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

        # ✅ Year basis: request must belong to current year (based on start_date)
        if s.year != year:
            messages.error(request, f"You can only file leave for the current year ({year}).")
            return redirect("employee_leave")

        # ✅ Block submit if reached limit (but allow saving drafts)
        if not is_draft and remaining_leave <= 0:
            messages.error(
                request,
                f"You already reached the {LEAVE_MAX_REQUESTS_PER_YEAR}-request leave limit for {year}. "
                f"You can request again next year."
            )
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

        if is_draft:
            messages.success(request, "Saved as draft.")
        else:
            # slot is consumed immediately (PENDING counts)
            new_remaining = max(0, remaining_leave - 1)
            messages.success(
                request,
                f"Leave request submitted! Awaiting approval. Remaining leave requests for {year}: {new_remaining}"
            )

        return redirect("employee_leave")

    # -------------------------
    # Fetch leave requests (display)
    # -------------------------
    leave_requests = (
        LeaveRequest.objects
        .filter(employee=request.user)
        .select_related("reviewed_by", "branch")
        .prefetch_related("attachments")
        .order_by("-created_at")
    )

    # Recompute counts for UI (safe even after POST redirect)
    used_requests, remaining_leave = _request_counts_for_year(request.user, year)

    total_count = leave_requests.count()
    approved_count = leave_requests.filter(status=LeaveRequest.STATUS_APPROVED).count()
    rejected_count = leave_requests.filter(status=LeaveRequest.STATUS_REJECTED).count()
    pending_count = leave_requests.filter(status=LeaveRequest.STATUS_PENDING).count()
    draft_count = leave_requests.filter(status=LeaveRequest.STATUS_DRAFT).count()
    cancelled_count = leave_requests.filter(status=LeaveRequest.STATUS_CANCELLED).count()

    # -------------------------
    # Notifications
    # -------------------------
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

    # Calendar
    calendar_events = []
    for lr in leave_requests.filter(status__in=[LeaveRequest.STATUS_APPROVED, LeaveRequest.STATUS_PENDING]):
        calendar_events.append({
            "start": lr.start_date.strftime("%b %d"),
            "title": f"{lr.get_leave_type_display()}",
            "status": lr.status,
        })

    # -------------------------
    # Render
    # -------------------------
    return render(
        request,
        "employee/leave.html",
        {
            "current": "leave",
            "leave_requests": leave_requests,

            # ✅ Request-based leave tracking
            "leave_year": year,
            "used_requests": used_requests,
            "remaining_leave": remaining_leave,
            "max_leave_requests": LEAVE_MAX_REQUESTS_PER_YEAR,

            # counts
            "pending_leave_count": pending_count,
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
@never_cache
def employee_payroll(request):
    return render(request, "employee/payroll.html", {"current": "payroll"})


@login_required
@never_cache
def employee_analytics(request):
    return render(request, "employee/analytics.html", {"current": "analytics"})


@login_required
@never_cache
def employee_notifications(request):
    return render(request, "employee/notification.html", {"current": "notification"})


@login_required
@never_cache
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
@never_cache
def admin_biometrics_attendance(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    branches_qs = _scoped_branch_queryset_for_admin(request)

    records_qs = AttendanceRecord.objects.select_related("branch").all()
    admin_branch = _get_admin_branch(request)
    if admin_branch:
        records_qs = records_qs.filter(branch=admin_branch)

    records = records_qs.order_by("-timestamp")[:100]

    holidays_qs = HolidaySuspension.objects.select_related("branch").all()
    if admin_branch:
        holidays_qs = holidays_qs.filter(
            Q(scope=HolidaySuspension.SCOPE_NATIONWIDE) |
            Q(scope=HolidaySuspension.SCOPE_REGION) |
            Q(scope=HolidaySuspension.SCOPE_BRANCH, branch=admin_branch)
        )

    holidays_qs = holidays_qs.order_by("-date", "-created_at")

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
        "holidays": holidays_qs,
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

    holidays_qs = HolidaySuspension.objects.select_related("branch").all()
    if admin_branch:
        holidays_qs = holidays_qs.filter(
            Q(scope=HolidaySuspension.SCOPE_NATIONWIDE)
            | Q(scope=HolidaySuspension.SCOPE_REGION)
            | Q(scope=HolidaySuspension.SCOPE_BRANCH, branch=admin_branch)
        )
    holidays_qs = holidays_qs.order_by("-date", "-created_at")
        

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
        "holidays": holidays_qs,
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
    admin_branch = _get_admin_branch(request)
    if admin_branch:
        records_qs2 = records_qs2.filter(branch=admin_branch)

    context["records"] = records_qs2.order_by("-timestamp")[:100]
    context["kpi"]["present"] = records_qs2.filter(attendance_status=AttendanceRecord.STATUS_CHECKIN).count()
    context["kpi"]["last_sync"] = records_qs2.order_by("-created_at").values_list("created_at", flat=True).first()
    context["can_import"] = False

    return render(request, "admin/biometrics_attendance.html", context)

#Holliday and work suspenssion
@login_required
@require_POST
def admin_biometrics_add_holiday(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    branches_qs = _scoped_branch_queryset_for_admin(request)

    name = (request.POST.get("holiday_name") or "").strip()
    raw_date = (request.POST.get("holiday_date") or "").strip()
    holiday_type = (request.POST.get("holiday_type") or HolidaySuspension.TYPE_HOLIDAY).strip()
    scope = (request.POST.get("holiday_scope") or HolidaySuspension.SCOPE_REGION).strip()
    notes = (request.POST.get("holiday_notes") or "").strip()
    branch_raw = (request.POST.get("holiday_branch") or "").strip()

    if not name:
        return JsonResponse({"ok": False, "error": "Holiday name is required."}, status=400)

    try:
        holiday_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except Exception:
        return JsonResponse({"ok": False, "error": "Valid holiday date is required."}, status=400)

    valid_types = {
        HolidaySuspension.TYPE_HOLIDAY,
        HolidaySuspension.TYPE_SUSPENSION,
        HolidaySuspension.TYPE_SPECIAL,
    }
    if holiday_type not in valid_types:
        return JsonResponse({"ok": False, "error": "Invalid holiday type."}, status=400)

    valid_scopes = {
        HolidaySuspension.SCOPE_NATIONWIDE,
        HolidaySuspension.SCOPE_REGION,
        HolidaySuspension.SCOPE_BRANCH,
    }
    if scope not in valid_scopes:
        return JsonResponse({"ok": False, "error": "Invalid holiday scope."}, status=400)

    branch_obj = None
    if scope == HolidaySuspension.SCOPE_BRANCH:
        if not branch_raw:
            return JsonResponse({"ok": False, "error": "Branch is required for branch scope."}, status=400)

        if branch_raw.isdigit():
            branch_obj = branches_qs.filter(id=int(branch_raw)).first()
        else:
            branch_obj = branches_qs.filter(name__iexact=branch_raw).first()

        if not branch_obj:
            return JsonResponse({"ok": False, "error": "Invalid or unauthorized branch."}, status=400)

    # Prevent exact duplicates
    existing = HolidaySuspension.objects.filter(
        date=holiday_date,
        name__iexact=name,
        type=holiday_type,
        scope=scope,
        branch=branch_obj,
    ).first()
    if existing:
        return JsonResponse({"ok": False, "error": "This holiday/suspension already exists."}, status=400)

    holiday = HolidaySuspension.objects.create(
        date=holiday_date,
        name=name,
        type=holiday_type,
        scope=scope,
        branch=branch_obj,
        notes=notes,
    )

    return JsonResponse({
        "ok": True,
        "id": holiday.id,
        "message": "Holiday / suspension saved successfully.",
    })


@login_required
@require_POST
def admin_biometrics_delete_holiday(request, holiday_id: int):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    obj = HolidaySuspension.objects.select_related("branch").filter(id=holiday_id).first()
    if not obj:
        return JsonResponse({"ok": False, "error": "Holiday not found."}, status=404)

    admin_branch = _get_admin_branch(request)
    if admin_branch and obj.scope == HolidaySuspension.SCOPE_BRANCH:
        if not obj.branch_id or obj.branch_id != admin_branch.id:
            return JsonResponse({"ok": False, "error": "You can only delete holidays in your branch."}, status=403)

    obj.delete()
    return JsonResponse({"ok": True, "message": "Holiday deleted successfully."})


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


def _ensure_aware(dt_value):
    """
    Make datetime timezone-aware if USE_TZ is enabled and value is naive.
    """
    if not dt_value:
        return dt_value
    if settings.USE_TZ and timezone.is_naive(dt_value):
        return timezone.make_aware(dt_value, timezone.get_current_timezone())
    return dt_value


def _get_or_create_rules(branch: Branch) -> PayrollRule:
    rules, _ = PayrollRule.objects.get_or_create(branch=branch)
    return rules


def _get_or_create_contrib(profile: UserProfile) -> EmployeeContribution:
    contrib, _ = EmployeeContribution.objects.get_or_create(profile=profile)
    return contrib


def _scoped_branch_for_admin_or_404(request, branch_id):
    """
    staff admin -> only their branch
    superuser -> any branch
    """
    if request.user.is_superuser:
        if branch_id and str(branch_id).isdigit():
            return Branch.objects.filter(id=int(branch_id)).first()
        return None

    try:
        b = request.user.profile.branch
    except UserProfile.DoesNotExist:
        return None

    if branch_id and b and str(branch_id).isdigit() and int(branch_id) != b.id:
        return None
    return b


def _is_holiday_for(branch: Branch, d: date):
    """
    Return matching holiday/suspension for a given branch/date.
    """
    qs = HolidaySuspension.objects.filter(date=d).filter(
        Q(scope=HolidaySuspension.SCOPE_NATIONWIDE)
        | Q(scope=HolidaySuspension.SCOPE_REGION)
        | Q(scope=HolidaySuspension.SCOPE_BRANCH, branch=branch)
    )
    return qs.first()


def _attendance_summary_for_period(employee_id: str, branch: Branch, period: PayrollPeriod, rules: PayrollRule):
    """
    Attendance summary for payroll + DTR.
    Holidays/suspensions are treated as remarks, not absences.
    Weekends are excluded from absence counting.
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
        by_day.setdefault(d, {
            "ins": [],
            "outs": [],
            "status": "",
            "remark": "",
            "is_holiday": False,
            "holiday_name": "",
        })
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
    start_limit_time = (datetime.combine(date.today(), rules.work_start_time) + timedelta(minutes=grace)).time()
    tz = timezone.get_current_timezone() if settings.USE_TZ else None

    for d in _daterange(start, end):
        by_day.setdefault(d, {
            "ins": [],
            "outs": [],
            "status": "",
            "remark": "",
            "is_holiday": False,
            "holiday_name": "",
        })

        logs = by_day[d]
        holiday_obj = _is_holiday_for(branch, d)

        # 1) Holiday / Suspension remark
        if holiday_obj:
            logs["is_holiday"] = True
            logs["holiday_name"] = holiday_obj.name

            if holiday_obj.type == HolidaySuspension.TYPE_HOLIDAY:
                logs["status"] = f"Holiday - {holiday_obj.name}"
            elif holiday_obj.type == HolidaySuspension.TYPE_SUSPENSION:
                logs["status"] = f"Suspension - {holiday_obj.name}"
            else:
                logs["status"] = f"Special Day - {holiday_obj.name}"

            logs["remark"] = logs["status"]
            continue

        # 2) Weekend = not absent
        if d.weekday() >= 5:
            logs["status"] = "Weekend"
            logs["remark"] = "Weekend"
            continue

        # 3) No logs on working day = absent
        if not logs["ins"] and not logs["outs"]:
            days_absent += 1
            logs["status"] = "Absent"
            logs["remark"] = "Absent"
            continue

        # 4) Present / missing logs
        if logs["ins"]:
            days_present += 1
        else:
            has_missing_logs = True

        if logs["ins"] and not logs["outs"]:
            has_missing_logs = True
            logs["status"] = "Missing Time Out"
        elif logs["outs"] and not logs["ins"]:
            has_missing_logs = True
            logs["status"] = "Missing Time In"
        else:
            logs["status"] = "Present"

        # 5) Late computation
        if logs["ins"]:
            first_in = min(logs["ins"])
            cutoff_dt = datetime.combine(d, start_limit_time)

            if settings.USE_TZ:
                cutoff_dt = timezone.make_aware(cutoff_dt, tz) if timezone.is_naive(cutoff_dt) else cutoff_dt
                first_in = _ensure_aware(first_in)

            if first_in > cutoff_dt:
                late_mins = int((first_in - cutoff_dt).total_seconds() // 60)
                late_minutes_total += late_mins
                if "Missing" not in logs["status"]:
                    logs["status"] = f"Present - Late ({late_mins} min)"

        # 6) Undertime computation
        if logs["outs"]:
            last_out = max(logs["outs"])
            end_dt = datetime.combine(d, rules.work_end_time)

            if settings.USE_TZ:
                end_dt = timezone.make_aware(end_dt, tz) if timezone.is_naive(end_dt) else end_dt
                last_out = _ensure_aware(last_out)

            if last_out < end_dt:
                undertime_mins = int((end_dt - last_out).total_seconds() // 60)
                undertime_minutes_total += undertime_mins
                if logs["status"] == "Present":
                    logs["status"] = f"Present - Undertime ({undertime_mins} min)"

        logs["remark"] = logs["status"]

    return {
        "days_present": days_present,
        "days_absent": days_absent,
        "total_late_minutes": late_minutes_total,
        "total_undertime_minutes": undertime_minutes_total,
        "has_missing_logs": has_missing_logs,
        "by_day": by_day,
    }


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

    # JO = daily rate x actual working days present
    if profile.employment_type == UserProfile.EMP_JO:
        base = Decimal(profile.daily_rate or 0) * Decimal(days_present)

    # COS = monthly or half-monthly fixed base
    else:
        ms = Decimal(profile.monthly_salary or 0)
        if period.pay_mode == PayrollPeriod.PAY_MONTHLY:
            base = ms
        else:
            base = ms / Decimal("2.0")

    premium = Decimal("0.00")
    if profile.has_premium and base > 0:
        premium = base * (Decimal(rules.premium_rate_percent or 0) / Decimal("100"))

    overtime_pay = Decimal("0.00")

    late_pen = Decimal(rules.late_penalty_per_minute or 0) * Decimal(late_minutes)
    under_pen = Decimal(rules.undertime_penalty_per_minute or 0) * Decimal(undertime_minutes)

    sss = Decimal(contrib.sss_amount or 0)
    pagibig = Decimal(contrib.pagibig_amount or 0)

    if contrib.philhealth_mode == EmployeeContribution.PHILHEALTH_FIXED:
        philhealth = Decimal(contrib.philhealth_value or 0)
    else:
        philhealth = base * (Decimal(contrib.philhealth_value or 0) / Decimal("100"))

    gov_total = sss + pagibig + philhealth

    gross = base + premium + overtime_pay

    tax = Decimal("0.00")
    if gross > 0:
        tax = gross * (Decimal(rules.tax_rate_percent or 0) / Decimal("100"))

    manual_deduction = Decimal(profile.manual_deduction_amount or 0)

    deductions_total = late_pen + under_pen + gov_total + tax + manual_deduction

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

    # 🔥 FIX HERE (REMOVE ADMINS)
    prof_qs = UserProfile.objects.select_related("user", "branch").filter(
        is_approved=True,
        user__is_staff=False,
        user__is_superuser=False
    )

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
                Decimal(payroll_rules.late_penalty_per_minute or 0)
                * Decimal(result["computed_payroll"]["late_minutes"])
                + Decimal(payroll_rules.undertime_penalty_per_minute or 0)
                * Decimal(result["computed_payroll"]["undertime_minutes"])
            )

            gov_contributions += result["gov"]["gov_total"]

            employees.append({
                "id": prof.id,
                "name": (prof.user.get_full_name() or prof.user.username),
                "position": prof.position or "—",
                "employment_type": prof.employment_type,
                "branch_name": prof.branch.name if prof.branch else "—",
                "attendance_summary": result["attendance_summary"],
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
        draft = PayrollBatch.objects.filter(
            branch=branch_obj,
            period=period_obj,
            status=PayrollBatch.STATUS_DRAFT
        ).first()

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

    # 🔥 FIX HERE
    prof_qs = UserProfile.objects.select_related("user", "branch").filter(
        is_approved=True,
        branch=branch_obj,
        user__is_staff=False,
        user__is_superuser=False
    )

    if emp_type in ("JO", "COS"):
        prof_qs = prof_qs.filter(employment_type=emp_type)

    rows = []
    total_net = Decimal("0.00")

    for prof in prof_qs.order_by("user__username"):
        res = _compute_payroll(prof, branch_obj, period, rules)
        p = res["computed_payroll"]

        rows.append({
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
        })

        total_net += p["net"]

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

    out = []
    for d in _daterange(period.start_date, period.end_date):
        logs = by_day.get(d, {
            "ins": [],
            "outs": [],
            "status": "Absent",
            "remark": "Absent",
            "is_holiday": False,
            "holiday_name": "",
        })

        ins = logs.get("ins", [])
        outs = logs.get("outs", [])

        time_in = min(ins).strftime("%H:%M") if ins else ""
        time_out = max(outs).strftime("%H:%M") if outs else ""

        late_val = 0
        undertime_val = 0

        if ins:
            grace = int(rules.grace_minutes_normal or 0)
            cutoff_dt = datetime.combine(d, rules.work_start_time) + timedelta(minutes=grace)
            first_in = min(ins)
            if settings.USE_TZ:
                tz = timezone.get_current_timezone()
                cutoff_dt = timezone.make_aware(cutoff_dt, tz) if timezone.is_naive(cutoff_dt) else cutoff_dt
                first_in = _ensure_aware(first_in)
            if first_in > cutoff_dt:
                late_val = int((first_in - cutoff_dt).total_seconds() // 60)

        if outs:
            end_dt = datetime.combine(d, rules.work_end_time)
            last_out = max(outs)
            if settings.USE_TZ:
                tz = timezone.get_current_timezone()
                end_dt = timezone.make_aware(end_dt, tz) if timezone.is_naive(end_dt) else end_dt
                last_out = _ensure_aware(last_out)
            if last_out < end_dt:
                undertime_val = int((end_dt - last_out).total_seconds() // 60)

        total_hours = ""
        if ins and outs:
            first_in = min(ins)
            last_out = max(outs)
            diff = last_out - first_in
            total_hours = round(diff.total_seconds() / 3600, 2)

        out.append({
            "date": d.strftime("%Y-%m-%d"),
            "day": d.strftime("%A"),
            "timeIn": time_in,
            "timeOut": time_out,
            "lunchIn": "",
            "lunchOut": "",
            "totalHours": total_hours,
            "late": late_val,
            "undertime": undertime_val,
            "status": logs.get("status", "Absent"),
        })

    return JsonResponse({"ok": True, "rows": out})

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

    # 🔥 FIX HERE
    prof_qs = UserProfile.objects.select_related("user", "branch").filter(
        is_approved=True,
        branch=branch_obj,
        user__is_staff=False,
        user__is_superuser=False
    )

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

            PayrollItem.objects.create(
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

            totals_net += p["net"]
            totals_deductions += p["deductions"]

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



#new added 3/3/2026 ======================================================================================

def _pick_attendance_employee_id(user, branch: Branch):
    """
    AttendanceRecord.employee_id might be:
    - username (common in demo)
    - user.id (if you stored numeric ids)
    Returns the first id that actually exists in AttendanceRecord for this branch.
    """
    candidates = [str(user.username), str(user.id)]
    for cid in candidates:
        if AttendanceRecord.objects.filter(branch=branch, employee_id=cid).exists():
            return cid
    # fallback (still return username so page won't crash)
    return candidates[0]


def _fmt_time_ampm(dt):
    if not dt:
        return ""
    if not hasattr(dt, "strftime"):
        return str(dt)
    return dt.strftime("%I:%M %p").lstrip("0")