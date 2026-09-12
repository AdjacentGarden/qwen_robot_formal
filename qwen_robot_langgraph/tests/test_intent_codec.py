import pytest
from robot_graph.intent_codec import decode_intent
from robot_graph.dialogue import ConservativeRouter
from robot_graph.intent_policy import validate_current_turn, quantity_matches
from robot_graph.contracts import PolicyError


class RecordingModel:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def classify(self, text):
        self.calls.append(text)
        return self.value


@pytest.mark.parametrize('raw', [
    '{"type":"system.time"}', '{"type":"system.time","args":{}}',
    '```json\n{"type":"action","kind":"system.time","args":{}}\n```',
])
def test_read_only_aliases(raw):
    assert decode_intent(raw, '现在几点')['kind'] == 'system.time'


@pytest.mark.parametrize('number', ['十', '１０', '"十"', '"10"'])
def test_grounded_number_spellings(number):
    raw = '{"type":"action","kind":"feeder.feed","args":{"grams":' + number + '}}'
    assert decode_intent(raw, '投食10克')['args']['grams'] == 10
    with pytest.raises(PolicyError): decode_intent(raw, '投食20克')


@pytest.mark.parametrize('raw,text,key,value',[
    ('{"type":"action","kind":"feeder.feed","args":{"grams":eighty}}','投食80克','args',80),
    ('{"type":"workflow","name":"exercise_stationary","parameters":{"exercise":"squat","count":10个}}','原地做10个深蹲','parameters',10),
    ('{"type":"workflow","name":"exercise_stationary","parameters":{"exercise":"squat","count":null}}','原地做十个深蹲','parameters',10),
    ('{"type":"workflow","name":"exercise_stationary","parameters":{"exercise":"squat","count":}}','原地做5个深蹲','parameters',5),
])
def test_bounded_grounded_numeric_repairs(raw,text,key,value):
    intent=decode_intent(raw,text)
    field='grams' if key=='args' else 'count'
    assert intent[key][field]==value


@pytest.mark.parametrize('raw', [
    '{"type":"chat","type":"action","reply":"你好"}',
    '{"type":"action","kind":"system.time","args":{}} {}',
    '我想执行 {"type":"action","kind":"system.time","args":{}}',
    '{"type":"action","kind":"head.move","args":{"pose":NaN}}',
    '[{"type":"chat","reply":"你好"}]',
])
def test_ambiguous_or_non_json_output_rejected(raw):
    with pytest.raises(ValueError): decode_intent(raw, '抬头')


@pytest.mark.parametrize('text', ['不要查时间', '如果要知道现在几点', '告诉我现在几点然后抬头', '你能开灯吗', '请问你能抬头吗'])
def test_clarification_before_shortcuts(text):
    assert ConservativeRouter().classify(text)['type'] == 'chat'


@pytest.mark.parametrize('text', ['关闭投影', '结束会议', '现在几点', '原地会议投影', '记住：投食20克'])
def test_model_only_mode_bypasses_all_local_intent_matching(text):
    value = {'type':'chat','reply':'来自模型'}
    model = RecordingModel(value)
    assert ConservativeRouter(model, local_matching=False).classify(text) == value
    assert model.calls == [text]


@pytest.mark.parametrize('text', ['关闭投影', '关掉会议投影', '关闭会议'])
def test_projector_close_can_ground_cancel_intent(text):
    validate_current_turn(text, {'type':'control','command':'cancel'})


@pytest.mark.parametrize('text,pose', [('麻烦抬一下头','up'), ('麻烦低一下头','down')])
def test_natural_head_wording_is_grounded(text,pose):
    validate_current_turn(text, {'type':'action','kind':'head.move','args':{'pose':pose}})


@pytest.mark.parametrize('text', ['投食-10克','投食20.10克','投食10到20克','投食负十克','投食十到二十克'])
def test_no_partial_numeric_match(text):
    assert not quantity_matches(text,10,'克')


def test_wrong_exercise_and_wrong_readonly_proposals_rejected():
    with pytest.raises(PolicyError):
        validate_current_turn('原地做十个深蹲', {'type':'workflow','name':'exercise_stationary','parameters':{'exercise':'push_up','count':10}})
    with pytest.raises(PolicyError):
        validate_current_turn('请抬头', {'type':'action','kind':'system.time','args':{}})


@pytest.mark.parametrize('text,command', [('请继续当前任务','resume'),('麻烦暂停正在进行的会议','pause'),('请取消目前任务吧','cancel')])
def test_explicit_task_control_does_not_need_model(text,command):
    assert ConservativeRouter().classify(text) == {'type':'control','command':command}


@pytest.mark.parametrize('text', ['投食负十克','投食-10克','投食10到20克','投食20.5克','介绍一下投食20克的功能','投食20克之后抬头','原地会议投影是什么','投食20克安全吗','投食20克需要几秒'])
def test_invalid_or_discussed_feed_never_becomes_feed(text):
    assert ConservativeRouter().classify(text)['type']=='chat'


def test_explicit_quantities_and_stationary_scope():
    router=ConservativeRouter()
    assert router.classify('请投食三十克')['args']=={'grams':30}
    assert router.classify('原地做十二个深蹲')['parameters']=={'exercise':'squat','count':12}
    assert router.classify('请在这里开始会议投影')['name']=='meeting_stationary'
    assert router.classify('请开始会议投影')['name']=='meeting'


def test_camera_choice_remains_explicit():
    with pytest.raises(PolicyError,match='not_unique'):
        validate_current_turn('用摄像头拍一张，前后都可以', {'type':'action','kind':'camera.capture','args':{'camera':'front'}})


def test_remembering_or_quoting_commands_does_not_execute_them():
    router=ConservativeRouter()
    assert router.classify('记住：投食20克') == {'type':'memory_save','text':'投食20克'}
    assert router.classify('记住：不要开灯')['type']=='memory_save'
    for text in ['请记住投食20克','请复述投食20克','把投食20克翻译成英语']:
        assert router.classify(text)['type']=='chat'
