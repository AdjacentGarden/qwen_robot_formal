#!/usr/bin/env python3
"""Six bounded intent calls; physical actions are never executed by this test."""
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from robot_graph.realtime_model import RealtimeJsonModel
from robot_graph.cloud_budget import CloudBudget
from robot_graph.intent_policy import validate_current_turn

config=json.loads((ROOT/'config/private_robot.json').read_text());model=RealtimeJsonModel(config)
cases=[('请把头抬起来','action','head.move'),('暂时不要开灯','chat',None),('在原地开始会议投影，不要导航','workflow','meeting_stationary'),('把头调到一百九十度','chat',None),('如果我要开会，你会怎么做','chat',None),('打开灯然后抬头','chat',None)]
rows=[]
for text,kind,name in cases:
    start=time.monotonic()
    try:
        result=model.classify(text);validate_current_turn(text,result)
        passed=result.get('type')==kind and (name is None or result.get('kind',result.get('name'))==name)
        row={'text':text,'passed':passed,'result':result,'seconds':time.monotonic()-start}
    except Exception as exc:row={'text':text,'passed':False,'error':str(exc),'seconds':time.monotonic()-start}
    rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
    (ROOT/'runtime/cloud_contract_acceptance.json').write_text(json.dumps({'passed':sum(r['passed'] for r in rows),'total':len(rows),'rows':rows,'hardware_executed':False,'budget':CloudBudget(config['cloud_budget_path']).status()},ensure_ascii=False,indent=2))
