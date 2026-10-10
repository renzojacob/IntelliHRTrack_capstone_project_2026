"""Direct automatic attendance upload. No new models or migrations."""
import json
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from .models import AttendanceRecord, BiometricDevice, BranchConnector


def authenticate(request):
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    if not token:
        return None
    connector = BranchConnector.objects.select_related("branch").filter(
        token_hash=BranchConnector.hash_token(token), is_active=True,
    ).first()
    if connector is None or not connector.check_token(token):
        return None
    connector.last_seen_at = timezone.now()
    connector.save(update_fields=["last_seen_at", "updated_at"])
    return connector


def answer(data, status=200):
    response = JsonResponse(data, status=status)
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@require_http_methods(["GET"])
def live_config(request):
    connector = authenticate(request)
    if connector is None:
        return answer({"ok": False, "error": "Invalid or inactive token."}, 401)
    devices = list(BiometricDevice.objects.filter(
        branch=connector.branch, is_active=True,
    ).values("id", "name", "ip_address", "port", "username", "password"))
    return answer({"ok": True, "branch_id": connector.branch_id,
                   "branch_name": connector.branch.name, "devices": devices})


def normalize_native(event):
    if not isinstance(event, dict):
        raise ValueError("Event must be an object.")
    try:
        codes = (int(event.get("major")), int(event.get("minor")))
    except (TypeError, ValueError):
        raise ValueError("Invalid event codes.")
    if codes != (5, 153) or str(event.get("currentVerifyMode", "")).lower() != "faceandfp":
        raise ValueError("Requires native 5/153 faceAndFp success.")
    status = {"checkin": AttendanceRecord.STATUS_CHECKIN,
              "checkout": AttendanceRecord.STATUS_CHECKOUT}.get(
                  str(event.get("attendanceStatus") or "").strip().lower(),
                  AttendanceRecord.STATUS_UNKNOWN)
    employee_id = str(event.get("employeeNoString") or event.get("employeeID")
                      or event.get("employeeNo") or event.get("employeeId") or "").strip()
    if not employee_id or len(employee_id) > 64:
        raise ValueError("Invalid employee ID.")
    stamp = parse_datetime(str(event.get("time") or ""))
    if stamp is None or timezone.is_naive(stamp):
        raise ValueError("Requires an event timestamp with a timezone.")
    return employee_id, stamp, status


@csrf_exempt
@never_cache
@require_http_methods(["POST"])
def live_events(request):
    connector = authenticate(request)
    if connector is None:
        return answer({"ok": False, "error": "Invalid or inactive token."}, 401)
    try:
        if len(request.body) > 2_000_000:
            return answer({"ok": False, "error": "Batch is too large."}, 413)
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            raise ValueError("Expected an object.")
        device_id = int(payload["device_id"])
        events = payload["events"]
        if not isinstance(events, list) or not 1 <= len(events) <= 100:
            raise ValueError("Send between 1 and 100 events.")
        validated = [normalize_native(event) for event in events]
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        return answer({"ok": False, "error": str(exc)}, 400)
    created_count = 0
    with transaction.atomic():
        # Serialize automatic uploads for this branch; DB uniqueness also
        # protects against retries and overlap with the existing manual path.
        active = BranchConnector.objects.select_for_update().get(pk=connector.pk)
        if not active.is_active or active.token_hash != connector.token_hash:
            return answer({"ok": False, "error": "Token changed or disabled."}, 401)
        device = BiometricDevice.objects.filter(
            pk=device_id, branch=active.branch, is_active=True,
        ).first()
        if device is None:
            return answer({"ok": False, "error": "Device not active in this branch."}, 403)
        for event, (employee_id, stamp, status) in zip(events, validated):
            _, created = AttendanceRecord.objects.get_or_create(
                employee_id=employee_id, timestamp=stamp,
                attendance_status=status, branch=active.branch,
                defaults={
                    "full_name": str(event.get("name") or event.get("employeeName") or "")[:255],
                    "department": "",
                    "raw_row": {**event, "source_device_id": device.pk,
                                "dual_biometric_verified": True,
                                "verification_methods": ["face", "fingerprint"],
                                "combined_device_event": True,
                                "verification_source": "native_combined_authentication"},
                },
            )
            created_count += int(created)
    return answer({"ok": True, "records_created": created_count,
                   "records_skipped": len(events) - created_count,
                   "records_failed": 0, "received_at": timezone.now().isoformat()})
