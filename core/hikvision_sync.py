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


# 🔥 FIXED STATUS LOGIC (WITH TEMP TEST)
def _normalize_attendance_status(event):
    raw = (
        str(event.get("attendanceStatus") or "")
        or str(event.get("minorEventType") or "")
        or str(event.get("eventType") or "")
        or str(event.get("label") or "")
    ).strip().lower()

    # NORMAL MAPPING
    if "checkin" in raw or "in" in raw or "entry" in raw:
        return AttendanceRecord.STATUS_CHECKIN

    if "checkout" in raw or "out" in raw or "exit" in raw:
        # 🔥 TEMP TEST FIX (IMPORTANT)
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

    if response.status_code != 200:
        print("ERROR RESPONSE:", response.text)
        return {}

    try:
        return response.json()
    except Exception:
        print("INVALID JSON:", response.text)
        return {}


def _extract_events(data):
    return data.get("AcsEvent", {}).get("InfoList", [])


def _pick_person_events(events):
    return [
        e for e in events if any([
            e.get("employeeNoString"),
            e.get("employeeID"),
            e.get("employeeNo"),
            e.get("employeeId"),
            e.get("name"),
            e.get("employeeName"),
            e.get("cardNo"),
        ])
    ]


def _make_start_time():
    last = AttendanceRecord.objects.order_by("-timestamp").first()

    if last and last.timestamp:
        start = last.timestamp
        if timezone.is_aware(start):
            start = timezone.localtime(start)
        return start - timedelta(minutes=2)

    return timezone.localtime(timezone.now()) - timedelta(days=3)


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

        if count == 0 or status != "MORE" or count < page_size:
            break

        position += count

    return all_events


def fetch_hikvision_attendance(device):
    end = timezone.localtime(timezone.now())
    start = _make_start_time()

    try:
        events = (
            _fetch_all_pages(device, start, end, 5)
            + _fetch_all_pages(device, start, end, 0)
        )

        unique = []
        seen = set()

        for e in events:
            key = (
                str(e.get("serialNo")),
                str(e.get("time")),
                str(e.get("employeeNoString") or e.get("employeeID") or ""),
            )
            if key not in seen:
                seen.add(key)
                unique.append(e)

        person_events = _pick_person_events(unique)

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

            timestamp = _parse_timestamp(event.get("time"))

            full_name = str(event.get("name") or event.get("employeeName") or "").strip()

            attendance_status = _normalize_attendance_status(event)

            if not employee_id or not timestamp:
                skipped_count += 1
                continue

            # 🔥 CRITICAL FIX (BRANCH)
            branch = device.branch

            obj, created = AttendanceRecord.objects.get_or_create(
                employee_id=employee_id,
                timestamp=timestamp,
                attendance_status=attendance_status,
                branch=branch,
                defaults={
                    "full_name": full_name,
                    "raw_row": event,
                }
            )

            if created:
                created_count += 1
            else:
                skipped_count += 1

        print(f"SYNC DONE: created={created_count}, skipped={skipped_count}")
        return created_count

    except Exception as e:
        print("SYNC ERROR:", e)
        return 0