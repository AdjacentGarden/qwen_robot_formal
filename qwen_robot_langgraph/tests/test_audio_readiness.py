from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import threading
import time

import httpx
import pytest

from robot_graph.contracts import Action
from robot_graph.dialogue import Dialogue
from robot_graph.execution import SimulatedBackend
from robot_graph.runtime import Runtime
from robot_graph.server import create_server
from robot_graph.workflows import atomic_plan


@pytest.fixture
def audio_api(tmp_path):
    backend=SimulatedBackend()
    backend.root=tmp_path
    rt=Runtime(tmp_path/'state',backend)
    server=create_server(rt,Dialogue(rt),0)
    thread=threading.Thread(target=server.serve_forever);thread.start()
    client=httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',timeout=10)
    try:
        yield rt,client
    finally:
        client.close();server.shutdown();thread.join();server.server_close();rt.close()


def test_recording_excludes_playback_and_same_key_concurrent_capture(audio_api,monkeypatch):
    rt,client=audio_api
    entered=threading.Event();finish=threading.Event()
    def capture(*args):
        entered.set()
        assert finish.wait(5)
        return {'text':'请检查一下设备状态','captured_seconds':4}
    monkeypatch.setattr('robot_graph.local_audio.record_and_transcribe',capture)
    with ThreadPoolExecutor() as pool:
        future=pool.submit(client.post,'/voice-turn',json={'session':'s','request_id':'same'})
        try:
            assert entered.wait(3)
            assert client.get('/audio-status').json()['reason']=='microphone_busy'
            duplicate=client.post('/voice-turn',json={'session':'other','request_id':'same'})
            assert duplicate.status_code==400 and duplicate.json()['error']=='microphone_busy'
            task=rt.submit('other','speech',atomic_plan(Action('speech.say',{'text':'queued speech'})))
            rt.tick_all()
            assert rt.status(task)['status']=='waiting_resources'
            assert not rt.executor.backend.calls
        finally:
            finish.set()
        response=future.result()
    assert response.status_code==200
    assert response.json()['intent']['kind']=='system.status'
    assert not rt.store.all('SELECT * FROM leases')
    rt.tick_all()
    assert rt.store.one("SELECT id FROM operations WHERE task=?",(task,))


@pytest.mark.parametrize('blocker',['unknown_lease','pending_announcement','speaker_test'])
def test_global_audio_blockers_prevent_capture(audio_api,monkeypatch,blocker):
    rt,client=audio_api
    def capture(*args):
        pytest.fail('blocked request must not record')
    monkeypatch.setattr('robot_graph.local_audio.record_and_transcribe',capture)
    if blocker=='unknown_lease':
        rt.store.claim('voice:unknown',0,['speaker'])
    elif blocker=='pending_announcement':
        parent=rt.submit('other','time',atomic_plan(Action('system.time')))
        rt.speech.enqueue(parent,0,'pending','other session announcement')
    else:
        rt.executor.backend.delays['speaker.test']=.5
        rt.submit('other','test',atomic_plan(Action('speaker.test')))
        rt.tick_all()
    assert client.get('/tasks',params={'session':'s'}).json()['tasks']==[]
    audio=client.get('/audio-status').json()
    assert not audio['ready']
    response=client.post('/voice-turn',json={'session':'s','request_id':'record'})
    assert response.status_code==400 and response.json()['error']=='speaker_busy_try_after_playback'


def test_capture_failure_releases_both_audio_resources(audio_api,monkeypatch):
    rt,client=audio_api
    def capture(*args):raise RuntimeError('capture_failed')
    monkeypatch.setattr('robot_graph.local_audio.record_and_transcribe',capture)
    response=client.post('/voice-turn',json={'request_id':'failure'})
    assert response.status_code==503 and response.json()['error']=='capture_failed'
    assert client.get('/audio-status').json()['ready']
    assert not rt.store.all('SELECT * FROM leases')


@pytest.mark.parametrize('outcome',['completed','failed','cancelled','unknown'])
def test_restart_reconciles_historical_voice_rows_without_replay(tmp_path,outcome):
    rt=Runtime(tmp_path,SimulatedBackend(failures={} if outcome=='completed' else {'speech.say':outcome}))
    task=rt.submit('s','time',atomic_plan(Action('system.time')))
    rt.speech.enqueue(task,0,'old','result')
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        rt.tick_all()
        row=rt.store.one("SELECT * FROM tasks WHERE id='voice:old'")
        if row and row['status']==outcome:break
        time.sleep(.01)
    assert row['status']==outcome
    with rt.store.tx() as db:
        db.execute("UPDATE tasks SET status='running' WHERE id='voice:old'")
    rt.close()
    backend=SimulatedBackend();rt=Runtime(tmp_path,backend)
    try:
        assert rt.store.one("SELECT status FROM tasks WHERE id='voice:old'")['status']==outcome
        assert bool(rt.store.one("SELECT * FROM leases WHERE resource='speaker'"))==(outcome=='unknown')
        rt.tick_all()
        assert not backend.calls
    finally:rt.close()


def test_acceptance_waits_for_hidden_audio_and_preserves_error_body():
    path=Path(__file__).parents[1]/'scripts/stationary_api_support.py'
    spec=importlib.util.spec_from_file_location('stationary_api_support',path)
    support=importlib.util.module_from_spec(spec);spec.loader.exec_module(support)
    polls=[]
    def handler(request):
        if request.url.path=='/tasks':return httpx.Response(200,json={'tasks':[]})
        polls.append(1)
        return httpx.Response(200,json={'ready':len(polls)>2})
    with httpx.Client(base_url='http://test',transport=httpx.MockTransport(handler)) as client:
        assert support.wait_quiet(client,'s',timeout=2)['audio']['ready']
    assert len(polls)>2
    response=httpx.Response(400,json={'error':'speaker_busy_try_after_playback'},request=httpx.Request('POST','http://test/voice-turn'))
    with pytest.raises(httpx.HTTPStatusError) as error:response.raise_for_status()
    record=support.error_record(error.value)
    assert record['http_status']==400
    assert 'speaker_busy_try_after_playback' in record['response_body']
    assert record['request_path']=='/voice-turn'
