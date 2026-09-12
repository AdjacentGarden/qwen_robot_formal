#!/usr/bin/env python3
"""Fixed 240-case offline evaluation. Simulated device results are never real acceptance."""
import argparse
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys
import time
import httpx
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from robot_graph.dialogue import INTENT_INSTRUCTIONS,ConservativeRouter,Dialogue
from robot_graph.runtime import Runtime
from robot_graph.execution import SimulatedBackend
from robot_graph.workflows import build_plan
from robot_graph.local_audio import request
from robot_graph.cloud_budget import CloudBudget

TERMINAL={'completed','failed','cancelled','blocked','unknown'}

def norm(text):return re.sub(r'[^\w\u4e00-\u9fff]','',text).lower()
def edit_distance(a,b):
    prior=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        current=[i]
        for j,y in enumerate(b,1):current.append(min(current[-1]+1,prior[j]+1,prior[j-1]+(x!=y)))
        prior=current
    return prior[-1]

def match(value,expected):
    if not isinstance(value,dict):return False
    if expected['type']=='chat':return value.get('type')=='chat' and isinstance(value.get('reply'),str) and bool(value['reply'].strip())
    return all(value.get(k)==v for k,v in expected.items())

class CaptureModel:
    def __init__(self):self.client=httpx.Client(timeout=30);self.last={}
    def classify(self,text):
        started=time.monotonic();self.last={'text':text}
        try:
            response=self.client.post('http://127.0.0.1:18087/v1/chat/completions',json={'model':'Qwen3-4B','messages':[{'role':'system','content':INTENT_INSTRUCTIONS},{'role':'user','content':text}],'temperature':0,'max_tokens':384,'chat_template_kwargs':{'enable_thinking':False}})
            response.raise_for_status();body=response.json();raw=body['choices'][0]['message']['content']
            self.last.update(raw=raw,usage=body.get('usage'),finish_reason=body['choices'][0].get('finish_reason'))
            if raw.startswith('```json') and raw.endswith('```'):raw=raw[7:-3].strip()
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError('invalid_model_intent')
            self.last['intent']=value;return value
        except Exception as exc:self.last['error']=f'{type(exc).__name__}:{exc}';raise
        finally:self.last['seconds']=time.monotonic()-started

class ReplayModel:
    def __init__(self,result):self.result=result
    def classify(self,text):
        if 'intent' not in self.result:raise ValueError(self.result.get('error','model_failed'))
        return self.result['intent']


def drive(rt,task,targets,timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        rt.tick_all();s=rt.status(task)['status']
        if s in targets or s in TERMINAL:return s
        time.sleep(.005)
    return 'test_deadline'


def evaluate_graph(rt,backend,case,text,captured):
    session=case['id'];expected=case['expected'];fixture=None
    if expected['type']=='control':
        fixture=rt.submit(session,'control_fixture',build_plan('meeting_stationary'))
        drive(rt,fixture,{'active'})
        if expected['command']=='resume':rt.control(session,fixture,'pause');drive(rt,fixture,{'paused'})
    before=len(backend.calls)
    # Production runs with every pre-model shortcut disabled.  Replaying through
    # the same mode prevents legacy regular expressions from skewing results.
    result=Dialogue(rt,ConservativeRouter(ReplayModel(captured),local_matching=False)).turn(session,'request',text)
    task=result.get('task_id');state=None;goal=False
    if task:
        targets={'active','paused','completed','cancelled'}
        if expected['type']=='control':targets={{'pause':'paused','resume':'active','cancel':'cancelled'}[expected['command']]}
        state=drive(rt,task,targets)
    calls=[{'kind':k,'args':a} for k,a,_ in backend.calls[before:] if k!='speech.say']
    if expected['type']=='chat':goal=not task and not calls and not result.get('error')
    elif expected['type']=='workflow' and expected['name']=='meeting':goal=not task and 'base_motion_forbidden' in result.get('error','') and not calls
    elif expected['type']=='control':goal=bool(task==fixture and state=={'pause':'paused','resume':'active','cancel':'cancelled'}[expected['command']])
    elif expected['type']=='action':goal=state=='completed' and calls==[{'kind':expected['kind'],'args':expected['args']}]
    elif expected['type']=='workflow':
        plan=build_plan(expected['name'],expected.get('parameters'))
        expected_calls=[{'kind':x['kind'],'args':x['args']} for x in plan['steps']]
        goal=state==('active' if expected['name']=='meeting_stationary' else 'completed') and calls[:len(expected_calls)]==expected_calls
    for t in {x for x in [task,fixture] if x}:
        if rt.status(t)['status'] not in TERMINAL:rt.control(session,t,'cancel');drive(rt,t,TERMINAL)
    production_match=match(result.get('intent'),expected)
    return {'production_intent_match':production_match,'graph_goal_match':goal,'strict_pipeline_pass':bool(goal and production_match),'safe_no_device_dispatch':not calls,'dialogue_error':result.get('error',''),'reply':result.get('reply'),'state':state,'device_calls':calls,'intent':result.get('intent')}


def summarize(rows):
    summary={}
    for lane in ('text','synthetic_voice'):
        selected=[x for x in rows if x['lane']==lane];n=len(selected)
        categories={}
        for category in sorted({x['category'] for x in selected}):
            items=[x for x in selected if x['category']==category]
            categories[category]={'total':len(items),'model_passed':sum(x['model_match'] for x in items),'pipeline_passed':sum(x.get('evaluation',{}).get('strict_pipeline_pass',False) for x in items)}
        summary[lane]={'total':n,'model_passed':sum(x['model_match'] for x in selected),'production_intent_passed':sum(x.get('evaluation',{}).get('production_intent_match',False) for x in selected),'strict_pipeline_passed':sum(x.get('evaluation',{}).get('strict_pipeline_pass',False) for x in selected),'parse_or_call_errors':sum('error' in x['model'] for x in selected),'categories':categories}
        times=[x['model']['seconds'] for x in selected]
        if times:summary[lane]['mean_model_seconds']=statistics.mean(times)
        if lane=='synthetic_voice' and selected:
            summary[lane]['asr_exact']=sum(x.get('asr_exact',False) for x in selected)
            summary[lane]['character_error_rate']=sum(x.get('edit_distance',0) for x in selected)/sum(len(norm(x['text'])) for x in selected)
    return summary

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--lane',choices=['text','synthetic_voice','both'],default='both');parser.add_argument('--output',default='local_stack_240');args=parser.parse_args()
    dataset=ROOT/'benchmarks/local_stack_240.json';cases=json.loads(dataset.read_text())['cases']
    out=ROOT/'runtime'/args.output;out.mkdir(exist_ok=False)
    budget=CloudBudget(ROOT/'runtime/cloud_budget.sqlite');before=budget.status()
    metadata={'dataset_sha256':hashlib.sha256(dataset.read_bytes()).hexdigest(),'prompt_sha256':hashlib.sha256(INTENT_INSTRUCTIONS.encode()).hexdigest(),'cloud_before':before,'hardware_execution':'simulated_only','started_at':time.time(),'unique_cases':len(cases)}
    (out/'metadata.json').write_text(json.dumps(metadata,indent=2));rows=[];model=CaptureModel()
    for lane in (['text','synthetic_voice'] if args.lane=='both' else [args.lane]):
        backend=SimulatedBackend();rt=Runtime(out/lane,backend)
        try:
            for index,case in enumerate(cases):
                row={**case,'lane':lane};input_text=case['text']
                if lane=='synthetic_voice':
                    try:
                        tts=request(ROOT,{'op':'tts','text':input_text})
                        if tts['rate']!=16000:raise ValueError('unexpected_tts_rate')
                        asr=request(ROOT,{'op':'asr','pcm':tts['pcm']})
                        row.update(asr_text=asr['text'],asr_seconds=asr['seconds'],tts_seconds=tts['synthesis_seconds'],audio_seconds=len(base64.b64decode(tts['pcm']))/32000)
                        input_text=asr['text'];row['asr_exact']=norm(input_text)==norm(case['text']);row['edit_distance']=edit_distance(norm(case['text']),norm(input_text))
                    except Exception as exc:row['audio_error']=str(exc);input_text=''
                try:model.classify(input_text)
                except Exception:pass
                row['model']=dict(model.last);row['model_match']=match(row['model'].get('intent'),case['expected'])
                try:row['evaluation']=evaluate_graph(rt,backend,case,input_text,row['model'])
                except Exception as exc:row['evaluation_error']=f'{type(exc).__name__}:{exc}'
                rows.append(row)
                with (out/'results.jsonl').open('a') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
                if (index+1)%20==0:print(json.dumps({'lane':lane,'done':index+1,'total':len(cases),'model_passed':sum(r['model_match'] for r in rows if r['lane']==lane)},ensure_ascii=False),flush=True)
        finally:rt.close(cancel_tasks=True)
    metadata.update(finished_at=time.time(),cloud_after=budget.status(),summary=summarize(rows))
    (out/'report.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2));print(json.dumps(metadata,ensure_ascii=False),flush=True)
