"""Offline only: every camera, motor, speech output and skill is replaced."""
import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from test_pet_centering import _load_pet_module
from test_task_interruption import FakeWebSocket, RecordingSpeaker
from video_recording import PetVideoError, record_frames
from realtime_chat import JsonLogger, RealtimeConversation
from runtime_supervisor import TaskSnapshot
from scenario_engine import ScenarioCatalog, ScenarioExecutor

ROOT = Path(__file__).resolve().parents[1]
FRAME = np.zeros((48, 64, 3), dtype=np.uint8)


class Camera:
    def read(self):
        return True, FRAME.copy()

    def isOpened(self):
        return True

    def release(self):
        pass


class Writer:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame.copy())


def test_slow_inference_does_not_reduce_video_fps():
    writer = Writer()
    caller = threading.get_ident()
    def slow(frame):
        assert threading.get_ident() == caller
        frame[:] = 255
        time.sleep(.18)
    result = record_frames(Camera(), writer, FRAME, 1, 15, slow)
    assert result['written_frames'] == 15
    assert 4 <= result['analysis_frames'] <= 7
    assert len(writer.frames) == 15
    assert all(not frame.any() for frame in writer.frames)


def test_recording_reports_missing_camera_frames():
    cap = SimpleNamespace(read=lambda: (False, None))
    result = record_frames(cap, Writer(), FRAME, .3, 15)
    assert result['written_frames'] == 1
    assert result['missed_frames'] == 4


def test_writer_failure_is_optional_video_error():
    def fail(_frame):
        raise OSError('disk full')
    with pytest.raises(PetVideoError, match='disk full'):
        record_frames(Camera(), SimpleNamespace(write=fail), FRAME, .3, 15)


def test_detection_or_control_failure_is_not_downgraded_to_video_warning():
    def fail(_frame):
        raise RuntimeError('detector failed')
    with pytest.raises(RuntimeError, match='detector failed') as exc:
        record_frames(Camera(), Writer(), FRAME, .3, 15, fail)
    assert not isinstance(exc.value, PetVideoError)
    assert not any(t.name == 'pet-video-recorder' for t in threading.enumerate())


def test_camera_disconnection_does_not_replay_stale_cached_frames(monkeypatch):
    pet = _load_pet_module()
    camera = object.__new__(pet.PetTrackingSystem.CameraReader)
    camera._lock = threading.Lock()
    camera.ret, camera.frame = True, FRAME
    camera.last_frame_at = time.monotonic()
    assert camera.read()[0]
    camera.last_frame_at -= 1
    assert camera.read() == (False, None)


@pytest.mark.parametrize('failure', ['short_video', 'transcode', 'detector', 'not_centered', 'not_found'])
def test_search_keeps_found_fact_but_not_detector_errors(monkeypatch, failure):
    pet = _load_pet_module()
    rect = SimpleNamespace(left=0, right=10, top=10, bottom=30) if failure == 'not_centered' else SimpleNamespace(left=24, right=40, top=10, bottom=30)
    detection = SimpleNamespace(rect=rect, score=.99, confidence=.99)
    detector = SimpleNamespace(detect=lambda *a, **k: [] if failure == 'not_found' else [detection])
    monkeypatch.setattr(pet.runtime_models, 'acquire_or_create', lambda *a, **k: (detector, False))
    monkeypatch.setattr(pet.PetTrackingSystem, 'CameraReader', lambda _: Camera())
    monkeypatch.setattr(pet.PetTrackingSystem, '_create_board', lambda: object())
    monkeypatch.setattr(pet.PetTrackingSystem, '_release_board', lambda _: None)
    commands = []
    monkeypatch.setattr(pet.PetTrackingSystem, 'set_motor', lambda b, speed_right, speed_left, **k: commands.append((speed_right, speed_left)))
    monkeypatch.setattr(pet.PetTrackingSystem, '_show_frame', lambda *a: None)
    monkeypatch.setattr(pet.PetTrackingSystem, '_close_windows', lambda: None)
    monkeypatch.setattr(pet.PetTrackingSystem, '_write_result_payload', lambda _: None)
    monkeypatch.setattr(pet, 'speak', lambda _: None)
    monkeypatch.setenv('PET_FIND_CENTER_TIMEOUT_SEC', '0.1')
    def record(cap, frame, duration_sec, frame_callback):
        for _ in range(3):
            frame_callback(FRAME.copy())
        if failure == 'detector':
            raise RuntimeError('detector_failed')
        raise PetVideoError(f'post_detection_{failure}_failed')
    monkeypatch.setattr(pet.PetTrackingSystem, '_record_found_video_for_app', record)
    result = pet.PetTrackingSystem.background_pet_search_task('/dev/fake', 'fake.rknn', 'dog', timeout_sec=.1)
    if failure in {'not_found', 'not_centered'}:
        assert not result['found']
        assert not result['centered']
        assert result['detected'] == (failure == 'not_centered')
        assert result['ok'] == (failure == 'not_found')
        assert commands[-1] == (0, 0)
        return
    assert result['detected'] and result['centered']
    assert result['found'] == (failure != 'detector')
    assert result['ok'] == (failure != 'detector')
    assert result['video_status'] == 'failed'
    assert commands[-1] == (0, 0)


@pytest.mark.parametrize('scenario', ['find_pet_here', 'find_pet_at', 'find_pet', 'find_and_feed_doudou'])
@pytest.mark.parametrize('feed_ok', [True, False])
def test_video_failure_does_not_abort_search_or_invent_upload(scenario, feed_ok):
    calls = []
    def invoke(skill, arguments):
        calls.append(skill)
        if skill == 'pet_tracking':
            return {'ok': True, 'validation_ok': True, 'executed': True,
                    'structured_result': {'found': True, 'video_status': 'failed', 'video_error': 'disk_full'}}
        if skill == 'feeder_control' and not feed_ok:
            return {'ok': False, 'executed': True, 'validation_ok': False,
                    'error': 'device_offline', 'spoken_summary': '投食器离线。'}
        return {'ok': True, 'executed': True, 'validation_ok': True}
    arguments = {'point': 'study_projection'} if scenario == 'find_pet_at' else {}
    result = ScenarioExecutor(ScenarioCatalog(ROOT/'scenarios/procedure_catalog.json'), invoke).execute(scenario, arguments)
    assert '已经找到豆豆' in result['spoken_summary'], result
    assert '没有视频同步' in result['spoken_summary']
    assert '正传到手机' not in result['spoken_summary']
    assert result['video_warnings'] == ['disk_full']
    assert calls.count('pet_tracking') == 1
    if scenario == 'find_and_feed_doudou':
        assert calls.count('feeder_control') == 1
        assert result['ok'] == feed_ok
        if not feed_ok:
            assert '投食器离线' in result['spoken_summary']
    else:
        assert 'feeder_control' not in calls
        assert result['ok']


@pytest.mark.parametrize('text', ['是的', '好的', '继续吧', '不用了'])
@pytest.mark.parametrize('context', ['current', 'other_question', 'expired', 'intervening_turn'])
def test_resume_confirmation_is_bound_to_current_question(tmp_path, text, context):
    async def run():
        client = RealtimeConversation(SimpleNamespace(), 'sk-test', JsonLogger(tmp_path/'events.jsonl'))
        client.websocket = FakeWebSocket()
        client.skill_event_speaker = RecordingSpeaker()
        async def capture_decision(accepted):
            pass  # no hardware task scheduling; inspect the committed decision below
        client._apply_resume_decision = capture_decision
        client.user_turn_id = 1
        client.skill_bridge = SimpleNamespace(
            scenario_catalog=None,
            recover_explicit_plan=lambda _: None,
            recover_contextual_plan=lambda *a: None,
        )
        client.task_coordinator.start(TaskSnapshot('push_up', {'duration': 30}, count=8))
        client.task_coordinator.interrupt('navigation_goto', {'point': 'origin'})
        client.task_coordinator.interruption_completed()
        client._speak_internal('attention', '刚才俯卧撑暂停了，还要继续吗？', event_id='ask-resume-test')
        if context == 'other_question':
            client._remember_local_assistant_speech('先找豆豆，找到后给它喂饭吗？')
        elif context == 'expired':
            client.resume_prompt_binding['created'] -= 130
        elif context == 'intervening_turn':
            client.user_turn_id += 1
        await client.accept_input_transcript(text, schedule_deferred=False)
        decision = (client.turn_recovery_plan or {}).get('internal_resume_decision')
        if context == 'current':
            assert decision == (text != '不用了')
        else:
            assert decision is None
        assert client.task_coordinator.suspended.count == 8
    asyncio.run(run())


@pytest.mark.parametrize('text', ['继续刚才的俯卧撑', '接着做俯卧撑', '继续刚才的运动', '不用继续俯卧撑了'])
def test_explicit_resume_or_cancel_remains_available_after_topic_change(tmp_path, text):
    async def run():
        client = RealtimeConversation(SimpleNamespace(), 'sk-test', JsonLogger(tmp_path/'events.jsonl'))
        client.websocket = FakeWebSocket()
        async def capture_decision(accepted):
            pass
        client._apply_resume_decision = capture_decision
        client.task_coordinator.start(TaskSnapshot('push_up', {'duration': 30}, count=8))
        client.task_coordinator.interrupt('navigation_goto', {})
        client.task_coordinator.interruption_completed()
        client._remember_local_assistant_speech('需要找豆豆吗？')
        await client.accept_input_transcript(text, schedule_deferred=False)
        assert client.turn_recovery_plan['internal_resume_decision'] == ('不用' not in text)
    asyncio.run(run())


def test_standalone_pet_skill_reports_missing_video():
    from skill_runner import build_spoken_summary
    message = build_spoken_summary(None, SimpleNamespace(skill_name='pet_tracking', arguments={'action': 'find'}),
        {'ok': True, 'parsed_json': {'found': True, 'video_status': 'failed'}})
    assert '已经找到豆豆' in message and '没有视频同步' in message


def test_memory_question_cannot_resume_suspended_workout(tmp_path):
    async def run():
        client = RealtimeConversation(SimpleNamespace(), 'sk-test', JsonLogger(tmp_path/'events.jsonl'))
        client.websocket = FakeWebSocket()
        client.task_coordinator.start(TaskSnapshot('push_up', {'duration': 30}, count=8))
        client.task_coordinator.interrupt('navigation_goto', {})
        client.task_coordinator.interruption_completed()
        await client.accept_input_transcript('上次继续俯卧撑后做了几个？')
        assert client.task_coordinator.suspended.count == 8
        assert client.turn_recovery_plan['name'] == 'memory_query'
    asyncio.run(run())
