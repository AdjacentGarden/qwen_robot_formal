from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from memory_store import MemoryStore


class MemoryStoreCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temporary.name), timezone_name="Asia/Shanghai")
        zone = ZoneInfo("Asia/Shanghai")
        today = datetime.now(zone).replace(hour=10, minute=0, second=0, microsecond=0)
        self.times = [
            (today - timedelta(days=2)).timestamp(),
            (today - timedelta(days=1)).timestamp(),
            today.timestamp(),
        ]
        commands = [
            ("打开客厅灯", "light_control", {"action": "on"}),
            ("导航到书房", "navigation_goto", {"point": "study_projection"}),
            ("播放一首轻松的音乐", "media_player", {"action": "play_music"}),
        ]
        for turn, ((text, skill, arguments), timestamp) in enumerate(zip(commands, self.times), 1):
            self.store.record_command(
                user_text=text,
                session_id="session-a",
                turn_id=turn,
                skill=skill,
                arguments=arguments,
                result={"ok": True, "executed": True, "mode": "execute", "spoken_summary": "完成"},
                received_at=timestamp,
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def query(self, **arguments):
        return self.store.invoke("memory_query", {"scope": "command_history", **arguments})["commands"]

    def test_latest_offset_and_first_are_exact_user_commands(self) -> None:
        self.assertEqual(self.query(query_type="latest")[0]["text"], "播放一首轻松的音乐")
        self.assertEqual(self.query(query_type="offset", offset=1)[0]["text"], "导航到书房")
        self.assertEqual(self.query(query_type="first")[0]["text"], "打开客厅灯")

    def test_relative_date_ranges_use_configured_timezone(self) -> None:
        self.assertEqual(
            [item["text"] for item in self.query(query_type="time_range", date_period="yesterday")],
            ["导航到书房"],
        )
        self.assertEqual(
            [item["text"] for item in self.query(query_type="time_range", date_period="day_before_yesterday")],
            ["打开客厅灯"],
        )

    def test_multiple_tool_calls_are_grouped_by_user_turn(self) -> None:
        self.store.record_command(
            user_text="播放一首轻松的音乐",
            session_id="session-a",
            turn_id=3,
            skill="light_control",
            arguments={"action": "off"},
            result={"ok": False, "executed": False, "error": "offline"},
            received_at=self.times[2],
        )
        latest = self.query(query_type="latest")[0]
        self.assertEqual(len(latest["calls"]), 2)
        self.assertEqual({item["skill"] for item in latest["calls"]}, {"media_player", "light_control"})

    def test_sensitive_arguments_are_redacted(self) -> None:
        self.store.record_command(
            user_text="连接服务",
            session_id="session-b",
            turn_id=1,
            skill="service_connect",
            arguments={"api_key": "sk-secret", "nested": {"password": "hidden", "room": "study"}},
            result={"ok": True, "executed": True},
            received_at=self.times[-1] + 1,
        )
        arguments = self.query(query_type="latest")[0]["calls"][0]["arguments"]
        self.assertEqual(arguments["api_key"], "[REDACTED]")
        self.assertEqual(arguments["nested"]["password"], "[REDACTED]")
        self.assertEqual(arguments["nested"]["room"], "study")

    def test_decision_context_contains_requests_but_not_stale_device_outcomes(self) -> None:
        context = json.loads(self.store.decision_context_for_prompt())
        rendered = json.dumps(context, ensure_ascii=False)
        self.assertIn("导航到书房", rendered)
        self.assertNotIn("spoken_summary", rendered)
        self.assertNotIn("完成", rendered)


if __name__ == "__main__":
    unittest.main()
