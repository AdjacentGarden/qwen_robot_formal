"""Render confirmed results without another LLM call or invented device facts."""
from datetime import datetime


def result_speech(s):
    name=s['plan']['name'];result=s.get('result',{})
    if name=='system.time' and result.get('time'):
        value=datetime.fromisoformat(result['time'])
        return f'现在是{value.hour}点{value.minute}分。'
    if name in {'meeting_stationary','meeting'}:return '会议投影已结束，头部已经回正。'
    if name=='head.move':
        pose=s['plan']['steps'][0]['args']['pose']
        return {'up':'头部已经抬起。','down':'头部已经低下。','level':'头部已经回正。'}[pose]
    if name=='camera.capture':return '拍照已完成。'
    if name=='light.set':return '灯光操作已完成。'
    if name=='feeder.feed':return '投食机已返回成功。'
    if name=='system.status':return '当前禁止底轮运动。'
    if name.startswith('exercise'):return '运动计数任务已完成。'
    return '任务已完成。'
