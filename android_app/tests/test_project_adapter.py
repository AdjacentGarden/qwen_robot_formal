from __future__ import annotations

import asyncio
import socket
import sys
import uuid
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "robot_bridge"))

import bridge as bridge_module  # noqa: E402
from bridge import Bridge, ProgramController, compatibility_microphone_value  # noqa: E402


class RecordingProgramController(ProgramController):
    def __init__(self, project: Path, operation: str) -> None:
        super().__init__(project, dry_run=False)
        self.operation = operation
        self.calls = []
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        if self.operation == "start":
            state = "stopped" if self.status_calls == 1 else "running"
        else:
            state = "running" if self.status_calls == 1 else "stopped"
        return {
            "state": state,
            "components": {
                "base": state == "running",
                "odometry": state == "running",
                "navigation": state == "running",
                "resident": state == "running",
                "skill_host": state == "running",
                "voice": state == "running",
            },
        }

    def _run(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        return {"returncode": 0, "output": "mocked"}


def test_program_buttons_target_qwen_realtime_resident_service(tmp_path):
    project = tmp_path / "qwen_audio_3_realtime_flash_scenarios_resident_test"
    start = RecordingProgramController(project, "start")
    started = start.start()
    assert started["status"] == "started"
    assert start.calls == [(["bash", str(project / "resident_service.sh"), "start"], 240.0)]

    stop = RecordingProgramController(project, "stop")
    stopped = stop.stop()
    assert stopped["status"] == "stopped"
    assert stop.calls == [(["bash", str(project / "resident_service.sh"), "stop"], 120.0)]


def test_program_status_matches_the_actual_zhenghang_navigation_launch():
    controller = ProgramController(Path("/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test"), dry_run=False)
    assert controller.process_patterns["navigation"] == "robot_bringup real_robot_nav.launch.py"


def test_every_app_button_command_has_a_non_hardware_dry_run_result():
    async def verify():
        bridge = Bridge(["http://127.0.0.1:9"], "token", dry_run=True)
        bridge.program.status = lambda: {
            "state": "running",
            "components": {name: True for name in bridge.program.COMPONENT_NAMES},
        }
        sent = []

        class Socket:
            async def send(self, payload):
                sent.append(payload)

        bridge.websocket = Socket()
        commands = [
            {"id": "start", "action": "program_start"},
            {"id": "status", "action": "program_status"},
            {"id": "move", "action": "manual_move", "direction": "forward", "duration": 0.1, "linear_speed": 0.1},
            {"id": "nav", "action": "navigate", "x": 0.0, "y": 0.0, "yaw": 0.0},
            {"id": "light", "action": "light", "state": "on"},
            {"id": "feed", "action": "feed", "grams": 10},
            {"id": "voice", "action": "voice_audio", "duration_ms": 1000},
            {"id": "mic-off", "action": "microphone_set", "enabled": False},
            {"id": "mic-on", "action": "microphone_set", "enabled": True},
            {"id": "request", "action": "request_state"},
            {"id": "stop_task", "action": "stop"},
            {"id": "stop_program", "action": "program_stop"},
        ]
        for command in commands:
            await bridge.execute_command(command)
        results = [item for item in sent if '"type": "command_result"' in item]
        assert len(results) == len(commands)
        assert all('"ok": true' in item for item in results)

    asyncio.run(verify())


def test_old_relay_microphone_compatibility_ids_are_strict_and_side_effect_free():
    assert compatibility_microphone_value("mic-set-0-12345678") is False
    assert compatibility_microphone_value("mic-set-1-abcdefghi") is True
    assert compatibility_microphone_value("normal-request-state") is None
    assert compatibility_microphone_value("mic-set-2-12345678") is None


def test_microphone_control_does_not_route_through_ros_or_app_voice():
    async def verify():
        bridge = Bridge(["http://127.0.0.1:9"], "token", dry_run=True)
        bridge.program.status = lambda: {
            "state": "running",
            "components": {name: True for name in bridge.program.COMPONENT_NAMES},
        }
        bridge.execute_app_voice = lambda _command: (_ for _ in ()).throw(AssertionError("app voice must remain independent"))
        bridge.execute_app_plan = lambda _command: (_ for _ in ()).throw(AssertionError("skills must not be invoked"))
        bridge.ros.stop = lambda: (_ for _ in ()).throw(AssertionError("ROS must not be touched"))
        sent = []

        class Socket:
            async def send(self, payload):
                sent.append(payload)

        bridge.websocket = Socket()
        await bridge.execute_command({"id": "mic-set-0-12345678", "action": "request_state"})
        await bridge.execute_command({"id": "mic-set-1-abcdefgh", "action": "request_state"})
        assert all('"action": "microphone_set"' in item for item in sent)
        assert all('"ok": true' in item for item in sent)

    asyncio.run(verify())


def test_stale_control_socket_persists_microphone_without_false_app_failure(tmp_path, monkeypatch):
    state_file = tmp_path / "microphone_state.json"
    # macOS limits AF_UNIX paths to roughly 104 bytes; pytest's tmp_path can
    # be longer than that even though production paths are short.
    stale_socket = Path("/tmp") / f"qwen-mic-{uuid.uuid4().hex}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(stale_socket))
    listener.close()
    monkeypatch.setattr(bridge_module, "MICROPHONE_STATE_FILE", state_file)
    monkeypatch.setattr(bridge_module, "APP_CONTROL_SOCKET", stale_socket)

    # Exercise this bridge method without constructing ROS; microphone
    # control deliberately has no ROS dependency.
    bridge = Bridge.__new__(Bridge)
    bridge.dry_run = False
    bridge.microphone_state = {}
    bridge.program = type("StoppedProgram", (), {})()
    bridge.program.COMPONENT_NAMES = ProgramController.COMPONENT_NAMES
    bridge.program.status = lambda: {
        "state": "stopped",
        "components": {name: False for name in bridge.program.COMPONENT_NAMES},
    }
    result = bridge.set_microphone_enabled(False)

    assert result["status"] == "saved_for_next_start"
    assert result["applied"] is False
    assert result["microphone"]["enabled"] is False
    assert result["microphone"]["app_voice_enabled"] is True
    assert state_file.stat().st_mode & 0o777 == 0o600
    stale_socket.unlink(missing_ok=True)
