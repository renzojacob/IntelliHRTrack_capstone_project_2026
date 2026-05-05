import json
from datetime import datetime, timedelta

import requests
from requests.auth import HTTPDigestAuth
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import AttendanceRecord


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


def _make_start_time():
    """
    Start sync from last saved AttendanceRecord minus 2 minutes.

    This avoids missing recent logs and get_or_create prevents duplicates.
    """
    last = AttendanceRecord.objects.order_by("-timestamp").first()

    if last and last.timestamp:
        start = last.timestamp

        if timezone.is_aware(start):
            start = timezone.localtime(start)
        else:
            start = timezone.make_aware(start, timezone.get_current_timezone())

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
    start = _make_start_time()

    try:
        # Major 5 and 0 are both queried because Hikvision firmware can vary.
        events = (
            _fetch_all_pages(device, start, end, 5)
            + _fetch_all_pages(device, start, end, 0)
        )

        unique_events = _dedupe_events(events)
        person_events = _pick_person_events(unique_events)

        created_count = 0
        skipped_count = 0
        unknown_count = 0
        checkin_count = 0
        checkout_count = 0

        for event in person_events:
            employee_id = _get_employee_id_from_event(event)
            timestamp = _parse_timestamp(event.get("time"))
            full_name = _get_full_name_from_event(event)
            attendance_status = _normalize_attendance_status(event)

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
                    "raw_row": event,
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
                    obj.raw_row = event
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
        return 0