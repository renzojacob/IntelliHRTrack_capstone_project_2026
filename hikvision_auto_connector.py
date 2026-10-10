"""Automatic incremental sync; requests is the only third-party dependency."""
import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPDigestAuth

MANILA = timezone(timedelta(hours=8))
FIELDS = ("time", "major", "minor", "employeeNoString", "employeeID",
          "employeeNo", "employeeId", "name", "employeeName", "serialNo",
          "currentVerifyMode", "attendanceStatus", "label")
LOG = logging.getLogger("automatic_attendance")


def parse_stamp(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=MANILA)
    return stamp


def select_native(events):
    selected = []
    seen = set()
    for event in events:
        try:
            if (int(event.get("major")), int(event.get("minor"))) != (5, 153):
                continue
            if str(event.get("currentVerifyMode", "")).lower() != "faceandfp":
                continue
            employee = str(event.get("employeeNoString") or event.get("employeeID")
                           or event.get("employeeNo") or event.get("employeeId") or "").strip()
            stamp = datetime.fromisoformat(event["time"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or not employee or len(employee) > 64:
                continue
            status = str(event.get("attendanceStatus") or "").strip().lower()
            key = (employee, stamp, status)
            if key in seen:
                continue
            seen.add(key)
            selected.append({key: event[key] for key in FIELDS if key in event})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return selected


def save_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class InstanceLock:
    def __init__(self, directory):
        self.path = directory / "connector.lock"
        self.file = None

    def __enter__(self):
        self.file = self.path.open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            if self.path.stat().st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close()
                raise RuntimeError("An automatic connector is already running for this website.")
        else:
            import fcntl
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close()
                raise RuntimeError("An automatic connector is already running for this website.")
        return self

    def __exit__(self, *args):
        self.file.close()


class DeviceReader:
    def __init__(self, device, manual_offset=None):
        self.device = device
        self.url = f"http://{device['ip_address']}:{int(device.get('port') or 80)}"
        self.session = requests.Session()
        self.session.trust_env = False  # Never send local-device credentials through a proxy.
        self.session.auth = HTTPDigestAuth(device["username"], device["password"])
        self.manual_offset = manual_offset
        self.offset = None
        self.clock_checked = 0

    def device_now(self):
        if self.offset is None or time.monotonic() - self.clock_checked > 60:
            old = self.offset
            if self.manual_offset is not None:
                self.offset = self.manual_offset
            else:
                start = datetime.now(timezone.utc)
                response = self.session.get(
                    self.url + "/ISAPI/System/time",
                    auth=HTTPDigestAuth(
                        self.device["username"],
                        self.device["password"],
                    ),
                    timeout=(3, 8),
                )
                response.raise_for_status()
                root = ET.fromstring(response.content)
                local = next((node.text for node in root.iter()
                              if node.tag.split("}")[-1] == "localTime"), None)
                if not local:
                    raise RuntimeError("Cannot read device time. Use --clock-offset-seconds only after checking its clock.")
                midpoint = start + (datetime.now(timezone.utc) - start) / 2
                self.offset = (parse_stamp(local) - midpoint).total_seconds()
            if old is None or abs(self.offset - old) > 5:
                LOG.info("Device %s clock offset: %.1f seconds; timestamps are preserved.",
                         self.device["id"], self.offset)
            self.clock_checked = time.monotonic()
        return datetime.now(timezone.utc) + timedelta(seconds=self.offset)

    def fetch(self, start, end):
        position = 0
        search_id = str(uuid.uuid4())
        events = []
        for _ in range(500):
            response = self.session.post(
                self.url + "/ISAPI/AccessControl/AcsEvent?format=json",
                json={"AcsEventCond": {
                    "searchID": search_id, "searchResultPosition": position,
                    "maxResults": 30, "major": 5, "minor": 0,
                    "startTime": start.astimezone(MANILA).strftime("%Y-%m-%dT%H:%M:%S"),
                    "endTime": end.astimezone(MANILA).strftime("%Y-%m-%dT%H:%M:%S"),
                }            },
            auth=HTTPDigestAuth(
                self.device["username"],
                self.device["password"],
            ),
            timeout=(3, 8),
            
            )
            response.raise_for_status()
            data = response.json().get("AcsEvent")
            if not isinstance(data, dict):
                raise RuntimeError("Device returned an invalid event response; checkpoint retained.")
            page = data.get("InfoList") or []
            if not isinstance(page, list):
                raise RuntimeError("Invalid device event list; checkpoint retained.")
            count = int(data.get("numOfMatches") or 0)
            status = str(data.get("responseStatusStrg") or "").upper()
            if count != len(page):
                raise RuntimeError("Device page count mismatch; checkpoint retained.")
            events.extend(page)
            if status in ("OK", "NO MATCH", "NO_MATCH", "NO_MATCHES"):
                return events
            if status != "MORE" or count == 0:
                raise RuntimeError("Incomplete device search; checkpoint retained.")
            # Even a short page marked MORE must be followed.
            position += count
        raise RuntimeError("Device search exceeded page limit; checkpoint retained.")


class Website:
    def __init__(self, url, token):
        self.url = url + "/api/biometrics/live/"
        self.session = requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + token, "Accept": "application/json"})

    def call(self, method, path, payload=None):
        response = self.session.request(method, self.url + path, json=payload,
                                        timeout=(5, 20), allow_redirects=False)
        if response.status_code == 404:
            raise RuntimeError("Automatic API is not deployed. Install and deploy the backend files first.")
        if response.status_code in (401, 403):
            raise RuntimeError("Website rejected token/device scope. Check active connector and branch.")
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError("Website did not acknowledge the operation; checkpoint retained.")
        return data


def upload_pending(website, device, state, path):
    created = skipped = 0
    pending = state.get("pending")
    if pending is None:
        return created, skipped
    events = pending["events"]
    for index in range(0, len(events), 100):
        batch = events[index:index + 100]
        result = website.call("POST", "events/", {"device_id": device["id"], "events": batch})
        new = int(result.get("records_created", -1))
        existing = int(result.get("records_skipped", -1))
        if int(result.get("records_failed", -1)) != 0 or new < 0 or existing < 0 or new + existing != len(batch):
            raise RuntimeError("Upload was not fully acknowledged; retrying saved events later.")
        created += new
        skipped += existing
    # Checkpoint advances only after EVERY event was acknowledged.
    state["cursor"] = pending["end"]
    state["pending"] = None
    save_json(path, state)
    if events:
        LOG.info("Device %s upload acknowledged: created=%s skipped=%s received_at=%s",
                 device["id"], created, skipped, result.get("received_at"))
    return created, skipped


def cycle(website, reader, state, path, initial_hours=1):
    upload_pending(website, reader.device, state, path)
    end = reader.device_now()
    cursor = parse_stamp(state["cursor"]) if state.get("cursor") else end - timedelta(hours=initial_hours)
    # Rewind safely when the device clock is moved backwards.
    if cursor > end:
        LOG.warning("Device clock moved backwards; replaying one hour with deduplication.")
        cursor = end - timedelta(hours=1)
    start = cursor - timedelta(seconds=60)
    end = min(end, start + timedelta(hours=1))
    events = reader.fetch(start, end)
    selected = select_native(events)
    state["pending"] = {"end": end.isoformat(), "events": selected}
    save_json(path, state)  # Write outbox BEFORE making any website upload.
    if selected:
        LOG.info("Device %s read %s events; native combined punches=%s",
                 reader.device["id"], len(events), len(selected))
    upload_pending(website, reader.device, state, path)


def main():
    parser = argparse.ArgumentParser(description="Automatic native combined attendance syncing.")
    parser.add_argument("--poll-seconds", type=float, default=3)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--initial-lookback-hours", type=float, default=1)
    parser.add_argument("--clock-offset-seconds", type=float, default=None,
                        help="Fallback: device time minus laptop time; use only if clock API is unavailable.")
    args = parser.parse_args()
    base_url = os.environ.get("INTELLIHRTRACK_BASE_URL", "").strip().rstrip("/")
    token = os.environ.get("INTELLIHRTRACK_CONNECTOR_TOKEN", "").strip()
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SystemExit("Set INTELLIHRTRACK_BASE_URL to the HTTPS Railway website.")
    if not token:
        raise SystemExit("INTELLIHRTRACK_CONNECTOR_TOKEN is missing.")
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "IntelliHRTrack" / "automatic-sync"
    directory = root / hashlib.sha256(base_url.encode()).hexdigest()[:16]
    directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_log = RotatingFileHandler(directory / "connector.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    stream = logging.StreamHandler()
    for handler in (file_log, stream):
        handler.setFormatter(formatter)
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.info("Automatic connector starting; poll interval %.1fs; no Sync Now required.", max(1, args.poll_seconds))
    LOG.info("Checkpoint/log directory: %s", directory)
    website = Website(base_url, token)
    readers = {}
    config = None
    refreshed = 0
    config_retry = 0
    failures = {}
    due = {}
    with InstanceLock(directory):
        while True:
            loop_start = time.monotonic()
            if loop_start >= config_retry and (config is None or loop_start - refreshed > 60):
                try:
                    config = website.call("GET", "config/")
                    refreshed = time.monotonic()
                    config_retry = 0
                    LOG.info("Configured branch: %s; active devices: %s", config["branch_name"], len(config["devices"]))
                    if not config["devices"]:
                        LOG.warning("No active devices configured for this token's branch.")
                except Exception as exc:
                    LOG.error("Configuration: %s", type(exc).__name__ + ": " + str(exc))
                    config_retry = time.monotonic() + 15
            unsuccessful = config is None
            for device in (config or {}).get("devices", []):
                key = str(device["id"])
                if time.monotonic() < due.get(key, 0):
                    continue
                try:
                    if key not in readers or readers[key].device != device:
                        if key in readers:
                            readers[key].session.close()
                        readers[key] = DeviceReader(device, args.clock_offset_seconds)
                    binding = {"branch_id": config["branch_id"], "device_id": device["id"],
                               "ip": device["ip_address"], "port": device["port"]}
                    path = directory / f"device-{config['branch_id']}-{key}.json"
                    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"binding": binding, "pending": None}
                    if state.get("binding") != binding:
                        raise RuntimeError("Device address changed; preserve old outbox and review checkpoint before resetting.")
                    cycle(website, readers[key], state, path, max(0.1, args.initial_lookback_hours))
                    failures[key] = 0
                    due[key] = 0
                except Exception as exc:
                    unsuccessful = True
                    failures[key] = failures.get(key, 0) + 1
                    delay = min(30, 2 ** min(failures[key], 5))
                    due[key] = time.monotonic() + delay
                    LOG.error("Device %s: %s; retry in %ss; saved data retained.", key, str(exc), delay)
            if args.once:
                return 1 if unsuccessful else 0
            # Maintain the interval, rather than adding a fixed sleep after work.
            time.sleep(max(0.1, max(1, args.poll_seconds) - (time.monotonic() - loop_start)))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOG.info("Automatic connector stopped.")
    except Exception as exc:
        LOG.error("Startup failed: %s", exc)
        raise SystemExit(1)
