import pytest
from robot_graph.contracts import PolicyError
from robot_graph.intent_policy import validate_current_turn

@pytest.mark.parametrize('text,intent',[
 ('暂时不要开灯',{'type':'action','kind':'light.set','args':{'enabled':False}}),
 ('把头调到一百九十度',{'type':'action','kind':'head.move','args':{'pose':'up'}}),
 ('如果我要开会，你会怎么做',{'type':'workflow','name':'meeting'}),
 ('你刚才抬头了吗',{'type':'action','kind':'head.move','args':{'pose':'up'}}),
 ('你可以直接在原地投影吗',{'type':'workflow','name':'meeting_stationary'}),
 ('开灯然后抬头',{'type':'action','kind':'head.move','args':{'pose':'up'}}),
 ('开个会',{'type':'workflow','name':'meeting_stationary'}),
 ('我只是试试',{'type':'control','command':'cancel'}),
 ('抬头',{'type':'action','kind':'feeder.feed','args':{'grams':10}}),
])
def test_bad_proposals_are_rejected(text,intent):
    with pytest.raises(PolicyError):validate_current_turn(text,intent)

@pytest.mark.parametrize('text,intent',[
 ('请把头抬起来',{'type':'action','kind':'head.move','args':{'pose':'up'}}),
 ('帮我打开灯',{'type':'action','kind':'light.set','args':{'enabled':True}}),
 ('在原地开始会议投影，不要导航',{'type':'workflow','name':'meeting_stationary'}),
])
def test_grounded_request(text,intent):validate_current_turn(text,intent)


def test_direct_time_question_is_read_only_and_grounded():
    validate_current_turn('现在是什么时间',{'type':'action','kind':'system.time','args':{}})


def test_how_to_query_time_does_not_become_an_action():
    with pytest.raises(PolicyError):
        validate_current_turn('如何查询时间',{'type':'action','kind':'system.time','args':{}})
