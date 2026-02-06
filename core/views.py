import csv
import io
import re
from datetime import datetime

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_http_methods

from .forms import AttendanceImportForm, AttendanceRecordForm, BRANCH_CHOICES
from .models import AttendanceRecord


# =========================
# Auth UI pages
# =========================
def login_ui(request):
    return render(request, "auth/login.html")


def signup_ui(request):
    return render(request, "auth/signup.html")


# =========================
# Admin Dashboard UI pages
# =========================
def admin_dashboard(request):
    return render(request, "admin/dashboard.html", {"current": "dashboard"})


def admin_analytics(request):
    return render(request, "admin/analytics.html", {"current": "analytics"})


def admin_employee_management(request):
    return render(request, "admin/employee_management.html", {"current": "employees"})


def admin_leave_approval(request):
    return render(request, "admin/leave_approval.html", {"current": "leave"})


def admin_payroll(request):
    return render(request, "admin/payroll.html", {"current": "payroll"})


def admin_reports(request):
    return render(request, "admin/reports.html", {"current": "reports"})


def admin_shift_scheduling(request):
    return render(request, "admin/shift_scheduling.html", {"current": "scheduling"})


def admin_system_administration(request):
    return render(request, "admin/system_administration.html", {"current": "system"})


# =========================
# Helpers (Normalization)
# =========================
def _norm_key(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\ufeff", "")  # BOM
    s = s.replace("\xa0", " ")   # NBSP
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
    checkout = {"OUT", "CHECKOUT", "CHECK-OUT", "CHECK OUT", "TIME OUT", "CLOCK OUT", "EXIT"}
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


def _read_excel(file_obj, filename: str):
    """
    Handles:
    - real XLSX/XLS
    - Hikvision HTML-as-XLS:
        Detail1 = header table
        Detail2 = data table (no headers)
    """
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("Install deps: pip install pandas openpyxl xlrd==2.0.1 lxml")

    file_obj.seek(0)
    raw = file_obj.read()
    head = raw[:500].lstrip().lower()

    def df_to_rows(df):
        df = df.fillna("")
        rows = []
        for _, r in df.iterrows():
            row = {}
            for k in df.columns:
                row[str(k).strip()] = str(r[k]).strip() if str(r[k]).strip() else ""
            if any(str(v).strip() for v in row.values()):
                rows.append(row)
        return rows

    # --- HTML-as-XLS detection ---
    if head.startswith(b"<html") or head.startswith(b"<!doctype") or head.startswith(b"<table") or b"<html" in head:
        text = None
        for enc in ["utf-8-sig", "utf-8", "cp1252", "latin-1"]:
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("File looks like HTML but could not decode it.")

        # ✅ BEST PATH: read Hikvision sections by class
        try:
            header_tables = pd.read_html(io.StringIO(text), attrs={"class": "Detail1"}, header=None)
            data_tables = pd.read_html(io.StringIO(text), attrs={"class": "Detail2"}, header=None)

            if header_tables and data_tables:
                df_h = header_tables[0].fillna("")
                df_d = data_tables[0].fillna("")

                # Find the header row inside Detail1 (the row containing "Person ID")
                header_row_idx = None
                for i in range(len(df_h.index)):
                    row_vals = [str(x).strip().lower() for x in df_h.iloc[i].tolist()]
                    if "person id" in row_vals:
                        header_row_idx = i
                        break

                if header_row_idx is None:
                    raise ValueError("Could not find header row in Detail1 table.")

                headers = [str(x).strip() for x in df_h.iloc[header_row_idx].tolist()]
                # Trim to actual columns count in data table
                headers = headers[: len(df_d.columns)]

                df_d.columns = headers

                # Drop rows that are completely empty
                df_d = df_d.replace("", None)
                df_d = df_d.dropna(how="all")
                df_d = df_d.fillna("")

                return df_to_rows(df_d)

        except Exception:
            # If class-based parsing fails, fallback below.
            pass

        # --- Fallback: pick the best table with required columns ---
        try:
            tables = pd.read_html(io.StringIO(text))
            if not tables:
                raise ValueError("No tables found in HTML file.")

            required = {"person id", "name", "department", "time", "attendance status"}
            best_df = None
            best_score = -1

            for df in tables:
                cols = {_norm_key(c) for c in df.columns}
                score = len(required.intersection(cols))
                if score > best_score:
                    best_score = score
                    best_df = df

            if best_df is None:
                best_df = max(tables, key=lambda d: len(d.index))

            return df_to_rows(best_df)

        except Exception as e:
            raise ValueError(f"HTML-as-Excel detected but failed to parse tables: {e}")

    # --- Real Excel (.xls/.xlsx) ---
    ext = filename.lower().split(".")[-1]
    engine = "openpyxl" if ext == "xlsx" else "xlrd" if ext == "xls" else None

    try:
        import io as _io
        df = pd.read_excel(_io.BytesIO(raw), sheet_name=0, engine=engine)
        return df_to_rows(df)
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

    status_raw = (r.get("attendance status") or r.get("status") or r.get("event type") or "").strip()
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
# Session cache helpers (so Import works after Validate)
# =========================
SESSION_KEY = "attendance_import_cache_v1"


def _save_import_cache(request, branch: str, skip_duplicates: bool, rows: list):
    """
    Save parsed rows to session so user can click Import without re-uploading
    (because browsers clear file inputs after POST).
    """
    cached = {
        "branch": branch,
        "skip_duplicates": bool(skip_duplicates),
        "rows": [],
    }
    for row in rows:
        mapped = _map_row(row, branch=branch)
        # store only serializable fields
        cached["rows"].append({
            "employee_id": mapped["employee_id"],
            "full_name": mapped["full_name"],
            "department": mapped["department"],
            "branch": mapped["branch"],
            "timestamp": mapped["timestamp"].isoformat(sep=" ") if mapped["timestamp"] else "",
            "attendance_status": mapped["attendance_status"],
        })

    request.session[SESSION_KEY] = cached
    request.session.modified = True


def _load_import_cache(request):
    return request.session.get(SESSION_KEY)


def _clear_import_cache(request):
    if SESSION_KEY in request.session:
        del request.session[SESSION_KEY]
        request.session.modified = True


# =========================
# Main page
# =========================
def admin_biometrics_attendance(request):
    records = AttendanceRecord.objects.all().order_by("-timestamp")[:100]

    kpi = {
        "present": AttendanceRecord.objects.filter(attendance_status=AttendanceRecord.STATUS_CHECKIN).count(),
        "late": 0,
        "absent": 0,
        "last_sync": AttendanceRecord.objects.order_by("-created_at").values_list("created_at", flat=True).first(),
    }

    cache = _load_import_cache(request)

    context = {
        "current": "biometrics",
        "records": records,
        "kpi": kpi,
        "preview_rows": [],
        "import_errors": [],
        "import_summary": "",
        "branches": BRANCH_CHOICES,
        "can_import": bool(cache and cache.get("rows")),
    }
    return render(request, "admin/Biometrics_attendance.html", context)


@require_http_methods(["POST"])
def admin_biometrics_import(request):
    form = AttendanceImportForm(request.POST, request.FILES)

    records = AttendanceRecord.objects.all().order_by("-timestamp")[:100]
    kpi = {
        "present": AttendanceRecord.objects.filter(attendance_status=AttendanceRecord.STATUS_CHECKIN).count(),
        "late": 0,
        "absent": 0,
        "last_sync": AttendanceRecord.objects.order_by("-created_at").values_list("created_at", flat=True).first(),
    }

    context = {
        "current": "biometrics",
        "records": records,
        "kpi": kpi,
        "preview_rows": [],
        "import_errors": [],
        "import_summary": "",
        "branches": BRANCH_CHOICES,
        "can_import": False,
    }

    action = request.POST.get("action", "validate")

    if not form.is_valid():
        context["import_errors"] = []
        for field, errs in form.errors.items():
            for e in errs:
                context["import_errors"].append(f"{field}: {e}")
        if not context["import_errors"]:
            context["import_errors"] = ["File upload failed. Please choose a valid CSV/Excel file."]
        return render(request, "admin/Biometrics_attendance.html", context)

    upload = form.cleaned_data.get("file") or form.cleaned_data.get("csv_file")
    skip_duplicates = form.cleaned_data.get("skip_duplicates", True)
    branch = (form.cleaned_data.get("branch") or "").strip()

    # =========================
    # READ rows (from upload or from session cache)
    # =========================
    rows = None

    # If importing and upload is missing, use session cache (THIS FIXES YOUR PROBLEM)
    if action == "import" and not upload:
        cache = _load_import_cache(request)
        if not cache or not cache.get("rows"):
            context["import_errors"] = ["No validated data found. Please upload and Validate first."]
            return render(request, "admin/Biometrics_attendance.html", context)

        # restore from cache
        branch = cache.get("branch") or branch
        skip_duplicates = cache.get("skip_duplicates", skip_duplicates)
        rows = cache["rows"]  # already mapped/serializable
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

    # =========================
    # VALIDATE + PREVIEW
    # =========================
    preview = []
    validation_errors = []

    # If rows came from session cache, they are already mapped dicts (employee_id, timestamp string, etc.)
    if rows and isinstance(rows[0], dict) and "timestamp" in rows[0] and "employee_id" in rows[0] and "attendance_status" in rows[0] and action == "import" and not upload:
        # build a preview from cached rows (first 20)
        for i, r in enumerate(rows[:20], start=2):
            ts = _parse_timestamp(r.get("timestamp", ""))
            row_errors = []
            if not (r.get("employee_id") or "").strip():
                row_errors.append("Missing Person ID")
            if not ts:
                row_errors.append("Invalid/missing Time")

            is_valid = len(row_errors) == 0
            preview.append({
                "employee_id": r.get("employee_id") or "—",
                "full_name": r.get("full_name") or "—",
                "department": r.get("department") or "—",
                "branch": r.get("branch") or branch or "—",
                "timestamp": ts or "—",
                "attendance_status": _status_label(r.get("attendance_status")),
                "status": "valid" if is_valid else "invalid",
                "errors": ", ".join(row_errors) if row_errors else "",
            })
    else:
        # normal preview from parsed file rows
        meaningful = 0
        for idx, row in enumerate(rows, start=2):
            mapped = _map_row(row, branch=branch)

            # skip junk empty rows
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
                preview.append({
                    "employee_id": mapped["employee_id"] or "—",
                    "full_name": mapped["full_name"] or "—",
                    "department": mapped["department"] or "—",
                    "branch": mapped["branch"] or "—",
                    "timestamp": mapped["timestamp"] or "—",
                    "attendance_status": _status_label(mapped["attendance_status"]),
                    "status": "valid" if is_valid else "invalid",
                    "errors": ", ".join(row_errors) if row_errors else "",
                })
                meaningful += 1

            if not is_valid:
                validation_errors.append(f"Row {idx}: {', '.join(row_errors)}")

            if meaningful >= 20:
                break

    context["preview_rows"] = preview

    # =========================
    # ACTION: VALIDATE
    # =========================
    if action == "validate":
        if validation_errors:
            context["import_errors"] = validation_errors[:10]
            context["import_summary"] = f"Validation detected {len(validation_errors)} error(s). Fix and try again."
            _clear_import_cache(request)
            context["can_import"] = False
            return render(request, "admin/Biometrics_attendance.html", context)

        # ✅ Save rows to session cache so Import works even without re-upload
        _save_import_cache(request, branch=branch, skip_duplicates=skip_duplicates, rows=rows)

        context["import_summary"] = f"✓ Validation passed! {len(rows)} row(s) ready to import."
        context["can_import"] = True
        return render(request, "admin/Biometrics_attendance.html", context)

    # =========================
    # ACTION: IMPORT
    # =========================
    if action != "import":
        context["import_errors"] = ["Invalid action."]
        return render(request, "admin/Biometrics_attendance.html", context)

    created = 0
    skipped = 0
    failed = 0
    import_errors = []

    try:
        with transaction.atomic():
            # If rows are cached-mapped:
            if rows and isinstance(rows[0], dict) and "timestamp" in rows[0] and "employee_id" in rows[0] and "attendance_status" in rows[0] and (not upload):
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
                        "branch": (r.get("branch") or branch or "").strip(),
                        "timestamp": ts,
                        "attendance_status": r.get("attendance_status") or AttendanceRecord.STATUS_UNKNOWN,
                        "raw_row": r,  # keep something for debugging
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
                # Import from freshly parsed file rows
                for idx, row in enumerate(rows, start=2):
                    mapped = _map_row(row, branch=branch)

                    # skip junk empty rows
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

    # ✅ Clear cache after import so you don’t accidentally re-import old rows
    _clear_import_cache(request)

    context["import_summary"] = f"✓ Import complete: {created} created | {skipped} skipped | {failed} failed"
    if import_errors:
        context["import_errors"] = import_errors[:10]

    context["records"] = AttendanceRecord.objects.all().order_by("-timestamp")[:100]
    context["kpi"]["present"] = AttendanceRecord.objects.filter(
        attendance_status=AttendanceRecord.STATUS_CHECKIN
    ).count()
    context["kpi"]["last_sync"] = AttendanceRecord.objects.order_by("-created_at").values_list("created_at", flat=True).first()
    context["can_import"] = False

    return render(request, "admin/Biometrics_attendance.html", context)


# =========================
# Export endpoints
# =========================
def admin_biometrics_template(request):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="attendance_template.csv"'
    writer = csv.writer(response)
    writer.writerow(["Person ID", "Name", "Department", "Time", "Attendance Status"])
    writer.writerow(["1", "Juan Dela Cruz", "Kitchen", "2026-02-04 08:00:00", "Check-in"])
    return response


def admin_biometrics_export(request):
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
    writer.writerow(["Person ID", "Name", "Department", "Branch", "Time", "Attendance Status", "Created At"])

    for rec in records:
        writer.writerow([
            rec.employee_id,
            rec.full_name,
            rec.department,
            rec.branch,
            rec.timestamp.strftime("%Y-%m-%d %H:%M:%S") if rec.timestamp else "",
            _status_label(rec.attendance_status),
            rec.created_at.strftime("%Y-%m-%d %H:%M:%S") if rec.created_at else "",
        ])

    return response


# =========================
# CRUD Operations
# =========================
def attendance_list(request):
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


def attendance_detail(request, pk):
    obj = get_object_or_404(AttendanceRecord, pk=pk)
    return render(request, "admin/attendance_detail.html", {"current": "biometrics", "obj": obj})


def attendance_create(request):
    if request.method == "POST":
        form = AttendanceRecordForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Record created successfully!")
            return redirect("admin_biometrics")
    else:
        form = AttendanceRecordForm()

    return render(request, "admin/attendance_form.html", {
        "current": "biometrics",
        "form": form,
        "mode": "create",
        "title": "Create Attendance Record",
    })


def attendance_update(request, pk):
    obj = get_object_or_404(AttendanceRecord, pk=pk)

    if request.method == "POST":
        form = AttendanceRecordForm(request.POST, instance=obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Record updated successfully!")
            return redirect("admin_biometrics")
    else:
        form = AttendanceRecordForm(instance=obj)

    return render(request, "admin/attendance_form.html", {
        "current": "biometrics",
        "form": form,
        "mode": "edit",
        "obj": obj,
        "title": f"Edit Record: {obj.employee_id}",
    })


def attendance_delete(request, pk):
    obj = get_object_or_404(AttendanceRecord, pk=pk)

    if request.method == "POST":
        obj.delete()
        messages.success(request, "Record deleted successfully!")
        return redirect("admin_biometrics")

    return render(request, "admin/attendance_delete.html", {
        "current": "biometrics",
        "obj": obj,
    })
