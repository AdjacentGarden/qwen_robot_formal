#!/usr/bin/env python3
"""48 preselected physical microphone trials; wheels blocked, no Mijia calls.

Fixture speech is synthesized locally and played through the real loudspeaker.
This measures loopback acoustics, not recognition of 48 independent human speakers.
"""
import base64
import json
import math
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from local_stack_benchmark import CaptureModel,match,norm,drive,edit_distance
from robot_graph.hardware import HardwareBackend
from robot_graph.execution import Unavailable
from robot_graph.runtime import Runtime
from robot_graph.dialogue import Dialogue,ConservativeRouter
from robot_graph.hybrid import HybridIntentModel
from robot_graph.realtime_model import RealtimeJsonModel
from robot_graph.local_audio import request
from robot_graph.cloud_budget import CloudBudget

class TestBackend(HardwareBackend):
    def preflight(self,action):
        if action.kind in {'light.set','feeder.feed','feeder.status','exercise.count','pet.observe'}:raise Unavailable('not_in_real_acceptance_scope')
        super().preflight(action)


def wait_quiet(rt,timeout=25):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        rt.tick_all()
        if not rt.store.one("SELECT id FROM operations WHERE kind='speech.say' AND status='accepted'") and not rt.store.one("SELECT id FROM outbox WHERE status IN ('pending','playing')"):return
        time.sleep(.03)
    raise RuntimeError('speech_did_not_finish')


def microphone(root,out,text):
    speech=request(root,{'op':'tts','text':text});pcm=base64.b64decode(speech['pcm']);clip=out/'fixture.pcm';clip.write_bytes(pcm)
    path=out/'capture.pcm'
    with path.open('wb') as stream:
        record=subprocess.Popen(['parec','--raw','--format=s16le','--rate=16000','--channels=1'],stdout=stream)
        try:
            time.sleep(.25)
            subprocess.run(['paplay','--raw','--format=s16le','--channels=1','--rate='+str(speech['rate']),str(clip)],check=True,timeout=15)
            time.sleep(.4)
        finally:record.terminate();record.wait(timeout=3)
    audio=path.read_bytes();audio=audio[:len(audio)//2*2]
    if len(audio)>640000:raise RuntimeError('recording_exceeds_asr_limit')
    result=request(root,{'op':'asr','pcm':base64.b64encode(audio).decode()})
    return {'text':result['text'],'asr_seconds':result['seconds'],'tts_seconds':speech['synthesis_seconds'],'recorded_seconds':len(audio)/32000,'exact':norm(text)==norm(result['text']),'edit_distance':edit_distance(norm(text),norm(result['text']))}


if __name__=='__main__':
    dataset={x['id']:x for x in json.loads((ROOT/'benchmarks/local_stack_240.json').read_text())['cases']}
    ids=[3,27,8,29,10,35]+list(range(37,43))+list(range(53,59))+list(range(69,77))+list(range(85,93))+list(range(189,195))+list(range(153,157))+[143,179,183,187]
    assert len(ids)==48 and len(set(ids))==48
    out=ROOT/'runtime/hardware_voice_48_v1';out.mkdir(exist_ok=False)
    (out/'case_ids.json').write_text(json.dumps(ids))
    backend=TestBackend(ROOT);backend.config['speech_backend']='local'
    budget=CloudBudget(ROOT/'runtime/cloud_budget.sqlite');before=budget.status();local=CaptureModel()
    hybrid=HybridIntentModel(local,RealtimeJsonModel(backend.config));rt=Runtime(out/'state',backend);dialogue=Dialogue(rt,ConservativeRouter(hybrid));rows=[]
    try:
        for number in ids:
            case=dataset[f'c{number:03}'];row={**case,'started_at':time.time()}
            try:
                wait_quiet(rt);row['audio']=microphone(ROOT,out,case['text'])
                oldop=rt.store.one('SELECT MAX(started) AS last FROM operations')['last'] or 0
                hybrid.last_route={};response=dialogue.turn('real_voice',case['id'],row['audio']['text']);row['dialogue']=response;row['route']=hybrid.last_route or {'route':'exact_rule'}
                row['intent_match']=match(response.get('intent'),case['expected']);task=response.get('task_id');status=None
                expected=case['expected']
                if task:
                    targets={'completed','active'}
                    if expected['type']=='control':targets={{'pause':'paused','resume':'active','cancel':'cancelled'}[expected['command']]}
                    status=drive(rt,task,targets,timeout=100)
                wait_quiet(rt);row['status']=status
                ops=rt.store.all("SELECT kind,args,status,result,started,finished FROM operations WHERE started>? AND kind!='speech.say'",(oldop,))
                for op in ops:op['args']=json.loads(op['args']);op['result']=json.loads(op['result'])
                row['operations']=ops
                if expected['type']=='chat':goal=not task and not ops and not response.get('error')
                elif expected['type']=='workflow' and expected['name']=='meeting':goal=not task and 'base_motion_forbidden' in response.get('error','') and not ops
                elif expected['type']=='workflow':goal=status=='active' and all(any(op['kind']==kind and op['status']=='completed' for op in ops) for kind in ['sensor.gate','head.move','projector.start'])
                elif expected['type']=='control':goal=status=={'pause':'paused','resume':'active','cancel':'cancelled'}[expected['command']]
                else:goal=status=='completed' and len(ops)==1 and ops[0]['kind']==expected['kind'] and ops[0]['args']==expected['args'] and ops[0]['status']=='completed'
                row['execution_goal_match']=bool(goal);row['strict_end_to_end_pass']=bool(goal and row['intent_match'])
                if status in {'unknown','test_deadline'}:raise RuntimeError('unconfirmed_device_outcome_stop_real_suite')
            except Exception as exc:row['error']=f'{type(exc).__name__}:{exc}';row['strict_end_to_end_pass']=False
            rows.append(row)
            with (out/'results.jsonl').open('a') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
            print(json.dumps({'done':len(rows),'id':case['id'],'text':case['text'],'recognized':row.get('audio',{}).get('text'),'passed':row['strict_end_to_end_pass'],'route':row.get('route'),'status':row.get('status'),'error':row.get('error')},ensure_ascii=False),flush=True)
            if 'unconfirmed_device' in row.get('error',''):break
    finally:rt.close(cancel_tasks=True)
    report={'total_planned':48,'total_run':len(rows),'asr_exact':sum(x.get('audio',{}).get('exact',False) for x in rows),'intent_passed':sum(x.get('intent_match',False) for x in rows),'execution_goal_passed':sum(x.get('execution_goal_match',False) for x in rows),'strict_end_to_end_passed':sum(x['strict_end_to_end_pass'] for x in rows),'cloud_before':before,'cloud_after':budget.status(),'wire_audit':json.loads((ROOT/'runtime/head_wire_audit.json').read_text()),'notes':['Real speaker -> real microphone -> local SenseVoice -> local Qwen with bounded cloud fallback -> real graph execution.','Scripted loopback speech, not human speech accuracy.','No base commands, no Mijia requests, no feeding. Navigation requests are expected to be rejected.']}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False),flush=True)
