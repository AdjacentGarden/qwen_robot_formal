import pytest
from robot_graph.hybrid import HybridIntentModel, validate_proposal

class Model:
    def __init__(self,value):self.value=value;self.calls=0
    def classify(self,text):
        self.calls+=1
        if isinstance(self.value,Exception):raise self.value
        return self.value

UP={'type':'action','kind':'head.move','args':{'pose':'up'}}
CHAT={'type':'chat','reply':'请明确请求。'}

def test_valid_local_never_calls_cloud():
    local=Model(UP);cloud=Model(RuntimeError('must not call'));model=HybridIntentModel(local,cloud)
    assert model.classify('请抬头')==UP and cloud.calls==0
    assert model.last_route['route']=='local'

@pytest.mark.parametrize('proposal',[ValueError('invalid JSON'),{'type':'action','kind':'head.move','args':{'pose':'bad'}},UP])
def test_negative_local_is_terminal_without_cloud(proposal):
    cloud=Model(CHAT);model=HybridIntentModel(Model(proposal),cloud)
    assert model.classify('不要抬头')['type']=='chat' and cloud.calls==0
    assert model.last_route['route']=='policy_clarify'


def test_malformed_valid_local_still_falls_back_to_cloud():
    cloud=Model(UP);model=HybridIntentModel(Model(ValueError('invalid JSON')),cloud)
    assert model.classify('请抬头')==UP and cloud.calls==1
    assert model.last_route['route']=='cloud'


@pytest.mark.parametrize('text',[
    '打开灯然后抬头',
    '投食15克',
    '帮我投食',
    '拍一张照片',
    '原地做几个深蹲',
    '把头抬高30度',
])
def test_non_executable_requests_do_not_spend_cloud_budget(text):
    cloud=Model(CHAT);model=HybridIntentModel(Model(ValueError('invalid JSON')),cloud)
    assert model.classify(text)['type']=='chat' and cloud.calls==0
    assert model.last_route['route']=='policy_clarify'

def test_invalid_cloud_never_returns_physical_proposal():
    model=HybridIntentModel(Model(UP),Model(UP))
    assert model.classify('不要抬头')['type']=='chat'
    assert model.last_route['route']=='policy_clarify'

def test_budget_failure_returns_clarification_without_retry():
    cloud=Model(RuntimeError('budget_exhausted'));model=HybridIntentModel(Model(ValueError('parse')),cloud)
    assert model.classify('请抬头')['type']=='chat' and cloud.calls==1

def test_navigation_intent_is_preserved_for_final_base_guard():
    value={'type':'workflow','name':'meeting','parameters':{}}
    cloud=Model(CHAT);model=HybridIntentModel(Model(value),cloud)
    assert model.classify('请启动会议投影')==value and cloud.calls==0


@pytest.mark.parametrize('text',[
    '请你原地投影会议的 p p t',
    '请在当前位置进行会议投影',
    '请你在这里投影会议内容',
    '不要导航，开始会议投影',
    '地投影会议 p p t',
])
def test_stationary_meeting_is_canonicalized_after_model_proposal(text):
    proposal={'type':'workflow','name':'meeting','parameters':{'action':'start','content':'PPT'}}
    assert validate_proposal(text,proposal)=={'type':'workflow','name':'meeting_stationary','parameters':{}}


def test_model_invented_stationary_meeting_parameters_are_removed():
    proposal={'type':'workflow','name':'meeting_stationary','parameters':{'camera':'front','head.move':'up'},'command':'navigate'}
    assert validate_proposal('请原地抬头播放会议内容',proposal)=={'type':'workflow','name':'meeting_stationary','parameters':{}}


def test_model_invented_navigation_meeting_fields_are_removed():
    proposal={'type':'workflow','name':'meeting','parameters':{},'command':'navigate'}
    assert validate_proposal('导航到书房投影点并开始会议',proposal)=={'type':'workflow','name':'meeting','parameters':{}}


def test_nonstationary_meeting_is_not_silently_changed():
    proposal={'type':'workflow','name':'meeting','parameters':{'action':'start'}}
    assert validate_proposal('请启动会议投影',proposal)=={'type':'workflow','name':'meeting','parameters':{}}


def test_model_stationary_guess_without_stationary_cue_stays_navigation():
    proposal={'type':'workflow','name':'meeting_stationary'}
    assert validate_proposal('帮我启动会议场景',proposal)=={'type':'workflow','name':'meeting','parameters':{}}


def test_explicit_camera_side_corrects_model_proposal():
    proposal={'type':'action','kind':'camera.capture','args':{'camera':'front'}}
    assert validate_proposal('请你用后面的摄像头拍个照',proposal)=={'type':'action','kind':'camera.capture','args':{'camera':'back'}}


def test_explicit_head_pose_corrects_model_proposal():
    proposal={'type':'action','kind':'head.move','args':{'pose':'down'}}
    assert validate_proposal('请抬头',proposal)=={'type':'action','kind':'head.move','args':{'pose':'up'}}


def test_exercise_parameters_are_grounded_after_model_proposal():
    proposal={'type':'workflow','name':'exercise_stationary','parameters':{'exercise':'pull_up','count':None}}
    assert validate_proposal('在原地帮我数十个深蹲',proposal)=={'type':'workflow','name':'exercise_stationary','parameters':{'exercise':'squat','count':10}}


def test_capability_question_does_not_dispatch_light():
    proposal={'type':'action','kind':'light.set','args':{'enabled':True}}
    cloud=Model(proposal);model=HybridIntentModel(Model(proposal),cloud)
    assert model.classify('你能开灯吗')['type']=='chat'
    assert model.last_route['route']=='policy_clarify'
    assert cloud.calls==0
