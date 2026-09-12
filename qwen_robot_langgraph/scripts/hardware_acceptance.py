#!/usr/bin/env python3
"""Runs reviewed stationary tests through the real graph, records every result."""
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from robot_graph.contracts import Action, PolicyError
from robot_graph.hardware import HardwareBackend
from robot_graph.runtime import Runtime
from robot_graph.workflows import atomic_plan, build_plan

report={'started_at':time.time(),'base_locked':True,'cases':[]}
rt=Runtime(ROOT/'runtime'/('hardware_acceptance_'+str(time.time_ns())),HardwareBackend(ROOT))


def wait(task,states={'completed','failed','unknown','cancelled'},timeout=75):
    start=time.monotonic()
    while time.monotonic()-start<timeout:
        rt.tick_all();s=rt.status(task)
        if s['status'] in states:return s
        time.sleep(.02)
    raise TimeoutError('acceptance_wait_timeout:'+task)


def save():
    (ROOT/'runtime/hardware_acceptance_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))


def run(name,plan):
    start=time.monotonic()
    try:
        task=rt.submit('acceptance',name,plan);s=wait(task)
        row={'name':name,'task_id':task,'status':s['status'],'elapsed_ms':(time.monotonic()-start)*1000,'state':s['state']}
    except Exception as exc:
        row={'name':name,'status':'not_started','error':str(exc)}
    report['cases'].append(row);save();print(json.dumps({k:v for k,v in row.items() if k!='state'},ensure_ascii=False),flush=True)
    return row


try:
    for name,action in [('system',Action('system.status')),('speaker',Action('speaker.test')),('camera_front',Action('camera.capture',{'camera':'front'})),('camera_back',Action('camera.capture',{'camera':'back'})),('feeder_status',Action('feeder.status')),('local_speech',Action('speech.say',{'text':'正在当前位置准备投影。'}))]:
        run(name,atomic_plan(action))
    try:
        rt.submit('acceptance','wheel_block',build_plan('meeting'))
        report['cases'].append({'name':'wheel_block','status':'FAILED_POLICY'})
    except PolicyError as exc:
        report['cases'].append({'name':'wheel_block','status':'passed','error':str(exc)})
    up=run('head_up',atomic_plan(Action('head.move',{'pose':'up'})))
    level=run('head_level',atomic_plan(Action('head.move',{'pose':'level'})))
    if up['status']=='completed' and level['status']=='completed':
        run('meeting_stationary',build_plan('meeting_stationary',{'hold_seconds':3}))
        task=rt.submit('acceptance','pause_resume',build_plan('meeting_stationary'))
        s=wait(task,{'active','failed','unknown'})
        if s['status']=='active':
            rt.control('acceptance',task,'pause');paused=wait(task,{'paused','failed','unknown'})
            if paused['status']=='paused':
                rt.control('acceptance',task,'resume');resumed=wait(task,{'active','failed','unknown'})
                rt.control('acceptance',task,'cancel');ended=wait(task)
                report['cases'].append({'name':'meeting_pause_resume_cancel','status':'passed' if resumed['status']=='active' and ended['status']=='cancelled' else 'failed','task_id':task,'paused':paused['status'],'resumed':resumed['status'],'ended':ended['status']})
        else:report['cases'].append({'name':'meeting_pause_resume_cancel','status':s['status'],'task_id':task})
finally:
    report['finished_at']=time.time();save();rt.close()
