#!/usr/bin/env python3
"""Live local-model evaluation plus simulated production graph. Never calls cloud."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from local_stack_benchmark import ReplayModel,evaluate_graph,match
from robot_graph.dialogue import JsonModel,ConservativeRouter,INTENT_INSTRUCTIONS
from robot_graph.hybrid import HybridIntentModel
from robot_graph.execution import SimulatedBackend
from robot_graph.runtime import Runtime
from robot_graph.cloud_budget import CloudBudget


class NoCloud:
    def classify(self,text):raise RuntimeError('cloud_disabled_for_regression')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--replay');args=parser.parse_args()
    out=ROOT/'runtime'/args.output;out.mkdir(exist_ok=False)
    cases=[]
    for name in ['local_stack_240.json','stationary_extended_20260908.json']:
        for case in json.loads((ROOT/'benchmarks'/name).read_text())['cases']:
            cases.append({**case,'suite':name,'category':case.get('category','extended')})
    budget=CloudBudget(ROOT/'runtime/cloud_budget.sqlite');before=budget.status()
    prior = {}
    if args.replay:
        prior_path = ROOT / 'runtime' / args.replay
        prior_report = json.loads((prior_path/'report.json').read_text())
        if prior_report['prompt_sha256'] != hashlib.sha256(INTENT_INSTRUCTIONS.encode()).hexdigest():
            raise ValueError('cannot_replay_outputs_from_a_different_prompt')
        prior = {x['id']: x['model'] for x in (json.loads(line) for line in (prior_path/'results.jsonl').read_text().splitlines())}
    model=JsonModel('http://127.0.0.1:18087/v1','Qwen3-4B')
    backend=SimulatedBackend();rt=Runtime(out/'simulation',backend);rows=[]
    try:
        for index,case in enumerate(cases):
            start=time.monotonic();captured={}
            if prior:
                captured = prior[case['id']]
            else:
                try:captured['intent']=model.classify(case['text'])
                except Exception as exc:captured['error']=f'{type(exc).__name__}:{exc}'
                captured.update(getattr(model,'last_response_info',{}));captured['seconds']=time.monotonic()-start
            hybrid=HybridIntentModel(ReplayModel(captured),NoCloud())
            value=ConservativeRouter(hybrid,local_matching=False).classify(case['text'])
            row={**case,'model':captured,'model_match':match(captured.get('intent'),case['expected']),'route':hybrid.last_route or {'route':'exact_rule'},'evaluation':evaluate_graph(rt,backend,case,case['text'],{'intent':value})}
            rows.append(row)
            with (out/'results.jsonl').open('a') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
            if (index+1)%20==0:print(json.dumps({'done':index+1,'model_passed':sum(x['model_match'] for x in rows),'pipeline_passed':sum(x['evaluation']['strict_pipeline_pass'] for x in rows)}),flush=True)
    finally:rt.close(cancel_tasks=True)
    summaries={}
    for suite in sorted({x['suite'] for x in rows}):
        items=[x for x in rows if x['suite']==suite]
        summaries[suite]={'total':len(items),'model_passed':sum(x['model_match'] for x in items),'pipeline_passed':sum(x['evaluation']['strict_pipeline_pass'] for x in items),'parse_errors':sum('error' in x['model'] for x in items),'routes':dict(Counter(x['route']['route'] for x in items)),'unexpected_dispatch':[x['id'] for x in items if x['expected']['type']=='chat' and x['evaluation']['device_calls']]}
    report={'summary':summaries,'cloud_before':before,'cloud_after':budget.status(),'prompt_sha256':hashlib.sha256(INTENT_INSTRUCTIONS.encode()).hexdigest(),'hardware':'simulation_only','replayed_from':args.replay}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False),flush=True)
