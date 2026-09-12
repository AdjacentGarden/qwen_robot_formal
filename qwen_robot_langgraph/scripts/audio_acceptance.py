#!/usr/bin/env python3
"""Speaker -> real microphone -> CPU ASR -> dialogue graph -> read-only time query."""
import json
from pathlib import Path
import re
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from robot_graph.runtime import Runtime
from robot_graph.hardware import HardwareBackend
from robot_graph.contracts import Action
from robot_graph.dialogue import Dialogue
from robot_graph.workflows import atomic_plan
from robot_graph.local_audio import request
import base64,math,struct

rt=Runtime(ROOT/'runtime'/('audio_acceptance_'+str(time.time_ns())),HardwareBackend(ROOT));record=None
report={'cloud_calls':0,'base_locked':True}
try:
    capture=ROOT/'runtime/microphone_loopback.pcm'
    with capture.open('wb') as out:
        record=subprocess.Popen(['parec','--raw','--format=s16le','--rate=16000','--channels=1'],stdout=out)
        time.sleep(.3)
        task=rt.submit('audio_test','spoken_fixture',atomic_plan(Action('speech.say',{'text':'现在几点？'})))
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            rt.tick_all();status=rt.status(task)
            if status['status'] in {'completed','failed','unknown'}:break
            time.sleep(.02)
        time.sleep(.5);record.terminate();record.wait(timeout=3)
    pcm=capture.read_bytes();pcm=pcm[:len(pcm)//2*2]
    values=struct.unpack('<'+'h'*(len(pcm)//2),pcm)
    report['playback_status']=status['status'];report['recorded_seconds']=len(pcm)/32000
    report['microphone_rms']=math.sqrt(sum(x*x for x in values)/max(1,len(values)))
    decoded=request(ROOT,{'op':'asr','pcm':base64.b64encode(pcm[:640000]).decode()})
    report['asr']=decoded
    if re.sub(r'[\s，。！？!?]','',decoded['text'])=='现在几点':
        result=Dialogue(rt).turn('audio_test','decoded_fixture',decoded['text']);report['dialogue']=result
        task=result.get('task_id')
        if task:
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                rt.tick_all();status=rt.status(task)
                if status['status'] in {'completed','failed','unknown'}:break
                time.sleep(.02)
            report['task']=status
        report['passed']=bool(task and status['status']=='completed')
    else:
        report['passed']=False;report['note']='Transcript did not match the fixture; no recognized hardware action dispatched.'
except Exception as exc:report['passed']=False;report['error']=str(exc)
finally:
    if record and record.poll() is None:record.terminate();record.wait(timeout=3)
    rt.close(cancel_tasks=True)
    (ROOT/'runtime/audio_acceptance_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
