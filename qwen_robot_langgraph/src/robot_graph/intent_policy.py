"""Ground model-proposed physical actions in this turn; constraints are not opposite commands."""
import re
from .contracts import CAPABILITIES,PolicyError


STATIONARY_PATTERN = r'原地|这里|当前位置|当前地点|原处|就地|(?:不|不用|不要|别)导航|(?:不|不用|不要)移动|别动底轮|^(?:请你|请|麻烦你)?地(?=投影|播放)'


def requests_stationary(text):
    return bool(re.search(STATIONARY_PATTERN, re.sub(r'\s+', '', text), re.I))


def request_needs_clarification(text):
    normalized = re.sub(r'\s+', '', text)
    if re.search(r'会议|开会|投影|俯卧撑|深蹲|引体向上', normalized):
        normalized = re.sub(r'(?:不要|不用|别|不)(?:导航|移动|动底轮)', '', normalized)
    if re.search(r'如果|假如|假设|要是|刚才|之前|上次|昨天|不要|别|不用|不许|不能|暂时不|先不|暂不|不做|不想|记住|复述|翻译', normalized):
        return True
    if re.search(r'然后|接着|之后|随后|同时|并且|顺便|以及|还要|再(?:把|帮|抬|开|关|拍)', normalized):
        return True
    if re.search(r'(可以|能够|能不能|能否|能).*吗[？?。]*$', normalized) and not re.search(r'帮我|请(?:你)?(?:抬|低|开|关|拍|投|启动|暂停|继续)', normalized):
        return True
    if re.search(r'为什么|如何|怎么|怎样|多久|多长时间|意思|介绍|解释|记录|是否|有没有|讨论|是什么|有什么|功能|原理|了解|会不会|好不好|需要.*(?:秒|分钟|时间)', normalized) and not re.search(r'(设备|机器人|系统).*状态', normalized):
        return True
    if re.search(r'吗[？?。]*$', normalized) and not re.search(r'帮我|请(?:你)?(?:抬|低|开|关|拍|投|启动|暂停|继续)', normalized):
        return True
    return False


def validate_current_turn(text,intent):
    kind=intent.get('type')
    if kind not in {'workflow','action','control'}:return
    if kind=='action' and not CAPABILITIES.get(intent.get('kind',''),None):return
    if kind=='action' and not CAPABILITIES[intent['kind']].physical:
        cues = {'system.time': r'时间|几点|报时|报个时', 'system.status': r'状态|运行情况'}
        if intent['kind'] in cues and not re.search(cues[intent['kind']], text):
            raise PolicyError('read_only_request_not_grounded')
        direct_time = intent['kind']=='system.time' and re.search(r'几点|(?:现在|当前|北京时间).{0,5}时间', re.sub(r'\s+','',text))
        if request_needs_clarification(text) and not direct_time:
            raise PolicyError('needs_clarification:request_context')
        return
    if request_needs_clarification(text):
        raise PolicyError('needs_clarification:request_context')
    normalized=re.sub(r'\s+','',text)
    if kind=='workflow' and intent.get('name') in {'meeting_stationary','exercise_stationary'}:
        normalized=re.sub(r'(?:不要|不用|别|不)(?:导航|移动|动底轮)','',normalized)
    if kind=='control':
        cues={'pause':'暂停|停一下|等一下|先停','resume':'继续|恢复','cancel':'取消|结束|停止|停下|关闭|关掉|关了'}
        if not re.search(cues.get(intent.get('command'),r'(?!)'),normalized):
            raise PolicyError('control_not_grounded')
    elif kind=='workflow':
        name=intent.get('name','')
        if 'stationary' in name and not requests_stationary(text):
            raise PolicyError('stationary_workflow_not_explicit')
        if name.startswith('meeting') and not re.search('会议|开会|投影',text):
            raise PolicyError('meeting_not_grounded')
        if name == 'exercise_stationary':
            parameters = intent.get('parameters', {})
            cue = {'push_up': '俯卧撑', 'squat': '深蹲', 'pull_up': '引体向上'}.get(parameters.get('exercise'))
            if not cue or cue not in text or not quantity_matches(text, parameters.get('count'), '个'):
                raise PolicyError('exercise_parameters_not_grounded')
    else:
        action=intent['kind'];args=intent.get('args',{})
        if action=='head.move':
            if '度' in text or re.search(r'\d',text):
                raise PolicyError('numeric_head_angle_not_supported')
            cues={'up':r'抬(?:一?下|起)?头|抬起|头.*(?:抬|向上)|向上.*头','down':r'低(?:一?下)?头|头.*(?:低下|向下)|向下.*头','level':r'回正|水平|摆正|归中'}
            if not re.search(cues.get(args.get('pose'),r'(?!)'),normalized):raise PolicyError('head_pose_not_grounded')
        if action=='light.set':
            cue=r'开|亮' if args.get('enabled') is True else r'关|熄|灭'
            if '灯' not in text or not re.search(cue,text):raise PolicyError('light_action_not_grounded')
        if action=='camera.capture':
            if re.search(r'前后|后前|前.*或.*后|后.*或.*前',text):
                raise PolicyError('camera_target_not_unique')
            cue='前' if args.get('camera')=='front' else '后'
            if cue not in text or not re.search('拍|照',text):raise PolicyError('camera_not_grounded')

        if action=='feeder.feed':
            if not re.search('喂|投粮|投食|出粮',text) or not quantity_matches(text,args.get('grams'),'克'):
                raise PolicyError('feeding_amount_not_grounded')


def quantity_matches(text, expected, unit):
    if re.search(r'[负\-－−+.＋]\s*[\d零一二三四五六七八九十百两]', text):
        return False
    if re.search(r'[\d零一二三四五六七八九十百两]\s*(?:到|至|~|～)\s*[\d零一二三四五六七八九十百两]', text):
        return False
    values=[]
    for match in re.finditer(r'(?<![\d.+\-零一二三四五六七八九十百两])(\d+|[零一二三四五六七八九十百两]+)\s*'+unit,text):
        word=match.group(1)
        if word.isdigit(): value=int(word)
        elif word in {'一百','二百','两百'}: value=100 if word=='一百' else 200
        else:
            digits={'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
            if '十' in word:
                a,b=word.split('十',1)
                if a not in digits and a!='' or b not in digits and b!='':continue
                value=(digits.get(a,1))*10+digits.get(b,0)
            else:value=digits.get(word,-1)
        values.append(value)
    return len(values)==1 and values[0]==expected
