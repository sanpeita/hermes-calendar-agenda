"""Regression tests: failed calendar helpers must not terminate Hermes."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_api():
    spec = importlib.util.spec_from_file_location("calendar_agenda_test_api", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ExitIsolationTests(unittest.TestCase):
    def setUp(self):
        self.api = load_api()

    def test_read_exit_becomes_local_503(self):
        service = types.SimpleNamespace(list_today_events=lambda: sys.exit(1))
        with patch.object(self.api, "_service_module", return_value=service):
            try:
                self.api.today()
            except self.api.HTTPException as exc:
                self.assertEqual(exc.status_code, 503)
            except SystemExit:
                self.fail("Calendar SystemExit escaped the read endpoint")
            else:
                self.fail("Calendar read did not report its failure")

    def test_update_exit_becomes_local_503(self):
        def exiting_update(**kwargs):
            raise SystemExit(1)
        service = types.SimpleNamespace(set_event_state=exiting_update)
        body = self.api.StateUpdate(outcome="done", keep_slot=True)
        with patch.object(self.api, "_service_module", return_value=service):
            try:
                self.api.update_state("offline-event", body)
            except self.api.HTTPException as exc:
                self.assertEqual(exc.status_code, 503)
            except SystemExit:
                self.fail("Calendar SystemExit escaped the update endpoint")
            else:
                self.fail("Calendar update did not report its failure")

    def test_keyboard_interrupt_is_not_swallowed(self):
        def interrupted():
            raise KeyboardInterrupt()
        with patch.object(self.api, "_service_module", return_value=types.SimpleNamespace(list_today_events=interrupted)):
            with self.assertRaises(KeyboardInterrupt):
                self.api.today()

    def test_normal_read_still_returns_service_result(self):
        expected = {"date": "2026-10-15", "events": []}
        with patch.object(self.api, "_service_module", return_value=types.SimpleNamespace(list_today_events=lambda: expected)):
            self.assertEqual(self.api.today(), expected)

    def test_legacy_service_cache_is_not_reused(self):
        stale = types.SimpleNamespace(list_today_events=lambda: {"wrong_profile": True})
        with patch.dict(sys.modules, {"hermes_calendar_agenda_service": stale}):
            service = self.api._service_module()
            self.assertIsNot(service, stale)
            self.assertEqual(service._PROFILE_HOME, ROOT.parents[1])


if __name__ == "__main__":
    unittest.main()
