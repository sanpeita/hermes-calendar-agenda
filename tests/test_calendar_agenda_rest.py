"""Adapter tests: the plugin reaches Calendar only through Hermes' grant."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from calendar_agenda_service import (
    CALENDAR_BASE_URL,
    CalendarAPIError,
    CalendarClient,
)

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self.body = json.dumps(payload if payload is not None else {}).encode("utf-8")
        self.headers = {}

    @property
    def text(self):
        return self.body.decode("utf-8")

    def json(self):
        return json.loads(self.body)


class RecordingTransport:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response or FakeResponse(200, {"items": []})
        self.error = error

    def __call__(self, provider, method, url, **kwargs):
        self.calls.append({"provider": provider, "method": method, "url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


class CalendarClientTests(unittest.TestCase):
    def test_list_events_asks_hermes_for_the_google_grant(self):
        transport = RecordingTransport()
        client = CalendarClient(request_fn=transport)

        client.list_events(time_min="2026-10-09T00:00:00+09:00", time_max="2026-10-10T00:00:00+09:00")

        call = transport.calls[0]
        self.assertEqual(call["provider"], "google")
        self.assertEqual(call["method"], "GET")
        parsed = urlparse(call["url"])
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", f"{CALENDAR_BASE_URL}/calendars/primary/events")
        query = parse_qs(parsed.query)
        self.assertEqual(query["timeMin"], ["2026-10-09T00:00:00+09:00"])
        self.assertEqual(query["timeMax"], ["2026-10-10T00:00:00+09:00"])
        self.assertEqual(query["singleEvents"], ["true"])
        self.assertEqual(query["orderBy"], ["startTime"])
        self.assertEqual(query["maxAttendees"], ["200"])
        # The plugin never carries a token of its own.
        self.assertNotIn("Authorization", (call.get("headers") or {}))

    def test_pagination_token_is_forwarded(self):
        transport = RecordingTransport()
        client = CalendarClient(request_fn=transport)
        client.list_events(time_min="a", time_max="b", page_token="p2")
        self.assertEqual(parse_qs(urlparse(transport.calls[0]["url"]).query)["pageToken"], ["p2"])

    def test_event_ids_are_url_escaped(self):
        transport = RecordingTransport(FakeResponse(200, {"id": "x"}))
        client = CalendarClient(request_fn=transport)
        client.get_event("weird id/with?chars")
        self.assertTrue(transport.calls[0]["url"].endswith("/events/weird%20id%2Fwith%3Fchars"))

    def test_patch_sends_if_match_and_send_updates(self):
        transport = RecordingTransport(FakeResponse(200, {"id": "e1", "etag": '"e2"'}))
        client = CalendarClient(request_fn=transport)

        client.patch_event("e1", {"end": {"dateTime": "2026-10-09T10:15:00+09:00"}}, etag='"etag-1"')

        call = transport.calls[0]
        self.assertEqual(call["method"], "PATCH")
        self.assertEqual(parse_qs(urlparse(call["url"]).query)["sendUpdates"], ["none"])
        self.assertEqual(call["headers"]["If-Match"], '"etag-1"')
        self.assertEqual(call["json"], {"end": {"dateTime": "2026-10-09T10:15:00+09:00"}})

    def test_patch_without_etag_omits_if_match(self):
        transport = RecordingTransport(FakeResponse(200, {"id": "e1"}))
        CalendarClient(request_fn=transport).patch_event("e1", {"extendedProperties": {}})
        self.assertNotIn("If-Match", transport.calls[0]["headers"])

    def test_http_error_status_raises_calendar_error(self):
        transport = RecordingTransport(FakeResponse(412, {"error": {"message": "Precondition Failed"}}))
        with self.assertRaises(CalendarAPIError) as caught:
            CalendarClient(request_fn=transport).get_event("e1")
        self.assertEqual(caught.exception.status, 412)
        self.assertIn("412", str(caught.exception))

    def test_timeout_propagates_as_timeout_error(self):
        transport = RecordingTransport(error=TimeoutError("read timed out"))
        with self.assertRaises(TimeoutError):
            CalendarClient(request_fn=transport).get_event("e1")


class NoBorrowedCredentialTests(unittest.TestCase):
    def test_service_never_reads_another_components_credentials(self):
        source = (ROOT / "calendar_agenda_service.py").read_text(encoding="utf-8")
        for forbidden in ("google_token", "google_api", "build_service", "hermes_constants", "_PROFILE_HOME"):
            self.assertNotIn(forbidden, source, f"{forbidden} would re-introduce the borrowed-credential path")

    def test_manifest_declares_the_oauth_client_and_its_variables(self):
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("oauth:", manifest)
        self.assertIn("https://www.googleapis.com/auth/calendar.events", manifest)
        self.assertIn("CALENDAR_AGENDA_GOOGLE_CLIENT_ID", manifest)
        self.assertIn("www.googleapis.com", manifest)
        self.assertIn("requires_hermes", manifest)
        self.assertNotIn(">=0.21.5", manifest)


if __name__ == "__main__":
    unittest.main()
