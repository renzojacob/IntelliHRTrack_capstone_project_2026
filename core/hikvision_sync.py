import json
from datetime import datetime, timedelta

import requests
from requests.auth import HTTPDigestAuth
from django.utils import timezone
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
        or str(event.get("label") or "")
    ).strip().upper()

    if any(word in raw for word in ["CHECKIN", "CHECK-IN", "CHECK IN", "IN", "ENTRY"]):
        return AttendanceRecord.STATUS_CHECKIN

    if any(word in raw for word in ["CHECKOUT", "CHECK-OUT", "CHECK OUT", "OUT", "EXIT"]):
        return AttendanceRecord.STATUS_CHECKOUT

    if (
        event.get("employeeNoString")
        or event.get("employeeID")
        or event.get("employeeNo")
        or event.get("employeeId")
        or event.get("name")
        or event.get("employeeName")
        or event.get("cardNo")
    ):
        return AttendanceRecord.STATUS_CHECKIN

    return AttendanceRecord.STATUS_UNKNOWN


def _search_events(device, payload):
    url = f"http://{device.ip_address}:{device.port}/ISAPI/AccessControl/AcsEvent?format=json"

    response = requests.post(
        url,
        json=payload,
        auth=HTTPDigestAuth(device.username, device.password),
        timeout=20,
    )

    print("STATUS:", response.status_code)

    if response.status_code != 200:
        print("RAW TEXT:", response.text)
        return {}

    try:
        data = response.json()
    except Exception:
        print("RAW TEXT:", response.text)
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


def _make_start_time():
    last_record = AttendanceRecord.objects.order_by("-timestamp").first()

    if last_record and last_record.timestamp:
        start = last_record.timestamp

        if timezone.is_aware(start):
            start = timezone.localtime(start)

        start = start - timedelta(minutes=2)
        print("📌 Using last record timestamp with overlap:", start)
        return start

    fallback = timezone.localtime(timezone.now()) - timedelta(days=3)
    print("📌 No previous data, fallback to:", fallback)
    return fallback


def _fetch_all_pages(device, start, end, major):
    all_events = []
    position = 0
    page_size = 30

    while True:
        payload = {
            "AcsEventCond": {
                "searchID": f"{major}-{position}",
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

        print(f"PAGE major={major} position={position} count={count} status={status}")

        if count == 0:
            break
        if status != "MORE":
            break
        if count < page_size:
            break

        position += count

    return all_events


def fetch_hikvision_attendance(device):
    end = timezone.localtime(timezone.now())
    start = _make_start_time()

    try:
        events_major_5 = _fetch_all_pages(device, start, end, major=5)
        events_major_0 = _fetch_all_pages(device, start, end, major=0)

        all_events = events_major_5 + events_major_0

        unique = []
        seen = set()
        for event in all_events:
            key = (
                str(event.get("serialNo") or ""),
                str(event.get("time") or ""),
                str(
                    event.get("employeeNoString")
                    or event.get("employeeID")
                    or event.get("employeeNo")
                    or event.get("employeeId")
                    or ""
                ),
            )
            if key not in seen:
                seen.add(key)
                unique.append(event)

        print(f"TOTAL EVENTS RETURNED: {len(unique)}")

        person_events = _pick_person_events(unique)
        print(f"PERSON EVENTS FOUND: {len(person_events)}")

        for idx, event in enumerate(person_events[:10], start=1):
            print(f"EVENT {idx}: {json.dumps(event, indent=2)}")

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
                print("⚠️ Skipped invalid record:", event)
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
                print(f"✅ SAVED: {employee_id} @ {timestamp}")
                created_count += 1
            else:
                skipped_count += 1

        print(f"✅ FINAL RESULT | created={created_count} skipped={skipped_count}")
        return created_count

    except Exception as e:
        print("❌ ERROR:", e)
        return 0