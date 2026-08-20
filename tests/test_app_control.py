from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from realtime_chat import JsonLogger, RealtimeConversation


def arguments(root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        memory_dir=root / "memory",
        persistent_memory=False,
        memory_max_history_items=10,
        memory_max_facts=10,
        local_skills=False,
        execute_skills=False,
        app_control_socket=root / "app_control.sock",
        app_voice_dir=root / "app_voice",
    )


class AppControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.conversation = RealtimeConversation(
            arguments(self.root),
            "test-key",
            JsonLogger(self.root / "events.jsonl"),
        )
        await self.conversation.start_control_server()

    async def asyncTearDown(self) -> None:
        await self.conversation.stop_control_server()
        self.temporary.cleanup()

    async def request(self, value: dict) -> dict:
        reader, writer = await asyncio.open_unix_connection(str(self.conversation.control_socket))
        writer.write(json.dumps(value).encode("utf-8") + b"\n")
        await writer.drain()
        response = json.loads((await reader.readline()).decode("utf-8"))
        writer.close()
        await writer.wait_closed()
        return response

    async def test_status_identifies_qwen_realtime_and_reports_task_shape(self) -> None:
        response = await self.request({"op": "status"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["service"], "qwen_audio_realtime")
        self.assertFalse(response["connected"])
        self.assertEqual(response["active_procedures"], [])

    async def test_app_voice_accepts_only_pcm_from_the_qwen_runtime(self) -> None:
        self.conversation.connected = True
        self.conversation.app_voice_root.mkdir(parents=True)
        pcm = self.conversation.app_voice_root / "voice.pcm"
        pcm.write_bytes(b"\x01\x00" * 1600)
        accepted = await self.request({"op": "app_voice", "pcm_path": str(pcm)})
        self.assertTrue(accepted["ok"])
        self.assertEqual(accepted["audio_bytes"], 3200)
        self.assertEqual(await self.conversation.external_audio_queue.get(), pcm.read_bytes())

        outside = self.root / "outside.pcm"
        outside.write_bytes(b"\x00\x00" * 1600)
        rejected = await self.request({"op": "app_voice", "pcm_path": str(outside)})
        self.assertFalse(rejected["ok"])
        self.assertIn("app_voice_path_outside_runtime", rejected["error"])

    async def test_real_app_skill_is_rejected_when_execution_is_disabled(self) -> None:
        response = await self.request(
            {"op": "app_skill", "skill": "light_control", "arguments": {"action": "on"}}
        )
        self.assertFalse(response["ok"])
        self.assertIn("local_skills_disabled", response["error"])


if __name__ == "__main__":
    unittest.main()
