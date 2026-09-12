#!/usr/bin/env python3
"""Interface/intent smoke suite; never executes the proposed hardware actions."""
from pathlib import Path
import json
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from robot_graph.dialogue import ConservativeRouter,JsonModel
from robot_graph.contracts import StationaryPolicy,Action
from robot_graph.workflows import build_plan

CASES=[
 ('请把头抬起来',{'type':'action','kind':'head.move','args':{'pose':'up'}}),
 ('麻烦让头部回正',{'type':'action','kind':'head.move','args':{'pose':'level'}}),
 ('用前面的摄像头拍一张照片',{'type':'action','kind':'camera.capture','args':{'camera':'front'}}),
 ('用后面的摄像头拍一张',{'type':'action','kind':'camera.capture','args':{'camera':'back'}}),
 ('请把灯关掉',{'type':'action','kind':'light.set','args':{'enabled':False}}),
 ('帮我打开灯',{'type':'action','kind':'light.set','args':{'enabled':True}}),
 ('在原地开始会议投影，不要导航',{'type':'workflow','name':'meeting_stationary'}),
 ('原地陪我做五个深蹲',{'type':'workflow','name':'exercise_stationary','parameters':{'exercise':'squat','count':5}}),
 ('给投食机发十克投粮指令',{'type':'action','kind':'feeder.feed','args':{'grams':10}}),
 ('暂时不要开灯',{'type':'chat'}),
 ('如果我要开会，你会怎么做',{'type':'chat'}),
 ('你刚才抬头了吗',{'type':'chat'}),
 ('打开灯再抬头',{'type':'chat'}),
 ('把头调到一百九十度',{'type':'chat'}),
 ('你可以直接在原地投影吗',{'type':'chat'}),
 ('你好',{'type':'chat'}),
 ('现在几点',{'type':'action','kind':'system.time','args':{}}),
 ('启动会议投影',{'type':'workflow','name':'meeting'}),
]
router=ConservativeRouter(JsonModel('http://127.0.0.1:18087/v1','Qwen3-4B'))
rows=[]
for text,expected in CASES:
 start=time.monotonic()
 try:
  result=router.classify(text)
  passed=all(result.get(k)==v for k,v in expected.items())
  row={'text':text,'expected':expected,'result':result,'passed':passed,'seconds':time.monotonic()-start}
 except Exception as exc:row={'text':text,'passed':False,'error':str(exc),'seconds':time.monotonic()-start}
 rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
 (ROOT/'runtime/model_contract_acceptance.json').write_text(json.dumps({'model':'Qwen3-4B','purpose':'18-case interface smoke test, not a general accuracy estimate','hardware_executed':False,'passed':sum(x['passed'] for x in rows),'total':len(rows),'rows':rows},ensure_ascii=False,indent=2))
