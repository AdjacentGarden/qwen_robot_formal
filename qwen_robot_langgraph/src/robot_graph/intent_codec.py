"""Bounded JSON decoding; no extraction of actions from free-form prose."""
import json
import re
import unicodedata

from .contracts import PolicyError
from .intent_policy import quantity_matches


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError('duplicate_intent_key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise PolicyError('non_finite_json_number')


def chinese_integer(value):
    value = unicodedata.normalize('NFKC', value).strip().lower()
    if value.endswith(('克', '个')):
        value = value[:-1]
    if re.fullmatch(r'[0-9]{1,3}', value):
        return int(value)
    english = {'zero':0,'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10,
               'twenty':20,'thirty':30,'forty':40,'fifty':50,'sixty':60,'seventy':70,'eighty':80,'ninety':90,'hundred':100}
    if value in english:
        return english[value]
    digits = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    if value in digits:
        return digits[value]
    if re.fullmatch(r'[一二两三四五六七八九]?十[一二三四五六七八九]?', value):
        a, b = value.split('十')
        return digits.get(a, 1) * 10 + digits.get(b, 0)
    if value in {'一百', '两百', '二百'}:
        return 100 if value == '一百' else 200
    raise PolicyError('invalid_integer_spelling')


def grounded_quantity(text, unit):
    words=re.findall(r'([0-9０-９零一二两三四五六七八九十百]+)\s*'+unit,text)
    if len(words)!=1:
        return None
    try:number=chinese_integer(words[0])
    except PolicyError:return None
    return number if quantity_matches(text,number,unit) else None


def decode_intent(content, text):
    if not isinstance(content, str) or len(content) > 8192:
        raise PolicyError('invalid_model_content')
    content = content.strip()
    if content.startswith('```json') and content.endswith('```'):
        content = content[7:-3].strip()
    # Repair only bare numeric spellings in the two advertised numeric fields.
    # Escaped keys inside a JSON string are not matched. All other syntax stays strict.
    content = re.sub(
        r'(?<!\\)"(grams|count)"\s*:\s*(?=[,}])',
        lambda m: json.dumps(m[1])+':null',content)
    content = re.sub(
        r'(?<!\\)"(grams|count)"\s*:\s*([0-9０-９零一二两三四五六七八九十百A-Za-z]+(?:克|个)?)(?=\s*[,}])',
        lambda m: json.dumps(m[1]) + ':' + ('null' if m[2].lower() in {'none','null'} else json.dumps(m[2], ensure_ascii=False)), content)
    value = json.loads(content, object_pairs_hook=_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise PolicyError('invalid_model_intent')
    # Unambiguous aliases observed on the RKNN server; never infer a physical action.
    if value.get('type') in {'system.time', 'system.status'} and set(value) <= {'type', 'args'} and value.get('args', {}) == {}:
        value = {'type': 'action', 'kind': value['type'], 'args': {}}
    target = None
    if value.get('type') == 'action' and value.get('kind') == 'feeder.feed':
        target = (value.get('args'), 'grams', '克')
    elif value.get('type') == 'workflow' and value.get('name') == 'exercise_stationary':
        target = (value.get('parameters'), 'count', '个')
    if target:
        args, key, unit = target
        if isinstance(args, dict):
            if isinstance(args.get(key), str):
                number = chinese_integer(args[key])
                if not quantity_matches(text, number, unit):
                    raise PolicyError('normalized_quantity_not_grounded')
                args[key] = number
            elif args.get(key) is None:
                number=grounded_quantity(text,unit)
                if number is not None:args[key]=number
    return value
