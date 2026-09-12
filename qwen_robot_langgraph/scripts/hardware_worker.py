#!/usr/bin/env python3
"""System-Python device adapter: avoids mixing ROS/NPU libraries with LangGraph's venv."""
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import wave

ROOT=Path(__file__).resolve().parents[1]
kind,args=sys.argv[1],json.loads(sys.argv[2])
os.environ['PYTHONDONTWRITEBYTECODE']='1'
started=time.monotonic()


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


try:
    if kind=='camera.capture':
        import cv2
        device={'front':'/dev/video22','back':'/dev/video31'}[args['camera']]
        cap=cv2.VideoCapture(device,cv2.CAP_V4L2)
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,640);cap.set(cv2.CAP_PROP_FRAME_HEIGHT,480)
            success,frame=cap.read()
            if not success or frame is None:
                raise RuntimeError('camera_read_failed')
            path=ROOT/'runtime/captures'/f"{args['camera']}_{time.time_ns()}.jpg"
            path.parent.mkdir(parents=True,exist_ok=True)
            if not cv2.imwrite(str(path),frame):
                raise RuntimeError('camera_write_failed')
            result={'path':str(path),'shape':list(frame.shape),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        finally:
            cap.release()
    elif kind=='speaker.test':
        path=ROOT/'runtime/speaker_test.wav';path.parent.mkdir(parents=True,exist_ok=True)
        with wave.open(str(path),'wb') as w:
            w.setparams((1,2,24000,0,'NONE','not compressed'))
            w.writeframes(b''.join(struct.pack('<h',int(2500*math.sin(2*math.pi*660*i/24000))) for i in range(7200)))
        subprocess.run(['paplay',str(path)],check=True,timeout=5)
        result={'duration_ms':300,'tone_hz':660,'playback_exit':0}
    elif kind.startswith('projector.'):
        os.environ['PROJECTOR_CONTROL_CONFIG']=str(ROOT/'config/private_projector.json')
        module=load(ROOT/'vendor/skills/projector_control/run.py','isolated_projector')
        act={'projector.off':'off','projector.status':'status','projector.pause':'meeting_pause','projector.resume':'meeting_resume','projector.start':'meeting_presentation_on' if args.get('mode')=='meeting' else 'fitness_video_on'}[kind]
        code=module.main([act,'--json','--timeout','20'])
        sys.exit(code or 0)
    elif kind in {'light.set','feeder.feed','feeder.status'}:
        skill='light_control' if kind=='light.set' else 'feeder_control'
        os.environ['MIJIA_LIGHT_CONFIG']=str(ROOT/'config/private_light.json')
        os.environ['MIJIA_FEEDER_CONFIG']=str(ROOT/'config/private_feeder.json')
        # Vendor auth files and lock paths are private copies inside this project.
        command=[sys.executable,str(ROOT/'vendor/skills'/skill/'run.py')]
        command+=['on' if args['enabled'] else 'off'] if kind=='light.set' else ['status'] if kind=='feeder.status' else ['feed','--grams',str(args['grams'])]
        completed=subprocess.run(command,text=True,capture_output=True,timeout=25)
        print(completed.stdout,end='');sys.exit(completed.returncode)
    else:
        raise RuntimeError('hardware_operation_not_allowed')
    print(json.dumps({'ok':True,'status':'completed','executed':True,'result':result,'elapsed_ms':(time.monotonic()-started)*1000}))
except Exception as exc:
    print(json.dumps({'ok':False,'status':'failed','executed':None,'error':f'{type(exc).__name__}:{exc}'}))
