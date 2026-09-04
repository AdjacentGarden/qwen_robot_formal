from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

from intent_policy import is_retrospective_query

from scenario_engine import (
    SCENARIO_TOOL_NAME,
    ScenarioCatalog,
    ScenarioError,
    ScenarioExecutor,
    _contains_term,
    _phonetic_text,
)


DEFAULT_SPEC_DIR = Path(__file__).with_name("robot_skills") / "config" / "skill_specs"
DEFAULT_ROBOT_PROJECT = Path("/home/test/qwen_robot_project")
DEFAULT_SCENARIO_CATALOG = Path(__file__).with_name("scenarios") / "procedure_catalog.json"
DEFAULT_SKILL_HOST_SOCKET = Path(__file__).with_name("runtime") / "skill_host.sock"
DEFAULT_ENABLED_SKILLS: tuple[str, ...] = ()  # Empty means every enabled local spec.
SEQUENCE_TOOL_NAME = "run_skill_sequence"
MAX_SEQUENCE_TASKS = 6


def _intent_text(value: str) -> str:
    """Normalize ASR text for conservative intent-evidence checks."""

    return re.sub(r"[\s，。！？!?、；;：:,.]+", "", str(value or "").lower())


def _normalize_realtime_information_arguments(
    arguments: dict[str, Any],
    user_text: str,
) -> dict[str, Any]:
    """Ground read-only live-information arguments in the current utterance.

    Qwen Realtime sometimes selects the correct action but omits optional
    ``query`` and ``location`` fields.  Without this normalization, a request
    for company traffic silently falls back to the default home coordinate.
    The transcript is authoritative here and only maps two explicitly saved
    place aliases; it never invents an arbitrary location.
    """

    normalized = dict(arguments or {})
    transcript = str(user_text or "").strip()
    if transcript and not str(normalized.get("query") or "").strip():
        normalized["query"] = transcript
    if str(normalized.get("action") or "").strip() == "commute_recommendation":
        inferred_origin, inferred_destination = _commute_places_from_text(transcript)
        if inferred_origin and not str(normalized.get("origin") or "").strip():
            normalized["origin"] = inferred_origin
        if inferred_destination and not str(normalized.get("destination") or "").strip():
            normalized["destination"] = inferred_destination
        normalized.setdefault("origin", "家庭")
        return normalized
    if str(normalized.get("location") or "").strip():
        return normalized
    text = _intent_text(transcript)
    if re.search(r"公司|单位|上班地点|工作地点|公司地址", text):
        normalized["location"] = "公司"
    elif re.search(r"家里|家庭|住宅|住处|家附近|家周边|家中", text):
        normalized["location"] = "家庭"
    return normalized


_INDOOR_ROUTE_DESTINATIONS = {"原点", "客厅", "客厅白墙", "白墙", "书房", "餐厅"}


def _clean_commute_place(value: str) -> str:
    text = re.sub(r"[\s，,。？?!！：:；;、]", "", str(value or ""))
    text = re.sub(r"^(?:现在|今天|我现在|我想|我要|准备|打算)+", "", text)
    text = re.sub(r"(?:上班|办事|出差|旅游|游玩|开会|那边|那里|那儿)$", "", text)
    return text[:32]


def _commute_places_from_text(user_text: str) -> tuple[str | None, str | None]:
    raw = str(user_text or "")
    stop = r"(?=上班|办事|出差|旅游|游玩|开会|的话|时|怎么|如何|路线|大概|需要|多久|有多远|多远|坐什么|哪种|你(?:是|会|觉得)|我(?:该|应该)|推荐|开车|自驾|驾车|坐地铁|乘地铁|坐公交|乘公交|，|,|。|？|\?|$)"
    origin: str | None = None
    destination: str | None = None
    from_match = re.search(rf"从\s*(?P<origin>[一-龥A-Za-z0-9·_-]{{1,24}}?)(?:去|到|前往)\s*(?P<destination>[一-龥A-Za-z0-9·_-]{{1,32}}?){stop}", raw)
    if from_match:
        origin = _clean_commute_place(from_match.group("origin"))
        destination = _clean_commute_place(from_match.group("destination"))
    else:
        destination_match = re.search(
            rf"(?:去|到|前往)\s*(?P<destination>[一-龥A-Za-z0-9·_-]{{1,32}}?){stop}",
            raw,
        )
        if destination_match:
            destination = _clean_commute_place(destination_match.group("destination"))
    if destination in _INDOOR_ROUTE_DESTINATIONS:
        return None, None
    return origin or None, destination or None


def _explicit_commute_recommendation_task(user_text: str) -> dict[str, Any] | None:
    text = _intent_text(user_text)
    origin, destination = _commute_places_from_text(user_text)
    if not destination:
        return None
    has_driving = bool(re.search(r"开车|自驾|驾车|自己开车", text))
    has_transit = bool(re.search(r"地铁|公交|公共交通", text))
    comparison = bool(re.search(r"还是|或者|哪个|哪种|推荐|怎么去|更合适|更快", text))
    general_advice = bool(re.search(
        r"怎么去|如何去|怎么走|怎么过去|如何到达|路线(?:怎么走|如何|建议)?|"
        r"交通建议|出行建议|哪种(?:交通)?方式|怎么坐车|坐什么车|"
        r"开车(?:要|需要)?多久|需要多久|大概多久|有多远",
        text,
    ))
    if not ((has_driving and has_transit and comparison) or general_advice):
        return None
    return {
        "name": "realtime_information",
        "arguments": {
            "action": "commute_recommendation",
            "query": str(user_text or ""),
            "origin": origin or "家庭",
            "destination": destination,
        },
    }


def _intent_evidence(text: str, pattern: str, terms: Sequence[str] = ()) -> bool:
    """Combine exact syntax evidence with the existing pinyin-aware matcher."""

    return bool(
        re.search(pattern, text)
        or any(_contains_term(text, term) for term in terms if str(term).strip())
    )


def _contains_any_term(text: str, terms: Sequence[str]) -> bool:
    return any(_contains_term(text, term) for term in terms if str(term).strip())


def _scenario_clarification(
    requested: str,
    user_text: str,
    reason: str = "",
) -> tuple[str, str | None]:
    """Return one concrete, non-executing clarification question.

    This is intentionally narrower than scenario matching.  It may explain a
    plausible interpretation, but it never upgrades that interpretation into
    permission to operate hardware.  The second value is a scenario that a
    later short affirmation may safely confirm.
    """

    text = _intent_text(user_text)
    if requested == "homecoming_welcome" and re.search(
        r"hello|哈喽|哈啰|哈罗|嗨|你好|理想|李想|李晓|同学",
        text,
        re.IGNORECASE,
    ):
        return "我听见你在跟我打招呼，但称呼没听清。你是在叫我“理想同学”吗？", requested
    if reason == "ambiguous_movie_polarity":
        return (
            "我没听清你是想看电影，还是不看电影改做运动。请直接说“看电影”或“不看电影”。",
            None,
        )
    if requested == "push_up_companion" and re.search(r"俯卧|辅导|运动|锻炼|健身|练", text):
        return "我听到的内容有点像“俯卧撑”。你是想让我陪你做俯卧撑吗？", requested
    if requested == "pull_up_companion" and re.search(r"引体|单杠|运动|锻炼", text):
        return "你是想让我陪你做引体向上吗？", requested
    if requested == "squat_companion" and re.search(r"深蹲|蹲|运动|锻炼", text):
        return "你是想让我陪你做深蹲吗？", requested
    if requested in {"find_pet", "find_pet_at", "find_pet_here", "find_and_feed_doudou"}:
        pet_named = bool(re.search(r"豆豆|豆儿|狗|宠物", text))
        feeding = bool(re.search(r"喂|吃饭|该吃|饿|狗粮|开饭", text))
        searching = bool(re.search(r"找|看看|瞧瞧|在哪|位置", text))
        if feeding and (pet_named or re.search(r"它|他", text)):
            return "你是想让我去找豆豆，找到后再给它喂食吗？", "find_and_feed_doudou"
        if pet_named and searching:
            if requested == "find_and_feed_doudou":
                return "我听清了要找豆豆，但没听清是否还要喂食。你要我只找豆豆吗？", "find_pet"
            return "你是想让我去找豆豆吗？", "find_pet"
        if re.search(r"找|在哪|喂|吃饭|饿", text):
            return "我没听清你说的是不是豆豆。你是想让我去找豆豆吗？", "find_pet"
    if requested in {"meeting_projection", "meeting_projection_stop"} and re.search(
        r"会议|开会|投影|投屏|ppt|幻灯", text
    ):
        if re.search(r"关|停|结束|退出|不投", text):
            return "你是想结束当前的会议投影吗？", "meeting_projection_stop"
        return "你是想让我开始会议投影吗？", "meeting_projection"
    if requested == "meeting_projection" and re.search(r"(?:我要|我想|现在要)?开", text):
        return "你是想开会并让我开始会议投影吗？", requested
    if requested.startswith("movie_projection") and re.search(r"电影|影片|播放|暂停|继续|结束", text):
        if requested == "movie_projection_pause":
            return "你是想暂停正在播放的电影吗？", requested
        if requested == "movie_projection_resume":
            return "你是想继续播放刚才的电影吗？", requested
        if requested == "movie_projection_stop":
            return "你是想结束电影播放并关闭投影吗？", requested
        return "你是想让我播放电影吗？", "movie_projection"
    if requested == "rest_lighting" and re.search(r"休息|累|困|灯|光", text):
        return "你是想让我调整灯光，好让你休息一会儿吗？", requested
    if reason == "not_requested":
        return "我没听清具体想做什么。你可以只重说动作，比如做运动、找豆豆或开始会议投影。", None
    return "我听出了大概方向，但还缺一个关键信息。请只重说你想执行的动作。", None


_CONTEXT_AFFIRMATIONS = {
    "是", "是的", "对", "对的", "对呀", "对啊", "没错", "嗯", "嗯嗯",
    "好", "好的", "好啊", "可以", "行", "开始吧", "那就开始吧",
}
_CONTEXT_REJECTIONS = {"不是", "不对", "不要", "不用", "算了", "取消", "先不要"}


def _contextual_affirmative(answer: str, scenario: str) -> bool:
    """Accept natural short confirmations only after one concrete question."""

    if answer in _CONTEXT_AFFIRMATIONS:
        return True
    if re.search(r"不要|不用|不想|算了|取消|不是|不对", answer):
        return False
    if scenario == "movie_projection":
        return bool(
            re.fullmatch(
                r"(?:好|好的|可以|行)?(?:啊|呀)?(?:那就)?(?:看|放|播放)(?:电影|影片)?(?:吧|呀|啊)?",
                answer,
            )
        )
    if scenario in {"push_up_companion", "pull_up_companion", "squat_companion"}:
        return bool(
            re.fullmatch(
                r"(?:好|好的|可以|行)(?:啊|呀)?(?:那就)?(?:陪我)?"
                r"(?:做(?:运动|俯卧撑|引体向上|深蹲)?|运动|锻炼|开始)?"
                r"(?:吧|呀|啊)?(?:好|好的)?",
                answer,
            )
        )
    return False


def _action_explicitly_negated(text: str, terms: Sequence[str]) -> bool:
    """Check negation inside the action's own spoken clause."""

    clauses = re.split(r"[，。！？!?、；;]|然后|接着|之后|随后|但是|不过|但(?=[^是])", str(text or ""))
    for clause in clauses:
        compact = _intent_text(clause)
        # Judge the earliest action mention in a clause.  Looking at every
        # nested term made ``识别一下我是谁`` match ``别一下`` immediately
        # before the later term ``我是谁`` and falsely turned it into a
        # negation.  The earliest term is ``识别`` and has no negative prefix.
        positions = [
            (position, _intent_text(term))
            for term in terms
            if (position := compact.find(_intent_text(term))) >= 0
        ]
        if not positions:
            continue
        position, _target = min(positions, key=lambda item: (item[0], -len(item[1])))
        prefix = compact[:position]
        if re.search(r"(?:不要|别|不用|无需|不需要|不想|不许|禁止)(?:再|去|给我|帮我)?$", prefix):
            return True
        if re.search(r"(?:不要|(?<!识)别|不用|无需|不需要|不想|不许|禁止).{0,5}$", prefix):
            return True
    return False


def _term_position(text: str, terms: Sequence[str], fallback: int = 10**9) -> int:
    """Return a stable relative position for exact or pinyin-equivalent text."""

    compact = _intent_text(text)
    positions = [compact.find(_intent_text(term)) for term in terms]
    exact = [value for value in positions if value >= 0]
    if exact:
        return min(exact)
    phonetic = _phonetic_text(compact)
    phonetic_positions = [phonetic.find(_phonetic_text(term)) for term in terms]
    repaired = [value for value in phonetic_positions if value >= 0]
    return min(repaired) if repaired else fallback


def _term_positions(text: str, terms: Sequence[str]) -> list[int]:
    """Return every exact/phonetic position for a repeatable action."""

    compact = _intent_text(text)
    exact_tokens = sorted(
        {_intent_text(item) for item in terms if _intent_text(item)},
        key=len,
        reverse=True,
    )
    positions = {
        match.start()
        for match in re.finditer("|".join(re.escape(token) for token in exact_tokens), compact)
    } if exact_tokens else set()
    if positions:
        return sorted(positions)
    phonetic = _phonetic_text(compact)
    phonetic_tokens = sorted(
        {_phonetic_text(item) for item in terms if _phonetic_text(item)},
        key=len,
        reverse=True,
    )
    if phonetic_tokens:
        positions = {
            match.start()
            for match in re.finditer(
                "|".join(re.escape(token) for token in phonetic_tokens),
                phonetic,
            )
        }
    return sorted(positions)


POINT_SPEECH_TERMS: dict[str, tuple[str, ...]] = {
    # Short forms are accepted only in a sentence that already contains an
    # explicit navigation predicate.  This repairs common end-of-utterance ASR
    # truncation without turning an arbitrary mention of “书” or “原” into
    # permission to move the base.
    "study_projection": ("书房", "书放", "书房白墙", "去书", "到书", "苏北", "书北"),
    "origin": ("原点", "餐厅", "回原", "到原", "远点"),
    "white_wall": ("客厅白墙", "白墙", "客厅", "客墙", "克墙", "克强"),
}


def _navigation_predicate(text: str) -> bool:
    compact = _intent_text(text)
    # “回到水平/平视” is a head transition, not navigation.  Treating bare
    # “回到” as a movement predicate also lets pinyin similarity confuse
    # “上方” with “书房”, producing a dangerous phantom navigation child.
    if (
        re.search(r"回到(?:水平|平视|正前方|正常角度|原来角度)|恢复(?:水平|平视|正前方)", compact)
        and not re.search(r"导航|前往|去往|回到(?:原点|客厅|书房|白墙|餐厅)", compact)
    ):
        return False
    return bool(
        re.search(r"导航|前往|过去|去往|回到|回原点|\bgo\b", compact)
        or re.search(
            r"(?:^|先|然后|接着|随后|最后|再|，|,|；|;)"
            r"(?:请你|麻烦你|帮我)?(?:去|到|回)",
            compact,
        )
        or _contains_any_term(compact, ("导航到", "去客厅", "去书房", "回原点", "前往"))
    )


def _navigation_point_evidence(text: str, point: str) -> bool:
    if not _navigation_predicate(text):
        return False
    terms = POINT_SPEECH_TERMS.get(str(point), ())
    return _contains_any_term(_intent_text(text), terms)


def _explicit_navigation_task(user_text: str) -> dict[str, Any] | None:
    tasks = _explicit_navigation_tasks(user_text)
    return copy.deepcopy(tasks[0]) if tasks else None


def _sequence_clauses(user_text: str) -> list[str]:
    """Split explicit ordered speech without inventing missing actions."""

    # Preserve separators until after splitting.  The former implementation
    # called ``_intent_text`` first, which deleted commas and semicolons and
    # collapsed ``开灯，拍照，再关灯`` into one clause.  That lost repeated
    # actions and changed their order.
    text = str(user_text or "").lower()
    clauses = [
        _intent_text(item)
        for item in re.split(
            r"(?:然后|接着|随后|最后|再|并且|并|做完(?:以后|之后)?|完成(?:以后|之后)?|到达后|成功后|，|,|；|;)",
            text,
        )
    ]
    return [item for item in clauses if item]


def _point_in_clause(clause: str) -> tuple[int, str] | None:
    matched: list[tuple[int, str]] = []
    for point, terms in POINT_SPEECH_TERMS.items():
        position = _term_position(clause, terms, -1)
        if position >= 0:
            matched.append((position, point))
    return min(matched) if matched else None


def _explicit_navigation_tasks(user_text: str) -> list[dict[str, Any]]:
    """Return every explicitly ordered navigation destination.

    The historical helper returned only the first point in an utterance.  It
    was then used to canonicalize every model navigation child, so
    ``客厅 -> 书房`` became ``客厅 -> 客厅``.  Parsing one ordered clause at a
    time keeps the safety property (only allow spoken, registered points)
    while preserving repeated navigation commands.
    """

    text = _intent_text(user_text)
    if not _navigation_predicate(text):
        return []
    values: list[dict[str, Any]] = []
    previous_was_navigation = False
    for clause in _sequence_clauses(text):
        matched = _point_in_clause(clause)
        if matched is None:
            previous_was_navigation = False
            continue
        explicit = _navigation_predicate(clause)
        # “先去客厅，然后书房” is an ordinary ellipsis after an explicit
        # navigation clause.  Do not apply this rule after a non-navigation
        # clause such as “先介绍书房，然后去客厅”.
        implied = previous_was_navigation and bool(
            re.match(r"^(?:请你|麻烦你|帮我)?(?:到|去|回)?", clause)
        )
        if not explicit and not implied:
            previous_was_navigation = False
            continue
        _position, point = matched
        values.append({"name": "navigation_goto", "arguments": {"point": point}})
        previous_was_navigation = True
    if values:
        return values
    matched = _point_in_clause(text)
    if matched is None:
        return []
    return [{"name": "navigation_goto", "arguments": {"point": matched[1]}}]


def _explicit_head_tasks(user_text: str) -> list[dict[str, Any]]:
    """Extract multiple head actions in their spoken clause order."""

    actions: list[dict[str, Any]] = []
    terms = {
        "up": (
            "抬头", "把头抬起", "头抬起来", "抬一下头", "把视线抬高",
            "向上看", "看上方", "看高处", "把头升起", "头升起来", "升起头部",
        ),
        "down": (
            "低头", "把头低下", "头低下来", "向下看", "看地面",
            "看下方", "把头降下", "头降下来", "降下头部",
        ),
        "level": (
            "恢复平视", "恢复水平", "平视", "头回正", "回正头部",
            "把头放平", "头放平", "恢复到水平", "恢复到水平位置", "恢复水平位置",
            "回到水平", "回到水平位置", "回到平视", "回到正前方", "恢复正前方", "水平",
        ),
    }
    for clause in _sequence_clauses(user_text):
        angle_match = re.search(r"([零一二两三四五六七八九十百\d]+)(?:点[零一二两三四五六七八九\d]+)?度", clause)
        if angle_match:
            angle = _spoken_integer(angle_match.group(1))
            if angle is not None:
                actions.append({
                    "name": "head_control",
                    "arguments": {"action": "angle", "angle": angle},
                })
                continue
        found: list[tuple[int, str]] = []
        for action, action_terms in terms.items():
            if _action_explicitly_negated(clause, action_terms):
                continue
            position = _term_position(clause, action_terms, -1)
            if position >= 0:
                found.append((position, action))
        if found:
            _position, action = min(found)
            actions.append({"name": "head_control", "arguments": {"action": action}})
    return actions


def _explicit_light_task(user_text: str, catalog: ScenarioCatalog | None = None) -> dict[str, Any] | None:
    text = _intent_text(user_text)
    if catalog is not None and catalog._lighting_negated(text):
        return None
    lighting = bool(
        re.search(r"灯|照明|光线|太暗|太黑|看不清", text)
        or _contains_any_term(text, ("客厅灯", "客厅的灯", "打开灯", "关闭灯", "灯打开", "灯关掉"))
    )
    if not lighting:
        return None
    explicit_off = bool(
        re.search(r"关(?:掉|闭|上)?.{0,4}灯|灯.{0,4}关(?:掉|闭|上)?|熄灭", text)
        or _contains_any_term(text, ("关灯", "关闭灯", "把客厅灯关掉", "灯关掉"))
    )
    explicit_on = bool(
        re.search(r"开(?:启)?.{0,4}灯|打开.{0,4}灯|灯.{0,4}(?:打开|开启|亮)|太暗|太黑|看不清", text)
        or _contains_any_term(text, ("开灯", "打开灯", "把客厅灯打开", "灯打开"))
    )
    if explicit_off:
        action = "off"
    elif explicit_on:
        action = "on"
    else:
        return None
    return {"name": "light_control", "arguments": {"action": action, "room": "living_room"}}


def _spoken_integer(value: str) -> int | None:
    match = re.search(r"\d+", value)
    if match:
        return int(match.group())
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    match = re.search(r"[零一二两三四五六七八九十百]+", value)
    if not match:
        return None
    token = match.group()
    if token == "十":
        return 10
    if "百" in token:
        left, right = token.split("百", 1)
        total = digits.get(left, 1) * 100
        token = right
    else:
        total = 0
    if "十" in token:
        left, right = token.split("十", 1)
        total += digits.get(left, 1) * 10 + digits.get(right, 0)
    elif token:
        total += digits.get(token, 0)
    return total


def _explicit_feeder_task(user_text: str) -> dict[str, Any] | None:
    text = _intent_text(user_text)
    if not re.search(r"投食|喂(?:食|豆豆|狗)?|出粮|投食器|狗粮|(?:豆豆|小狗|宠物).{0,6}粮", text):
        return None
    if _action_explicitly_negated(
        str(user_text or ""),
        ("投食", "喂食", "喂豆豆", "喂狗", "出粮"),
    ):
        return None
    if re.search(r"查询|看看|状态|还有多少|在线|检查|确认|每份|多少克|怎么用", text):
        return {"name": "feeder_control", "arguments": {"action": "status"}}
    grams_match = re.search(r"([零一二两三四五六七八九十百\d]+)\s*克", text)
    arguments: dict[str, Any] = {"action": "feed"}
    if grams_match:
        grams = _spoken_integer(grams_match.group(1))
        if grams is not None:
            arguments["grams"] = grams
    else:
        portions_matches = list(re.finditer(r"([零一二两三四五六七八九十百\d]+)\s*(?:份|次)(?:粮)?", text))
        if portions_matches:
            # Prefer the final explicit amount.  In “一次投食一百份”, the
            # first “一次” describes occurrence count while “一百份” is the
            # actual feeder quantity and must reach the safety bound check.
            portions = _spoken_integer(portions_matches[-1].group(1))
            if portions is not None:
                arguments["portions"] = portions
    return {"name": "feeder_control", "arguments": arguments}


def _explicit_projector_task(user_text: str) -> dict[str, Any] | None:
    """Recover projector transport without expanding it into a start scene."""

    text = _intent_text(user_text)
    if not re.search(r"投影|投屏|ppt|幻灯|会议画面|会议内容|投影仪", text):
        return None
    if _projection_stop_requested(text):
        return None
    if re.search(r"暂停|停一下|先停", text):
        action = "meeting_pause"
    elif re.search(r"继续|恢复|接着(?:播放|投影|放)", text):
        action = "meeting_resume"
    elif re.search(r"状态|开着吗|关着吗|是否(?:正在|还在)", text):
        action = "status"
    elif re.search(
        r"(?:打开|开启|开一下|开开|开(?!始|会)).{0,4}(?:投影仪|投影|投屏)|"
        r"(?:投影仪|投影|投屏).{0,4}(?:打开|开启|开一下|开开)",
        text,
    ):
        # Bare projector power-on deliberately carries no meeting/movie
        # content. Content is selected only by a separate explicit scene.
        action = "on"
    else:
        return None
    return {"name": "projector_control", "arguments": {"action": action}}


def _explicit_media_task(user_text: str) -> dict[str, Any] | None:
    text = _intent_text(user_text)
    known_title = next((title for title in ("七里香", "晴天") if title in text), None)
    media_topic = bool(re.search(r"音乐|歌曲|听歌|放歌|视频|电影|短片|节目|娱乐视频", text) or known_title)
    if not media_topic:
        return None
    # Movie transport owns head/projector cleanup and therefore remains a
    # protected scenario.  Generic entertainment video and music controls
    # continue to use the atomic media player.
    if re.search(r"电影|影片", text) and re.search(r"暂停|停一下|继续|恢复|接着|停止|结束|关闭|关掉|不看", text):
        return None
    if re.search(r"暂停|停一下", text):
        action = "pause"
    elif re.search(r"继续|恢复|接着", text):
        action = "resume"
    elif re.search(r"停止|停掉|结束|关闭|关掉|不听|不看", text):
        action = "stop"
    elif re.search(r"下一首|换一首|换歌", text):
        action = "next"
    elif re.search(r"列表|有哪些|有什么", text):
        action = "list"
    elif re.search(r"状态|在播什么|播放到哪", text):
        action = "status"
    elif re.search(r"视频|短片|节目|娱乐视频", text):
        action = "play_video"
    else:
        action = "play_music"
    arguments: dict[str, Any] = {"action": action}
    if known_title and action == "play_music":
        arguments["title"] = known_title
    return {"name": "media_player", "arguments": arguments}


def _explicit_movement_task(user_text: str) -> dict[str, Any] | None:
    text = _intent_text(user_text)
    movement_actions = (
        ("move_forward", r"前进|往前(?:走|移动)?|向前(?:走|移动)?"),
        ("move_backward", r"后退|往后(?:走|移动)?|向后(?:走|移动)?|倒退"),
        ("move_left", r"左转|向左转|往左转"),
        ("move_right", r"右转|向右转|往右转"),
    )
    found = [
        (match.start(), name)
        for name, pattern in movement_actions
        if (match := re.search(pattern, text)) is not None
    ]
    if not found:
        return None
    arguments: dict[str, Any] = {}
    duration_match = re.search(r"([零一二两三四五六七八九十百\d]+)\s*秒", text)
    if duration_match:
        duration = _spoken_integer(duration_match.group(1))
        if duration is not None:
            arguments["duration"] = duration
    return {"name": min(found)[1], "arguments": arguments}


def _explicit_clause_atomic_tasks(user_text: str, catalog: ScenarioCatalog | None) -> list[dict[str, Any]]:
    """Recover repeatable atomic actions one ordered clause at a time."""

    values: list[dict[str, Any]] = []
    for clause in _sequence_clauses(user_text):
        local: list[dict[str, Any]] = []
        local.extend(_explicit_navigation_tasks(clause))
        local.extend(_explicit_head_tasks(clause))
        # “前后摄像头各拍一张” contains two explicit camera actions but no
        # ordinary sequence separator.  Preserve both in the spoken order
        # instead of letting a single direction keyword win.
        if re.search(r"(?:前后|前、后|前和后)(?:置)?(?:摄像头|摄|镜头).{0,8}(?:各|分别).{0,6}(?:拍|照)", clause):
            local.extend((
                {"name": "front_camera_capture", "arguments": {}},
                {"name": "back_camera_capture", "arguments": {}},
            ))
        for task in (
            _explicit_light_task(clause, catalog),
            _explicit_media_task(clause),
            _explicit_projector_task(clause),
            _explicit_feeder_task(clause),
            _explicit_movement_task(clause),
        ):
            if task is not None:
                local.append(task)
        indexed = list(enumerate(local))
        indexed.sort(key=lambda pair: (_task_position(pair[1], clause, 10**9 + pair[0]), pair[0]))
        values.extend(task for _index, task in indexed)
    return values


def _projection_stop_requested(user_text: str) -> bool:
    text = _intent_text(user_text)
    return bool(
        re.search(
            r"(?:关|关闭|关掉|结束|停止|停掉|收起|收起来).{0,8}(?:会议)?(?:投影|画面)|"
            r"(?:会议)?(?:投影|画面).{0,8}(?:关|关闭|关掉|结束|停止|停掉|收起|收起来)",
            text,
        )
        or _contains_any_term(
            text,
            ("关投影", "投影先停掉", "会议画面收起来", "把会议画面收起来"),
        )
    )


def _task_position(task: dict[str, Any], user_text: str, fallback: int) -> int:
    name = str(task.get("name") or "")
    arguments = dict(task.get("arguments") or {})
    action = str(arguments.get("action") or "").lower()
    if name == SCENARIO_TOOL_NAME:
        scenario = str(arguments.get("scenario") or "")
        terms = {
            "meeting_projection_stop": (
                "关闭会议投影", "关掉会议投影", "结束会议投影", "停止会议投影",
                "关闭投影", "关掉投影", "关投影", "结束投影", "停止投影",
                "停掉投影", "投影先停掉", "收起投影", "投影收起来",
                "会议画面收起来", "把会议画面收起来",
            ),
            "meeting_projection": ("会议投影", "投影会议内容", "开始投影"),
            "movie_projection": ("播放电影", "看电影", "放电影", "电影投影"),
            "movie_projection_pause": ("暂停电影", "电影暂停"),
            "movie_projection_resume": ("继续播放电影", "恢复电影", "接着看电影"),
            "movie_projection_stop": ("结束电影", "关闭电影", "停止播放电影"),
            "find_pet_at": ("找一下", "找豆豆", "寻找豆豆"),
            "find_pet_here": ("找一下", "找豆豆", "寻找豆豆"),
            "find_pet": ("找一下", "找豆豆", "寻找豆豆"),
        }.get(scenario, (scenario,))
        return _term_position(user_text, terms, fallback)
    if name == "navigation_goto":
        point_terms = {
            "origin": ("回原点", "原点", "餐厅"),
            "study_projection": ("去书房", "书房"),
            "white_wall": ("客厅白墙", "去客厅", "客厅", "白墙"),
            "living_room": ("去客厅", "客厅"),
        }.get(str(arguments.get("point") or ""), ("导航", "前往"))
        return _term_position(user_text, point_terms, fallback)
    if name == "light_control":
        light_terms = {
            "on": ("打开灯", "开启灯", "开灯", "灯打开", "照亮"),
            "off": (
                "关闭灯", "关掉灯", "关灯", "灯关掉", "熄灭",
                "把灯关", "灯关了", "关了灯", "把客厅灯关",
            ),
        }.get(action, ("客厅灯", "灯", "照明"))
        return _term_position(user_text, light_terms, fallback)
    if name == "media_player":
        return _term_position(user_text, ("音乐", "歌曲", "视频", "电影", "播放", "暂停"), fallback)
    if name == "projector_control":
        return _term_position(user_text, ("投影仪", "会议画面", "投影", "投屏", "暂停", "继续"), fallback)
    if name.startswith("reminder_"):
        return _term_position(user_text, ("提醒", "闹钟"), fallback)
    if name == "realtime_information":
        terms = {
            "location": (
                "当前位置", "机器人位置", "现在在哪", "机器人在哪", "你在哪",
                "所在位置", "看看机器人在哪",
            ),
            "indoor_location": (
                "当前位置", "机器人位置", "现在在哪", "机器人在哪", "你在哪",
                "所在位置", "客厅", "书房", "餐厅",
            ),
            "external_location": (
                "GPS", "经纬度", "外部位置", "地理位置", "哪个城市", "哪个街道",
            ),
            "current_time": ("现在几点", "几点", "时间", "日期"),
            "weather": ("天气", "下雨", "气温"),
        }.get(action, ("查询",))
        return _term_position(user_text, terms, fallback)
    if name in {"pet_tracking", "person_tracking"}:
        return _term_position(user_text, ("停止跟踪", "停止跟随", "跟踪", "跟随"), fallback)
    if "camera" in name:
        terms = (
            ("录像", "录视频", "录一段", "录五秒")
            if name.endswith("record")
            else ("拍照", "拍张", "拍一张", "拍一下", "照片", "保存一张图片")
        )
        return _term_position(user_text, terms, fallback)
    if name == "face_recognition":
        return _term_position(user_text, ("我是谁", "识别", "看看我"), fallback)
    if name == "feeder_control":
        return _term_position(user_text, ("投食", "喂", "出粮", "狗粮", "份粮", "给豆豆"), fallback)
    if name == "head_control":
        terms = {
            "up": ("抬头", "向上看", "看上方", "看高处", "把头升起", "头升起来"),
            "down": ("低头", "向下看", "看地面", "看下方", "把头降下", "头降下来"),
            "level": (
                "恢复平视", "恢复水平", "平视", "头回正", "回正头部",
                "回到水平", "回到平视", "回到正前方", "水平",
            ),
        }.get(action, ("抬头", "低头", "平视"))
        return _term_position(user_text, terms, fallback)
    return fallback


def _deduplicate_tasks(tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    previous_key: tuple[str, str] | None = None
    for task in tasks:
        normalized = {
            "name": str(task.get("name") or "").strip(),
            "arguments": dict(task.get("arguments") or {}),
        }
        key = (normalized["name"], json.dumps(normalized["arguments"], ensure_ascii=False, sort_keys=True))
        # Collapse an immediately repeated model call, but preserve an
        # intentional return to an earlier state: up -> level -> down -> level
        # must keep the final level action.
        if key == previous_key:
            continue
        values.append(normalized)
        previous_key = key
    return values


def _explicit_continue_requested(user_text: str) -> bool:
    compact = "".join(str(user_text or "").split())
    return (
        "继续" in compact
        and any(marker in compact for marker in ("不管", "不论", "无论", "即使", "哪怕"))
        and any(
            marker in compact
            for marker in ("成功", "失败", "完成", "没完成", "没到", "未到", "到达", "结果")
        )
    )


def _repair_sequence_tasks(
    tasks: Sequence[dict[str, Any]],
    user_text: str,
    catalog: ScenarioCatalog | None,
) -> list[dict[str, Any]]:
    """Repair representation errors while preserving only spoken actions."""

    repaired: list[dict[str, Any]] = []
    explicit_navigations = _explicit_navigation_tasks(user_text)
    explicit_navigation = explicit_navigations[0] if explicit_navigations else None
    navigation_count = sum(
        1 for task in tasks if str(task.get("name") or "").strip() == "navigation_goto"
    )
    navigation_index = 0
    explicit_light = _explicit_light_task(user_text, catalog)
    text = _intent_text(user_text)
    pet_stop = bool(
        re.search(r"(?:停止|结束|别再|不要再|不用再).{0,5}(?:跟踪|跟随|追踪).{0,5}(?:豆豆|狗|宠物)", text)
        or _contains_any_term(text, ("停止跟踪豆豆", "停止跟随豆豆", "别再跟着狗"))
    )

    for raw in tasks:
        task = {"name": str(raw.get("name") or "").strip(), "arguments": dict(raw.get("arguments") or {})}
        if task["name"] == "navigation_goto" and explicit_navigations:
            # ASR wording can leak into the model argument (for example
            # point="苏北").  Canonicalize from the user's transcript before
            # validation and never pass an arbitrary model point through.
            if navigation_index < len(explicit_navigations):
                task["arguments"] = dict(explicit_navigations[navigation_index]["arguments"])
            elif navigation_count == 1:
                task["arguments"] = dict(explicit_navigations[0]["arguments"])
            navigation_index += 1
        if task["name"] == SCENARIO_TOOL_NAME and catalog is not None:
            requested = catalog.normalize_scenario_name(str(task["arguments"].get("scenario") or ""), user_text)
            task["arguments"]["scenario"] = requested
            inferred = catalog.infer_arguments(requested, user_text) if requested in catalog.procedures else {}
            task["arguments"] = {**task["arguments"], **inferred}
            if requested == "living_room_light_service":
                if catalog._lighting_negated(user_text):
                    if explicit_navigation is not None:
                        repaired.append(explicit_navigation)
                    continue
                if catalog.living_light_requires_atomic_sequence(user_text) or (explicit_light or {}).get("arguments", {}).get("action") == "off":
                    if explicit_navigation is not None:
                        repaired.append(explicit_navigation)
                    if explicit_light is not None:
                        repaired.append(explicit_light)
                    continue
            if requested in {"find_pet", "find_pet_at", "find_pet_here"} and pet_stop:
                repaired.append({"name": "pet_tracking", "arguments": {"action": "stop"}})
                continue
        repaired.append(task)

    # The model may emit only one child for a compact but explicit compound
    # expression (for example “前后摄像头各拍一张”).  Add only actions that the
    # conservative transcript parser can prove were requested; no defaults or
    # associative guesses are introduced here.
    explicit_atomic = _explicit_clause_atomic_tasks(user_text, catalog)
    if len(explicit_atomic) >= 2:
        def task_identity(task: dict[str, Any]) -> tuple[str, str]:
            name = str(task.get("name") or "")
            arguments = dict(task.get("arguments") or {})
            action = str(arguments.get("action") or "")
            if name == "navigation_goto":
                detail = str(arguments.get("point") or "")
            elif name == "head_control":
                detail = f"{action}:{arguments.get('angle', '')}"
            elif name == "feeder_control":
                detail = f"{action}:{arguments.get('grams', '')}:{arguments.get('portions', '')}"
            elif name in {"media_player", "projector_control"}:
                detail = f"{action}:{arguments.get('title', '')}"
            else:
                # Room/default metadata does not make “开灯” a second action.
                detail = action
            return name, detail

        present = {task_identity(task) for task in repaired}
        for task in explicit_atomic:
            key = task_identity(task)
            if key not in present:
                repaired.append(copy.deepcopy(task))
                present.add(key)

    # A protected meeting scene already owns its navigation stage.  Remove a
    # duplicate model-generated navigation to the same point, but never remove
    # navigation to a genuinely different destination.
    meeting_points = {
        str(task["arguments"].get("point") or "study_projection")
        for task in repaired
        if task["name"] == SCENARIO_TOOL_NAME
        and task["arguments"].get("scenario") == "meeting_projection"
        and not task["arguments"].get("stay_put")
    }
    if meeting_points:
        repaired = [
            task
            for task in repaired
            if not (
                task["name"] == "navigation_goto"
                and str(task["arguments"].get("point") or "") in meeting_points
            )
        ]

    if re.search(r"先|然后|再|最后|之后|以后|到达后|成功后|如果|即使|不管|不论|无论", text):
        head_terms = {
            "up": ("抬头", "向上看", "看上方", "看高处", "把头升起", "头升起来"),
            "down": ("低头", "向下看", "看地面", "看下方", "把头降下", "头降下来"),
            "level": (
                "恢复平视", "恢复水平", "平视", "头回正", "回正头部",
                "回到水平", "回到平视", "回到正前方", "水平",
            ),
        }
        head_seen: dict[str, int] = {}
        positioned: list[tuple[int, int, dict[str, Any]]] = []
        for index, task in enumerate(repaired):
            position = _task_position(task, user_text, 10**9 + index)
            if task["name"] == "head_control":
                action = str(task["arguments"].get("action") or "")
                occurrences = _term_positions(user_text, head_terms.get(action, ()))
                occurrence = head_seen.get(action, 0)
                head_seen[action] = occurrence + 1
                if occurrence < len(occurrences):
                    position = occurrences[occurrence]
            positioned.append((position, index, task))
        positioned.sort(key=lambda item: (item[0], item[1]))
        repaired = [task for _position, _index, task in positioned]
    spoken_counts: dict[tuple[str, str], int] = {}
    for spoken_task in _explicit_clause_atomic_tasks(user_text, catalog):
        key = (
            str(spoken_task.get("name") or ""),
            json.dumps(spoken_task.get("arguments") or {}, ensure_ascii=False, sort_keys=True),
        )
        spoken_counts[key] = spoken_counts.get(key, 0) + 1
    values: list[dict[str, Any]] = []
    emitted: dict[tuple[str, str], int] = {}
    for task in repaired:
        key = (
            task["name"],
            json.dumps(task["arguments"], ensure_ascii=False, sort_keys=True),
        )
        # A task may appear repeatedly only as many times as the transcript
        # explicitly contains it.  This removes model duplicates while keeping
        # intentional non-adjacent returns such as level -> down -> level.
        allowed = max(1, spoken_counts.get(key, 0))
        if emitted.get(key, 0) >= allowed:
            continue
        emitted[key] = emitted.get(key, 0) + 1
        values.append(task)
    return values


def _recover_explicit_sequence_tasks(user_text: str, catalog: ScenarioCatalog | None) -> list[dict[str, Any]]:
    """Conservative fallback for a malformed model sequence call.

    Recovery is attempted only after Qwen already selected the sequence tool,
    and only unambiguous positive actions found in the transcript are kept.
    """

    text = _intent_text(user_text)
    candidates: list[dict[str, Any]] = []
    commute_task = _explicit_commute_recommendation_task(user_text)
    if commute_task is not None:
        candidates.append(commute_task)
    matched_scenarios: list[str] = []
    if catalog is not None:
        for clause in _sequence_clauses(user_text):
            projector_transport = _explicit_projector_task(clause)
            matched = None if projector_transport is not None else (
                "meeting_projection_stop"
                if _projection_stop_requested(clause)
                and "meeting_projection_stop" in catalog.procedures
                else catalog.match(clause)
            )
            # “给豆豆喂十克” is a direct feeder command.  The protected
            # find-and-feed scene is permitted only when the same clause also
            # explicitly asks to find/search for the pet.
            if (
                matched == "find_and_feed_doudou"
                and not re.search(r"找|找到|寻找|搜索|看看.{0,4}(?:豆豆|狗|宠物)", clause)
            ):
                matched = None
            if matched and matched != "living_room_light_service":
                scenario_arguments = {"scenario": matched}
                scenario_arguments.update(catalog.infer_arguments(matched, clause))
                candidates.append({"name": SCENARIO_TOOL_NAME, "arguments": scenario_arguments})
                matched_scenarios.append(matched)
        if not matched_scenarios:
            matched = (
                "meeting_projection_stop"
                if _projection_stop_requested(text)
                and "meeting_projection_stop" in catalog.procedures
                else catalog.match(text)
            )
            if (
                matched == "find_and_feed_doudou"
                and not re.search(r"找|找到|寻找|搜索|看看.{0,4}(?:豆豆|狗|宠物)", text)
            ):
                matched = None
            if matched and matched != "living_room_light_service":
                scenario_arguments = {"scenario": matched}
                scenario_arguments.update(catalog.infer_arguments(matched, text))
                candidates.append({"name": SCENARIO_TOOL_NAME, "arguments": scenario_arguments})
                matched_scenarios.append(matched)
        if not matched_scenarios and catalog._has_topic_evidence("meeting_projection_stop", text):
            candidates.append({"name": SCENARIO_TOOL_NAME, "arguments": {"scenario": "meeting_projection_stop"}})
    # Keep punctuation while splitting clauses.  Normalizing first removes
    # commas, so “不要去找豆豆，只投食十克” becomes one negative clause and
    # incorrectly suppresses the explicit feeder command after the comma.
    clause_tasks = _explicit_clause_atomic_tasks(user_text, catalog)
    # Preserve the established single-scene behavior when navigation wording
    # and the protected scene occur in the same clause (for example “去客厅做
    # 俯卧撑”).  A genuinely separated “先去……再做……” keeps both tasks.
    clauses = _sequence_clauses(user_text)
    if catalog is not None and len(matched_scenarios) == 1 and len(clauses) == 1:
        procedure = catalog.procedures.get(matched_scenarios[0], {})
        if any(
            str(step.get("skill") or "") == "navigation_goto"
            for step in procedure.get("steps", [])
            if isinstance(step, dict)
        ):
            clause_tasks = [task for task in clause_tasks if task["name"] != "navigation_goto"]
    candidates.extend(clause_tasks)
    # Route-advice questions are read-only but time-sensitive. Recover them
    # locally when the realtime model answers conversationally or selects a
    # nearby/traffic action without the required route endpoints.
    commute_task = _explicit_commute_recommendation_task(text)
    if commute_task is not None and not any(
        str(task.get("name") or "") == "realtime_information"
        and str((task.get("arguments") or {}).get("action") or "") == "commute_recommendation"
        for task in candidates
    ):
        candidates.append(commute_task)
    weather_or_outdoor_clothing = (
        r"天气|下雨|降雨|气温|温度|最高温|最低温|冷不冷|热不热|"
        r"(?:出门|外出).{0,12}(?:穿什么|穿哪|怎么穿|衣服|穿搭|带伞)|"
        r"(?:穿什么|穿哪|怎么穿|衣服|穿搭).{0,12}(?:出门|外出|适合|合适)"
    )
    if re.search(weather_or_outdoor_clothing, text):
        candidates.append({"name": "realtime_information", "arguments": {"action": "weather"}})
    if re.search(r"几点|时间|日期|年月日|星期|几月几日|几号", text):
        candidates.append({"name": "realtime_information", "arguments": {"action": "current_time"}})
    # Explicit read-only queries should be refreshed on every turn.  Without
    # this conservative fallback, the realtime model can reuse an earlier
    # answer from conversation memory and skip the local tool entirely.
    # Keep the evidence narrow so a destination request can never be turned
    # into a list query (and therefore can never mask navigation).
    if (
        re.search(r"(?:导航点|保存(?:的)?(?:地点|位置)|可以去(?:哪些|什么|哪几个)(?:地点|位置)?|能去(?:哪些|什么|哪几个)(?:地点|位置)?|(?:机器人|你).{0,6}有哪些(?:导航)?(?:地点|位置|点位))", text)
        and not re.search(r"导航到|前往|带我去|到.+去|回原点|去(?:原点|客厅|书房|白墙)", text)
    ):
        candidates.append({"name": "navigation_list", "arguments": {}})
    if re.search(
        r"(?:查|查询|看看|告诉我).{0,5}(?:当前位置|当前定位|所在位置|位置)|"
        r"(?:机器人|你|本机).{0,6}(?:在哪|哪里|位置|所在)",
        text,
    ):
        external_location = bool(re.search(
            r"GPS|GNSS|卫星定位|经纬度|坐标|外部位置|地理位置|哪个(?:国家|省|城市|区县|街道)|"
            r"哪(?:个|座)?城市|哪条街|所在(?:国家|省份|城市|区县|街道)",
            text,
            re.IGNORECASE,
        ))
        action = "external_location" if external_location else "indoor_location"
        candidates.append({"name": "realtime_information", "arguments": {"action": action}})
    traffic_requested = bool(re.search(r"路况|交通|堵车|拥堵|堵不堵", text))
    if not traffic_requested and re.search(r"附近|周边|周围|就近|最近的(?:医院|餐厅|咖啡店|公园|商店)", text):
        candidates.append({"name": "realtime_information", "arguments": {"action": "nearby"}})
    if traffic_requested:
        candidates.append({"name": "realtime_information", "arguments": {"action": "traffic"}})
    if re.search(r"提醒|闹钟", text):
        reminder_arguments: dict[str, Any] = {}
        relative = re.search(
            r"提醒我(?P<trigger>[零一二两三四五六七八九十百\d]+(?:秒钟?|分钟?|小时|天)后)"
            r"(?P<content>[^然后再最后，。！？!?]{1,30})",
            text,
        )
        absolute = re.search(
            r"提醒我(?P<trigger>(?:今天|明天|后天)?(?:上午|中午|下午|晚上)?"
            r"[零一二两三四五六七八九十百\d]+点(?:半|[零一二两三四五六七八九十百\d]+分)?)"
            r"(?P<content>[^然后再最后，。！？!?]{1,30})",
            text,
        )
        reminder = relative or absolute
        if reminder:
            reminder_arguments["trigger_condition"] = reminder.group("trigger")
            reminder_arguments["content"] = reminder.group("content").strip("的事一下")
        if reminder_arguments.get("content") and reminder_arguments.get("trigger_condition"):
            candidates.append({"name": "reminder_schedule", "arguments": reminder_arguments})
        elif re.search(r"查|查询|看看|列一下|列表|念|读|都有|安排了|哪些|什么|多少|有没有", text):
            candidates.append({"name": "reminder_query", "arguments": {}})
    pet_stop = bool(re.search(r"(?:停止|结束|别再|不要再|不用再).{0,5}(?:跟踪|跟随|追踪).{0,5}(?:豆豆|狗|宠物)", text))
    person_stop = bool(re.search(r"(?:停止|结束|别再|不要再|不用再).{0,5}(?:跟踪|跟随|追踪).{0,5}(?:人|他|她|面前)", text))
    if pet_stop:
        candidates.append({"name": "pet_tracking", "arguments": {"action": "stop"}})
    if person_stop:
        candidates.append({"name": "person_tracking", "arguments": {"action": "stop"}})
    if re.search(r"拍照|拍张|拍一张|拍一下|照片|照相|合影|保存一张图片", text):
        both_cameras = bool(
            re.search(r"(?:前后|前、后|前和后)(?:置)?(?:摄像头|摄|镜头).{0,8}(?:各|分别).{0,6}(?:拍|照)", text)
        )
        if not both_cameras:
            prefix = "back_" if re.search(r"后置|后摄|后面|后方|背后", text) else "front_" if re.search(r"前置|前摄|前面|前方|面前", text) else ""
            candidates.append({"name": f"{prefix}camera_capture", "arguments": {}})
    if re.search(r"录像|录.{0,12}视频|拍视频|录制", text):
        prefix = "back_" if re.search(r"后置|后摄|后面", text) else "front_" if re.search(r"前置|前摄|前面", text) else ""
        candidates.append({"name": f"{prefix}camera_record", "arguments": {}})
    if re.search(
        r"我是谁|认得我|认识我|认一下|认出|知道我是谁|"
        r"识别.{0,4}(?:我|人脸|身份)|确认.{0,8}(?:身份|注册用户|是谁)|"
        r"镜头前.{0,5}(?:谁|人)",
        text,
    ):
        candidates.append({"name": "face_recognition", "arguments": {}})
    candidates = _repair_sequence_tasks(candidates, user_text, catalog)
    if len(candidates) >= 2:
        return candidates
    if (
        len(candidates) == 1
        and candidates[0]["name"] == SCENARIO_TOOL_NAME
        and catalog is not None
    ):
        scenario = str(candidates[0]["arguments"].get("scenario") or "")
        supported, _reason = catalog.model_scenario_supported(
            scenario,
            user_text,
            allow_additional_intents=True,
        )
        if supported:
            return candidates
    if len(candidates) == 1 and candidates[0]["name"] != SCENARIO_TOOL_NAME:
        supported, _reason = _atomic_intent_supported(
            candidates[0]["name"],
            dict(candidates[0].get("arguments") or {}),
            user_text,
        )
        if supported:
            return candidates
    return []


def _atomic_intent_supported(
    name: str,
    arguments: dict[str, Any],
    user_text: str,
    *,
    in_sequence: bool = False,
    prior_assistant_text: str = "",
) -> tuple[bool, str]:
    """Reject associative tool guesses without becoming a second NLU model.

    Qwen remains responsible for broad semantic understanding. This guard is
    intentionally limited to high-impact, unambiguous evidence: a room name
    alone cannot authorize lighting, a company question cannot authorize a
    robot-location lookup, and a sequence child that moves the base must have
    an actual movement predicate and a matching destination in the utterance.
    """

    if is_retrospective_query(user_text):
        return False, "retrospective_query_must_use_memory"
    text = _intent_text(user_text)
    context = _intent_text(prior_assistant_text)
    if not text:
        return True, "empty_text_not_checked"

    # A capability question mentions a function but does not authorize a
    # hardware action.  Identity and read-only query tools are handled below
    # because “你知道我是谁吗” genuinely requests a live recognition result.
    read_only_or_identity = {
        "face_recognition", "navigation_list", "reminder_query", "realtime_information",
    }
    capability_question = bool(
        re.search(r"^(?:你)?(?:会不会|会(?!议)|能不能|能否|能|有没有|支持不支持|具备).{0,12}(?:吗|么|功能|$)", text)
        and not re.search(r"帮我|给我|替我|请你|提醒我|现在|马上|开始|执行|打开|关闭|播放|拍一|录一|看一下|看看|识别一下|导航|前往|抬头|低头", text)
    )
    if capability_question and name not in read_only_or_identity:
        return False, "capability_question_not_execution"

    evidence_text = _intent_text(str(arguments.get("evidence") or ""))
    structured_evidence = bool(evidence_text and evidence_text in text)

    negatable_actions = {
        "navigation_goto": ("导航", "前往", "去书", "去客厅", "回原点", "到白墙"),
        "move_forward": ("前进", "往前", "向前"),
        "move_backward": ("后退", "往后", "向后", "倒退"),
        "move_left": ("左转", "向左转", "往左转"),
        "move_right": ("右转", "向右转", "往右转"),
        "head_control": (
            "抬头", "把头抬起", "头抬起来", "抬一下头", "低头", "把头低下", "平视", "回正",
            "向上看", "向下看", "视线往上", "视线往下", "镜头往上", "镜头往下",
            "看正前方", "恢复正常角度", "把头放平", "头放平",
        ),
        "camera_capture": ("拍照", "拍张", "照相", "合影"),
        "front_camera_capture": ("拍照", "拍张", "照相", "合影"),
        "back_camera_capture": ("拍照", "拍张", "照相", "合影"),
        "camera_record": ("录像", "录视频", "拍视频", "录制"),
        "front_camera_record": ("录像", "录视频", "拍视频", "录制"),
        "back_camera_record": ("录像", "录视频", "拍视频", "录制"),
        "face_recognition": ("识别", "看看我", "我是谁"),
        "face_registration": ("注册", "登记", "录入", "记住"),
        "feeder_control": ("投食", "喂食", "出粮"),
    }
    if name in negatable_actions and _action_explicitly_negated(user_text, negatable_actions[name]):
        return False, "explicitly_negated_action"

    general_evidence = {
        "face_recognition": r"我是谁|认得我|认识我|认一下|认出我|看出我|知道我是谁|面前.{0,6}(?:谁|人)|镜头前.{0,6}(?:谁|人)|识别.{0,4}(?:我|人脸|身份)|确认.{0,8}(?:身份|注册用户|是谁)|看看.{0,6}(?:我|谁|认不认得)",
        "face_registration": r"注册|登记|录入|添加|保存|记住.{0,3}(?:人脸|脸|身份)",
        "camera_capture": r"拍照|拍张|拍一张|拍一下|照片|照相|合影|保存一张图片",
        "front_camera_capture": r"拍照|拍张|拍一张|拍一下|照片|照相|合影|保存一张图片",
        "back_camera_capture": r"拍照|拍张|拍一张|拍一下|照片|照相|合影|保存一张图片",
        "camera_record": r"录像|录.{0,12}视频|拍视频|录制",
        "front_camera_record": r"录像|录.{0,12}视频|拍视频|录制",
        "back_camera_record": r"录像|录.{0,12}视频|拍视频|录制",
        "fan_control": r"风扇|吹风|风机",
        "feeder_control": r"投食|喂|出粮|狗粮|(?:豆豆|小狗|宠物).{0,8}粮|[零一二两三四五六七八九十百\d]+份粮|吃饭|开饭|投食器",
        "head_control": r"抬头|抬.{0,3}头|头.{0,3}抬|低头|低.{0,3}头|头.{0,3}低|平视|放平|回正|头部|脑袋|视线|镜头|向上看|向下看|看上方|看地面|看下方|升起|降下|回到.{0,3}(?:水平|平视|正前方)|往高处看|看正前方|恢复.{0,4}水平|恢复正常角度|[零一二两三四五六七八九十百\d]+度",
        "move_forward": r"前进|往前|向前",
        "move_backward": r"后退|往后|向后|倒退",
        "move_left": r"左转|向左|往左",
        "move_right": r"右转|向右|往右",
        "navigation_goto": r"导航|前往|过去|去往|到|去|回",
        "person_tracking": r"跟踪|跟随|追踪|找人|寻找.{0,8}(?:人|用户)|找.{0,8}(?:人|用户)|跟着",
        "pet_tracking": r"豆豆|小狗|狗狗|宠物|找狗|跟踪狗|跟着狗",
        "projector_control": r"投影|投屏|ppt|幻灯|会议画面|会议内容|墙上.{0,4}内容|大屏|正在放的内容",
        "reminder_schedule": r"提醒|闹钟|到点叫我|记得叫我",
        "reminder_query": r"提醒|闹钟|待办",
        "reminder_cancel": r"提醒|闹钟|待办",
        "media_player": r"音乐|歌曲|听歌|视频|电影|短片|节目|媒体|正在播|暂停|继续播放|恢复播放|下一首|换一首|播放器|七里香|晴天",
    }
    general_terms = {
        "face_recognition": ("人脸识别", "身份识别", "我是谁", "认得我", "认出我", "看出我"),
        "face_registration": ("登记人脸", "注册人脸", "录入人脸"),
        "camera_capture": ("拍照", "拍张照片", "照相"),
        "front_camera_capture": ("拍照", "前摄拍照"),
        "back_camera_capture": ("拍照", "后摄拍照"),
        "camera_record": ("录像", "录视频"),
        "front_camera_record": ("录像", "前摄录像"),
        "back_camera_record": ("录像", "后摄录像"),
        "fan_control": ("风扇", "风机"),
        "feeder_control": ("投食", "喂食", "狗粮", "给豆豆粮", "豆豆两份粮", "投食器"),
        "head_control": ("抬头", "把头抬起来", "低头", "平视", "把头放平", "回正", "视线往上", "视线往下", "看正前方"),
        "move_forward": ("前进", "往前", "向前"),
        "move_backward": ("后退", "往后", "倒退"),
        "move_left": ("左转", "向左"),
        "move_right": ("右转", "向右"),
        "navigation_goto": ("导航", "前往", "回原点"),
        "person_tracking": ("跟踪人", "跟随人", "追踪人", "寻找用户", "找用户"),
        "pet_tracking": ("豆豆", "小狗", "宠物", "找狗"),
        "projector_control": ("投影", "投屏", "幻灯", "会议内容", "墙上内容", "大屏内容"),
        "reminder_schedule": ("设置提醒", "提醒我", "闹钟"),
        "reminder_query": ("查询提醒", "查看提醒"),
        "reminder_cancel": ("取消提醒", "删除提醒"),
        "media_player": ("播放音乐", "听歌", "播放视频", "媒体播放状态", "暂停播放", "继续播放", "正在播的内容", "播放七里香", "播放晴天"),
    }
    evidence = general_evidence.get(name)
    current_evidence = bool(
        structured_evidence
        or (evidence and _intent_evidence(text, evidence, general_terms.get(name, ())))
    )
    action = str(arguments.get("action") or "").strip().lower()
    context_groundable = bool(
        (name == "projector_control" and action in {"off", "stop", "meeting_pause", "meeting_resume"})
        or (name == "media_player" and action in {"pause", "resume", "next", "stop", "status"})
        or name in {"reminder_cancel", "reminder_query"}
        or (name in {"person_tracking", "pet_tracking"} and action == "stop")
    )
    contextual_reference = bool(
        re.search(r"这个|那个|刚才|前面|正在|当前|它|先|继续|不用再|别再", text)
    )
    context_evidence = bool(
        evidence
        and context_groundable
        and contextual_reference
        and context
        and _intent_evidence(context, evidence, general_terms.get(name, ()))
    )
    # If Qwen has already selected a tool, the immediately preceding robot
    # sentence may provide an omitted object (“这个先停了”).  Context can
    # ground the object, but every action-specific polarity/destination check
    # below still uses only the current user turn.
    if evidence and not current_evidence and not context_evidence:
        return False, f"missing_{name}_evidence"

    if name == "light_control":
        if not (
            re.search(r"灯|照明|光线|亮(?:一点|起来|些)?|暗|太黑|看不清", text)
            or _contains_any_term(text, ("客厅灯", "客厅的灯", "打开灯", "关闭灯", "灯打开", "灯关掉"))
        ):
            return False, "missing_lighting_evidence"
        action = str(arguments.get("action") or "").strip().lower()
        negated_open = bool(
            re.search(r"不想(?:要)?开灯|(?:不要|别|不用|无需|不需要)(?:帮我|给我)?(?:打开|开启|开)?(?:客厅)?(?:的)?灯|不开灯", text)
        )
        explicit_off = bool(re.search(r"关(?:掉|闭|上)?(?:客厅)?(?:的)?灯|把(?:客厅)?(?:的)?灯关", text))
        if negated_open and (action == "on" or (action == "off" and not explicit_off)):
            return False, "explicitly_negated_lighting"
        if action == "on" and not _intent_evidence(text, r"开|打开|开启|亮|暗|太黑|看不清", ("打开灯", "开灯", "灯打开", "太暗")):
            return False, "light_on_not_requested"
        if action == "off" and not _intent_evidence(text, r"关|关闭|关掉|熄灭", ("关灯", "关闭灯")):
            return False, "light_off_not_requested"
        if action in {"status", "check"} and not re.search(r"状态|开着|关着|亮着|有没有开|在线|检查|确认", text):
            return False, "light_status_not_requested"

    if name == "realtime_information":
        action = str(arguments.get("action") or "").strip().lower()
        if action in {"location", "indoor_location", "external_location"}:
            robot_subject = bool(re.search(r"你|机器人|本机", text))
            non_robot_subject = bool(re.search(r"手机|电话|用户|我的位置|我在哪|我在哪里|豆豆|小狗|宠物|\b(?:他|她|它)\b", text))
            if non_robot_subject and not robot_subject:
                return False, "location_subject_is_not_robot"
        evidence = {
            "location": r"(?:查|查询|看看|告诉我).{0,6}(?:当前位置|当前定位|所在位置|位置|本机定位)|(?:你|机器人|本机).{0,8}(?:在哪|哪里|位置|什么地方|所在)|(?:在哪|哪里).{0,4}(?:你|机器人)|定位(?:在哪|信息|状态)",
            "indoor_location": r"(?:查|查询|看看|告诉我).{0,6}(?:当前位置|当前定位|所在位置|位置|本机定位)|(?:你|机器人|本机).{0,8}(?:在哪|哪里|位置|什么地方|所在)|(?:客厅|书房|餐厅).{0,6}(?:哪个|哪里)|(?:在哪|哪里).{0,4}(?:你|机器人)|(?:家里|室内).{0,8}(?:哪个|什么|哪一)(?:房间|区域)|(?:哪个|什么|哪一)(?:房间|室内区域)",
            "external_location": r"gps|gnss|卫星定位|经纬度|坐标|外部位置|地理位置|哪个(?:国家|省|城市|区县|街道)|哪(?:个|座)?城市|哪条街|所在(?:国家|省份|城市|区县|街道)",
            "current_time": r"几点|时间|日期|年月日|星期[几几一二三四五六日天]?|几月几日|几号|现在是",
            "weather": (
                r"天气|下雨|降雨|温度|气温|最高温|最低温|晴天|阴天|刮风|台风|冷不冷|热不热|"
                r"(?:出门|外出).{0,12}(?:穿什么|穿哪|怎么穿|衣服|穿搭|带伞)|"
                r"(?:穿什么|穿哪|怎么穿|衣服|穿搭).{0,12}(?:出门|外出|适合|合适)"
            ),
            "nearby": r"附近|周边|周围|就近|最近的",
            "traffic": r"路况|交通|堵车|拥堵|堵不堵",
            "commute_recommendation": r"(?:去|到|前往).{1,32}(?:开车|自驾|驾车|地铁|公交|怎么去|如何去|怎么走|怎么过去|如何到达|路线|怎么坐车|坐什么车|需要多久|大概多久|多远|交通建议|出行建议)",
        }.get(action)
        if evidence and not re.search(evidence, text):
            return False, f"missing_realtime_{action}_evidence"
        if action == "indoor_location" and re.search(
            r"gps|gnss|卫星定位|经纬度|坐标|外部位置|地理位置|哪个(?:国家|省|城市|区县|街道)|哪(?:个|座)?城市|哪条街",
            text,
            re.IGNORECASE,
        ):
            return False, "external_location_must_not_use_indoor_location"

    if name == "navigation_list":
        if not re.search(
            r"导航点|保存(?:的)?(?:地点|位置)|可以去(?:哪些|什么|哪几个)|能去(?:哪些|什么|哪几个)|(?:机器人|你).{0,6}有哪些(?:导航)?(?:地点|位置|点位)",
            text,
        ):
            return False, "navigation_list_not_requested"
        if re.search(r"导航到|前往|带我去|回原点|去(?:原点|客厅|书房|白墙)", text):
            return False, "navigation_list_conflicts_with_destination"

    if name == "navigation_goto":
        point = str(arguments.get("point") or "").strip()
        if not point and (arguments.get("x") is None or arguments.get("y") is None):
            return False, "navigation_destination_missing"
        canonical_point = {
            "living_room": "white_wall",
            "living_room_entry_a": "white_wall",
        }.get(point, point)
        if (
            canonical_point not in POINT_SPEECH_TERMS
            and arguments.get("x") is None
            and arguments.get("y") is None
        ):
            return False, "navigation_destination_unknown"
        if canonical_point in POINT_SPEECH_TERMS and not _navigation_point_evidence(text, canonical_point):
            return False, "navigation_destination_conflict"

    if name in {"camera_capture", "camera_record", "front_camera_capture", "front_camera_record", "back_camera_capture", "back_camera_record"}:
        both_cameras = bool(re.search(r"(?:前后|前、后|前和后)(?:置)?(?:摄像头|摄|镜头)", text))
        requested_back = bool(both_cameras or re.search(r"后置|后摄|后面|后方|背后", text) or _contains_term(text, "后摄"))
        requested_front = bool(both_cameras or re.search(r"前置|前摄|前面|前方|面前", text) or _contains_term(text, "前摄"))
        selected_back = name.startswith("back_") or str(arguments.get("camera_name") or arguments.get("camera") or "").lower() == "back"
        selected_front = name.startswith("front_") or str(arguments.get("camera_name") or arguments.get("camera") or "").lower() == "front"
        if selected_back and not requested_back:
            return False, "back_camera_not_requested"
        if selected_front and requested_back and not requested_front:
            return False, "camera_direction_conflict"

    if name == "head_control":
        action = str(arguments.get("action") or "").strip().lower()
        action_evidence = {
            "up": r"抬头|向上看|往上看|看上方|看高处|升起|头.{0,3}升|往高处看|视线.{0,4}(?:上|高)|镜头.{0,4}(?:上|高)|头.{0,3}抬",
            "down": r"低头|向下看|往下看|看地面|看下方|降下|头.{0,3}降|视线.{0,4}下|镜头.{0,4}下|头.{0,3}低",
            "level": r"平视|水平|放平|回正|摆正|看正前方|回到.{0,3}(?:水平|平视|正前方)|恢复.{0,3}水平|恢复正常角度|头.{0,3}正",
            "angle": r"角度|[零一二两三四五六七八九十百\d]+(?:点[零一二两三四五六七八九\d]+)?度",
        }.get(action)
        head_terms = {
            "up": ("抬头", "向上看", "看上方", "看高处", "头升起来", "视线往上", "往高处看"),
            "down": ("低头", "向下看", "看地面", "看下方", "头降下来", "视线往下"),
            "level": ("平视", "放平", "回正", "摆正", "看正前方", "回到水平", "回到平视", "恢复水平", "恢复到水平", "恢复正常角度"),
            "angle": ("角度",),
        }.get(action, ())
        if action_evidence and not structured_evidence and not _intent_evidence(text, action_evidence, head_terms):
            return False, "head_action_conflict"

    if name == "projector_control":
        action = str(arguments.get("action") or "").strip().lower()
        if action not in {
            "on", "internal_on", "off", "stop", "meeting_pause",
            "meeting_resume", "meeting_presentation_on", "status",
        }:
            return False, "projector_action_not_model_exposed"
        if action in {"off", "stop"} and re.search(
            r"(?:不要|别|先别|暂时别).{0,8}(?:投影|投屏|ppt|幻灯|画面|内容).{0,6}(?:关|停|结束|收起|撤掉)|"
            r"(?:投影|投屏|ppt|幻灯|画面|内容).{0,6}(?:不要|别|先别|暂时别).{0,4}(?:关|停|结束|收起|撤掉)",
            text,
        ):
            return False, "projector_stop_explicitly_negated"
        if action in {"off", "stop"} and not re.search(
            r"关|停|结束|收起|撤掉|到这里|到这|不投|不用继续|不用放|别播|取消",
            text,
        ):
            return False, "projector_stop_not_requested"
        if action in {"meeting_pause"} and not _intent_evidence(
            text, r"暂停|停一下|停在", ("暂停投影", "暂停播放")
        ):
            return False, "projector_pause_not_requested"
        if action in {"meeting_resume"} and not _intent_evidence(
            text, r"继续|恢复", ("继续播放", "恢复播放")
        ):
            return False, "projector_resume_not_requested"
        if action in {"on", "internal_on"} and not re.search(
            r"(?:打开|开启|开一下|开开|开(?!始|会)).{0,5}(?:投影仪|投影|投屏)|"
            r"(?:投影仪|投影|投屏).{0,5}(?:打开|开启|开一下|开开)",
            text,
        ):
            return False, "projector_on_not_requested"
        if action == "meeting_presentation_on":
            # Starting meeting content is a protected parameterized scene;
            # an atomic tool call must never bypass its navigation/head rules.
            return False, "meeting_start_requires_scene"
        if action == "status" and not re.search(
            r"状态|开着吗|关着吗|是否(?:正在|还在)|有没有(?:开|播放)", text
        ):
            return False, "projector_status_not_requested"

    if name in {"person_tracking", "pet_tracking"}:
        action = str(arguments.get("action") or "").strip().lower()
        if action == "stop" and not _intent_evidence(
            text,
            r"停止|结束|别跟|不要跟|不用跟|别再跟|不要再跟|不用再跟",
            ("停止跟踪", "停止跟随", "结束追踪"),
        ):
            return False, "tracking_stop_not_requested"
        if action in {"track", "find_and_track", "find_route_and_track"} and not re.search(r"跟踪|跟随|追踪|跟着", text):
            return False, "tracking_start_not_requested"

    if name == "media_player":
        action = str(arguments.get("action") or "").strip().lower()
        action_evidence = {
            "play_music": r"音乐|歌曲|听歌|唱首歌|放歌|七里香|晴天",
            "play_video": r"视频|电影|短片|节目",
            "play_movie": r"电影|影片|投影",
            "pause": r"暂停|停一下",
            "resume": r"继续|恢复",
            "next": r"下一首|换一首|换歌",
            "stop": r"停止|停掉|停了|结束|关闭|关掉|到这里|到这|先这样|不用继续|不听|不看",
            "list": r"有什么|列表|哪些|可以播放",
            "status": r"状态|在播什么|播放到哪",
        }.get(action)
        media_terms = {
            "play_music": ("播放音乐", "听歌", "放歌"),
            "play_video": ("播放视频", "看视频", "看电影"),
            "play_movie": ("播放电影", "电影投影", "看电影"),
            "pause": ("暂停播放", "停一下"),
            "resume": ("继续播放", "恢复播放"),
            "next": ("下一首", "换歌"),
            "stop": ("停止播放", "结束播放", "就到这里", "不用继续播放"),
            "list": ("播放列表",),
            "status": ("播放状态",),
        }.get(action, ())
        if action_evidence and not _intent_evidence(text, action_evidence, media_terms):
            return False, "media_action_conflict"

    if name == "reminder_cancel":
        if re.search(r"(?:不要|别|先别|暂时别).{0,6}(?:删|取消|作废|撤掉|清除|移除)", text):
            return False, "reminder_cancel_explicitly_negated"
        if not re.search(r"删|取消|不要|不用留|作废|撤掉|清除|移除", text):
            return False, "reminder_cancel_not_requested"
    if name == "reminder_query" and not re.search(r"查|查询|看看|列一下|列出|列表|念|读|都有|安排了|哪些|什么|多少|有没有", text):
        return False, "reminder_query_not_requested"
    if name == "reminder_schedule" and not re.search(r"提醒|闹钟|到点叫我|记得叫我", text):
        return False, "reminder_schedule_not_requested"

    if name == "environment_perception":
        if not re.search(r"看看|看一下|看一眼|瞧瞧|识别|观察|检查|摄像头|画面|镜头|周围|面前|环境|能看见", text):
            return False, "missing_visual_inspection_evidence"

    if name in {"move_forward", "move_backward", "move_left", "move_right"}:
        direction_terms = {
            "move_forward": r"前进|往前|向前",
            "move_backward": r"后退|往后|向后",
            "move_left": r"左转|向左|往左",
            "move_right": r"右转|向右|往右",
        }
        if not re.search(direction_terms[name], text):
            return False, "missing_motion_evidence"

    return True, "explicit_or_unrestricted"

POINT_SPOKEN_NAMES = {
    "origin": "原点",
    "white_wall": "客厅白墙",
    "study_projection": "书房",
    "living_room": "客厅",
    "living_room_entry_a": "客厅",
}

def _spoken_point(value: Any) -> str:
    point = str(value or "目标位置").strip()
    return POINT_SPOKEN_NAMES.get(point, point)


def task_future_phrase(name: str, arguments: dict[str, Any]) -> str:
    """Describe a requested task in future tense without claiming success."""

    action = str(arguments.get("action") or "").strip().lower()
    if name == SCENARIO_TOOL_NAME:
        scenario = str(arguments.get("scenario") or "").strip()
        phrases = {
            "homecoming_welcome": "播放欢迎回家画面",
            "push_up_companion": "陪你做俯卧撑",
            "pull_up_companion": "陪你做引体向上",
            "squat_companion": "陪你做深蹲",
            "find_pet": "寻找豆豆",
            "find_pet_at": "去指定地点寻找豆豆",
            "find_pet_here": "在当前位置寻找豆豆",
            "find_and_feed_doudou": "寻找并投喂豆豆",
            "meeting_projection": "准备会议投影",
            "meeting_projection_stop": "结束会议投影",
            "rest_lighting": "调整休息灯光",
        }
        return phrases.get(scenario, "执行场景流程")
    if name == "navigation_goto":
        return f"去{_spoken_point(arguments.get('point'))}"
    if name == "navigation_list":
        return "查询可以前往的地点"
    if name == "light_control":
        return {"on": "打开灯光", "off": "关闭灯光"}.get(action, "查询灯光状态")
    if name == "feeder_control":
        grams = arguments.get("grams")
        return f"投食{grams}克" if action == "feed" and grams else ("启动投食" if action == "feed" else "查询投食器状态")
    if name == "head_control":
        return {"up": "抬头", "down": "低头", "level": "恢复平视"}.get(action, "调整头部角度")
    if name in {"move_forward", "move_backward", "move_left", "move_right"}:
        return {
            "move_forward": "向前移动",
            "move_backward": "向后移动",
            "move_left": "向左转",
            "move_right": "向右转",
        }[name]
    if name in {"front_camera_capture", "back_camera_capture", "camera_capture"}:
        return "拍一张照片"
    if name in {"front_camera_record", "back_camera_record", "camera_record"}:
        return "录一段视频"
    if name == "face_recognition":
        return "看看面前是谁"
    if name == "media_player":
        labels = {
            "play_music": "播放音乐",
            "play_video": "播放娱乐视频",
            "play_movie": "投影播放电影",
            "pause": "暂停播放",
            "resume": "继续播放",
            "next": "切换下一首",
            "stop": "结束播放",
            "status": "查看播放状态",
            "list": "查看可播放内容",
        }
        return labels.get(action, "处理媒体播放")
    if name == "face_registration":
        return "登记这张人脸"
    if name == "person_tracking":
        return {"stop": "停止跟随"}.get(action, "识别并跟随这个人")
    if name == "pet_tracking":
        return "寻找豆豆"
    if name == "projector_control":
        return "结束投影" if action in {"off", "stop"} else "准备投影"
    if name in {"push_up", "pull_up", "squat"}:
        label = {"push_up": "俯卧撑", "pull_up": "引体向上", "squat": "深蹲"}[name]
        return f"开始{label}计数"
    if name == "reminder_schedule":
        return "设置提醒"
    if name == "reminder_query":
        return "查询提醒"
    if name == "reminder_cancel":
        return "删除提醒"
    if name == "realtime_information":
        return {
            "current_time": "查询当前日期和时间",
            "weather": "查询天气",
            "location": "查询机器人所在的地理位置",
            "indoor_location": "查询机器人当前所在的室内房间",
            "external_location": "查询机器人配置的外部地理位置",
            "nearby": "查询附近地点",
            "traffic": "查询交通",
            "commute_recommendation": "比较前往目的地的驾车和公交地铁方案",
        }.get(action, "查询实时信息")
    if name == "environment_perception":
        return "查看当前环境"
    return "处理这项任务"


def _pick_variant(options: Sequence[str], variation_key: str) -> str:
    if not options:
        return ""
    turn_prefix = str(variation_key or "").split("|", 1)[0]
    if turn_prefix.isdigit():
        return options[(max(1, int(turn_prefix)) - 1) % len(options)]
    digest = hashlib.sha256(str(variation_key or time.time_ns()).encode("utf-8")).digest()
    return options[int.from_bytes(digest[:4], "big") % len(options)]


def build_skill_start_speech(
    name: str,
    arguments: dict[str, Any],
    variation_key: str = "",
) -> str:
    phrase = task_future_phrase(name, arguments)
    key = f"{variation_key}|{name}|{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
    if name == "navigation_goto":
        destination = _spoken_point(arguments.get("point"))
        return _pick_variant(
            (
                f"好，我去{destination}。",
                f"收到，这就去{destination}。",
                f"行，出发去{destination}。",
                f"好，马上到{destination}。",
            ),
            key,
        )
    if phrase.startswith(("查询", "看看", "查看", "读取")):
        return _pick_variant(
            ("好，我查一下。", "稍等，我来看看。", "收到，马上查。"),
            key,
        )
    return _pick_variant(
        ("好，我来处理。", "收到，马上开始。", "可以，交给我。"),
        key,
    )


def build_sequence_start_speech(tasks: list[dict[str, Any]], variation_key: str = "") -> str:
    phrases = [task_future_phrase(str(item["name"]), dict(item.get("arguments") or {})) for item in tasks]
    if len(phrases) == 2:
        return _pick_variant(
            (
                "好，我按顺序来。",
                "收到，这两件事依次处理。",
                "明白，我先做第一件。",
            ),
            f"{variation_key}|sequence|{phrases}",
        )
    return _pick_variant(
        (
            "好，我按顺序处理。",
            f"收到，这{len(phrases)}件事依次来。",
            "明白，我一项一项完成。",
        ),
        f"{variation_key}|sequence|{phrases}",
    )


SEQUENCE_READOUT_SKILLS = {
    "navigation_list",
    "reminder_query",
    "realtime_information",
    "face_recognition",
    "environment_perception",
}


def build_sequence_success_summary(
    tasks: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> str:
    """Produce one aggregate completion sentence for a successful sequence.

    Child ``spoken_summary`` values belong to the execution layer. Joining all
    of them caused speech such as “该操作已完成；该操作已完成”. Keep live
    readout facts from query skills, but collapse ordinary device actions into
    one task-aware final state.
    """

    readouts: list[str] = []
    ordinary_tasks: list[dict[str, Any]] = []
    ordinary_summaries: list[str] = []
    for task, record in zip(tasks, records):
        name = str(task.get("name") or "")
        arguments = dict(task.get("arguments") or {})
        action = str(arguments.get("action") or "").lower()
        is_readout = bool(
            name in SEQUENCE_READOUT_SKILLS
            or action in {"status", "query", "list", "check"}
            or name.startswith("memory_query")
        )
        summary = str((record.get("result") or {}).get("spoken_summary") or "").strip()
        if is_readout and summary:
            normalized = re.sub(r"[\s，。！？!?、；;：:,.]+", "", summary)
            if normalized and all(
                re.sub(r"[\s，。！？!?、；;：:,.]+", "", item) != normalized
                for item in readouts
            ):
                readouts.append(summary.rstrip("。") + "。")
        else:
            ordinary_tasks.append(task)
            ordinary_summaries.append(summary)

    def is_generic_completion(summary: str) -> bool:
        normalized = re.sub(r"[\s，。！？!?、；;：:,.]+", "", summary)
        return bool(
            re.fullmatch(
                r"(?:该|这项|当前)?(?:操作|动作|任务)?(?:已经|已)?(?:执行)?完成(?:了)?",
                normalized,
            )
        )

    meaningful_summaries: list[str] = []
    for summary in ordinary_summaries:
        if not summary or is_generic_completion(summary):
            continue
        normalized = re.sub(r"[\s，。！？!?、；;：:,.]+", "", summary)
        if normalized and all(
            re.sub(r"[\s，。！？!?、；;：:,.]+", "", item) != normalized
            for item in meaningful_summaries
        ):
            meaningful_summaries.append(summary.rstrip("。"))

    action_summary = ""
    if ordinary_tasks:
        names = [str(item.get("name") or "") for item in ordinary_tasks]
        if len(ordinary_tasks) == 1:
            if meaningful_summaries:
                action_summary = meaningful_summaries[0] + "。"
            else:
                phrase = task_future_phrase(
                    str(ordinary_tasks[0].get("name") or ""),
                    dict(ordinary_tasks[0].get("arguments") or {}),
                )
                action_summary = f"{phrase}已经完成了。"
        elif all(name == "head_control" for name in names):
            final_action = str((ordinary_tasks[-1].get("arguments") or {}).get("action") or "")
            final_state = {
                "up": "抬头状态",
                "down": "低头状态",
                "level": "平视状态",
            }.get(final_action, "目标角度")
            action_summary = f"头部已经按你的顺序调整到{final_state}了。"
        elif all(name == "navigation_goto" for name in names):
            destination = _spoken_point((ordinary_tasks[-1].get("arguments") or {}).get("point"))
            action_summary = f"导航任务已经按顺序完成，现在在{destination}。"
        elif all(name == "light_control" for name in names):
            action_summary = "灯光已经按你的顺序调整好了。"
        elif all(name in {"media_player", "projector_control"} for name in names):
            action_summary = "播放和投影已经按你的顺序调整好了。"
        elif all(name in {"move_forward", "move_backward", "move_left", "move_right"} for name in names):
            action_summary = "移动动作已经按你的顺序完成了。"
        elif meaningful_summaries:
            action_summary = "；".join(meaningful_summaries)
            missing_count = len(ordinary_tasks) - len(meaningful_summaries)
            if missing_count > 0:
                action_summary += f"；其余{missing_count}项操作也已按顺序完成"
            action_summary += "。"
        else:
            action_summary = f"这{len(ordinary_tasks)}项操作已经按你的顺序完成了。"

    if action_summary and readouts:
        return action_summary + " ".join(readouts)
    if readouts:
        return " ".join(readouts)
    return action_summary or f"这{len(tasks)}项任务已经按顺序完成了。"

# Infrastructure parameters are owned by the robot configuration, never by the model.
INTERNAL_ARGUMENTS = {
    "dry_run",
    "json_output",
    "timeout",
    "hold",
    "topic",
    "cmd_vel_topic",
    "action_name",
    "frame_id",
    "device",
    "output_path",
    "backend",
    "model",
    "use_env_proxy",
    "call_services",
    "discovery_timeout",
    "service_timeout",
    "wait",
    "repeat",
    "interval",
}
PLANNER_METADATA_ARGUMENTS = {"evidence", "confidence", "constraints"}
RUNNER_RESULT_PREFIX = "QWEN_SKILL_RUNNER_RESULT="
SYNTHETIC_PROPERTIES: dict[str, dict[str, dict[str, Any]]] = {
    "realtime_information": {
        "action": {
            "type": "string",
            "enum": ["current_time", "weather", "location", "indoor_location", "external_location", "nearby", "traffic", "commute_recommendation"],
            "description": "实时查询类型。必须根据用户问题选择。",
        },
        "query": {"type": "string", "description": "保留用户的原始查询文本。"},
        "location": {"type": "string", "description": "用户明确指定的地点；未指定则省略。"},
        "latitude": {"type": "number", "description": "用户明确指定的纬度；通常省略。"},
        "longitude": {"type": "number", "description": "用户明确指定的经度；通常省略。"},
        "radius": {"type": "integer", "minimum": 100, "maximum": 50000},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        "origin": {"type": "string", "description": "出发地；未指定时使用已保存的家庭位置。"},
        "destination": {"type": "string", "description": "用户明确提出的外部目的地。"},
    },
    "pet_tracking": {
        "action": {
            "type": "string",
            "enum": ["find", "track", "stop", "find_route", "find_and_track", "find_route_and_track"],
            "description": "寻找、跟踪或停止宠物任务。",
        },
        "pet": {"type": "string", "description": "宠物类型或名称。"},
        "camera": {"type": "string", "enum": ["front", "back"]},
        "duration": {"type": "number", "minimum": 1, "maximum": 600},
    },
}
DESCRIPTION_OVERRIDES = {
    "face_recognition": (
        "识别当前前摄像头画面中的已注册人脸，用来回答用户身份。用户问‘你知道我是谁吗’、"
        "‘我是谁’、‘你认得我吗’或‘看看面前的人是谁’时必须调用本工具；不得仅凭对话记忆猜身份。"
    ),
    "media_player": (
        "控制机器人独立多媒体播放器。支持默认或指定音乐、娱乐视频、换歌、暂停、继续、结束、"
        "状态和内容列表。该工具不调整头部、不移动底盘，也不自动打开投影光机。"
    ),
    "realtime_information": (
        "查询联网校时后的当前日期时间、实时天气、机器人室内位置、配置的外部粗略位置、附近地点或交通。"
        "用户询问从家庭、公司或其他明确外部地点前往任意可解析外部目的地的路线、耗时、"
        "驾车与公交地铁选择时，必须调用 commute_recommendation 获取实时路线后再回答。"
        "用户泛问机器人在哪里时必须使用 indoor_location，从地图定位判断客厅、书房或餐厅；"
        "只有用户明确询问 GPS、经纬度、城市、街道或外部地理位置时才使用 external_location。"
        "location 是兼容旧调用的自动分流入口。位置查询只回答机器人自身，"
        "绝不用于查询人物、手机、用户、宠物或其他设备在哪里；普通知识问题直接聊天回答。"
    ),
    "environment_perception": (
        "仅当用户明确要求机器人通过当前摄像头看看、观察、识别或检查眼前环境时调用。"
        "单独询问‘客厅有什么、某地怎么样’不代表授权打开摄像头，应先正常回答或澄清。"
    ),
    "navigation_list": (
        "只查询机器人已经保存了哪些导航点，不移动机器人。仅当用户询问“有哪些地点、可以去哪、"
        "列出导航点”时调用。用户说“导航到、去、前往某地点”时绝对禁止调用本工具，必须直接调用 navigation_goto。"
    ),
    "navigation_goto": (
        "让机器人导航到用户指定的地点或坐标。用户说“导航到书房、去厨房、前往某地”时直接调用，"
        "把地点写入 point；不得先调用 navigation_list。只有用户明确要求导航时才能调用。"
    ),
    "projector_control": (
        "只控制投影仪的独立电源、会议画面暂停、继续和状态查询。打开投影仪但未说明内容时只用 on，"
        "不得猜测电影或会议；会议开始必须调用 meeting_projection 参数化场景，会议结束必须调用 "
        "meeting_projection_stop，暂停和继续不得导航或重新开始播放。"
    ),
}


class LocalSkillError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LocalSkillError(f"invalid_skill_spec:{path}")
    return value


def _is_disabled(spec: dict[str, Any]) -> bool:
    return bool(
        spec.get("disabled_reason")
        or "disabled" in {str(item) for item in spec.get("side_effects") or []}
        or "disabled" in {str(item) for item in spec.get("domain_tags") or []}
    )


def _normalized_schema(spec: dict[str, Any]) -> dict[str, Any]:
    source = spec.get("parameters")
    source = source if isinstance(source, dict) else {}
    raw_properties = source.get("properties")
    raw_properties = raw_properties if isinstance(raw_properties, dict) else {}
    allowed_actions = [str(item) for item in spec.get("allowed_actions") or []]
    properties: dict[str, Any] = {}
    for name, raw in raw_properties.items():
        if name in INTERNAL_ARGUMENTS or not isinstance(raw, dict):
            continue
        item = dict(raw)
        default_marker = object()
        default_value = raw.get("default", raw.get("python_default", default_marker))
        value_type = item.get("type")
        if isinstance(value_type, str) and "|" in value_type:
            # The API expects standard JSON Schema. Models should omit optional nulls.
            item["type"] = next(
                (part for part in value_type.split("|") if part and part != "null"),
                "string",
            )
        item.pop("default_source", None)
        item.pop("python_default", None)
        item.pop("required", None)
        if default_value is not default_marker and default_value is not None:
            rendered_default = json.dumps(default_value, ensure_ascii=False)
            description = str(item.get("description") or "").strip()
            suffix = f"省略时使用默认值 {rendered_default}"
            item["description"] = f"{description}；{suffix}" if description else suffix
        if name == "action" and allowed_actions:
            item["enum"] = allowed_actions
            if spec.get("name") == "navigation_goto":
                item["enum"] = ["goto"]
        properties[str(name)] = item
    synthetic = SYNTHETIC_PROPERTIES.get(str(spec.get("name") or ""), {})
    for name, item in synthetic.items():
        properties.setdefault(name, dict(item))
    if str(spec.get("name") or "") == "realtime_information" and "action" in properties:
        properties["action"]["enum"] = [
            "current_time", "weather", "location", "indoor_location",
            "external_location", "nearby", "traffic", "commute_recommendation",
        ]
    if str(spec.get("name") or "") == "projector_control" and "action" in properties:
        properties["action"]["enum"] = ["on", "internal_on", "meeting_pause", "meeting_resume", "status"]
    required = [
        str(name)
        for name in source.get("required") or []
        if str(name) in properties
    ]
    return {"type": "object", "properties": properties, "required": required}


def _tool_description(spec: dict[str, Any]) -> str:
    skill_name = str(spec.get("name") or "")
    parts = [
        DESCRIPTION_OVERRIDES.get(
            skill_name,
            str(spec.get("description_zh") or skill_name or "本地机器人功能"),
        )
    ]
    when = [str(item) for item in spec.get("when_to_call_zh") or [] if str(item).strip()]
    if when:
        parts.append("仅在以下情况调用：" + "；".join(when[:4]))
    effects = [str(item) for item in spec.get("side_effects") or [] if str(item).strip()]
    if effects:
        parts.append("可能副作用：" + "、".join(effects[:6]))
    return "。".join(parts)[:900]


def _parse_runner_result(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if not line.startswith(RUNNER_RESULT_PREFIX):
            continue
        try:
            value = json.loads(line[len(RUNNER_RESULT_PREFIX) :])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None
    return None


class LocalSkillBridge:
    def __init__(
        self,
        *,
        spec_dir: Path = DEFAULT_SPEC_DIR,
        enabled_skills: Sequence[str] | None = None,
        execute: bool = False,
        timeout: float = 120.0,
        robot_project: Path = DEFAULT_ROBOT_PROJECT,
        runner_path: Path | None = None,
        backend: str = "auto",
        host_socket: Path = DEFAULT_SKILL_HOST_SOCKET,
        scenario_catalog_path: Path | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.spec_dir = Path(spec_dir)
        self.execute = bool(execute)
        self.timeout = max(1.0, float(timeout))
        self.robot_project = Path(robot_project)
        self.runner_path = runner_path or Path(__file__).with_name("skill_runner.py")
        self.backend = str(backend or "auto").strip().lower()
        if self.backend not in {"auto", "host", "subprocess"}:
            raise LocalSkillError(f"invalid_skill_backend:{self.backend}")
        self.host_socket = Path(host_socket)
        self.event_callback = event_callback
        self.specs: dict[str, dict[str, Any]] = {}
        self.unavailable: dict[str, str] = {}
        self._processes: set[subprocess.Popen[str]] = set()
        self._process_lock = threading.Lock()

        requested = [str(item).strip() for item in (enabled_skills or []) if str(item).strip()]
        if requested:
            candidates = requested
        else:
            candidates = [
                path.stem
                for path in sorted(self.spec_dir.glob("*.json"))
                if path.name != "index.json"
            ]
        for name in candidates:
            path = self.spec_dir / f"{name}.json"
            if not path.is_file():
                raise LocalSkillError(f"skill_spec_not_found:{name}")
            spec = _load_json(path)
            canonical_name = str(spec.get("name") or name)
            if _is_disabled(spec):
                self.unavailable[canonical_name] = str(spec.get("disabled_reason") or "disabled_by_existing_spec")
                continue
            self.specs[canonical_name] = spec
        if not self.specs:
            raise LocalSkillError("no_enabled_local_skills")
        if not self.runner_path.is_file():
            raise LocalSkillError(f"skill_runner_not_found:{self.runner_path}")
        self.scenario_catalog: ScenarioCatalog | None = None
        self.scenario_executor: ScenarioExecutor | None = None
        if scenario_catalog_path is not None and Path(scenario_catalog_path).is_file():
            self.scenario_catalog = ScenarioCatalog(Path(scenario_catalog_path))
            self.scenario_executor = ScenarioExecutor(
                self.scenario_catalog,
                lambda skill, args: self._invoke_atomic(
                    skill,
                    args,
                    "",
                    turn_id=self.current_turn_id,
                    announce=False,
                ),
                progress_callback=self._emit_speech_event,
            )
        self._turn_scenario_results: dict[tuple[str, str], dict[str, Any]] = {}
        self.current_turn_id = ""

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        protected = {"push_up", "pull_up", "squat", "welcome_projection"}
        schemas = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": _tool_description(spec),
                    "parameters": _normalized_schema(spec),
                },
            }
            for name, spec in sorted(self.specs.items())
            if not (self.scenario_catalog is not None and name in protected)
        ]
        if self.scenario_catalog is not None:
            schemas.insert(0, self.scenario_catalog.tool_schema)
        child_names = [str(item["function"]["name"]) for item in schemas]
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": SEQUENCE_TOOL_NAME,
                    "description": (
                        "按用户说出的先后顺序连续执行两个到六个已经注册的 Skill 或完整场景。"
                        "当同一句话明确包含‘先……然后……’、‘做完……再……’等多个动作时必须只调用本工具一次，"
                        "不得只执行第一项，也不得把受保护场景拆成原子动作。默认前一项失败就停止后续；"
                        "只有用户明确说不论前一项结果都继续时才使用 failure_policy=continue。"
                        "每个 task 必须有用户原话依据，地点词不能自动补成灯光等关联场景。完整场景统一写成"
                        "name=run_robot_scenario、arguments.scenario=场景名；例如关闭会议投影再导航到客厅，"
                        "依次填写 meeting_projection_stop 场景和 navigation_goto，不得增加开灯。"
                        "‘不要开灯、不要投影’是约束，不是 task；只保留句中其他正向动作。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": MAX_SEQUENCE_TASKS,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "enum": child_names},
                                        "arguments": {"type": "object"},
                                    },
                                    "required": ["name", "arguments"],
                                },
                            },
                            "failure_policy": {
                                "type": "string",
                                "enum": ["stop", "continue"],
                                "default": "stop",
                            },
                        },
                        "required": ["tasks"],
                    },
                },
            }
        )
        return schemas

    def catalog_summary(self) -> dict[str, Any]:
        return {
            "enabled_count": len(self.specs),
            "enabled_skills": sorted(self.specs),
            "unavailable_count": len(self.unavailable),
            "unavailable_skills": dict(sorted(self.unavailable.items())),
            "mode": "execute" if self.execute else "dry_run",
            "backend": self.backend,
            "skill_host_socket": str(self.host_socket),
            "scenario_count": len(self.scenario_catalog.procedures) if self.scenario_catalog else 0,
            "scenarios": sorted(self.scenario_catalog.procedures) if self.scenario_catalog else [],
        }

    def recover_explicit_plan(self, user_text: str) -> dict[str, Any] | None:
        """Compile a high-confidence local plan when Qwen only talks.

        The realtime model remains the primary planner.  This is a narrow
        enforcement layer for utterances whose positive device action is
        already provable by the same scenario/atomic validators used during a
        normal function call.  It never guesses a destination or invents a
        missing action.
        """

        if is_retrospective_query(user_text):
            return None
        tasks = _recover_explicit_sequence_tasks(user_text, self.scenario_catalog)
        if not tasks:
            return None
        if len(tasks) == 1:
            return copy.deepcopy(tasks[0])
        return {
            "name": SEQUENCE_TOOL_NAME,
            "arguments": {
                "tasks": copy.deepcopy(tasks),
                "failure_policy": (
                    "continue"
                    if _explicit_continue_requested(user_text)
                    else "stop"
                ),
            },
        }

    def recover_contextual_plan(
        self,
        user_text: str,
        prior_assistant_text: str,
    ) -> dict[str, Any] | None:
        """Resolve only a short answer to our immediately preceding question.

        Ordinary conversation never enters this path.  A plan is recovered
        only when the current answer is an unambiguous affirmation and the
        preceding local speech contains one concrete confirmation question,
        or when the preceding question requested a navigation destination and
        the user supplies an allowed point.
        """

        answer = _intent_text(user_text)
        prior = _intent_text(prior_assistant_text)
        if not answer or not prior:
            return None
        active_movie = bool(
            re.search(
                r"电影.{0,10}(?:已经|正在|开始|播放|暂停|投好)|"
                r"(?:已经|正在|开始|继续|暂停).{0,10}电影|"
                r"(?:大雄兔|影片).{0,10}(?:播放|暂停)",
                prior,
            )
        )
        if active_movie:
            contextual_controls = (
                ("movie_projection_pause", r"^(?:暂停|停一下|先停|暂停一下)(?:播放|电影|影片)?$"),
                ("movie_projection_resume", r"^(?:继续|恢复|接着播|接着看|继续播放)(?:电影|影片)?$"),
                ("movie_projection_stop", r"^(?:不看了|结束|停止|关掉|关闭|收起来|就到这里|就到这)(?:电影|影片|播放|投影)?$"),
            )
            for scenario, pattern in contextual_controls:
                if re.match(pattern, answer) and scenario in self.scenario_catalog.procedures:
                    return {"name": SCENARIO_TOOL_NAME, "arguments": {"scenario": scenario}}
        if answer in _CONTEXT_REJECTIONS or re.match(r"^(?:不是|不对|不要|不用|算了|取消)", answer):
            return None

        if any(term in prior for term in ("要去哪个位置", "要去哪里", "目的地没听清", "原点客厅白墙还是书房")):
            plan = _explicit_navigation_task(f"导航到{user_text}")
            if plan is not None:
                return plan

        scenario = None
        if re.search(r"(?:需要|要不要|想不想).{0,10}(?:放|看|播放).{0,4}(?:电影|影片)", prior):
            scenario = "movie_projection"
        elif re.search(r"(?:需要|要不要|想不想).{0,10}(?:陪你|陪您|一起).{0,5}(?:运动|锻炼)", prior):
            scenario = "push_up_companion"
        elif "跟我打招呼" in prior and "理想同学" in prior:
            scenario = "homecoming_welcome"
        elif "你是想" not in prior and "是想" not in prior:
            return None
        elif "俯卧撑" in prior:
            scenario = "push_up_companion"
        elif "引体向上" in prior:
            scenario = "pull_up_companion"
        elif "深蹲" in prior:
            scenario = "squat_companion"
        elif "找豆豆" in prior and "喂食" in prior:
            scenario = "find_and_feed_doudou"
        elif "找豆豆" in prior:
            scenario = "find_pet"
        elif "结束当前的会议投影" in prior:
            scenario = "meeting_projection_stop"
        elif "开始会议投影" in prior:
            scenario = "meeting_projection"
        elif "调整灯光" in prior and "休息" in prior:
            scenario = "rest_lighting"
        if scenario is None or scenario not in self.scenario_catalog.procedures:
            return None
        if not _contextual_affirmative(answer, scenario):
            return None
        return {
            "name": SCENARIO_TOOL_NAME,
            "arguments": {"scenario": scenario},
        }

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        user_text: str = "",
        turn_id: str = "",
        prior_assistant_text: str = "",
        trusted_scenario: bool = False,
        announce_scenario: bool = True,
    ) -> dict[str, Any]:
        trusted_resume = bool(
            trusted_scenario
            and (
                arguments.get("resume_from_interrupt")
                or name == SEQUENCE_TOOL_NAME
            )
        )
        contextual_movie_scene: str | None = None
        prior = _intent_text(prior_assistant_text)
        active_movie = bool(
            re.search(
                r"电影.{0,10}(?:已经|正在|开始|播放|暂停|投好)|"
                r"(?:已经|正在|开始|继续|暂停).{0,10}电影|"
                r"(?:大雄兔|影片).{0,10}(?:播放|暂停)",
                prior,
            )
        )
        # A short “暂停一下/继续/不看了” while a movie projection is active
        # must control the whole projection session, not only the Android
        # player.  Constrain this conversion to authoritative preceding movie
        # status speech so the same words still control music normally and a
        # declined movie offer cannot turn off unrelated hardware.
        if name == "media_player" and self.scenario_catalog is not None:
            action = str(arguments.get("action") or "").strip().lower()
            scene_for_action = {
                "pause": "movie_projection_pause",
                "resume": "movie_projection_resume",
                "stop": "movie_projection_stop",
            }.get(action)
            if active_movie and scene_for_action in self.scenario_catalog.procedures:
                contextual_movie_scene = scene_for_action
                name = SCENARIO_TOOL_NAME
                arguments = {"scenario": scene_for_action}
        elif name == SCENARIO_TOOL_NAME and active_movie:
            requested_movie_control = str(arguments.get("scenario") or "")
            if requested_movie_control in {
                "movie_projection_pause", "movie_projection_resume", "movie_projection_stop",
            }:
                contextual_movie_scene = requested_movie_control
        # Realtime models occasionally emit a valid scenario name directly as
        # the function name (for example ``meeting_projection``) instead of
        # calling ``run_robot_scenario`` with a scenario argument.  Sequence
        # calls already canonicalize this harmless representation difference;
        # apply the same rule to a top-level call so the protected compiler,
        # intent evidence checks and dependency gates still run.
        if (
            not trusted_resume
            and self.scenario_catalog is not None
            and name != SCENARIO_TOOL_NAME
            and name in self.scenario_catalog.procedures
        ):
            arguments = {**dict(arguments), "scenario": name}
            name = SCENARIO_TOOL_NAME

        # Transcript-grounded recovery may repair missing or contradictory
        # model arguments, but never invents an action.  Apply it when the
        # recovered plan is one unambiguous atomic task.  This keeps Qwen as
        # the broad semantic planner while preventing an empty call from
        # defaulting ``抬头`` to ``level`` or ``播放七里香`` to no action.
        if not trusted_resume and name != SEQUENCE_TOOL_NAME:
            explicit_plan = self.recover_explicit_plan(user_text)
            if isinstance(explicit_plan, dict) and explicit_plan.get("name") != SEQUENCE_TOOL_NAME:
                explicit_name = str(explicit_plan.get("name") or "")
                explicit_arguments = dict(explicit_plan.get("arguments") or {})
                if explicit_name == name and name != SCENARIO_TOOL_NAME:
                    arguments = {**dict(arguments), **explicit_arguments}
                elif (
                    name == SCENARIO_TOOL_NAME
                    and str(arguments.get("scenario") or "") == "meeting_projection_stop"
                    and not _projection_stop_requested(user_text)
                ):
                    # “只平视，不要打开投影” and “暂停会议画面”
                    # must not be coerced into the full stop scene merely
                    # because the utterance mentions projection negatively.
                    name = explicit_name
                    arguments = explicit_arguments

        # Normalize meeting transport before the generic scene matcher. A
        # realtime model sometimes emits meeting_projection for “暂停/继续/结束”;
        # operation semantics must win so transport can never restart the
        # navigation + head + projection start procedure.
        if self.scenario_catalog is not None and name == SCENARIO_TOOL_NAME:
            requested_scene = str(arguments.get("scenario") or "")
            if requested_scene == "meeting_projection":
                # The complete user utterance is authoritative.  Model-provided
                # evidence may be a shortened positive fragment and must not
                # erase an explicit prohibition such as “不要抬头”.
                normalization_text = user_text
                normalized_preview = self.scenario_catalog.normalize_intent(
                    requested_scene, dict(arguments), normalization_text
                )
                meeting_operation = str(normalized_preview.get("operation") or "start")
                if meeting_operation in {"pause", "resume", "status"}:
                    name = "projector_control"
                    arguments = {
                        "action": {
                            "pause": "meeting_pause",
                            "resume": "meeting_resume",
                            "status": "status",
                        }[meeting_operation]
                    }
                elif meeting_operation == "stop":
                    arguments = {"scenario": "meeting_projection_stop"}

        # “豆豆” is not permission to start a patrol. If the model selected
        # the complete find-and-feed scene without a positive find/search
        # predicate, safely narrow it to the direct feeder operation and keep
        # the explicit quantity.
        if self.scenario_catalog is not None and name == SCENARIO_TOOL_NAME:
            requested_scene = str(arguments.get("scenario") or "")
            direct_feed = _explicit_feeder_task(user_text)
            positive_search = bool(
                re.search(r"找|找到|寻找|搜索|看看|瞧瞧|在哪|去看", _intent_text(user_text))
                and not self.scenario_catalog._pet_search_negated(user_text)
            )
            if requested_scene == "find_and_feed_doudou" and direct_feed is not None and not positive_search:
                name = direct_feed["name"]
                arguments = direct_feed["arguments"]
        if name == SEQUENCE_TOOL_NAME:
            return self._invoke_sequence(
                arguments,
                user_text=user_text,
                turn_id=turn_id,
                prior_assistant_text=prior_assistant_text,
                trusted_resume=trusted_resume,
            )
        if not trusted_resume and self.scenario_catalog is not None and self.scenario_executor is not None:
            matched = self.scenario_catalog.match(user_text)
            if (
                matched
                and name != SCENARIO_TOOL_NAME
                and matched != "homecoming_welcome"
                and not self._scenario_contains_skill(matched, name)
            ):
                # A topic word inside another tool's argument is not permission
                # to replace that tool with a whole scene.  “提醒我十分钟后开会”
                # must remain a reminder, while an incomplete navigation/head
                # plan for a real meeting utterance is still upgraded to the
                # protected meeting procedure.
                matched = None
            requested = str(arguments.get("scenario") or "") if name == SCENARIO_TOOL_NAME else ""
            if requested:
                # A sequence child has already been normalized and validated
                # against its own spoken clause.  Re-normalizing every child
                # against the complete utterance makes the first scenario win
                # again (for example pause -> resume became pause -> pause).
                if not trusted_scenario:
                    requested = self.scenario_catalog.normalize_scenario_name(requested, user_text)
                arguments = {**dict(arguments), "scenario": requested}
            # The legacy living-room convenience scene is parallel by design.
            # When the user explicitly separates navigation and lighting, turn
            # even a top-level model scene call back into the requested atomic
            # sequence so order, negation and failure policy remain intact.
            if (
                requested == "living_room_light_service"
                and self.scenario_catalog.living_light_requires_atomic_sequence(user_text)
            ):
                repaired = _repair_sequence_tasks(
                    [{"name": SCENARIO_TOOL_NAME, "arguments": {"scenario": requested}}],
                    user_text,
                    self.scenario_catalog,
                )
                if repaired:
                    return self._invoke_sequence(
                        {"tasks": repaired, "failure_policy": "continue" if "继续" in user_text else "stop"},
                        user_text=user_text,
                        turn_id=turn_id,
                        prior_assistant_text=prior_assistant_text,
                    )
            protected = self.scenario_catalog.protected_scenario(name, arguments)
            normalized_user = "".join(str(user_text or "").split()).strip("，。！？!?、")
            affirmative = normalized_user.lower() in _CONTEXT_AFFIRMATIONS | {"没问题", "就这样吧"}
            contextual = contextual_movie_scene or (
                self.scenario_catalog.match(f"{prior_assistant_text} {user_text}")
                if affirmative and str(prior_assistant_text or "").strip()
                else None
            )
            semantic_reason = "not_requested"
            if requested and not trusted_scenario and requested not in {matched, contextual}:
                semantic_ok, semantic_reason = self.scenario_catalog.model_scenario_supported(
                    requested,
                    user_text,
                    matched=matched,
                    prior_context=prior_assistant_text,
                )
                if not semantic_ok:
                    clarification, suggested = _scenario_clarification(
                        requested,
                        user_text,
                        semantic_reason,
                    )
                    return {
                        "ok": False,
                        "validation_ok": False,
                        "executed": False,
                        "device_state_changed": False,
                        "skill": SCENARIO_TOOL_NAME,
                        "scenario": requested,
                        "mode": "intent_rejected",
                        "error": "scenario_not_supported_by_user_intent",
                        "routing_reason": semantic_reason,
                        "clarification_required": True,
                        "suggested_scenario": suggested,
                        "spoken_summary": clarification,
                    }
            scenario = (
                requested
                if trusted_scenario and requested
                else matched or contextual or requested or protected
            )
            if scenario:
                try:
                    # Always validate against the complete utterance.  Evidence
                    # is retained for audit, but never replaces user constraints.
                    normalization_text = user_text
                    normalized_intent = self.scenario_catalog.normalize_intent(
                        scenario, dict(arguments), normalization_text
                    )
                    # Compile once before any announcement or hardware dispatch.
                    # This is the single final gate for negative, only= and
                    # base-motion invariants; semantic evidence is not judged a
                    # second time by another narrow phrase list here.
                    self.scenario_catalog.compile_intent(normalized_intent)
                except ScenarioError as exc:
                    return {
                        "ok": False,
                        "validation_ok": False,
                        "executed": False,
                        "device_state_changed": False,
                        "skill": SCENARIO_TOOL_NAME,
                        "scenario": scenario,
                        "mode": "intent_rejected",
                        "error": str(exc),
                        "spoken_summary": "你的限制条件和任务步骤有冲突，所以我先没有执行。",
                    }
                # Keep the executor call backward-compatible: catalog defaults
                # remain catalog-owned, while only transcript-grounded
                # overrides are passed explicitly. The normalized full intent
                # above is still the authoritative object used for validation
                # and audit output.
                allowed = set(
                    (self.scenario_catalog.procedures.get(scenario, {}).get("parameters") or {}).keys()
                )
                inferred = self.scenario_catalog.infer_arguments(scenario, normalization_text)
                clean = {
                    key: value for key, value in arguments.items()
                    if key in allowed
                }
                clean = {**clean, **inferred}
                if scenario in {"meeting_projection", "movie_projection"}:
                    if "point" not in inferred:
                        clean.pop("point", None)
                    if inferred.get("stay_put"):
                        clean["stay_put"] = True
                        clean.pop("navigate", None)
                        clean.pop("point", None)
                    else:
                        clean.pop("stay_put", None)
                        clean.pop("navigate", None)
                    if inferred.get("head") != "keep":
                        clean.pop("head", None)
                    clean.pop("content", None)
                turn_key = str(turn_id or self.current_turn_id or "")
                cache_key = (turn_key, scenario)
                if turn_key and cache_key in self._turn_scenario_results:
                    cached = copy.deepcopy(self._turn_scenario_results[cache_key])
                    cached["deduplicated"] = True
                    return cached
                if announce_scenario:
                    result = self.scenario_executor.execute(scenario, clean)
                else:
                    result = self.scenario_executor.execute(
                        scenario,
                        clean,
                        announce=False,
                    )
                result.setdefault(
                    "routing",
                    {
                        "source": (
                            "local_match" if matched
                            else "assistant_context" if contextual
                            else "trusted_scenario" if trusted_scenario and requested
                            else semantic_reason if requested
                            else "protected_atomic"
                        ),
                        "matched_scenario": matched,
                        "requested_scenario": requested or None,
                    },
                )
                result.setdefault("normalized_intent", copy.deepcopy(normalized_intent))
                if turn_key:
                    self._turn_scenario_results = {
                        key: value for key, value in self._turn_scenario_results.items()
                        if key[0] == turn_key
                    }
                    self._turn_scenario_results[cache_key] = copy.deepcopy(result)
                return result
        if name == SCENARIO_TOOL_NAME:
            return {
                "ok": False,
                "validation_ok": False,
                "executed": False,
                "skill": name,
                "error": "scenario_catalog_unavailable",
                "spoken_summary": "场景功能现在还没准备好，所以我没有贸然执行。",
            }
        if name == "realtime_information":
            arguments = _normalize_realtime_information_arguments(arguments, user_text)
        if name == "navigation_goto":
            explicit_navigation = _explicit_navigation_task(user_text)
            if explicit_navigation is not None:
                arguments = {
                    **dict(arguments),
                    "point": explicit_navigation["arguments"]["point"],
                }
        if name == "feeder_control":
            explicit_feeder = _explicit_feeder_task(user_text)
            if explicit_feeder is not None:
                arguments = {
                    **dict(arguments),
                    **dict(explicit_feeder.get("arguments") or {}),
                }
            if str(arguments.get("action") or "").lower() == "feed":
                grams = arguments.get("grams")
                portions = arguments.get("portions")
                if grams is not None:
                    try:
                        grams_value = int(grams)
                    except (TypeError, ValueError):
                        grams_value = -1
                    if not 10 <= grams_value <= 100 or grams_value % 10:
                        return {
                            "ok": False,
                            "validation_ok": False,
                            "executed": False,
                            "device_state_changed": False,
                            "skill": name,
                            "mode": "intent_rejected",
                            "error": "invalid_feed_grams:must_be_10_to_100_multiple_of_10",
                            "clarification_required": True,
                            "spoken_summary": "投食器只能按十克一份投食，请告诉我要投十克到一百克中的哪个数量。",
                        }
                if portions is not None:
                    try:
                        portions_value = int(portions)
                    except (TypeError, ValueError):
                        portions_value = -1
                    if not 1 <= portions_value <= 10:
                        return {
                            "ok": False,
                            "validation_ok": False,
                            "executed": False,
                            "device_state_changed": False,
                            "skill": name,
                            "mode": "intent_rejected",
                            "error": "invalid_feed_portions:must_be_1_to_10",
                            "clarification_required": True,
                            "spoken_summary": "一次最多投十份，请告诉我要投一份到十份中的哪个数量。",
                        }
        if not trusted_resume and self.scenario_catalog is not None:
            atomic_constraints = self.scenario_catalog.explicit_constraints(user_text)
            # “原地左转/右转” explicitly requests an in-place base rotation;
            # here “原地” describes the path, unlike “原地投影” where it
            # forbids navigation.  Explicit no-movement wording still wins.
            compact_user_text = _intent_text(user_text)
            explicit_in_place_turn = bool(
                name in {"move_left", "move_right"}
                and re.search(r"原地.{0,6}(?:左转|右转|向左转|向右转|往左转|往右转)", compact_user_text)
                and not re.search(r"不要移动|不用移动|别移动|底盘(?:不要|别|不许)动|原地不动", compact_user_text)
            )
            if explicit_in_place_turn:
                atomic_constraints["forbid_base_motion"] = False
            allowed_only = set(atomic_constraints.get("allowed_skills") or [])
            forbidden = set(atomic_constraints.get("forbidden") or [])
            action = str(arguments.get("action") or "")
            resources = set(self.scenario_catalog.skill_resources.get(name, ()))
            violation = ""
            if allowed_only and name not in allowed_only:
                violation = f"only_constraint:{name}"
            elif atomic_constraints.get("forbid_base_motion") and "base" in resources:
                violation = f"forbid_base_motion:{name}"
            elif name in forbidden or f"{name}:{action}" in forbidden:
                violation = f"forbidden:{name}:{action}"
            if violation:
                return {
                    "ok": False,
                    "validation_ok": False,
                    "executed": False,
                    "device_state_changed": False,
                    "skill": name,
                    "mode": "intent_rejected",
                    "error": f"intent_constraint_violation:{violation}",
                    "spoken_summary": "你的限制条件不允许执行这项动作，所以我没有操作。",
                }
        return self._invoke_atomic(
            name,
            arguments,
            user_text,
            turn_id=turn_id,
            prior_assistant_text=prior_assistant_text,
            trusted_resume=trusted_resume,
        )

    def _emit_speech_event(self, event: dict[str, Any]) -> None:
        if self.event_callback is None:
            return
        value = dict(event)
        value.setdefault("skill_name", SEQUENCE_TOOL_NAME)
        self.event_callback(value)

    def _sequence_child_names(self) -> set[str]:
        protected = {"push_up", "pull_up", "squat", "welcome_projection"}
        names = {
            name
            for name in self.specs
            if not (self.scenario_catalog is not None and name in protected)
        }
        if self.scenario_catalog is not None:
            names.add(SCENARIO_TOOL_NAME)
        return names

    def _scenario_contains_skill(self, scenario: str, skill: str) -> bool:
        if self.scenario_catalog is None:
            return False
        procedure = self.scenario_catalog.procedures.get(str(scenario)) or {}
        return any(
            str(step.get("skill") or "") == str(skill)
            for step in procedure.get("steps") or []
            if isinstance(step, dict)
        )

    def _invoke_sequence(
        self,
        arguments: dict[str, Any],
        *,
        user_text: str,
        turn_id: str,
        prior_assistant_text: str,
        trusted_resume: bool = False,
    ) -> dict[str, Any]:
        started = time.monotonic()
        self.current_turn_id = str(turn_id or self.current_turn_id or "")
        raw_tasks = arguments.get("tasks")
        failure_policy = str(arguments.get("failure_policy") or "stop").strip().lower()
        if failure_policy not in {"stop", "continue"}:
            failure_policy = "stop"
        explicit_continue = _explicit_continue_requested(user_text)
        if failure_policy == "continue" and not explicit_continue:
            failure_policy = "stop"
        # The public schema asks Qwen to use this wrapper for two or more
        # actions. Be tolerant if it correctly reduces a negated clause and
        # sends the one remaining affirmative task through the wrapper.
        if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= MAX_SEQUENCE_TASKS:
            recovered = _recover_explicit_sequence_tasks(user_text, self.scenario_catalog)
            if recovered:
                raw_tasks = recovered
            else:
                return {
                    "ok": False,
                    "validation_ok": False,
                    "executed": False,
                    "skill": SEQUENCE_TOOL_NAME,
                    "error": "invalid_sequence_task_count",
                    "spoken_summary": "我没有收到完整的任务顺序，所以先没有执行。",
                }

        allowed = set(self.specs) if trusted_resume else self._sequence_child_names()
        tasks: list[dict[str, Any]] = []
        for index, item in enumerate(raw_tasks):
            if not isinstance(item, dict):
                return {
                    "ok": False,
                    "validation_ok": False,
                    "executed": False,
                    "skill": SEQUENCE_TOOL_NAME,
                    "error": f"invalid_sequence_task:{index}",
                    "spoken_summary": "任务顺序里有一项内容不完整，所以我没有开始执行。",
                }
            child_name = str(item.get("name") or "").strip()
            child_arguments = item.get("arguments")
            # Realtime models occasionally put a valid protected scenario name
            # directly in ``name`` even though the schema asks for the wrapper.
            # Canonicalize that harmless representation difference locally;
            # the scenario compiler and all of its safety gates still run.
            if self.scenario_catalog is not None and isinstance(child_arguments, dict):
                if child_name in self.scenario_catalog.procedures:
                    child_arguments = {**child_arguments, "scenario": child_name}
                    child_name = SCENARIO_TOOL_NAME
                elif child_name == SCENARIO_TOOL_NAME:
                    requested = self.scenario_catalog.normalize_scenario_name(
                        str(child_arguments.get("scenario") or ""), user_text
                    )
                    child_arguments = {**child_arguments, "scenario": requested}
                    child_operation = str(child_arguments.get("operation") or "start").lower()
                    if requested == "meeting_projection" and child_operation in {"pause", "resume", "status"}:
                        child_name = "projector_control"
                        child_arguments = {
                            "action": {
                                "pause": "meeting_pause",
                                "resume": "meeting_resume",
                                "status": "status",
                            }[child_operation]
                        }
                    elif requested == "meeting_projection" and child_operation == "stop":
                        child_arguments = {"scenario": "meeting_projection_stop"}
                elif child_name == "projector_control":
                    projector_action = str(child_arguments.get("action") or "").lower()
                    if projector_action == "meeting_presentation_on":
                        child_name = SCENARIO_TOOL_NAME
                        child_arguments = {"scenario": "meeting_projection"}
                    elif projector_action in {"off", "stop"}:
                        child_name = SCENARIO_TOOL_NAME
                        child_arguments = {"scenario": "meeting_projection_stop"}
            if child_name not in allowed or not isinstance(child_arguments, dict):
                return {
                    "ok": False,
                    "validation_ok": False,
                    "executed": False,
                    "skill": SEQUENCE_TOOL_NAME,
                    "error": f"sequence_task_not_allowed:{index}:{child_name}",
                    "spoken_summary": "任务顺序里包含当前不能直接执行的动作，所以我没有开始执行。",
                }
            tasks.append({"name": child_name, "arguments": dict(child_arguments)})

        if not trusted_resume:
            tasks = _repair_sequence_tasks(tasks, user_text, self.scenario_catalog)

        # Enforce explicit “only” constraints before child validation. Prefer
        # deterministic recovery from the user's positive clauses so a model's
        # extra associative task is removed, never executed and never allowed
        # to block the one requested operation.
        sequence_constraints = (
            self.scenario_catalog.explicit_constraints(user_text)
            if self.scenario_catalog is not None
            else {"forbid_base_motion": False, "forbidden": [], "allowed_skills": None}
        )
        # “原地” can scope only one clause in a legitimate compound command
        # (“先原地投影，然后导航到客厅”). Keep global base prohibition only
        # for an explicit no-navigation/no-movement statement; each scene is
        # still compiled against its own clause and therefore skips its own
        # navigation when that clause says 原地.
        if sequence_constraints.get("forbid_base_motion") and not re.search(
            r"不要导航|不用导航|无需导航|不需要导航|别导航|"
            r"不要移动|不用移动|别移动|底盘(?:不要|别|不许)动|原地不动",
            _intent_text(user_text),
        ):
            sequence_constraints["forbid_base_motion"] = False
        allowed_only = set(sequence_constraints.get("allowed_skills") or [])
        if allowed_only and not trusted_resume:
            # ``只`` often modifies a quantity inside one clause rather than
            # the whole utterance: “开灯，然后只喂十克” still requests both
            # actions.  Keep a global allow-list only when the conservative
            # transcript parser cannot prove another positive clause exists.
            explicit_names = {
                str(task.get("name") or "")
                for task in _explicit_clause_atomic_tasks(user_text, self.scenario_catalog)
            }
            if explicit_names - allowed_only:
                allowed_only.clear()
        if allowed_only and not trusted_resume:
            recovered_only = [
                task for task in _explicit_clause_atomic_tasks(user_text, self.scenario_catalog)
                if str(task.get("name") or "") in allowed_only
            ]
            tasks = recovered_only
        if not tasks:
            return {
                "ok": False,
                "validation_ok": False,
                "executed": False,
                "device_state_changed": False,
                "skill": SEQUENCE_TOOL_NAME,
                "mode": "intent_rejected",
                "error": "sequence_contains_no_affirmative_task",
                "spoken_summary": "这句话里没有需要执行的正向设备动作，所以我没有操作。",
            }

        # Validate every child before announcing or executing the first one.
        # This prevents a partly executed sequence when Qwen associated a room
        # with an unspoken light scene, and permits multiple genuine scenarios
        # in one utterance without the catalog's single-winner matcher blocking
        # the second scene.
        validated_tasks: list[dict[str, Any]] = []
        recovery_attempted = False
        while True:
            validated_tasks = []
            rejected: tuple[int, str] | None = None
            for index, task in enumerate(tasks):
                child_name = task["name"]
                child_arguments = task["arguments"]
                clauses = _sequence_clauses(user_text)
                child_text = (
                    str(child_arguments.get("evidence") or "").strip()
                    or (clauses[index] if len(clauses) >= len(tasks) and index < len(clauses) else user_text)
                )
                supported = True
                reason = "explicit_or_unrestricted"
                if trusted_resume:
                    supported, reason = True, "trusted_resume"
                elif child_name == SCENARIO_TOOL_NAME and self.scenario_catalog is not None:
                    requested = str(child_arguments.get("scenario") or "")
                    supported, reason = self.scenario_catalog.model_scenario_supported(
                        requested,
                        user_text,
                        allow_additional_intents=True,
                        prior_context=prior_assistant_text,
                    )
                    if supported:
                        try:
                            normalized_child = self.scenario_catalog.normalize_intent(
                                requested, child_arguments, child_text
                            )
                            self.scenario_catalog.compile_intent(normalized_child)
                            child_arguments = {
                                "scenario": requested,
                                **dict(normalized_child.get("parameters") or {}),
                                "operation": normalized_child.get("operation", "start"),
                                "constraints": normalized_child.get("constraints", {}),
                                "evidence": normalized_child.get("evidence", child_text),
                                "confidence": normalized_child.get("confidence", 1.0),
                            }
                            task = {"name": child_name, "arguments": child_arguments}
                        except ScenarioError as exc:
                            supported, reason = False, str(exc)
                else:
                    supported, reason = _atomic_intent_supported(
                        child_name,
                        child_arguments,
                        user_text,
                        in_sequence=True,
                        prior_assistant_text=prior_assistant_text,
                    )
                    if supported and sequence_constraints.get("forbid_base_motion"):
                        resources = set(
                            self.scenario_catalog.skill_resources.get(child_name, ())
                            if self.scenario_catalog is not None else ()
                        )
                        if "base" in resources:
                            supported, reason = False, "forbid_base_motion"
                if not supported:
                    # A negative constraint is not an executable task. If the
                    # model nevertheless materialized it as one child, omit it.
                    if (
                        reason.startswith("explicitly_negated_")
                        or reason == "negated_action"
                        or reason in {"informational_question", "capability_question", "capability_question_not_execution"}
                    ):
                        continue
                    rejected = (index, reason)
                    break
                validated_tasks.append(task)
            if rejected is None:
                break
            recovered = (
                []
                if recovery_attempted
                else _recover_explicit_sequence_tasks(user_text, self.scenario_catalog)
            )
            if recovered:
                tasks = recovered
                recovery_attempted = True
                continue
            index, reason = rejected
            return {
                "ok": False,
                "validation_ok": False,
                "executed": False,
                "device_state_changed": False,
                "skill": SEQUENCE_TOOL_NAME,
                "mode": "intent_rejected",
                "error": f"sequence_task_not_supported_by_user_intent:{index}:{reason}",
                "spoken_summary": "这组任务里有一项不是你明确要求的，所以我没有开始执行。",
            }

        tasks = validated_tasks
        if not tasks:
            return {
                "ok": False,
                "validation_ok": False,
                "executed": False,
                "device_state_changed": False,
                "skill": SEQUENCE_TOOL_NAME,
                "mode": "intent_rejected",
                "error": "sequence_contains_no_affirmative_task",
                "spoken_summary": "这句话里没有需要执行的正向设备动作，所以我没有操作。",
            }

        if not trusted_resume:
            self._emit_speech_event(
                {
                    "skill_name": SEQUENCE_TOOL_NAME,
                    "kind": "acknowledgement",
                    "text": build_sequence_start_speech(tasks, turn_id),
                    "task_count": len(tasks),
                }
            )
        records: list[dict[str, Any]] = []
        stopped = False
        for index, task in enumerate(tasks):
            if stopped:
                records.append(
                    {
                        "index": index,
                        "name": task["name"],
                        "arguments": task["arguments"],
                        "finished": False,
                        "succeeded": False,
                        "skipped": True,
                        "error": "previous_task_failed",
                    }
                )
                continue
            if index:
                prior_ok = bool(records[-1].get("succeeded"))
                # Successful internal transitions do not need narration; the
                # user already heard one acknowledgement and will receive one
                # aggregate result. Only a meaningful exception is announced.
                if not prior_ok:
                    self._emit_speech_event(
                        {
                            "skill_name": SEQUENCE_TOOL_NAME,
                            "kind": "progress",
                            "text": "上一项没完成，我按你的要求继续。",
                            "task_index": index,
                        }
                    )
            if task["name"] == SCENARIO_TOOL_NAME:
                result = self.invoke(
                    task["name"],
                    task["arguments"],
                    user_text,
                    turn_id,
                    prior_assistant_text,
                    True,
                    False,
                )
            else:
                # Only model-exposed atomic children enter the sequence, so
                # direct dispatch here cannot bypass protected scene routing.
                atomic_kwargs: dict[str, Any] = {"announce": False}
                # Keep compatibility with existing integrations/tests that
                # wrap the historical _invoke_atomic signature. Context is
                # only passed when it can actually contribute evidence.
                if str(prior_assistant_text or "").strip():
                    atomic_kwargs["prior_assistant_text"] = prior_assistant_text
                if trusted_resume:
                    atomic_kwargs["trusted_resume"] = True
                result = self._invoke_atomic(
                    task["name"],
                    task["arguments"],
                    user_text,
                    **atomic_kwargs,
                )
            succeeded = bool(result.get("ok") or result.get("validation_ok"))
            records.append(
                {
                    "index": index,
                    "name": task["name"],
                    "arguments": task["arguments"],
                    "finished": True,
                    "succeeded": succeeded,
                    "skipped": False,
                    "result": result,
                    "error": result.get("error"),
                }
            )
            if not succeeded and failure_policy == "stop":
                stopped = True

        completed = [item for item in records if item.get("finished")]
        all_succeeded = len(completed) == len(tasks) and all(item.get("succeeded") for item in completed)
        validation_ok = all_succeeded and all(
            bool((item.get("result") or {}).get("ok") or (item.get("result") or {}).get("validation_ok"))
            for item in completed
        )
        executed = any(bool((item.get("result") or {}).get("executed")) for item in completed)
        dry_run = validation_ok and not executed
        failed = next((item for item in completed if not item.get("succeeded")), None)
        skipped_count = sum(1 for item in records if item.get("skipped"))
        if dry_run:
            spoken = "安全模拟校验通过，但这组任务没有实际执行。"
        elif all_succeeded:
            spoken = build_sequence_success_summary(tasks, records)
        elif failed is not None:
            failed_summary = str((failed.get("result") or {}).get("spoken_summary") or "这一项没有完成。")
            suffix = f"所以后面的{skipped_count}项没有继续。" if skipped_count else ""
            spoken = failed_summary.rstrip("。") + "。" + suffix
        else:
            spoken = "这组任务没有完整执行。"
        return {
            "ok": bool(all_succeeded and executed),
            "validation_ok": validation_ok,
            "executed": executed,
            "device_state_changed": False if dry_run else None,
            "skill": SEQUENCE_TOOL_NAME,
            "mode": "dry_run" if dry_run else "execute",
            "failure_policy": failure_policy,
            "tasks": records,
            "structured_result": {"tasks": records, "task_count": len(tasks)},
            "result_is_authoritative": True,
            "error": str(failed.get("error") or "sequence_task_failed") if failed else None,
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
            "spoken_summary": spoken,
        }

    def _invoke_atomic(
        self,
        name: str,
        arguments: dict[str, Any],
        user_text: str,
        *,
        turn_id: str = "",
        prior_assistant_text: str = "",
        announce: bool = True,
        trusted_resume: bool = False,
    ) -> dict[str, Any]:
        started = time.monotonic()
        spec = self.specs.get(name)
        if spec is None:
            reason = self.unavailable.get(name, "not_registered")
            return {
                "ok": False,
                "skill": name,
                "error": f"unavailable_skill:{reason}",
                "spoken_summary": "这个功能现在还没准备好，所以我没有贸然执行。",
            }
        if trusted_resume:
            supported, support_reason = True, "trusted_resume"
        else:
            supported, support_reason = _atomic_intent_supported(
                name,
                arguments,
                user_text,
                prior_assistant_text=prior_assistant_text,
            )
        if not supported:
            rejection_speech = {
                "navigation_destination_missing": (
                    "我没听清要去哪个位置，所以没有启动导航。你可以说原点、客厅白墙或书房。"
                ),
                "navigation_destination_conflict": (
                    "我听到了导航指令，但目的地没有确认清楚，所以没有移动。"
                    "请再说一次原点、客厅白墙或书房。"
                ),
                "navigation_destination_unknown": (
                    "这个目的地不在已保存的导航点里，所以我没有移动。"
                    "目前可以去原点、客厅白墙或书房。"
                ),
                "navigation_list_not_requested": (
                    "我听到你想导航，但目的地没听清。"
                    "你要去原点、客厅白墙，还是书房？"
                ) if _navigation_predicate(user_text) else "你是想查看已保存的导航点吗？",
                "navigation_list_conflicts_with_destination": (
                    "我听到你是要导航，不是查列表。"
                    "请只说一次目的地：原点、客厅白墙或书房。"
                ),
            }.get(
                support_reason,
                "这句话没有明确要求这项设备操作，所以我没有执行。",
            )
            return {
                "ok": False,
                "validation_ok": False,
                "executed": False,
                "device_state_changed": False,
                "skill": name,
                "mode": "intent_rejected",
                "error": f"tool_not_supported_by_user_intent:{support_reason}",
                "clarification_required": support_reason in {
                    "navigation_destination_missing",
                    "navigation_destination_conflict",
                    "navigation_destination_unknown",
                    "navigation_list_not_requested",
                    "navigation_list_conflicts_with_destination",
                },
                "spoken_summary": rejection_speech,
            }
        schema = _normalized_schema(spec)
        allowed = set((schema.get("properties") or {}).keys())
        # These arguments are authored only by the protected fitness
        # procedures. The atomic fitness tools are hidden from the model, so
        # retaining them here cannot widen direct model access.
        if name in {"push_up", "pull_up", "squat"} and self.scenario_catalog is not None:
            allowed.update(
                {
                    "identity_camera",
                    "identity_policy",
                    "projector_after_identity",
                    "preparation_delay",
                    "initial_count",
                    "initial_elapsed_seconds",
                    "resume_from_interrupt",
                }
            )
        clean = {
            str(key): value for key, value in arguments.items()
            if str(key) in allowed and str(key) not in PLANNER_METADATA_ARGUMENTS
        }
        command = [
            sys.executable,
            str(self.runner_path),
            "--robot-project",
            str(self.robot_project),
            "--skill",
            name,
            "--arguments-json",
            json.dumps(clean, ensure_ascii=False),
        ]
        if not self.execute:
            command.append("--dry-run")
        if user_text:
            command.extend(["--user-text", user_text[:1000]])
        if announce:
            self._emit_speech_event(
                {
                    "skill_name": name,
                    "kind": "acknowledgement",
                    "text": build_skill_start_speech(name, clean, turn_id or self.current_turn_id),
                }
            )
        completed = self._run_request(name, clean, user_text, command)
        runner_result = _parse_runner_result(completed["stdout"])
        if runner_result is None:
            return {
                "ok": False,
                "skill": name,
                "mode": "execute" if self.execute else "dry_run",
                "error": completed.get("error") or "missing_runner_result",
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "spoken_summary": "执行服务这次没有正常返回，我停在了当前步骤，没有继续。",
            }
        runner_ok = bool(runner_result.get("ok")) and not completed.get("error")
        message = str(runner_result.get("message") or "")
        structured_result = runner_result.get("structured_result")
        if not isinstance(structured_result, dict):
            structured_result = {}
        authoritative_summary = str(runner_result.get("spoken_summary") or "").strip()
        if not self.execute and runner_ok:
            spoken = "安全模拟校验通过，但本次操作没有实际执行。"
            ok = False
            validation_ok = True
            error = "dry_run_only_not_executed"
            message = spoken
        elif runner_ok:
            spoken = authoritative_summary or message or "这项操作已经完成。"
            ok = True
            validation_ok = True
            error = None
        else:
            spoken = (
                authoritative_summary
                or message
                or "这项操作没有完成，我没有继续后面的动作。"
            )
            ok = False
            validation_ok = False
            # Preserve the executor's concrete failure (for example
            # cmd_vel_subscribers_0) instead of hiding it behind exit code 5.
            error = runner_result.get("error") or completed.get("error")
        return {
            "ok": ok,
            "validation_ok": validation_ok,
            "executed": bool(self.execute and runner_ok),
            "device_state_changed": False if not self.execute else None,
            "skill": name,
            "arguments": clean,
            "mode": "execute" if self.execute else "dry_run",
            "resources": runner_result.get("resources") or spec.get("resources") or [],
            "message": message,
            "structured_result": structured_result,
            "result_is_authoritative": bool(structured_result),
            "error": error,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "spoken_summary": spoken,
            "transport": completed.get("transport") or "subprocess",
        }

    def _run_request(
        self,
        name: str,
        arguments: dict[str, Any],
        user_text: str,
        command: list[str],
    ) -> dict[str, Any]:
        if self.backend != "subprocess":
            completed = self._run_host(name, arguments, user_text)
            if completed.get("dispatch_state") == "completed":
                return completed
            # Fallback is safe only when connecting failed before a request was
            # sent. Never repeat an action after an uncertain socket failure.
            if self.backend == "host" or completed.get("dispatch_state") != "not_sent":
                return completed
        return self._run(command)

    def _run_host(self, name: str, arguments: dict[str, Any], user_text: str) -> dict[str, Any]:
        request = {
            "op": "invoke",
            "request_id": uuid.uuid4().hex,
            "skill": name,
            "arguments": arguments,
            "dry_run": not self.execute,
            "user_text": user_text[:1000],
        }
        sent = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.timeout)
                conn.connect(str(self.host_socket))
                conn.sendall(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                sent = True
                stream = conn.makefile("rb")
                result = None
                while True:
                    response = stream.readline(1024 * 1024 + 1)
                    if not response or len(response) > 1024 * 1024:
                        raise ValueError("invalid_skill_host_response")
                    value = json.loads(response.decode("utf-8"))
                    if value.get("type") == "skill_event":
                        event = value.get("event")
                        if isinstance(event, dict) and self.event_callback is not None:
                            self.event_callback(dict(event))
                        continue
                    if value.get("type") == "final":
                        value.pop("type", None)
                        result = value
                        break
                if result is None:
                    raise ValueError("missing_skill_host_final_response")
            if not isinstance(result, dict):
                raise ValueError("skill_host_response_not_object")
            return {
                "returncode": 0 if result.get("ok") else 5,
                "stdout": RUNNER_RESULT_PREFIX + json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n",
                "stderr": "",
                "error": None if result.get("ok") else result.get("error"),
                "transport": "skill_host",
                "dispatch_state": "completed",
            }
        except Exception as exc:
            error = f"skill_host_{type(exc).__name__}:{exc}"
            return {
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "error": error,
                "transport": "skill_host",
                "dispatch_state": "sent_unknown" if sent else "not_sent",
            }

    def _run(self, command: list[str]) -> dict[str, Any]:
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        with self._process_lock:
            self._processes.add(process)
        try:
            try:
                stdout, stderr = process.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self._terminate(process)
                stdout, stderr = process.communicate(timeout=3)
                return {
                    "returncode": process.returncode,
                    "stdout": stdout,
                    "stderr": stderr[-2000:],
                    "error": f"skill_timeout_after_{self.timeout:g}s",
                }
            return {
                "returncode": process.returncode,
                "stdout": stdout,
                "stderr": stderr[-2000:],
                "error": None if process.returncode == 0 else f"skill_runner_exit_{process.returncode}",
            }
        finally:
            with self._process_lock:
                self._processes.discard(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                process.kill()

    def cancel_all(self) -> None:
        if self.backend != "subprocess" and self.host_socket.is_socket():
            request = {
                "op": "cancel_active",
                "request_id": f"cancel_{uuid.uuid4().hex}",
            }
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                    conn.settimeout(min(3.0, self.timeout))
                    conn.connect(str(self.host_socket))
                    conn.sendall(
                        json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        + b"\n"
                    )
                    conn.makefile("rb").readline(1024 * 1024 + 1)
            except (OSError, TimeoutError, ValueError):
                pass
        with self._process_lock:
            processes = list(self._processes)
        for process in processes:
            self._terminate(process)
