"""Local-first intent proposals with validated, budgeted cloud fallback.

Neither model can issue hardware commands. Final validation stays in Dialogue.
"""
from .contracts import Action, PolicyError, StationaryPolicy
from .intent_codec import grounded_quantity
from .intent_policy import request_needs_clarification, requests_stationary, validate_current_turn
from .workflows import build_plan


def canonicalize_proposal(text, intent):
    """Repair only a model-proposed meeting workflow, never create an intent."""
    if not isinstance(intent,dict):
        return intent
    result=dict(intent)
    if result.get('type')=='workflow' and result.get('name') in {'meeting','meeting_stationary'}:
        # Meeting has a fixed reviewed plan. User text cannot add arbitrary
        # actions through omitted or model-invented fields/parameters.
        return {
            'type':'workflow',
            'name':'meeting_stationary' if requests_stationary(text) else 'meeting',
            'parameters':{},
        }
    elif result.get('type')=='action' and result.get('kind')=='camera.capture':
        import re
        normalized=re.sub(r'\s+','',text)
        front=bool(re.search(r'前置|前面|前摄|前方.*摄像头',normalized))
        back=bool(re.search(r'后置|后面|后摄|后方.*摄像头',normalized))
        if front != back:
            result['args']={'camera':'front' if front else 'back'}
    elif result.get('type')=='action' and result.get('kind')=='head.move':
        import re
        normalized=re.sub(r'\s+','',text)
        poses=[]
        if re.search(r'抬(?:一?下|起)?头|抬起|头.*(?:抬|向上)|向上.*头',normalized):poses.append('up')
        if re.search(r'低(?:一?下)?头|头.*(?:低下|向下)|向下.*头',normalized):poses.append('down')
        if re.search(r'回正|水平|摆正|归中',normalized):poses.append('level')
        if len(set(poses))==1:result['args']={'pose':poses[0]}
    elif result.get('type')=='workflow' and result.get('name')=='exercise_stationary' and isinstance(result.get('parameters'),dict):
        parameters=dict(result['parameters'])
        exercises=[kind for phrase,kind in [('俯卧撑','push_up'),('深蹲','squat'),('引体向上','pull_up')] if phrase in text]
        count=grounded_quantity(text,'个')
        if len(exercises)==1:parameters['exercise']=exercises[0]
        if count is not None:parameters['count']=count
        result['parameters']=parameters
    return result


def validate_proposal(text, intent):
    intent=canonicalize_proposal(text,intent)
    schemas={'action':{'type','kind','args'},'workflow':{'type','name','parameters'},'control':{'type','command'},'chat':{'type','reply'}}
    if not isinstance(intent,dict) or intent.get('type') not in schemas or set(intent)!=schemas[intent['type']]:
        raise PolicyError('invalid_intent_schema')
    kind=intent['type']
    if kind=='action':
        if intent['kind'] not in {'head.move','camera.capture','light.set','feeder.feed','system.time','system.status'}:raise PolicyError('model_action_not_advertised')
        StationaryPolicy().validate(Action(intent['kind'],intent['args']))
    elif kind=='workflow':
        plan=build_plan(intent['name'],intent['parameters'])
        for item in plan['steps']+plan['cleanup']:
            try:StationaryPolicy().validate(Action.parse(item))
            except PolicyError as exc:
                # Understanding a navigation request is valid; Dialogue must block it.
                if 'base_motion_forbidden' not in str(exc):raise
    elif kind=='control' and intent['command'] not in {'pause','resume','cancel'}:raise PolicyError('invalid_control')
    elif kind=='chat' and (not isinstance(intent['reply'],str) or not 0<len(intent['reply'])<=1000):raise PolicyError('invalid_reply')
    validate_current_turn(text,intent)
    return intent


def terminal_clarification(text):
    """Return a safe reply when cloud validation cannot make a request executable.

    This runs only after the local model has produced an invalid proposal.  It is
    a safety/parameter gate, not an intent shortcut: valid physical requests still
    reach cloud fallback when the local proposal is malformed.
    """
    import re
    if request_needs_clarification(text):
        return '请一次说明一个当前要执行的操作及其参数；当前未执行硬件操作。'
    normalized=re.sub(r'\s+','',text)
    if re.search(r'喂|投粮|投食|出粮',normalized):
        grams=grounded_quantity(text,'克')
        if grams is None or not (10 <= grams <= 100 and grams % 10 == 0):
            return '请明确提供10到100克之间、以10克递增的投食量；当前未执行投食。'
    if re.search(r'拍|照',normalized):
        front=bool(re.search(r'前置|前面|前摄|前方.*摄像头',normalized))
        back=bool(re.search(r'后置|后面|后摄|后方.*摄像头',normalized))
        if front == back:
            return '请明确使用前置或后置摄像头；当前未拍照。'
    if re.search(r'俯卧撑|深蹲|引体向上',normalized):
        count=grounded_quantity(text,'个')
        if count is None or not 1 <= count <= 200:
            return '请明确提供1到200之间的运动次数；当前未开始运动计数。'
    if re.search(r'(?:抬|低|转|摆).*头|头.*(?:抬|低|转|摆)',normalized) and (re.search(r'\d',normalized) or '度' in normalized):
        return '当前头部只支持抬头、低头和回正，请选择其中一个动作。'
    return None


class HybridIntentModel:
    model='Qwen3-4B + Qwen cloud fallback'
    url='http://127.0.0.1:18087/v1'
    def __init__(self,local,cloud,event_sink=None):
        self.local=local;self.cloud=cloud;self.event_sink=event_sink;self.last_route={}

    def record(self,**record):
        self.last_route=record
        if self.event_sink:self.event_sink(record)

    def classify(self,text):
        try:
            intent=validate_proposal(text,self.local.classify(text))
            self.record(route='local',fallback=False)
            return intent
        except Exception as exc:
            reason=f'{type(exc).__name__}:{exc}'[:240]
        reply=terminal_clarification(text)
        if reply is not None:
            self.record(route='policy_clarify',fallback=False,local_issue=reason)
            return {'type':'chat','reply':reply}
        try:
            intent=validate_proposal(text,self.cloud.classify(text))
            self.record(route='cloud',fallback=True,local_issue=reason)
            return intent
        except Exception as exc:
            self.record(route='clarify',fallback=True,local_issue=reason,cloud_issue=f'{type(exc).__name__}:{exc}'[:240])
            return {'type':'chat','reply':'这条指令还需要明确，请一次说明一个操作及其参数；当前未执行硬件操作。'}
