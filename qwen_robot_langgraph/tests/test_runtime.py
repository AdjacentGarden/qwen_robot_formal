import json
import time

import pytest

from robot_graph.contracts import Action, PolicyError, StationaryPolicy
from robot_graph.dialogue import Dialogue
from robot_graph.execution import SimulatedBackend
from robot_graph.runtime import Runtime
from robot_graph.workflows import atomic_plan, build_plan


def test_head_plans_keep_observing_after_the_motor_hard_deadline():
    atomic = atomic_plan(Action('head.move', {'pose':'up'}))
    meeting = build_plan('meeting_stationary')
    head_steps = [x for x in atomic['steps'] + meeting['steps'] + meeting['cleanup'] if x['kind']=='head.move']
    assert head_steps and all(x['timeout']==45.0 for x in head_steps)


def drive(rt, task, states=("completed", "failed", "cancelled", "unknown"), timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rt.tick_all()
        result = rt.status(task)
        if result["status"] in states:
            return result
        time.sleep(.005)
    raise AssertionError(rt.status(task))


@pytest.fixture
def pair(tmp_path):
    driver = SimulatedBackend()
    rt = Runtime(tmp_path, driver)
    yield rt, driver
    rt.close()


def test_atomic(pair):
    rt, driver = pair
    task = rt.submit("s", "1", atomic_plan(Action("head.move", {"pose": "up"})))
    assert drive(rt, task)["status"] == "completed"
    assert [x[0] for x in driver.calls] == ["head.move"]


def test_wheel_blocked_before_any_side_effect(pair):
    rt, driver = pair
    with pytest.raises(PolicyError, match="base_motion_forbidden"):
        rt.submit("s", "1", build_plan("meeting"))
    assert not driver.calls


def test_stationary_meeting_cleanup(pair):
    rt, driver = pair
    task = rt.submit("s", "1", build_plan("meeting_stationary", {"hold_seconds": .03}))
    assert drive(rt, task)["status"] == "completed"
    calls = [x[0] for x in driver.calls if x[0] != "speech.say"]
    assert calls == ["sensor.gate", "head.move", "projector.start", "projector.off", "head.move", "sensor.gate"]
    assert not rt.store.all("SELECT * FROM leases WHERE resource != 'speaker'")


def test_dedup(pair):
    rt, driver = pair
    plan = atomic_plan(Action("feeder.feed", {"grams": 10}))
    task = rt.submit("s", "1", plan)
    assert rt.submit("s", "1", plan) == task
    drive(rt, task)
    assert rt.submit("s", "1", plan) == task
    assert len(driver.calls) == 1
    with pytest.raises(PolicyError, match="payload_mismatch"):
        rt.submit("s", "1", atomic_plan(Action("feeder.feed", {"grams": 20})))


def test_pause_and_resume_meeting(pair):
    rt, driver = pair
    task = rt.submit("s", "1", build_plan("meeting_stationary"))
    drive(rt, task, ("active",))
    rt.control("s", task, "pause")
    assert drive(rt, task, ("paused",))["status"] == "paused"
    assert any(x[0] == "projector.pause" for x in driver.calls)
    rt.control("s", task, "resume")
    assert drive(rt, task, ("active",))["status"] == "active"
    rt.control("s", task, "cancel")
    assert drive(rt, task)["status"] == "cancelled"


def test_dialogue_does_not_wait_for_navigation(pair):
    rt, driver = pair
    dialogue = Dialogue(rt)
    r = dialogue.turn("s", "a", "启动会议投影")
    assert "禁止" in r["reply"] and not driver.calls
    r = dialogue.turn("s", "b", "原地会议投影")
    assert r["task_id"]
    assert dialogue.turn("s", "b", "原地会议投影")["deduplicated"]


def test_unknown_holds_leases(pair):
    rt, driver = pair
    driver.failures["feeder.feed"] = ConnectionError("lost after write")
    task = rt.submit("s", "1", atomic_plan(Action("feeder.feed", {"grams": 10})))
    assert drive(rt, task)["status"] == "unknown"
    assert rt.store.one("SELECT task FROM leases WHERE resource='feeder'")["task"] == task
    second = rt.submit("s", "2", atomic_plan(Action("feeder.feed", {"grams": 20})))
    rt.tick_all()
    assert rt.status(second)["status"] == "waiting_resources"
    assert len(driver.calls) == 1


def test_pet_not_seen_no_feed(pair):
    rt, driver = pair
    driver.pet_visible = False
    task = rt.submit("s", "1", build_plan("observe_and_feed"))
    assert drive(rt, task)["status"] == "failed"
    assert [x[0] for x in driver.calls] == ["pet.observe"]


def test_resource_arbitration(pair):
    rt, driver = pair
    first = rt.submit("s", "1", build_plan("meeting_stationary"))
    drive(rt, first, ("active",))
    second = rt.submit("s", "2", atomic_plan(Action("head.move", {"pose": "down"})))
    independent = rt.submit("s", "3", atomic_plan(Action("system.time")))
    drive(rt, independent)
    assert rt.status(second)["status"] == "waiting_resources"
    rt.control("s", first, "cancel")
    drive(rt, first)
    assert drive(rt, second)["status"] == "completed"


def test_speech_overlaps_head(pair):
    rt, driver = pair
    driver.delays["speech.say"] = .4
    task = rt.submit("s", "1", build_plan("meeting_stationary", {"hold_seconds": .01}))
    drive(rt, task)
    calls = {x[0]: x[2] for x in reversed(driver.calls)}
    assert calls["head.move"] - calls["speech.say"] < .2


def test_cancel_before_first_tick(pair):
    rt, driver = pair
    task = rt.submit("s", "1", build_plan("meeting_stationary"))
    rt.control("s", task, "cancel")
    assert drive(rt, task)["status"] == "cancelled"
    assert not driver.calls


def test_session_fence(pair):
    rt, _ = pair
    task = rt.submit("s", "1", build_plan("meeting_stationary"))
    with pytest.raises(PolicyError):
        rt.control("other", task, "cancel")


def test_cancel_uncancellable_waits_for_ack(pair):
    rt, driver = pair
    driver.delays["head.move"] = .15
    task = rt.submit("s", "1", build_plan("meeting_stationary"))
    for _ in range(6):
        rt.tick_all()
        time.sleep(.01)
    rt.control("s", task, "cancel")
    assert drive(rt, task)["status"] == "cancelled"
    assert not any(x[0] == "projector.start" for x in driver.calls)


def test_pause_exercise_preserves_remaining(pair):
    rt, driver = pair
    driver.delays["exercise.count"] = .3
    task = rt.submit("s", "1", atomic_plan(Action("exercise.count", {"exercise": "squat", "count": 10})))
    rt.tick_all()
    time.sleep(.12)
    rt.control("s", task, "pause")
    state = drive(rt, task, ("paused",))
    assert state["state"]["partial_count"] >= 3
    rt.control("s", task, "resume")
    assert drive(rt, task)["status"] == "completed"
    assert driver.calls[-1][1]["count"] < 10


def test_restart_completed_no_repeat(tmp_path):
    d = SimulatedBackend()
    rt = Runtime(tmp_path, d)
    task = rt.submit("s", "1", atomic_plan(Action("feeder.feed", {"grams": 10})))
    drive(rt, task)
    rt.close()
    rt = Runtime(tmp_path, d)
    rt.tick_all()
    assert rt.status(task)["status"] == "completed" and len(d.calls) == 1
    rt.close()


@pytest.mark.parametrize("action", [Action("base.move", {"direction": "forward"}), Action("pet.track"), Action("person.track"), Action("navigation.goto", {"point": "origin"}), Action("head.move", {"pose": "up", "execute": True}), Action("feeder.feed", {"grams": 15}), Action("feeder.feed", {"grams": True}), Action("sensor.gate", {"enabled": "false"}), Action("camera.capture", {"camera": "../../tmp"}), Action("head.move", {"pose": "up"}, float("nan")), Action("shell", {"cmd": "x"})])
def test_policy_rejects(action):
    with pytest.raises(PolicyError):
        StationaryPolicy().validate(action)


class SimulationOnlyNavigationPolicy(StationaryPolicy):
    def validate(self,action):
        from robot_graph.contracts import CAPABILITIES
        if action.kind=='navigation.goto':return CAPABILITIES[action.kind]
        return super().validate(action)


def test_full_meeting_navigation_in_simulator_only(tmp_path):
    driver=SimulatedBackend(delays={'navigation.goto':.08,'speech.say':.3})
    rt=Runtime(tmp_path,driver,policy=SimulationOnlyNavigationPolicy())
    try:
        task=rt.submit('sim','nav',build_plan('meeting',{'hold_seconds':.02}))
        assert drive(rt,task)['status']=='completed'
        kinds=[x[0] for x in driver.calls]
        assert kinds[0]=='navigation.goto'
        assert kinds.index('head.move')>kinds.index('navigation.goto')
        assert not any(k.startswith('base.') for k in kinds)
    finally:rt.close()


def test_failed_navigation_never_projects(tmp_path):
    driver=SimulatedBackend(failures={'navigation.goto':'failed'})
    rt=Runtime(tmp_path,driver,policy=SimulationOnlyNavigationPolicy())
    try:
        task=rt.submit('sim','nav',build_plan('meeting'))
        assert drive(rt,task)['status']=='failed'
        assert not any(k[0]=='projector.start' for k in driver.calls)
    finally:rt.close()


def test_time_uses_robot_user_timezone(tmp_path):
    import threading
    from robot_graph.hardware import HardwareBackend
    result=HardwareBackend(tmp_path).run(Action('system.time'),threading.Event())
    assert result['time'].endswith('+08:00')
