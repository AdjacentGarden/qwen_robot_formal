from __future__ import annotations

import asyncio
import base64
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from local_skills import LocalSkillBridge
from skill_event_audio import QwenSkillEventSpeaker


class FakeRealtimeSocket:
    def __init__(self) -> None:
        pcm = b"\x01\x02" * 120
        self.messages = [
            json.dumps({"type": "response.audio.delta", "delta": base64.b64encode(pcm).decode()}),
            json.dumps({"type": "response.audio_transcript.done", "transcript": "第一个"}),
            json.dumps({"type": "response.done"}),
        ]
        self.sent: list[dict] = []

    async def send(self, value: str) -> None:
        self.sent.append(json.loads(value))

    async def recv(self) -> str:
        return self.messages.pop(0)


class SkillEventAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_fitness_completion_is_speakable_and_receipted_after_pcm_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            queued: list[bytes] = []
            speaker = QwenSkillEventSpeaker(
                api_key="unused",
                voice="longanqian",
                workspace="",
                region="cn-beijing",
                endpoint="",
                connect_timeout=1,
                enqueue_pcm=queued.append,
                log=lambda _event, **_fields: None,
                cache_dir=Path(directory),
            )
            speaker.loop = asyncio.get_running_loop()
            text = "运动结束，你一共完成了三个俯卧撑。辛苦了，喝口水吧。"
            path = speaker._cache_path(text)
            self.assertIsNotNone(path)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"\x01\x02" * 100)
            speaker.submit_from_thread({
                "turn_id": "8",
                "skill_name": "push_up",
                "kind": "complete",
                "count": 3,
                "text": text,
            })
            await asyncio.sleep(0)
            _sequence, _priority, event = speaker.queue.get_nowait()
            self.assertIsNone(speaker.delivered_event(
                turn_id="8", kind="complete", skill_names={"push_up"}
            ))
            await speaker._synthesize(event)
            speaker.queue.task_done()

        self.assertEqual(queued, [b"\x01\x02" * 100])
        delivered = speaker.delivered_event(
            turn_id="8", kind="complete", skill_names={"push_up"}
        )
        self.assertIsNotNone(delivered)
        self.assertEqual(delivered["text"], text)

    async def test_status_and_live_count_events_are_queued_and_audio_is_forwarded(self):
        pcm: list[bytes] = []
        logs: list[tuple[str, dict]] = []
        speaker = QwenSkillEventSpeaker(
            api_key="unused",
            voice="longanqian",
            workspace="",
            region="cn-beijing",
            endpoint="",
            connect_timeout=1,
            enqueue_pcm=pcm.append,
            log=lambda event, **fields: logs.append((event, fields)),
        )
        speaker.loop = asyncio.get_running_loop()
        speaker.submit_from_thread({"skill_name": "push_up", "kind": "debug", "text": "内部调试"})
        speaker.submit_from_thread({"skill_name": "push_up", "kind": "acknowledgement", "text": "现在开始"})
        speaker.submit_from_thread({"skill_name": "push_up", "kind": "count", "count": 1, "text": "第一个"})
        await asyncio.sleep(0)
        _sequence, _priority, event = speaker.queue.get_nowait()
        self.assertEqual(event["text"], "现在开始")
        speaker.queue.task_done()
        _sequence, _priority, count = speaker.queue.get_nowait()
        self.assertEqual(count["text"], "第一个")
        speaker.queue.task_done()
        self.assertTrue(speaker.queue.empty())
        speaker.websocket = FakeRealtimeSocket()
        await speaker._synthesize(count)
        self.assertEqual(len(pcm[0]), 240)
        self.assertEqual(logs[-1][0], "skill_event_audio_generated")
        self.assertEqual(logs[-1][1]["transcript"], "第一个")

    async def test_long_audio_starts_streaming_before_response_done(self):
        pcm: list[bytes] = []
        logs: list[tuple[str, dict]] = []
        speaker = QwenSkillEventSpeaker(
            api_key="unused",
            voice="longanqian",
            workspace="",
            region="cn-beijing",
            endpoint="",
            connect_timeout=1,
            enqueue_pcm=pcm.append,
            log=lambda event, **fields: logs.append((event, fields)),
        )
        chunk = b"\x01\x02" * 2400

        class Socket:
            def __init__(self):
                self.messages = [
                    json.dumps({"type": "response.audio.delta", "delta": base64.b64encode(chunk).decode()}),
                    json.dumps({"type": "response.done"}),
                ]

            async def send(self, _value):
                return None

            async def recv(self):
                return self.messages.pop(0)

        speaker.websocket = Socket()
        await speaker._synthesize({"skill_name": "navigation_goto", "kind": "acknowledgement", "text": "现在出发"})
        self.assertGreaterEqual(len(pcm), 1)
        self.assertEqual(b"".join(pcm), chunk)
        self.assertIn("skill_event_audio_first_chunk", [event for event, _fields in logs])

    async def test_wait_idle_does_not_finish_before_thread_submission_is_processed(self):
        generated: list[str] = []
        speaker = QwenSkillEventSpeaker(
            api_key="unused",
            voice="longanqian",
            workspace="",
            region="cn-beijing",
            endpoint="",
            connect_timeout=1,
            enqueue_pcm=lambda _pcm: None,
            log=lambda _event, **_fields: None,
        )
        speaker.loop = asyncio.get_running_loop()

        async def consume() -> None:
            _sequence, _priority, event = await speaker.queue.get()
            generated.append(event["text"])
            speaker.queue.task_done()

        speaker.task = asyncio.create_task(consume())
        speaker.submit_from_thread({"skill_name": "navigation_goto", "kind": "acknowledgement", "text": "现在去书房"})
        self.assertTrue(await speaker.wait_idle(timeout=1.0))
        await speaker.task
        self.assertEqual(generated, ["现在去书房"])

    async def test_user_interrupt_invalidates_current_and_queued_audio(self):
        queued: list[bytes] = []
        speaker = QwenSkillEventSpeaker(
            api_key="unused",
            voice="longanqian",
            workspace="",
            region="cn-beijing",
            endpoint="",
            connect_timeout=1,
            enqueue_pcm=queued.append,
            log=lambda _event, **_fields: None,
        )
        speaker.loop = asyncio.get_running_loop()
        speaker.submit_from_thread(
            {"skill_name": "navigation_goto", "kind": "progress", "text": "正在导航"}
        )
        await asyncio.sleep(0)
        _sequence, _priority, stale = speaker.queue.get_nowait()
        speaker.queue.task_done()
        speaker.submit_from_thread(
            {"skill_name": "navigation_goto", "kind": "result", "text": "已经到达"}
        )
        await asyncio.sleep(0)
        speaker.cancel_pending()
        await asyncio.sleep(0)
        self.assertTrue(speaker.queue.empty())
        await speaker._synthesize(stale)
        self.assertEqual(queued, [])

    async def test_cached_acknowledgement_replays_without_cloud_synthesis(self):
        with tempfile.TemporaryDirectory() as directory:
            queued: list[bytes] = []
            logs: list[tuple[str, dict]] = []
            speaker = QwenSkillEventSpeaker(
                api_key="unused",
                voice="longanqian",
                workspace="",
                region="cn-beijing",
                endpoint="",
                connect_timeout=1,
                enqueue_pcm=queued.append,
                log=lambda event, **fields: logs.append((event, fields)),
                cache_dir=Path(directory),
            )
            path = speaker._cache_path("现在去书房")
            self.assertIsNotNone(path)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"\x01\x02" * 100)
            speaker.websocket = None
            await speaker._synthesize(
                {"skill_name": "navigation_goto", "kind": "acknowledgement", "text": "现在去书房"}
            )
        self.assertEqual(queued, [b"\x01\x02" * 100])
        generated = [fields for event, fields in logs if event == "skill_event_audio_generated"]
        self.assertTrue(generated[-1]["cached"])


class SkillHostStreamingProtocolTests(unittest.TestCase):
    def test_host_events_arrive_before_one_final_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host.sock"
            ready = threading.Event()

            def serve() -> None:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.bind(str(path))
                    server.listen(1)
                    ready.set()
                    conn, _ = server.accept()
                    with conn:
                        stream = conn.makefile("rwb")
                        json.loads(stream.readline().decode())
                        stream.write(json.dumps({"type": "skill_event", "event": {"kind": "count", "text": "第一个"}}).encode() + b"\n")
                        stream.write(json.dumps({"type": "final", "ok": True, "spoken_summary": "完成"}).encode() + b"\n")
                        stream.flush()

            thread = threading.Thread(target=serve)
            thread.start()
            ready.wait(1)
            received: list[dict] = []
            bridge = LocalSkillBridge.__new__(LocalSkillBridge)
            bridge.host_socket = path
            bridge.timeout = 2.0
            bridge.execute = True
            bridge.event_callback = received.append
            result = bridge._run_host("push_up", {"action": "run"}, "开始")
            thread.join(1)
            self.assertEqual(result["dispatch_state"], "completed")
            self.assertEqual(received, [{"kind": "count", "text": "第一个"}])
            self.assertIn("QWEN_SKILL_RUNNER_RESULT=", result["stdout"])


if __name__ == "__main__":
    unittest.main()
