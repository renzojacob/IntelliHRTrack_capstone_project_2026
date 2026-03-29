import json
from datetime import datetime, timedelta

import requests
from requests.auth import HTTPDigestAuth
from django.utils.dateparse import parse_datetime

from .models import AttendanceRecord


def _parse_timestamp(value):
    if not value:
        return None

    value = str(value).strip()

    dt = parse_datetime(value)
    if dt:
        return dt

    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    return None


def _normalize_attendance_status(event):
    raw = (
        str(event.get("attendanceStatus") or "")
        or str(event.get("minorEventType") or "")
        or str(event.get("eventType") or "")
    ).strip().upper()

    if any(word in raw for word in ["CHECKIN", "CHECK-IN", "IN", "ENTRY", "AUTHENTICATED VIA FACE"]):
        return AttendanceRecord.STATUS_CHECKIN

    if any(word in raw for word in ["CHECKOUT", "CHECK-OUT", "OUT", "EXIT"]):
        return AttendanceRecord.STATUS_CHECKOUT

    # fallback: if this is clearly a person-auth event but no text label exists
    if (
        event.get("employeeNoString")
        or event.get("employeeID")
        or event.get("employeeNo")
        or event.get("name")
        or event.get("employeeName")
    ):
        return AttendanceRecord.STATUS_CHECKIN

    return AttendanceRecord.STATUS_UNKNOWN


def _search_events(device, payload):
    url = f"http://{device.ip_address}:{device.port}/ISAPI/AccessControl/AcsEvent?format=json"

    r = requests.post(
        url,
        json=payload,
        auth=HTTPDigestAuth(device.username, device.password),
        timeout=15,
    )

    print("STATUS:", r.status_code)

    if r.status_code != 200:
        print("RAW TEXT:", r.text)
        return {}

    try:
        data = r.json()
    except Exception:
        print("RAW TEXT:", r.text)
        return {}

    print("RAW RESPONSE:")
    print(json.dumps(data, indent=2))
    return data


def _extract_events(data):
    return data.get("AcsEvent", {}).get("InfoList", [])


def _pick_person_events(events):
    person_events = []
    for event in events:
        has_person = any(
            [
                event.get("employeeNoString"),
                event.get("employeeID"),
                event.get("employeeNo"),
                event.get("employeeId"),
                event.get("name"),
                event.get("employeeName"),
                event.get("cardNo"),
            ]
        )
        if has_person:
            person_events.append(event)
    return person_events


def fetch_hikvision_attendance(device):
    now = datetime.now()
    start = now - timedelta(hours=24)

    # Strategy:
    # 1) Query broad access events for last 24 hours
    # 2) Try major=5 (common access/auth events)
    # 3) Fallback to major=0 (all events)
    queries = [
        {
            "AcsEventCond": {
                "searchID": "1",
                "searchResultPosition": 0,
                "maxResults": 200,
                "major": 5,
                "minor": 0,
                "startTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "endTime": now.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        },
        {
            "AcsEventCond": {
                "searchID": "2",
                "searchResultPosition": 0,
                "maxResults": 200,
                "major": 0,
                "minor": 0,
                "startTime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "endTime": now.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        },
    ]

    all_events = []

    try:
        for payload in queries:
            data = _search_events(device, payload)
            events = _extract_events(data)
            if events:
                all_events.extend(events)

        # remove exact duplicates by serialNo + time if possible
        unique = []
        seen = set()
        for event in all_events:
            key = (
                str(event.get("serialNo") or ""),
                str(event.get("time") or ""),
                str(event.get("employeeNoString") or event.get("employeeID") or event.get("employeeNo") or ""),
            )
            if key not in seen:
                seen.add(key)
                unique.append(event)

        print(f"TOTAL EVENTS RETURNED: {len(unique)}")

        person_events = _pick_person_events(unique)
        print(f"PERSON EVENTS FOUND: {len(person_events)}")

        # Debug print so you can see what the device really returns
        for idx, event in enumerate(person_events[:10], start=1):
            print(f"PERSON EVENT #{idx}: {json.dumps(event, indent=2)}")

        created_count = 0
        skipped_count = 0

        for event in person_events:
            employee_id = (
                event.get("employeeNoString")
                or event.get("employeeID")
                or event.get("employeeNo")
                or event.get("employeeId")
                or event.get("cardNo")
                or ""
            )
            employee_id = str(employee_id).strip()

            timestamp_raw = event.get("time")
            timestamp = _parse_timestamp(timestamp_raw)

            full_name = str(event.get("name") or event.get("employeeName") or "").strip()
            department = str(event.get("department") or "").strip()

            attendance_status = _normalize_attendance_status(event)

            if not employee_id or not timestamp:
                skipped_count += 1
                continue

            _, created = AttendanceRecord.objects.get_or_create(
                employee_id=employee_id,
                timestamp=timestamp,
                attendance_status=attendance_status,
                branch=device.branch,
                defaults={
                    "full_name": full_name,
                    "department": department,
                    "raw_row": event,
                },
            )

            if created:
                created_count += 1
            else:
                skipped_count += 1

        print(f"✅ Sync success | created={created_count} skipped={skipped_count}")
        return created_count

    except Exception as e:
        print("❌ ERROR:", e)
        return 0