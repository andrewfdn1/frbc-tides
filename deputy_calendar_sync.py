#!/usr/bin/env python3
"""
Sync Deputy shift rosters into a single Google Calendar.

Fetches all assigned shifts across every staff member from Deputy for a
rolling date window, merges shifts that overlap (or touch) within the same
Deputy area into one calendar event per activity, and reconciles that set
against the target Google Calendar (creating/updating/deleting as needed)
so the calendar always reflects Deputy's current roster.

Intended to run as a standalone job (see
.github/workflows/deputy-calendar-sync.yml) rather than inside the Flask
app, since Render's free tier spins the dashboard down when idle.

Required environment variables:
    DEPUTY_API_BASE_URL          e.g. https://yourinstall.eu.deputy.com/api/v1
    DEPUTY_ACCESS_TOKEN          Deputy permanent access token
    GOOGLE_SERVICE_ACCOUNT_JSON  full JSON key of a Google service account
    SHIFTS_CALENDAR_ID           target Google Calendar ID (shared with the
                                  service account, "Make changes to events")

Optional:
    SYNC_DAYS_BEHIND   how many days into the past to include (default 1)
    SYNC_DAYS_AHEAD    how many days into the future to include (default 21)
    DRY_RUN            "1"/"true" to log planned changes without writing
"""

import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.service_account import Credentials as ServiceAccountCredentials

LONDON_TZ = ZoneInfo("Europe/London")

DEPUTY_API_BASE_URL         = os.environ["DEPUTY_API_BASE_URL"].rstrip("/")
DEPUTY_ACCESS_TOKEN         = os.environ["DEPUTY_ACCESS_TOKEN"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SHIFTS_CALENDAR_ID          = os.environ["SHIFTS_CALENDAR_ID"]

SYNC_DAYS_BEHIND = int(os.environ.get("SYNC_DAYS_BEHIND", "1"))
SYNC_DAYS_AHEAD  = int(os.environ.get("SYNC_DAYS_AHEAD", "21"))
DRY_RUN          = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

SOURCE_TAG            = "deputy-sync"
GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
GOOGLE_API_BASE        = "https://www.googleapis.com/calendar/v3"


# ---------------------------------------------------------------------------
# Deputy resource API
# ---------------------------------------------------------------------------

def _deputy_headers():
    # Deputy's REST API expects the permanent/OAuth token under the "OAuth"
    # scheme, not "Bearer" — this is a Deputy-specific quirk.
    return {
        "Authorization": f"OAuth {DEPUTY_ACCESS_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def deputy_query(resource, search=None):
    """POST .../resource/{resource}/QUERY, paginating past the 500-row window."""
    results = []
    start = 0
    window = 500
    while True:
        body = {"search": search or {}, "start": start, "max": window}
        r = requests.post(
            f"{DEPUTY_API_BASE_URL}/resource/{resource}/QUERY",
            headers=_deputy_headers(),
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        page = r.json()
        if not isinstance(page, list):
            raise ValueError(f"Unexpected Deputy {resource} response: {page!r}")
        results.extend(page)
        if len(page) < window:
            break
        start += window
    return results


def _ref_id(value):
    """Deputy relation fields may come back as a bare id or a nested object."""
    if isinstance(value, dict):
        value = value.get("Id")
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def fetch_employee_names():
    employees = deputy_query("Employee")
    names = {}
    for e in employees:
        eid = e.get("Id")
        if eid is None:
            continue
        display = e.get("DisplayName") or f"{e.get('FirstName', '')} {e.get('LastName', '')}".strip()
        names[eid] = display or f"Staff #{eid}"
    return names


def fetch_area_names():
    areas = deputy_query("OperationalUnit")
    return {a["Id"]: a.get("Name", "") for a in areas if a.get("Id") is not None}


def fetch_shifts(date_from, date_to):
    """All rostered-and-assigned shifts between date_from and date_to inclusive."""
    search = {
        "s1": {"field": "Date", "type": "ge", "data": date_from.isoformat()},
        "s2": {"field": "Date", "type": "le", "data": date_to.isoformat()},
    }
    rosters = deputy_query("Roster", search=search)

    shifts = []
    for r in rosters:
        if r.get("Open"):
            continue  # unfilled shift — no staff assigned
        emp_id = _ref_id(r.get("Employee"))
        if not emp_id:
            continue
        start_ts = r.get("StartTime")
        end_ts = r.get("EndTime")
        if not start_ts or not end_ts:
            continue
        shifts.append({
            "employee_id": emp_id,
            "area_id": _ref_id(r.get("OperationalUnit")),
            "start": datetime.fromtimestamp(int(start_ts), tz=timezone.utc).astimezone(LONDON_TZ),
            "end": datetime.fromtimestamp(int(end_ts), tz=timezone.utc).astimezone(LONDON_TZ),
        })
    return shifts


# ---------------------------------------------------------------------------
# Merge overlapping/touching shifts in the same area into single blocks
# ---------------------------------------------------------------------------

def merge_shifts(shifts, employee_names, area_names):
    by_group = defaultdict(list)
    for s in shifts:
        by_group[(s["start"].date(), s["area_id"])].append(s)

    raw_blocks = []
    for (_shift_date, area_id), group in by_group.items():
        group.sort(key=lambda s: s["start"])
        current_members = []
        current_end = None
        for s in group:
            if current_members and s["start"] <= current_end:
                current_members.append(s)
                current_end = max(current_end, s["end"])
            else:
                if current_members:
                    raw_blocks.append((area_id, current_members))
                current_members = [s]
                current_end = s["end"]
        if current_members:
            raw_blocks.append((area_id, current_members))

    blocks = []
    for area_id, members in raw_blocks:
        start = min(m["start"] for m in members)
        end = max(m["end"] for m in members)
        area_name = area_names.get(area_id, "") if area_id else ""
        staff_names = sorted({
            employee_names.get(m["employee_id"], f"Staff #{m['employee_id']}")
            for m in members
        })
        lines = []
        for m in sorted(members, key=lambda m: m["start"]):
            name = employee_names.get(m["employee_id"], f"Staff #{m['employee_id']}")
            lines.append(f"{m['start'].strftime('%H:%M')}-{m['end'].strftime('%H:%M')}  {name}")
        blocks.append({
            "area_id": area_id,
            "area_name": area_name,
            "start": start,
            "end": end,
            "staff": staff_names,
            "lines": lines,
        })
    return blocks


def event_id_for_block(block):
    """Deterministic Google Calendar event id: base32hex charset only (a-v0-9),
    derived from area + time span so re-syncing the same shift is idempotent
    while a change in time or staff assignment on that shift correctly maps to
    a different id (old one gets cleaned up, new one created)."""
    key = f"{block['area_id']}|{block['start'].isoformat()}|{block['end'].isoformat()}"
    digest = hashlib.sha1(key.encode()).hexdigest()  # hex chars are all within a-v0-9
    return f"dep{digest}"


# ---------------------------------------------------------------------------
# Google Calendar API
# ---------------------------------------------------------------------------

def google_access_token():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = ServiceAccountCredentials.from_service_account_info(info, scopes=GOOGLE_CALENDAR_SCOPES)
    creds.refresh(GoogleAuthRequest())
    return creds.token


def _gcal_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _events_url(suffix=""):
    cal = requests.utils.quote(SHIFTS_CALENDAR_ID, safe="")
    return f"{GOOGLE_API_BASE}/calendars/{cal}/events{suffix}"


def list_synced_events(token, time_min, time_max):
    """All events we previously created (tagged with our private extendedProperty)
    that fall within the sync window, keyed by event id."""
    events = {}
    page_token = None
    while True:
        params = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": "true",
            "privateExtendedProperty": f"source={SOURCE_TAG}",
            "maxResults": 250,
        }
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(_events_url(), headers=_gcal_headers(token), params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        for item in data.get("items", []):
            events[item["id"]] = item
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return events


def build_event_body(block):
    area_label = block["area_name"] or "Shift"
    return {
        "summary": f"{area_label} — {', '.join(block['staff'])}",
        "location": block["area_name"],
        "description": "\n".join(block["lines"]),
        "start": {"dateTime": block["start"].isoformat(), "timeZone": "Europe/London"},
        "end": {"dateTime": block["end"].isoformat(), "timeZone": "Europe/London"},
        "extendedProperties": {"private": {"source": SOURCE_TAG}},
    }


def upsert_event(token, event_id, body, already_exists):
    if already_exists:
        r = requests.put(_events_url(f"/{event_id}"), headers=_gcal_headers(token), json=body, timeout=20)
        if r.status_code == 404:
            already_exists = False  # fall through to insert
        else:
            r.raise_for_status()
            return
    if not already_exists:
        r = requests.post(_events_url(), headers=_gcal_headers(token), json=dict(body, id=event_id), timeout=20)
        r.raise_for_status()


def delete_event(token, event_id):
    r = requests.delete(_events_url(f"/{event_id}"), headers=_gcal_headers(token), timeout=20)
    if r.status_code not in (200, 204, 404, 410):
        r.raise_for_status()


# ---------------------------------------------------------------------------

def main():
    today = datetime.now(LONDON_TZ).date()
    date_from = today - timedelta(days=SYNC_DAYS_BEHIND)
    date_to = today + timedelta(days=SYNC_DAYS_AHEAD)

    print(f"Fetching Deputy shifts {date_from} to {date_to}...")
    employee_names = fetch_employee_names()
    area_names = fetch_area_names()
    shifts = fetch_shifts(date_from, date_to)
    print(f"{len(shifts)} assigned shifts fetched across {len(employee_names)} staff")

    blocks = merge_shifts(shifts, employee_names, area_names)
    print(f"Merged into {len(blocks)} deduplicated calendar events")

    token = google_access_token()
    time_min = datetime.combine(date_from, datetime.min.time(), tzinfo=LONDON_TZ)
    time_max = datetime.combine(date_to + timedelta(days=1), datetime.min.time(), tzinfo=LONDON_TZ)
    existing = list_synced_events(token, time_min, time_max)
    print(f"{len(existing)} previously-synced events currently on the calendar in this window")

    seen_ids = set()
    created = updated = 0
    for block in blocks:
        event_id = event_id_for_block(block)
        seen_ids.add(event_id)
        body = build_event_body(block)
        already_exists = event_id in existing
        if DRY_RUN:
            action = "update" if already_exists else "create"
            print(f"[dry-run] would {action} {event_id}: {body['summary']} "
                  f"{block['start'].strftime('%a %d %b %H:%M')}-{block['end'].strftime('%H:%M')}")
            continue
        upsert_event(token, event_id, body, already_exists)
        if already_exists:
            updated += 1
        else:
            created += 1

    stale_ids = [eid for eid in existing if eid not in seen_ids]
    for eid in stale_ids:
        if DRY_RUN:
            print(f"[dry-run] would delete stale event {eid} ({existing[eid].get('summary', '')})")
            continue
        delete_event(token, eid)

    print(f"Done. created={created} updated={updated} deleted={len(stale_ids)} dry_run={DRY_RUN}")


if __name__ == "__main__":
    main()
