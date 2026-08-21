from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from new_project.reminder_service import parse_trigger


class ReminderTimezoneTests(unittest.TestCase):
    def test_relative_reminder_uses_configured_local_timezone(self):
        now = datetime(2026, 8, 18, 2, 29, 20, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        with patch.dict(os.environ, {"REMINDER_TIMEZONE": "Asia/Shanghai"}):
            due_at, display = parse_trigger(trigger_condition="两小时后", now=now)
        self.assertEqual(due_at, now + 7200)
        self.assertEqual(display, "两小时后（08月18日 04:29:20）")

    def test_clock_reminder_uses_configured_local_date(self):
        now = datetime(2026, 8, 18, 2, 29, 20, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        with patch.dict(os.environ, {"REMINDER_TIMEZONE": "Asia/Shanghai"}):
            due_at, display = parse_trigger(trigger_condition="下午三点", now=now)
        expected = datetime(2026, 8, 18, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        self.assertEqual(due_at, expected)
        self.assertEqual(display, "08月18日 15:00")


if __name__ == "__main__":
    unittest.main()
