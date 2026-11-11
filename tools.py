"""Agent tools for reading today's agenda and updating its private state."""

from __future__ import annotations

import json
import logging
from typing import Any

from .calendar_agenda_service import list_today_events, set_event_state

logger = logging.getLogger(__name__)
TOOLSET = "calendar_agenda"

LIST_TODAY_SCHEMA = {
    "name": "calendar_agenda_list_today",
    "description": (
        "Read today's Google Calendar agenda in Asia/Tokyo. Returns event IDs, titles, "
        "times and the Hermes outcome/slot state; it does not return descriptions or locations."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SET_STATE_SCHEMA = {
    "name": "calendar_agenda_set_state",
    "description": (
        "Record a Google Calendar event's result and whether its schedule slot should remain as two "
        "independent state fields. Use only when the user requests a state change for a specific event. "
        "Results: done, interrupted, cancelled, skipped. 'Frozen' means keep_slot=true. "
        "For a personal event owned by the user, keep_slot=false shortens the event end to actual_end "
        "(or the current time if omitted) only if that time is inside the remaining timed interval; the "
        "original end is saved and can be restored by setting keep_slot=true later. Events with guests, "
        "all-day events, or events not clearly owned by the user never have their calendar times changed; "
        "their private Hermes state alone is updated. The visible title and Google event status are never changed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": "Exact Google Calendar event ID from calendar_agenda_list_today.",
            },
            "outcome": {
                "type": "string",
                "enum": ["done", "interrupted", "cancelled", "skipped"],
                "description": "What happened to the planned activity; 'frozen' is represented separately by keep_slot=true.",
            },
            "keep_slot": {
                "type": "boolean",
                "description": "True to preserve the scheduled time block (凍結); false to release it where safe.",
            },
            "actual_end": {
                "type": "string",
                "description": "Optional ISO 8601 timestamp with timezone offset for when work actually ended; defaults to now.",
            },
        },
        "required": ["event_id", "outcome", "keep_slot"],
    },
}


def _json_result(value: Any) -> str:
    return json.dumps({"result": value}, ensure_ascii=False)


def _json_error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _handle_list_today(args: dict, **kwargs) -> str:
    try:
        return _json_result(list_today_events())
    except (Exception, SystemExit) as exc:
        logger.error("calendar_agenda_list_today failed (%s)", type(exc).__name__)
        return _json_error(
            f"Could not read Google Calendar ({type(exc).__name__}). "
            "If this profile has never signed in, run `hermes auth add calendar-agenda:google`."
        )


def _handle_set_state(args: dict, **kwargs) -> str:
    try:
        result = set_event_state(
            event_id=args.get("event_id", ""),
            outcome=args.get("outcome", ""),
            keep_slot=args.get("keep_slot"),
            actual_end=args.get("actual_end"),
        )
        return _json_result(result)
    except ValueError as exc:
        return _json_error(str(exc))
    except (Exception, SystemExit) as exc:
        logger.error("calendar_agenda_set_state failed (%s)", type(exc).__name__)
        return _json_error(f"Calendar state update failed ({type(exc).__name__}); no successful update is being claimed.")


def register_tools(ctx) -> None:
    ctx.register_tool(
        name=LIST_TODAY_SCHEMA["name"],
        toolset=TOOLSET,
        schema=LIST_TODAY_SCHEMA,
        handler=_handle_list_today,
        emoji="🗓️",
    )
    ctx.register_tool(
        name=SET_STATE_SCHEMA["name"],
        toolset=TOOLSET,
        schema=SET_STATE_SCHEMA,
        handler=_handle_set_state,
        emoji="🗓️",
    )
