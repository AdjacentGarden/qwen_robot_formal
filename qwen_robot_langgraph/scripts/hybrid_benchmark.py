#!/usr/bin/env python3
"""Replay fixed local baseline; call cloud only for validated fallback, max 30 attempts."""
from collections import Counter
import json
from pathlib import Path
import random
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from local_stack_benchmark import ReplayModel,evaluate_graph,match
from robot_graph.dialogue import ConservativeRouter
from robot_graph.hybrid import HybridIntentModel
from robot_graph.realtime_model import RealtimeJsonModel
from robot_graph.hardware import HardwareBackend
from robot_graph.execution import SimulatedBackend
from robot_graph.runtime import Runtime
from robot_graph.cloud_budget import CloudBudget

class LimitedCloud:
    def __init__(self,config,limit=30):self.inner=RealtimeJsonModel(config);self.calls=0;self.limit=limit;self.last={}
    def classify(self,text):
        if self.calls>=self.limit:raise RuntimeError('evaluation_cloud_attempt_cap_30')
        self.calls+=1;start=time.monotonic();self.last={'text':text}
        try:
            result=self.inner.classify(text);self.last['intent']=result;return result
        except Exception as exc:self.last['error']=f'{type(exc).__name__}:{exc}';raise
        finally:self.last['seconds']=time.monotonic()-start

if __name__=='__main__':
    baseline=ROOT/'runtime/local_stack_240_v1/results.jsonl'
    cases=[json.loads(line) for line in baseline.read_text().splitlines() if json.loads(line)['lane']=='text']
    assert len(cases)==240
    random.Random(20260907).shuffle(cases)
    out=ROOT/'runtime/hybrid_240_v1';out.mkdir(exist_ok=False)
    cloud=LimitedCloud(HardwareBackend(ROOT).config);budget=CloudBudget(ROOT/'runtime/cloud_budget.sqlite');before=budget.status()
    backend=SimulatedBackend();rt=Runtime(out/'simulation',backend);rows=[]
    try:
        for index,case in enumerate(cases):
            hybrid=HybridIntentModel(ReplayModel(case['model']),cloud);calls=cloud.calls
            value=ConservativeRouter(hybrid,local_matching=False).classify(case['text'])
            row={'id':case['id'],'category':case['category'],'text':case['text'],'expected':case['expected'],'intent':value,'route':hybrid.last_route or {'route':'exact_rule','fallback':False},'intent_match':match(value,case['expected'])}
            if cloud.calls>calls:row['cloud_response']=dict(cloud.last)
            row['evaluation']=evaluate_graph(rt,backend,case,case['text'],{'intent':value})
            rows.append(row)
            with (out/'results.jsonl').open('a') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
            if (index+1)%20==0:print(json.dumps({'done':index+1,'cloud_attempts':cloud.calls,'pipeline_passed':sum(x['evaluation']['strict_pipeline_pass'] for x in rows)}),flush=True)
    finally:rt.close(cancel_tasks=True)
    categories={}
    for category in sorted({x['category'] for x in rows}):
        items=[x for x in rows if x['category']==category]
        categories[category]={'total':len(items),'intent_passed':sum(x['intent_match'] for x in items),'pipeline_passed':sum(x['evaluation']['strict_pipeline_pass'] for x in items)}
    summary={'total':len(rows),'intent_passed':sum(x['intent_match'] for x in rows),'strict_pipeline_passed':sum(x['evaluation']['strict_pipeline_pass'] for x in rows),'routes':dict(Counter(x['route']['route'] for x in rows)),'cloud_attempts':cloud.calls,'test_cap_declines':sum('evaluation_cloud_attempt_cap' in x['route'].get('cloud_issue','') for x in rows),'cloud_before':before,'cloud_after':budget.status(),'categories':categories,'notes':['Local results replayed without new local inference; exact same fixed prompts and cases.','Cloud attempt cap 30, fixed random order seed 20260907. Cases over cap return explicit clarification.','All device execution in this report is simulated; no real hardware.']}
    (out/'report.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False),flush=True)
