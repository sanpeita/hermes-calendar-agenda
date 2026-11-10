"""Offline checks for agent-tool error isolation; never calls Calendar transport."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_tools():
    name = "calendar_agenda_tool_tests"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return sys.modules[name + ".tools"]


class ToolIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tools = load_tools()

    def test_read_system_exit_returns_tool_error(self):
        with patch.object(self.tools, "list_today_events", side_effect=SystemExit(1)):
            self.assertIn("error", json.loads(self.tools._handle_list_today({})))

    def test_update_system_exit_does_not_claim_success(self):
        with patch.object(self.tools, "set_event_state", side_effect=SystemExit(1)):
            result = json.loads(self.tools._handle_set_state({"event_id": "offline", "outcome": "done", "keep_slot": True}))
            self.assertIn("error", result)
            self.assertNotIn("result", result)

    def test_keyboard_interrupt_remains_a_real_interrupt(self):
        with patch.object(self.tools, "list_today_events", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.tools._handle_list_today({})


if __name__ == "__main__":
    unittest.main()
