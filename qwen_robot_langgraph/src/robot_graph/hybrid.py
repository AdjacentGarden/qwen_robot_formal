"""Local-first intent proposals with validated, budgeted cloud fallback.

Neither model can issue hardware commands. Final validation stays in Dialogue.
"""
from .contracts import Action, PolicyError, StationaryPolicy
from .intent_policy import validate_current_turn
from .workflows import build_plan


def canonicalize_proposal(text, intent):
    """Normalize the fixed meeting schema without interpreting user text.

    Meeting is a reviewed workflow with no user parameters.  Removing invented
    fields is structural normalization; the proposal's semantic choice is never
    changed here.  Any mismatch with the current utterance is rejected by the
    grounding policy and sent to the cloud model.
    """
    if not isinstance(intent,dict):
        return intent
    if intent.get('type')=='workflow' and intent.get('name') in {'meeting','meeting_stationary'}:
        return {
            'type':'workflow',
            'name':intent['name'],
            'parameters':{},
        }
    return intent


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


class HybridIntentModel:
    model='Qwen3-4B + Qwen cloud fallback'
    url='http://127.0.0.1:18087/v1'
    def __init__(self,local,cloud,event_sink=None):
        self.local=local;self.cloud=cloud;self.event_sink=event_sink;self.last_route={}

    def record(self,**record):
        self.last_route=record
        if self.event_sink:self.event_sink(record)

    def classify(self,text):
        proposal=None
        try:
            proposal=self.local.classify(text)
            intent=validate_proposal(text,proposal)
            self.record(route='local',fallback=False)
            return intent
        except Exception as exc:
            reason=f'{type(exc).__name__}:{exc}'[:240]
        try:
            if hasattr(self.cloud,'review'):
                raw=getattr(self.local,'last_response_info',{}).get('raw')
                intent=self.cloud.review(text,proposal if proposal is not None else raw,reason)
            else:
                intent=self.cloud.classify(text)
            intent=validate_proposal(text,intent)
            self.record(route='cloud',fallback=True,local_issue=reason)
            return intent
        except Exception as exc:
            self.record(route='clarify',fallback=True,local_issue=reason,cloud_issue=f'{type(exc).__name__}:{exc}'[:240])
            return {'type':'chat','reply':'这条指令还需要明确，请一次说明一个操作及其参数；当前未执行硬件操作。'}
