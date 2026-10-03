import argparse
import os
import sys
import time
from datetime import timedelta
from types import SimpleNamespace

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

import requests
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.hikvision_sync import (
    _dedupe_events,
    _fetch_all_pages,
    _get_employee_id_from_event,
    _get_full_name_from_event,
    _normalize_attendance_status,
    _parse_timestamp,
    _pick_person_events,
    test_hikvision_connection,
)


DEFAULT_POLL_SECONDS = 10
DEFAULT_LOOKBACK_DAYS = 7


def get_configuration():
    base_url = os.environ.get(
        "INTELLIHRTRACK_BASE_URL",
        "http://127.0.0.1:8000",
    ).strip().rstrip("/")

    connector_token = os.environ.get(
        "INTELLIHRTRACK_CONNECTOR_TOKEN",
        "",
    ).strip()

    if not connector_token:
        raise RuntimeError(
            "INTELLIHRTRACK_CONNECTOR_TOKEN is missing."
        )

    return base_url, connector_token


def connector_headers(connector_token):
    return {
        "Authorization": f"Bearer {connector_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def get_next_request(base_url, connector_token):
    url = f"{base_url}/api/biometrics/connector/next/"

    response = requests.get(
        url,
        headers=connector_headers(connector_token),
        timeout=30,
    )

    response.raise_for_status()
    return response.json()


def submit_result(
    base_url,
    connector_token,
    request_id,
    payload,
):
    url = (
        f"{base_url}/api/biometrics/connector/"
        f"requests/{request_id}/result/"
    )

    response = requests.post(
        url,
        headers=connector_headers(connector_token),
        json=payload,
        timeout=120,
    )

    response.raise_for_status()
    return response.json()


def build_device(device_data):
    return SimpleNamespace(
        id=device_data["id"],
        name=device_data["name"],
        ip_address=device_data["ip_address"],
        port=int(device_data.get("port") or 80),
        username=device_data["username"],
        password=device_data["password"],
    )


def determine_time_range(sync_request):
    end_time = timezone.localtime(timezone.now())

    date_from_text = sync_request.get("date_from")
    date_to_text = sync_request.get("date_to")

    start_time = (
        parse_datetime(date_from_text)
        if date_from_text
        else None
    )

    requested_end_time = (
        parse_datetime(date_to_text)
        if date_to_text
        else None
    )

    if start_time is None:
        start_time = end_time - timedelta(
            days=DEFAULT_LOOKBACK_DAYS
        )

    if requested_end_time is not None:
        end_time = requested_end_time

    if timezone.is_naive(start_time):
        start_time = timezone.make_aware(
            start_time,
            timezone.get_current_timezone(),
        )

    if timezone.is_naive(end_time):
        end_time = timezone.make_aware(
            end_time,
            timezone.get_current_timezone(),
        )

    return start_time, end_time


def read_hikvision_events(device, start_time, end_time):
    connection = test_hikvision_connection(device)

    if not connection.get("ok"):
        raise RuntimeError(
            connection.get("message")
            or "Could not connect to Hikvision device."
        )

    events = (
        _fetch_all_pages(device, start_time, end_time, 5)
        + _fetch_all_pages(device, start_time, end_time, 0)
    )

    unique_events = _dedupe_events(events)
    person_events = _pick_person_events(unique_events)

    normalized_events = []
    ignored_events = 0

    for event in person_events:
        employee_id = _get_employee_id_from_event(event)
        event_time = _parse_timestamp(event.get("time"))

        if not employee_id or event_time is None:
            ignored_events += 1
            continue

        full_name = _get_full_name_from_event(event)
        attendance_status = _normalize_attendance_status(event)

        normalized_events.append(
            {
                "employee_id": employee_id,
                "full_name": full_name,
                "department": "",
                "timestamp": event_time.isoformat(),
                "attendance_status": attendance_status,
                "raw_row": event,
            }
        )

    print(f"Raw events: {len(events)}")
    print(f"Unique events: {len(unique_events)}")
    print(f"Person events: {len(person_events)}")
    print(f"Events prepared for upload: {len(normalized_events)}")
    print(f"Events ignored: {ignored_events}")

    return normalized_events


def process_one_request(base_url, connector_token):
    response_data = get_next_request(
        base_url,
        connector_token,
    )

    sync_request = response_data.get("request")

    if sync_request is None:
        print("No pending synchronization request.")
        return False

    request_id = sync_request["id"]
    branch = sync_request["branch"]
    device_data = sync_request["device"]
    device = build_device(device_data)

    print("=" * 60)
    print(f"Processing request: {request_id}")
    print(f"Branch: {branch['name']}")
    print(
        f"Device: {device.name} "
        f"({device.ip_address}:{device.port})"
    )

    try:
        start_time, end_time = determine_time_range(
            sync_request
        )

        print(f"Start: {start_time}")
        print(f"End: {end_time}")
        print("Connecting to Hikvision through the local network...")

        events = read_hikvision_events(
            device,
            start_time,
            end_time,
        )

        result = submit_result(
            base_url,
            connector_token,
            request_id,
            {
                "success": True,
                "events": events,
            },
        )

        print("Synchronization completed.")
        print(
            "Records created:",
            result.get("records_created", 0),
        )
        print(
            "Records skipped:",
            result.get("records_skipped", 0),
        )
        print(
            "Records failed:",
            result.get("records_failed", 0),
        )

        return True

    except Exception as exc:
        error_message = str(exc)

        print(f"Synchronization failed: {error_message}")

        try:
            submit_result(
                base_url,
                connector_token,
                request_id,
                {
                    "success": False,
                    "error": error_message,
                },
            )
        except Exception as report_error:
            print(
                "Could not report the failure to IntelliHRTrack:",
                report_error,
            )

        return False


def run_connector(run_once=False, poll_seconds=DEFAULT_POLL_SECONDS):
    base_url, connector_token = get_configuration()

    print("IntelliHRTrack Hikvision Connector")
    print(f"Website: {base_url}")
    print(
        "Connector token loaded:",
        f"{len(connector_token)} characters",
    )

    while True:
        try:
            process_one_request(
                base_url,
                connector_token,
            )
        except requests.RequestException as exc:
            print(f"Website connection error: {exc}")
        except Exception as exc:
            print(f"Connector error: {exc}")

        if run_once:
            break

        print(
            f"Waiting {poll_seconds} seconds "
            "for another request..."
        )
        time.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Connect a branch Hikvision device to the "
            "hosted IntelliHRTrack website."
        )
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Check and process one request, then stop.",
    )

    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=DEFAULT_POLL_SECONDS,
        help="Seconds between request checks.",
    )

    args = parser.parse_args()

    try:
        run_connector(
            run_once=args.once,
            poll_seconds=max(5, args.poll_seconds),
        )
    except KeyboardInterrupt:
        print("\nConnector stopped.")
    except Exception as exc:
        print(f"Fatal connector error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()