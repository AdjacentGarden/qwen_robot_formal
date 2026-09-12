#!/usr/bin/env python3
"""Persistent CPU speech models in system Python, outside the graph interpreter."""
import base64
import json
import os
from pathlib import Path
import signal
import socketserver
import struct
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'vendor/local_audio'))
from tts_backend import MatchaTTS
from sensevoice_backend import SenseVoiceASR

SOCKET=ROOT/'runtime/audio.sock'
tts=MatchaTTS(threads=3)
asr=SenseVoiceASR('/home/test/qwen_edge_cloud_switch_lab_20260904/runtime/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17',threads=3)
tts_lock=threading.Lock();asr_lock=threading.Lock()


def exact(sock,n):
    data=bytearray()
    while len(data)<n:
        part=sock.recv(n-len(data))
        if not part:raise EOFError()
        data.extend(part)
    return bytes(data)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(45)
        try:
            length=struct.unpack('!I',exact(self.request,4))[0]
            if not 0<length<=2_000_000:raise ValueError('request_too_large')
            data=json.loads(exact(self.request,length))
            if data['op']=='tts':
                text=data['text']
                if not isinstance(text,str) or not 0<len(text)<=500:raise ValueError('invalid_text')
                with tts_lock:pcm,rate,seconds=tts.synthesize(text)
                result={'ok':True,'pcm':base64.b64encode(pcm).decode(),'rate':rate,'synthesis_seconds':seconds,'backend':'local_matcha'}
            elif data['op']=='asr':
                pcm=base64.b64decode(data['pcm'],validate=True)
                if len(pcm)>640000:raise ValueError('audio_too_long')
                with asr_lock:text,seconds=asr.transcribe(pcm)
                result={'ok':True,'text':text,'seconds':seconds,'backend':'sensevoice_cpu'}
            elif data['op']=='ping':result={'ok':True,'tts_loaded':tts.engine is not None,'asr_loaded':asr.engine is not None}
            else:raise ValueError('unknown_audio_operation')
        except Exception as exc:result={'ok':False,'error':f'{type(exc).__name__}:{exc}'}
        payload=json.dumps(result,ensure_ascii=False).encode()
        self.request.sendall(struct.pack('!I',len(payload))+payload)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads=True


if __name__=='__main__':
    SOCKET.parent.mkdir(exist_ok=True)
    if SOCKET.exists():
        import socket
        with socket.socket(socket.AF_UNIX) as probe:
            try:probe.connect(str(SOCKET))
            except OSError:SOCKET.unlink()
            else:raise SystemExit('audio_worker_already_running')
    server=Server(str(SOCKET),Handler);os.chmod(SOCKET,0o600)
    def stop(*args):threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    try:server.serve_forever()
    finally:server.server_close();SOCKET.unlink(missing_ok=True)
