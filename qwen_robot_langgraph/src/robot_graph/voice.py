from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from pathlib import Path
import time
import uuid

import websockets


def session_update(config, instructions):
    return {'type':'session.update','session':{'modalities':['text','audio'],'voice':config.get('voice','longanqian'),'instructions':instructions,'input_audio_format':'pcm','output_audio_format':'pcm','turn_detection':{'type':'server_vad','threshold':.5,'silence_duration_ms':600},'max_history_turns':2}}


async def receive(ws, expected, timeout=15):
    async with asyncio.timeout(timeout) if hasattr(asyncio, 'timeout') else _Timeout(timeout):
        while True:
            event=json.loads(await ws.recv())
            if event.get('type')=='error':
                err=event.get('error',{})
                raise RuntimeError('qwen_realtime:'+str(err.get('code'))+':'+str(err.get('message'))[:300])
            if event.get('type')==expected:
                return event


class _Timeout:
    """Python 3.10 timeout context for the robot's system-compatible venv."""
    def __init__(self, seconds): self.seconds=seconds
    async def __aenter__(self):
        self.task=asyncio.current_task();self.handle=asyncio.get_running_loop().call_later(self.seconds,self.task.cancel)
    async def __aexit__(self, typ, value, tb):
        self.handle.cancel()
        if typ is asyncio.CancelledError:
            raise TimeoutError('realtime_timeout') from value


async def connect(config):
    from .cloud_budget import CloudBudget
    if not config.get('cloud_budget_path'):
        raise RuntimeError('cloud_budget_not_configured')
    CloudBudget(config['cloud_budget_path']).reserve('realtime_connection')
    key=Path(config['api_key_path']).read_text().strip()
    ws=await websockets.connect(config['realtime_url'],extra_headers={'Authorization':'Bearer '+key},open_timeout=10,close_timeout=2,max_size=8*1024*1024)
    try:
        await receive(ws,'session.created')
    except BaseException:
        await ws.close();raise
    return ws


async def synthesize(text, config, cancel, runtime_path):
    started=time.monotonic(); first=None; total=0; player=None; ws=None
    try:
        ws=await connect(config)
        await ws.send(json.dumps(session_update(config,'你是播报器，只逐字朗读用户提供的文字，不添加任何内容，不调用工具。')))
        await receive(ws,'session.updated')
        await ws.send(json.dumps({'type':'conversation.item.create','item':{'type':'message','role':'user','content':[{'type':'input_text','text':'只朗读：'+text}]}}))
        from .cloud_budget import CloudBudget
        CloudBudget(config['cloud_budget_path']).reserve('realtime_tts_response')
        await ws.send(json.dumps({'type':'response.create','response':{'modalities':['audio','text']}}))
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            if cancel.is_set():
                return {'ok':False,'status':'cancelled','executed':first is not None}
            try:
                event=json.loads(await asyncio.wait_for(ws.recv(),.15))
            except asyncio.TimeoutError:
                continue
            if event.get('type')=='error':
                raise RuntimeError('qwen_tts:'+str(event.get('error',{}).get('code')))
            if event.get('type')=='response.audio.delta':
                pcm=base64.b64decode(event['delta'],validate=True)
                if player is None:
                    player=await asyncio.create_subprocess_exec('paplay','--raw','--format=s16le','--rate=24000','--channels=1',stdin=asyncio.subprocess.PIPE)
                    first=time.monotonic()
                player.stdin.write(pcm);await player.stdin.drain();total+=len(pcm)
            if event.get('type')=='response.done':
                break
        else:
            raise TimeoutError('tts_deadline')
        if player is None:
            raise RuntimeError('tts_no_audio')
        player.stdin.close()
        while player.returncode is None:
            if cancel.is_set():
                player.terminate();await player.wait()
                return {'ok':False,'status':'cancelled','executed':True}
            await asyncio.sleep(.02)
        if player.returncode:
            raise RuntimeError('audio_player_failed')
        return {'ok':True,'status':'completed','executed':True,'first_audio_ms':(first-started)*1000,'elapsed_ms':(time.monotonic()-started)*1000,'pcm_bytes':total}
    except Exception as exc:
        return {'ok':False,'status':'failed','executed':first is not None,'error':str(exc)[:400]}
    finally:
        if player and player.returncode is None:
            with contextlib.suppress(ProcessLookupError): player.terminate()
            await player.wait()
        if ws:
            await ws.close()


class TranscriptGate:
    """Only a final transcript of the active item may create a robot task."""
    def __init__(self):
        self.item=None;self.seen=set();self.generation=uuid.uuid4().hex
    def handle(self,event):
        kind=event.get('type');item=event.get('item_id')
        if kind=='input_audio_buffer.speech_started':
            self.item=item;return None
        if kind!='conversation.item.input_audio_transcription.completed':
            return None
        if not item or item!=self.item or item in self.seen:
            return None
        text=event.get('transcript','').strip()
        if not text:
            return None
        self.seen.add(item)
        return self.generation+':'+item,text


async def listen(dialogue, config, session='voice'):
    """Transport-only: ignore all unsolicited cloud audio and tool suggestions."""
    ws=await connect(config);mic=None;sender=None;workers=set();gate=TranscriptGate()
    async def process(turn,text):
        result=await asyncio.to_thread(dialogue.turn,session,turn,text)
        # Every reply is emitted through the same outbox and resource arbiter.
        dialogue.runtime.store.event('', 'voice_turn', reply=result.get('reply'), task_id=result.get('task_id'))
        from .contracts import Action
        from .workflows import atomic_plan
        reply_task=dialogue.runtime.submit(session,turn+':reply',atomic_plan(Action('speech.say',{'text':result.get('reply','')[:500]})))
    try:
        await ws.send(json.dumps(session_update(config,'仅转写用户语音。不要建议或执行任何机器人动作。')))
        await receive(ws,'session.updated')
        mic=await asyncio.create_subprocess_exec('parec','--raw','--format=s16le','--rate=16000','--channels=1',stdout=asyncio.subprocess.PIPE)
        async def send_audio():
            last_speech=0
            while True:
                pcm=await mic.stdout.readexactly(640)
                speaking=dialogue.runtime.store.one("SELECT id FROM operations WHERE kind='speech.say' AND status='accepted'")
                if speaking: last_speech=time.monotonic()
                # Echo protection until an AEC path is validated on this robot.
                if speaking or time.monotonic()-last_speech<.4: pcm=bytes(len(pcm))
                await ws.send(json.dumps({'type':'input_audio_buffer.append','audio':base64.b64encode(pcm).decode()}))
        sender=asyncio.create_task(send_audio())
        async for raw in ws:
            event=json.loads(raw)
            if event.get('type')=='error':
                raise RuntimeError('qwen_voice:'+str(event.get('error',{}).get('code')))
            if event.get('type')=='input_audio_buffer.speech_started':
                from .cloud_budget import CloudBudget
                CloudBudget(config['cloud_budget_path']).reserve('realtime_audio_turn')
            turn=gate.handle(event)
            if turn:
                worker=asyncio.create_task(process(*turn));workers.add(worker);worker.add_done_callback(workers.discard)
    finally:
        if sender:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError): await sender
        if mic and mic.returncode is None:
            mic.terminate();await mic.wait()
        if workers: await asyncio.gather(*workers,return_exceptions=True)
        await ws.close()
