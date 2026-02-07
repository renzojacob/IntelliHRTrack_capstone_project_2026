# core/views.py

import csv
import io
import re
from datetime import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Q  # ✅ NEW
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods, require_POST

from .forms import AttendanceImportForm, AttendanceRecordForm
from .models import AttendanceRecord, UserProfile, Branch


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

            UserProfile.objects.create(
                user=user,
                branch=branch,
                employment_type=employment_type,
                is_approved=False
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


# ✅ NEW helper (used by employee management CRUD)
def _scoped_profiles_for_admin(request):
    """
    Admin visibility rule:
    - superuser: all branches
    - staff admin: only their branch (request.user.profile.branch)
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

    Rule:
    - Superuser sees all branches
    - Staff admin sees only their branch
    """
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    qs = _scoped_profiles_for_admin(request)

    # =========================
    # ✅ CRUD ACTIONS (same page POST)
    # =========================
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()

        # resolve allowed branch
        def _get_allowed_branch_from_post():
            if request.user.is_superuser:
                bid = (request.POST.get("branch_id") or "").strip()
                if not bid:
                    return None
                return Branch.objects.filter(id=bid).first()
            # staff admin: auto branch
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
                        is_approved=True,  # created by admin -> approved
                    )
                messages.success(request, f"Employee created: {username}")
            except Exception as e:
                messages.error(request, f"Create failed: {e}")

            return redirect("admin_employees")

        elif action == "update_employee":
            profile_id = request.POST.get("profile_id")
            prof = get_object_or_404(UserProfile.objects.select_related("user", "branch"), id=profile_id)

            # staff admin can only edit same branch
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

            # superuser can change branch
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
            prof = get_object_or_404(UserProfile.objects.select_related("user", "branch"), id=profile_id)

            # staff admin can only delete same branch
            if not request.user.is_superuser:
                try:
                    if prof.branch != request.user.profile.branch:
                        messages.error(request, "You can only delete employees in your branch.")
                        return redirect("admin_employees")
                except UserProfile.DoesNotExist:
                    messages.error(request, "Admin profile missing.")
                    return redirect("admin_employees")

            username = prof.user.username
            prof.user.delete()  # cascades profile
            messages.success(request, f"Deleted employee: {username}")
            return redirect("admin_employees")

        # approve/reject are handled by separate endpoints, not here
        else:
            messages.error(request, "Invalid action.")
            return redirect("admin_employees")

    # =========================
    # Page data (GET)
    # =========================
    pending_profiles = qs.filter(is_approved=False)
    approved_profiles = qs.filter(is_approved=True)

    # ✅ This is the table you want (separate CRUD table)
    # You can include pending too if you want, but usually manage employees = approved only
    employee_profiles = qs.filter(user__is_staff=False, user__is_superuser=False).order_by("user__username")

    branches = Branch.objects.all().order_by("name")  # needed for superuser create/edit

    return render(
        request,
        "admin/employee_management.html",
        {
            "current": "employees",
            "pending_profiles": pending_profiles,
            "approved_profiles": approved_profiles,

            # ✅ NEW context for the CRUD table
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

    # staff can only approve same branch
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
    """
    Reject = delete the user (profile cascades)
    Only superuser or staff admin of same branch can reject.
    """
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    prof = get_object_or_404(UserProfile, id=profile_id)

    # staff can only reject same branch
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


@login_required
def admin_leave_approval(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")
    return render(request, "admin/leave_approval.html", {"current": "leave"})


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
# Employee UI pages
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
    return render(request, "employee/leave.html", {"current": "leave"})


@login_required
def employee_payroll(request):
    return render(request, "employee/payroll.html", {"current": "payroll"})


@login_required
def employee_analytics(request):
    return render(request, "employee/analytics.html", {"current": "analytics"})


@login_required
def employee_notifications(request):
    # NOTE: your template name earlier was "employee/notification.html"
    return render(request, "employee/notification.html", {"current": "notification"})


@login_required
def employee_profile(request):
    # your file is named: setting_&_profile.html
    return render(request, "employee/setting_&_profile.html", {"current": "profile"})


# =========================
# Helpers (Normalization)
# =========================
def _norm_key(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\ufeff", "")  # BOM
    s = s.replace("\xa0", " ")  # NBSP
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


# =========================
# Status + Timestamp parsing
# =========================
def _normalize_status(value: str) -> str:
    if not value:
        return AttendanceRecord.STATUS_UNKNOWN

    v = str(value).strip().upper()

    checkin = {"IN", "CHECKIN", "CHECK-IN", "CHECK IN", "TIME IN", "CLOCK IN", "ENTRY"}
    checkout = {
        "OUT",
        "CHECKOUT",
        "CHECK-OUT",
        "CHECK OUT",
        "TIME OUT",
        "CLOCK OUT",
        "EXIT",
    }
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


def _parse_timestamp(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    s = str(value).strip()

    dt = parse_datetime(s)
    if dt:
        return dt

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
            return datetime.strptime(s, f)
        except ValueError:
            continue

    return None


def _status_label(value: str) -> str:
    return dict(AttendanceRecord.ATTENDANCE_STATUS_CHOICES).get(value, value)


# =========================
# File readers
# =========================
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
    """
    Detail1 sometimes contains title rows above headers.
    We search for a row containing 'Person ID' (case-insensitive).
    """
    for i in range(len(df.index)):
        row_vals = [str(x).strip().lower() for x in df.iloc[i].tolist()]
        if "person id" in row_vals:
            return i
    return None


def _read_excel(file_obj, filename: str):
    """
    Handles:
    - real XLSX/XLS
    - Hikvision HTML-as-XLS:
        Detail1 = header table
        Detail2 = data table (may be split into multiple tables/pages)
    """
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("Install deps: pip install pandas openpyxl xlrd==2.0.1 lxml")

    file_obj.seek(0)
    raw = file_obj.read()
    head = raw[:500].lstrip().lower()

    # --- HTML-as-XLS detection ---
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

        # Try class-based parse: Detail1 headers + ALL Detail2 tables concatenated
        try:
            header_tables = pd.read_html(
                io.StringIO(text), attrs={"class": "Detail1"}, header=None
            )
            data_tables = pd.read_html(
                io.StringIO(text), attrs={"class": "Detail2"}, header=None
            )

            if header_tables and data_tables:
                df_h = header_tables[0].fillna("")
                header_row_idx = _find_header_row_in_df(df_h)
                if header_row_idx is None:
                    raise ValueError("Could not find header row in Detail1 table.")

                headers = [str(x).strip() for x in df_h.iloc[header_row_idx].tolist()]
                headers = [h if h else f"col_{i}" for i, h in enumerate(headers)]

                # concat ALL Detail2 tables
                df_d = pd.concat([d.fillna("") for d in data_tables], ignore_index=True)

                headers = headers[: len(df_d.columns)]
                df_d.columns = headers

                first_col = headers[0] if headers else None
                if first_col:
                    df_d = df_d[
                        df_d[first_col].astype(str).str.strip().str.lower() != "person id"
                    ]

                df_d = df_d.replace("", None).dropna(how="all").fillna("")
                return _df_to_rows(df_d)
        except Exception:
            pass

        # fallback: parse all tables and choose a large data-like table
        try:
            tables = pd.read_html(io.StringIO(text), header=None)
            if not tables:
                raise ValueError("No tables found in HTML file.")

            candidates = [t for t in tables if t.shape[1] >= 5]
            df_best = (
                max(candidates, key=lambda d: d.shape[0])
                if candidates
                else max(tables, key=lambda d: d.shape[0])
            )

            row0 = [str(x).strip().lower() for x in df_best.iloc[0].tolist()]
            if "person id" in row0 and "time" in row0:
                df_best.columns = [str(x).strip() for x in df_best.iloc[0].tolist()]
                df_best = df_best.iloc[1:].reset_index(drop=True)

            df_best = df_best.fillna("")
            return _df_to_rows(df_best)
        except Exception as e:
            raise ValueError(f"HTML-as-Excel detected but failed to parse tables: {e}")

    # --- Real Excel (.xls/.xlsx) ---
    ext = filename.lower().split(".")[-1]
    engine = "openpyxl" if ext == "xlsx" else "xlrd" if ext == "xls" else None

    try:
        import pandas as pd
        import io as _io

        df = pd.read_excel(_io.BytesIO(raw), sheet_name=0, engine=engine)
        return _df_to_rows(df)
    except Exception as e:
        raise ValueError(f"Error reading Excel file: {e}")


# =========================
# Row mapping (robust headers)
# =========================
def _map_row(row: dict, branch: str) -> dict:
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

    status_raw = (
        r.get("attendance status") or r.get("status") or r.get("event type") or ""
    ).strip()
    status = _normalize_status(status_raw)

    return {
        "employee_id": employee_id,
        "full_name": full_name,
        "department": department,
        "branch": (branch or "").strip(),
        "timestamp": ts,
        "attendance_status": status,
        "raw_row": row,
    }


# =========================
# Session cache helpers
# =========================
SESSION_KEY = "attendance_import_cache_v1"


def _save_import_cache(request, branch: str, skip_duplicates: bool, rows: list):
    cached = {
        "branch": branch,
        "skip_duplicates": bool(skip_duplicates),
        "rows": [],
    }

    for row in rows:
        mapped = _map_row(row, branch=branch)
        cached["rows"].append(
            {
                "employee_id": mapped["employee_id"],
                "full_name": mapped["full_name"],
                "department": mapped["department"],
                "branch": mapped["branch"],
                "timestamp": mapped["timestamp"].isoformat(sep=" ")
                if mapped["timestamp"]
                else "",
                "attendance_status": mapped["attendance_status"],
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

    records = AttendanceRecord.objects.all().order_by("-timestamp")[:100]

    kpi = {
        "present": AttendanceRecord.objects.filter(
            attendance_status=AttendanceRecord.STATUS_CHECKIN
        ).count(),
        "late": 0,
        "absent": 0,
        "last_sync": AttendanceRecord.objects.order_by("-created_at")
        .values_list("created_at", flat=True)
        .first(),
    }

    cache = _load_import_cache(request)

    context = {
        "current": "biometrics",
        "records": records,
        "kpi": kpi,
        "preview_rows": [],
        "import_errors": [],
        "import_summary": "",
        "branches": Branch.objects.all().order_by("name").values_list("id", "name"),
        "can_import": bool(cache and cache.get("rows")),
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

    form = AttendanceImportForm(request.POST, request.FILES)

    records = AttendanceRecord.objects.all().order_by("-timestamp")[:100]
    kpi = {
        "present": AttendanceRecord.objects.filter(
            attendance_status=AttendanceRecord.STATUS_CHECKIN
        ).count(),
        "late": 0,
        "absent": 0,
        "last_sync": AttendanceRecord.objects.order_by("-created_at")
        .values_list("created_at", flat=True)
        .first(),
    }

    context = {
        "current": "biometrics",
        "records": records,
        "kpi": kpi,
        "preview_rows": [],
        "import_errors": [],
        "import_summary": "",
        "branches": Branch.objects.all().order_by("name").values_list("id", "name"),
        "can_import": False,
    }

    action = request.POST.get("action", "validate")

    if not form.is_valid():
        context["import_errors"] = []
        for field, errs in form.errors.items():
            for e in errs:
                context["import_errors"].append(f"{field}: {e}")
        if not context["import_errors"]:
            context["import_errors"] = [
                "File upload failed. Please choose a valid CSV/Excel file."
            ]
        return render(request, "admin/Biometrics_attendance.html", context)

    upload = form.cleaned_data.get("file") or form.cleaned_data.get("csv_file")
    skip_duplicates = form.cleaned_data.get("skip_duplicates", True)

    branch_obj = form.cleaned_data.get("branch")
    branch_name = ""

    if hasattr(branch_obj, "name"):
        branch_name = branch_obj.name
    else:
        branch_id = str(branch_obj or "").strip()
        if branch_id:
            b = Branch.objects.filter(id=branch_id).only("name").first()
            branch_name = b.name if b else ""

    if not branch_name:
        context["import_errors"] = ["Invalid branch selected. Please choose a branch."]
        return render(request, "admin/Biometrics_attendance.html", context)

    rows = None

    if action == "import" and not upload:
        cache = _load_import_cache(request)
        if not cache or not cache.get("rows"):
            context["import_errors"] = [
                "No validated data found. Please upload and Validate first."
            ]
            return render(request, "admin/Biometrics_attendance.html", context)

        branch_name = cache.get("branch") or branch_name
        skip_duplicates = cache.get("skip_duplicates", skip_duplicates)
        rows = cache["rows"]
    else:
        if not upload:
            context["import_errors"] = ["No file received. Please select a file to upload."]
            return render(request, "admin/Biometrics_attendance.html", context)

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
            return render(request, "admin/Biometrics_attendance.html", context)

        if not rows:
            context["import_errors"] = ["File is empty or has no data rows."]
            return render(request, "admin/Biometrics_attendance.html", context)

    preview = []
    validation_errors = []

    is_cached_mapped = (
        rows
        and isinstance(rows[0], dict)
        and "employee_id" in rows[0]
        and "timestamp" in rows[0]
        and "attendance_status" in rows[0]
        and (action == "import" and not upload)
    )

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
                    "branch": (r.get("branch") or branch_name or "").strip() or "—",
                    "timestamp": ts or "—",
                    "attendance_status": _status_label(r.get("attendance_status")),
                    "status": "valid" if is_valid else "invalid",
                    "errors": ", ".join(row_errors) if row_errors else "",
                }
            )
    else:
        meaningful = 0
        for idx, row in enumerate(rows, start=2):
            mapped = _map_row(row, branch=branch_name)

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
                        "branch": mapped["branch"] or "—",
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
            context["import_summary"] = (
                f"Validation detected {len(validation_errors)} error(s). Fix and try again."
            )
            _clear_import_cache(request)
            context["can_import"] = False
            return render(request, "admin/Biometrics_attendance.html", context)

        _save_import_cache(request, branch=branch_name, skip_duplicates=skip_duplicates, rows=rows)
        context["import_summary"] = f"✓ Validation passed! {len(rows)} row(s) ready to import."
        context["can_import"] = True
        return render(request, "admin/Biometrics_attendance.html", context)

    if action != "import":
        context["import_errors"] = ["Invalid action."]
        return render(request, "admin/Biometrics_attendance.html", context)

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

                    mapped = {
                        "employee_id": employee_id,
                        "full_name": (r.get("full_name") or "").strip(),
                        "department": (r.get("department") or "").strip(),
                        "branch": (r.get("branch") or branch_name or "").strip(),
                        "timestamp": ts,
                        "attendance_status": r.get("attendance_status")
                        or AttendanceRecord.STATUS_UNKNOWN,
                        "raw_row": r,
                    }

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
            else:
                for idx, row in enumerate(rows, start=2):
                    mapped = _map_row(row, branch=branch_name)

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
        return render(request, "admin/Biometrics_attendance.html", context)

    _clear_import_cache(request)

    context["import_summary"] = (
        f"✓ Import complete: {created} created | {skipped} skipped | {failed} failed"
    )
    if import_errors:
        context["import_errors"] = import_errors[:10]

    context["records"] = AttendanceRecord.objects.all().order_by("-timestamp")[:100]
    context["kpi"]["present"] = AttendanceRecord.objects.filter(
        attendance_status=AttendanceRecord.STATUS_CHECKIN
    ).count()
    context["kpi"]["last_sync"] = AttendanceRecord.objects.order_by("-created_at").values_list(
        "created_at", flat=True
    ).first()
    context["can_import"] = False

    return render(request, "admin/Biometrics_attendance.html", context)


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

    records = AttendanceRecord.objects.all().order_by("-timestamp")

    employee_id = request.GET.get("employee_id", "").strip()
    branch = request.GET.get("branch", "").strip()

    if employee_id:
        records = records.filter(employee_id__icontains=employee_id)
    if branch:
        records = records.filter(branch=branch)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="attendance_export.csv"'

    writer = csv.writer(response)
    writer.writerow(
        [
            "Person ID",
            "Name",
            "Department",
            "Branch",
            "Time",
            "Attendance Status",
            "Created At",
        ]
    )

    for rec in records:
        writer.writerow(
            [
                rec.employee_id,
                rec.full_name,
                rec.department,
                rec.branch,
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

    records = AttendanceRecord.objects.all().order_by("-timestamp")

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

    obj = get_object_or_404(AttendanceRecord, pk=pk)
    return render(
        request, "admin/attendance_detail.html", {"current": "biometrics", "obj": obj}
    )


@login_required
def attendance_create(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("login_ui")

    if request.method == "POST":
        form = AttendanceRecordForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Record created successfully!")
            return redirect("admin_biometrics")
    else:
        form = AttendanceRecordForm()

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

    obj = get_object_or_404(AttendanceRecord, pk=pk)

    if request.method == "POST":
        form = AttendanceRecordForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Record updated successfully!")
            return redirect("admin_biometrics")
    else:
        form = AttendanceRecordForm(instance=obj)

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

    obj = get_object_or_404(AttendanceRecord, pk=pk)

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
