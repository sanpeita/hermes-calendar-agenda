from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from calendar_agenda_service import (
    KEEP_SLOT_KEY,
    ORIGINAL_END_KEY,
    OUTCOME_KEY,
    list_today_events,
    set_event_state,
)

JST = ZoneInfo("Asia/Tokyo")


class FakeAPI:
    """The CalendarClient surface the service depends on, without any transport."""

    def __init__(self, events):
        self.events_by_id = {event["id"]: copy.deepcopy(event) for event in events}
        self.list_args = None
        self.patches = []

    def list_events(self, *, time_min, time_max, page_token=None):
        self.list_args = {"timeMin": time_min, "timeMax": time_max, "pageToken": page_token}
        return {"items": [copy.deepcopy(event) for event in self.events_by_id.values()]}

    def get_event(self, event_id):
        return copy.deepcopy(self.events_by_id[event_id])

    def patch_event(self, event_id, body, *, etag=None, send_updates="none"):
        self.patches.append({"body": copy.deepcopy(body), "etag": etag, "sendUpdates": send_updates})
        self.events_by_id[event_id].update(copy.deepcopy(body))
        return copy.deepcopy(self.events_by_id[event_id])


def _personal_event():
    return {
        "id": "personal-1",
        "etag": '"etag-1"',
        "status": "confirmed",
        "summary": "制作時間",
        "start": {"dateTime": "2026-10-09T09:00:00+09:00", "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": "2026-10-09T12:00:00+09:00", "timeZone": "Asia/Tokyo"},
        "organizer": {"self": True},
        "extendedProperties": {"private": {"otherPrivate": "keep-me"}, "shared": {"otherShared": "keep-me-too"}},
    }


class CalendarAgendaServiceTests(unittest.TestCase):
    def test_list_today_uses_tokyo_midnight_and_hides_cancelled_items(self):
        event = _personal_event()
        cancelled = dict(event, id="cancelled", status="cancelled")
        api = FakeAPI([event, cancelled])
        result = list_today_events(now=datetime(2026, 10, 9, 10, tzinfo=JST), api=api)

        self.assertEqual(result["date"], "2026-10-09")
        self.assertEqual(result["timeZone"], "Asia/Tokyo")
        self.assertEqual([item["id"] for item in result["events"]], ["personal-1"])
        self.assertEqual(api.list_args["timeMin"], "2026-10-09T00:00:00+09:00")
        self.assertEqual(api.list_args["timeMax"], "2026-10-10T00:00:00+09:00")

    def test_list_today_follows_page_tokens(self):
        class PagedAPI(FakeAPI):
            def list_events(self, *, time_min, time_max, page_token=None):
                if page_token is None:
                    return {"items": [dict(_personal_event(), id="a")], "nextPageToken": "p2"}
                return {"items": [dict(_personal_event(), id="b")]}

        result = list_today_events(now=datetime(2026, 10, 9, 10, tzinfo=JST), api=PagedAPI([]))
        self.assertEqual([item["id"] for item in result["events"]], ["a", "b"])

    def test_personal_event_shortens_and_saves_exact_original_end(self):
        event = _personal_event()
        api = FakeAPI([event])
        now = datetime(2026, 10, 9, 10, 15, tzinfo=JST)
        result = set_event_state("personal-1", "done", False, now=now, api=api)
        updated = api.events_by_id["personal-1"]
        private = updated["extendedProperties"]["private"]

        self.assertEqual(updated["end"]["dateTime"], "2026-10-09T10:15:00+09:00")
        self.assertEqual(json.loads(private[ORIGINAL_END_KEY]), event["end"])
        self.assertEqual(private[OUTCOME_KEY], "done")
        self.assertEqual(private[KEEP_SLOT_KEY], "false")
        self.assertEqual(updated["extendedProperties"]["shared"], {"otherShared": "keep-me-too"})
        self.assertEqual(api.patches[-1]["sendUpdates"], "none")
        self.assertEqual(api.patches[-1]["etag"], '"etag-1"')
        self.assertEqual(api.patches[-1]["body"]["end"]["dateTime"], "2026-10-09T10:15:00+09:00")
        self.assertTrue(result["verified"])
        self.assertTrue(result["calendarEndChanged"])
        self.assertTrue(result["originalEndSaved"])

    def test_guest_event_only_changes_private_state(self):
        event = _personal_event()
        event["attendees"] = [{"email": "guest@example.test", "self": False}]
        api = FakeAPI([event])
        result = set_event_state(
            "personal-1", "interrupted", False,
            now=datetime(2026, 10, 9, 10, 15, tzinfo=JST),
            api=api,
        )

        updated = api.events_by_id["personal-1"]
        self.assertEqual(updated["end"], event["end"])
        self.assertEqual(updated["extendedProperties"]["private"][OUTCOME_KEY], "interrupted")
        self.assertNotIn("end", api.patches[-1]["body"])
        self.assertFalse(result["calendarEndChanged"])
        self.assertEqual(result["timeChangeReason"], "shared_or_not_owned")

    def test_retained_slot_restores_previously_shortened_end(self):
        event = _personal_event()
        original_end = copy.deepcopy(event["end"])
        event["end"] = {"dateTime": "2026-10-09T10:15:00+09:00", "timeZone": "Asia/Tokyo"}
        event["extendedProperties"]["private"].update({
            ORIGINAL_END_KEY: json.dumps(original_end, separators=(",", ":")),
            KEEP_SLOT_KEY: "false",
        })
        api = FakeAPI([event])

        result = set_event_state(
            "personal-1", "done", True,
            now=datetime(2026, 10, 9, 10, 30, tzinfo=JST),
            api=api,
        )

        updated = api.events_by_id["personal-1"]
        self.assertEqual(updated["end"], original_end)
        self.assertEqual(updated["extendedProperties"]["private"][KEEP_SLOT_KEY], "true")
        self.assertTrue(result["calendarEndChanged"])

    def test_naive_actual_end_is_rejected(self):
        api = FakeAPI([_personal_event()])
        with self.assertRaisesRegex(ValueError, "timezone"):
            set_event_state(
                "personal-1", "done", False,
                actual_end="2026-10-09T10:15:00",
                api=api,
            )


if __name__ == "__main__":
    unittest.main()
