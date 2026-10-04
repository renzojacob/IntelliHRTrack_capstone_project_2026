import json
from datetime import datetime, timedelta

import requests
from requests.auth import HTTPDigestAuth
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import AttendanceRecord


# Hikvision MAJOR_EVENT (5) successful authentication minor codes.
# Values are decimal forms of the SDK constants:
#   0x26 -> fingerprint comparison passed
#   0x36 -> face + fingerprint verification passed
#   0x4B -> face verification passed
HIKVISION_MINOR_FINGERPRINT_PASS = 38
HIKVISION_MINOR_FACE_AND_FINGERPRINT_PASS = 54
HIKVISION_MINOR_FACE_PASS = 75

# Separate face and fingerprint events must occur close together to form one
# valid attendance punch. The later timestamp is used because attendance is
# complete only after both factors have succeeded.
DUAL_BIOMETRIC_PAIR_WINDOW = timedelta(minutes=2)


# =========================================================
# HIKVISION SYNC HELPERS
# =========================================================

def _parse_timestamp(value):
    """
    Converts Hikvision event time into timezone-aware datetime.

    Example Hikvision time:
    2026-05-06T08:03:00+08:00
    2026-05-06T08:03:00
    2026-05-06 08:03:00
    """
    if not value:
        return None

    value = str(value).strip()

    dt = parse_datetime(value)
    if dt:
        if timezone.is_aware(dt):
            return dt
        return timezone.make_aware(dt, timezone.get_current_timezone())

    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
            return timezone.make_aware(parsed, timezone.get_current_timezone())
        except ValueError:
            continue

    return None


def _clean_text(value):
    return str(value or "").strip()


def _normalize_for_status(value):
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "")
        .replace("_", "")
        .replace(" ", "")
    )


def _get_event_status_texts(event):
    """
    Hikvision can send different fields depending on device/firmware.
    We collect all possible fields and evaluate them carefully.
    """
    fields = [
        event.get("attendanceStatus"),
        event.get("label"),
        event.get("minorEventType"),
        event.get("eventType"),
        event.get("eventName"),
        event.get("subEventType"),
        event.get("status"),
    ]

    return [_normalize_for_status(v) for v in fields if str(v or "").strip()]


def _normalize_attendance_status(event):
    """
    Correct status mapping.

    IMPORTANT:
    Check Out must be saved as AttendanceRecord.STATUS_CHECKOUT.
    Your previous code temporarily saved Check Out as CHECK_IN.
    """

    texts = _get_event_status_texts(event)

    # Join for fallback checking
    joined = " ".join(texts)

    # Strong check-out patterns first.
    checkout_patterns = {
        "checkout",
        "timeout",
        "clockout",
        "out",
        "exit",
        "checkouter",
    }

    checkin_patterns = {
        "checkin",
        "timein",
        "clockin",
        "in",
        "entry",
    }

    for text in texts:
        if text in checkout_patterns:
            return AttendanceRecord.STATUS_CHECKOUT

    for text in texts:
        if text in checkin_patterns:
            return AttendanceRecord.STATUS_CHECKIN

    # Fallback partial matching.
    # Check OUT first because "checkout" contains "check".
    if "checkout" in joined or "timeout" in joined or "clockout" in joined:
        return AttendanceRecord.STATUS_CHECKOUT

    if "checkin" in joined or "timein" in joined or "clockin" in joined:
        return AttendanceRecord.STATUS_CHECKIN

    # Avoid treating random words containing "in" as Check In.
    # Only use these if the whole normalized text is exactly "in" or "out".
    if "out" in texts:
        return AttendanceRecord.STATUS_CHECKOUT

    if "in" in texts:
        return AttendanceRecord.STATUS_CHECKIN

    return AttendanceRecord.STATUS_UNKNOWN


def _search_events(device, payload):
    """
    Calls Hikvision ISAPI AcsEvent endpoint.
    """
    url = f"http://{device.ip_address}:{device.port}/ISAPI/AccessControl/AcsEvent?format=json"

    response = requests.post(
        url,
        json=payload,
        auth=HTTPDigestAuth(device.username, device.password),
        timeout=20,
    )

    if response.status_code != 200:
        print("========== HIKVISION ERROR RESPONSE ==========")
        print("DEVICE:", device.name, device.ip_address)
        print("STATUS:", response.status_code)
        print("BODY:", response.text)
        print("=============================================")
        return {}

    try:
        return response.json()
    except Exception:
        print("========== HIKVISION INVALID JSON ==========")
        print(response.text)
        print("===========================================")
        return {}


def _extract_events(data):
    return data.get("AcsEvent", {}).get("InfoList", [])


def _pick_person_events(events):
    """
    Keeps only events with employee/card identity.
    """
    picked = []

    for e in events:
        has_identity = any([
            e.get("employeeNoString"),
            e.get("employeeID"),
            e.get("employeeNo"),
            e.get("employeeId"),
            e.get("name"),
            e.get("employeeName"),
            e.get("cardNo"),
        ])

        if has_identity:
            picked.append(e)

    return picked


def _make_start_time(device):
    """
    Uses the latest attendance record from the device's branch.

    Records from another branch must not affect the synchronization
    starting time of this device.
    """

    last = (
        AttendanceRecord.objects
        .filter(branch=device.branch)
        .order_by("-timestamp")
        .first()
    )

    if last and last.timestamp:
        start = last.timestamp

        if timezone.is_aware(start):
            start = timezone.localtime(start)
        else:
            start = timezone.make_aware(
                start,
                timezone.get_current_timezone(),
            )

        return start - timedelta(minutes=2)

    return timezone.localtime(timezone.now()) - timedelta(days=3)


def _fetch_all_pages(device, start, end, major):
    """
    Fetch paginated Hikvision events.
    """
    all_events = []
    position = 0
    page_size = 30

    while True:
        payload = {
            "AcsEventCond": {
                "searchID": f"{device.id}-{major}-{position}",
                "searchResultPosition": position,
                "maxResults": page_size,
                "major": major,
                "minor": 0,
                "startTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "endTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        }

        data = _search_events(device, payload)
        events = _extract_events(data)

        if events:
            all_events.extend(events)

        acs = data.get("AcsEvent", {}) if data else {}
        status = acs.get("responseStatusStrg")
        count = int(acs.get("numOfMatches") or 0)

        if count == 0 or status != "MORE" or count < page_size:
            break

        position += count

    return all_events


def _get_employee_id_from_event(event):
    employee_id = (
        event.get("employeeNoString")
        or event.get("employeeID")
        or event.get("employeeNo")
        or event.get("employeeId")
        or event.get("cardNo")
        or ""
    )

    return str(employee_id).strip()


def _get_full_name_from_event(event):
    return str(event.get("name") or event.get("employeeName") or "").strip()


def _get_authentication_method(event):
    """Return the successful biometric method represented by an event."""
    try:
        major = int(event.get("major") or 0)
        minor = int(event.get("minor") or 0)
    except (TypeError, ValueError):
        return None

    if major != 5:
        return None

    if minor == HIKVISION_MINOR_FINGERPRINT_PASS:
        return "fingerprint"

    if minor == HIKVISION_MINOR_FACE_PASS:
        return "face"

    if minor == HIKVISION_MINOR_FACE_AND_FINGERPRINT_PASS:
        return "face_and_fingerprint"

    return None


def _build_dual_biometric_event(face_event, fingerprint_event):
    """Combine two successful source events into one auditable punch."""
    face_time = _parse_timestamp(face_event.get("time"))
    fingerprint_time = _parse_timestamp(fingerprint_event.get("time"))

    if face_time is None or fingerprint_time is None:
        return None

    completed_at = max(face_time, fingerprint_time)
    employee_id = (
        _get_employee_id_from_event(face_event)
        or _get_employee_id_from_event(fingerprint_event)
    )
    full_name = (
        _get_full_name_from_event(face_event)
        or _get_full_name_from_event(fingerprint_event)
    )

    status = _normalize_attendance_status(face_event)
    fingerprint_status = _normalize_attendance_status(fingerprint_event)

    if status != fingerprint_status:
        return None

    if status == AttendanceRecord.STATUS_UNKNOWN:
        return None

    return {
        "employee_id": employee_id,
        "full_name": full_name,
        "department": "",
        "timestamp": completed_at.isoformat(),
        "attendance_status": status,
        "raw_row": {
            "time": completed_at.isoformat(),
            "employeeNoString": employee_id,
            "name": full_name,
            "attendanceStatus": face_event.get("attendanceStatus")
                or fingerprint_event.get("attendanceStatus"),
            "label": face_event.get("label") or fingerprint_event.get("label"),
            "dual_biometric_verified": True,
            "verification_methods": ["face", "fingerprint"],
            "face_event": face_event,
            "fingerprint_event": fingerprint_event,
        },
    }


def pair_dual_biometric_events(events):
    """
    Return only attendance punches proven by both face and fingerprint.

    Separate face/fingerprint events are paired when they have the same
    employee, the same IN/OUT status, the same local date, and occur within
    DUAL_BIOMETRIC_PAIR_WINDOW. A native face+fingerprint success event is
    already a complete punch.
    """
    grouped = {}
    completed = []

    for event in events:
        employee_id = _get_employee_id_from_event(event)
        event_time = _parse_timestamp(event.get("time"))
        method = _get_authentication_method(event)
        status = _normalize_attendance_status(event)

        if (
            not employee_id
            or event_time is None
            or method is None
            or status == AttendanceRecord.STATUS_UNKNOWN
        ):
            continue

        if method == "face_and_fingerprint":
            full_name = _get_full_name_from_event(event)
            completed.append({
                "employee_id": employee_id,
                "full_name": full_name,
                "department": "",
                "timestamp": event_time.isoformat(),
                "attendance_status": status,
                "raw_row": {
                    **event,
                    "dual_biometric_verified": True,
                    "verification_methods": ["face", "fingerprint"],
                    "combined_device_event": True,
                },
            })
            continue

        local_time = (
            timezone.localtime(event_time)
            if timezone.is_aware(event_time)
            else event_time
        )
        key = (employee_id, local_time.date(), status)
        grouped.setdefault(key, []).append((event_time, method, event))

    for candidates in grouped.values():
        candidates.sort(key=lambda item: item[0])
        used_indexes = set()

        for index, (event_time, method, event) in enumerate(candidates):
            if index in used_indexes:
                continue

            opposite_method = "fingerprint" if method == "face" else "face"
            best_index = None
            best_distance = None

            for other_index, (other_time, other_method, _) in enumerate(candidates):
                if other_index == index or other_index in used_indexes:
                    continue
                if other_method != opposite_method:
                    continue

                distance = abs(other_time - event_time)
                if distance > DUAL_BIOMETRIC_PAIR_WINDOW:
                    continue

                if best_distance is None or distance < best_distance:
                    best_index = other_index
                    best_distance = distance

            if best_index is None:
                continue

            _, _, other_event = candidates[best_index]
            if method == "face":
                face_event, fingerprint_event = event, other_event
            else:
                face_event, fingerprint_event = other_event, event

            combined = _build_dual_biometric_event(
                face_event,
                fingerprint_event,
            )
            if combined is not None:
                completed.append(combined)
                used_indexes.update({index, best_index})

    completed.sort(key=lambda item: item["timestamp"])
    return completed


def _dedupe_events(events):
    """
    Remove duplicate Hikvision events from multiple major searches/pages.
    """
    unique = []
    seen = set()

    for e in events:
        key = (
            str(e.get("serialNo") or ""),
            str(e.get("time") or ""),
            str(
                e.get("employeeNoString")
                or e.get("employeeID")
                or e.get("employeeNo")
                or e.get("employeeId")
                or e.get("cardNo")
                or ""
            ),
            str(e.get("attendanceStatus") or ""),
            str(e.get("label") or ""),
            str(e.get("major") or ""),
            str(e.get("minor") or ""),
        )

        if key not in seen:
            seen.add(key)
            unique.append(e)

    return unique


# =========================================================
# MAIN SYNC FUNCTION
# =========================================================

def fetch_hikvision_attendance(device):
    """
    Fetch attendance events from Hikvision device and save to AttendanceRecord.

    Saves:
    - employee_id from Hikvision employeeNoString / employeeID
    - timestamp from event time
    - attendance_status as CHECK_IN / CHECK_OUT / UNKNOWN
    - branch from BiometricDevice.branch
    - raw_row as the full Hikvision event
    """

    end = timezone.localtime(timezone.now())
    start = _make_start_time(device)

    try:
        # Major 5 and 0 are both queried because Hikvision firmware can vary.
        events = (
            _fetch_all_pages(device, start, end, 5)
            + _fetch_all_pages(device, start, end, 0)
        )

        unique_events = _dedupe_events(events)
        person_events = _pick_person_events(unique_events)
        validated_punches = pair_dual_biometric_events(person_events)

        created_count = 0
        skipped_count = 0
        unknown_count = 0
        checkin_count = 0
        checkout_count = 0

        for punch in validated_punches:
            employee_id = punch["employee_id"]
            timestamp = _parse_timestamp(punch["timestamp"])
            full_name = punch["full_name"]
            attendance_status = punch["attendance_status"]
            raw_row = punch["raw_row"]

            if not employee_id or not timestamp:
                skipped_count += 1
                continue

            if attendance_status == AttendanceRecord.STATUS_CHECKIN:
                checkin_count += 1
            elif attendance_status == AttendanceRecord.STATUS_CHECKOUT:
                checkout_count += 1
            else:
                unknown_count += 1

            branch = device.branch

            obj, created = AttendanceRecord.objects.get_or_create(
                employee_id=employee_id,
                timestamp=timestamp,
                attendance_status=attendance_status,
                branch=branch,
                defaults={
                    "full_name": full_name,
                    "department": "",
                    "raw_row": raw_row,
                }
            )

            if created:
                created_count += 1
            else:
                # Update raw data/full name if already exists.
                update_fields = []

                if full_name and obj.full_name != full_name:
                    obj.full_name = full_name
                    update_fields.append("full_name")

                if not obj.raw_row:
                    obj.raw_row = raw_row
                    update_fields.append("raw_row")

                if update_fields:
                    obj.save(update_fields=update_fields)

                skipped_count += 1

        print("========== HIKVISION SYNC DONE ==========")
        print("DEVICE:", device.name)
        print("BRANCH:", device.branch.name if device.branch else None)
        print("START:", start)
        print("END:", end)
        print("RAW EVENTS:", len(events))
        print("UNIQUE EVENTS:", len(unique_events))
        print("PERSON EVENTS:", len(person_events))
        print("VALID DUAL-BIOMETRIC PUNCHES:", len(validated_punches))
        print("CREATED:", created_count)
        print("SKIPPED/DUPLICATE:", skipped_count)
        print("CHECK IN:", checkin_count)
        print("CHECK OUT:", checkout_count)
        print("UNKNOWN:", unknown_count)
        print("=========================================")

        return created_count

    except Exception as e:
        print("========== HIKVISION SYNC ERROR ==========")
        print("DEVICE:", getattr(device, "name", "Unknown"))
        print("ERROR:", e)
        print("==========================================")

        raise RuntimeError(
            f"Could not synchronize {getattr(device, 'name', 'device')}: {e}"
        ) from e


def test_hikvision_connection(device):
    """
    Tests whether Django can reach and authenticate with the
    registered Hikvision device.
    """

    url = (
        f"http://{device.ip_address}:{device.port}"
        "/ISAPI/System/deviceInfo"
    )

    try:
        response = requests.get(
            url,
            auth=HTTPDigestAuth(
                device.username,
                device.password,
            ),
            timeout=8,
        )

        if response.status_code == 200:
            return {
                "ok": True,
                "message": (
                    f"Connected successfully to {device.name} at "
                    f"{device.ip_address}:{device.port}."
                ),
            }

        if response.status_code == 401:
            return {
                "ok": False,
                "message": (
                    "The device was reached, but the username or "
                    "password was rejected."
                ),
            }

        if response.status_code == 403:
            return {
                "ok": False,
                "message": (
                    "The device rejected access. Check the Hikvision "
                    "account permissions and ISAPI settings."
                ),
            }

        return {
            "ok": False,
            "message": (
                f"The Hikvision device returned HTTP status "
                f"{response.status_code}."
            ),
        }

    except requests.exceptions.ConnectTimeout:
        return {
            "ok": False,
            "message": (
                "Connection timed out. Check the IP address, port, "
                "device power, firewall, and network connection."
            ),
        }

    except requests.exceptions.ConnectionError:
        return {
            "ok": False,
            "message": (
                "The server could not connect to the device. Check "
                "whether both are reachable through the same network."
            ),
        }

    except requests.RequestException as exc:
        return {
            "ok": False,
            "message": f"Connection failed: {exc}",
        }

        
