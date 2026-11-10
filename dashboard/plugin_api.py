"""Read-only agenda endpoint for the Desktop pane plus explicit state updates."""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import sys
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SERVICE_MODULE = "hermes_calendar_agenda_service_" + hashlib.sha256(
    str(_PLUGIN_ROOT).encode("utf-8")
).hexdigest()[:16]
_SERVICE_LOCK = threading.RLock()


def _service_module():
    with _SERVICE_LOCK:
        loaded = sys.modules.get(_SERVICE_MODULE)
        if loaded is not None:
            return loaded
        service_path = _PLUGIN_ROOT / "calendar_agenda_service.py"
        spec = importlib.util.spec_from_file_location(_SERVICE_MODULE, service_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Calendar Agenda service module is missing")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_SERVICE_MODULE] = module
        try:
            spec.loader.exec_module(module)
        except (Exception, SystemExit):
            sys.modules.pop(_SERVICE_MODULE, None)
            raise
        return module


class StateUpdate(BaseModel):
    outcome: Literal["done", "interrupted", "cancelled", "skipped"]
    keep_slot: bool
    actual_end: str | None = None


@router.get("/today")
def today():
    try:
        return _service_module().list_today_events()
    except (Exception, SystemExit) as exc:
        logger.error("Calendar Agenda read failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Google Calendar is temporarily unavailable") from exc


@router.patch("/events/{event_id}/state")
def update_state(event_id: str, body: StateUpdate):
    try:
        return _service_module().set_event_state(
            event_id=event_id,
            outcome=body.outcome,
            keep_slot=body.keep_slot,
            actual_end=body.actual_end,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (Exception, SystemExit) as exc:
        logger.error("Calendar Agenda update failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Calendar state update failed") from exc
