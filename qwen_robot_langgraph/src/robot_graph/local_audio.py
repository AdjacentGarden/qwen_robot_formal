from __future__ import annotations
import base64
import json
from pathlib import Path
import socket
import struct
import subprocess
import time


def request(root,payload,timeout=45):
    data=json.dumps(payload,ensure_ascii=False).encode()
    def exact(sock,n):
        chunks=bytearray()
        while len(chunks)<n:
            part=sock.recv(n-len(chunks))
            if not part:raise ConnectionError('audio_worker_closed')
            chunks.extend(part)
        return bytes(chunks)
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(timeout);sock.connect(str(Path(root)/'runtime/audio.sock'))
        sock.sendall(struct.pack('!I',len(data))+data)
        n=struct.unpack('!I',exact(sock,4))[0]
        if n>20_000_000:raise ValueError('audio_response_too_large')
        result=json.loads(exact(sock,n))
    if not result.get('ok'):raise RuntimeError(result.get('error','audio_worker_failed'))
    return result


def speak(root,text,cancel):
    start=time.monotonic()
    try:
        result=request(root,{'op':'tts','text':text})
        if cancel.is_set():return {'ok':False,'status':'cancelled','executed':False}
        pcm=base64.b64decode(result.pop('pcm'),validate=True)
        path=Path(root)/'runtime/current_speech.pcm';path.write_bytes(pcm)
        player=subprocess.Popen(['paplay','--raw','--format=s16le','--channels=1','--rate='+str(result['rate']),str(path)])
        first=time.monotonic()
        try:
            while player.poll() is None:
                if cancel.is_set():
                    player.terminate();player.wait(timeout=3)
                    return {'ok':False,'status':'cancelled','executed':True}
                time.sleep(.02)
            return {**result,'ok':player.returncode==0,'status':'completed' if player.returncode==0 else 'failed','executed':True,'first_audio_ms':(first-start)*1000,'elapsed_ms':(time.monotonic()-start)*1000,'pcm_bytes':len(pcm)}
        finally:
            if player.poll() is None:player.terminate();player.wait(timeout=3)
    except Exception as exc:
        return {'ok':False,'status':'failed','executed':False,'error':str(exc)}


def record_and_transcribe(root, seconds=4):
    if type(seconds) not in (int,float) or not 1<=seconds<=8:
        raise ValueError('recording_seconds_must_be_1_to_8')
    # Bound Pulse buffering so stopping the client does not discard a long audio tail.
    recorder=subprocess.Popen(['parec','--raw','--format=s16le','--rate=16000','--channels=1',
                               '--latency-msec=40','--process-time-msec=20'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    stopped = False
    try:
        try:pcm,diagnostic=recorder.communicate(timeout=seconds + .1)
        except subprocess.TimeoutExpired:
            stopped = True
            recorder.terminate();pcm,diagnostic=recorder.communicate(timeout=3)
    finally:
        if recorder.poll() is None:recorder.kill();recorder.wait()
    pcm=pcm[:len(pcm)//2*2]
    if not stopped and recorder.returncode:
        raise RuntimeError('microphone_capture_failed:' + diagnostic.decode(errors='replace')[-200:])
    if len(pcm) < 3200:
        raise RuntimeError('microphone_audio_too_short')
    result=request(root,{'op':'asr','pcm':base64.b64encode(pcm).decode()})
    result['captured_seconds'] = len(pcm) / 32000
    return result
