"""Use the existing paid Qwen-Audio model for bounded text-only intent proposals."""
import asyncio
import json

from .cloud_budget import CloudBudget
from .dialogue import INTENT_INSTRUCTIONS
from .voice import connect, receive, session_update


class RealtimeJsonModel:
    def __init__(self,config):self.config=config
    def classify(self,text):return asyncio.run(self._classify(text))
    async def _classify(self,text):
        ws=await connect(self.config)
        try:
            update=session_update(self.config,INTENT_INSTRUCTIONS+'\n只输出合法JSON，不要Markdown，不要额外解释。')
            update['session']['modalities']=['text']
            await ws.send(json.dumps(update,ensure_ascii=False));await receive(ws,'session.updated')
            await ws.send(json.dumps({'type':'conversation.item.create','item':{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}},ensure_ascii=False))
            CloudBudget(self.config['cloud_budget_path']).reserve('realtime_intent_response')
            await ws.send(json.dumps({'type':'response.create','response':{'modalities':['text']}}))
            chunks=[]
            async def collect():
                while True:
                    event=json.loads(await ws.recv());kind=event.get('type')
                    if kind=='error':raise RuntimeError('qwen_intent:'+str(event.get('error',{}).get('code')))
                    if kind=='response.text.delta':chunks.append(event.get('delta',''))
                    elif kind=='response.text.done' and not chunks:chunks.append(event.get('text',''))
                    elif kind=='response.done':
                        if event.get('response',{}).get('status') in {'cancelled','failed','incomplete'}:raise RuntimeError('qwen_intent_incomplete')
                        break
            await asyncio.wait_for(collect(),20)
            content=''.join(chunks).strip()
            if content.startswith('```json') and content.endswith('```'):content=content[7:-3].strip()
            result=json.loads(content)
            if not isinstance(result,dict):raise ValueError('qwen_intent_not_object')
            return result
        finally:await ws.close()
