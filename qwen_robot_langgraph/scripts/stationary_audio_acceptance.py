#!/usr/bin/env python3
"""Narrow real-audio regression: time/status only, with no head cleanup.

Fixtures use local TTS and the current system volume. Results include raw ASR,
HTTP error bodies and resource readiness; this is a development regression.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import httpx
from robot_graph.local_audio import request
from stationary_api_support import TERMINAL, error_record, response_json, wait_quiet


def run(label,repetitions=3):
    out=ROOT/'runtime'/label
    out.mkdir(exist_ok=False)
    session='audio_regression_'+uuid.uuid4().hex
    client=httpx.Client(base_url='http://127.0.0.1:18884',timeout=45)
    rows=[]

    def snapshot():
        with sqlite3.connect(f'file:{ROOT}/runtime/service/ledger.sqlite?mode=ro',uri=True) as db:
            operations=dict(db.execute('SELECT kind,count(*) FROM operations GROUP BY kind'))
        return {'health':response_json(client.get('/health')),
                'audio':response_json(client.get('/audio-status')),
                'operations':operations,
                'wire_audit':json.loads((ROOT/'runtime/head_wire_audit.json').read_text()),
                'budget_sha256':hashlib.sha256((ROOT/'runtime/cloud_budget.sqlite').read_bytes()).hexdigest()}

    def call(path,payload):
        return response_json(client.post(path,json={'session':session,**payload}))

    def wait_task(task):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            state=response_json(client.get('/task',params={'session':session,'id':task}))
            if state['status'] in TERMINAL:return state
            time.sleep(.05)
        raise TimeoutError('read_only_task_not_confirmed')

    def save(row):
        rows.append(row)
        with (out/'results.jsonl').open('a') as stream:
            stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        print(json.dumps({k:row[k] for k in ('case','passed','seconds','error') if k in row},ensure_ascii=False),flush=True)

    before=snapshot()
    if before['health']['cloud_budget']['remaining'] != 0:
        raise RuntimeError('this_regression_requires_existing_exhausted_cloud_budget')
    final_error=None
    try:
        # Verify the protective busy response still exists and preserves its body.
        row={'case':'busy_guard','expected_http_status':400}
        started=time.monotonic()
        try:
            row['quiet']=wait_quiet(client,session)
            seed=call('/turn',{'request_id':'busy-seed','text':'请告诉我现在的时间'})
            row['seed_state']=wait_task(seed['task_id'])
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                audio=response_json(client.get('/audio-status'))
                if audio['accepted_playbacks']:break
                time.sleep(.02)
            else:raise RuntimeError('internal_announcement_not_observed')
            row['before_recording']=audio
            response=client.post('/voice-turn',json={'session':session,'request_id':'busy-probe','seconds':1})
            row.update(http_status=response.status_code,response_body=response.text)
            row['passed']=response.status_code==400 and response.json().get('error')=='speaker_busy_try_after_playback'
        except Exception as exc:row.update(passed=False,**error_record(exc))
        row['seconds']=time.monotonic()-started;save(row)
        for repeat in range(repetitions):
            for text,kind in [('请告诉我现在的时间','system.time'),('请检查一下设备状态','system.status')]:
                key=f'{repeat}-{kind}'
                row={'case':key,'text':text,'expected':kind}
                started=time.monotonic()
                try:
                    row['quiet']=wait_quiet(client,session)
                    speech=request(ROOT,{'op':'tts','text':text})
                    clip=out/(key+'.pcm');clip.write_bytes(base64.b64decode(speech['pcm']))
                    row['fixture']={'path':str(clip.relative_to(ROOT)),'rate':speech['rate'],
                                    'seconds':clip.stat().st_size/(2*speech['rate'])}
                    with ThreadPoolExecutor() as pool:
                        future=pool.submit(call,'/voice-turn',{'request_id':key,'seconds':4})
                        deadline=time.monotonic()+3
                        while time.monotonic()<deadline:
                            if future.done():
                                future.result()
                                raise RuntimeError('recording_finished_before_fixture')
                            if response_json(client.get('/audio-status'))['microphone_busy']:break
                            time.sleep(.02)
                        else:raise TimeoutError('recording_did_not_start')
                        time.sleep(.3)
                        subprocess.run(['paplay','--raw','--format=s16le','--channels=1',
                                        '--rate='+str(speech['rate']),str(clip)],check=True,timeout=15)
                        result=future.result()
                    row['response']=result
                    state=wait_task(result['task_id']) if result.get('task_id') else None
                    row['state']=state
                    row['passed']=bool(result.get('intent',{}).get('kind')==kind and not result.get('error') and state and state['status']=='completed')
                    if state and state['status']=='unknown':raise RuntimeError('unknown_hardware_result')
                    row['after_quiet']=wait_quiet(client,session)
                except Exception as exc:row.update(passed=False,**error_record(exc))
                row['seconds']=time.monotonic()-started;save(row)
                if 'unknown_hardware_result' in row.get('error',''):
                    raise RuntimeError('stop_on_unknown_result')
    finally:
        try:wait_quiet(client,session)
        except Exception as exc:final_error=error_record(exc)
        after=snapshot()
        delta={k:n-before['operations'].get(k,0) for k,n in after['operations'].items() if n!=before['operations'].get(k,0)}
        report={'session':session,'planned':1+2*repetitions,'total':len(rows),'passed':sum(r['passed'] for r in rows),
                'scope':'development regression; local TTS speaker-to-microphone loopback; time/status only',
                'before':before,'after':after,'operation_delta':delta,'settle_error':final_error,
                'invariants_ok':before['budget_sha256']==after['budget_sha256'] and
                before['wire_audit']['wheel_packets_sent']==after['wire_audit']['wheel_packets_sent']==0 and
                before['wire_audit']['head_packets']==after['wire_audit']['head_packets'] and
                set(delta)<={'system.time','system.status','speech.say'}}
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False),flush=True)
        client.close()
    return report['passed']==report['planned'] and report['invariants_ok'] and not final_error


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--repetitions',type=int,choices=range(1,6),default=3)
    args=parser.parse_args()
    raise SystemExit(0 if run(args.output,args.repetitions) else 1)
