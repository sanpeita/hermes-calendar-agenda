"""Google Calendar reads and two-axis agenda state updates.

Credentials never live in this plugin. Every call goes through Hermes'
``credentialed_provider_request`` using the OAuth client this plugin declares
under ``oauth:`` in ``plugin.yaml`` (``hermes auth add calendar-agenda:google``).
The plugin never reads, refreshes or rewrites a token file, and never imports
another component's helper script.

State is stored only in each event's private extended properties. The standard
Google Calendar event status and visible event title are never repurposed.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

TIME_ZONE_NAME = "Asia/Tokyo"
TIME_ZONE = ZoneInfo(TIME_ZONE_NAME)
CALENDAR_ID = "primary"
OUTCOME_KEY = "hermesAgendaOutcome"
KEEP_SLOT_KEY = "hermesAgendaKeepSlot"
UPDATED_AT_KEY = "hermesAgendaUpdatedAt"
ORIGINAL_END_KEY = "hermesAgendaOriginalEnd"
VALID_OUTCOMES = frozenset({"done", "interrupted", "cancelled", "skipped"})

# The oauth: entry declared in plugin.yaml. Hermes attaches the token; this
# plugin only ever names the client id.
PROVIDER_ID = "google"
CALENDAR_BASE_URL = "https://www.googleapis.com/calendar/v3"
EVENTS_PATH = "/calendars/{calendar_id}/events"
REQUEST_TIMEOUT_SECONDS = 20
FETCH_ATTEMPTS = 2


class CalendarAPIError(RuntimeError):
    """A Calendar REST call returned a non-2xx status."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"Google Calendar returned HTTP {status}: {detail}")


class ProviderSignedOut(RuntimeError):
    """No usable sign-in for the plugin's oauth client in this profile."""


def _detail(response: Any) -> str:
    text = getattr(response, "text", "") or ""
    text = " ".join(str(text).split())
    return text[:300] if text else "(no body)"


def _default_request_fn(provider: str, method: str, url: str, **kwargs: Any) -> Any:
    """Hand the request to Hermes, which attaches the sign-in itself.

    Imported lazily so the module still imports (and the tests still run) outside
    a Hermes process that has the plugin-provider surface.
    """
    from hermes_cli.plugin_provider_requests import (
        ProviderNotSignedIn,
        credentialed_provider_request,
    )

    try:
        return credentialed_provider_request(provider, method, url, **kwargs)
    except ProviderNotSignedIn as exc:
        raise ProviderSignedOut(
            "Calendar Agenda is not signed in. Run `hermes auth add calendar-agenda:google` "
            "in your own terminal."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - re-raised detached below
        if isinstance(exc, TimeoutError) or type(exc).__name__.endswith("TimeoutException"):
            raise TimeoutError(str(exc)) from None
        raise


class CalendarClient:
    """Minimal Calendar v3 REST client whose only credential is Hermes' grant."""

    def __init__(
        self,
        *,
        provider: str = PROVIDER_ID,
        base_url: str = CALENDAR_BASE_URL,
        calendar_id: str = CALENDAR_ID,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        request_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.calendar_id = calendar_id
        self.timeout = timeout
        self._request_fn = request_fn or _default_request_fn

    # -- transport -------------------------------------------------------
    def _url(self, path: str, params: dict[str, Any] | None) -> str:
        url = f"{self.base_url}{path}"
        clean = {key: value for key, value in (params or {}).items() if value is not None}
        return f"{url}?{urlencode(clean)}" if clean else url

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = self._url(path, params)
        response = self._request_fn(
            self.provider, method, url, json=body, headers=headers, timeout=self.timeout
        )
        if getattr(response, "status", 0) >= 400:
            raise CalendarAPIError(int(response.status), _detail(response))
        return response

    def _event_path(self) -> str:
        return EVENTS_PATH.format(calendar_id=quote(self.calendar_id, safe=""))

    def _event_url(self, event_id: str) -> str:
        return f"{self._event_path()}/{quote(str(event_id), safe='')}"

    # -- Calendar v3 surface ---------------------------------------------
    def list_events(self, *, time_min: str, time_max: str, page_token: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": 2500,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxAttendees": 200,
        }
        if page_token:
            params["pageToken"] = page_token
        return self._call("GET", self._event_path(), params=params).json()

    def get_event(self, event_id: str) -> dict[str, Any]:
        return self._call("GET", self._event_url(event_id)).json()

    def patch_event(
        self,
        event_id: str,
        body: dict[str, Any],
        *,
        etag: str | None = None,
        send_updates: str = "none",
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if etag:
            headers["If-Match"] = etag
        params = {"sendUpdates": send_updates} if send_updates else None
        return self._call(
            "PATCH", self._event_url(event_id), params=params, body=body, headers=headers
        ).json()


# ---------------------------------------------------------------------------
# Event projection helpers
# ---------------------------------------------------------------------------

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
        response = api.list_events(time_min=_rfc3339(start), time_max=_rfc3339(end), page_token=page_token)
        events.extend(
            _event_for_display(event)
            for event in response.get("items", [])
            if event.get("status") != "cancelled"
        )
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return {"date": today.isoformat(), "timeZone": TIME_ZONE_NAME, "events": events}


def list_today_events(*, now: datetime | None = None, api: Any = None) -> dict[str, Any]:
    """Return today's primary-calendar agenda, bounded by Asia/Tokyo midnight.

    A caller-supplied ``api`` is used as-is. Otherwise a fresh client is used and one
    TimeoutError is retried once, so a stalled request costs one short attempt
    instead of surfacing a 503 to the agenda pane.
    """
    current = now or datetime.now(TIME_ZONE)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone")
    today = current.astimezone(TIME_ZONE).date()
    start = datetime.combine(today, time.min, tzinfo=TIME_ZONE)
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=TIME_ZONE)

    if api is not None:
        return _fetch_agenda(api, today, start, end)

    last: TimeoutError | None = None
    client = _client()
    for attempt in range(FETCH_ATTEMPTS):
        try:
            return _fetch_agenda(client, today, start, end)
        except TimeoutError as exc:
            last = exc
            logger.warning(
                "Calendar Agenda read timed out (attempt %d/%d, %ss budget)",
                attempt + 1, FETCH_ATTEMPTS, REQUEST_TIMEOUT_SECONDS,
            )
            client = _client()  # a fresh attempt, not one sharing the stalled state
    assert last is not None
    raise last


def _client() -> CalendarClient:
    return CalendarClient()


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


def set_event_state(
    event_id: str,
    outcome: str,
    keep_slot: bool,
    *,
    actual_end: str | None = None,
    now: datetime | None = None,
    api: Any = None,
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

    client = api or _client()
    event = client.get_event(event_id)
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

    client.patch_event(event_id, body, etag=event.get("etag"))

    verified = client.get_event(event_id)
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
