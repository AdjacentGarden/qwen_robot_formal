from __future__ import annotations

import base64
import asyncio
import json
import queue
import threading
import tempfile
import unittest
from pathlib import Path

from realtime_core import (
    ConversationState,
    build_session_update,
    build_websocket_url,
    pcm_rms,
)
from realtime_chat import (
    AudioEngine,
    RECONNECTABLE_SERVICE_ERRORS,
    RealtimeConversation,
    is_benign_service_error,
    load_microphone_enabled,
    save_microphone_enabled,
)


class RealtimeCoreTests(unittest.TestCase):
    def test_audio_close_aborts_streams_without_restart_race(self):
        calls = []

        class Stream:
            def __init__(self, name):
                self.name = name

            def abort_stream(self):
                calls.append((self.name, "abort"))

            def stop_stream(self):
                calls.append((self.name, "stop"))

            def close(self):
                calls.append((self.name, "close"))

        class Worker:
            def join(self, timeout):
                calls.append(("worker", "join", timeout))

        class Interface:
            def terminate(self):
                calls.append(("interface", "terminate"))

        audio = AudioEngine.__new__(AudioEngine)
        audio.closed = threading.Event()
        audio.generation = 0
        audio.output_queue = queue.Queue(maxsize=4)
        audio.input_stream = Stream("input")
        audio.output_stream = Stream("output")
        audio.worker = Worker()
        audio.interface = Interface()

        audio.close()

        self.assertTrue(audio.closed.is_set())
        self.assertEqual(audio.generation, 1)
        self.assertLess(calls.index(("output", "abort")), calls.index(("worker", "join", 5.0)))
        self.assertNotIn(("output", "start"), calls)

    def test_microphone_preference_is_atomic_persistent_and_defaults_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "microphone_state.json"
            self.assertTrue(load_microphone_enabled(path))
            save_microphone_enabled(path, False)
            self.assertFalse(load_microphone_enabled(path))
            self.assertEqual(json.loads(path.read_text())["enabled"], False)
            save_microphone_enabled(path, True)
            self.assertTrue(load_microphone_enabled(path))

    def test_only_transient_idle_errors_are_reconnectable(self):
        self.assertIn("response_idle_timeout", RECONNECTABLE_SERVICE_ERRORS)
        self.assertNotIn("invalid_api_key", RECONNECTABLE_SERVICE_ERRORS)

    def test_only_the_no_active_response_cancel_race_is_ignored(self):
        self.assertTrue(
            is_benign_service_error(
                "invalid_value",
                "Conversation has no active response.",
            )
        )
        self.assertFalse(is_benign_service_error("invalid_value", "bad session parameter"))
        self.assertFalse(is_benign_service_error("invalid_api_key", "no active response"))
    def test_empty_workspace_uses_verified_public_beijing_url(self) -> None:
        self.assertEqual(
            build_websocket_url("", "cn-beijing"),
            "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
            "?model=qwen-audio-3.0-realtime-flash",
        )

    def test_builds_flash_workspace_url(self) -> None:
        url = build_websocket_url("workspace-123", "cn-beijing")
        self.assertEqual(
            url,
            "wss://workspace-123.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
            "?model=qwen-audio-3.0-realtime-flash",
        )

    def test_rejects_invalid_workspace_and_region(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid_workspace_id"):
            build_websocket_url("https://bad")
        with self.assertRaisesRegex(ValueError, "unsupported_region"):
            build_websocket_url("workspace-123", "unknown")

    def test_smart_turn_session_matches_official_contract(self) -> None:
        event = build_session_update(
            voice="longanqian",
            instructions="自然交流",
            turn_detection="smart_turn",
            silence_duration_ms=800,
            threshold=0.5,
            max_history_turns=80,
        )
        session = event["session"]
        self.assertEqual(session["turn_detection"], {"type": "smart_turn"})
        self.assertEqual(session["input_audio_format"], "pcm")
        self.assertEqual(session["output_audio_format"], "pcm")
        self.assertEqual(session["max_history_turns"], 50)

    def test_session_can_register_function_tools(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "projector_control",
                    "description": "控制投影",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        session = build_session_update(
            voice="longanqian",
            instructions="调用本地工具",
            turn_detection="smart_turn",
            silence_duration_ms=800,
            threshold=0.5,
            max_history_turns=20,
            tools=tools,
        )["session"]
        self.assertEqual(session["tools"], tools)

    def test_server_vad_values_are_bounded(self) -> None:
        session = build_session_update(
            voice="",
            instructions="",
            turn_detection="server_vad",
            silence_duration_ms=10_000,
            threshold=2,
            max_history_turns=0,
        )["session"]
        self.assertEqual(session["voice"], "longanqian")
        self.assertEqual(session["turn_detection"]["silence_duration_ms"], 3000)
        self.assertEqual(session["turn_detection"]["threshold"], 1.0)
        self.assertEqual(session["max_history_turns"], 1)

    def test_state_streams_audio_and_transcripts(self) -> None:
        state = ConversationState()
        state.process({"type": "response.created", "response": {"id": "response-1"}})
        pcm = b"\x01\x00\x02\x00"
        actions = state.process(
            {"type": "response.audio.delta", "delta": base64.b64encode(pcm).decode()}
        )
        self.assertEqual([(action.kind, action.value) for action in actions], [("play_audio", pcm)])
        user = state.process(
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "你好"}
        )
        assistant = state.process(
            {"type": "response.audio_transcript.done", "transcript": "你好，有什么可以帮你？"}
        )
        self.assertEqual(user[0].kind, "input_transcript")
        self.assertEqual(assistant[0].kind, "output_transcript")

    def test_transcription_failure_is_visible_to_the_conversation_guard(self) -> None:
        state = ConversationState()
        actions = state.process(
            {
                "type": "conversation.item.input_audio_transcription.failed",
                "item_id": "audio-17",
                "error": {"code": "asr_failed", "message": "no final transcript"},
            }
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "input_transcription_failed")
        self.assertEqual(actions[0].value["item_id"], "audio-17")
        self.assertEqual(actions[0].value["code"], "asr_failed")

    def test_speech_started_cancels_response_and_suppresses_stale_audio(self) -> None:
        state = ConversationState()
        state.process({"type": "response.created", "response": {"id": "response-1"}})
        actions = state.process({"type": "input_audio_buffer.speech_started"})
        self.assertEqual([action.kind for action in actions], ["interrupt_playback", "cancel_response"])
        stale = state.process(
            {"type": "response.audio.delta", "delta": base64.b64encode(b"stale").decode()}
        )
        self.assertEqual(stale, [])
        state.process({"type": "response.created", "response": {"id": "response-2"}})
        fresh = state.process(
            {"type": "response.audio.delta", "delta": base64.b64encode(b"fresh").decode()}
        )
        self.assertEqual(fresh[0].value, b"fresh")

    def test_service_error_is_structured(self) -> None:
        actions = ConversationState().process(
            {"type": "error", "error": {"code": "bad", "message": "failed"}}
        )
        self.assertEqual(actions[0].kind, "error")
        self.assertEqual(actions[0].value, {"code": "bad", "message": "failed"})

    def test_function_call_arguments_done_becomes_action(self) -> None:
        actions = ConversationState().process(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call-1",
                "name": "projector_control",
                "arguments": '{"action":"on"}',
            }
        )
        self.assertEqual(actions[0].kind, "function_call")
        self.assertEqual(actions[0].value["name"], "projector_control")
        self.assertEqual(actions[0].value["call_id"], "call-1")

    def test_pcm_rms(self) -> None:
        self.assertEqual(pcm_rms(b"\x00\x00" * 10), 0.0)
        self.assertAlmostEqual(pcm_rms((1000).to_bytes(2, "little", signed=True) * 10), 1000.0)
        self.assertEqual(pcm_rms(b"bad"), 0.0)


class MicrophoneUploadGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_socket_mutes_only_local_audio_and_keeps_app_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conversation = RealtimeConversation.__new__(RealtimeConversation)
            conversation.local_microphone_enabled = True
            conversation.microphone_state_file = root / "microphone_state.json"
            conversation.app_voice_root = root
            conversation.external_audio_queue = asyncio.Queue(maxsize=8)
            conversation.external_audio_active = False
            conversation.connected = True
            conversation.audio = None
            conversation.send_lock = asyncio.Lock()

            class Logger:
                def write(self, *_args, **_kwargs):
                    return None

            class WebSocket:
                def __init__(self):
                    self.messages = []

                async def send(self, payload):
                    self.messages.append(json.loads(payload))

            conversation.logger = Logger()
            conversation.websocket = WebSocket()

            socket_path = root / "control.sock"
            server = await asyncio.start_unix_server(
                conversation.handle_control_client,
                path=str(socket_path),
            )

            async def request(payload):
                reader, writer = await asyncio.open_unix_connection(str(socket_path))
                writer.write(json.dumps(payload).encode() + b"\n")
                await writer.drain()
                response = json.loads((await reader.readline()).decode())
                writer.close()
                await writer.wait_closed()
                return response

            try:
                muted = await request({"op": "microphone_set", "enabled": False})
                self.assertTrue(muted["ok"])
                self.assertFalse(conversation.local_microphone_enabled)
                self.assertTrue(muted["microphone"]["app_voice_enabled"])
                self.assertEqual(conversation.websocket.messages[-1]["type"], "input_audio_buffer.clear")

                pcm_path = root / "app-command.pcm"
                pcm_path.write_bytes(b"\x01\x00" * 2000)
                accepted = await request({"op": "app_voice", "pcm_path": str(pcm_path)})
                self.assertTrue(accepted["ok"])
                self.assertEqual(conversation.external_audio_queue.qsize(), 1)

                enabled = await request({"op": "microphone_set", "enabled": True})
                self.assertTrue(enabled["ok"])
                self.assertTrue(conversation.local_microphone_enabled)
                self.assertTrue(load_microphone_enabled(conversation.microphone_state_file))
                self.assertTrue(enabled["microphone"]["recovering"])
                self.assertFalse(enabled["microphone"]["accepting_local_voice"])
                self.assertGreater(enabled["microphone"]["ready_after_ms"], 0)
                self.assertEqual(
                    [item["type"] for item in conversation.websocket.messages],
                    ["input_audio_buffer.clear", "input_audio_buffer.clear"],
                )
                conversation.local_microphone_ready_monotonic = 0.0
                settled = conversation.microphone_status_payload()
                self.assertFalse(settled["recovering"])
                self.assertEqual(settled["ready_after_ms"], 0)
            finally:
                server.close()
                await server.wait_closed()

    async def test_disabled_local_microphone_is_read_but_never_uploaded(self):
        conversation = RealtimeConversation.__new__(RealtimeConversation)
        conversation.stop_event = asyncio.Event()
        conversation.local_microphone_enabled = False
        conversation.external_audio_active = False
        loop = asyncio.get_running_loop()

        class Audio:
            def read_microphone(self):
                loop.call_soon_threadsafe(conversation.stop_event.set)
                return b"\x01\x00" * 160

            def microphone_allowed(self, *_args):
                raise AssertionError("disabled local audio must be discarded before VAD")

        conversation.audio = Audio()

        async def unexpected_send(_payload):
            raise AssertionError("disabled local microphone uploaded audio")

        conversation.send = unexpected_send
        await conversation.send_microphone()

    async def test_reenabled_microphone_drains_settling_frames_without_uploading(self):
        conversation = RealtimeConversation.__new__(RealtimeConversation)
        conversation.stop_event = asyncio.Event()
        conversation.local_microphone_enabled = True
        conversation.local_microphone_ready_monotonic = float("inf")
        conversation.external_audio_active = False
        loop = asyncio.get_running_loop()

        class Audio:
            def read_microphone(self):
                loop.call_soon_threadsafe(conversation.stop_event.set)
                return b"\x01\x00" * 160

            def microphone_allowed(self, *_args):
                raise AssertionError("settling local audio must be discarded before VAD")

        conversation.audio = Audio()

        async def unexpected_send(_payload):
            raise AssertionError("settling local microphone uploaded transition audio")

        conversation.send = unexpected_send
        await conversation.send_microphone()

    async def test_enabled_local_microphone_still_uses_existing_upload_path(self):
        conversation = RealtimeConversation.__new__(RealtimeConversation)
        conversation.stop_event = asyncio.Event()
        conversation.local_microphone_enabled = True
        conversation.external_audio_active = False
        loop = asyncio.get_running_loop()
        sent = []

        class Audio:
            def read_microphone(self):
                loop.call_soon_threadsafe(conversation.stop_event.set)
                return b"\x01\x00" * 160

            def microphone_allowed(self, *_args):
                return True

        conversation.audio = Audio()
        conversation.args = type("Args", (), {"echo_mode": "speaker-safe", "echo_tail_seconds": 0.5, "noise_gate": 500.0})()

        async def record(payload):
            sent.append(payload)

        conversation.send = record
        await conversation.send_microphone()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["type"], "input_audio_buffer.append")


if __name__ == "__main__":
    unittest.main()
