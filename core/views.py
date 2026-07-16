# core/views.py

import csv
import io
import re
import json
from decimal import Decimal
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from collections import defaultdict
from django.utils import timezone
from django.core.exceptions import PermissionDenied


from django.db.models import Count, Sum, Q


from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from .models import TravelOrder, UserProfile
from django.contrib import messages

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.cache import never_cache
from .models import BiometricDevice
from .hikvision_sync import fetch_hikvision_attendance
from core.models import AttendanceRecord

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
    FinalizedDTR,
    TravelOrder,
    OvertimeRequest,
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


@login_required
@never_cache
def admin_analytics(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    payload = _analytics_build_payload(request)

    branches = _analytics_get_branches_for_filter(request)

    selected_branch_id = str(payload["filters"]["branch"] or "")
    selected_emp_type = payload["filters"]["emp_type"]
    selected_department = payload["filters"]["department"]

    dept_qs = UserProfile.objects.filter(
        is_approved=True,
        user__is_staff=False,
        user__is_superuser=False,
    )

    admin_branch = _get_admin_branch(request)
    if admin_branch:
        dept_qs = dept_qs.filter(branch=admin_branch)

    departments = (
        dept_qs.exclude(department="")
        .values_list("department", flat=True)
        .distinct()
        .order_by("department")
    )

    context = {
        "current": "analytics",

        "branches": branches,
        "departments": departments,

        "selected_branch_id": selected_branch_id,
        "selected_emp_type": selected_emp_type,
        "selected_department": selected_department,
        "selected_start": payload["filters"]["start"],
        "selected_end": payload["filters"]["end"],

        "branch_label": payload["filters"]["branch_name"],
        "range_label": f"{payload['filters']['start']} to {payload['filters']['end']}",

        "chart_payload_json": json.dumps(payload, default=str),
    }

    return render(request, "admin/analytics.html", context)


@login_required
@never_cache
def admin_analytics_api(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    payload = _analytics_build_payload(request)
    return JsonResponse({"ok": True, "data": payload}, safe=False)


@login_required
@never_cache
def admin_analytics_employee_risks_api(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    payload = _analytics_build_payload(request)
    return JsonResponse({
        "ok": True,
        "risk_rows": payload.get("risk_rows", []),
        "late_rows": payload.get("late_rows", []),
        "overwork_rows": payload.get("overwork_rows", []),
    })


@login_required
@never_cache
def admin_analytics_insights_api(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    payload = _analytics_build_payload(request)
    return JsonResponse({
        "ok": True,
        "generated_at": payload.get("generated_at"),
        "insights": payload.get("insights", []),
        "summary": payload.get("summary", {}),
        "comparison": payload.get("comparison", {}),
    })




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
#===================
#Add and delete employee travel feature
@login_required
@require_POST
def admin_add_travel(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    profile_id = (request.POST.get("profile_id") or "").strip()
    start_date = (request.POST.get("start_date") or "").strip()
    end_date = (request.POST.get("end_date") or "").strip()
    reason = (request.POST.get("reason") or "Official Travel").strip()

    if not profile_id or not start_date or not end_date:
        return JsonResponse({"ok": False, "error": "Missing employee, start date, or end date."}, status=400)

    try:
        s = datetime.strptime(start_date, "%Y-%m-%d").date()
        e = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid date format. Use YYYY-MM-DD."}, status=400)

    if e < s:
        return JsonResponse({"ok": False, "error": "End date cannot be before start date."}, status=400)

    admin_branch = _get_admin_branch(request)

    profiles = UserProfile.objects.select_related("user", "branch").filter(
        id=profile_id,
        is_approved=True,
        user__is_staff=False,
        user__is_superuser=False,
    )

    if admin_branch:
        profiles = profiles.filter(branch=admin_branch)

    profile = profiles.first()

    if not profile:
        return JsonResponse({"ok": False, "error": "Employee not found or not allowed for your branch."}, status=404)

    TravelOrder.objects.update_or_create(
        employee=profile,
        start_date=s,
        end_date=e,
        defaults={
            "reason": reason,
        }
    )

    return JsonResponse({
        "ok": True,
        "message": f"{profile.user.username} is now on travel from {s} to {e}."
    })


@login_required
@require_POST
def admin_delete_travel(request, travel_id):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    admin_branch = _get_admin_branch(request)

    qs = TravelOrder.objects.select_related("employee", "employee__user", "employee__branch")
    if admin_branch:
        qs = qs.filter(employee__branch=admin_branch)

    travel = get_object_or_404(qs, id=travel_id)
    travel.delete()

    return JsonResponse({"ok": True})

#=========================
@login_required
@never_cache
def admin_employee_management(request):
    """
    Employee Management:
    - Pending profiles for approval
    - Approved profiles list
    - Employee Profiles CRUD
    - Payroll setup per employee:
        biometric_employee_id
        employment_type: COS / JO / PERMANENT
        monthly_salary
        daily_rate
        PERA / other earnings for permanent employees
        has_premium
        EmployeeContribution:
            JO/COS: SSS, Pag-IBIG, PhilHealth
            Permanent: WTAX, PhilHealth, GSIS, Pag-IBIG, loans, other deductions
    """
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    qs = _scoped_profiles_for_admin(request)

    ALLOWED_EMPLOYMENT_TYPES = (
        UserProfile.EMP_COS,
        UserProfile.EMP_JO,
        UserProfile.EMP_PERMANENT,
    )

    def _to_decimal(value, default="0.00"):
        raw = str(value or "").strip().replace(",", "")
        if not raw:
            return Decimal(default)

        try:
            return Decimal(raw)
        except Exception:
            return Decimal(default)

    def _get_allowed_branch_from_post():
        """
        Superuser can choose branch from POST.
        Branch admin is forced to their own branch.
        """
        if request.user.is_superuser:
            bid = (request.POST.get("branch_id") or "").strip()
            if not bid:
                return None
            return Branch.objects.filter(id=bid).first()

        try:
            return request.user.profile.branch
        except UserProfile.DoesNotExist:
            return None

    def _save_employee_contribution(profile):
        """
        Saves employee-specific contribution/deduction settings.

        JO/COS:
        - SSS
        - Pag-IBIG
        - PhilHealth

        Permanent:
        - WTAX
        - PhilHealth
        - GSIS employee share
        - GSIS employer share
        - Pag-IBIG
        - Loans
        - Other deductions
        - Other employer contributions
        """

        is_permanent = profile.employment_type == UserProfile.EMP_PERMANENT

        # For permanent employees, SSS should normally be 0 because GSIS is used.
        # Still stored for compatibility, but default is safer as 0.
        sss_default = "0.00" if is_permanent else "760.00"

        sss_amount = _to_decimal(request.POST.get("sss_amount"), sss_default)
        pagibig_amount = _to_decimal(request.POST.get("pagibig_amount"), "0.00" if is_permanent else "400.00")

        philhealth_mode = (request.POST.get("philhealth_mode") or "percent").strip().lower()
        philhealth_value = _to_decimal(request.POST.get("philhealth_value"), "0.00" if is_permanent else "5.00")

        if philhealth_mode not in ("percent", "fixed"):
            philhealth_mode = "percent"

        # Permanent employee deduction fields.
        # These are manual/configurable first because exact official formulas
        # still need client confirmation.
        wtax_amount = _to_decimal(request.POST.get("wtax_amount"), "0.00")
        gsis_employee_share = _to_decimal(request.POST.get("gsis_employee_share"), "0.00")
        gsis_employer_share = _to_decimal(request.POST.get("gsis_employer_share"), "0.00")
        loan_deduction_amount = _to_decimal(request.POST.get("loan_deduction_amount"), "0.00")
        other_deduction_amount = _to_decimal(request.POST.get("other_deduction_amount"), "0.00")
        other_employer_contribution = _to_decimal(request.POST.get("other_employer_contribution"), "0.00")

        EmployeeContribution.objects.update_or_create(
            profile=profile,
            defaults={
                "sss_amount": sss_amount,
                "pagibig_amount": pagibig_amount,
                "philhealth_mode": philhealth_mode,
                "philhealth_value": philhealth_value,

                "wtax_amount": wtax_amount,
                "gsis_employee_share": gsis_employee_share,
                "gsis_employer_share": gsis_employer_share,
                "loan_deduction_amount": loan_deduction_amount,
                "other_deduction_amount": other_deduction_amount,
                "other_employer_contribution": other_employer_contribution,
            },
        )

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        if action == "create_employee":
            username = (request.POST.get("username") or "").strip()
            email = (request.POST.get("email") or "").strip()
            password = request.POST.get("password") or ""

            department = (request.POST.get("department") or "").strip()
            position = (request.POST.get("position") or "").strip()
            biometric_employee_id = (request.POST.get("biometric_employee_id") or "").strip()

            employment_type = (request.POST.get("employment_type") or "").strip().upper()
            branch = _get_allowed_branch_from_post()

            monthly_salary = _to_decimal(request.POST.get("monthly_salary"), "0.00")
            daily_rate = _to_decimal(request.POST.get("daily_rate"), "0.00")

            pera_allowance = _to_decimal(request.POST.get("pera_allowance"), "0.00")
            other_earnings_amount = _to_decimal(request.POST.get("other_earnings_amount"), "0.00")

            manual_deduction_amount = _to_decimal(request.POST.get("manual_deduction_amount"), "0.00")
            has_premium = bool(request.POST.get("has_premium"))

            if not username:
                messages.error(request, "Username is required.")
                return redirect("admin_employees")

            if not branch:
                messages.error(request, "Branch is required.")
                return redirect("admin_employees")

            if employment_type not in ALLOWED_EMPLOYMENT_TYPES:
                messages.error(request, "Employment type must be COS, JO, or Permanent.")
                return redirect("admin_employees")

            if not password or len(password) < 8:
                messages.error(request, "Password is required and must be at least 8 characters.")
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

                    profile = UserProfile.objects.create(
                        user=user,
                        branch=branch,
                        department=department,
                        position=position,
                        employment_type=employment_type,
                        biometric_employee_id=biometric_employee_id,
                        monthly_salary=monthly_salary,
                        daily_rate=daily_rate,
                        pera_allowance=pera_allowance,
                        other_earnings_amount=other_earnings_amount,
                        manual_deduction_amount=manual_deduction_amount,
                        has_premium=has_premium,
                        is_approved=True,
                    )

                    _save_employee_contribution(profile)

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

            email = (request.POST.get("email") or "").strip()
            department = (request.POST.get("department") or "").strip()
            position = (request.POST.get("position") or "").strip()
            biometric_employee_id = (request.POST.get("biometric_employee_id") or "").strip()
            employment_type = (request.POST.get("employment_type") or "").strip().upper()

            monthly_salary = _to_decimal(request.POST.get("monthly_salary"), "0.00")
            daily_rate = _to_decimal(request.POST.get("daily_rate"), "0.00")

            pera_allowance = _to_decimal(request.POST.get("pera_allowance"), "0.00")
            other_earnings_amount = _to_decimal(request.POST.get("other_earnings_amount"), "0.00")

            manual_deduction_amount = _to_decimal(request.POST.get("manual_deduction_amount"), "0.00")
            has_premium = bool(request.POST.get("has_premium"))

            if employment_type not in ALLOWED_EMPLOYMENT_TYPES:
                messages.error(request, "Employment type must be COS, JO, or Permanent.")
                return redirect("admin_employees")

            if request.user.is_superuser:
                bid = (request.POST.get("branch_id") or "").strip()
                if bid:
                    b = Branch.objects.filter(id=bid).first()
                    if b:
                        prof.branch = b

            try:
                with transaction.atomic():
                    prof.department = department
                    prof.position = position
                    prof.employment_type = employment_type
                    prof.biometric_employee_id = biometric_employee_id
                    prof.monthly_salary = monthly_salary
                    prof.daily_rate = daily_rate
                    prof.pera_allowance = pera_allowance
                    prof.other_earnings_amount = other_earnings_amount
                    prof.manual_deduction_amount = manual_deduction_amount
                    prof.has_premium = has_premium
                    prof.save()

                    if email != prof.user.email:
                        prof.user.email = email
                        prof.user.save(update_fields=["email"])

                    _save_employee_contribution(prof)

                messages.success(request, f"Updated: {prof.user.username}")

            except Exception as e:
                messages.error(request, f"Update failed: {e}")

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

    employee_profiles = (
        qs.filter(
            user__is_staff=False,
            user__is_superuser=False,
        )
        .select_related("user", "branch")
        .order_by("user__username")
    )

    # Ensure every employee has contribution row for display.
    for prof in employee_profiles:
        is_permanent = prof.employment_type == UserProfile.EMP_PERMANENT

        EmployeeContribution.objects.get_or_create(
            profile=prof,
            defaults={
                "sss_amount": Decimal("0.00") if is_permanent else Decimal("760.00"),
                "pagibig_amount": Decimal("0.00") if is_permanent else Decimal("400.00"),
                "philhealth_mode": "percent",
                "philhealth_value": Decimal("0.00") if is_permanent else Decimal("5.00"),

                "wtax_amount": Decimal("0.00"),
                "gsis_employee_share": Decimal("0.00"),
                "gsis_employer_share": Decimal("0.00"),
                "loan_deduction_amount": Decimal("0.00"),
                "other_deduction_amount": Decimal("0.00"),
                "other_employer_contribution": Decimal("0.00"),
            },
        )

    branches = _scoped_branch_queryset_for_admin(request)

    return render(
        request,
        "admin/employee_management.html",
        {
            "current": "employees",
            "pending_profiles": pending_profiles,
            "approved_profiles": approved_profiles,
            "employee_profiles": employee_profiles,
            "branches": branches,
            "employment_type_choices": UserProfile.EMPLOYMENT_TYPE_CHOICES,
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
    """
    Employee Attendance Page:
    - Shows only the logged-in employee's attendance.
    - Uses UserProfile.biometric_employee_id to match AttendanceRecord.employee_id.
    - Reuses _build_dtr_and_summary() so employee DTR follows the same payroll rules.
    """

    # -------------------------
    # Employee approval/profile gate
    # -------------------------
    if request.user.is_staff or request.user.is_superuser:
        return redirect("admin_dashboard")

    try:
        profile = request.user.profile
    except UserProfile.DoesNotExist:
        messages.error(request, "Account profile missing. Contact admin.")
        return redirect("login_ui")

    if not profile.is_approved:
        messages.error(request, "Your account is pending approval by your branch admin.")
        return redirect("login_ui")

    branch = profile.branch
    if not branch:
        messages.error(request, "No branch assigned. Contact admin.")
        return redirect("employee_dashboard")

    employee_id_used = _pick_attendance_employee_id(request.user, branch)
    if not employee_id_used:
        messages.error(request, "No biometric employee ID configured. Contact admin.")
        return redirect("employee_dashboard")

    # -------------------------
    # Date filter
    # Default: current month
    # -------------------------
    today = timezone.localdate()
    default_start = today.replace(day=1)
    default_end = today

    start_raw = (request.GET.get("start") or "").strip()
    end_raw = (request.GET.get("end") or "").strip()

    try:
        start_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else default_start
    except ValueError:
        start_date = default_start

    try:
        end_date = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else default_end
    except ValueError:
        end_date = default_end

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    # -------------------------
    # Use existing payroll rules
    # -------------------------
    rules = _get_or_create_rules(branch)

    # Fake/simple period object for DTR computation
    period = SimpleNamespace(
        name=f"{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}",
        start_date=start_date,
        end_date=end_date,
        pay_mode=PayrollPeriod.PAY_MONTHLY,
    )

    # Reuse your existing DTR builder
    dtr = _build_dtr_and_summary(profile, branch, period, rules)
    dtr_rows = dtr.get("rows", [])

    # -------------------------
    # Today's attendance status
    # -------------------------
    today_row = None
    for row in dtr_rows:
        if row.get("date") == today.isoformat():
            today_row = row
            break

    if not today_row:
        today_row = {
            "date": today.isoformat(),
            "am_in": "",
            "am_out": "",
            "pm_in": "",
            "pm_out": "",
            "total_hours": "0.00",
            "late": 0,
            "undertime": 0,
            "status": "No Record",
            "remarks": "No attendance row found for today.",
        }

    has_checkin = bool(today_row.get("am_in") or today_row.get("pm_in"))
    has_checkout = bool(today_row.get("am_out") or today_row.get("pm_out"))

    if has_checkin and has_checkout:
        current_status = "Checked Out"
        status_kind = "out"
    elif has_checkin:
        current_status = "Checked In"
        status_kind = "in"
    else:
        current_status = "Not Checked In"
        status_kind = "none"

    # -------------------------
    # Summary cards
    # -------------------------
    summary = {
        "days_present": dtr.get("days_present", 0),
        "travel_days": dtr.get("travel_days", 0),
        "holiday_days": dtr.get("holiday_days", 0),
        "absences": dtr.get("absences", 0),
        "late_minutes": dtr.get("late_minutes", 0),
        "undertime_minutes": dtr.get("undertime_minutes", 0),
        "missing_logs": dtr.get("missing_logs", 0),
        "records_found": dtr.get("records_found", 0),
        "records_used": dtr.get("records_used", 0),
    }

    context = {
        "current": "attendance",

        "profile": profile,
        "employee_id_used": employee_id_used,
        "employee_name": request.user.get_full_name() or request.user.username,
        "employee_branch": branch.name,

        "start_date": start_date,
        "end_date": end_date,
        "period_name": period.name,

        "current_status": current_status,
        "status_kind": status_kind,
        "today_row": today_row,
        "today_checkin": today_row.get("am_in") or today_row.get("pm_in") or "--",
        "today_checkout": today_row.get("pm_out") or today_row.get("am_out") or "--",
        "work_duration": today_row.get("total_hours", "0.00"),
        "today_late_minutes": today_row.get("late", 0),
        "today_undertime_minutes": today_row.get("undertime", 0),
        "today_attendance_status": today_row.get("status", "No Record"),
        "today_remarks": today_row.get("remarks", ""),

        "summary": summary,
        "history_rows": dtr_rows,
        "dtr_rows": dtr_rows,

        "issues": dtr.get("issues", []),
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
    """
    Employee Payroll Page:
    - Shows only logged-in employee's payroll.
    - Uses request.user.profile only.
    - Reuses _compute_payroll() for current preview.
    - Shows saved payroll history from PayrollItem.
    """

    if request.user.is_staff or request.user.is_superuser:
        return redirect("admin_dashboard")

    try:
        profile = request.user.profile
    except UserProfile.DoesNotExist:
        messages.error(request, "Account profile missing. Contact admin.")
        return redirect("login_ui")

    if not profile.is_approved:
        messages.error(request, "Your account is pending approval by your branch admin.")
        return redirect("login_ui")

    branch = profile.branch
    if not branch:
        messages.error(request, "No branch assigned. Contact admin.")
        return redirect("employee_dashboard")

    employee_id_used = _pick_attendance_employee_id(request.user, branch)

    # -------------------------
    # Selected payroll period
    # -------------------------
    period_id = (request.GET.get("period") or "").strip()

    if period_id.isdigit():
        selected_period = PayrollPeriod.objects.filter(id=int(period_id)).first()
    else:
        selected_period = (
            PayrollPeriod.objects
            .filter(start_date__lte=timezone.localdate(), end_date__gte=timezone.localdate())
            .order_by("-start_date")
            .first()
        )

    if not selected_period:
        selected_period = PayrollPeriod.objects.order_by("-start_date").first()

    payroll_periods = PayrollPeriod.objects.all().order_by("-start_date")[:24]

    rules = _get_or_create_rules(branch)

    preview = None
    computed = {}
    attendance_summary = {}
    rates = {}
    gov = {}
    dtr_rows = []
    issues = []

    if selected_period:
        preview = _compute_payroll(profile, branch, selected_period, rules)
        computed = preview.get("computed_payroll", {})
        attendance_summary = preview.get("attendance_summary", {})
        rates = preview.get("rates", {})
        gov = preview.get("gov", {})
        dtr_rows = preview.get("dtr_rows", [])
        issues_text = preview.get("issues", "")
        if isinstance(issues_text, str) and issues_text:
            issues = [x.strip() for x in issues_text.split(";") if x.strip()]
        elif isinstance(issues_text, list):
            issues = issues_text

    # -------------------------
    # Saved payroll history
    # -------------------------
    payroll_history = (
        PayrollItem.objects
        .select_related("batch", "batch__period", "batch__branch")
        .filter(profile=profile)
        .order_by("-batch__period__start_date", "-batch__created_at")
    )

    latest_saved_item = payroll_history.first()

    context = {
        "current": "payroll",

        "profile": profile,
        "employee_name": request.user.get_full_name() or request.user.username,
        "employee_branch": branch.name,
        "employee_id_used": employee_id_used,

        "payroll_periods": payroll_periods,
        "selected_period": selected_period,

        "rules": rules,
        "rates": rates,
        "attendance_summary": attendance_summary,
        "computed": computed,
        "gov": gov,
        "dtr_rows": dtr_rows,
        "issues": issues,

        "payroll_history": payroll_history,
        "latest_saved_item": latest_saved_item,
    }

    return render(request, "employee/payroll.html", context)

@login_required
@never_cache
def employee_dtr_print(request):
    """
    Printable employee DTR.
    Employee can only print their own DTR.
    """

    if request.user.is_staff or request.user.is_superuser:
        return redirect("admin_dashboard")

    try:
        profile = request.user.profile
    except UserProfile.DoesNotExist:
        messages.error(request, "Account profile missing. Contact admin.")
        return redirect("login_ui")

    branch = profile.branch
    if not branch:
        messages.error(request, "No branch assigned. Contact admin.")
        return redirect("employee_attendance")

    today = timezone.localdate()
    default_start = today.replace(day=1)
    default_end = today

    start_raw = (request.GET.get("start") or "").strip()
    end_raw = (request.GET.get("end") or "").strip()

    try:
        start_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else default_start
    except ValueError:
        start_date = default_start

    try:
        end_date = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else default_end
    except ValueError:
        end_date = default_end

    if end_date < start_date:
        start_date, end_date = end_date, start_date

    rules = _get_or_create_rules(branch)

    period = SimpleNamespace(
        name=f"{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}",
        start_date=start_date,
        end_date=end_date,
        pay_mode=PayrollPeriod.PAY_MONTHLY,
    )

    dtr = _build_dtr_and_summary(profile, branch, period, rules)

    context = {
        "profile": profile,
        "employee_name": request.user.get_full_name() or request.user.username,
        "employee_branch": branch.name,
        "employee_id_used": _pick_attendance_employee_id(request.user, branch),
        "period": period,
        "dtr": dtr,
        "dtr_rows": dtr.get("rows", []),
        "today": today,
    }

    return render(request, "employee/dtr_print.html", context)

def _safe_decimal(value, default="0.00"):
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _payslip_money(value):
    return _money(_safe_decimal(value))


def _get_from_meta(meta, *keys, default=None):
    """
    Safely get possible keys from PayrollItem.meta.
    This helps if your processed PayrollItem saved extra data differently.
    """
    if not isinstance(meta, dict):
        return default

    for key in keys:
        if key in meta:
            return meta.get(key)

    return default
def _get_from_nested_meta(meta, section, key, default=None):
    """
    Safely read nested values from PayrollItem.meta.
    Example:
        meta["permanent_breakdown"]["gsis_employee"]
    """
    if not isinstance(meta, dict):
        return default

    nested = meta.get(section, {})
    if not isinstance(nested, dict):
        return default

    return nested.get(key, default)


def _build_payslip_data(profile, period, branch, rules, generated_by_user, saved_item=None):
    """
    Builds one payslip dictionary for both:
    - Admin payslip page
    - Employee payslip print page

    Supports:
    - JO
    - COS
    - PERMANENT
    """

    live_result = _compute_payroll(profile, branch, period, rules)

    rates = live_result.get("rates", {})
    summary = live_result.get("attendance_summary", {})
    computed = live_result.get("computed_payroll", {})
    gov = live_result.get("gov", {})
    dtr_rows = live_result.get("dtr_rows", [])
    issues = live_result.get("issues", "")

    meta = {}
    source_label = "Live Payroll Preview"
    processed_batch = None

    # -------------------------
    # Start with live values
    # -------------------------
    base_pay = _payslip_money(computed.get("base", Decimal("0.00")))
    basic_salary = _payslip_money(computed.get("basic_salary", base_pay))
    pera = _payslip_money(computed.get("pera", Decimal("0.00")))
    other_earnings = _payslip_money(computed.get("other_earnings", Decimal("0.00")))

    premium_pay = _payslip_money(computed.get("premium", Decimal("0.00")))
    overtime_hours = _safe_decimal(computed.get("overtime_hours", Decimal("0.00")))
    overtime_pay = _payslip_money(computed.get("ot", Decimal("0.00")))

    gross_pay = _payslip_money(computed.get("gross", base_pay + pera + other_earnings + premium_pay + overtime_pay))

    late_minutes = int(computed.get("late_minutes", 0) or 0)
    undertime_minutes = int(computed.get("undertime_minutes", 0) or 0)
    absences = int(computed.get("absences", 0) or 0)

    late_deduction = _payslip_money(computed.get("late_deduction", Decimal("0.00")))
    undertime_deduction = _payslip_money(computed.get("undertime_deduction", Decimal("0.00")))
    absence_deduction = _payslip_money(computed.get("absence_deduction", Decimal("0.00")))
    attendance_deduction = _payslip_money(computed.get("attendance_deduction", late_deduction + undertime_deduction + absence_deduction))

    manual_deduction = _payslip_money(computed.get("manual_deduction", Decimal("0.00")))
    loan_deduction = _payslip_money(computed.get("loan_deduction", Decimal("0.00")))
    other_deduction = _payslip_money(computed.get("other_deduction", Decimal("0.00")))

    deductions_total = _payslip_money(computed.get("deductions", Decimal("0.00")))
    net_pay = _payslip_money(computed.get("net", Decimal("0.00")))

    sss = _payslip_money(gov.get("sss", Decimal("0.00")))
    philhealth = _payslip_money(gov.get("philhealth", Decimal("0.00")))
    pagibig = _payslip_money(gov.get("pagibig", Decimal("0.00")))
    gov_total = _payslip_money(gov.get("gov_total", Decimal("0.00")))
    tax = _payslip_money(gov.get("tax", Decimal("0.00")))

    wtax = _payslip_money(gov.get("wtax", tax))
    gsis_employee = _payslip_money(gov.get("gsis_employee", Decimal("0.00")))
    gsis_employer = _payslip_money(gov.get("gsis_employer", Decimal("0.00")))
    other_employer_contribution = _payslip_money(gov.get("other_employer_contribution", Decimal("0.00")))
    employer_contributions_total = _payslip_money(gov.get("employer_contributions_total", Decimal("0.00")))

    # -------------------------
    # If processed payroll exists, use saved values as official values
    # -------------------------
    if saved_item:
        source_label = "Processed Payroll Record"
        processed_batch = saved_item.batch
        meta = saved_item.meta or {}

        base_pay = _payslip_money(saved_item.base_pay)
        basic_salary = _payslip_money(
            _get_from_nested_meta(meta, "permanent_breakdown", "basic_salary", default=basic_salary)
        )

        pera = _payslip_money(
            _get_from_nested_meta(meta, "permanent_breakdown", "pera", default=pera)
        )

        other_earnings = _payslip_money(
            _get_from_nested_meta(meta, "permanent_breakdown", "other_earnings", default=other_earnings)
        )

        premium_pay = _payslip_money(saved_item.premium_pay)
        overtime_hours = _safe_decimal(saved_item.overtime_hours)
        overtime_pay = _payslip_money(saved_item.overtime_pay)

        late_minutes = int(saved_item.late_minutes or 0)
        undertime_minutes = int(saved_item.undertime_minutes or 0)
        absences = int(saved_item.absences or 0)

        manual_deduction = _payslip_money(saved_item.manual_deduction)
        gov_total = _payslip_money(saved_item.gov_contributions_total)
        tax = _payslip_money(saved_item.tax_total)
        deductions_total = _payslip_money(saved_item.deductions_total)
        net_pay = _payslip_money(saved_item.net_pay)

        issues = saved_item.issues or issues

        sss = _payslip_money(_get_from_meta(meta, "sss", "sss_amount", default=sss))
        pagibig = _payslip_money(_get_from_meta(meta, "pagibig", "pagibig_amount", default=pagibig))
        philhealth = _payslip_money(_get_from_meta(meta, "philhealth", "philhealth_amount", default=philhealth))

        wtax = _payslip_money(
            _get_from_nested_meta(meta, "permanent_breakdown", "wtax", default=wtax)
        )

        gsis_employee = _payslip_money(
            _get_from_nested_meta(meta, "permanent_breakdown", "gsis_employee", default=gsis_employee)
        )

        gsis_employer = _payslip_money(
            _get_from_nested_meta(meta, "permanent_breakdown", "gsis_employer", default=gsis_employer)
        )

        loan_deduction = _payslip_money(
            _get_from_nested_meta(meta, "permanent_breakdown", "loan_deduction", default=loan_deduction)
        )

        other_deduction = _payslip_money(
            _get_from_nested_meta(meta, "permanent_breakdown", "other_deduction", default=other_deduction)
        )

        other_employer_contribution = _payslip_money(
            _get_from_nested_meta(meta, "permanent_breakdown", "other_employer_contribution", default=other_employer_contribution)
        )

        employer_contributions_total = _payslip_money(
            _get_from_nested_meta(meta, "permanent_breakdown", "employer_contributions_total", default=employer_contributions_total)
        )

        late_deduction = _payslip_money(_get_from_meta(meta, "late_deduction", default=late_deduction))
        undertime_deduction = _payslip_money(_get_from_meta(meta, "undertime_deduction", default=undertime_deduction))
        absence_deduction = _payslip_money(_get_from_meta(meta, "absence_deduction", default=absence_deduction))

        attendance_deduction = _payslip_money(
            _get_from_meta(
                meta,
                "attendance_deduction",
                default=late_deduction + undertime_deduction + absence_deduction,
            )
        )

        meta_dtr_rows = _get_from_meta(meta, "dtr_rows", "dtr", default=None)
        if meta_dtr_rows:
            dtr_rows = meta_dtr_rows

        # Rebuild gross from saved official line items.
        gross_pay = _money(base_pay + pera + other_earnings + premium_pay + overtime_pay)

    is_permanent = profile.employment_type == UserProfile.EMP_PERMANENT

    reference_code = f"ITHR-{period.id}-{profile.id}-{timezone.now().strftime('%Y%m%d%H%M')}"

    payslip = {
        "reference_code": reference_code,
        "source_label": source_label,

        "employee_name": profile.user.get_full_name() or profile.user.username,
        "employee_username": profile.user.username,
        "employee_id": live_result.get("picked_employee_id") or profile.biometric_employee_id or profile.user.id,
        "branch": branch.name if branch else "—",
        "department": profile.department or "—",
        "position": profile.position or "—",
        "employment_type": profile.employment_type,
        "is_permanent": is_permanent,

        "period_name": period.name,
        "period_start": period.start_date,
        "period_end": period.end_date,
        "pay_mode": period.get_pay_mode_display() if hasattr(period, "get_pay_mode_display") else period.pay_mode,

        "date_generated": timezone.now(),
        "generated_by": generated_by_user.get_full_name() or generated_by_user.username,

        "daily_rate": _payslip_money(rates.get("daily")),
        "hourly_rate": _payslip_money(rates.get("hourly")),
        "per_minute_rate": _payslip_money(rates.get("per_minute")),

        # Earnings
        "base_pay": _money(base_pay),
        "basic_salary": _money(basic_salary),
        "pera": _money(pera),
        "other_earnings": _money(other_earnings),
        "premium_pay": _money(premium_pay),
        "overtime_hours": overtime_hours,
        "overtime_pay": _money(overtime_pay),
        "gross_pay": _money(gross_pay),

        # Attendance basis
        "days_present": summary.get("present_days", summary.get("days_present", 0)),
        "travel_days": summary.get("travel_days", 0),
        "holiday_days": summary.get("holiday_days", 0),
        "absences": absences,
        "missing_logs": summary.get("missing_logs", 0),
        "late_minutes": late_minutes,
        "undertime_minutes": undertime_minutes,

        # Attendance deductions
        "late_deduction": _money(late_deduction),
        "undertime_deduction": _money(undertime_deduction),
        "absence_deduction": _money(absence_deduction),
        "attendance_deduction": _money(attendance_deduction),

        # JO/COS deductions
        "sss": _money(sss),
        "philhealth": _money(philhealth),
        "pagibig": _money(pagibig),
        "tax": _money(tax),

        # Permanent deductions
        "wtax": _money(wtax),
        "gsis_employee": _money(gsis_employee),
        "gsis_employer": _money(gsis_employer),
        "loan_deduction": _money(loan_deduction),
        "other_deduction": _money(other_deduction),
        "other_employer_contribution": _money(other_employer_contribution),
        "employer_contributions_total": _money(employer_contributions_total),

        # Totals
        "gov_total": _money(gov_total),
        "manual_deduction": _money(manual_deduction),
        "total_deductions": _money(deductions_total),
        "net_pay": _money(net_pay),

        "issues": issues,
    }

    return {
        "payslip": payslip,
        "dtr_rows": dtr_rows,
        "saved_item": saved_item,
        "processed_batch": processed_batch,
        "meta": meta,
    }

@login_required
@never_cache
def employee_payslip_print(request, item_id):
    """
    Printable employee payslip.
    Security rule:
    Employee can only open PayrollItem where PayrollItem.profile == request.user.profile.

    Supports:
    - JO
    - COS
    - PERMANENT
    """

    if request.user.is_staff or request.user.is_superuser:
        return redirect("admin_dashboard")

    try:
        profile = request.user.profile
    except UserProfile.DoesNotExist:
        messages.error(request, "Account profile missing. Contact admin.")
        return redirect("login_ui")

    item = get_object_or_404(
        PayrollItem.objects.select_related(
            "batch",
            "batch__period",
            "batch__branch",
            "profile",
            "profile__user",
            "profile__branch",
        ),
        id=item_id,
        profile=profile,
    )

    branch = item.batch.branch or profile.branch
    period = item.batch.period

    if not branch:
        messages.error(request, "No branch found for this payroll item.")
        return redirect("employee_payroll")

    rules = _get_or_create_rules(branch)

    payslip_data = _build_payslip_data(
        profile=profile,
        period=period,
        branch=branch,
        rules=rules,
        generated_by_user=request.user,
        saved_item=item,
    )

    context = {
        "item": item,
        "profile": profile,
        "employee_name": request.user.get_full_name() or request.user.username,
        "period": period,
        "batch": item.batch,
        "branch": branch,
        "payslip": payslip_data["payslip"],
        "meta": payslip_data["meta"],
        "dtr_rows": payslip_data["dtr_rows"],
        "today": timezone.localdate(),
    }

    return render(request, "employee/payslip_print.html", context)
    

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
from django.core.paginator import Paginator

@login_required
@never_cache
def admin_biometrics_attendance(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    branches_qs = _scoped_branch_queryset_for_admin(request)
    admin_branch = _get_admin_branch(request)

    records_qs = AttendanceRecord.objects.select_related("branch").all()

    if admin_branch:
        records_qs = records_qs.filter(branch=admin_branch)

    records_qs = records_qs.order_by("-timestamp")

    paginator = Paginator(records_qs, 20)
    page_number = request.GET.get("page")
    records = paginator.get_page(page_number)

    today = timezone.localdate()
    today_records = records_qs.filter(timestamp__date=today)

    employees_qs = UserProfile.objects.select_related("user", "branch").filter(
        is_approved=True,
        user__is_staff=False,
        user__is_superuser=False,
    )

    if admin_branch:
        employees_qs = employees_qs.filter(branch=admin_branch)

    present_ids = set(
        today_records.filter(attendance_status=AttendanceRecord.STATUS_CHECKIN)
        .values_list("employee_id", flat=True)
    )

    travel_today_qs = TravelOrder.objects.select_related(
        "employee", "employee__user", "employee__branch"
    ).filter(
        start_date__lte=today,
        end_date__gte=today,
    )

    if admin_branch:
        travel_today_qs = travel_today_qs.filter(employee__branch=admin_branch)

    travel_count = travel_today_qs.count()

    late_count = today_records.filter(
        attendance_status=AttendanceRecord.STATUS_CHECKIN,
        timestamp__time__gt=time(8, 15),
    ).count()

    total_employees = employees_qs.count()
    present_count = len(present_ids)
    absent_count = max(total_employees - present_count - travel_count, 0)

    kpi = {
        "present": present_count,
        "late": late_count,
        "absent": absent_count,
        "on_travel": travel_count,
        "last_sync": records_qs.order_by("-created_at").values_list("created_at", flat=True).first(),
    }

    # ✅ FIXED: filter first, slice last
    holidays_qs = HolidaySuspension.objects.all().order_by("-date")

    if admin_branch:
        holidays_qs = holidays_qs.filter(
            Q(scope=HolidaySuspension.SCOPE_NATIONWIDE)
            | Q(scope=HolidaySuspension.SCOPE_REGION)
            | Q(scope=HolidaySuspension.SCOPE_BRANCH, branch=admin_branch)
        )

    holidays = holidays_qs[:20]

    # ✅ FIXED: filter first, slice last
    travel_orders_qs = TravelOrder.objects.select_related(
        "employee", "employee__user", "employee__branch"
    ).order_by("-start_date")

    if admin_branch:
        travel_orders_qs = travel_orders_qs.filter(employee__branch=admin_branch)

    travel_orders = travel_orders_qs[:20]

    context = {
        "current": "biometrics",
        "records": records,
        "kpi": kpi,
        "preview_rows": [],
        "import_errors": [],
        "import_summary": "",
        "branches": branches_qs.values_list("id", "name"),
        "can_import": bool(_load_import_cache(request)),
        "employees": employees_qs.order_by("user__username"),
        "travel_orders": travel_orders,
        "travel_today": travel_today_qs,
        "holidays": holidays,
    }

    return render(request, "admin/Biometrics_attendance.html", context)
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
def admin_biometrics_create_holiday(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    name = (request.POST.get("name") or "").strip()
    date_value = (request.POST.get("date") or "").strip()
    type_value = (request.POST.get("type") or HolidaySuspension.TYPE_HOLIDAY).strip()
    scope = (request.POST.get("scope") or HolidaySuspension.SCOPE_REGION).strip()
    branch_id = (request.POST.get("branch") or "").strip()
    notes = (request.POST.get("notes") or "").strip()

    if not name or not date_value:
        return JsonResponse({"ok": False, "error": "Name and date are required."}, status=400)

    try:
        holiday_date = datetime.strptime(date_value, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid date format."}, status=400)

    branch = None
    if scope == HolidaySuspension.SCOPE_BRANCH:
        if not branch_id:
            return JsonResponse({"ok": False, "error": "Branch is required for branch scope."}, status=400)
        branch = Branch.objects.filter(id=branch_id).first()
        if not branch:
            return JsonResponse({"ok": False, "error": "Invalid branch."}, status=400)

    HolidaySuspension.objects.create(
        name=name,
        date=holiday_date,
        type=type_value,
        scope=scope,
        branch=branch,
        notes=notes,
    )

    return JsonResponse({"ok": True})


@login_required
@require_POST
def admin_biometrics_update_holiday(request, holiday_id):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    holiday = get_object_or_404(HolidaySuspension, id=holiday_id)

    name = (request.POST.get("name") or "").strip()
    date_value = (request.POST.get("date") or "").strip()
    type_value = (request.POST.get("type") or HolidaySuspension.TYPE_HOLIDAY).strip()
    scope = (request.POST.get("scope") or HolidaySuspension.SCOPE_REGION).strip()
    branch_id = (request.POST.get("branch") or "").strip()
    notes = (request.POST.get("notes") or "").strip()

    if not name or not date_value:
        return JsonResponse({"ok": False, "error": "Name and date are required."}, status=400)

    try:
        holiday.date = datetime.strptime(date_value, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"ok": False, "error": "Invalid date format."}, status=400)

    branch = None
    if scope == HolidaySuspension.SCOPE_BRANCH:
        if not branch_id:
            return JsonResponse({"ok": False, "error": "Branch is required for branch scope."}, status=400)
        branch = Branch.objects.filter(id=branch_id).first()
        if not branch:
            return JsonResponse({"ok": False, "error": "Invalid branch."}, status=400)

    holiday.name = name
    holiday.type = type_value
    holiday.scope = scope
    holiday.branch = branch
    holiday.notes = notes
    holiday.save()

    return JsonResponse({"ok": True})


@login_required
@require_POST
def admin_biometrics_delete_holiday(request, holiday_id):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    holiday = get_object_or_404(HolidaySuspension, id=holiday_id)
    holiday.delete()

    return JsonResponse({"ok": True})

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
# Payroll helpers / Payroll Engine
# -------------------------

SALARY_DIVISOR = Decimal("22")
DAILY_HOURS = Decimal("8")
OT_MULTIPLIER = Decimal("1.25")


def _money(value):
    return Decimal(value or 0).quantize(Decimal("0.01"))


def _get_or_create_rules(branch: Branch) -> PayrollRule:
    rules, _ = PayrollRule.objects.get_or_create(branch=branch)
    return rules


def _get_or_create_contrib(profile: UserProfile) -> EmployeeContribution:
    contrib, _ = EmployeeContribution.objects.get_or_create(profile=profile)
    return contrib


def _daterange(d1: date, d2: date):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


def _is_weekday(d: date):
    return d.weekday() < 5


def _ensure_aware(dt: datetime) -> datetime:
    if not dt:
        return dt
    if settings.USE_TZ and timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _scoped_branch_for_admin_or_404(request, branch_id):
    if request.user.is_superuser:
        if branch_id and str(branch_id).isdigit():
            return Branch.objects.filter(id=int(branch_id)).first()
        return Branch.objects.first()

    try:
        admin_branch = request.user.profile.branch
    except UserProfile.DoesNotExist:
        return None

    if branch_id and str(branch_id).isdigit() and admin_branch and int(branch_id) != admin_branch.id:
        return None

    return admin_branch


def _get_profile_biometric_id(profile: UserProfile):
    """
    Payroll must match AttendanceRecord.employee_id using Hikvision employee number.
    Example:
    UserProfile.biometric_employee_id = 3
    AttendanceRecord.employee_id = 3
    """
    biometric_id = str(getattr(profile, "biometric_employee_id", "") or "").strip()
    if biometric_id:
        return biometric_id

    # fallback only, but biometric_employee_id should be filled
    return str(profile.user.username).strip()


def _holiday_for(branch: Branch, d: date):
    return HolidaySuspension.objects.filter(date=d).filter(
        Q(scope=HolidaySuspension.SCOPE_NATIONWIDE)
        | Q(scope=HolidaySuspension.SCOPE_REGION)
        | Q(scope=HolidaySuspension.SCOPE_BRANCH, branch=branch)
    ).first()


def _is_travel_day(profile: UserProfile, d: date):
    return TravelOrder.objects.filter(
        employee=profile,
        start_date__lte=d,
        end_date__gte=d,
    ).exists()


def _flag_ceremony_day_for_week(branch: Branch, d: date):
    """
    Monday = flag ceremony.
    If Monday holiday, move to next working day.
    """
    monday = d - timedelta(days=d.weekday())

    for i in range(5):
        candidate = monday + timedelta(days=i)
        if not _holiday_for(branch, candidate):
            return candidate

    return monday


def _late_cutoff_for_day(branch: Branch, d: date, rules: PayrollRule):
    flag_day = _flag_ceremony_day_for_week(branch, d)

    if d == flag_day:
        return rules.flag_ceremony_cutoff_time or time(8, 0)

    normal_start = rules.work_start_time or time(8, 0)
    grace = int(rules.grace_minutes_normal or 15)
    return (datetime.combine(date.today(), normal_start) + timedelta(minutes=grace)).time()


def _rate_info(profile: UserProfile):
    monthly = Decimal(profile.monthly_salary or 0)
    daily = Decimal(profile.daily_rate or 0)

    if daily <= 0 and monthly > 0:
        daily = monthly / SALARY_DIVISOR

    hourly = daily / DAILY_HOURS if daily > 0 else Decimal("0")
    per_minute = hourly / Decimal("60") if hourly > 0 else Decimal("0")

    return {
        "monthly": _money(monthly),
        "daily": _money(daily),
        "hourly": _money(hourly),
        "per_minute": _money(per_minute),
    }


def _daily_logs(employee_id: str, branch: Branch, d: date):
    qs = AttendanceRecord.objects.filter(
        branch=branch,
        employee_id=employee_id,
        timestamp__date=d,
    ).order_by("timestamp")

    ins = []
    outs = []

    for rec in qs:
        if rec.attendance_status == AttendanceRecord.STATUS_CHECKIN:
            ins.append(rec.timestamp)
        elif rec.attendance_status == AttendanceRecord.STATUS_CHECKOUT:
            outs.append(rec.timestamp)

    return ins, outs


# =========================================================
# PAYROLL STEP 2 HELPERS
# Attendance matching + DTR summary computation
# =========================================================

def _money(value):
    """
    Safe money formatter for Decimal values.
    If you already have _money above this, you may keep only one version.
    """
    try:
        return Decimal(value or 0).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")


def _normalize_emp_id(value):
    """
    Normalize biometric IDs:
    3, "3", " 3 " => "3"
    """
    return str(value or "").strip()


def _get_profile_biometric_id(profile):
    """
    The official attendance identity must come from UserProfile.biometric_employee_id.
    Fallbacks are only for debugging / old data compatibility.
    """
    bio_id = _normalize_emp_id(getattr(profile, "biometric_employee_id", ""))

    if bio_id:
        return bio_id

    # fallback only if biometric_employee_id is empty
    return _normalize_emp_id(getattr(profile.user, "id", ""))


def _normalize_status_text(value):
    return str(value or "").strip().lower().replace("-", "").replace("_", "").replace(" ", "")


def _record_attendance_kind(record):
    """
    Returns: "in", "out", or "unknown"

    IMPORTANT:
    Your SQL dump shows some Hikvision Check Out logs were saved as CHECK_IN.
    So we trust raw_row['label'] / raw_row['attendanceStatus'] first,
    then fallback to AttendanceRecord.attendance_status.
    """
    raw = record.raw_row or {}

    raw_status = _normalize_status_text(raw.get("attendanceStatus"))
    raw_label = _normalize_status_text(raw.get("label"))
    db_status = _normalize_status_text(record.attendance_status)

    # Hikvision raw values
    if raw_status in {"checkin", "timein", "in"}:
        return "in"
    if raw_status in {"checkout", "timeout", "out"}:
        return "out"

    if raw_label in {"checkin", "timein", "in"}:
        return "in"
    if raw_label in {"checkout", "timeout", "out"}:
        return "out"

    # Django stored values
    if db_status in {"checkin", "check_in", "timein", "in"}:
        return "in"
    if db_status in {"checkout", "check_out", "timeout", "out"}:
        return "out"

    return "unknown"


def _record_local_datetime(record):
    """
    Prefer Hikvision raw time because it contains +08:00.
    This avoids timezone mismatch when DB stores UTC-like timestamp.
    """
    raw = record.raw_row or {}
    raw_time = raw.get("time")

    if raw_time:
        try:
            dt = parse_datetime(str(raw_time))
            if dt:
                if timezone.is_aware(dt):
                    return timezone.localtime(dt)
                return timezone.make_aware(dt, timezone.get_current_timezone())
        except Exception:
            pass

    ts = record.timestamp

    if timezone.is_aware(ts):
        return timezone.localtime(ts)

    if settings.USE_TZ:
        try:
            return timezone.make_aware(ts, timezone.get_current_timezone())
        except Exception:
            return ts

    return ts


def _date_range(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _is_weekend(day):
    # Monday = 0, Sunday = 6
    return day.weekday() >= 5


def _holidays_for_period(branch, period):
    """
    Return dictionary:
    {
        date: HolidaySuspension object
    }
    """
    qs = HolidaySuspension.objects.filter(
        date__gte=period.start_date,
        date__lte=period.end_date,
    ).filter(
        Q(scope=HolidaySuspension.SCOPE_NATIONWIDE)
        | Q(scope=HolidaySuspension.SCOPE_REGION)
        | Q(scope=HolidaySuspension.SCOPE_BRANCH, branch=branch)
    )

    return {h.date: h for h in qs}


def _is_holiday_for_branch(day, holiday_map):
    return day in holiday_map


def _is_flag_ceremony_day(day, holiday_map):
    """
    Monday flag ceremony rule:
    - Normal: Monday is flag ceremony day.
    - If Monday is holiday/suspension, move to next working day.
    - If Monday and Tuesday are holidays, move to Wednesday, etc.
    """
    monday = day - timedelta(days=day.weekday())

    candidate = monday
    for _ in range(5):  # Mon-Fri only
        if not _is_weekend(candidate) and not _is_holiday_for_branch(candidate, holiday_map):
            return day == candidate
        candidate += timedelta(days=1)

    return False


def _time_to_str(value):
    if not value:
        return ""
    try:
        return value.strftime("%I:%M %p")
    except Exception:
        return str(value)


def _minutes_between(start_time, end_time, day):
    if not start_time or not end_time:
        return 0

    start_dt = datetime.combine(day, start_time)
    end_dt = datetime.combine(day, end_time)

    if end_dt < start_dt:
        return 0

    return int((end_dt - start_dt).total_seconds() // 60)


def _build_dtr_and_summary(profile, branch, period, rules):
    """
    STEP 2 FIX:
    Build DTR rows and attendance summary from AttendanceRecord.

    Main fixes:
    1. Match AttendanceRecord.employee_id to UserProfile.biometric_employee_id.
    2. Use raw_row['label'] / raw_row['attendanceStatus'] to detect Check In / Check Out.
    3. Use raw_row['time'] to avoid timezone mismatch.
    4. Compute present, absent, late, undertime, travel, holiday/suspension.
    """

    employee_id = _get_profile_biometric_id(profile)
    issues = []

    if not employee_id:
        issues.append("Missing biometric employee ID")

    # This is your period range.
    start_day = period.start_date
    end_day = period.end_date

    # Wide datetime range. We still group using local/raw date below.
    start_dt = datetime.combine(start_day, time.min)
    end_dt = datetime.combine(end_day + timedelta(days=1), time.min)

    if settings.USE_TZ:
        try:
            start_dt = timezone.make_aware(start_dt, timezone.get_current_timezone())
            end_dt = timezone.make_aware(end_dt, timezone.get_current_timezone())
        except Exception:
            pass

    # Employee ID candidates. Official is biometric_employee_id.
    employee_id_candidates = {
        _normalize_emp_id(employee_id),
        _normalize_emp_id(profile.user.id),
        _normalize_emp_id(profile.user.username),
    }
    employee_id_candidates = [x for x in employee_id_candidates if x]

    # IMPORTANT:
    # First, get records without strict branch filter.
    # This helps if older attendance rows have null/wrong branch.
    records_qs = AttendanceRecord.objects.filter(
        employee_id__in=employee_id_candidates,
        timestamp__gte=start_dt,
        timestamp__lt=end_dt,
    ).order_by("timestamp")

    records_without_branch_count = records_qs.count()

    # Then prefer same-branch records.
    branch_records_qs = records_qs.filter(branch=branch)

    if branch_records_qs.exists():
        records_qs = branch_records_qs
    else:
        # Fallback: use records even if branch is missing/wrong, but warn.
        if records_without_branch_count > 0:
            issues.append("Attendance branch mismatch or blank branch; used employee ID match fallback")

    records = list(records_qs)

    # Debug print. Keep this while testing.
    print("========== PAYROLL DTR DEBUG ==========")
    print("USER:", profile.user.username)
    print("PROFILE ID:", profile.id)
    print("BIO ID USED:", employee_id)
    print("PERIOD:", start_day, "to", end_day)
    print("BRANCH:", branch.name if branch else None)
    print("EMPLOYEE ID CANDIDATES:", employee_id_candidates)
    print("RECORDS FOUND WITHOUT BRANCH FILTER:", records_without_branch_count)
    print("RECORDS USED:", len(records))
    print("MATCHING RECORD BRANCHES:", list(records_qs.values_list("branch__name", flat=True).distinct()))
    print("MATCHING RECORD SAMPLE:", [
        {
            "id": r.id,
            "employee_id": r.employee_id,
            "timestamp": str(r.timestamp),
            "local": str(_record_local_datetime(r)),
            "db_status": r.attendance_status,
            "raw_label": (r.raw_row or {}).get("label"),
            "raw_attendanceStatus": (r.raw_row or {}).get("attendanceStatus"),
            "kind": _record_attendance_kind(r),
            "branch": r.branch.name if r.branch else None,
        }
        for r in records[:10]
    ])
    print("=======================================")

    # Group records by local date
    records_by_day = defaultdict(list)

    for rec in records:
        local_dt = _record_local_datetime(rec)
        local_day = local_dt.date()

        if start_day <= local_day <= end_day:
            records_by_day[local_day].append(rec)

    holiday_map = _holidays_for_period(branch, period)

    travel_qs = TravelOrder.objects.filter(
        employee=profile,
        start_date__lte=end_day,
        end_date__gte=start_day,
    )

    travel_days_set = set()
    for travel in travel_qs:
        s = max(travel.start_date, start_day)
        e = min(travel.end_date, end_day)
        for d in _date_range(s, e):
            if not _is_weekend(d):
                travel_days_set.add(d)

    required_minutes = int(Decimal(rules.daily_hours_required or 8) * Decimal("60"))

    rows = []

    days_present = 0
    travel_days = 0
    holiday_days = 0
    absences = 0
    missing_logs = 0

    late_minutes_total = 0
    undertime_minutes_total = 0

    for current_day in _date_range(start_day, end_day):
        weekday_name = current_day.strftime("%A")
        is_weekend = _is_weekend(current_day)
        holiday_obj = holiday_map.get(current_day)
        is_holiday = bool(holiday_obj)
        is_travel = current_day in travel_days_set

        day_records = sorted(
            records_by_day.get(current_day, []),
            key=lambda r: _record_local_datetime(r)
        )

        in_times = []
        out_times = []
        unknown_times = []

        for rec in day_records:
            local_dt = _record_local_datetime(rec)
            kind = _record_attendance_kind(rec)

            if kind == "in":
                in_times.append(local_dt.time().replace(second=0, microsecond=0))
            elif kind == "out":
                out_times.append(local_dt.time().replace(second=0, microsecond=0))
            else:
                unknown_times.append(local_dt.time().replace(second=0, microsecond=0))

        # Remove duplicates while preserving order
        in_times = sorted(set(in_times))
        out_times = sorted(set(out_times))
        unknown_times = sorted(set(unknown_times))

        has_in = bool(in_times)
        has_out = bool(out_times)
        has_any_log = bool(in_times or out_times or unknown_times)

        am_in = ""
        am_out = ""
        pm_in = ""
        pm_out = ""

        total_rendered_minutes = 0
        late_minutes = 0
        undertime_minutes = 0

        status = "Absent"
        remarks = ""

        if is_weekend:
            status = "Weekend"
            remarks = "Weekend"

        elif is_holiday:
            status = "Holiday/Suspension"
            remarks = holiday_obj.name if holiday_obj else "Holiday/Suspension"
            holiday_days += 1

        elif is_travel:
            status = "Official Travel"
            remarks = "Official travel"
            travel_days += 1
            days_present += 1

        elif has_any_log:
            status = "Present"
            days_present += 1

            first_in = in_times[0] if in_times else None
            last_out = out_times[-1] if out_times else None

            # Civil Service Form No. 48 style columns
            morning_ins = [t for t in in_times if t < time(12, 0)]
            afternoon_ins = [t for t in in_times if t >= time(12, 0)]

            morning_outs = [t for t in out_times if t <= time(12, 59)]
            afternoon_outs = [t for t in out_times if t > time(12, 0)]

            if morning_ins:
                am_in = _time_to_str(morning_ins[0])
            elif first_in:
                am_in = _time_to_str(first_in)

            if morning_outs:
                am_out = _time_to_str(morning_outs[-1])

            if afternoon_ins:
                pm_in = _time_to_str(afternoon_ins[0])

            if afternoon_outs:
                pm_out = _time_to_str(afternoon_outs[-1])
            elif last_out:
                pm_out = _time_to_str(last_out)

            if first_in and last_out:
                span_minutes = _minutes_between(first_in, last_out, current_day)

                # Deduct 1 hour lunch if work span crosses lunch period.
                lunch_deduct = 0
                if first_in < time(12, 0) and last_out > time(13, 0):
                    lunch_deduct = 60

                total_rendered_minutes = max(0, span_minutes - lunch_deduct)

            elif first_in and not last_out:
                status = "Incomplete"
                remarks = "Missing check-out"
                missing_logs += 1

            elif last_out and not first_in:
                status = "Incomplete"
                remarks = "Missing check-in"
                missing_logs += 1

            else:
                status = "Incomplete"
                remarks = "Unknown attendance logs"
                missing_logs += 1

            # Late computation
            if first_in:
                if _is_flag_ceremony_day(current_day, holiday_map):
                    threshold = rules.flag_ceremony_cutoff_time
                else:
                    threshold_dt = datetime.combine(current_day, rules.work_start_time) + timedelta(
                        minutes=int(rules.grace_minutes_normal or 15)
                    )
                    threshold = threshold_dt.time()

                if first_in > threshold:
                    late_minutes = _minutes_between(threshold, first_in, current_day)

            # Undertime computation
            if first_in and last_out:
                if total_rendered_minutes < required_minutes:
                    undertime_minutes = required_minutes - total_rendered_minutes

            late_minutes_total += late_minutes
            undertime_minutes_total += undertime_minutes

        else:
            status = "Absent"
            remarks = "No attendance logs"
            absences += 1

        total_hours = Decimal(total_rendered_minutes) / Decimal("60")

        rows.append({
            "date": current_day.isoformat(),
            "day": current_day.day,
            "weekday": weekday_name,

            # Civil Service Form No. 48 style fields
            "am_in": am_in,
            "am_out": am_out,
            "pm_in": pm_in,
            "pm_out": pm_out,

            "total_hours": f"{total_hours:.2f}",
            "late": int(late_minutes),
            "undertime": int(undertime_minutes),

            # Optional display helpers
            "undertime_hour": int(undertime_minutes // 60),
            "undertime_minute": int(undertime_minutes % 60),

            "status": status,
            "remarks": remarks,

            "raw_log_count": len(day_records),
        })

    return {
        "employee_id_used": employee_id,
        "rows": rows,

        "days_present": int(days_present),
        "travel_days": int(travel_days),
        "holiday_days": int(holiday_days),
        "absences": int(absences),
        "missing_logs": int(missing_logs),

        "late_minutes": int(late_minutes_total),
        "undertime_minutes": int(undertime_minutes_total),

        "records_found": int(records_without_branch_count),
        "records_used": int(len(records)),
        "issues": issues,
    }


    
def _compute_payroll(profile: UserProfile, branch: Branch, period: PayrollPeriod, rules: PayrollRule):
    """
    Safe payroll computation for:
    - Job Order (JO)
    - Contract of Service (COS)
    - Permanent employees

    Main rule:
    JO:
        No work, no pay.

    COS:
        Monthly or semi-monthly base pay.
        Existing JO/COS behavior is preserved as much as possible.

    PERMANENT:
        Gross = basic salary + PERA + other earnings + approved overtime
        Deductions = WTAX + PhilHealth + GSIS employee share + Pag-IBIG
                     + loans + other deductions + attendance deductions if policy applies
        Employer contributions are tracked but NOT deducted from net pay.
    """

    issues = []

    dtr = _build_dtr_and_summary(profile, branch, period, rules)
    issues.extend(dtr.get("issues", []))

    emp_type = str(profile.employment_type or "").upper()

    # -------------------------
    # Money helper inside function
    # -------------------------
    def D(value, default="0.00"):
        try:
            return Decimal(value or default)
        except Exception:
            return Decimal(default)

    # -------------------------
    # Rates
    # -------------------------
    monthly_salary = D(profile.monthly_salary)
    daily_rate_profile = D(profile.daily_rate)

    salary_divisor = D(rules.salary_divisor, "22")
    if salary_divisor <= 0:
        salary_divisor = Decimal("22")

    daily_required_hours = D(rules.daily_hours_required, "8")
    if daily_required_hours <= 0:
        daily_required_hours = Decimal("8")

    # Daily Rate = Monthly Salary / Salary Divisor
    # For permanent employees, monthly_salary is treated as basic salary.
    if monthly_salary > 0:
        daily_rate = monthly_salary / salary_divisor
    else:
        daily_rate = daily_rate_profile

    hourly_rate = daily_rate / daily_required_hours if daily_required_hours > 0 else Decimal("0.00")
    per_minute_rate = hourly_rate / Decimal("60") if hourly_rate > 0 else Decimal("0.00")

    if daily_rate <= 0 and monthly_salary <= 0:
        issues.append("No daily/monthly salary configured")

    # -------------------------
    # Attendance summary
    # -------------------------
    present_days = int(dtr.get("days_present", 0) or 0)
    travel_days = int(dtr.get("travel_days", 0) or 0)
    holiday_days = int(dtr.get("holiday_days", 0) or 0)
    absences = int(dtr.get("absences", 0) or 0)
    late_minutes = int(dtr.get("late_minutes", 0) or 0)
    undertime_minutes = int(dtr.get("undertime_minutes", 0) or 0)

    # In your current DTR builder, official travel is already treated as a paid status.
    # So we use present_days as the paid basis to avoid double-counting travel.
    paid_attendance_days = present_days

    # -------------------------
    # Base pay
    # -------------------------
    pera_allowance = D(getattr(profile, "pera_allowance", Decimal("0.00")))
    other_earnings_amount = D(getattr(profile, "other_earnings_amount", Decimal("0.00")))

    pera_for_period = Decimal("0.00")
    other_earnings_for_period = Decimal("0.00")
    base_pay = Decimal("0.00")

    if emp_type == UserProfile.EMP_JO:
        # JO = no work, no pay.
        # Gross basic pay is based only on actual paid attendance days.
        base_pay = daily_rate * Decimal(paid_attendance_days)

    elif emp_type == UserProfile.EMP_COS:
        # COS = fixed monthly/semi-monthly base.
        # This preserves your current behavior.
        if monthly_salary > 0:
            if period.pay_mode == PayrollPeriod.PAY_MONTHLY:
                base_pay = monthly_salary
            else:
                base_pay = monthly_salary / Decimal("2")
        else:
            # Fallback if COS has no monthly salary but has daily rate.
            working_days = 0
            for row in dtr.get("rows", []):
                if row.get("status") not in ["Weekend", "Holiday/Suspension"]:
                    working_days += 1

            base_pay = daily_rate * Decimal(max(0, working_days - absences))

    elif emp_type == UserProfile.EMP_PERMANENT:
        # Permanent = basic salary for the period.
        # PERA and other earnings are added to gross.
        if monthly_salary > 0:
            if period.pay_mode == PayrollPeriod.PAY_MONTHLY:
                base_pay = monthly_salary
                pera_for_period = pera_allowance
                other_earnings_for_period = other_earnings_amount
            else:
                base_pay = monthly_salary / Decimal("2")
                pera_for_period = pera_allowance / Decimal("2")
                other_earnings_for_period = other_earnings_amount / Decimal("2")
        else:
            base_pay = Decimal("0.00")
            issues.append("Permanent employee has no basic/monthly salary configured")

    else:
        base_pay = Decimal("0.00")
        issues.append(f"Unknown employment type: {emp_type or 'blank'}")

    base_pay = _money(base_pay)
    pera_for_period = _money(pera_for_period)
    other_earnings_for_period = _money(other_earnings_for_period)

    # -------------------------
    # Attendance deductions
    # -------------------------
    late_deduction = _money(Decimal(late_minutes) * per_minute_rate)
    undertime_deduction = _money(Decimal(undertime_minutes) * per_minute_rate)

    # Do not add absence deduction yet.
    # Reason: client policy for permanent/COS unpaid absences must be confirmed first.
    # JO already follows no-work-no-pay through base pay.
    absence_deduction = Decimal("0.00")

    attendance_deduction = _money(
        late_deduction
        + undertime_deduction
        + absence_deduction
    )

    # -------------------------
    # Premium
    # -------------------------
    premium_pay = Decimal("0.00")

    if emp_type in [UserProfile.EMP_JO, UserProfile.EMP_COS]:
        if profile.has_premium:
            premium_rate = D(rules.premium_rate_percent) / Decimal("100")
            premium_pay = _money(base_pay * premium_rate)

    # Permanent employees normally do not use the JO/COS premium.
    # If the client later allows a permanent premium, we can add a separate rule.
    if emp_type == UserProfile.EMP_PERMANENT and profile.has_premium:
        issues.append("Premium flag ignored for Permanent employee unless client policy allows it")

    # -------------------------
    # Overtime
    # -------------------------
    ot_hours = Decimal("0.00")

    try:
        ot_qs = OvertimeRequest.objects.filter(
            profile=profile,
            approved=True,
            date__gte=period.start_date,
            date__lte=period.end_date,
        )

        for ot in ot_qs:
            matching_dtr_row = None

            for row in dtr.get("rows", []):
                if row.get("date") == ot.date.isoformat():
                    matching_dtr_row = row
                    break

            # Rule: if late that day, OT is disqualified.
            if matching_dtr_row and int(matching_dtr_row.get("late", 0) or 0) > 0:
                issues.append(f"OT disqualified on {ot.date}: employee was late")
                continue

            ot_hours += D(ot.hours)

    except Exception:
        ot_hours = Decimal("0.00")

    ot_multiplier = D(rules.ot_multiplier, "1.25")
    overtime_pay = _money(ot_hours * hourly_rate * ot_multiplier)

    # -------------------------
    # Employee contribution/deduction profile
    # -------------------------
    contrib, _ = EmployeeContribution.objects.get_or_create(
        profile=profile,
        defaults={
            "sss_amount": Decimal("0.00") if emp_type == UserProfile.EMP_PERMANENT else D(rules.sss_minimum, "760.00"),
            "pagibig_amount": Decimal("0.00") if emp_type == UserProfile.EMP_PERMANENT else D(rules.pagibig_minimum, "400.00"),
            "philhealth_mode": getattr(rules, "philhealth_default_mode", EmployeeContribution.PHILHEALTH_PERCENT) or EmployeeContribution.PHILHEALTH_PERCENT,
            "philhealth_value": Decimal("0.00") if emp_type == UserProfile.EMP_PERMANENT else D(rules.philhealth_default_value, "5.00"),
        },
    )

    gross_before_deductions = _money(
        base_pay
        + pera_for_period
        + other_earnings_for_period
        + premium_pay
        + overtime_pay
    )

    # -------------------------
    # PhilHealth
    # -------------------------
    philhealth = Decimal("0.00")

    if contrib.philhealth_mode == EmployeeContribution.PHILHEALTH_FIXED:
        philhealth = D(contrib.philhealth_value)
    else:
        philhealth_rate = D(contrib.philhealth_value) / Decimal("100")
        philhealth = gross_before_deductions * philhealth_rate

    philhealth = _money(philhealth)

    # -------------------------
    # JO/COS deductions
    # -------------------------
    sss = Decimal("0.00")
    pagibig = Decimal("0.00")
    tax_total = Decimal("0.00")

    # -------------------------
    # Permanent deductions
    # -------------------------
    wtax_amount = Decimal("0.00")
    gsis_employee_share = Decimal("0.00")
    gsis_employer_share = Decimal("0.00")
    loan_deduction_amount = Decimal("0.00")
    other_deduction_amount = Decimal("0.00")
    other_employer_contribution = Decimal("0.00")

    if emp_type == UserProfile.EMP_PERMANENT:
        # Permanent employees use GSIS, WTAX, PhilHealth, Pag-IBIG, loans, and other deductions.
        sss = Decimal("0.00")

        pagibig = _money(D(contrib.pagibig_amount))
        wtax_amount = _money(D(getattr(contrib, "wtax_amount", Decimal("0.00"))))
        gsis_employee_share = _money(D(getattr(contrib, "gsis_employee_share", Decimal("0.00"))))
        gsis_employer_share = _money(D(getattr(contrib, "gsis_employer_share", Decimal("0.00"))))
        loan_deduction_amount = _money(D(getattr(contrib, "loan_deduction_amount", Decimal("0.00"))))
        other_deduction_amount = _money(D(getattr(contrib, "other_deduction_amount", Decimal("0.00"))))
        other_employer_contribution = _money(D(getattr(contrib, "other_employer_contribution", Decimal("0.00"))))

        tax_total = wtax_amount

        gov_total = _money(
            philhealth
            + pagibig
            + gsis_employee_share
        )

        employer_contributions_total = _money(
            gsis_employer_share
            + other_employer_contribution
        )

        if monthly_salary <= 0:
            issues.append("Permanent basic salary missing")

        if gov_total <= 0 and tax_total <= 0 and loan_deduction_amount <= 0:
            issues.append("Permanent deductions not configured yet")

    else:
        # JO/COS keep the old logic: SSS, Pag-IBIG, PhilHealth, tax.
        sss = D(contrib.sss_amount)
        pagibig = D(contrib.pagibig_amount)

        # Enforce minimums for JO/COS only.
        if sss < D(rules.sss_minimum):
            sss = D(rules.sss_minimum)

        if pagibig < D(rules.pagibig_minimum):
            pagibig = D(rules.pagibig_minimum)

        sss = _money(sss)
        pagibig = _money(pagibig)

        gov_total = _money(sss + pagibig + philhealth)

        tax_rate = D(rules.tax_rate_percent) / Decimal("100")
        tax_total = _money(gross_before_deductions * tax_rate)

        employer_contributions_total = Decimal("0.00")

    # -------------------------
    # Manual deduction
    # -------------------------
    manual_deduction = _money(profile.manual_deduction_amount or Decimal("0.00"))

    # -------------------------
    # Final computation
    # -------------------------
    deductions_total = _money(
        attendance_deduction
        + gov_total
        + tax_total
        + manual_deduction
        + loan_deduction_amount
        + other_deduction_amount
    )

    net_pay = _money(gross_before_deductions - deductions_total)

    if net_pay < 0:
        net_pay = Decimal("0.00")

    # -------------------------
    # Issue flags
    # -------------------------
    if dtr.get("records_found", 0) == 0:
        issues.append("No attendance records found for biometric ID in selected period")

    if dtr.get("missing_logs", 0) > 0:
        issues.append(f"{dtr.get('missing_logs', 0)} day(s) with missing attendance logs")

    if emp_type in [UserProfile.EMP_JO, UserProfile.EMP_COS]:
        if sss <= 0 or pagibig <= 0:
            issues.append("Contribution missing")

    issues_text = ", ".join(dict.fromkeys([i for i in issues if i]))

    return {
        "picked_employee_id": dtr.get("employee_id_used"),

        "attendance_summary": {
            "present_days": present_days,
            "travel_days": travel_days,
            "holiday_days": holiday_days,
            "absences": absences,
            "missing_logs": int(dtr.get("missing_logs", 0) or 0),
            "records_found": int(dtr.get("records_found", 0) or 0),
            "records_used": int(dtr.get("records_used", 0) or 0),
        },

        "rates": {
            "daily": _money(daily_rate),
            "hourly": _money(hourly_rate),
            "per_minute": _money(per_minute_rate),
        },

        "computed_payroll": {
            "base": _money(base_pay),
            "basic_salary": _money(base_pay),
            "pera": _money(pera_for_period),
            "other_earnings": _money(other_earnings_for_period),

            "premium": _money(premium_pay),
            "overtime_hours": _money(ot_hours),
            "ot": _money(overtime_pay),
            "gross": _money(gross_before_deductions),

            "late_minutes": int(late_minutes),
            "undertime_minutes": int(undertime_minutes),
            "absences": int(absences),

            "late_deduction": _money(late_deduction),
            "undertime_deduction": _money(undertime_deduction),
            "absence_deduction": _money(absence_deduction),
            "attendance_deduction": _money(attendance_deduction),

            "manual_deduction": _money(manual_deduction),
            "loan_deduction": _money(loan_deduction_amount),
            "other_deduction": _money(other_deduction_amount),

            "deductions": _money(deductions_total),
            "net": _money(net_pay),
        },

        "gov": {
            # JO/COS
            "sss": _money(sss),

            # Common
            "pagibig": _money(pagibig),
            "philhealth": _money(philhealth),
            "gov_total": _money(gov_total),
            "tax": _money(tax_total),

            # Permanent-specific
            "wtax": _money(wtax_amount),
            "gsis_employee": _money(gsis_employee_share),
            "gsis_employer": _money(gsis_employer_share),
            "loan_deduction": _money(loan_deduction_amount),
            "other_deduction": _money(other_deduction_amount),
            "other_employer_contribution": _money(other_employer_contribution),
            "employer_contributions_total": _money(employer_contributions_total),
        },

        "issues": issues_text,
        "dtr_rows": dtr.get("rows", []),
    }
    
# =========================================================
# AI Analytics Helpers
# Rule-based analytics: no ML training required
# =========================================================

def _analytics_money(value):
    try:
        return Decimal(value or 0).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")


def _analytics_float(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _analytics_percent(part, total):
    try:
        total = float(total or 0)
        part = float(part or 0)
        if total <= 0:
            return 0.0
        return round((part / total) * 100, 2)
    except Exception:
        return 0.0


def _analytics_clamp(value, minimum=0, maximum=100):
    try:
        value = float(value)
    except Exception:
        value = 0
    return max(minimum, min(maximum, value))


def _analytics_date_range(start_date, end_date):
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _analytics_is_weekend(day):
    return day.weekday() >= 5


def _analytics_parse_date(value, fallback):
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        return fallback


def _analytics_default_range():
    today = timezone.localdate()
    start = today.replace(day=1)
    end = today
    return start, end


def _analytics_get_allowed_branch(request, branch_id=None):
    """
    Superuser:
      - can view all branches when branch_id is empty
      - can view selected branch when branch_id is valid

    Staff admin:
      - can view only their own branch
    """
    admin_branch = _get_admin_branch(request)

    if admin_branch:
        return admin_branch

    if request.user.is_superuser and branch_id:
        return Branch.objects.filter(id=branch_id).first()

    return None


def _analytics_get_branches_for_filter(request):
    if request.user.is_superuser:
        return Branch.objects.all().order_by("name")
    b = _get_admin_branch(request)
    if b:
        return Branch.objects.filter(id=b.id)
    return Branch.objects.none()


def _analytics_get_rules(branch):
    """
    Uses your existing payroll rule helper if available.
    Fallback creates default PayrollRule for the branch.
    """
    if not branch:
        return None

    try:
        return _get_or_create_rules(branch)
    except Exception:
        rules, _ = PayrollRule.objects.get_or_create(branch=branch)
        return rules


def _analytics_make_period(start_date, end_date):
    """
    Fake payroll period object for analytics only.
    This allows _build_dtr_and_summary() to be reused without saving a PayrollPeriod.
    """
    return SimpleNamespace(
        id=None,
        name=f"Analytics Range {start_date} to {end_date}",
        start_date=start_date,
        end_date=end_date,
        pay_mode=PayrollPeriod.PAY_MONTHLY,
    )


def _analytics_leave_days_for_profile(profile, start_date, end_date):
    """
    Count approved leave days inside selected range.
    Uses weekday days only.
    """
    total = 0.0

    leave_qs = LeaveRequest.objects.filter(
        employee=profile.user,
        status=LeaveRequest.STATUS_APPROVED,
        start_date__lte=end_date,
        end_date__gte=start_date,
    )

    for leave in leave_qs:
        s = max(leave.start_date, start_date)
        e = min(leave.end_date, end_date)

        for d in _analytics_date_range(s, e):
            if _analytics_is_weekend(d):
                continue

            if leave.duration in (LeaveRequest.DURATION_HALF_AM, LeaveRequest.DURATION_HALF_PM):
                total += 0.5
            else:
                total += 1

    return total


def _analytics_holiday_dates(branch, start_date, end_date):
    """
    Used for pattern detection like absence after/before holidays.
    """
    qs = HolidaySuspension.objects.filter(
        date__gte=start_date,
        date__lte=end_date,
    )

    if branch:
        qs = qs.filter(
            Q(scope=HolidaySuspension.SCOPE_NATIONWIDE)
            | Q(scope=HolidaySuspension.SCOPE_REGION)
            | Q(scope=HolidaySuspension.SCOPE_BRANCH, branch=branch)
        )

    return set(qs.values_list("date", flat=True))


def _analytics_profile_display_name(profile):
    try:
        return profile.user.get_full_name() or profile.user.username
    except Exception:
        return "Unknown Employee"


def _analytics_empty_employee_summary(profile):
    return {
        "profile": profile,
        "profile_id": profile.id,
        "name": _analytics_profile_display_name(profile),
        "username": profile.user.username,
        "branch": profile.branch.name if profile.branch else "Unassigned",
        "department": profile.department or "Unassigned",
        "position": profile.position or "Not set",
        "employment_type": profile.employment_type or "—",

        "present_days": 0,
        "absences": 0,
        "late_days": 0,
        "late_minutes": 0,
        "undertime_days": 0,
        "undertime_minutes": 0,
        "missing_logs": 0,
        "travel_days": 0,
        "holiday_days": 0,
        "leave_days": 0,

        "working_days": 0,
        "attendance_rate": 0.0,
        "punctuality_rate": 0.0,
        "total_hours": 0.0,
        "long_work_days": 0,

        "risk_score": 0,
        "risk_level": "Low",
        "risk_reasons": [],

        "late_risk_score": 0,
        "late_risk_level": "Low",
        "late_pattern": "No repeated late pattern",

        "overwork_score": 0,
        "overwork_level": "Low",
        "overwork_note": "Normal workload",

        "estimated_gross": 0.0,
        "estimated_deductions": 0.0,
        "estimated_net": 0.0,

        "deduction_drivers": {
            "late": 0.0,
            "undertime": 0.0,
            "absence": 0.0,
        },
    }


def _analytics_compute_employee_summary(profile, start_date, end_date):
    """
    Core rule-based AI analytics per employee.
    Uses your existing DTR builder, so analytics follows payroll attendance rules.
    """
    summary = _analytics_empty_employee_summary(profile)

    branch = profile.branch
    if not branch:
        summary["risk_score"] = 30
        summary["risk_level"] = "Medium"
        summary["risk_reasons"] = ["Employee has no assigned branch."]
        return summary

    rules = _analytics_get_rules(branch)
    if not rules:
        summary["risk_score"] = 30
        summary["risk_level"] = "Medium"
        summary["risk_reasons"] = ["No payroll rules found for branch."]
        return summary

    period = _analytics_make_period(start_date, end_date)

    try:
        dtr = _build_dtr_and_summary(profile, branch, period, rules)
    except Exception as e:
        summary["risk_score"] = 50
        summary["risk_level"] = "Medium"
        summary["risk_reasons"] = [f"DTR analytics failed: {e}"]
        return summary

    rows = dtr.get("rows", [])

    working_days = 0
    present_days = 0
    absences = 0
    late_days = 0
    late_minutes = 0
    undertime_days = 0
    undertime_minutes = 0
    missing_logs = 0
    travel_days = int(dtr.get("travel_days", 0) or 0)
    holiday_days = int(dtr.get("holiday_days", 0) or 0)
    total_hours = Decimal("0.00")
    long_work_days = 0

    monday_friday_absences = 0
    weekday_late_counter = defaultdict(int)

    holiday_dates = _analytics_holiday_dates(branch, start_date - timedelta(days=3), end_date + timedelta(days=3))
    absence_near_holiday = 0

    for row in rows:
        status = str(row.get("status") or "")
        row_date_raw = row.get("date")

        try:
            row_date = datetime.strptime(row_date_raw, "%Y-%m-%d").date()
        except Exception:
            continue

        if status in ["Weekend", "Holiday/Suspension"]:
            continue

        working_days += 1

        row_late = int(row.get("late", 0) or 0)
        row_undertime = int(row.get("undertime", 0) or 0)

        try:
            row_hours = Decimal(str(row.get("total_hours", "0") or "0"))
        except Exception:
            row_hours = Decimal("0.00")

        if status in ["Present", "Official Travel", "Incomplete"]:
            present_days += 1

        if status == "Absent":
            absences += 1

            if row_date.weekday() in [0, 4]:
                monday_friday_absences += 1

            if (row_date - timedelta(days=1)) in holiday_dates or (row_date + timedelta(days=1)) in holiday_dates:
                absence_near_holiday += 1

        if status == "Incomplete":
            missing_logs += 1

        if row_late > 0:
            late_days += 1
            late_minutes += row_late
            weekday_late_counter[row_date.strftime("%A")] += 1

        if row_undertime > 0:
            undertime_days += 1
            undertime_minutes += row_undertime

        total_hours += row_hours

        if row_hours >= Decimal("9.50"):
            long_work_days += 1

    leave_days = _analytics_leave_days_for_profile(profile, start_date, end_date)

    attendance_rate = _analytics_percent(present_days, working_days)
    punctuality_rate = _analytics_percent(max(0, present_days - late_days), max(1, present_days))

    # -------------------------
    # Absenteeism risk score
    # -------------------------
    risk_score = 0
    risk_reasons = []

    if absences > 0:
        add = min(45, absences * 15)
        risk_score += add
        risk_reasons.append(f"{absences} absence(s) in the selected range.")

    if late_days >= 2:
        add = min(25, late_days * 5)
        risk_score += add
        risk_reasons.append(f"{late_days} late day(s) detected.")

    if missing_logs > 0:
        add = min(20, missing_logs * 8)
        risk_score += add
        risk_reasons.append(f"{missing_logs} incomplete DTR day(s).")

    if monday_friday_absences > 0:
        risk_score += min(15, monday_friday_absences * 5)
        risk_reasons.append(f"{monday_friday_absences} absence(s) happened on Monday/Friday.")

    if absence_near_holiday > 0:
        risk_score += min(12, absence_near_holiday * 4)
        risk_reasons.append(f"{absence_near_holiday} absence(s) near a holiday/suspension.")

    if leave_days >= 3:
        risk_score += min(10, int(leave_days) * 2)
        risk_reasons.append(f"{leave_days:g} approved leave day(s) in the selected range.")

    risk_score = int(_analytics_clamp(risk_score))

    if risk_score >= 70:
        risk_level = "High"
    elif risk_score >= 40:
        risk_level = "Medium"
    else:
        risk_level = "Low"

    if not risk_reasons:
        risk_reasons = ["Stable attendance behavior in the selected range."]

    # -------------------------
    # Late arrival risk
    # -------------------------
    late_risk_score = int(_analytics_clamp((late_days * 12) + (late_minutes / 10)))

    if late_risk_score >= 70:
        late_risk_level = "High"
    elif late_risk_score >= 35:
        late_risk_level = "Medium"
    else:
        late_risk_level = "Low"

    if weekday_late_counter:
        top_day, top_count = sorted(weekday_late_counter.items(), key=lambda x: x[1], reverse=True)[0]
        late_pattern = f"Most late arrivals happened on {top_day} ({top_count} time/s)."
    else:
        late_pattern = "No repeated late pattern."

    # -------------------------
    # Workload / overwork risk
    # -------------------------
    overwork_score = int(_analytics_clamp((long_work_days * 12) + (float(total_hours) / max(1, working_days) - 8) * 10))

    if overwork_score >= 70:
        overwork_level = "High"
        overwork_note = "Frequent long work hours. Review workload and overtime authority."
    elif overwork_score >= 35:
        overwork_level = "Medium"
        overwork_note = "Some long work days detected. Monitor workload."
    else:
        overwork_level = "Low"
        overwork_note = "Normal workload pattern."

    # -------------------------
    # Payroll forecast / cost projection
    # -------------------------
    monthly_salary = Decimal(profile.monthly_salary or 0)
    daily_rate_profile = Decimal(profile.daily_rate or 0)
    salary_divisor = Decimal(rules.salary_divisor or 22)
    daily_hours_required = Decimal(rules.daily_hours_required or 8)

    if salary_divisor <= 0:
        salary_divisor = Decimal("22")

    if daily_hours_required <= 0:
        daily_hours_required = Decimal("8")

    if monthly_salary > 0:
        daily_rate = monthly_salary / salary_divisor
    else:
        daily_rate = daily_rate_profile

    hourly_rate = daily_rate / daily_hours_required if daily_hours_required > 0 else Decimal("0.00")
    per_minute_rate = hourly_rate / Decimal("60") if hourly_rate > 0 else Decimal("0.00")

    emp_type = str(profile.employment_type or "").upper()

    if emp_type == UserProfile.EMP_JO:
        estimated_gross = daily_rate * Decimal(present_days)
        absence_deduction = daily_rate * Decimal(absences)
    else:
        # Analytics projection: daily prorated estimate for selected date range.
        # This is for forecasting display only, not final payroll processing.
        estimated_gross = daily_rate * Decimal(max(0, working_days))
        absence_deduction = daily_rate * Decimal(absences)

    late_deduction = Decimal(late_minutes) * per_minute_rate
    undertime_deduction = Decimal(undertime_minutes) * per_minute_rate

    estimated_deductions = late_deduction + undertime_deduction + absence_deduction
    estimated_net = estimated_gross - estimated_deductions

    if estimated_net < 0:
        estimated_net = Decimal("0.00")

    summary.update({
        "present_days": int(present_days),
        "absences": int(absences),
        "late_days": int(late_days),
        "late_minutes": int(late_minutes),
        "undertime_days": int(undertime_days),
        "undertime_minutes": int(undertime_minutes),
        "missing_logs": int(missing_logs),
        "travel_days": int(travel_days),
        "holiday_days": int(holiday_days),
        "leave_days": float(leave_days),

        "working_days": int(working_days),
        "attendance_rate": round(attendance_rate, 2),
        "punctuality_rate": round(punctuality_rate, 2),
        "total_hours": round(float(total_hours), 2),
        "long_work_days": int(long_work_days),

        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,

        "late_risk_score": late_risk_score,
        "late_risk_level": late_risk_level,
        "late_pattern": late_pattern,

        "overwork_score": overwork_score,
        "overwork_level": overwork_level,
        "overwork_note": overwork_note,

        "estimated_gross": float(_analytics_money(estimated_gross)),
        "estimated_deductions": float(_analytics_money(estimated_deductions)),
        "estimated_net": float(_analytics_money(estimated_net)),

        "deduction_drivers": {
            "late": float(_analytics_money(late_deduction)),
            "undertime": float(_analytics_money(undertime_deduction)),
            "absence": float(_analytics_money(absence_deduction)),
        },
    })

    return summary


def _analytics_queryset_for_request(request, selected_branch, emp_type, department):
    profiles = UserProfile.objects.select_related("user", "branch").filter(
        is_approved=True,
        user__is_staff=False,
        user__is_superuser=False,
    )

    if selected_branch:
        profiles = profiles.filter(branch=selected_branch)
    elif not request.user.is_superuser:
        admin_branch = _get_admin_branch(request)
        profiles = profiles.filter(branch=admin_branch)

    if emp_type in ["JO", "COS"]:
        profiles = profiles.filter(employment_type=emp_type)

    if department:
        profiles = profiles.filter(department__iexact=department)

    return profiles.order_by("branch__name", "department", "user__username")


def _analytics_daily_timeline(employee_summaries, start_date, end_date):
    """
    Builds daily present/absent/late/undertime chart series.
    Reuses DTR builder again per employee for exact daily status.
    """
    labels = []
    present_count = []
    absent_count = []
    late_count = []
    undertime_count = []
    attendance_rate = []
    productivity_rate = []

    day_map = {}
    for d in _analytics_date_range(start_date, end_date):
        if _analytics_is_weekend(d):
            continue

        key = d.isoformat()
        labels.append(d.strftime("%b %d"))
        day_map[key] = {
            "present": 0,
            "absent": 0,
            "late": 0,
            "undertime": 0,
            "total": 0,
        }

    for emp in employee_summaries:
        profile = emp["profile"]
        branch = profile.branch
        if not branch:
            continue

        rules = _analytics_get_rules(branch)
        if not rules:
            continue

        period = _analytics_make_period(start_date, end_date)

        try:
            dtr = _build_dtr_and_summary(profile, branch, period, rules)
        except Exception:
            continue

        for row in dtr.get("rows", []):
            key = row.get("date")
            if key not in day_map:
                continue

            status = str(row.get("status") or "")
            day_map[key]["total"] += 1

            if status in ["Present", "Official Travel", "Incomplete"]:
                day_map[key]["present"] += 1

            if status == "Absent":
                day_map[key]["absent"] += 1

            if int(row.get("late", 0) or 0) > 0:
                day_map[key]["late"] += 1

            if int(row.get("undertime", 0) or 0) > 0:
                day_map[key]["undertime"] += 1

    for key in day_map:
        item = day_map[key]
        total = item["total"]

        present_count.append(item["present"])
        absent_count.append(item["absent"])
        late_count.append(item["late"])
        undertime_count.append(item["undertime"])

        att = _analytics_percent(item["present"], total)
        attendance_rate.append(att)

        # Productivity proxy:
        # attendance minus penalty for late and undertime.
        penalty = (item["late"] * 3) + (item["undertime"] * 4)
        productivity_rate.append(round(_analytics_clamp(att - penalty), 2))

    return {
        "timeline_labels": labels,
        "present_count": present_count,
        "absent_count": absent_count,
        "late_count": late_count,
        "undertime_count": undertime_count,
        "attendance_rate": attendance_rate,
        "productivity_rate": productivity_rate,
    }


def _analytics_compare_period(request, selected_branch, emp_type, department, start_date, end_date):
    """
    Compare current selected range vs previous same-length range.
    """
    days_len = (end_date - start_date).days + 1
    prev_end = start_date - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days_len - 1)

    current_profiles = _analytics_queryset_for_request(request, selected_branch, emp_type, department)
    previous_profiles = current_profiles

    current_summaries = [
        _analytics_compute_employee_summary(profile, start_date, end_date)
        for profile in current_profiles
    ]

    previous_summaries = [
        _analytics_compute_employee_summary(profile, prev_start, prev_end)
        for profile in previous_profiles
    ]

    def aggregate(items):
        working = sum(i["working_days"] for i in items)
        present = sum(i["present_days"] for i in items)
        late = sum(i["late_days"] for i in items)
        absent = sum(i["absences"] for i in items)
        return {
            "attendance_rate": _analytics_percent(present, working),
            "late_days": late,
            "absences": absent,
        }

    current = aggregate(current_summaries)
    previous = aggregate(previous_summaries)

    def diff_percent(a, b):
        if b <= 0:
            return 0.0
        return round(((a - b) / b) * 100, 2)

    return {
        "previous_start": prev_start.isoformat(),
        "previous_end": prev_end.isoformat(),
        "current": current,
        "previous": previous,
        "attendance_change": diff_percent(current["attendance_rate"], previous["attendance_rate"]),
        "late_change": diff_percent(current["late_days"], previous["late_days"]),
        "absence_change": diff_percent(current["absences"], previous["absences"]),
    }


def _analytics_build_payload(request):
    today = timezone.localdate()
    default_start, default_end = _analytics_default_range()

    start_date = _analytics_parse_date(request.GET.get("start"), default_start)
    end_date = _analytics_parse_date(request.GET.get("end"), default_end)

    if end_date < start_date:
        end_date = start_date

    branch_id = (request.GET.get("branch") or "").strip()
    emp_type = (request.GET.get("emp_type") or "ALL").strip().upper()
    department = (request.GET.get("department") or "").strip()

    if emp_type not in ["ALL", "JO", "COS"]:
        emp_type = "ALL"

    selected_branch = _analytics_get_allowed_branch(request, branch_id)

    profiles_qs = _analytics_queryset_for_request(
        request=request,
        selected_branch=selected_branch,
        emp_type=emp_type,
        department=department,
    )

    employee_summaries = [
        _analytics_compute_employee_summary(profile, start_date, end_date)
        for profile in profiles_qs
    ]

    total_employees = len(employee_summaries)
    total_working_days = sum(x["working_days"] for x in employee_summaries)
    total_present = sum(x["present_days"] for x in employee_summaries)
    total_absences = sum(x["absences"] for x in employee_summaries)
    total_late_days = sum(x["late_days"] for x in employee_summaries)
    total_late_minutes = sum(x["late_minutes"] for x in employee_summaries)
    total_undertime_minutes = sum(x["undertime_minutes"] for x in employee_summaries)
    total_missing_logs = sum(x["missing_logs"] for x in employee_summaries)
    total_overtime_proxy = sum(x["long_work_days"] for x in employee_summaries)
    total_hours = sum(x["total_hours"] for x in employee_summaries)

    estimated_gross = sum(Decimal(str(x["estimated_gross"])) for x in employee_summaries)
    estimated_deductions = sum(Decimal(str(x["estimated_deductions"])) for x in employee_summaries)
    estimated_net = sum(Decimal(str(x["estimated_net"])) for x in employee_summaries)

    attendance_score = _analytics_percent(total_present, total_working_days)
    punctuality_score = _analytics_percent(max(0, total_present - total_late_days), max(1, total_present))
    leave_consistency = _analytics_percent(max(0, total_working_days - total_absences), max(1, total_working_days))

    stability_value = round(
        (attendance_score * 0.45) +
        (punctuality_score * 0.35) +
        (leave_consistency * 0.20),
        2
    )

    avg_risk = 0.0
    if total_employees > 0:
        avg_risk = round(sum(x["risk_score"] for x in employee_summaries) / total_employees, 2)

    high_risk_count = sum(1 for x in employee_summaries if x["risk_level"] == "High")
    medium_risk_count = sum(1 for x in employee_summaries if x["risk_level"] == "Medium")
    low_risk_count = sum(1 for x in employee_summaries if x["risk_level"] == "Low")

    turnover_forecast = round(_analytics_clamp((high_risk_count * 12) + (medium_risk_count * 4)), 2)

    # AI confidence is rule/data completeness score.
    data_quality_penalty = 0
    if total_employees <= 0:
        data_quality_penalty += 30
    if total_missing_logs > 0:
        data_quality_penalty += min(25, total_missing_logs * 2)
    if total_working_days <= 0:
        data_quality_penalty += 20

    ai_confidence = round(_analytics_clamp(95 - data_quality_penalty), 2)

    timeline = _analytics_daily_timeline(employee_summaries, start_date, end_date)
    comparison = _analytics_compare_period(request, selected_branch, emp_type, department, start_date, end_date)

    # Department aggregation
    dept_map = defaultdict(lambda: {
        "employees": 0,
        "present": 0,
        "working": 0,
        "late": 0,
        "risk": 0,
        "net": Decimal("0.00"),
        "long_days": 0,
    })

    for emp in employee_summaries:
        dept = emp["department"] or "Unassigned"
        dept_map[dept]["employees"] += 1
        dept_map[dept]["present"] += emp["present_days"]
        dept_map[dept]["working"] += emp["working_days"]
        dept_map[dept]["late"] += emp["late_days"]
        dept_map[dept]["risk"] += emp["risk_score"]
        dept_map[dept]["net"] += Decimal(str(emp["estimated_net"]))
        dept_map[dept]["long_days"] += emp["long_work_days"]

    dept_labels = list(dept_map.keys())
    dept_attendance = [
        _analytics_percent(dept_map[d]["present"], dept_map[d]["working"])
        for d in dept_labels
    ]
    dept_late = [dept_map[d]["late"] for d in dept_labels]
    dept_risk = [
        round(dept_map[d]["risk"] / max(1, dept_map[d]["employees"]), 2)
        for d in dept_labels
    ]
    dept_overtime = [dept_map[d]["long_days"] for d in dept_labels]

    # Leave weekday chart
    leave_weekday = [0, 0, 0, 0, 0]
    leave_qs = LeaveRequest.objects.filter(
        status=LeaveRequest.STATUS_APPROVED,
        start_date__lte=end_date,
        end_date__gte=start_date,
    )

    if selected_branch:
        leave_qs = leave_qs.filter(branch=selected_branch)
    elif not request.user.is_superuser:
        admin_branch = _get_admin_branch(request)
        leave_qs = leave_qs.filter(branch=admin_branch)

    for leave in leave_qs:
        s = max(leave.start_date, start_date)
        e = min(leave.end_date, end_date)

        for d in _analytics_date_range(s, e):
            if d.weekday() < 5:
                leave_weekday[d.weekday()] += 1

    # Payroll forecast series: cumulative net estimate through timeline days.
    payroll_labels = timeline["timeline_labels"]
    payroll_series = []

    running_estimate = Decimal("0.00")
    if payroll_labels:
        daily_estimate = estimated_net / Decimal(len(payroll_labels)) if len(payroll_labels) > 0 else Decimal("0.00")
        for _ in payroll_labels:
            running_estimate += daily_estimate
            payroll_series.append(float(_analytics_money(running_estimate)))

    salary_att_points = []
    for emp in employee_summaries:
        salary_est = emp["estimated_gross"]
        salary_att_points.append({
            "x": emp["attendance_rate"],
            "y": salary_est,
            "name": emp["name"],
        })

    # Deduction drivers
    total_late_deduction = sum(Decimal(str(x["deduction_drivers"]["late"])) for x in employee_summaries)
    total_undertime_deduction = sum(Decimal(str(x["deduction_drivers"]["undertime"])) for x in employee_summaries)
    total_absence_deduction = sum(Decimal(str(x["deduction_drivers"]["absence"])) for x in employee_summaries)

    deduction_labels = ["Late", "Undertime", "Absence"]
    deduction_values = [
        float(_analytics_money(total_late_deduction)),
        float(_analytics_money(total_undertime_deduction)),
        float(_analytics_money(total_absence_deduction)),
    ]

    # Actionable insights
    insights = []

    if comparison["late_change"] > 0:
        insights.append({
            "level": "warning",
            "title": "Rising late arrival trend",
            "text": f"Late arrivals increased by {comparison['late_change']}% compared to the previous period.",
        })
    elif comparison["late_change"] < 0:
        insights.append({
            "level": "success",
            "title": "Improving punctuality",
            "text": f"Late arrivals decreased by {abs(comparison['late_change'])}% compared to the previous period.",
        })

    if comparison["absence_change"] > 0:
        insights.append({
            "level": "danger",
            "title": "Absence pattern increased",
            "text": f"Absences increased by {comparison['absence_change']}% compared to the previous period.",
        })

    if high_risk_count > 0:
        top_high = sorted(employee_summaries, key=lambda x: x["risk_score"], reverse=True)[0]
        insights.append({
            "level": "danger",
            "title": "High absenteeism risk detected",
            "text": f"{top_high['name']} has high risk due to: {', '.join(top_high['risk_reasons'][:2])}",
        })

    if total_missing_logs > 0:
        insights.append({
            "level": "warning",
            "title": "Incomplete DTR logs found",
            "text": f"{total_missing_logs} incomplete attendance record(s) may affect payroll accuracy.",
        })

    if estimated_deductions > 0:
        biggest_driver = max(
            [
                ("late deductions", total_late_deduction),
                ("undertime deductions", total_undertime_deduction),
                ("absence deductions", total_absence_deduction),
            ],
            key=lambda x: x[1]
        )

        insights.append({
            "level": "info",
            "title": "Top payroll deduction driver",
            "text": f"The largest deduction driver is {biggest_driver[0]} with an estimated ₱{_analytics_money(biggest_driver[1])}.",
        })

    if not insights:
        insights.append({
            "level": "success",
            "title": "Stable workforce pattern",
            "text": "No critical attendance or payroll risks were detected in the selected range.",
        })

    # Table rows
    risk_rows = sorted(employee_summaries, key=lambda x: x["risk_score"], reverse=True)
    late_rows = sorted(employee_summaries, key=lambda x: x["late_risk_score"], reverse=True)
    overwork_rows = sorted(employee_summaries, key=lambda x: x["overwork_score"], reverse=True)

    payload = {
        "generated_at": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %I:%M %p"),

        "filters": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "branch": selected_branch.id if selected_branch else "",
            "branch_name": selected_branch.name if selected_branch else "All Branches",
            "emp_type": emp_type,
            "department": department,
        },

        "summary": {
            "total_employees": total_employees,
            "working_days_total": total_working_days,
            "present_days_total": total_present,
            "absences_total": total_absences,
            "late_days_total": total_late_days,
            "late_minutes_total": total_late_minutes,
            "undertime_minutes_total": total_undertime_minutes,
            "missing_logs_total": total_missing_logs,
            "total_hours": round(total_hours, 2),
            "estimated_gross": float(_analytics_money(estimated_gross)),
            "estimated_deductions": float(_analytics_money(estimated_deductions)),
            "estimated_net": float(_analytics_money(estimated_net)),
        },

        "stability_value": stability_value,
        "stability_parts_attendance": attendance_score,
        "stability_parts_schedule": punctuality_score,
        "stability_parts_leave": leave_consistency,

        "absenteeism_risk": avg_risk,
        "turnover_forecast": turnover_forecast,
        "ai_confidence": ai_confidence,

        "overtime_hours": total_overtime_proxy,
        "attendance": attendance_score,
        "punctuality": punctuality_score,
        "consistency": leave_consistency,

        "risk_buckets": {
            "low": low_risk_count,
            "medium": medium_risk_count,
            "high": high_risk_count,
        },

        "turnover_buckets": [low_risk_count, medium_risk_count, high_risk_count],

        "dept_labels": dept_labels,
        "dept_attendance": dept_attendance,
        "dept_late": dept_late,
        "dept_risk": dept_risk,
        "dept_overtime": dept_overtime,

        "payroll_labels": payroll_labels,
        "payroll_series": payroll_series,

        "salary_att_points": salary_att_points,
        "leave_weekday": leave_weekday,

        "deduction_labels": deduction_labels,
        "deduction_values": deduction_values,

        "comparison": comparison,

        "insights": insights,

        "risk_rows": [
            {
                "profile_id": r["profile_id"],
                "name": r["name"],
                "branch": r["branch"],
                "department": r["department"],
                "employment_type": r["employment_type"],
                "risk_score": r["risk_score"],
                "risk_level": r["risk_level"],
                "reasons": r["risk_reasons"],
                "absences": r["absences"],
                "late_days": r["late_days"],
                "missing_logs": r["missing_logs"],
                "attendance_rate": r["attendance_rate"],
            }
            for r in risk_rows[:50]
        ],

        "late_rows": [
            {
                "name": r["name"],
                "department": r["department"],
                "late_days": r["late_days"],
                "late_minutes": r["late_minutes"],
                "late_risk_score": r["late_risk_score"],
                "late_risk_level": r["late_risk_level"],
                "late_pattern": r["late_pattern"],
            }
            for r in late_rows[:50]
        ],

        "overwork_rows": [
            {
                "name": r["name"],
                "department": r["department"],
                "total_hours": r["total_hours"],
                "long_work_days": r["long_work_days"],
                "overwork_score": r["overwork_score"],
                "overwork_level": r["overwork_level"],
                "overwork_note": r["overwork_note"],
            }
            for r in overwork_rows[:50]
        ],

        **timeline,
    }

    return payload

# ============================================
# 🔥 PAYROLL ENGINE (FINAL - GOVERNMENT LOGIC)
# ============================================

def _get_or_create_rules(branch):
    rule, _ = PayrollRule.objects.get_or_create(branch=branch)
    return rule


def _get_employee_attendance(profile, period):
    if not profile.biometric_employee_id:
        return AttendanceRecord.objects.none()

    qs = AttendanceRecord.objects.filter(
        employee_id=str(profile.biometric_employee_id),
        timestamp__date__gte=period.start_date,
        timestamp__date__lte=period.end_date,
    )

    # 🔥 IMPORTANT FIX: ignore branch mismatch issue
    return qs.order_by("timestamp")


def _group_attendance_by_day(records):
    grouped = defaultdict(list)
    for r in records:
        grouped[r.timestamp.date()].append(r)
    return grouped


def _compute_daily_hours_and_late(day_records, rules):
    if not day_records:
        return 0, 0, 0

    day_records = sorted(day_records, key=lambda x: x.timestamp)

    check_in = None
    check_out = None

    for r in day_records:
        if r.attendance_status == AttendanceRecord.STATUS_CHECKIN and not check_in:
            check_in = r.timestamp

        if r.attendance_status == AttendanceRecord.STATUS_CHECKOUT:
            check_out = r.timestamp

    if not check_in or not check_out:
        return 0, 0, 0

    total_hours = (check_out - check_in).total_seconds() / 3600

    # 🔥 LATE LOGIC (with grace + flag ceremony)
    late_minutes = 0
    undertime_minutes = 0

    scheduled_start = datetime.combine(check_in.date(), rules.work_start_time)

    # Monday flag ceremony strict
    if check_in.weekday() == 0:
        if check_in.time() > rules.flag_ceremony_cutoff_time:
            late_minutes = (check_in - scheduled_start).total_seconds() / 60
    else:
        if check_in > scheduled_start:
            diff = (check_in - scheduled_start).total_seconds() / 60
            if diff > rules.grace_minutes_normal:
                late_minutes = diff

    # undertime
    required_hours = float(rules.daily_hours_required)
    if total_hours < required_hours:
        undertime_minutes = (required_hours - total_hours) * 60

    return total_hours, late_minutes, undertime_minutes

def _compute_employee_payroll(profile, period, rules):
    records = _get_employee_attendance(profile, period)
    grouped = _group_attendance_by_day(records)

    days_present = 0
    total_hours = 0
    total_late = 0
    total_undertime = 0

    for day, recs in grouped.items():
        hours, late, undertime = _compute_daily_hours_and_late(recs, rules)

        if hours > 0:
            days_present += 1

        total_hours += hours
        total_late += late
        total_undertime += undertime

    # =========================
    # 💰 RATE COMPUTATION
    # =========================
    if profile.monthly_salary:
        daily_rate = profile.monthly_salary / rules.salary_divisor
    else:
        daily_rate = profile.daily_rate or 0

    hourly_rate = daily_rate / 8
    per_minute_rate = hourly_rate / 60

    late_deduction = Decimal(total_late) * Decimal(per_minute_rate)
    undertime_deduction = Decimal(total_undertime) * Decimal(per_minute_rate)

    base_pay = Decimal(days_present) * Decimal(daily_rate)

    # =========================
    # 💸 CONTRIBUTIONS
    # =========================
    sss = rules.sss_minimum
    pagibig = rules.pagibig_minimum

    philhealth = base_pay * (rules.philhealth_default_value / 100)

    tax = base_pay * (rules.tax_rate_percent / 100)

    gov_total = sss + pagibig + philhealth

    # =========================
    # 🧾 NET PAY
    # =========================
    if base_pay <= 0:
        gov_total = Decimal("0.00")
        tax = Decimal("0.00")
        net_pay = Decimal("0.00")
    else:
        net_pay = base_pay - late_deduction - undertime_deduction - gov_total - tax

    if net_pay < 0:
        net_pay = Decimal("0.00")

    return {
        "days_present": days_present,
        "late_minutes": int(total_late),
        "undertime_minutes": int(total_undertime),
        "base_pay": base_pay,
        "late_deduction": late_deduction,
        "undertime_deduction": undertime_deduction,
        "gov_contributions": gov_total,
        "tax": tax,
        "net_pay": net_pay,
    }
#==============================
# =========================================================
# PAYROLL BATCH FINALIZATION HELPERS
# Place immediately above:
# @login_required
# def admin_payroll(request):
# =========================================================

def _payroll_batch_redirect_url(batch):
    """
    Return the admin payroll page for this batch's exact
    branch, period, and employee scope.
    """
    return (
        f"{reverse('admin_payroll')}"
        f"?branch={batch.branch_id}"
        f"&period={batch.period_id}"
        f"&type={batch.employee_type_scope}"
    )


def _admin_can_access_payroll_batch(request, batch):
    """
    Superuser:
        Can access every payroll batch.

    Staff branch admin:
        Can access only batches belonging to their assigned branch.
    """
    if request.user.is_superuser:
        return True

    try:
        admin_branch = request.user.profile.branch
    except UserProfile.DoesNotExist:
        return False

    return bool(
        admin_branch
        and batch.branch_id == admin_branch.id
    )

# =========================================================
# FINAL PAYROLL VALIDATION CHECKLIST
# Place immediately above:
# def _get_payroll_batch_finalization_errors(batch):
# =========================================================

def _build_payroll_batch_validation(batch):
    """
    Validate one saved payroll batch before official finalization.

    Returns:
        {
            "is_ready": bool,
            "errors": list[str],
            "warnings": list[str],
            "checks": list[dict],
        }
    """

    errors = []
    warnings = []
    checks = []

    def add_check(label, passed, message=""):
        checks.append({
            "label": label,
            "passed": bool(passed),
            "message": message,
        })

    items = list(
        PayrollItem.objects
        .select_related(
            "profile",
            "profile__user",
        )
        .filter(batch=batch)
    )

       # -------------------------------------------------
    # 1. Batch status
    # -------------------------------------------------
    batch_completed = (
        batch.status == PayrollBatch.STATUS_COMPLETED
        or batch.status == PayrollBatch.STATUS_FINALIZED
    )

    add_check(
        "Payroll batch is processed",
        batch_completed,
        (
            "Batch status is completed/finalized."
            if batch_completed
            else "Process payroll before finalization."
        ),
    )

    if not batch_completed:
        errors.append(
            "Payroll must be processed and completed before finalization."
        )

    # -------------------------------------------------
    # 2. Payroll items exist
    # -------------------------------------------------
    has_items = len(items) > 0

    add_check(
        "Payroll batch contains employees",
        has_items,
        (
            f"{len(items)} payroll item(s) found."
            if has_items
            else "No payroll items were found."
        ),
    )

    if not has_items:
        errors.append(
            "Payroll batch contains no payroll items."
        )

        return {
            "is_ready": False,
            "errors": errors,
            "warnings": warnings,
            "checks": checks,
        }

    profile_ids = [
        item.profile_id
        for item in items
    ]

    # -------------------------------------------------
    # 3. Every DTR is locked
    # -------------------------------------------------
    locked_profile_ids = set(
        FinalizedDTR.objects
        .filter(
            profile_id__in=profile_ids,
            period=batch.period,
            is_locked=True,
        )
        .values_list(
            "profile_id",
            flat=True,
        )
    )

    employees_without_locked_dtr = []

    for item in items:
        if item.profile_id not in locked_profile_ids:
            employee_name = (
                item.profile.user.get_full_name()
                or item.profile.user.username
            )

            employees_without_locked_dtr.append(
                employee_name
            )

    all_dtrs_locked = (
        len(employees_without_locked_dtr) == 0
    )

    add_check(
        "All employee DTRs are locked",
        all_dtrs_locked,
        (
            "Every included employee has a locked DTR."
            if all_dtrs_locked
            else (
                "Not locked: "
                + ", ".join(
                    employees_without_locked_dtr[:5]
                )
            )
        ),
    )

    if not all_dtrs_locked:
        errors.append(
            "Every employee must have a locked DTR. "
            "Not locked: "
            + ", ".join(
                employees_without_locked_dtr[:5]
            )
            + "."
        )

    # -------------------------------------------------
    # 4. Valid employment types
    # -------------------------------------------------
    allowed_types = {
        UserProfile.EMP_JO,
        UserProfile.EMP_COS,
        UserProfile.EMP_PERMANENT,
    }

    invalid_type_names = []

    for item in items:
        if item.profile.employment_type not in allowed_types:
            invalid_type_names.append(
                item.profile.user.get_full_name()
                or item.profile.user.username
            )

    valid_types = len(invalid_type_names) == 0

    add_check(
        "All employee types are valid",
        valid_types,
        (
            "All employees are JO, COS, or Permanent."
            if valid_types
            else (
                "Invalid type: "
                + ", ".join(
                    invalid_type_names[:5]
                )
            )
        ),
    )

    if not valid_types:
        errors.append(
            "Invalid employment type found for: "
            + ", ".join(
                invalid_type_names[:5]
            )
            + "."
        )

    # -------------------------------------------------
    # 5. Salary configuration
    # -------------------------------------------------
    missing_salary_names = []

    for item in items:
        profile = item.profile

        has_salary = (
            Decimal(
                str(
                    profile.monthly_salary
                    or "0.00"
                )
            ) > 0
            or Decimal(
                str(
                    profile.daily_rate
                    or "0.00"
                )
            ) > 0
        )

        if not has_salary:
            missing_salary_names.append(
                profile.user.get_full_name()
                or profile.user.username
            )

    salaries_complete = (
        len(missing_salary_names) == 0
    )

    add_check(
        "All employees have salary rates",
        salaries_complete,
        (
            "Salary configuration is complete."
            if salaries_complete
            else (
                "Missing salary: "
                + ", ".join(
                    missing_salary_names[:5]
                )
            )
        ),
    )

    if not salaries_complete:
        errors.append(
            "Salary is not configured for: "
            + ", ".join(
                missing_salary_names[:5]
            )
            + "."
        )

    # -------------------------------------------------
    # 6. Critical saved payroll issues
    # -------------------------------------------------
    critical_issue_phrases = (
        "no daily/monthly salary configured",
        "permanent employee has no basic/monthly salary configured",
        "permanent basic salary missing",
        "unknown employment type",
    )

    critical_issue_names = []

    for item in items:
        issue_text = str(
            item.issues or ""
        ).lower()

        if any(
            phrase in issue_text
            for phrase in critical_issue_phrases
        ):
            critical_issue_names.append(
                item.profile.user.get_full_name()
                or item.profile.user.username
            )

    no_critical_issues = (
        len(critical_issue_names) == 0
    )

    add_check(
        "No critical payroll configuration issues",
        no_critical_issues,
        (
            "No critical configuration issue was found."
            if no_critical_issues
            else (
                "Critical issue: "
                + ", ".join(
                    critical_issue_names[:5]
                )
            )
        ),
    )

    if not no_critical_issues:
        errors.append(
            "Critical payroll issues exist for: "
            + ", ".join(
                critical_issue_names[:5]
            )
            + "."
        )

    # -------------------------------------------------
    # 7. Missing attendance logs
    # -------------------------------------------------
    employees_with_missing_logs = []

    for item in items:
        meta = (
            item.meta
            if isinstance(item.meta, dict)
            else {}
        )

        attendance_summary = meta.get(
            "attendance_summary",
            {},
        )

        if not isinstance(
            attendance_summary,
            dict,
        ):
            attendance_summary = {}

        missing_logs = int(
            attendance_summary.get(
                "missing_logs",
                0,
            )
            or 0
        )

        if missing_logs > 0:
            employees_with_missing_logs.append(
                {
                    "name": (
                        item.profile.user.get_full_name()
                        or item.profile.user.username
                    ),
                    "count": missing_logs,
                }
            )

    no_missing_logs = (
        len(employees_with_missing_logs) == 0
    )

    add_check(
        "No missing attendance logs",
        no_missing_logs,
        (
            "No missing attendance logs were found."
            if no_missing_logs
            else (
                "Review missing logs for: "
                + ", ".join(
                    entry["name"]
                    for entry
                    in employees_with_missing_logs[:5]
                )
            )
        ),
    )

    if not no_missing_logs:
        warnings.append(
            "Some employees have missing attendance logs. "
            "Review them before approving payroll: "
            + ", ".join(
                entry["name"]
                for entry
                in employees_with_missing_logs[:5]
            )
            + "."
        )

    # -------------------------------------------------
    # 8. Deductions must not exceed gross pay
    # -------------------------------------------------
    deduction_over_gross_names = []

    for item in items:
        meta = (
            item.meta
            if isinstance(item.meta, dict)
            else {}
        )

        computed = meta.get(
            "computed_payroll",
            {},
        )

        if not isinstance(computed, dict):
            computed = {}

        gross_pay = Decimal(
            str(
                computed.get(
                    "gross",
                    item.base_pay
                    or "0.00",
                )
                or "0.00"
            )
        )

        total_deductions = Decimal(
            str(
                item.deductions_total
                or "0.00"
            )
        )

        if total_deductions > gross_pay:
            deduction_over_gross_names.append(
                item.profile.user.get_full_name()
                or item.profile.user.username
            )

    deductions_valid = (
        len(deduction_over_gross_names) == 0
    )

    add_check(
        "Deductions do not exceed gross pay",
        deductions_valid,
        (
            "All employee deductions are within gross pay."
            if deductions_valid
            else (
                "Deductions exceed gross pay: "
                + ", ".join(
                    deduction_over_gross_names[:5]
                )
            )
        ),
    )

    if not deductions_valid:
        errors.append(
            "Total deductions exceed gross pay for: "
            + ", ".join(
                deduction_over_gross_names[:5]
            )
            + "."
        )

    # -------------------------------------------------
    # 9. Contribution settings exist
    # -------------------------------------------------
    profiles_with_contributions = set(
        EmployeeContribution.objects
        .filter(
            profile_id__in=profile_ids
        )
        .values_list(
            "profile_id",
            flat=True,
        )
    )

    missing_contribution_names = []

    for item in items:
        if (
            item.profile_id
            not in profiles_with_contributions
        ):
            missing_contribution_names.append(
                item.profile.user.get_full_name()
                or item.profile.user.username
            )

    contributions_complete = (
        len(missing_contribution_names) == 0
    )

    add_check(
        "Contribution records exist",
        contributions_complete,
        (
            "All employees have contribution settings."
            if contributions_complete
            else (
                "Missing contribution settings: "
                + ", ".join(
                    missing_contribution_names[:5]
                )
            )
        ),
    )

    if not contributions_complete:
        errors.append(
            "Contribution settings are missing for: "
            + ", ".join(
                missing_contribution_names[:5]
            )
            + "."
        )

    # -------------------------------------------------
    # 10. Batch totals match PayrollItem totals
    # -------------------------------------------------
    calculated_total_net = sum(
        (
            Decimal(
                str(
                    item.net_pay
                    or "0.00"
                )
            )
            for item in items
        ),
        Decimal("0.00"),
    )

    calculated_total_deductions = sum(
        (
            Decimal(
                str(
                    item.deductions_total
                    or "0.00"
                )
            )
            for item in items
        ),
        Decimal("0.00"),
    )

    saved_total_net = Decimal(
        str(
            batch.totals_net
            or "0.00"
        )
    )

    saved_total_deductions = Decimal(
        str(
            batch.totals_deductions
            or "0.00"
        )
    )

    totals_match = (
        _money(calculated_total_net)
        == _money(saved_total_net)
        and
        _money(calculated_total_deductions)
        == _money(saved_total_deductions)
    )

    add_check(
        "Batch totals match employee payroll items",
        totals_match,
        (
            "Saved totals match PayrollItem totals."
            if totals_match
            else (
                "Saved batch totals do not match "
                "the sum of employee payroll items."
            )
        ),
    )

    if not totals_match:
        errors.append(
            "Batch totals do not match the saved employee payroll-item totals. "
            "Reprocess the batch before finalization."
        )

    return {
        "is_ready": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }
def _get_payroll_batch_finalization_errors(batch):
    """
    Return only the critical errors used by the
    payroll-batch finalization view.
    """
    validation = _build_payroll_batch_validation(
        batch
    )

    return validation["errors"]

@login_required
def admin_payroll(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    selected_branch = request.GET.get("branch")
    branch_obj = _scoped_branch_for_admin_or_404(request, selected_branch)

    if not branch_obj:
        messages.error(request, "No valid branch selected or assigned.")
        return redirect("admin_dashboard")

    branches = (
        Branch.objects.all().order_by("name")
        if request.user.is_superuser
        else Branch.objects.filter(id=branch_obj.id)
    )

    payroll_periods = PayrollPeriod.objects.all().order_by("-start_date")[:24]
    selected_period = request.GET.get("period")

    period_obj = None
    if selected_period and str(selected_period).isdigit():
        period_obj = PayrollPeriod.objects.filter(id=int(selected_period)).first()

    if not period_obj:
        period_obj = payroll_periods.first()

    if not period_obj:
        messages.error(request, "Please create a payroll period first.")
        return render(request, "admin/payroll.html", {
            "current": "payroll",
            "is_superadmin": request.user.is_superuser,
            "branches": branches,
            "selected_branch": branch_obj.id,
            "payroll_periods": [],
            "selected_period": None,
            "payroll_rules": None,
            "employees": [],
            "payroll_batches": [],
            "total_payroll": Decimal("0.00"),
            "attendance_deductions": Decimal("0.00"),
            "gov_contributions": Decimal("0.00"),
            "travel_count": 0,
            "payroll_status": "Draft",
            "missing_checkout_count": 0,
            "missing_lunch_count": 0,
            "no_salary_count": 0,
            "contribution_missing_count": 0,
            "ot_disqualified_count": 0,
            "salary_divisor": Decimal("22"),
            "ot_multiplier": Decimal("1.25"),
        })

    payroll_rules = _get_or_create_rules(branch_obj)

    emp_type = (request.GET.get("emp_type") or request.GET.get("type") or "ALL").upper()

    valid_emp_types = {
        "ALL",
        UserProfile.EMP_JO,
        UserProfile.EMP_COS,
        UserProfile.EMP_PERMANENT,
    }

    if emp_type not in valid_emp_types:
        emp_type = "ALL"

    prof_qs = UserProfile.objects.select_related("user", "branch").filter(
        is_approved=True,
        branch=branch_obj,
        user__is_staff=False,
        user__is_superuser=False,
    )

    if emp_type != "ALL":
        prof_qs = prof_qs.filter(employment_type=emp_type)

    employees = []
    total_payroll = Decimal("0.00")
    attendance_deductions = Decimal("0.00")
    gov_contributions = Decimal("0.00")

    missing_checkout_count = 0
    missing_lunch_count = 0
    no_salary_count = 0
    contribution_missing_count = 0
    travel_count = 0
    ot_disqualified_count = 0

    # -------------------------------------------------
    # DTR lock/finalization status for selected period
    # -------------------------------------------------
    finalized_dtr_qs = (
        FinalizedDTR.objects
        .select_related("profile", "finalized_by", "unlocked_by")
        .filter(
            profile__in=prof_qs,
            period=selected_period,
        )
    )

    finalized_dtr_map = {
        dtr.profile_id: dtr
        for dtr in finalized_dtr_qs
    }

    dtr_locked_count = finalized_dtr_qs.filter(is_locked=True).count()
    dtr_unlocked_count = finalized_dtr_qs.filter(is_locked=False).count()
    dtr_finalized_count = finalized_dtr_qs.count()

    for prof in prof_qs.order_by("user__username"):
        result = _compute_payroll(prof, branch_obj, period_obj, payroll_rules)
        saved_item = (
            PayrollItem.objects
            .filter(
                batch__branch=branch_obj,
                batch__period=period_obj,
                profile=prof,
            )
            .order_by("-batch__processed_at", "-id")
            .first()
        )

        rates = result.get("rates", {})
        summary = result.get("attendance_summary", {})
        p = result.get("computed_payroll", {})
        gov = result.get("gov", {})

        present_days = summary.get("present_days", summary.get("days_present", 0))
        travel_days = summary.get("travel_days", 0)
        holiday_days = summary.get("holiday_days", 0)
        absences = p.get("absences", summary.get("absences", 0))

        late_minutes = p.get("late_minutes", 0)
        undertime_minutes = p.get("undertime_minutes", 0)

        late_deduction = p.get("late_deduction", Decimal("0.00"))
        undertime_deduction = p.get("undertime_deduction", Decimal("0.00"))

        net_pay = p.get("net", Decimal("0.00"))
        deductions = p.get("deductions", Decimal("0.00"))
        gov_total = gov.get("gov_total", Decimal("0.00"))

        total_payroll += Decimal(net_pay or 0)
        attendance_deductions += Decimal(late_deduction or 0) + Decimal(undertime_deduction or 0)
        gov_contributions += Decimal(gov_total or 0)

        issues_text = result.get("issues", "") or ""

        if "missing" in issues_text.lower():
            missing_checkout_count += 1

        if "lunch" in issues_text.lower():
            missing_lunch_count += 1

        if "salary" in issues_text.lower():
            no_salary_count += 1

        if "contribution" in issues_text.lower():
            contribution_missing_count += 1

        if int(travel_days or 0) > 0:
            travel_count += 1

        if "ot disqualified" in issues_text.lower():
            ot_disqualified_count += 1
        
        finalized_dtr = finalized_dtr_map.get(prof.id)

        if finalized_dtr:
            if finalized_dtr.is_locked:
                dtr_status = "LOCKED"
                dtr_status_label = "Locked / Finalized"
            else:
                dtr_status = "UNLOCKED"
                dtr_status_label = "Unlocked for Correction"
        else:
            dtr_status = "NOT_FINALIZED"
            dtr_status_label = "Not Finalized"

        employees.append({
            "id": prof.id,
            "profile_id": prof.id,
            "payroll_item_id": saved_item.id if saved_item else None,
            "user_id": prof.user.id,

            "username": prof.user.username,
            "user": prof.user,
            "employee": prof.user,
            "name": prof.user.get_full_name() or prof.user.username,
            "full_name": prof.user.get_full_name() or prof.user.username,
            "employee_name": prof.user.get_full_name() or prof.user.username,

            "employee_id": result.get("picked_employee_id", ""),
            "biometric_employee_id": result.get("picked_employee_id", ""),
            "department": prof.department or "—",

            "type": prof.employment_type,
            "emp_type": prof.employment_type,
            "employment_type": prof.employment_type,

            "branch": prof.branch,
            "branch_name": prof.branch.name if prof.branch else "—",
            "position": prof.position or "—",

            "picked_employee_id": result.get("picked_employee_id", ""),
            "employee_id": result.get("picked_employee_id", ""),
            "biometric_employee_id": result.get("picked_employee_id", ""),
            "department": prof.department or "—",

            "days_present": present_days,


            "present_days": present_days,
            "travel_days": travel_days,
            "holidays_suspensions": holiday_days,
            "holiday_days": holiday_days,

            "late_minutes": late_minutes,
            "undertime_minutes": undertime_minutes,
            "absences": absences,

            "daily_rate": rates.get("daily", Decimal("0.00")),
            "hourly_rate": rates.get("hourly", Decimal("0.00")),
            "per_minute_rate": rates.get("per_minute", Decimal("0.00")),

            "base_pay": p.get("base", Decimal("0.00")),
            "premium": p.get("premium", Decimal("0.00")),
            "premium_pay": p.get("premium", Decimal("0.00")),
            "ot_pay": p.get("ot", Decimal("0.00")),
            "overtime_hours": p.get("overtime_hours", Decimal("0.00")),

            "gov_contributions": gov_total,
            "gov_contributions_total": gov_total,
            "tax": gov.get("tax", Decimal("0.00")),
            "tax_total": gov.get("tax", Decimal("0.00")),

            "late_deduction": late_deduction,
            "undertime_deduction": undertime_deduction,
            "deductions": deductions,
            "deductions_total": deductions,
            "net_pay": net_pay,

            "attendance_summary": summary,
            "computed_payroll": {
                "base": p.get("base", Decimal("0.00")),
                "premium": p.get("premium", Decimal("0.00")),
                "ot": p.get("ot", Decimal("0.00")),
                "net": net_pay,
                "late_minutes": late_minutes,
                "undertime_minutes": undertime_minutes,
                "absences": absences,
                "late_deduction": late_deduction,
                "undertime_deduction": undertime_deduction,
                "deductions": deductions,
            },
            

            "issues": issues_text,
            "dtr_records_json": json.dumps(result.get("dtr_rows", []), default=str),

            "basic_salary": p.get("basic_salary", p.get("base", Decimal("0.00"))),
            "pera": p.get("pera", Decimal("0.00")),
            "other_earnings": p.get("other_earnings", Decimal("0.00")),

            "wtax": gov.get("wtax", gov.get("tax", Decimal("0.00"))),
            "gsis_employee": gov.get("gsis_employee", Decimal("0.00")),
            "gsis_employer": gov.get("gsis_employer", Decimal("0.00")),
            "loan_deduction": gov.get("loan_deduction", Decimal("0.00")),
            "other_deduction": gov.get("other_deduction", Decimal("0.00")),
            "other_employer_contribution": gov.get("other_employer_contribution", Decimal("0.00")),
            "employer_contributions_total": gov.get("employer_contributions_total", Decimal("0.00")),

            "dtr_status": dtr_status,
            "dtr_status_label": dtr_status_label,
            "dtr_locked": bool(finalized_dtr and finalized_dtr.is_locked),
            "dtr_finalized": bool(finalized_dtr),
            "dtr_id": finalized_dtr.id if finalized_dtr else None,
            "dtr_finalized_by": finalized_dtr.finalized_by.username if finalized_dtr and finalized_dtr.finalized_by else "",
            "dtr_finalized_at": finalized_dtr.finalized_at if finalized_dtr else None,
            "dtr_unlocked_by": finalized_dtr.unlocked_by.username if finalized_dtr and finalized_dtr.unlocked_by else "",
            "dtr_unlocked_at": finalized_dtr.unlocked_at if finalized_dtr else None,
            "dtr_unlock_reason": finalized_dtr.unlock_reason if finalized_dtr else "",

                        
        })

    batch_qs = (
    PayrollBatch.objects
    .select_related(
    "branch",
    "period",
    "processed_by",
    "finalized_by",
    "reopened_by",
    )
    .order_by("-processed_at", "-created_at")
    )

    if branch_obj:
        batch_qs = batch_qs.filter(branch=branch_obj)

    if emp_type != "ALL":
        batch_qs = batch_qs.filter(employee_type_scope=emp_type)

    payroll_batches = batch_qs[:10]


    formatted_batches = []
    for b in payroll_batches:
        formatted_batches.append({
            "id": b.id,
            "name": b.name,
            "branch_name": b.branch.name,
            "period_name": b.period.name,
            "employee_type_scope": getattr(b, "employee_type_scope", "ALL"),
            "is_finalized": (
                b.status == PayrollBatch.STATUS_FINALIZED
            ),
            "finalized_by": (
                b.finalized_by.username
                if b.finalized_by
                else ""
            ),
            "finalized_at": b.finalized_at,
            "reopened_by": (
                b.reopened_by.username
                if b.reopened_by
                else ""
            ),
            "reopened_at": b.reopened_at,
            "reopen_reason": b.reopen_reason or "",

            "status": b.get_status_display() if hasattr(b, "get_status_display") else b.status,
            "processed_by": b.processed_by.username if b.processed_by else "—",
            "processed_at": b.processed_at,
            "total_net": b.totals_net,
        })

    context = {
        "current": "payroll",
        "is_superadmin": request.user.is_superuser,
        "branches": branches,
        "selected_branch": branch_obj.id,
        "payroll_periods": payroll_periods,
        "selected_period": period_obj,
        "payroll_rules": payroll_rules,

        "employees": employees,
        "payroll_batches": formatted_batches,

        "total_payroll": _money(total_payroll),
        "attendance_deductions": _money(attendance_deductions),
        "gov_contributions": _money(gov_contributions),
        "travel_count": travel_count,
        "payroll_status": "Draft",

        "missing_checkout_count": missing_checkout_count,
        "missing_lunch_count": missing_lunch_count,
        "no_salary_count": no_salary_count,
        "contribution_missing_count": contribution_missing_count,
        "ot_disqualified_count": ot_disqualified_count,

        "salary_divisor": payroll_rules.salary_divisor,
        "ot_multiplier": payroll_rules.ot_multiplier,

        "selected_emp_type": emp_type,
        "missing_contribution_count": contribution_missing_count,

        "dtr_locked_count": dtr_locked_count,
        "dtr_unlocked_count": dtr_unlocked_count,
        "dtr_finalized_count": dtr_finalized_count,
    }

    return render(request, "admin/payroll.html", context)
@login_required
def admin_payslip(request, profile_id, period_id):
    """
    Admin-side printable payslip.

    Supports:
    - JO
    - COS
    - PERMANENT
    """

    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    profile = get_object_or_404(
        UserProfile.objects.select_related("user", "branch"),
        id=profile_id,
        is_approved=True,
        user__is_staff=False,
        user__is_superuser=False,
    )

    period = get_object_or_404(PayrollPeriod, id=period_id)

    # Branch restriction
    if not request.user.is_superuser:
        admin_profile = getattr(request.user, "profile", None)
        admin_branch = getattr(admin_profile, "branch", None)

        if not admin_branch or profile.branch_id != admin_branch.id:
            raise PermissionDenied("You are not allowed to view payslips outside your assigned branch.")

    branch = profile.branch

    if not branch:
        messages.error(request, "Employee has no assigned branch.")
        return redirect("admin_payroll")

    rules = _get_or_create_rules(branch)

    saved_item = (
        PayrollItem.objects
        .select_related("batch", "batch__period", "batch__branch", "batch__processed_by")
        .filter(
            profile=profile,
            batch__period=period,
            batch__branch=branch,
        )
        .order_by("-batch__processed_at", "-batch__created_at", "-id")
        .first()
    )

    payslip_data = _build_payslip_data(
        profile=profile,
        period=period,
        branch=branch,
        rules=rules,
        generated_by_user=request.user,
        saved_item=saved_item,
    )

    context = {
        "profile": profile,
        "period": period,
        "branch": branch,
        "payslip": payslip_data["payslip"],
        "dtr_rows": payslip_data["dtr_rows"],
        "saved_item": saved_item,
        "processed_batch": payslip_data["processed_batch"],
        "meta": payslip_data["meta"],
    }

    return render(request, "admin/payslip.html", context)
    


@login_required
def admin_payroll_preview_api(request):
    """
    Payroll preview API.
    Does not save to database.
    Used for frontend preview/testing.
    """
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=403)

    branch_id = (request.GET.get("branch") or "").strip()
    period_id = (request.GET.get("period") or "").strip()
    emp_type = (request.GET.get("emp_type") or request.GET.get("type") or "ALL").upper()

    branch_obj = _scoped_branch_for_admin_or_404(request, branch_id)
    if not branch_obj:
        return JsonResponse({"ok": False, "error": "Invalid or unauthorized branch."}, status=400)

    period = PayrollPeriod.objects.filter(id=period_id).first() if period_id.isdigit() else None
    if not period:
        period = PayrollPeriod.objects.order_by("-start_date").first()

    if not period:
        return JsonResponse({"ok": False, "error": "No payroll period found."}, status=400)

    rules = _get_or_create_rules(branch_obj)

    prof_qs = UserProfile.objects.select_related("user", "branch").filter(
        is_approved=True,
        branch=branch_obj,
        user__is_staff=False,
        user__is_superuser=False,
    )

    valid_emp_types = {
    "ALL",
    UserProfile.EMP_JO,
    UserProfile.EMP_COS,
    UserProfile.EMP_PERMANENT,
    }

    if emp_type not in valid_emp_types:
        emp_type = "ALL"

    if emp_type != "ALL":
        prof_qs = prof_qs.filter(employment_type=emp_type)
    rows = []
    total_base = Decimal("0.00")
    total_premium = Decimal("0.00")
    total_ot = Decimal("0.00")
    total_deductions = Decimal("0.00")
    total_gov = Decimal("0.00")
    total_tax = Decimal("0.00")
    total_net = Decimal("0.00")

    for prof in prof_qs.order_by("user__username"):
        res = _compute_payroll(prof, branch_obj, period, rules)

        p = res.get("computed_payroll", {})
        gov = res.get("gov", {})
        rates = res.get("rates", {})
        summary = res.get("attendance_summary", {})

        base = _money(p.get("base", Decimal("0.00")))
        premium = _money(p.get("premium", Decimal("0.00")))
        ot = _money(p.get("ot", Decimal("0.00")))
        deductions = _money(p.get("deductions", Decimal("0.00")))
        gov_total = _money(gov.get("gov_total", Decimal("0.00")))
        tax = _money(gov.get("tax", Decimal("0.00")))
        net = _money(p.get("net", Decimal("0.00")))

        rows.append({
            "id": prof.id,
            "profile_id": prof.id,
            "user_id": prof.user.id,
            "username": prof.user.username,
            "name": prof.user.get_full_name() or prof.user.username,
            "full_name": prof.user.get_full_name() or prof.user.username,

            "type": prof.employment_type,
            "employment_type": prof.employment_type,
            "branch": branch_obj.name,
            "department": prof.department or "",
            "position": prof.position or "",

            "biometric_employee_id": prof.biometric_employee_id or "",
            "picked_employee_id": res.get("picked_employee_id", ""),

            "monthly_salary": float(_money(prof.monthly_salary or Decimal("0.00"))),
            "daily_rate_profile": float(_money(prof.daily_rate or Decimal("0.00"))),
            "computed_daily_rate": float(_money(rates.get("daily", Decimal("0.00")))),
            "computed_hourly_rate": float(_money(rates.get("hourly", Decimal("0.00")))),
            "computed_per_minute_rate": float(_money(rates.get("per_minute", Decimal("0.00")))),

            "present_days": int(summary.get("present_days", 0) or 0),
            "travel_days": int(summary.get("travel_days", 0) or 0),
            "holiday_days": int(summary.get("holiday_days", 0) or 0),
            "absences": int(p.get("absences", 0) or 0),
            "missing_logs": int(summary.get("missing_logs", 0) or 0),
            "records_found": int(summary.get("records_found", 0) or 0),
            "records_used": int(summary.get("records_used", 0) or 0),

            "base": float(base),
            "premium": float(premium),
            "overtime_hours": float(_money(p.get("overtime_hours", Decimal("0.00")))),
            "ot": float(ot),

            "late_minutes": int(p.get("late_minutes", 0) or 0),
            "undertime_minutes": int(p.get("undertime_minutes", 0) or 0),
            "late_deduction": float(_money(p.get("late_deduction", Decimal("0.00")))),
            "undertime_deduction": float(_money(p.get("undertime_deduction", Decimal("0.00")))),

            "sss": float(_money(gov.get("sss", Decimal("0.00")))),
            "pagibig": float(_money(gov.get("pagibig", Decimal("0.00")))),
            "philhealth": float(_money(gov.get("philhealth", Decimal("0.00")))),
            "gov_total": float(gov_total),
            "tax": float(tax),

            "deductions": float(deductions),
            "net": float(net),

            "issues": res.get("issues", ""),
            "dtr_rows": res.get("dtr_rows", []),
        })

        total_base += base
        total_premium += premium
        total_ot += ot
        total_deductions += deductions
        total_gov += gov_total
        total_tax += tax
        total_net += net

    return JsonResponse({
        "ok": True,
        "period": {
            "id": period.id,
            "name": period.name,
            "start": str(period.start_date),
            "end": str(period.end_date),
            "pay_mode": period.pay_mode,
        },
        "branch": {
            "id": branch_obj.id,
            "name": branch_obj.name,
        },
        "filter": {
            "employment_type": emp_type,
        },
        "employees": rows,
        "total_employees": len(rows),
        "totals": {
            "base": float(_money(total_base)),
            "premium": float(_money(total_premium)),
            "ot": float(_money(total_ot)),
            "gov": float(_money(total_gov)),
            "tax": float(_money(total_tax)),
            "deductions": float(_money(total_deductions)),
            "net": float(_money(total_net)),
        },
        "total_net": float(_money(total_net)),
    })


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
    result = _compute_payroll(prof, branch_obj, period, rules)

    return JsonResponse({
        "ok": True,
        "employee": {
            "id": prof.id,
            "name": prof.user.get_full_name() or prof.user.username,
            "biometric_employee_id": _get_profile_biometric_id(prof),
        },
        "branch": {"id": branch_obj.id, "name": branch_obj.name},
        "period": {"id": period.id, "name": period.name},
        "rows": result["dtr_rows"],
    })
def _admin_can_access_profile(request, profile):
    """
    Superuser can access all branches.
    Staff admin can access only employees in their branch.
    """
    if request.user.is_superuser:
        return True

    try:
        admin_branch = request.user.profile.branch
    except UserProfile.DoesNotExist:
        return False

    return admin_branch and profile.branch_id == admin_branch.id


def _safe_json_snapshot(value, default):
    """
    Converts Decimals/dates safely into JSON-compatible data.
    """
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return default


def _get_item_dtr_rows(item):
    meta = item.meta or {}

    rows = meta.get("dtr_rows")
    if rows:
        return rows

    rows = meta.get("dtr")
    if rows:
        return rows

    return []


def _get_item_dtr_summary(item):
    meta = item.meta or {}

    summary = meta.get("attendance_summary")
    if isinstance(summary, dict):
        return summary

    computed = meta.get("computed_payroll")
    if isinstance(computed, dict):
        return {
            "late_minutes": computed.get("late_minutes", 0),
            "undertime_minutes": computed.get("undertime_minutes", 0),
            "absences": computed.get("absences", 0),
        }

    return {}


def _normalize_dtr_rows_for_print(rows):
    """
    Ensures each DTR row has a day value for Civil Service Form No. 48.
    """
    normalized = []

    for row in rows or []:
        if not isinstance(row, dict):
            continue

        item = dict(row)

        if not item.get("day"):
            raw_date = str(item.get("date") or "").strip()

            try:
                parsed = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
                item["day"] = parsed.day
            except Exception:
                item["day"] = ""

        item.setdefault("am_in", "")
        item.setdefault("am_out", "")
        item.setdefault("pm_in", "")
        item.setdefault("pm_out", "")
        item.setdefault("total_hours", "0.00")
        item.setdefault("late", 0)
        item.setdefault("undertime", 0)
        item.setdefault("status", "")
        item.setdefault("remarks", "")

        normalized.append(item)

    return normalized


def _dtr_totals(rows):
    total_undertime = 0

    for row in rows or []:
        try:
            total_undertime += int(row.get("undertime", 0) or 0)
        except Exception:
            pass

    return {
        "total_undertime_hours": total_undertime // 60,
        "total_undertime_minutes": total_undertime % 60,
    }


@login_required
def admin_payroll_item_dtr_print(request, item_id):
    """
    Print saved Civil Service Form No. 48 DTR.

    If FinalizedDTR exists and is locked:
        use locked DTR rows.

    If no locked FinalizedDTR exists:
        use PayrollItem.meta DTR snapshot.
    """

    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    item = get_object_or_404(
        PayrollItem.objects.select_related(
            "batch",
            "batch__period",
            "batch__branch",
            "batch__processed_by",
            "profile",
            "profile__user",
            "profile__branch",
        ),
        id=item_id,
    )

    profile = item.profile
    batch = item.batch
    period = batch.period
    branch = batch.branch or profile.branch

    if not _admin_can_access_profile(request, profile):
        raise PermissionDenied("You are not allowed to print DTR outside your assigned branch.")

    finalized_dtr = (
        FinalizedDTR.objects
        .select_related("profile", "period", "branch", "finalized_by", "unlocked_by", "payroll_item")
        .filter(profile=profile, period=period)
        .first()
    )

    if finalized_dtr and finalized_dtr.is_locked:
        dtr_rows = _normalize_dtr_rows_for_print(finalized_dtr.rows)
        dtr_summary = finalized_dtr.summary or {}
        dtr_source_label = "Locked / Finalized DTR"
        dtr_locked = True
    else:
        dtr_rows = _normalize_dtr_rows_for_print(_get_item_dtr_rows(item))
        dtr_summary = _get_item_dtr_summary(item)
        dtr_source_label = "Processed Payroll Snapshot"
        dtr_locked = False

    totals = _dtr_totals(dtr_rows)

    meta = item.meta or {}
    employee_meta = meta.get("employee", {}) if isinstance(meta.get("employee", {}), dict) else {}

    employee_name = (
        employee_meta.get("full_name")
        or profile.user.get_full_name()
        or profile.user.username
    )

    biometric_employee_id = (
        employee_meta.get("biometric_employee_id")
        or profile.biometric_employee_id
        or ""
    )

    division = (
        employee_meta.get("department")
        or profile.department
        or "—"
    )

    context = {
        "item": item,
        "batch": batch,
        "period": period,
        "branch": branch,
        "profile": profile,

        "employee_name": employee_name,
        "biometric_employee_id": biometric_employee_id,
        "division": division,
        "employee_type_scope": getattr(batch, "employee_type_scope", "ALL"),

        "month_label": f"{period.start_date.strftime('%B %d, %Y')} - {period.end_date.strftime('%B %d, %Y')}",
        "dtr_rows": dtr_rows,
        "dtr_summary": dtr_summary,

        "total_undertime_hours": totals["total_undertime_hours"],
        "total_undertime_minutes": totals["total_undertime_minutes"],

        "finalized_dtr": finalized_dtr,
        "dtr_locked": dtr_locked,
        "dtr_source_label": dtr_source_label,

        "printed_by": request.user.get_full_name() or request.user.username,
        "printed_at": timezone.localtime(timezone.now()),
    }

    return render(request, "admin/payroll_dtr_print.html", context)

@login_required
@require_POST
def admin_finalize_payroll_item_dtr(request, item_id):
    """
    Finalize and lock DTR from a processed PayrollItem snapshot.
    """

    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    item = get_object_or_404(
        PayrollItem.objects.select_related(
            "batch",
            "batch__period",
            "batch__branch",
            "profile",
            "profile__user",
            "profile__branch",
        ),
        id=item_id,
    )

    profile = item.profile
    batch = item.batch
    period = batch.period
    branch = batch.branch or profile.branch

    if not _admin_can_access_profile(request, profile):
        raise PermissionDenied("You are not allowed to finalize DTR outside your assigned branch.")

    rows = _safe_json_snapshot(_get_item_dtr_rows(item), [])
    summary = _safe_json_snapshot(_get_item_dtr_summary(item), {})

    if not rows:
        messages.error(request, "Cannot finalize DTR because this payroll item has no saved DTR rows.")
        return redirect("admin_payroll_item_dtr_print", item_id=item.id)

    source_meta = {
        "payroll_item_id": item.id,
        "batch_id": batch.id,
        "batch_name": batch.name,
        "employee_type_scope": getattr(batch, "employee_type_scope", "ALL"),
        "processed_at": str(batch.processed_at or ""),
        "processed_by": batch.processed_by.username if batch.processed_by else "",
    }

    with transaction.atomic():
        finalized_dtr, created = FinalizedDTR.objects.select_for_update().get_or_create(
            profile=profile,
            period=period,
            defaults={
                "branch": branch,
                "payroll_item": item,
                "rows": rows,
                "summary": summary,
                "source_meta": source_meta,
                "is_locked": True,
                "finalized_by": request.user,
                "finalized_at": timezone.now(),
            },
        )

        if finalized_dtr.is_locked and not created:
            messages.info(request, "This DTR is already finalized and locked.")
            return redirect("admin_payroll_item_dtr_print", item_id=item.id)

        finalized_dtr.branch = branch
        finalized_dtr.payroll_item = item
        finalized_dtr.rows = rows
        finalized_dtr.summary = summary
        finalized_dtr.source_meta = source_meta
        finalized_dtr.is_locked = True
        finalized_dtr.finalized_by = request.user
        finalized_dtr.finalized_at = timezone.now()
        finalized_dtr.unlocked_by = None
        finalized_dtr.unlocked_at = None
        finalized_dtr.unlock_reason = ""
        finalized_dtr.save()

    messages.success(request, "DTR finalized and locked successfully.")
    return redirect("admin_payroll_item_dtr_print", item_id=item.id)


@login_required
@require_POST
def admin_unlock_finalized_dtr(request, dtr_id):
    """
    Unlock a finalized DTR so payroll/DTR can be corrected.

    This keeps the audit trail:
    - who unlocked
    - when unlocked
    - reason
    """

    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    finalized_dtr = get_object_or_404(
        FinalizedDTR.objects.select_related(
            "profile",
            "profile__user",
            "profile__branch",
            "period",
            "branch",
            "payroll_item",
        ),
        id=dtr_id,
    )

    if not _admin_can_access_profile(request, finalized_dtr.profile):
        raise PermissionDenied("You are not allowed to unlock DTR outside your assigned branch.")

    reason = (request.POST.get("unlock_reason") or "").strip()

    if len(reason) < 5:
        messages.error(request, "Unlock reason is required and must be at least 5 characters.")
        if finalized_dtr.payroll_item_id:
            return redirect("admin_payroll_item_dtr_print", item_id=finalized_dtr.payroll_item_id)
        return redirect("admin_payroll")

    finalized_dtr.is_locked = False
    finalized_dtr.unlocked_by = request.user
    finalized_dtr.unlocked_at = timezone.now()
    finalized_dtr.unlock_reason = reason
    finalized_dtr.save(update_fields=["is_locked", "unlocked_by", "unlocked_at", "unlock_reason", "updated_at"])

    messages.success(request, "DTR unlocked. You may now correct attendance/payroll and re-finalize.")
    if finalized_dtr.payroll_item_id:
        return redirect("admin_payroll_item_dtr_print", item_id=finalized_dtr.payroll_item_id)

    return redirect("admin_payroll")


@login_required
@require_POST
def admin_payroll_process_batch(request):
    """
    Safely process payroll by branch + period + employee_type_scope.

    This prevents this problem:
    - Process JO for May 1-15
    - Then process PERMANENT for May 1-15
    - Permanent processing accidentally deletes JO payroll items

    Scope values:
    - ALL
    - JO
    - COS
    - PERMANENT
    """

    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "Unauthorized."}, status=403)

    period_id = (request.POST.get("period") or request.POST.get("period_id") or "").strip()
    branch_id = (request.POST.get("branch") or request.POST.get("branch_id") or "").strip()
    emp_type = (request.POST.get("type") or request.POST.get("emp_type") or "ALL").strip().upper()
    search = (request.POST.get("search") or "").strip()

    valid_emp_types = {
        PayrollBatch.SCOPE_ALL,
        PayrollBatch.SCOPE_JO,
        PayrollBatch.SCOPE_COS,
        PayrollBatch.SCOPE_PERMANENT,
    }

    if emp_type not in valid_emp_types:
        emp_type = PayrollBatch.SCOPE_ALL

    if not period_id:
        return JsonResponse({"ok": False, "error": "Payroll period is required."}, status=400)

    period = PayrollPeriod.objects.filter(id=period_id).first()
    if not period:
        return JsonResponse({"ok": False, "error": "Invalid payroll period."}, status=400)

    # -------------------------
    # Branch scope
    # -------------------------
    if request.user.is_superuser:
        if not branch_id:
            return JsonResponse({"ok": False, "error": "Branch is required."}, status=400)

        branch_obj = Branch.objects.filter(id=branch_id).first()
        if not branch_obj:
            return JsonResponse({"ok": False, "error": "Invalid branch."}, status=400)

    else:
        branch_obj = _get_admin_branch(request)
        if not branch_obj:
            return JsonResponse({"ok": False, "error": "Admin has no assigned branch."}, status=400)

    rules = _get_or_create_rules(branch_obj)

    # -------------------------
    # Employees included
    # -------------------------
    prof_qs = (
        UserProfile.objects
        .select_related("user", "branch")
        .filter(
            branch=branch_obj,
            is_approved=True,
            user__is_staff=False,
            user__is_superuser=False,
        )
    )

    if emp_type != PayrollBatch.SCOPE_ALL:
        prof_qs = prof_qs.filter(employment_type=emp_type)

    if search:
        prof_qs = prof_qs.filter(
            Q(user__username__icontains=search)
            | Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)
            | Q(biometric_employee_id__icontains=search)
            | Q(department__icontains=search)
            | Q(position__icontains=search)
        )

    if not prof_qs.exists():
        return JsonResponse(
            {"ok": False, "error": "No approved employees found for this branch and filter."},
            status=400,
        )
    locked_dtrs = (
        FinalizedDTR.objects
        .select_related("profile", "profile__user")
        .filter(
            profile__in=prof_qs,
            period=period,
            is_locked=True,
        )
    )

    if locked_dtrs.exists():
        locked_names = [
            d.profile.user.get_full_name() or d.profile.user.username
            for d in locked_dtrs[:5]
        ]

        return JsonResponse(
            {
                "ok": False,
                "error": (
                    "Payroll cannot be reprocessed because some DTRs are finalized/locked: "
                    + ", ".join(locked_names)
                    + ". Unlock the DTR first if correction is required."
                ),
            },
            status=400,
        )
        

    totals_net = Decimal("0.00")
    totals_deductions = Decimal("0.00")
    total_items = 0

    with transaction.atomic():
        # -------------------------------------------------
        # Block regeneration of an officially finalized batch
        # -------------------------------------------------
        existing_batch = (
            PayrollBatch.objects
            .select_for_update()
            .filter(
                branch=branch_obj,
                period=period,
                employee_type_scope=emp_type,
            )
            .first()
        )

        if (
            existing_batch
            and existing_batch.status == PayrollBatch.STATUS_FINALIZED
        ):
            return JsonResponse(
                {
                    "ok": False,
                    "error": (
                        "This payroll batch is officially finalized. "
                        "Reopen the payroll batch before attempting "
                        "to process it again."
                    ),
                },
                status=400,
            )
        batch, created = PayrollBatch.objects.get_or_create(
            branch=branch_obj,
            period=period,
            employee_type_scope=emp_type,
            defaults={
                "name": f"Payroll {branch_obj.name} - {period.name} - {emp_type}",
                "status": PayrollBatch.STATUS_DRAFT,
                "processed_by": request.user,
                "processed_at": timezone.now(),
            },
        )

        # Lock this batch row during processing.
        batch = PayrollBatch.objects.select_for_update().get(id=batch.id)

        batch.name = f"Payroll {branch_obj.name} - {period.name} - {emp_type}"
        batch.status = PayrollBatch.STATUS_DRAFT
        batch.processed_by = request.user
        batch.processed_at = timezone.now()
        batch.save(update_fields=["name", "status", "processed_by", "processed_at"])

        # Safe deletion:
        # Only delete items inside this exact scoped batch.
        # JO batch will not delete PERMANENT batch.
        # COS batch will not delete JO batch.
        PayrollItem.objects.filter(batch=batch).delete()

        for prof in prof_qs.order_by("user__username"):
            res = _compute_payroll(prof, branch_obj, period, rules)

            p = res.get("computed_payroll", {})
            gov = res.get("gov", {})
            rates = res.get("rates", {})
            attendance_summary = res.get("attendance_summary", {})
            dtr_rows = res.get("dtr_rows", [])

            base_pay = _money(p.get("base", Decimal("0.00")))
            premium_pay = _money(p.get("premium", Decimal("0.00")))
            overtime_hours = _money(p.get("overtime_hours", Decimal("0.00")))
            overtime_pay = _money(p.get("ot", Decimal("0.00")))

            late_minutes = int(p.get("late_minutes", 0) or 0)
            undertime_minutes = int(p.get("undertime_minutes", 0) or 0)
            absences = int(p.get("absences", 0) or 0)

            manual_deduction = _money(p.get("manual_deduction", prof.manual_deduction_amount or Decimal("0.00")))
            deductions_total = _money(p.get("deductions", Decimal("0.00")))
            gov_contributions_total = _money(gov.get("gov_total", Decimal("0.00")))
            tax_total = _money(gov.get("tax", Decimal("0.00")))
            net_pay = _money(p.get("net", Decimal("0.00")))

            issues_text = res.get("issues", "") or ""

            safe_dtr_rows = json.loads(json.dumps(dtr_rows, default=str))

            PayrollItem.objects.create(
                batch=batch,
                profile=prof,

                base_pay=base_pay,
                premium_pay=premium_pay,

                overtime_hours=overtime_hours,
                overtime_pay=overtime_pay,

                late_minutes=late_minutes,
                undertime_minutes=undertime_minutes,
                absences=absences,

                manual_deduction=manual_deduction,

                deductions_total=deductions_total,
                gov_contributions_total=gov_contributions_total,
                tax_total=tax_total,

                net_pay=net_pay,
                issues=issues_text,

                meta={
                    "processed_snapshot": {
                        "processed_at": str(timezone.localtime(timezone.now())),
                        "processed_by": request.user.username,
                        "branch_id": branch_obj.id,
                        "branch_name": branch_obj.name,
                        "period_id": period.id,
                        "period_name": period.name,
                        "employee_type_scope": emp_type,
                    },

                    "employee": {
                        "profile_id": prof.id,
                        "user_id": prof.user.id,
                        "username": prof.user.username,
                        "full_name": prof.user.get_full_name() or prof.user.username,
                        "employment_type": prof.employment_type,
                        "branch": prof.branch.name if prof.branch else "",
                        "department": prof.department or "",
                        "position": prof.position or "",
                        "biometric_employee_id": prof.biometric_employee_id or "",
                        "employee_id_used": res.get("picked_employee_id") or "",
                    },

                    "rates": {
                        "daily": str(rates.get("daily", Decimal("0.00"))),
                        "hourly": str(rates.get("hourly", Decimal("0.00"))),
                        "per_minute": str(rates.get("per_minute", Decimal("0.00"))),
                    },

                    "attendance_summary": attendance_summary,

                    "computed_payroll": {
                        "base": str(p.get("base", Decimal("0.00"))),
                        "basic_salary": str(p.get("basic_salary", p.get("base", Decimal("0.00")))),
                        "pera": str(p.get("pera", Decimal("0.00"))),
                        "other_earnings": str(p.get("other_earnings", Decimal("0.00"))),

                        "premium": str(p.get("premium", Decimal("0.00"))),
                        "overtime_hours": str(p.get("overtime_hours", Decimal("0.00"))),
                        "ot": str(p.get("ot", Decimal("0.00"))),
                        "gross": str(p.get("gross", Decimal("0.00"))),

                        "late_minutes": late_minutes,
                        "undertime_minutes": undertime_minutes,
                        "absences": absences,

                        "late_deduction": str(p.get("late_deduction", Decimal("0.00"))),
                        "undertime_deduction": str(p.get("undertime_deduction", Decimal("0.00"))),
                        "absence_deduction": str(p.get("absence_deduction", Decimal("0.00"))),
                        "attendance_deduction": str(p.get("attendance_deduction", Decimal("0.00"))),

                        "manual_deduction": str(manual_deduction),
                        "loan_deduction": str(p.get("loan_deduction", Decimal("0.00"))),
                        "other_deduction": str(p.get("other_deduction", Decimal("0.00"))),

                        "deductions": str(deductions_total),
                        "net": str(net_pay),
                    },

                    # Flat keys used by payslip helpers.
                    "sss_amount": str(gov.get("sss", Decimal("0.00"))),
                    "philhealth_amount": str(gov.get("philhealth", Decimal("0.00"))),
                    "pagibig_amount": str(gov.get("pagibig", Decimal("0.00"))),
                    "tax_amount": str(gov.get("tax", Decimal("0.00"))),

                    "late_deduction": str(p.get("late_deduction", Decimal("0.00"))),
                    "undertime_deduction": str(p.get("undertime_deduction", Decimal("0.00"))),
                    "absence_deduction": str(p.get("absence_deduction", Decimal("0.00"))),
                    "attendance_deduction": str(p.get("attendance_deduction", Decimal("0.00"))),

                    # Permanent-specific snapshot.
                    "permanent_breakdown": {
                        "basic_salary": str(p.get("basic_salary", p.get("base", Decimal("0.00")))),
                        "pera": str(p.get("pera", Decimal("0.00"))),
                        "other_earnings": str(p.get("other_earnings", Decimal("0.00"))),

                        "wtax": str(gov.get("wtax", gov.get("tax", Decimal("0.00")))),
                        "gsis_employee": str(gov.get("gsis_employee", Decimal("0.00"))),
                        "gsis_employer": str(gov.get("gsis_employer", Decimal("0.00"))),

                        "loan_deduction": str(gov.get("loan_deduction", Decimal("0.00"))),
                        "other_deduction": str(gov.get("other_deduction", Decimal("0.00"))),

                        "other_employer_contribution": str(gov.get("other_employer_contribution", Decimal("0.00"))),
                        "employer_contributions_total": str(gov.get("employer_contributions_total", Decimal("0.00"))),
                    },

                    "gov": {
                        "sss": str(gov.get("sss", Decimal("0.00"))),
                        "philhealth": str(gov.get("philhealth", Decimal("0.00"))),
                        "pagibig": str(gov.get("pagibig", Decimal("0.00"))),
                        "gov_total": str(gov.get("gov_total", Decimal("0.00"))),
                        "tax": str(gov.get("tax", Decimal("0.00"))),

                        "wtax": str(gov.get("wtax", Decimal("0.00"))),
                        "gsis_employee": str(gov.get("gsis_employee", Decimal("0.00"))),
                        "gsis_employer": str(gov.get("gsis_employer", Decimal("0.00"))),
                        "employer_contributions_total": str(gov.get("employer_contributions_total", Decimal("0.00"))),
                    },

                    "dtr_rows": safe_dtr_rows,
                },
            )

            totals_net += net_pay
            totals_deductions += deductions_total
            total_items += 1

        batch.totals_net = _money(totals_net)
        batch.totals_deductions = _money(totals_deductions)
        batch.status = PayrollBatch.STATUS_COMPLETED
        batch.processed_by = request.user
        batch.processed_at = timezone.now()
        batch.save(
            update_fields=[
                "totals_net",
                "totals_deductions",
                "status",
                "processed_by",
                "processed_at",
            ]
        )

    return JsonResponse({
        "ok": True,
        "message": "Payroll processed successfully.",
        "batch_id": batch.id,
        "employee_type_scope": emp_type,
        "total_items": total_items,
        "totals_net": str(_money(totals_net)),
        "totals_deductions": str(_money(totals_deductions)),
    })

# =========================================================
# FINALIZE PAYROLL BATCH
# Place immediately after admin_payroll_process_batch()
# =========================================================

@login_required
@require_POST
def admin_finalize_payroll_batch(request, batch_id):
    """
    Officially finalize one completed payroll batch.

    Finalization requirements:
    - Batch is completed
    - Batch contains payroll items
    - Every included employee has a locked FinalizedDTR
    - No critical payroll configuration errors exist
    """
    if not (
        request.user.is_staff
        or request.user.is_superuser
    ):
        return redirect("login_ui")

    with transaction.atomic():
        batch = get_object_or_404(
            PayrollBatch.objects
            .select_for_update()
            .select_related(
                "branch",
                "period",
                "processed_by",
                "finalized_by",
                "reopened_by",
            ),
            id=batch_id,
        )

        if not _admin_can_access_payroll_batch(request, batch):
            raise PermissionDenied(
                "You cannot finalize payroll outside your assigned branch."
            )

        validation_errors = _get_payroll_batch_finalization_errors(
            batch
        )

        if validation_errors:
            for error in validation_errors:
                messages.error(request, error)

            return redirect(
                _payroll_batch_redirect_url(batch)
            )

        batch.status = PayrollBatch.STATUS_FINALIZED
        batch.finalized_by = request.user
        batch.finalized_at = timezone.now()

        batch.save(
            update_fields=[
                "status",
                "finalized_by",
                "finalized_at",
            ]
        )

    messages.success(
        request,
        f'Payroll batch "{batch.name}" was finalized successfully.',
    )

    return redirect(
        _payroll_batch_redirect_url(batch)
    )


# =========================================================
# REOPEN PAYROLL BATCH
# Place directly below admin_finalize_payroll_batch()
# =========================================================

@login_required
@require_POST
def admin_reopen_payroll_batch(request, batch_id):
    """
    Reopen an officially finalized payroll batch.

    Important:
    Reopening the batch does not automatically unlock employee DTRs.
    Affected DTRs must still be unlocked individually.
    """
    if not (
        request.user.is_staff
        or request.user.is_superuser
    ):
        return redirect("login_ui")

    reopen_reason = (
        request.POST.get("reopen_reason")
        or ""
    ).strip()

    with transaction.atomic():
        batch = get_object_or_404(
            PayrollBatch.objects
            .select_for_update()
            .select_related(
                "branch",
                "period",
                "finalized_by",
                "reopened_by",
            ),
            id=batch_id,
        )

        if not _admin_can_access_payroll_batch(request, batch):
            raise PermissionDenied(
                "You cannot reopen payroll outside your assigned branch."
            )

        if len(reopen_reason) < 5:
            messages.error(
                request,
                "A reopening reason of at least 5 characters is required.",
            )

            return redirect(
                _payroll_batch_redirect_url(batch)
            )

        if batch.status != PayrollBatch.STATUS_FINALIZED:
            messages.info(
                request,
                "This payroll batch is not currently finalized.",
            )

            return redirect(
                _payroll_batch_redirect_url(batch)
            )

        batch.status = PayrollBatch.STATUS_COMPLETED
        batch.reopened_by = request.user
        batch.reopened_at = timezone.now()
        batch.reopen_reason = reopen_reason

        batch.save(
            update_fields=[
                "status",
                "reopened_by",
                "reopened_at",
                "reopen_reason",
            ]
        )

    messages.success(
        request,
        f'Payroll batch "{batch.name}" was reopened successfully.',
    )

    return redirect(
        _payroll_batch_redirect_url(batch)
    )

# =========================================================
# PAYROLL BATCH DETAIL / PRINTABLE PAYROLL REGISTER
# Place immediately after admin_reopen_payroll_batch()
# =========================================================

@login_required
def admin_payroll_batch_detail(request, batch_id):
    """
    Display one saved payroll batch and its official payroll register.

    This page uses saved PayrollItem records only.
    It does not recompute live payroll.
    """

    if not (
        request.user.is_staff
        or request.user.is_superuser
    ):
        return redirect("login_ui")

    batch = get_object_or_404(
        PayrollBatch.objects.select_related(
            "branch",
            "period",
            "processed_by",
            "finalized_by",
            "reopened_by",
        ),
        id=batch_id,
    )

    if not _admin_can_access_payroll_batch(request, batch):
        raise PermissionDenied(
            "You cannot view payroll outside your assigned branch."
        )

    payroll_items = list(
        PayrollItem.objects
        .select_related(
            "profile",
            "profile__user",
            "profile__branch",
        )
        .filter(batch=batch)
        .order_by(
            "profile__employment_type",
            "profile__user__last_name",
            "profile__user__first_name",
            "profile__user__username",
        )
    )

    def money(value):
        try:
            return _money(Decimal(str(value or "0.00")))
        except Exception:
            return Decimal("0.00")

    def get_dict(value):
        return value if isinstance(value, dict) else {}

    def get_meta_money(meta, section_name, key, fallback="0.00"):
        section = get_dict(meta.get(section_name))
        return money(section.get(key, fallback))

    finalized_dtrs = (
        FinalizedDTR.objects
        .select_related(
            "profile",
            "finalized_by",
            "unlocked_by",
        )
        .filter(
            period=batch.period,
            profile_id__in=[
                item.profile_id
                for item in payroll_items
            ],
        )
    )

    finalized_dtr_map = {
        dtr.profile_id: dtr
        for dtr in finalized_dtrs
    }

    register_rows = []

    calculated_total_gross = Decimal("0.00")
    calculated_total_deductions = Decimal("0.00")
    calculated_total_net = Decimal("0.00")
    calculated_total_employer_cost = Decimal("0.00")

    for item in payroll_items:
        profile = item.profile
        user = profile.user
        meta = get_dict(item.meta)

        computed_meta = get_dict(
            meta.get("computed_payroll")
        )

        permanent_meta = get_dict(
            meta.get("permanent_breakdown")
        )

        gov_meta = get_dict(
            meta.get("gov")
        )

        attendance_meta = get_dict(
            meta.get("attendance_summary")
        )

        employment_type = profile.employment_type

        employee_name = (
            user.get_full_name()
            or user.username
        )

        basic_or_base_pay = money(item.base_pay)
        premium_pay = money(item.premium_pay)
        overtime_pay = money(item.overtime_pay)

        pera = Decimal("0.00")
        other_earnings = Decimal("0.00")

        if employment_type == UserProfile.EMP_PERMANENT:
            basic_or_base_pay = money(
                permanent_meta.get(
                    "basic_salary",
                    item.base_pay,
                )
            )

            pera = money(
                permanent_meta.get(
                    "pera",
                    "0.00",
                )
            )

            other_earnings = money(
                permanent_meta.get(
                    "other_earnings",
                    "0.00",
                )
            )

        gross_pay = money(
            basic_or_base_pay
            + premium_pay
            + overtime_pay
            + pera
            + other_earnings
        )

        late_deduction = money(
            computed_meta.get(
                "late_deduction",
                meta.get(
                    "late_deduction",
                    "0.00",
                ),
            )
        )

        undertime_deduction = money(
            computed_meta.get(
                "undertime_deduction",
                meta.get(
                    "undertime_deduction",
                    "0.00",
                ),
            )
        )

        absence_deduction = money(
            computed_meta.get(
                "absence_deduction",
                meta.get(
                    "absence_deduction",
                    "0.00",
                ),
            )
        )

        manual_deduction = money(
            item.manual_deduction
        )

        sss = money(
            meta.get(
                "sss_amount",
                gov_meta.get(
                    "sss",
                    "0.00",
                ),
            )
        )

        philhealth = money(
            meta.get(
                "philhealth_amount",
                gov_meta.get(
                    "philhealth",
                    "0.00",
                ),
            )
        )

        pagibig = money(
            meta.get(
                "pagibig_amount",
                gov_meta.get(
                    "pagibig",
                    "0.00",
                ),
            )
        )

        tax_or_wtax = money(
            permanent_meta.get(
                "wtax",
                item.tax_total,
            )
            if employment_type == UserProfile.EMP_PERMANENT
            else item.tax_total
        )

        gsis_employee = money(
            permanent_meta.get(
                "gsis_employee",
                "0.00",
            )
        )

        gsis_employer = money(
            permanent_meta.get(
                "gsis_employer",
                "0.00",
            )
        )

        loan_deduction = money(
            permanent_meta.get(
                "loan_deduction",
                computed_meta.get(
                    "loan_deduction",
                    "0.00",
                ),
            )
        )

        other_deduction = money(
            permanent_meta.get(
                "other_deduction",
                computed_meta.get(
                    "other_deduction",
                    "0.00",
                ),
            )
        )

        employer_contributions_total = money(
            permanent_meta.get(
                "employer_contributions_total",
                gov_meta.get(
                    "employer_contributions_total",
                    "0.00",
                ),
            )
        )

        total_deductions = money(
            item.deductions_total
        )

        net_pay = money(
            item.net_pay
        )

        finalized_dtr = finalized_dtr_map.get(
            profile.id
        )

        if finalized_dtr and finalized_dtr.is_locked:
            dtr_status = "LOCKED"
            dtr_status_label = "Locked / Finalized"
        elif finalized_dtr:
            dtr_status = "UNLOCKED"
            dtr_status_label = "Unlocked for Correction"
        else:
            dtr_status = "NOT_FINALIZED"
            dtr_status_label = "Not Finalized"

        register_rows.append({
            "item_id": item.id,
            "profile_id": profile.id,

            "employee_name": employee_name,
            "username": user.username,
            "biometric_employee_id": (
                profile.biometric_employee_id
                or "—"
            ),
            "employment_type": employment_type,
            "department": profile.department or "—",
            "position": profile.position or "—",

            "days_present": attendance_meta.get(
                "present_days",
                attendance_meta.get(
                    "days_present",
                    0,
                ),
            ),
            "travel_days": attendance_meta.get(
                "travel_days",
                0,
            ),
            "holiday_days": attendance_meta.get(
                "holiday_days",
                0,
            ),
            "late_minutes": item.late_minutes or 0,
            "undertime_minutes": (
                item.undertime_minutes
                or 0
            ),
            "absences": item.absences or 0,

            "base_pay": basic_or_base_pay,
            "pera": pera,
            "other_earnings": other_earnings,
            "premium_pay": premium_pay,
            "overtime_hours": money(
                item.overtime_hours
            ),
            "overtime_pay": overtime_pay,
            "gross_pay": gross_pay,

            "late_deduction": late_deduction,
            "undertime_deduction": (
                undertime_deduction
            ),
            "absence_deduction": (
                absence_deduction
            ),

            "sss": sss,
            "philhealth": philhealth,
            "pagibig": pagibig,
            "tax_or_wtax": tax_or_wtax,
            "gsis_employee": gsis_employee,
            "gsis_employer": gsis_employer,
            "loan_deduction": loan_deduction,
            "other_deduction": other_deduction,
            "manual_deduction": manual_deduction,

            "government_total": money(
                item.gov_contributions_total
            ),
            "total_deductions": total_deductions,
            "net_pay": net_pay,

            "employer_contributions_total": (
                employer_contributions_total
            ),

            "dtr_status": dtr_status,
            "dtr_status_label": dtr_status_label,
            "issues": item.issues or "",
        })

        calculated_total_gross += gross_pay
        calculated_total_deductions += (
            total_deductions
        )
        calculated_total_net += net_pay
        calculated_total_employer_cost += (
            employer_contributions_total
        )

    calculated_total_gross = money(
        calculated_total_gross
    )

    calculated_total_deductions = money(
        calculated_total_deductions
    )

    calculated_total_net = money(
        calculated_total_net
    )

    calculated_total_employer_cost = money(
        calculated_total_employer_cost
    )

    locked_dtr_count = sum(
        1
        for row in register_rows
        if row["dtr_status"] == "LOCKED"
    )

    unlocked_dtr_count = sum(
        1
        for row in register_rows
        if row["dtr_status"] == "UNLOCKED"
    )

    not_finalized_dtr_count = sum(
        1
        for row in register_rows
        if row["dtr_status"] == "NOT_FINALIZED"
    )
    batch_validation = (
        _build_payroll_batch_validation(
            batch
        )
    )
    # Remove accidentally duplicated validation checks while preserving order.
    unique_validation_checks = []
    seen_validation_labels = set()

    for check in batch_validation.get("checks", []):
        label = str(check.get("label", "")).strip()

        if label and label not in seen_validation_labels:
            seen_validation_labels.add(label)
            unique_validation_checks.append(check)

    batch_validation["checks"] = unique_validation_checks

    context = {
        "current": "payroll",

        "batch": batch,
        "period": batch.period,
        "branch": batch.branch,

        "register_rows": register_rows,
        "employee_count": len(register_rows),

        "calculated_total_gross": (
            calculated_total_gross
        ),
        "calculated_total_deductions": (
            calculated_total_deductions
        ),
        "calculated_total_net": (
            calculated_total_net
        ),
        "calculated_total_employer_cost": (
            calculated_total_employer_cost
        ),

        "locked_dtr_count": locked_dtr_count,
        "unlocked_dtr_count": unlocked_dtr_count,
        "not_finalized_dtr_count": (
            not_finalized_dtr_count
        ),
        "batch_validation": batch_validation,
        "validation_ready": (
            batch_validation["is_ready"]
        ),
        "validation_errors": (
            batch_validation["errors"]
        ),
        "validation_warnings": (
            batch_validation["warnings"]
        ),
        "validation_checks": (
            batch_validation["checks"]
        ),

        "printed_by": (
            request.user.get_full_name()
            or request.user.username
        ),
        "printed_at": timezone.localtime(
            timezone.now()
        ),
    }

    return render(
        request,
        "admin/payroll_batch_detail.html",
        context,
    )