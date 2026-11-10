"""Offline profile-isolation regression using harmless Workspace helper fixtures."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

ROOT = Path(__file__).resolve().parents[1]


class ProfileIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(os.environ["HERMES_HOME"]) / "cache" / "scratch")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / "profiles" / "owner-profile"
        self.plugin = self.home / "plugins" / "calendar-agenda"
        self.plugin.mkdir(parents=True)
        shutil.copyfile(ROOT / "calendar_agenda_service.py", self.plugin / "calendar_agenda_service.py")
        self.default = self.base / "default"
        self._helper(self.home)
        self._helper(self.default)
        spec = importlib.util.spec_from_file_location("calendar_recovery_profile_fixture", self.plugin / "calendar_agenda_service.py")
        self.service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.service)
        self.original_modules = set(sys.modules)
        self.addCleanup(self._clear_new_helper_modules)

    def _clear_new_helper_modules(self):
        for name in set(sys.modules) - self.original_modules:
            if name.startswith("hermes_calendar_agenda_google_api"):
                sys.modules.pop(name, None)

    def _helper(self, home):
        scripts = home / "skills" / "productivity" / "google-workspace" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "google_api.py").write_text(
            "from hermes_constants import get_hermes_home\n"
            "HERMES_HOME = get_hermes_home()\n"
            "TOKEN_PATH = HERMES_HOME / 'google_token.json'\n"
            "def build_service(api, version):\n"
            "    return TOKEN_PATH\n",
            encoding="utf-8",
        )

    def test_default_context_does_not_select_default_helper(self):
        token = set_hermes_home_override(self.default)
        try:
            result = self.service._calendar_service()
            self.assertEqual(result, self.home / "google_token.json")
            self.assertEqual(get_hermes_home(), self.default)
        finally:
            reset_hermes_home_override(token)

    def test_legacy_shared_cache_does_not_override_profile(self):
        stale = types.SimpleNamespace(build_service=lambda *args: self.default / "google_token.json")
        with patch.dict(sys.modules, {"hermes_calendar_agenda_google_api": stale}):
            self.assertEqual(self.service._calendar_service(), self.home / "google_token.json")

    def test_missing_owner_helper_never_falls_back_to_default(self):
        (self.home / "skills/productivity/google-workspace/scripts/google_api.py").unlink()
        token = set_hermes_home_override(self.default)
        try:
            with self.assertRaises(RuntimeError):
                self.service._calendar_service()
            self.assertEqual(get_hermes_home(), self.default)
        finally:
            reset_hermes_home_override(token)


if __name__ == "__main__":
    unittest.main()
