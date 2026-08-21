from __future__ import annotations

import asyncio
import json
import queue
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from realtime_chat import (
    AudioEngine,
    DEFAULT_ASSISTANT_INSTRUCTIONS,
    JsonLogger,
    RealtimeConversation,
    build_tool_reply_instruction,
)
from memory_store import MemoryStore


class FakeWebSocket:
    def __init__(self) -> None:
        self.events = [
            {"type": "session.created", "session": {"id": "session-test"}},
            {"type": "session.updated", "session": {"id": "session-test"}},
        ]
        self.sent = []

    async def recv(self):
        return json.dumps(self.events.pop(0))

    async def send(self, value):
        self.sent.append(json.loads(value))


class ClientHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_transcript_discards_its_tool_call_and_prompts_retry(self) -> None:
        spoken: list[dict] = []

        class Speaker:
            def cancel_pending(self):
                return None

            def submit_from_thread(self, event):
                spoken.append(dict(event))

        args = SimpleNamespace(transcript_grace_seconds=0.01)
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            client.skill_event_speaker = Speaker()
            client.mark_input_speech_started()
            first_utterance = client.active_input_utterance_id
            await client.handle_or_defer_function_call(
                {
                    "call_id": "stale-call",
                    "name": "navigation_goto",
                    "arguments": '{"point":"study_projection"}',
                },
                utterance_id=first_utterance,
            )
            self.assertEqual(len(client.deferred_function_calls), 1)
            client.schedule_transcript_timeout()
            await asyncio.sleep(0.25)

            self.assertEqual(client.deferred_function_calls, [])
            self.assertFalse(client.awaiting_input_transcript)
            self.assertEqual(spoken[-1]["text"], "我没听清刚才那句话，请再说一遍。")
            output = websocket.sent[-1]["item"]
            self.assertEqual(output["type"], "function_call_output")
            self.assertEqual(output["call_id"], "stale-call")
            self.assertIn("NOT_EXECUTED", output["output"])

            client.mark_input_speech_started()
            await client.accept_input_transcript("今天天气怎么样")
            self.assertEqual(client.deferred_function_calls, [])

    async def test_local_clarification_is_available_to_the_next_short_answer(self) -> None:
        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            client._remember_local_assistant_speech("你是想让我陪你做俯卧撑吗？")
            client.mark_input_speech_started()
            self.assertEqual(
                client.speech_turn_assistant_context,
                "你是想让我陪你做俯卧撑吗？",
            )

    def test_audio_interrupt_does_not_restart_stream_from_non_owner_thread(self) -> None:
        class Stream:
            def __init__(self) -> None:
                self.stop_calls = 0
                self.start_calls = 0

            def stop_stream(self) -> None:
                self.stop_calls += 1

            def start_stream(self) -> None:
                self.start_calls += 1

        audio = AudioEngine.__new__(AudioEngine)
        audio.generation = 7
        audio.output_queue = queue.Queue()
        audio.output_queue.put_nowait((7, b"old audio"))
        audio.output_stream = Stream()
        audio.playing = threading.Event()
        audio.playing.set()
        audio.last_playback_at = 0.0

        audio.interrupt()

        self.assertEqual(audio.generation, 8)
        self.assertTrue(audio.output_queue.empty())
        self.assertFalse(audio.playing.is_set())
        self.assertEqual(audio.output_stream.stop_calls, 0)
        self.assertEqual(audio.output_stream.start_calls, 0)

    def test_default_prompt_covers_memory_warmth_truth_and_no_duplicate_speech(self) -> None:
        for rule in (
            "结合前文理解",
            "避免机械重复",
            "提醒喝水",
            "只有工具明确成功后",
            "一项事实只播报一次",
            "不能拆成",
            "不得声称自己完全没有历史记忆",
            "不要把“好的、当然可以",
            "先说结论",
            "本地执行器会用同一音色立即并行播报",
            "欢迎回家”只由本地场景开始语说一次",
            "失败、零计数或尚未执行时，不说庆祝完成的话",
            "必须调用 run_skill_sequence 一次",
            "动作守恒",
            "不是关键词触发器",
            "公司”就查询机器人位置",
            "关闭会议，投影导航到客厅去",
            "不能把场景名直接写进 name",
            "只有高置信的常见转写偏差",
            "用户回答“是的/对/不是”时必须承接",
        ):
            self.assertIn(rule, DEFAULT_ASSISTANT_INSTRUCTIONS)

    def test_tool_reply_contract_is_human_but_result_authoritative(self) -> None:
        success = build_tool_reply_instruction(
            "reminder_schedule",
            {
                "ok": True,
                "executed": True,
                "skill": "reminder_schedule",
                "spoken_summary": "提醒设好了，到了时间我叫你：喝水。",
            },
        )
        self.assertIn("权威播报要点", success)
        self.assertIn("自然改写", success)
        self.assertIn("不要再补一遍", success)
        self.assertIn("不要以‘还有什么需要吗’", success)

        failure = build_tool_reply_instruction(
            "face_recognition",
            {
                "ok": False,
                "executed": False,
                "error": "camera_open_failed",
                "spoken_summary": "摄像头这次没能正常打开，所以没有继续。",
            },
        )
        self.assertIn("明确说哪件事没有完成", failure)
        self.assertIn("不得暗示已经执行", failure)
        self.assertIn("不要暴露工具名、错误代码", failure)

    def test_homecoming_reply_contract_forbids_duplicate_greeting(self) -> None:
        prompt = build_tool_reply_instruction(
            "run_robot_scenario",
            {
                "ok": True,
                "executed": True,
                "scenario": "homecoming_welcome",
                "spoken_summary": "欢迎画面播放好了。",
            },
        )
        self.assertIn("结果回复绝对不要再次重复这四个字", prompt)

    def test_sequence_reply_contract_requires_every_child_outcome(self) -> None:
        prompt = build_tool_reply_instruction(
            "run_skill_sequence",
            {
                "ok": False,
                "executed": True,
                "skill": "run_skill_sequence",
                "spoken_summary": "已经到达客厅；灯光没有打开。",
                "tasks": [
                    {"name": "navigation_goto", "succeeded": True},
                    {"name": "light_control", "succeeded": False},
                ],
            },
        )
        self.assertIn("每个子任务的真实结果", prompt)
        self.assertIn("不能只汇报第一项", prompt)

    async def test_final_result_waits_until_parallel_status_speech_is_queued(self) -> None:
        order: list[str] = []

        class Speaker:
            async def wait_idle(self, timeout=0):
                order.append(f"speech:{timeout}")
                return True

        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            client.skill_event_speaker = Speaker()
            client.pending_tool_followup = True
            client.pending_tool_followup_prompts.append("最终结果")
            self.assertTrue(await client.create_tool_followup_if_needed())

        self.assertEqual(order, ["speech:6.0"])
        self.assertEqual(websocket.sent[-1]["type"], "response.create")

    async def test_style_rotation_never_injects_a_new_realtime_turn(self) -> None:
        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            client.style_hint_pending = True
            self.assertFalse(await client.inject_next_turn_style_hint())
        self.assertEqual(websocket.sent, [])

    async def test_authoritative_result_uses_event_speaker_without_model_followup(self) -> None:
        spoken: list[dict] = []

        class Speaker:
            def submit_from_thread(self, event):
                spoken.append(dict(event))

        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            client.skill_event_speaker = Speaker()
            client.last_user_text = "导航到书房"
            client.user_turn_id = 4
            client.skill_bridge = SimpleNamespace(
                current_turn_id="",
                scenario_catalog=None,
                invoke=lambda *_args: {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "skill": "navigation_goto",
                    "spoken_summary": "我已经到书房了。",
                },
            )
            await client.handle_function_call(
                {
                    "call_id": "result-call",
                    "name": "navigation_goto",
                    "arguments": '{"point":"study_projection"}',
                }
            )
        self.assertEqual(spoken[-1]["kind"], "result")
        self.assertEqual(spoken[-1]["text"], "我已经到书房了。")
        self.assertEqual([item["type"] for item in websocket.sent], ["conversation.item.create"])
        self.assertFalse(client.pending_tool_followup)

    async def test_missing_tool_call_discards_false_completion_and_runs_local_plan(self) -> None:
        queued_audio: list[bytes] = []
        spoken: list[dict] = []

        class Audio:
            def enqueue(self, pcm):
                queued_audio.append(pcm)

        class Speaker:
            def submit_from_thread(self, event):
                spoken.append(dict(event))

        class Bridge:
            scenario_catalog = None
            current_turn_id = ""

            @staticmethod
            def recover_explicit_plan(text):
                if text == "导航去书":
                    return {"name": "navigation_goto", "arguments": {"point": "study_projection"}}
                return None

            @staticmethod
            def invoke(name, arguments, *_context):
                return {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "skill": name,
                    "arguments": arguments,
                    "spoken_summary": "我已经到书房了。",
                }

            @staticmethod
            def cancel_all():
                return None

        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            client.audio = Audio()
            client.skill_event_speaker = Speaker()
            client.skill_bridge = Bridge()
            client.mark_input_speech_started()
            client.quarantined_model_audio.append(b"false-completion")
            client.quarantined_output_transcripts.append("已经到书房了。")
            await client.accept_input_transcript("导航去书")
            await client.finish_response_turn()
            await asyncio.sleep(0)
            tasks = list(client.function_call_tasks)
            if tasks:
                await asyncio.gather(*tasks)

        self.assertEqual(queued_audio, [])
        self.assertNotEqual(client.last_assistant_text, "已经到书房了。")
        self.assertEqual(spoken[-1]["kind"], "result")
        self.assertEqual(spoken[-1]["text"], "我已经到书房了。")
        self.assertEqual(websocket.sent, [])

    async def test_start_speech_is_submitted_while_skill_is_still_running(self) -> None:
        started_speech = threading.Event()
        release_skill = threading.Event()

        class Speaker:
            def submit_from_thread(self, event):
                if event.get("kind") == "acknowledgement":
                    started_speech.set()

        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            client.skill_event_speaker = Speaker()

            def invoke(name, arguments, user_text, *context):
                client.handle_skill_event_from_thread(
                    {
                        "skill_name": name,
                        "kind": "acknowledgement",
                        "text": "收到，我现在去书房。",
                    }
                )
                release_skill.wait(1.0)
                return {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "skill": name,
                    "spoken_summary": "已经到达书房。",
                }

            client.skill_bridge = SimpleNamespace(invoke=invoke, scenario_catalog=None)
            task = asyncio.create_task(
                client.handle_function_call(
                    {
                        "call_id": "parallel-call-1",
                        "name": "navigation_goto",
                        "arguments": '{"point":"study_projection"}',
                    }
                )
            )
            self.assertTrue(await asyncio.to_thread(started_speech.wait, 0.5))
            self.assertFalse(task.done())
            release_skill.set()
            result = await task

        self.assertTrue(result["executed"])

    async def test_long_tool_keeps_its_original_turn_context(self) -> None:
        observed: list[tuple[str, str]] = []
        release = threading.Event()

        class Speaker:
            def submit_from_thread(self, _event):
                return None

        def invoke(_name, _arguments, user_text, turn_id, *_context):
            release.wait(1.0)
            observed.append((user_text, turn_id))
            return {
                "ok": True,
                "validation_ok": True,
                "executed": True,
                "skill": "navigation_goto",
                "spoken_summary": "已经到书房。",
            }

        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            client.websocket = FakeWebSocket()
            client.websocket.events = []
            client.skill_event_speaker = Speaker()
            client.skill_bridge = SimpleNamespace(
                invoke=invoke,
                scenario_catalog=None,
                current_turn_id="",
            )
            client.user_turn_id = 1
            client.last_user_text = "导航到书房"
            task = client.schedule_function_call(
                {
                    "call_id": "turn-one",
                    "name": "navigation_goto",
                    "arguments": '{"point":"study_projection"}',
                }
            )
            await asyncio.sleep(0)
            client.user_turn_id = 2
            client.last_user_text = "现在几点"
            release.set()
            await task

        self.assertEqual(observed, [("导航到书房", "1")])

    async def test_audio_can_start_only_after_created_and_updated(self) -> None:
        fake_ws = FakeWebSocket()
        observed = {}

        async def connect(
            url,
            *,
            additional_headers=None,
            open_timeout=None,
            close_timeout=None,
            max_size=None,
            ping_interval=None,
            ping_timeout=None,
        ):
            observed.update(url=url, headers=additional_headers, timeout=open_timeout)
            return fake_ws

        fake_module = types.SimpleNamespace(connect=connect)
        args = SimpleNamespace(
            workspace="workspace-test",
            endpoint="",
            region="cn-beijing",
            connect_timeout=2.0,
            voice="longanqian",
            instructions="自然交流",
            turn_detection="smart_turn",
            silence_duration_ms=800,
            vad_threshold=0.5,
            max_history_turns=50,
        )
        with tempfile.TemporaryDirectory() as directory:
            logger = JsonLogger(Path(directory) / "events.jsonl")
            client = RealtimeConversation(args, "sk-test", logger)
            with patch.dict(sys.modules, {"websockets": fake_module}):
                await client.connect()

        self.assertIn("qwen-audio-3.0-realtime-flash", observed["url"])
        self.assertEqual(observed["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(fake_ws.sent[0]["type"], "session.update")
        self.assertEqual(
            fake_ws.sent[0]["session"]["turn_detection"], {"type": "smart_turn"}
        )
        memory_tools = {
            item["function"]["name"] for item in fake_ws.sent[0]["session"]["tools"]
        }
        self.assertEqual(memory_tools, {"memory_save", "memory_query", "memory_delete"})

    async def test_connect_injects_persisted_history_after_session_update(self) -> None:
        fake_ws = FakeWebSocket()

        async def connect(url, **kwargs):
            return fake_ws

        fake_module = types.SimpleNamespace(connect=connect)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = MemoryStore(root / "memory")
            memory.append_conversation("user", "我叫张三")
            memory.append_conversation("assistant", "好的，我记住了")
            args = SimpleNamespace(
                workspace="workspace-test",
                endpoint="",
                region="cn-beijing",
                connect_timeout=2.0,
                voice="longanqian",
                instructions="自然交流",
                turn_detection="smart_turn",
                silence_duration_ms=800,
                vad_threshold=0.5,
                max_history_turns=50,
                memory_dir=root / "memory",
                persistent_memory=True,
                memory_history_turns=12,
                memory_history_chars=6000,
            )
            client = RealtimeConversation(args, "sk-test", JsonLogger(root / "events.jsonl"))
            with patch.dict(sys.modules, {"websockets": fake_module}):
                await client.connect()
        self.assertEqual([item["type"] for item in fake_ws.sent], [
            "session.update",
            "conversation.item.create",
            "conversation.item.create",
        ])
        self.assertEqual(fake_ws.sent[1]["item"]["role"], "user")
        self.assertEqual(fake_ws.sent[2]["item"]["role"], "assistant")

    async def test_persisted_history_is_not_injected_by_default(self) -> None:
        fake_ws = FakeWebSocket()

        async def connect(url, **kwargs):
            return fake_ws

        fake_module = types.SimpleNamespace(connect=connect)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = MemoryStore(root / "memory")
            memory.append_conversation("assistant", "上一次投影启动失败")
            args = SimpleNamespace(
                workspace="workspace-test",
                endpoint="",
                region="cn-beijing",
                connect_timeout=2.0,
                voice="longanqian",
                instructions="自然交流",
                turn_detection="smart_turn",
                silence_duration_ms=800,
                vad_threshold=0.5,
                max_history_turns=50,
                memory_dir=root / "memory",
                persistent_memory=True,
                memory_history_chars=6000,
            )
            client = RealtimeConversation(args, "sk-test", JsonLogger(root / "events.jsonl"))
            with patch.dict(sys.modules, {"websockets": fake_module}):
                await client.connect()
        self.assertEqual([item["type"] for item in fake_ws.sent], ["session.update"])

    async def test_memory_tool_executes_without_hardware_skill_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(memory_dir=root / "memory", persistent_memory=True)
            client = RealtimeConversation(args, "sk-test", JsonLogger(root / "events.jsonl"))
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            result = await client.handle_function_call(
                {
                    "call_id": "memory-call-1",
                    "name": "memory_save",
                    "arguments": '{"content":"用户喜欢喝绿茶","category":"preference"}',
                }
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["executed"])
        output = json.loads(websocket.sent[0]["item"]["output"])
        self.assertEqual(output["skill"], "memory_save")

    async def test_dry_run_tool_output_is_minimal_and_cannot_claim_execution(self) -> None:
        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            client.skill_bridge = SimpleNamespace(
                invoke=lambda name, arguments, user_text, *context: {
                    "ok": False,
                    "validation_ok": True,
                    "executed": False,
                    "mode": "dry_run",
                    "spoken_summary": "安全模拟成功，没有实际执行。",
                }
            )
            await client.handle_function_call(
                {
                    "call_id": "call-1",
                    "name": "reminder_schedule",
                    "arguments": '{"content":"喝水"}',
                }
            )
            await client.create_tool_followup_if_needed()
        output = json.loads(websocket.sent[0]["item"]["output"])
        self.assertEqual(output["status"], "NOT_EXECUTED")
        self.assertFalse(output["success"])
        self.assertIn("禁止", output["mandatory_rule_zh"])
        self.assertEqual(websocket.sent[1]["item"]["role"], "system")
        self.assertIn("逐字回复", websocket.sent[1]["item"]["content"][0]["text"])
        self.assertEqual(websocket.sent[2]["type"], "response.create")

    async def test_executed_tool_returns_authoritative_observation_to_model(self) -> None:
        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            client.skill_bridge = SimpleNamespace(
                invoke=lambda name, arguments, user_text, *context: {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "mode": "execute",
                    "skill": "face_recognition",
                    "spoken_summary": "看起来是zhangsan。",
                    "structured_result": {
                        "status": "matched",
                        "result": {"status": "matched", "name": "zhangsan", "score": 0.837},
                    },
                    "result_is_authoritative": True,
                }
            )
            await client.handle_function_call(
                {
                    "call_id": "face-call-1",
                    "name": "face_recognition",
                    "arguments": "{}",
                }
            )
            await client.create_tool_followup_if_needed()
        output = json.loads(websocket.sent[0]["item"]["output"])
        self.assertTrue(output["result_is_authoritative"])
        self.assertEqual(output["structured_result"]["result"]["name"], "zhangsan")
        self.assertEqual(output["spoken_summary"], "看起来是zhangsan。")
        followup_prompt = websocket.sent[1]["item"]["content"][0]["text"]
        self.assertIn("这是已真实执行的成功结果", followup_prompt)
        self.assertEqual(websocket.sent[2]["type"], "response.create")

    async def test_function_call_waits_for_current_audio_transcript(self) -> None:
        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            calls = []

            def invoke(name, arguments, user_text, *context):
                calls.append((name, arguments, user_text))
                return {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "mode": "execute",
                    "scenario": "homecoming_welcome",
                    "spoken_summary": "欢迎回家。",
                }

            client.skill_bridge = SimpleNamespace(invoke=invoke, scenario_catalog=None)
            client.mark_input_speech_started()
            deferred = await client.handle_or_defer_function_call(
                {
                    "call_id": "welcome-call-1",
                    "name": "run_robot_scenario",
                    "arguments": '{"scenario":"homecoming_welcome"}',
                }
            )
            self.assertIsNone(deferred)
            self.assertEqual(calls, [])

            results = await client.accept_input_transcript("Hello，理想同学。")

        self.assertEqual(len(results), 1)
        self.assertEqual(calls[0][2], "Hello，理想同学。")
        self.assertTrue(results[0]["executed"])
        output = json.loads(websocket.sent[0]["item"]["output"])
        self.assertEqual(output["scenario"], "homecoming_welcome")

    async def test_scheduled_call_uses_target_turn_when_transcript_wins_race(self) -> None:
        """Regression: a close-projection call must not reuse the open turn."""

        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            calls = []

            def invoke(name, arguments, user_text, turn_id, *_context):
                calls.append((name, arguments, user_text, turn_id))
                return {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "mode": "execute",
                    "scenario": arguments.get("scenario"),
                    "spoken_summary": "会议投影已经关闭了。",
                }

            client.skill_bridge = SimpleNamespace(
                invoke=invoke,
                scenario_catalog=None,
                current_turn_id="",
            )
            await client.accept_input_transcript("帮我投影会议内容")
            client.mark_input_speech_started()
            task = client.schedule_function_call(
                {
                    "call_id": "stop-race-1",
                    "name": "run_robot_scenario",
                    "arguments": '{"scenario":"meeting_projection_stop"}',
                }
            )
            # Reproduce the production ordering: transcript commits before the
            # newly-created background task receives its first event-loop turn.
            await client.accept_input_transcript("停止投影")
            await task

        self.assertEqual(
            calls,
            [
                (
                    "run_robot_scenario",
                    {"scenario": "meeting_projection_stop"},
                    "停止投影",
                    "2",
                )
            ],
        )

    async def test_deferred_call_keeps_its_transcript_after_a_later_turn(self) -> None:
        """Per-turn binding remains stable even after global last_user_text changes."""

        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            client.websocket = FakeWebSocket()
            client.websocket.events = []
            observed = []

            def invoke(name, arguments, user_text, turn_id, *_context):
                observed.append((name, user_text, turn_id))
                return {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "mode": "execute",
                    "spoken_summary": "操作完成。",
                }

            client.skill_bridge = SimpleNamespace(
                invoke=invoke,
                scenario_catalog=None,
                current_turn_id="",
            )
            await client.accept_input_transcript("打开会议投影")
            client.mark_input_speech_started()
            await client.handle_or_defer_function_call(
                {
                    "call_id": "stop-deferred-1",
                    "name": "run_robot_scenario",
                    "arguments": '{"scenario":"meeting_projection_stop"}',
                },
                turn_id=2,
            )
            await client.accept_input_transcript("关闭投影", schedule_deferred=True)
            await client.accept_input_transcript("现在几点")
            await asyncio.gather(*list(client.function_call_tasks))

        self.assertEqual(observed[0], ("run_robot_scenario", "关闭投影", "2"))

    async def test_identical_tool_call_is_executed_only_once_per_user_turn(self) -> None:
        args = SimpleNamespace()
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                args,
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            calls = []

            def invoke(name, arguments, user_text, *context):
                calls.append((name, arguments, user_text))
                return {
                    "ok": False,
                    "validation_ok": False,
                    "executed": False,
                    "mode": "intent_rejected",
                    "spoken_summary": "当前意图不支持这个场景。",
                }

            client.skill_bridge = SimpleNamespace(invoke=invoke, scenario_catalog=None)
            await client.accept_input_transcript("今天好累")
            first = await client.handle_function_call(
                {
                    "call_id": "bad-call-1",
                    "name": "run_robot_scenario",
                    "arguments": '{"scenario":"homecoming_welcome"}',
                }
            )
            second = await client.handle_function_call(
                {
                    "call_id": "bad-call-2",
                    "name": "run_robot_scenario",
                    "arguments": '{"scenario":"homecoming_welcome"}',
                }
            )
            await client.create_tool_followup_if_needed()

        self.assertEqual(len(calls), 1)
        self.assertEqual(first["mode"], "intent_rejected")
        self.assertEqual(second["mode"], "deduplicated")
        self.assertTrue(second["deduplicated"])
        combined_prompt = websocket.sent[-2]["item"]["content"][0]["text"]
        self.assertIn("不得再次调用", combined_prompt)
        self.assertNotIn("安全模拟校验通过", combined_prompt)

    async def test_executed_skill_is_recorded_in_command_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(memory_dir=root / "memory", persistent_memory=True)
            client = RealtimeConversation(args, "sk-test", JsonLogger(root / "events.jsonl"))
            websocket = FakeWebSocket()
            websocket.events = []
            client.websocket = websocket
            client.skill_bridge = SimpleNamespace(
                invoke=lambda name, arguments, user_text, *context: {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "mode": "execute",
                    "skill": name,
                    "spoken_summary": "已经到书房。",
                },
                scenario_catalog=None,
            )
            await client.accept_input_transcript("请到书房去")
            await client.handle_function_call(
                {
                    "call_id": "remember-command-1",
                    "name": "navigation_goto",
                    "arguments": '{"point":"study_projection"}',
                }
            )
            commands = client.memory_store.invoke(
                "memory_query",
                {"scope": "command_history", "query_type": "latest"},
            )["commands"]

        self.assertEqual(commands[0]["text"], "请到书房去")
        self.assertEqual(commands[0]["calls"][0]["skill"], "navigation_goto")


if __name__ == "__main__":
    unittest.main()
