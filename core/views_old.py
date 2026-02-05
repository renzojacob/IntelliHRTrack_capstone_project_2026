import csv
import io
from datetime import datetime
from django.shortcuts import render
from django.shortcuts import redirect
from django.contrib import messages


from django.db import IntegrityError
from django.db.models import Count
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from .forms import AttendanceImportForm, AttendanceRecordForm
from .models import AttendanceRecord




# ===== Auth UI pages (UI only for now) =====
def login_ui(request):
    return render(request, "auth/login.html")

def signup_ui(request):
    return render(request, "auth/signup.html")


# ===== Admin UI pages (UI only for now) =====
def admin_dashboard(request):
    return render(request, "admin/dashboard.html")

def admin_analytics(request):
    return render(request, "admin/analytics.html")

def admin_biometrics_attendance(request):
    return render(request, "admin/Biometrics_attendance.html", { 'current': 'biometrics' })


def admin_biometrics_import(request):
    """Handle CSV import form submission for biometrics attendance.

    This is a simple placeholder handler: it accepts POST with a file
    and redirects back to the biometrics page with a success message.
    """
    if request.method == 'POST':
        csv_file = request.FILES.get('csv_file')
        if csv_file:
            # In a real app you'd validate and process the CSV here.
            messages.success(request, f"Received file '{csv_file.name}' — preview processing not implemented.")
        else:
            messages.warning(request, "No file uploaded.")
    return redirect('admin_biometrics')

def admin_employee_management(request):
    return render(request, "admin/employee_management.html")

def admin_leave_approval(request):
    return render(request, "admin/leave_approval.html")

def admin_payroll(request):
    return render(request, "admin/payroll.html")

def admin_reports(request):
    return render(request, "admin/reports.html")

def admin_shift_scheduling(request):
    return render(request, "admin/shift_scheduling.html")

def admin_system_administration(request):
    return render(request, "admin/system_administration.html")









# ===== CRUD for Attendance Records =====
# ---------------------------
# Helpers
# ---------------------------
def _normalize_event_type(value: str) -> str:
    if not value:
        return AttendanceRecord.EVENT_UNKNOWN
    v = value.strip().upper()
    if v in ["IN", "CHECK IN", "TIME IN", "CLOCK IN"]:
        return AttendanceRecord.EVENT_IN
    if v in ["OUT", "CHECK OUT", "TIME OUT", "CLOCK OUT"]:
        return AttendanceRecord.EVENT_OUT
    return AttendanceRecord.EVENT_UNKNOWN


def _parse_timestamp(value: str):
    """
    Accepts common formats.
    Prefer ISO: 2026-02-03 08:30:00 or 2026-02-03T08:30:00
    """
    if not value:
        return None

    value = value.strip()

    # Try Django parser (handles many ISO-ish forms)
    dt = parse_datetime(value)
    if dt:
        return dt

    # Try a few common fallbacks
    for fmt in [
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%Y-%m-%d %H:%M",
    ]:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass

    return None


def _csv_to_rows(file_obj):
    """
    Returns list[dict] from an uploaded CSV.
    """
    raw = file_obj.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # fallback (some exports use ANSI/latin1)
        text = raw.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for r in reader:
        # normalize keys: trim spaces in header names
        rows.append({(k.strip() if k else k): (v.strip() if isinstance(v, str) else v) for k, v in r.items()})
    return rows


def _map_row(row: dict, device_source: str = ""):
    """
    Map whatever CSV columns into our model fields.
    Adjust these mappings to match your actual export headers.
    """
    # Common possible column names from exports
    employee_id = row.get("employee_id") or row.get("Employee ID") or row.get("EmployeeID") or row.get("Person ID") or row.get("ID")
    full_name = row.get("full_name") or row.get("Full Name") or row.get("Name") or row.get("Person Name")

    ts = row.get("timestamp") or row.get("Time") or row.get("Date Time") or row.get("Attendance Time") or row.get("Check Time")
    event_type = row.get("event_type") or row.get("Event Type") or row.get("Status") or row.get("Attendance Status")

    device_name = row.get("device_name") or row.get("Device") or row.get("Device Name") or device_source or ""
    verification_mode = row.get("verification_mode") or row.get("Verification") or row.get("Verify Mode") or row.get("Auth Mode") or ""
    event_code = row.get("event_code") or row.get("Event Code") or row.get("Code") or ""

    dt = _parse_timestamp(ts)

    mapped = {
        "employee_id": (employee_id or "").strip(),
        "full_name": (full_name or "").strip(),
        "timestamp": dt,
        "event_type": _normalize_event_type(event_type or ""),
        "device_name": (device_name or "").strip(),
        "verification_mode": (verification_mode or "").strip(),
        "event_code": (event_code or "").strip(),
        "raw_row": row,
    }
    return mapped


# ---------------------------
# Admin Biometrics Page (GET)
# ---------------------------
def admin_biometrics(request):
    records = AttendanceRecord.objects.all()[:200]

    # KPIs (simple; you can improve later)
    kpi = {
        "present": AttendanceRecord.objects.filter(event_type=AttendanceRecord.EVENT_IN).count(),
        "late": 0,
        "absent": 0,
        "last_sync": AttendanceRecord.objects.order_by("-created_at").values_list("created_at", flat=True).first(),
    }

    context = {
        "current": "biometrics",
        "records": records,
        "kpi": kpi,
        "preview_rows": [],
        "import_errors": "",
        "import_summary": "",
    }
    return render(request, "admin/Biometrics_attendance.html", context)


# ---------------------------
# Import Handler (POST)
# ---------------------------
@require_http_methods(["POST"])
def admin_biometrics_import(request):
    form = AttendanceImportForm(request.POST, request.FILES)
    records = AttendanceRecord.objects.all()[:200]

    context = {
        "current": "biometrics",
        "records": records,
        "kpi": {
            "present": AttendanceRecord.objects.filter(event_type=AttendanceRecord.EVENT_IN).count(),
            "late": 0,
            "absent": 0,
            "last_sync": AttendanceRecord.objects.order_by("-created_at").values_list("created_at", flat=True).first(),
        },
        "preview_rows": [],
        "import_errors": "",
        "import_summary": "",
    }

    if not form.is_valid():
        context["import_errors"] = "Invalid upload. Please choose a CSV file."
        return render(request, "admin/Biometrics_attendance.html", context)

    csv_file = form.cleaned_data["csv_file"]
    skip_duplicates = form.cleaned_data.get("skip_duplicates", True)
    device_source = form.cleaned_data.get("device_source", "")

    # Parse CSV
    try:
        rows = _csv_to_rows(csv_file)
    except Exception as e:
        context["import_errors"] = f"Could not read CSV: {e}"
        return render(request, "admin/Biometrics_attendance.html", context)

    if not rows:
        context["import_errors"] = "CSV is empty."
        return render(request, "admin/Biometrics_attendance.html", context)

    action = request.POST.get("action", "validate")

    # Validate + preview
    preview = []
    errors = []
    valid_mapped = []

    for i, row in enumerate(rows, start=1):
        mapped = _map_row(row, device_source=device_source)

        if not mapped["employee_id"]:
            errors.append(f"Row {i}: missing employee_id (header might be different).")
        if not mapped["timestamp"]:
            errors.append(f"Row {i}: invalid or missing timestamp.")
        status = "valid" if (mapped["employee_id"] and mapped["timestamp"]) else "invalid"

        preview.append({
            "employee_id": mapped["employee_id"] or "—",
            "full_name": mapped["full_name"] or "—",
            "timestamp": mapped["timestamp"] or "—",
            "event_type": mapped["event_type"],
            "device_name": mapped["device_name"] or "—",
            "status": status,
        })

        if status == "valid":
            valid_mapped.append(mapped)

        if len(preview) >= 20:
            break

    context["preview_rows"] = preview

    if action == "validate":
        if errors:
            context["import_errors"] = "\n".join(errors[:10])
        else:
            context["import_summary"] = f"Validation passed. {len(rows)} rows detected. Ready to import."
        return render(request, "admin/Biometrics_attendance.html", context)

    # Import
    created = 0
    skipped = 0
    failed = 0

    # Re-parse full file again because we only previewed first 20 above
    csv_file.seek(0)
    all_rows = _csv_to_rows(csv_file)

    for row in all_rows:
        mapped = _map_row(row, device_source=device_source)

        if not mapped["employee_id"] or not mapped["timestamp"]:
            failed += 1
            continue

        try:
            obj = AttendanceRecord(**mapped)
            obj.save()
            created += 1
        except IntegrityError:
            # duplicate unique constraint
            if skip_duplicates:
                skipped += 1
            else:
                failed += 1
        except Exception:
            failed += 1

    context["import_summary"] = f"Imported: {created} | Skipped duplicates: {skipped} | Failed: {failed}"
    context["records"] = AttendanceRecord.objects.all()[:200]
    return render(request, "admin/Biometrics_attendance.html", context)


# ---------------------------
# CRUD Views
# ---------------------------
def attendance_detail(request, pk):
    obj = get_object_or_404(AttendanceRecord, pk=pk)
    return render(request, "admin/attendance_detail.html", {"current": "biometrics", "obj": obj})


def attendance_create(request):
    if request.method == "POST":
        form = AttendanceRecordForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("admin_biometrics")
    else:
        form = AttendanceRecordForm()

    return render(request, "admin/attendance_form.html", {"current": "biometrics", "form": form, "mode": "create"})


def attendance_update(request, pk):
    obj = get_object_or_404(AttendanceRecord, pk=pk)
    if request.method == "POST":
        form = AttendanceRecordForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            return redirect("admin_biometrics")
    else:
        form = AttendanceRecordForm(instance=obj)

    return render(request, "admin/attendance_form.html", {"current": "biometrics", "form": form, "mode": "edit", "obj": obj})


def attendance_delete(request, pk):
    obj = get_object_or_404(AttendanceRecord, pk=pk)
    if request.method == "POST":
        obj.delete()
        return redirect("admin_biometrics")

    return render(request, "admin/attendance_delete.html", {"current": "biometrics", "obj": obj})