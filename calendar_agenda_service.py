"""Google Calendar reads and two-axis agenda state updates.

State is stored only in each event's private extended properties. The standard
Google Calendar event status and visible event title are never repurposed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import sys
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hermes_constants import reset_hermes_home_override, set_hermes_home_override

# This installed plugin belongs to this home, not to whichever request last
# changed the host's active profile. Never use default-profile credentials.
_PROFILE_HOME = Path(__file__).resolve().parents[2]
_HELPER_MODULE_NAME = "hermes_calendar_agenda_google_api_" + hashlib.sha256(
    str(_PROFILE_HOME).encode("utf-8")
).hexdigest()[:16]
_HELPER_LOCK = threading.RLock()
logger = logging.getLogger(__name__)

TIME_ZONE_NAME = "Asia/Tokyo"
TIME_ZONE = ZoneInfo(TIME_ZONE_NAME)
CALENDAR_ID = "primary"
OUTCOME_KEY = "hermesAgendaOutcome"
KEEP_SLOT_KEY = "hermesAgendaKeepSlot"
UPDATED_AT_KEY = "hermesAgendaUpdatedAt"
ORIGINAL_END_KEY = "hermesAgendaOriginalEnd"
VALID_OUTCOMES = frozenset({"done", "interrupted", "cancelled", "skipped"})


def _google_api_module():
    """Load the owning profile's helper under a thread-local home override."""
    with _HELPER_LOCK:
        loaded = sys.modules.get(_HELPER_MODULE_NAME)
        if loaded is not None:
            return loaded

        script = (
            _PROFILE_HOME / "skills" / "productivity" / "google-workspace"
            / "scripts" / "google_api.py"
        )
        if not script.is_file():
            raise RuntimeError("Google Workspace skill script is not installed for this Hermes profile")

        spec = importlib.util.spec_from_file_location(_HELPER_MODULE_NAME, script)
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load the Google Workspace API helper")
        module = importlib.util.module_from_spec(spec)
        token = set_hermes_home_override(_PROFILE_HOME)
        sys.modules[_HELPER_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except (Exception, SystemExit):
            sys.modules.pop(_HELPER_MODULE_NAME, None)
            raise
        finally:
            reset_hermes_home_override(token)
        return module


# googleapiclient's build_http() bakes DEFAULT_HTTP_TIMEOUT_SEC = 60 into the
# httplib2 transport and nothing else in this path sets a timeout, so one stalled
# socket held a /today request for a full minute before the pane got a 503
# (observed as "Calendar Agenda read failed (TimeoutError)"). The plugin owns its
# request budget, so clamp the transport and retry once instead.
SERVICE_TIMEOUT_SECONDS = 20
FETCH_ATTEMPTS = 2
_SERVICE_LOCK = threading.RLock()
_SERVICE_CACHE: dict[str, Any] = {}


def _clamp_transport_timeout(service: Any) -> None:
    """Best-effort clamp of the built client's socket timeout.

    httplib2 reads ``.timeout`` per request (``sock.settimeout(self.timeout)``), so
    setting it after ``build_service()`` sticks. A fixture service with no transport
    (the offline profile-isolation tests return a Path) is left untouched.
    """
    transport = getattr(service, "_http", None)
    http = getattr(transport, "http", transport)
    if http is not None and hasattr(http, "timeout"):
        try:
            http.timeout = SERVICE_TIMEOUT_SECONDS
        except Exception:  # a read-only transport must not break the read path
            logger.debug("Calendar Agenda: could not clamp the transport timeout", exc_info=True)


def _calendar_service(*, fresh: bool = False):
    """The profile's Calendar client, built once per process.

    Caching keeps one transport (and its connection pool) for the process instead of
    a fresh client per request; ``fresh=True`` discards it so a retry after a timeout
    does not reuse a connection the stall poisoned.
    """
    with _SERVICE_LOCK:
        if fresh:
            _SERVICE_CACHE.pop("calendar", None)
        cached = _SERVICE_CACHE.get("calendar")
        if cached is not None:
            return cached
        service = _google_api_module().build_service("calendar", "v3")
        _clamp_transport_timeout(service)
        _SERVICE_CACHE["calendar"] = service
        return service


def _parse_event_datetime(value: dict[str, Any]) -> datetime | None:
    raw = value.get("dateTime")
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(value.get("timeZone") or TIME_ZONE_NAME))
    return parsed


def _rfc3339(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _private_properties(event: dict[str, Any]) -> dict[str, str]:
    properties = event.get("extendedProperties") or {}
    private = properties.get("private") or {}
    return dict(private) if isinstance(private, dict) else {}


def _has_guests(event: dict[str, Any]) -> bool:
    if event.get("attendeesOmitted"):
        return True
    attendees = event.get("attendees") or []
    return any(not (person.get("self") or person.get("organizer")) for person in attendees)


def _can_change_time(event: dict[str, Any]) -> bool:
    """Only shorten/restore an event owned by the user with no other invitees."""
    organizer = event.get("organizer") or {}
    return not event.get("attendeesOmitted") and not _has_guests(event) and organizer.get("self") is True


def _state_from_event(event: dict[str, Any]) -> dict[str, Any]:
    private = _private_properties(event)
    result = private.get(OUTCOME_KEY) or None
    keep_raw = private.get(KEEP_SLOT_KEY)
    keep_slot = None if keep_raw is None else str(keep_raw).lower() == "true"
    return {
        "result": result,
        "keepSlot": keep_slot,
        "updatedAt": private.get(UPDATED_AT_KEY) or None,
    }


def _event_for_display(event: dict[str, Any]) -> dict[str, Any]:
    start = event.get("start") or {}
    end = event.get("end") or {}
    return {
        "id": event.get("id", ""),
        "summary": event.get("summary") or "(タイトルなし)",
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
        "allDay": bool(start.get("date") and not start.get("dateTime")),
        "state": _state_from_event(event),
        "hasAttendees": _has_guests(event),
        "timeChangeAllowed": _can_change_time(event),
    }


def _fetch_agenda(api: Any, today: date, start: datetime, end: datetime) -> dict[str, Any]:
    """Page through today's events on an already-resolved client."""
    events: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = api.events().list(
            calendarId=CALENDAR_ID,
            timeMin=_rfc3339(start),
            timeMax=_rfc3339(end),
            maxResults=2500,
            singleEvents=True,
            orderBy="startTime",
            maxAttendees=200,
            **({"pageToken": page_token} if page_token else {}),
        ).execute()
        events.extend(
            _event_for_display(event)
            for event in response.get("items", [])
            if event.get("status") != "cancelled"
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return {"date": today.isoformat(), "timeZone": TIME_ZONE_NAME, "events": events}


def list_today_events(*, now: datetime | None = None, service=None) -> dict[str, Any]:
    """Return today's primary-calendar agenda, bounded by Asia/Tokyo midnight.

    A caller-supplied ``service`` is used as-is. Otherwise the process-wide client is
    used and a single TimeoutError is retried on a freshly built one, so a stalled
    connection costs one short attempt instead of surfacing a 503 to the agenda pane.
    """
    current = now or datetime.now(TIME_ZONE)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone")
    today = current.astimezone(TIME_ZONE).date()
    start = datetime.combine(today, time.min, tzinfo=TIME_ZONE)
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=TIME_ZONE)

    if service is not None:
        return _fetch_agenda(service, today, start, end)

    last: TimeoutError | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            return _fetch_agenda(_calendar_service(fresh=attempt > 0), today, start, end)
        except TimeoutError as exc:
            last = exc
            logger.warning(
                "Calendar Agenda read timed out (attempt %d/%d, %ss budget)",
                attempt + 1, FETCH_ATTEMPTS, SERVICE_TIMEOUT_SECONDS,
            )
    assert last is not None
    raise last


def _parse_actual_end(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("actual_end must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("actual_end must include a timezone offset")
    return parsed


def _set_if_match(request, etag: str | None) -> None:
    headers = getattr(request, "headers", None)
    if etag and isinstance(headers, dict):
        headers["If-Match"] = etag


def set_event_state(
    event_id: str,
    outcome: str,
    keep_slot: bool,
    *,
    actual_end: str | None = None,
    now: datetime | None = None,
    service=None,
) -> dict[str, Any]:
    """Record the independent outcome/slot axes and make the approved time adjustment.

    Personal events with a clear owner and no guests are shortened when a slot is
    released during the event. Their exact original end is retained privately and
    restored if the slot is later marked as retained. Guest/shared event times are
    never changed; only the private Hermes state is updated.
    """
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 255:
        raise ValueError("event_id must be a non-empty Google Calendar event ID")
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"outcome must be one of: {', '.join(sorted(VALID_OUTCOMES))}")
    if type(keep_slot) is not bool:
        raise ValueError("keep_slot must be a boolean")

    api = service or _calendar_service()
    event = api.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
    if event.get("status") == "cancelled":
        raise ValueError("Cancelled Google Calendar events cannot receive agenda state")

    current = now or datetime.now(TIME_ZONE)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone")
    action_time = _parse_actual_end(actual_end, current)
    private = _private_properties(event)
    old_end = event.get("end") or {}
    new_end: dict[str, Any] | None = None
    time_change_reason: str | None = None

    if keep_slot:
        saved_end = private.get(ORIGINAL_END_KEY)
        if saved_end:
            try:
                original_end = json.loads(saved_end)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("Saved original event end is invalid; refusing to overwrite the event") from exc
            if not isinstance(original_end, dict) or not original_end.get("dateTime"):
                raise ValueError("Saved original event end is not a timed event; refusing to overwrite it")
            if _can_change_time(event) and old_end != original_end:
                new_end = original_end
            elif not _can_change_time(event):
                time_change_reason = "shared_or_not_owned"
        else:
            time_change_reason = "slot_retained"
    elif _can_change_time(event):
        start_value = event.get("start") or {}
        start_dt = _parse_event_datetime(start_value)
        end_dt = _parse_event_datetime(old_end)
        if start_value.get("date") and not start_value.get("dateTime"):
            time_change_reason = "all_day_event"
        elif start_dt is None or end_dt is None:
            time_change_reason = "event_has_no_timed_interval"
        else:
            original_end = private.get(ORIGINAL_END_KEY)
            if action_time.tzinfo is None:
                raise ValueError("actual_end must include a timezone")
            # A previous shortening remains the lower bound: repeated updates must
            # never extend a released slot merely because the agent ran later.
            if start_dt < action_time < end_dt:
                if not original_end:
                    private[ORIGINAL_END_KEY] = json.dumps(old_end, ensure_ascii=False, separators=(",", ":"))
                zone_name = old_end.get("timeZone") or TIME_ZONE_NAME
                shortened = action_time.astimezone(ZoneInfo(zone_name))
                new_end = {"dateTime": _rfc3339(shortened)}
                if old_end.get("timeZone"):
                    new_end["timeZone"] = old_end["timeZone"]
            else:
                time_change_reason = "actual_end_outside_remaining_event_time"
    else:
        time_change_reason = "shared_or_not_owned"

    private[OUTCOME_KEY] = outcome
    private[KEEP_SLOT_KEY] = "true" if keep_slot else "false"
    private[UPDATED_AT_KEY] = _rfc3339(current.astimezone(TIME_ZONE))
    extended = dict(event.get("extendedProperties") or {})
    extended["private"] = private
    body: dict[str, Any] = {"extendedProperties": extended}
    if new_end is not None:
        body["end"] = new_end

    request = api.events().patch(
        calendarId=CALENDAR_ID,
        eventId=event_id,
        body=body,
        sendUpdates="none",
    )
    _set_if_match(request, event.get("etag"))
    request.execute()

    verified = api.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
    verified_private = _private_properties(verified)
    if verified_private.get(OUTCOME_KEY) != outcome or verified_private.get(KEEP_SLOT_KEY) != ("true" if keep_slot else "false"):
        raise RuntimeError("Calendar state update could not be verified")
    if new_end is not None and (verified.get("end") or {}) != new_end:
        raise RuntimeError("Calendar end-time update could not be verified")

    verified_end = verified.get("end") or {}
    return {
        "eventId": event_id,
        "summary": verified.get("summary") or "(タイトルなし)",
        "start": (verified.get("start") or {}).get("dateTime") or (verified.get("start") or {}).get("date") or "",
        "outcome": outcome,
        "keepSlot": keep_slot,
        "calendarEndChanged": new_end is not None,
        "calendarEnd": verified_end.get("dateTime") or verified_end.get("date") or "",
        "originalEndSaved": bool(verified_private.get(ORIGINAL_END_KEY)),
        "timeChangeReason": time_change_reason,
        "verified": True,
    }
