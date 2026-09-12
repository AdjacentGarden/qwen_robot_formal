"""Deterministic slots for explicit commands; unknown wording still reaches the LLM."""
import re
from .contracts import Action, PolicyError, StationaryPolicy
from .intent_codec import chinese_integer
from .intent_policy import quantity_matches

CLARIFY = {'type':'chat','reply':'请明确一个操作、准确数量和目标设备。'}


def quantity(text, unit):
    tokens = re.findall(r'([0-9０-９零一二两三四五六七八九十百]+)\s*' + unit, text)
    if len(tokens) != 1:
        return None
    try:
        number = chinese_integer(tokens[0])
        return number if quantity_matches(text, number, unit) else None
    except PolicyError:
        return None


def explicit_command(text):
    stationary = bool(re.search(r'原地|当前位置|这里|(?:不|不用|不要|别)导航', text))
    if re.search(r'会议|开会|投影', text) and re.search(r'启动|开始|开会|投影', text):
        # A discussion, negative or compound request is rejected before this parser.
        if re.search(r'灯|拍照|投食|喂|电影|天气|提醒', text):
            return None
        return {'type':'workflow','name':'meeting_stationary' if stationary else 'meeting','parameters':{}}
    exercises = [kind for phrase,kind in [('俯卧撑','push_up'),('深蹲','squat'),('引体向上','pull_up')] if phrase in text]
    if exercises and stationary:
        count = quantity(text, '个')
        if len(exercises) != 1 or count is None or not 1 <= count <= 200:
            return dict(CLARIFY)
        return {'type':'workflow','name':'exercise_stationary','parameters':{'exercise':exercises[0],'count':count}}
    if re.search(r'投食|投粮|喂|出粮', text):
        grams = quantity(text, '克')
        if grams is None:
            return dict(CLARIFY)
        action = Action('feeder.feed', {'grams': grams})
        try:
            StationaryPolicy().validate(action)
        except PolicyError:
            return dict(CLARIFY)
        return {'type':'action','kind':action.kind,'args':action.args}
    if re.fullmatch(r'(?:请你|请|帮我|麻烦|机器人|让)?(?:头部|头)?(?:保持|恢复)?(?:水平|回正|摆正|归中)(?:一下|吧)?', text):
        return {'type':'action','kind':'head.move','args':{'pose':'level'}}
    return None
