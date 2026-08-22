from __future__ import annotations

import asyncio
import json
import socket
import sys
import uuid
from pathlib import Path

import pytest


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
                "manager": state == "running",
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
    assert start.calls == [(["bash", str(project / "resident_service.sh"), "start"], 780.0)]

    stop = RecordingProgramController(project, "stop")
    stopped = stop.stop()
    assert stopped["status"] == "stopped"
    assert stop.calls == [(["bash", str(project / "resident_service.sh"), "stop"], 120.0)]


def test_app_start_failure_is_not_downgraded_to_voice_only(tmp_path):
    project = tmp_path / "qwen_audio_3_realtime_flash_scenarios_resident_test"

    class FailedController(ProgramController):
        def __init__(self):
            super().__init__(project, dry_run=False)
            self.calls = []

        def status(self):
            components = {name: False for name in self.COMPONENT_NAMES}
            return {"state": "stopped", "components": components}

        def _run(self, argv, timeout):
            self.calls.append((list(argv), timeout))
            raise RuntimeError("head_target_unconfirmed")

    controller = FailedController()
    with pytest.raises(RuntimeError, match="head_target_unconfirmed"):
        controller.start()
    assert [call[0][-1] for call in controller.calls] == ["start", "stop"]


def test_app_start_rolls_back_if_any_required_component_is_missing(tmp_path):
    project = tmp_path / "qwen_audio_3_realtime_flash_scenarios_resident_test"

    class PartialController(ProgramController):
        def __init__(self):
            super().__init__(project, dry_run=False)
            self.calls = []
            self.status_calls = 0

        def status(self):
            self.status_calls += 1
            components = {name: True for name in self.COMPONENT_NAMES}
            if self.status_calls > 1:
                components["manager"] = False
            return {
                "state": "stopped" if self.status_calls == 1 else "partial",
                "components": components,
            }

        def _run(self, argv, timeout):
            self.calls.append((list(argv), timeout))
            return {"returncode": 0, "output": "mocked"}

    controller = PartialController()
    with pytest.raises(RuntimeError, match="program_incomplete_after_start: missing=manager"):
        controller.start()
    assert [call[0][-1] for call in controller.calls] == ["start", "stop"]


def test_program_status_matches_the_car_real_copy_manager():
    controller = ProgramController(Path("/home/test/qwen_audio_3_realtime_flash_scenarios_resident_test"), dry_run=False)
    assert controller.process_patterns["manager"] == "mapping_navigation_manager.py"


def test_terminal_start_in_progress_is_not_restarted_or_stopped(tmp_path, monkeypatch):
    project = tmp_path / "qwen_audio_3_realtime_flash_scenarios_resident_test"
    state_file = project / "runtime/resident_service/service_state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps({"state": "starting", "pid": 12345, "updated_at": 1}),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge_module.os, "kill", lambda _pid, _sig: None)

    controller = ProgramController(project, dry_run=False)
    monkeypatch.setattr(controller, "_pid_matches", lambda _pid, _pattern: True)
    monkeypatch.setattr(controller, "_process_running", lambda _pattern: False)
    monkeypatch.setattr(controller, "_qwen_voice_connected", lambda: False)
    calls = []
    monkeypatch.setattr(controller, "_run", lambda argv, timeout: calls.append((argv, timeout)))

    result = controller.start()
    assert result["status"] == "already_starting"
    assert result["program"]["state"] == "starting"
    assert calls == []


@pytest.mark.parametrize(
    ("command", "expected_skill", "expected_evidence"),
    [
        (
            {"action": "navigate", "x": -2.2, "y": 0.1, "yaw": -1.5708},
            "navigation_goto",
            "App 地图导航到坐标 x=-2.200, y=0.100, yaw=-1.571",
        ),
        ({"action": "light", "state": "on"}, "light_control", "App 按键打开灯光"),
        ({"action": "light", "state": "off"}, "light_control", "App 按键关闭灯光"),
        ({"action": "feed", "grams": 20}, "feeder_control", "App 按键启动投食器，投食20克"),
    ],
)
def test_app_explicit_controls_keep_semantic_guard_with_truthful_intent(
    command, expected_skill, expected_evidence
):
    bridge = Bridge.__new__(Bridge)
    captured = []

    def request(payload, timeout=0.0):
        captured.append((payload, timeout))
        return {"ok": True, "status": "accepted"}

    bridge.qwen_control_request = request
    result = bridge.execute_app_plan(command)

    assert result["ok"] is True
    assert len(captured) == 1
    payload, timeout = captured[0]
    assert payload["op"] == "app_skill"
    assert payload["skill"] == expected_skill
    assert payload["user_text"] == expected_evidence
    assert payload["user_text"] != "App 控制"
    assert timeout == 155.0


def test_app_voice_waits_through_a_short_realtime_reconnect(monkeypatch):
    controller = ProgramController(Path("/tmp/project"), dry_run=False)
    samples = iter((False, False, True))
    monkeypatch.setattr(controller, "_qwen_voice_connected", lambda: next(samples))
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)
    assert controller.wait_for_voice_ready(10.0) is True


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


def test_link_heartbeat_is_independent_of_ros_and_carries_live_status(monkeypatch):
    async def verify():
        bridge = Bridge(["http://127.0.0.1:9"], "token", dry_run=True)
        sent = []

        class Socket:
            async def send(self, payload):
                sent.append(json.loads(payload))

        bridge.websocket = Socket()
        bridge.program_state = {"state": "running"}
        bridge.task_state = {"active": False, "queued": 0}
        bridge.microphone_state = {"enabled": True, "accepting_local_voice": True}
        monkeypatch.setattr(bridge_module, "LINK_HEARTBEAT_INTERVAL_SEC", 0.01)
        task = asyncio.create_task(bridge.link_heartbeat_loop())
        while len(sent) < 2:
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert all(item["type"] == "link_heartbeat" for item in sent)
        assert sent[-1]["program"]["state"] == "running"
        assert sent[-1]["microphone"]["accepting_local_voice"] is True

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
