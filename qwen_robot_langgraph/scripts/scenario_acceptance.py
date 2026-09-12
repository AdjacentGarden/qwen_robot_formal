#!/usr/bin/env python3
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from robot_graph.runtime import Runtime
from robot_graph.hardware import HardwareBackend
from robot_graph.workflows import build_plan

report={'started_at':time.time(),'base_locked':True,'cases':[]}
statepath=ROOT/'runtime'/('scenario_acceptance_'+str(time.time_ns()))
rt=Runtime(statepath,HardwareBackend(ROOT))

def wait(task,states,seconds=100):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        rt.tick_all();result=rt.status(task)
        if result['status'] in states:return result
        time.sleep(.02)
    raise TimeoutError(str(rt.status(task)))

def save():
    (ROOT/'runtime/scenario_acceptance_report.json').write_text(json.dumps({**report,'state_directory':str(statepath)},ensure_ascii=False,indent=2))

try:
    task=rt.submit('acceptance','meeting_finite',build_plan('meeting_stationary',{'hold_seconds':3}))
    result=wait(task,{'completed','failed','unknown'})
    report['cases'].append({'name':'meeting_finite','task_id':task,'status':result['status'],'state':result['state']});save();print(json.dumps({'name':'meeting_finite','status':result['status']},ensure_ascii=False),flush=True)
    if result['status']=='completed':
        task=rt.submit('acceptance','pause_resume',build_plan('meeting_stationary'))
        started=wait(task,{'active','failed','unknown'})
        if started['status']=='active':
            rt.control('acceptance',task,'pause');paused=wait(task,{'paused','failed','unknown'})
            if paused['status']=='paused':
                rt.control('acceptance',task,'resume');resumed=wait(task,{'active','failed','unknown'})
                rt.control('acceptance',task,'cancel');ended=wait(task,{'cancelled','failed','unknown'})
                report['cases'].append({'name':'meeting_pause_resume_cancel','task_id':task,'status':'passed' if resumed['status']=='active' and ended['status']=='cancelled' else 'failed','paused':paused['status'],'resumed':resumed['status'],'ended':ended['status'],'state':ended['state']})
            else:report['cases'].append({'name':'meeting_pause','status':paused['status'],'state':paused['state']})
        else:report['cases'].append({'name':'meeting_start','status':started['status'],'state':started['state']})
finally:
    rt.close(cancel_tasks=True);report['finished_at']=time.time();save()
    print(json.dumps({'cases':[{'name':c['name'],'status':c['status']} for c in report['cases']]},ensure_ascii=False),flush=True)
