import json
import sqlite3
import threading
import time
import socket
import shutil
import tempfile
from pathlib import Path

import pytest

from robot_graph.contracts import Action, PolicyError
from robot_graph.dialogue import Dialogue
from robot_graph.execution import SimulatedBackend, Unavailable
from robot_graph.runtime import Runtime
from robot_graph.workflows import atomic_plan, build_plan
from test_runtime import drive


@pytest.mark.parametrize('stage',['sensor.gate','head.move','projector.start','projector.off'])
def test_stage_failure_cleanup_recorded(tmp_path,stage):
    driver=SimulatedBackend(failures={stage:'failed'})
    rt=Runtime(tmp_path,driver)
    try:
        task=rt.submit('s','1',build_plan('meeting_stationary',{'hold_seconds':.01}))
        assert drive(rt,task)['status']=='failed'
        assert not rt.store.all("SELECT * FROM leases WHERE resource != 'speaker'")
        assert rt.store.one("SELECT id FROM events WHERE type='task_finished'")
    finally:rt.close()


def test_validate_cleanup_before_hardware(tmp_path):
    driver=SimulatedBackend();rt=Runtime(tmp_path,driver)
    try:
        plan=atomic_plan(Action('head.move',{'pose':'up'}));plan['cleanup']=[Action('navigation.goto',{'point':'origin'}).to_dict()]
        with pytest.raises(PolicyError):rt.submit('s','1',plan)
        assert not driver.calls
    finally:rt.close()


def test_parallel_duplicate_submission(tmp_path):
    driver=SimulatedBackend();rt=Runtime(tmp_path,driver);ids=[]
    try:
        def submit():ids.append(rt.submit('s','same',atomic_plan(Action('feeder.feed',{'grams':10}))))
        threads=[threading.Thread(target=submit) for _ in range(20)]
        for t in threads:t.start()
        for t in threads:t.join()
        assert len(set(ids))==1
        drive(rt,ids[0]);assert len(driver.calls)==1
    finally:rt.close()


def test_restart_unknown_does_not_repeat_feed(tmp_path):
    driver=SimulatedBackend();rt=Runtime(tmp_path,driver)
    task=rt.submit('s','one',atomic_plan(Action('feeder.feed',{'grams':10})))
    drive(rt,task)
    # Reproduce a crash after device acceptance but before the final response/checkpoint.
    with rt.store.tx() as db:
        db.execute("UPDATE operations SET status='accepted',result='{}',finished=NULL WHERE task=?",(task,))
        db.execute("UPDATE tasks SET status='running' WHERE id=?",(task,))
        db.execute("INSERT OR REPLACE INTO leases VALUES('feeder',?,0)",(task,))
    rt.close();rt=Runtime(tmp_path,driver)
    try:
        rt.tick_all();assert rt.status(task)['status']=='unknown'
        assert len(driver.calls)==1
        assert rt.store.one("SELECT task FROM leases WHERE resource='feeder'")['task']==task
    finally:rt.close()


def test_exclusive_runtime_owner(tmp_path):
    rt=Runtime(tmp_path,SimulatedBackend())
    try:
        with pytest.raises(RuntimeError,match='already_owned'):Runtime(tmp_path,SimulatedBackend())
    finally:rt.close()


def test_missing_hardware_preflight_no_partial_scene(tmp_path):
    class Missing(SimulatedBackend):
        def preflight(self,action):
            if action.kind=='projector.start':raise Unavailable('projector_missing')
    driver=Missing();rt=Runtime(tmp_path,driver)
    try:
        with pytest.raises(Unavailable):rt.submit('s','1',build_plan('meeting_stationary'))
        assert not driver.calls
    finally:rt.close()


def test_model_failure_has_no_actions(tmp_path):
    class Broken:
        def classify(self,text):raise TimeoutError('offline')
    rt=Runtime(tmp_path,SimulatedBackend())
    try:
        result=Dialogue(rt,Broken()).turn('s','1','帮我看看')
        assert not result['task_id']
        assert result['reply'] == '模型调用失败，未执行硬件操作。'
        assert not rt.store.all('SELECT * FROM tasks')
    finally:rt.close()


def test_finished_speech_task_does_not_remain_running(tmp_path):
    rt=Runtime(tmp_path,SimulatedBackend())
    try:
        plan=atomic_plan(Action('system.time'));plan['announce_result']=True
        task=rt.submit('s','announced_time',plan)
        drive(rt,task)
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            rt.tick_all()
            row=rt.store.one("SELECT status FROM tasks WHERE id LIKE 'voice:%'")
            if row and row['status']=='completed':break
            time.sleep(.01)
        assert row and row['status']=='completed'
        assert not rt.store.one("SELECT task FROM leases WHERE resource='speaker'")
    finally:rt.close()


def test_unknown_speech_keeps_speaker_lease(tmp_path):
    rt=Runtime(tmp_path,SimulatedBackend(failures={'speech.say':'unknown'}))
    try:
        plan=atomic_plan(Action('system.time'));plan['announce_result']=True
        task=rt.submit('s','unknown_time',plan);drive(rt,task)
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            rt.tick_all()
            row=rt.store.one("SELECT status FROM tasks WHERE id LIKE 'voice:%'")
            if row and row['status']=='unknown':break
            time.sleep(.01)
        assert row and row['status']=='unknown'
        assert rt.store.one("SELECT task FROM leases WHERE resource='speaker'")
    finally:rt.close()


def test_graph_checkpoint_replay_deduplicates_side_effect(tmp_path):
    rt=Runtime(tmp_path,SimulatedBackend())
    try:
        task=rt.submit('s','1',atomic_plan(Action('feeder.feed',{'grams':10})))
        rt.tick_all();time.sleep(.04)
        # Re-invoke issue with exactly the same operation ID, as after a checkpoint replay.
        state=rt.status(task)['state']
        op=state['operation_id'];action=Action.parse(state['current_action'])
        rt.executor.issue(task,0,op,action)
        assert drive(rt,task)['status']=='completed'
        assert len(rt.executor.backend.calls)==1
    finally:rt.close()


def test_cancelled_speech_not_later_replayed(tmp_path):
    driver=SimulatedBackend(delays={'speech.say':.3});rt=Runtime(tmp_path,driver)
    try:
        task=rt.submit('s','1',build_plan('meeting_stationary'))
        drive(rt,task,('active',));rt.control('s',task,'cancel');drive(rt,task)
        for _ in range(10):rt.tick_all();time.sleep(.01)
        assert not rt.store.all("SELECT * FROM outbox WHERE task=? AND status='pending'",(task,))
    finally:rt.close()


def test_pause_waiting_task_does_not_wait_for_other_owner(tmp_path):
    rt=Runtime(tmp_path,SimulatedBackend())
    try:
        a=rt.submit('s','a',build_plan('meeting_stationary'));drive(rt,a,('active',))
        b=rt.submit('s','b',atomic_plan(Action('head.move',{'pose':'level'})))
        rt.control('s',b,'pause');assert drive(rt,b,('paused',))['status']=='paused'
        rt.control('s',b,'cancel');assert drive(rt,b)['status']=='cancelled'
        rt.control('s',a,'cancel');drive(rt,a)
    finally:rt.close()


def test_deadline_does_not_unlock_running_device(tmp_path):
    driver=SimulatedBackend(delays={'head.move':.3});rt=Runtime(tmp_path,driver)
    try:
        a=rt.submit('s','a',atomic_plan(Action('head.move',{'pose':'up'},.04)))
        assert drive(rt,a)['status']=='unknown'
        b=rt.submit('s','b',atomic_plan(Action('head.move',{'pose':'level'})))
        rt.tick_all();assert rt.status(b)['status']=='waiting_resources'
        time.sleep(.35);rt.tick_all()
        assert rt.status(a)['status']=='unknown' and len(driver.calls)==1
    finally:rt.close()


def test_shutdown_runs_session_cleanup(tmp_path):
    driver=SimulatedBackend();rt=Runtime(tmp_path,driver)
    a=rt.submit('s','a',build_plan('meeting_stationary'));drive(rt,a,('active',))
    rt.close(cancel_tasks=True)
    calls=[x[0] for x in driver.calls]
    assert 'projector.off' in calls
    assert [x[1] for x in driver.calls if x[0]=='head.move'][-1]=={'pose':'level'}


def test_hardware_owner_shared_across_databases(tmp_path):
    from robot_graph.hardware import HardwareBackend
    (tmp_path/'runtime').mkdir()
    rt=Runtime(tmp_path/'a',HardwareBackend(tmp_path))
    try:
        with pytest.raises(RuntimeError,match='hardware_already_owned'):
            Runtime(tmp_path/'b',HardwareBackend(tmp_path))
    finally:rt.close()


def test_latched_head_fault_blocks_motion_but_allows_recovery():
    from robot_graph.hardware import HardwareBackend
    root=Path(tempfile.mkdtemp(prefix='qrf-',dir='/tmp'))
    runtime=root/'runtime';runtime.mkdir()
    sock=socket.socket(socket.AF_UNIX)
    sock.bind(str(runtime/'ros.sock'))
    (runtime/'head_wire_audit.json').write_text(json.dumps({
        'pid':__import__('os').getpid(), 'updated_at':time.time(),
        'wheel_packets_sent':0,
    }))
    (runtime/'head_motion_fault.json').write_text(json.dumps({
        'fault':'head_target_unconfirmed',
    }))
    backend=HardwareBackend(root)
    try:
        with pytest.raises(Unavailable,match='head_motion_fault_latched'):
            backend.preflight(Action('head.move',{'pose':'up'}))
        backend.preflight(Action('head.move',{'pose':'level'}))
        backend.preflight(Action('sensor.gate',{'enabled':True}))
    finally:
        sock.close()
        shutil.rmtree(root)


def test_restart_active_session_is_unknown_not_assumed_paused(tmp_path):
    driver=SimulatedBackend();rt=Runtime(tmp_path,driver)
    task=rt.submit('s','1',build_plan('meeting_stationary'));drive(rt,task,('active',));rt.close()
    rt=Runtime(tmp_path,driver)
    try:
        assert rt.status(task)['status']=='unknown'
        before=len(driver.calls);rt.tick_all();assert len(driver.calls)==before
        assert not rt.control('s',task,'resume')['accepted']
    finally:rt.close()


def test_cancel_before_hardware_dispatch_does_not_send_command(tmp_path):
    driver=SimulatedBackend();rt=Runtime(tmp_path,driver)
    try:
        task=rt.submit('s','1',atomic_plan(Action('feeder.feed',{'grams':10})))
        rt.store.claim(task,0,['feeder']);rt._status(task,'running')
        rt.control('s',task,'cancel')
        with pytest.raises(PolicyError,match='dispatch_cancelled'):
            rt.executor.issue(task,0,'id',Action('feeder.feed',{'grams':10}))
        assert not driver.calls
    finally:rt.close()
