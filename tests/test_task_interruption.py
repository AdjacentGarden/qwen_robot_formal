from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from realtime_chat import JsonLogger, RealtimeConversation
from local_skills import LocalSkillBridge, SEQUENCE_TOOL_NAME
from runtime_supervisor import TaskSnapshot


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, value: str) -> None:
        self.sent.append(json.loads(value))


class RecordingSpeaker:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def submit_from_thread(self, event: dict) -> None:
        self.events.append(dict(event))

    def cancel_pending(self) -> None:
        return None


class InterruptResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_uncommitted_model_call_does_not_preempt_active_task(self) -> None:
        old_started = threading.Event()
        release_old = threading.Event()
        cancel_calls = 0

        class Bridge:
            current_turn_id = ""
            scenario_catalog = None

            @staticmethod
            def recover_explicit_plan(_text):
                return None

            @staticmethod
            def recover_contextual_plan(_text, _context):
                return None

            def cancel_all(self):
                nonlocal cancel_calls
                cancel_calls += 1
                release_old.set()

            def invoke(self, name, _arguments, _user_text, *_context):
                if name == "push_up":
                    old_started.set()
                    release_old.wait(2.0)
                return {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "skill": name,
                    "spoken_summary": "完成。",
                }

        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                SimpleNamespace(),
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            client.websocket = FakeWebSocket()
            client.skill_bridge = Bridge()
            client.user_turn_id = 1
            old = client.schedule_function_call(
                {"call_id": "old-asr", "name": "push_up", "arguments": '{"action":"run"}'}
            )
            self.assertTrue(await asyncio.to_thread(old_started.wait, 1.0))

            client.mark_input_speech_started()
            deferred = client.schedule_function_call(
                {
                    "call_id": "new-before-asr",
                    "name": "navigation_goto",
                    "arguments": '{"point":"origin"}',
                }
            )
            await deferred
            self.assertEqual(cancel_calls, 0)
            self.assertEqual(len(client.deferred_function_calls), 1)

            await client.accept_input_transcript("导航回原点", schedule_deferred=True)
            await asyncio.gather(old, *list(client.function_call_tasks))
            self.assertEqual(cancel_calls, 1)

    async def test_fitness_is_interrupted_without_stale_result_then_resumes_checkpoint(self) -> None:
        old_started = threading.Event()
        cancel_requested = threading.Event()
        resumed_arguments: list[dict] = []

        class Bridge:
            current_turn_id = ""
            scenario_catalog = None

            @staticmethod
            def recover_explicit_plan(_text):
                return None

            @staticmethod
            def recover_contextual_plan(_text, _context):
                return None

            def cancel_all(self):
                cancel_requested.set()

            def invoke(self, name, arguments, _user_text, *_context):
                if name == "push_up" and not arguments.get("resume_from_interrupt"):
                    old_started.set()
                    cancel_requested.wait(2.0)
                    client.handle_skill_event_from_thread(
                        {
                            "skill_name": "push_up",
                            "kind": "count",
                            "count": 5,
                            "text": "不应播报的过期第五个",
                        }
                    )
                    return {
                        "ok": True,
                        "validation_ok": True,
                        "executed": True,
                        "skill": name,
                        "structured_result": {
                            "state": "interrupted",
                            "count": 4,
                            "elapsed_seconds": 12.5,
                        },
                        # This must never be spoken after the newer command.
                        "spoken_summary": "运动结束，你一共完成了四个俯卧撑。",
                    }
                if name == "push_up":
                    resumed_arguments.append(dict(arguments))
                    return {
                        "ok": True,
                        "validation_ok": True,
                        "executed": True,
                        "skill": name,
                        "spoken_summary": "继续运动完成。",
                    }
                return {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "skill": name,
                    "spoken_summary": "插入任务完成。",
                }

        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                SimpleNamespace(),
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            client.websocket = FakeWebSocket()
            speaker = RecordingSpeaker()
            client.skill_event_speaker = speaker
            client.skill_bridge = Bridge()
            client.user_turn_id = 1
            client.last_user_text = "陪我做俯卧撑"
            old = client.schedule_function_call(
                {
                    "call_id": "fitness-old",
                    "name": "push_up",
                    "arguments": '{"action":"run","duration":30}',
                }
            )
            self.assertTrue(await asyncio.to_thread(old_started.wait, 1.0))
            client.handle_skill_event_from_thread(
                {"skill_name": "push_up", "kind": "count", "count": 4, "text": "第四个"}
            )

            client.user_turn_id = 2
            client.last_user_text = "先帮我处理另一个任务"
            inserted = client.schedule_function_call(
                {
                    "call_id": "inserted-task",
                    "name": "navigation_goto",
                    "arguments": '{"point":"study_projection"}',
                }
            )
            old_result, inserted_result = await asyncio.gather(old, inserted)

            self.assertTrue(old_result["interrupted"])
            self.assertEqual(old_result["mode"], "interrupted")
            self.assertTrue(inserted_result["ok"])
            self.assertEqual(client.task_coordinator.state, "awaiting_resume")
            spoken = [str(item.get("text") or "") for item in speaker.events]
            self.assertFalse(any("运动结束，你一共完成了四个" in item for item in spoken))
            self.assertFalse(any("过期第五个" in item for item in spoken))
            self.assertTrue(any("暂停" in item for item in spoken))
            self.assertTrue(any("第4个" in item and "还要" in item for item in spoken))

            await client.accept_input_transcript("继续吧")
            if client.function_call_tasks:
                await asyncio.gather(*list(client.function_call_tasks))

            self.assertEqual(client.task_coordinator.state, "idle")
            self.assertEqual(len(resumed_arguments), 1)
            self.assertTrue(resumed_arguments[0]["resume_from_interrupt"])
            self.assertEqual(resumed_arguments[0]["initial_count"], 4)
            self.assertGreaterEqual(resumed_arguments[0]["initial_elapsed_seconds"], 12.5)
            self.assertAlmostEqual(resumed_arguments[0]["duration"], 17.5, places=2)

    async def test_meeting_projection_defers_resume_question_until_projection_stops(self) -> None:
        old_started = threading.Event()
        cancel_requested = threading.Event()

        class Bridge:
            current_turn_id = ""
            scenario_catalog = None

            @staticmethod
            def recover_explicit_plan(_text):
                return None

            @staticmethod
            def recover_contextual_plan(_text, _context):
                return None

            def cancel_all(self):
                cancel_requested.set()

            def invoke(self, name, arguments, _user_text, *_context):
                if name == "push_up":
                    old_started.set()
                    cancel_requested.wait(2.0)
                    return {
                        "ok": True,
                        "validation_ok": True,
                        "executed": True,
                        "structured_result": {"state": "interrupted", "count": 2},
                        "spoken_summary": "不应播报的旧结果。",
                    }
                return {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "scenario": arguments.get("scenario"),
                    "spoken_summary": "场景操作完成。",
                }

        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                SimpleNamespace(),
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            client.websocket = FakeWebSocket()
            speaker = RecordingSpeaker()
            client.skill_event_speaker = speaker
            client.skill_bridge = Bridge()
            client.user_turn_id = 1
            old = client.schedule_function_call(
                {"call_id": "old", "name": "push_up", "arguments": '{"action":"run"}'}
            )
            self.assertTrue(await asyncio.to_thread(old_started.wait, 1.0))
            client.user_turn_id = 2
            meeting = client.schedule_function_call(
                {
                    "call_id": "meeting-on",
                    "name": "run_robot_scenario",
                    "arguments": '{"scenario":"meeting_projection","stay_put":true}',
                }
            )
            await asyncio.gather(old, meeting)
            self.assertEqual(client.task_coordinator.state, "interruption_session_active")
            self.assertFalse(any("还要" in str(item.get("text")) for item in speaker.events))

            client.user_turn_id = 3
            stop = client.schedule_function_call(
                {
                    "call_id": "meeting-off",
                    "name": "run_robot_scenario",
                    "arguments": '{"scenario":"meeting_projection_stop"}',
                }
            )
            await stop
            self.assertEqual(client.task_coordinator.state, "awaiting_resume")
            self.assertTrue(any("还要" in str(item.get("text")) for item in speaker.events))

    async def test_failed_projection_stop_keeps_session_active_and_does_not_ask_early(self) -> None:
        class Bridge:
            scenario_catalog = None

            def invoke(self, _name, arguments, *_context):
                stopped = arguments.get("scenario") == "meeting_projection_stop"
                return {
                    "ok": not stopped,
                    "validation_ok": not stopped,
                    "executed": not stopped,
                    "spoken_summary": "投影关闭失败。" if stopped else "完成。",
                }

        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                SimpleNamespace(),
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            client.websocket = FakeWebSocket()
            speaker = RecordingSpeaker()
            client.skill_event_speaker = speaker
            client.skill_bridge = Bridge()
            client.task_coordinator.start(TaskSnapshot("push_up", {"duration": 30}, count=2))
            client.task_coordinator.interrupt("run_robot_scenario", {"scenario": "meeting_projection"})
            client.task_coordinator.interruption_completed(session_remains_active=True)
            client.interruption_session_kind = "projector"
            client.user_turn_id = 2

            result = await client.schedule_function_call(
                {
                    "call_id": "failed-stop",
                    "name": "run_robot_scenario",
                    "arguments": '{"scenario":"meeting_projection_stop"}',
                }
            )

            self.assertFalse(result["ok"])
            self.assertEqual(client.task_coordinator.state, "interruption_session_active")
            self.assertFalse(any("还要" in str(item.get("text")) for item in speaker.events))

    async def test_explicit_stop_discards_task_without_resume_question(self) -> None:
        old_started = threading.Event()
        cancelled = threading.Event()

        class Bridge:
            current_turn_id = ""
            scenario_catalog = None

            def cancel_all(self):
                cancelled.set()

            def invoke(self, name, arguments, *_context):
                if name == "push_up" and arguments.get("action") != "stop":
                    old_started.set()
                    cancelled.wait(2.0)
                return {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "spoken_summary": "已停止。",
                }

        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                SimpleNamespace(),
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            client.websocket = FakeWebSocket()
            speaker = RecordingSpeaker()
            client.skill_event_speaker = speaker
            client.skill_bridge = Bridge()
            client.user_turn_id = 1
            old = client.schedule_function_call(
                {"call_id": "workout", "name": "push_up", "arguments": '{"action":"run"}'}
            )
            self.assertTrue(await asyncio.to_thread(old_started.wait, 1.0))
            client.user_turn_id = 2
            stop = client.schedule_function_call(
                {"call_id": "stop-workout", "name": "push_up", "arguments": '{"action":"stop"}'}
            )
            await asyncio.gather(old, stop)

            self.assertEqual(client.task_coordinator.state, "idle")
            self.assertFalse(any("还要" in str(item.get("text")) for item in speaker.events))

    async def test_repeated_interruption_only_latest_inserted_task_triggers_resume_question(self) -> None:
        workout_started = threading.Event()
        navigation_started = threading.Event()
        release_workout = threading.Event()
        release_navigation = threading.Event()
        cancel_count = 0

        class Bridge:
            current_turn_id = ""
            scenario_catalog = None

            def cancel_all(self):
                nonlocal cancel_count
                cancel_count += 1
                if cancel_count == 1:
                    release_workout.set()
                else:
                    release_navigation.set()

            def invoke(self, name, _arguments, *_context):
                if name == "push_up":
                    workout_started.set()
                    release_workout.wait(2.0)
                    summary = "不应播报的运动旧结果。"
                elif name == "navigation_goto":
                    navigation_started.set()
                    release_navigation.wait(2.0)
                    summary = "不应播报的导航旧结果。"
                else:
                    summary = "最后插入的任务完成。"
                return {
                    "ok": True,
                    "validation_ok": True,
                    "executed": True,
                    "skill": name,
                    "structured_result": {"state": "interrupted", "count": 3},
                    "spoken_summary": summary,
                }

        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                SimpleNamespace(),
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            client.websocket = FakeWebSocket()
            speaker = RecordingSpeaker()
            client.skill_event_speaker = speaker
            client.skill_bridge = Bridge()
            client.user_turn_id = 1
            workout = client.schedule_function_call(
                {"call_id": "repeat-workout", "name": "push_up", "arguments": '{"action":"run"}'}
            )
            self.assertTrue(await asyncio.to_thread(workout_started.wait, 1.0))

            client.user_turn_id = 2
            navigation = client.schedule_function_call(
                {
                    "call_id": "repeat-navigation",
                    "name": "navigation_goto",
                    "arguments": '{"point":"origin"}',
                }
            )
            self.assertTrue(await asyncio.to_thread(navigation_started.wait, 1.0))

            client.user_turn_id = 3
            light = client.schedule_function_call(
                {
                    "call_id": "repeat-light",
                    "name": "light_control",
                    "arguments": '{"action":"on"}',
                }
            )
            await asyncio.gather(workout, navigation, light)

            spoken = [str(item.get("text") or "") for item in speaker.events]
            self.assertEqual(sum("还要" in item for item in spoken), 1)
            self.assertFalse(any("运动旧结果" in item or "导航旧结果" in item for item in spoken))
            self.assertTrue(any("最后插入的任务完成" in item for item in spoken))
            self.assertEqual(client.task_coordinator.state, "awaiting_resume")
            self.assertEqual(client.task_coordinator.suspended.task_name, "push_up")

    async def test_decline_clears_suspended_task_without_invoking_skill(self) -> None:
        calls: list[tuple[str, dict]] = []

        class Bridge:
            scenario_catalog = None

            @staticmethod
            def recover_explicit_plan(_text):
                return None

            @staticmethod
            def recover_contextual_plan(_text, _context):
                return None

            @staticmethod
            def invoke(name, arguments, *_context):
                calls.append((name, dict(arguments)))
                return {"ok": True, "executed": True, "spoken_summary": "完成。"}

        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                SimpleNamespace(),
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            client.websocket = FakeWebSocket()
            speaker = RecordingSpeaker()
            client.skill_event_speaker = speaker
            client.skill_bridge = Bridge()
            client.task_coordinator.start(TaskSnapshot("push_up", {"duration": 30}, count=3))
            client.task_coordinator.interrupt("navigation_goto", {"point": "origin"})
            client.task_coordinator.interruption_completed()

            client._speak_internal("attention", "刚才的运动还要继续吗？", event_id="ask-resume-test-decline")
            await client.accept_input_transcript("不用了，今天先到这里")

            self.assertEqual(client.task_coordinator.state, "idle")
            self.assertEqual(calls, [])
            self.assertTrue(any("不继续" in str(item.get("text")) for item in speaker.events))

    def test_resume_reply_classifier_does_not_steal_continue_meeting(self) -> None:
        self.assertIsNone(RealtimeConversation._classify_resume_reply("继续开会", "push_up"))
        self.assertTrue(RealtimeConversation._classify_resume_reply("继续刚才的俯卧撑吧", "push_up"))
        self.assertFalse(RealtimeConversation._classify_resume_reply("不用继续了", "push_up"))

    def test_scenario_snapshot_restores_location_head_and_fitness_progress(self) -> None:
        procedure = {
            "parameters": {"duration": {"default": 30}, "name": {"default": "zhangsan"}},
            "steps": [
                {"skill": "navigation_goto", "arguments": {"point": "white_wall"}},
                {"skill": "head_control", "action": "up", "arguments": {}},
                {
                    "skill": "push_up",
                    "action": "run",
                    "arguments": {
                        "duration": {"$arg": "duration", "default": 30},
                        "name": {"$arg": "name", "default": "zhangsan"},
                        "projector_after_identity": True,
                    },
                },
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            client = RealtimeConversation(
                SimpleNamespace(),
                "sk-test",
                JsonLogger(Path(directory) / "events.jsonl"),
            )
            client.skill_bridge = SimpleNamespace(
                scenario_catalog=SimpleNamespace(procedures={"push_up_companion": procedure})
            )
            snapshot = client._snapshot_from_call(
                {
                    "name": "run_robot_scenario",
                    "arguments": {"scenario": "push_up_companion", "duration": 45},
                }
            )
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.task_name, "push_up")
        self.assertEqual(snapshot.location, "white_wall")
        self.assertEqual(snapshot.arguments["duration"], 45)
        self.assertEqual(snapshot.resume_prefix[0].name, "head_control")

    def test_trusted_resume_passes_formal_atomic_validator_without_scene_replay(self) -> None:
        root = Path(__file__).resolve().parents[1]
        bridge = LocalSkillBridge(
            spec_dir=root / "robot_skills" / "config" / "skill_specs",
            enabled_skills=["push_up"],
            execute=False,
            backend="subprocess",
            scenario_catalog_path=root / "scenarios" / "procedure_catalog.json",
        )
        arguments = {
            "action": "run",
            "duration": 17.5,
            "initial_count": 4,
            "initial_elapsed_seconds": 12.5,
            "resume_from_interrupt": True,
        }
        result = bridge.invoke(
            "push_up",
            arguments,
            "继续刚才暂停的任务",
            "9",
            "好，我从第4个之后继续。",
            True,
            False,
        )
        self.assertTrue(result["validation_ok"])
        self.assertFalse(result["executed"])
        self.assertEqual(result["error"], "dry_run_only_not_executed")
        self.assertIsNone(result.get("scenario"))
        self.assertEqual(result["arguments"]["initial_count"], 4)

    def test_resume_flag_without_internal_trust_cannot_bypass_scene_protection(self) -> None:
        root = Path(__file__).resolve().parents[1]
        bridge = LocalSkillBridge(
            spec_dir=root / "robot_skills" / "config" / "skill_specs",
            enabled_skills=["push_up"],
            execute=False,
            backend="subprocess",
            scenario_catalog_path=root / "scenarios" / "procedure_catalog.json",
        )
        result = bridge.invoke(
            "push_up",
            {"action": "run", "resume_from_interrupt": True},
            "继续刚才暂停的任务",
            "10",
            "刚才的俯卧撑暂停了，还要继续吗？",
            False,
            False,
        )
        self.assertEqual(result.get("scenario"), "push_up_companion")

    def test_trusted_resume_sequence_validates_navigation_head_and_counter_in_order(self) -> None:
        root = Path(__file__).resolve().parents[1]
        spoken_events: list[dict] = []
        bridge = LocalSkillBridge(
            spec_dir=root / "robot_skills" / "config" / "skill_specs",
            enabled_skills=["navigation_goto", "head_control", "push_up"],
            execute=False,
            backend="subprocess",
            scenario_catalog_path=root / "scenarios" / "procedure_catalog.json",
            event_callback=lambda event: spoken_events.append(dict(event)),
        )
        result = bridge.invoke(
            SEQUENCE_TOOL_NAME,
            {
                "tasks": [
                    {"name": "navigation_goto", "arguments": {"point": "white_wall"}},
                    {"name": "head_control", "arguments": {"direction": "up"}},
                    {
                        "name": "push_up",
                        "arguments": {
                            "action": "run",
                            "duration": 17.5,
                            "initial_count": 4,
                            "initial_elapsed_seconds": 12.5,
                            "resume_from_interrupt": True,
                        },
                    },
                ],
                "failure_policy": "stop",
            },
            "继续刚才暂停的任务",
            "11",
            "好，我回到刚才的位置，从第4个之后继续。",
            True,
            False,
        )
        self.assertTrue(result["validation_ok"])
        self.assertFalse(result["executed"])
        self.assertEqual([item["name"] for item in result["tasks"]], [
            "navigation_goto", "head_control", "push_up",
        ])
        self.assertEqual(spoken_events, [])


if __name__ == "__main__":
    unittest.main()
