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

from scenario_engine import (
    SCENARIO_TOOL_NAME,
    ScenarioCatalog,
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


def _intent_evidence(text: str, pattern: str, terms: Sequence[str] = ()) -> bool:
    """Combine exact syntax evidence with the existing pinyin-aware matcher."""

    return bool(
        re.search(pattern, text)
        or any(_contains_term(text, term) for term in terms if str(term).strip())
    )


def _contains_any_term(text: str, terms: Sequence[str]) -> bool:
    return any(_contains_term(text, term) for term in terms if str(term).strip())


def _action_explicitly_negated(text: str, terms: Sequence[str]) -> bool:
    """Check negation inside the action's own spoken clause."""

    clauses = re.split(r"[，。！？!?、；;]|然后|接着|之后|随后|但是|不过|但(?=[^是])", str(text or ""))
    for clause in clauses:
        compact = _intent_text(clause)
        for term in terms:
            target = _intent_text(term)
            position = compact.find(target)
            if position < 0:
                continue
            prefix = compact[:position]
            if re.search(r"(?:不要|别|不用|无需|不需要|不想|不许|禁止)(?:再|去|给我|帮我)?$", prefix):
                return True
            if re.search(r"(?:不要|别|不用|无需|不需要|不想|不许|禁止).{0,5}$", prefix):
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
    return bool(
        re.search(r"导航|前往|过去|去往|回到|回原点|\bgo\b", compact)
        or re.search(r"(?:^|先|然后|再)(?:请你|麻烦你|帮我)?(?:去|到|回)", compact)
        or _contains_any_term(compact, ("导航到", "去客厅", "去书房", "回原点", "前往"))
    )


def _navigation_point_evidence(text: str, point: str) -> bool:
    if not _navigation_predicate(text):
        return False
    terms = POINT_SPEECH_TERMS.get(str(point), ())
    return _contains_any_term(_intent_text(text), terms)


def _explicit_navigation_task(user_text: str) -> dict[str, Any] | None:
    text = _intent_text(user_text)
    if not _navigation_predicate(text):
        return None
    candidates = (
        ("study_projection", POINT_SPEECH_TERMS["study_projection"]),
        ("origin", POINT_SPEECH_TERMS["origin"]),
        ("white_wall", POINT_SPEECH_TERMS["white_wall"]),
    )
    matched: list[tuple[int, str]] = []
    for point, terms in candidates:
        if _contains_any_term(text, terms):
            matched.append((_term_position(text, terms), point))
    if not matched:
        return None
    _position, point = min(matched)
    return {"name": "navigation_goto", "arguments": {"point": point}}


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


def _task_position(task: dict[str, Any], user_text: str, fallback: int) -> int:
    name = str(task.get("name") or "")
    arguments = dict(task.get("arguments") or {})
    action = str(arguments.get("action") or "").lower()
    if name == SCENARIO_TOOL_NAME:
        scenario = str(arguments.get("scenario") or "")
        terms = {
            "meeting_projection_stop": (
                "关闭会议投影", "关掉会议投影", "关闭投影", "关掉投影", "结束投影", "停止投影",
            ),
            "meeting_projection": ("会议投影", "投影会议内容", "开始投影"),
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
        return _term_position(user_text, ("关灯", "开灯", "客厅灯", "灯", "照明"), fallback)
    if name == "media_player":
        return _term_position(user_text, ("音乐", "歌曲", "视频", "电影", "播放", "暂停"), fallback)
    if name.startswith("reminder_"):
        return _term_position(user_text, ("提醒", "闹钟"), fallback)
    if name == "realtime_information":
        terms = {
            "location": ("当前位置", "机器人位置", "现在在哪"),
            "current_time": ("现在几点", "几点", "时间", "日期"),
            "weather": ("天气", "下雨", "气温"),
        }.get(action, ("查询",))
        return _term_position(user_text, terms, fallback)
    if name in {"pet_tracking", "person_tracking"}:
        return _term_position(user_text, ("停止跟踪", "停止跟随", "跟踪", "跟随"), fallback)
    if "camera" in name:
        terms = ("录像", "录视频", "录一段", "录五秒") if name.endswith("record") else ("拍照", "拍张", "照片")
        return _term_position(user_text, terms, fallback)
    if name == "face_recognition":
        return _term_position(user_text, ("我是谁", "识别", "看看我"), fallback)
    if name == "feeder_control":
        return _term_position(user_text, ("投食", "喂", "出粮"), fallback)
    return fallback


def _deduplicate_tasks(tasks: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for task in tasks:
        normalized = {
            "name": str(task.get("name") or "").strip(),
            "arguments": dict(task.get("arguments") or {}),
        }
        key = (normalized["name"], json.dumps(normalized["arguments"], ensure_ascii=False, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        values.append(normalized)
    return values


def _repair_sequence_tasks(
    tasks: Sequence[dict[str, Any]],
    user_text: str,
    catalog: ScenarioCatalog | None,
) -> list[dict[str, Any]]:
    """Repair representation errors while preserving only spoken actions."""

    repaired: list[dict[str, Any]] = []
    explicit_navigation = _explicit_navigation_task(user_text)
    explicit_light = _explicit_light_task(user_text, catalog)
    text = _intent_text(user_text)
    pet_stop = bool(
        re.search(r"(?:停止|结束|别再|不要再|不用再).{0,5}(?:跟踪|跟随|追踪).{0,5}(?:豆豆|狗|宠物)", text)
        or _contains_any_term(text, ("停止跟踪豆豆", "停止跟随豆豆", "别再跟着狗"))
    )

    for raw in tasks:
        task = {"name": str(raw.get("name") or "").strip(), "arguments": dict(raw.get("arguments") or {})}
        if task["name"] == "navigation_goto" and explicit_navigation is not None:
            # ASR wording can leak into the model argument (for example
            # point="苏北").  Canonicalize from the user's transcript before
            # validation and never pass an arbitrary model point through.
            task["arguments"] = dict(explicit_navigation["arguments"])
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

    repaired = _deduplicate_tasks(repaired)

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
        indexed = list(enumerate(repaired))
        indexed.sort(key=lambda pair: (_task_position(pair[1], user_text, 10**9 + pair[0]), pair[0]))
        repaired = [task for _index, task in indexed]
    return _deduplicate_tasks(repaired)


def _recover_explicit_sequence_tasks(user_text: str, catalog: ScenarioCatalog | None) -> list[dict[str, Any]]:
    """Conservative fallback for a malformed model sequence call.

    Recovery is attempted only after Qwen already selected the sequence tool,
    and only unambiguous positive actions found in the transcript are kept.
    """

    text = _intent_text(user_text)
    candidates: list[dict[str, Any]] = []
    matched_scenario = None
    scenario_owns_navigation = False
    if catalog is not None:
        matched_scenario = catalog.match(text)
        if matched_scenario and matched_scenario != "living_room_light_service":
            scenario_arguments = {"scenario": matched_scenario}
            scenario_arguments.update(catalog.infer_arguments(matched_scenario, text))
            candidates.append({"name": SCENARIO_TOOL_NAME, "arguments": scenario_arguments})
            procedure = catalog.procedures.get(matched_scenario, {})
            scenario_owns_navigation = any(
                str(step.get("skill") or "") == "navigation_goto"
                for step in procedure.get("steps", [])
                if isinstance(step, dict)
            )
        elif catalog._has_topic_evidence("meeting_projection_stop", text):
            candidates.append({"name": SCENARIO_TOOL_NAME, "arguments": {"scenario": "meeting_projection_stop"}})
    navigation = _explicit_navigation_task(text)
    if navigation is not None and not scenario_owns_navigation:
        candidates.append(navigation)
    light = _explicit_light_task(text, catalog)
    if light is not None:
        candidates.append(light)
    if re.search(r"音乐|歌曲|听歌|放歌", text):
        action = "stop" if re.search(r"停止|结束|关掉|不听", text) else "play_music"
        candidates.append({"name": "media_player", "arguments": {"action": action}})
    if re.search(r"天气|下雨|降雨|气温|温度", text):
        candidates.append({"name": "realtime_information", "arguments": {"action": "weather"}})
    if re.search(r"几点|时间|日期|年月日|星期|几月几日|几号", text):
        candidates.append({"name": "realtime_information", "arguments": {"action": "current_time"}})
    if re.search(
        r"(?:查|查询|看看|告诉我).{0,5}(?:当前位置|当前定位|所在位置|位置)|"
        r"机器人.{0,4}(?:在哪|哪里|位置)",
        text,
    ):
        candidates.append({"name": "realtime_information", "arguments": {"action": "location"}})
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
    pet_stop = bool(re.search(r"(?:停止|结束|别再|不要再|不用再).{0,5}(?:跟踪|跟随|追踪).{0,5}(?:豆豆|狗|宠物)", text))
    person_stop = bool(re.search(r"(?:停止|结束|别再|不要再|不用再).{0,5}(?:跟踪|跟随|追踪).{0,5}(?:人|他|她|面前)", text))
    if pet_stop:
        candidates.append({"name": "pet_tracking", "arguments": {"action": "stop"}})
    if person_stop:
        candidates.append({"name": "person_tracking", "arguments": {"action": "stop"}})
    if re.search(r"拍照|拍张|照片|照相|合影", text):
        prefix = "back_" if re.search(r"后置|后摄|后面", text) else "front_" if re.search(r"前置|前摄|前面", text) else ""
        candidates.append({"name": f"{prefix}camera_capture", "arguments": {}})
    if re.search(r"录像|录.{0,12}视频|拍视频|录制", text):
        prefix = "back_" if re.search(r"后置|后摄|后面", text) else "front_" if re.search(r"前置|前摄|前面", text) else ""
        candidates.append({"name": f"{prefix}camera_record", "arguments": {}})
    if re.search(r"我是谁|认得我|认识我|知道我是谁|识别.{0,3}(?:我|人脸|身份)", text):
        candidates.append({"name": "face_recognition", "arguments": {}})
    movement_actions = (
        ("move_forward", r"前进|往前(?:走|移动)?|向前(?:走|移动)?"),
        ("move_backward", r"后退|往后(?:走|移动)?|向后(?:走|移动)?|倒退"),
        ("move_left", r"左转|向左转|往左转"),
        ("move_right", r"右转|向右转|往右转"),
    )
    for name, pattern in movement_actions:
        if re.search(pattern, text):
            candidates.append({"name": name, "arguments": {}})
    if re.search(r"抬头|向上看", text):
        candidates.append({"name": "head_control", "arguments": {"action": "up"}})
    elif re.search(r"低头|向下看", text):
        candidates.append({"name": "head_control", "arguments": {"action": "down"}})
    elif re.search(r"平视|恢复水平|头.{0,3}回正", text):
        candidates.append({"name": "head_control", "arguments": {"action": "level"}})

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

    negatable_actions = {
        "navigation_goto": ("导航", "前往", "去书", "去客厅", "回原点", "到白墙"),
        "move_forward": ("前进", "往前", "向前"),
        "move_backward": ("后退", "往后", "向后", "倒退"),
        "move_left": ("左转", "向左转", "往左转"),
        "move_right": ("右转", "向右转", "往右转"),
        "head_control": (
            "抬头", "低头", "平视", "回正", "向上看", "向下看", "视线往上", "视线往下",
            "镜头往上", "镜头往下", "看正前方", "恢复正常角度",
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
        "face_recognition": r"我是谁|认得我|认识我|认出我|看出我|知道我是谁|面前.{0,4}(?:谁|人)|识别.{0,3}(?:我|人脸|身份)|看看.{0,6}(?:我|谁|认不认得)",
        "face_registration": r"注册|登记|录入|添加|保存|记住.{0,3}(?:人脸|脸|身份)",
        "camera_capture": r"拍照|拍张|照片|照相|合影",
        "front_camera_capture": r"拍照|拍张|照片|照相|合影",
        "back_camera_capture": r"拍照|拍张|照片|照相|合影",
        "camera_record": r"录像|录.{0,12}视频|拍视频|录制",
        "front_camera_record": r"录像|录.{0,12}视频|拍视频|录制",
        "back_camera_record": r"录像|录.{0,12}视频|拍视频|录制",
        "fan_control": r"风扇|吹风|风机",
        "feeder_control": r"投食|喂|出粮|狗粮|吃饭|开饭|投食器",
        "head_control": r"抬头|低头|平视|回正|头部|脑袋|视线|镜头|向上看|向下看|往高处看|看正前方|恢复水平|恢复正常角度",
        "move_forward": r"前进|往前|向前",
        "move_backward": r"后退|往后|向后|倒退",
        "move_left": r"左转|向左|往左",
        "move_right": r"右转|向右|往右",
        "navigation_goto": r"导航|前往|过去|去往|到|去|回",
        "person_tracking": r"跟踪|跟随|追踪|找人|寻找.{0,3}人|跟着",
        "pet_tracking": r"豆豆|小狗|狗狗|宠物|找狗|跟踪狗|跟着狗",
        "projector_control": r"投影|投屏|ppt|幻灯|会议画面|会议内容|墙上.{0,4}内容|大屏|正在放的内容",
        "reminder_schedule": r"提醒|闹钟|到点叫我|记得叫我",
        "reminder_query": r"提醒|闹钟|待办",
        "reminder_cancel": r"提醒|闹钟|待办",
        "media_player": r"音乐|歌曲|听歌|视频|电影|短片|节目|正在播|暂停|继续播放|恢复播放|下一首|换一首|播放器",
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
        "feeder_control": ("投食", "喂食", "狗粮", "投食器"),
        "head_control": ("抬头", "低头", "平视", "回正", "视线往上", "视线往下", "看正前方"),
        "move_forward": ("前进", "往前", "向前"),
        "move_backward": ("后退", "往后", "倒退"),
        "move_left": ("左转", "向左"),
        "move_right": ("右转", "向右"),
        "navigation_goto": ("导航", "前往", "回原点"),
        "person_tracking": ("跟踪人", "跟随人", "追踪人"),
        "pet_tracking": ("豆豆", "小狗", "宠物", "找狗"),
        "projector_control": ("投影", "投屏", "幻灯", "会议内容", "墙上内容", "大屏内容"),
        "reminder_schedule": ("设置提醒", "提醒我", "闹钟"),
        "reminder_query": ("查询提醒", "查看提醒"),
        "reminder_cancel": ("取消提醒", "删除提醒"),
        "media_player": ("播放音乐", "听歌", "播放视频", "暂停播放", "继续播放", "正在播的内容"),
    }
    evidence = general_evidence.get(name)
    current_evidence = bool(
        evidence and _intent_evidence(text, evidence, general_terms.get(name, ()))
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
        if action in {"status", "check"} and not re.search(r"状态|开着|关着|亮着|有没有开", text):
            return False, "light_status_not_requested"

    if name == "realtime_information":
        action = str(arguments.get("action") or "").strip().lower()
        evidence = {
            "location": r"(?:查|查询|看看|告诉我).{0,6}(?:当前位置|当前定位|所在位置|位置|本机定位)|(?:你|机器人|本机).{0,6}(?:在哪|哪里|位置|什么地方)|(?:在哪|哪里).{0,4}(?:你|机器人)|定位(?:在哪|信息|状态)",
            "current_time": r"几点|时间|日期|年月日|星期[几几一二三四五六日天]?|几月几日|几号|现在是",
            "weather": r"天气|下雨|降雨|温度|气温|晴天|阴天|刮风|台风",
            "nearby": r"附近|周边|就近",
            "traffic": r"路况|交通|堵车|拥堵",
        }.get(action)
        if evidence and not re.search(evidence, text):
            return False, f"missing_realtime_{action}_evidence"

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
        requested_back = bool(re.search(r"后置|后摄|后面|背后", text) or _contains_term(text, "后摄"))
        requested_front = bool(re.search(r"前置|前摄|前面|面前", text) or _contains_term(text, "前摄"))
        selected_back = name.startswith("back_") or str(arguments.get("camera_name") or arguments.get("camera") or "").lower() == "back"
        selected_front = name.startswith("front_") or str(arguments.get("camera_name") or arguments.get("camera") or "").lower() == "front"
        if selected_back and not requested_back:
            return False, "back_camera_not_requested"
        if selected_front and requested_back and not requested_front:
            return False, "camera_direction_conflict"

    if name == "head_control":
        action = str(arguments.get("action") or "").strip().lower()
        action_evidence = {
            "up": r"抬头|向上看|往上看|往高处看|视线.{0,4}(?:上|高)|镜头.{0,4}(?:上|高)|头.{0,3}抬",
            "down": r"低头|向下看|往下看|视线.{0,4}下|镜头.{0,4}下|头.{0,3}低",
            "level": r"平视|回正|摆正|看正前方|恢复水平|恢复正常角度|头.{0,3}正",
            "angle": r"角度|\d+(?:\.\d+)?度",
        }.get(action)
        head_terms = {
            "up": ("抬头", "向上看", "视线往上", "往高处看"),
            "down": ("低头", "向下看", "视线往下"),
            "level": ("平视", "回正", "摆正", "看正前方", "恢复正常角度"),
            "angle": ("角度",),
        }.get(action, ())
        if action_evidence and not _intent_evidence(text, action_evidence, head_terms):
            return False, "head_action_conflict"

    if name == "projector_control":
        action = str(arguments.get("action") or "").strip().lower()
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
            "play_music": r"音乐|歌曲|听歌|唱首歌|放歌",
            "play_video": r"视频|电影|短片|节目",
            "pause": r"暂停|停一下",
            "resume": r"继续|恢复",
            "next": r"下一首|换一首|换歌",
            "stop": r"停止|停了|结束|关掉|到这里|到这|先这样|不用继续|不听|不看",
            "list": r"有什么|列表|哪些|可以播放",
            "status": r"状态|在播什么|播放到哪",
        }.get(action)
        media_terms = {
            "play_music": ("播放音乐", "听歌", "放歌"),
            "play_video": ("播放视频", "看视频", "看电影"),
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
    if name == "reminder_query" and not re.search(r"查|查询|看看|列一下|都有|安排了|哪些|什么|多少|有没有", text):
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
            "nearby": "查询附近地点",
            "traffic": "查询交通",
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
                f"好，我这就出发去{destination}。",
                f"明白，我现在前往{destination}。",
                f"收到，准备去{destination}，我出发了。",
                f"行，我马上去{destination}。",
            ),
            key,
        )
    if phrase.startswith(("查询", "看看", "查看", "读取")):
        return _pick_variant(
            (f"我马上{phrase}。", f"好，我来{phrase}。", f"稍等，我现在{phrase}。"),
            key,
        )
    return _pick_variant(
        (f"收到，我现在{phrase}。", f"好，我这就{phrase}。", f"明白，马上{phrase}。"),
        key,
    )


def build_sequence_start_speech(tasks: list[dict[str, Any]], variation_key: str = "") -> str:
    phrases = [task_future_phrase(str(item["name"]), dict(item.get("arguments") or {})) for item in tasks]
    if len(phrases) == 2:
        return _pick_variant(
            (
                f"收到，我先{phrases[0]}，完成后再{phrases[1]}。",
                f"好，我按你说的来：先{phrases[0]}，再{phrases[1]}。",
                f"明白，这两件事按顺序处理，先{phrases[0]}，再{phrases[1]}。",
            ),
            f"{variation_key}|sequence|{phrases}",
        )
    return _pick_variant(
        (
            f"收到，我会按顺序完成这{len(phrases)}项任务，先{phrases[0]}。",
            f"明白，这{len(phrases)}项我依次处理，先{phrases[0]}。",
            f"好，我从{phrases[0]}开始，一项一项完成。",
        ),
        f"{variation_key}|sequence|{phrases}",
    )

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
RUNNER_RESULT_PREFIX = "QWEN_SKILL_RUNNER_RESULT="
SYNTHETIC_PROPERTIES: dict[str, dict[str, dict[str, Any]]] = {
    "realtime_information": {
        "action": {
            "type": "string",
            "enum": ["current_time", "weather", "location", "nearby", "traffic"],
            "description": "实时查询类型。必须根据用户问题选择。",
        },
        "query": {"type": "string", "description": "保留用户的原始查询文本。"},
        "location": {"type": "string", "description": "用户明确指定的地点；未指定则省略。"},
        "latitude": {"type": "number", "description": "用户明确指定的纬度；通常省略。"},
        "longitude": {"type": "number", "description": "用户明确指定的经度；通常省略。"},
        "radius": {"type": "integer", "minimum": 100, "maximum": 50000},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
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
        "查询联网校时后的当前日期时间、实时天气、机器人配置的粗略地理位置、附近地点或交通。"
        "location 只回答‘机器人现在在哪里、机器人当前位置’这一类机器人自身位置问题，"
        "绝不用于查询公司、机构、人物、手机、用户、宠物或其他设备在哪里；公司和知识问题直接聊天回答。"
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
        properties["action"]["enum"] = ["current_time", "weather", "location", "nearby", "traffic"]
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
        self.homecoming_greeting_consumed = False

    @property
    def tool_schemas(self) -> list[dict[str, Any]]:
        protected = {"push_up", "pull_up", "squat", "welcome_projection", "projector_control"}
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
                    if re.search(r"不管|不论|无论|即使|哪怕", _intent_text(user_text))
                    else "stop"
                ),
            },
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
        # Realtime models occasionally emit a valid scenario name directly as
        # the function name (for example ``meeting_projection``) instead of
        # calling ``run_robot_scenario`` with a scenario argument.  Sequence
        # calls already canonicalize this harmless representation difference;
        # apply the same rule to a top-level call so the protected compiler,
        # intent evidence checks and dependency gates still run.
        if (
            self.scenario_catalog is not None
            and name != SCENARIO_TOOL_NAME
            and name in self.scenario_catalog.procedures
        ):
            arguments = {**dict(arguments), "scenario": name}
            name = SCENARIO_TOOL_NAME
        if name == SEQUENCE_TOOL_NAME:
            return self._invoke_sequence(
                arguments,
                user_text=user_text,
                turn_id=turn_id,
                prior_assistant_text=prior_assistant_text,
            )
        if self.scenario_catalog is not None and self.scenario_executor is not None:
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
            affirmative = normalized_user.lower() in {
                "好", "好的", "好啊", "可以", "行", "没问题", "开始吧", "那就开始吧", "就这样吧"
            }
            contextual = (
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
                        "spoken_summary": "我还没完全听明白你想启动哪个场景，所以先没有动作。你直接说想做什么就行。",
                    }
            scenario = matched or contextual or requested or protected
            if scenario:
                if (
                    scenario == "homecoming_welcome"
                    and self.homecoming_greeting_consumed
                    and not self.scenario_catalog.explicit_homecoming_replay(user_text)
                ):
                    return {
                        "ok": True,
                        "validation_ok": True,
                        "executed": True,
                        "device_state_changed": False,
                        "skill": SCENARIO_TOOL_NAME,
                        "scenario": "homecoming_welcome",
                        "mode": "greeting_only",
                        "spoken_summary": "我在呢，今天想让我陪你做点什么？",
                    }
                inferred = self.scenario_catalog.infer_arguments(scenario, user_text)
                allowed = set(
                    (self.scenario_catalog.procedures.get(scenario, {}).get("parameters") or {}).keys()
                )
                clean = {
                    key: value
                    for key, value in arguments.items()
                    if key in allowed
                }
                # Arguments inferred from explicit user words are authoritative
                # over model-supplied defaults or guesses.
                clean = {**clean, **inferred}
                if scenario == "meeting_projection":
                    if "point" not in inferred:
                        # A partial atomic plan may carry a model-invented point.
                        # Only explicit location words in the user's transcript
                        # may override the meeting's catalog default.
                        clean.pop("point", None)
                    if inferred.get("stay_put"):
                        # Explicit no-navigation language always wins and point
                        # is left to the current physical location.
                        clean["stay_put"] = True
                        clean.pop("point", None)
                    else:
                        # stay_put is opt-in: the model cannot silently prevent
                        # the catalog's default navigation to the study.
                        clean.pop("stay_put", None)
                if scenario == "homecoming_welcome":
                    self.homecoming_greeting_consumed = True
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
        if name == "navigation_goto":
            explicit_navigation = _explicit_navigation_task(user_text)
            if explicit_navigation is not None:
                arguments = {
                    **dict(arguments),
                    "point": explicit_navigation["arguments"]["point"],
                }
        return self._invoke_atomic(
            name,
            arguments,
            user_text,
            turn_id=turn_id,
            prior_assistant_text=prior_assistant_text,
        )

    def _emit_speech_event(self, event: dict[str, Any]) -> None:
        if self.event_callback is None:
            return
        value = dict(event)
        value.setdefault("skill_name", SEQUENCE_TOOL_NAME)
        self.event_callback(value)

    def _sequence_child_names(self) -> set[str]:
        protected = {"push_up", "pull_up", "squat", "welcome_projection", "projector_control"}
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
    ) -> dict[str, Any]:
        started = time.monotonic()
        self.current_turn_id = str(turn_id or self.current_turn_id or "")
        raw_tasks = arguments.get("tasks")
        failure_policy = str(arguments.get("failure_policy") or "stop").strip().lower()
        if failure_policy not in {"stop", "continue"}:
            failure_policy = "stop"
        compact_user_text = "".join(str(user_text or "").split())
        explicit_continue = (
            "继续" in compact_user_text
            and any(marker in compact_user_text for marker in ("不管", "不论", "无论", "即使", "哪怕"))
            and any(marker in compact_user_text for marker in ("成功", "失败", "完成", "没完成", "没到", "未到", "到达", "结果"))
        )
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

        allowed = self._sequence_child_names()
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

        tasks = _repair_sequence_tasks(tasks, user_text, self.scenario_catalog)
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
                supported = True
                reason = "explicit_or_unrestricted"
                if child_name == SCENARIO_TOOL_NAME and self.scenario_catalog is not None:
                    requested = str(child_arguments.get("scenario") or "")
                    supported, reason = self.scenario_catalog.model_scenario_supported(
                        requested,
                        user_text,
                        allow_additional_intents=True,
                        prior_context=prior_assistant_text,
                    )
                else:
                    supported, reason = _atomic_intent_supported(
                        child_name,
                        child_arguments,
                        user_text,
                        in_sequence=True,
                        prior_assistant_text=prior_assistant_text,
                    )
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
                prefix = "上一项已经完成，" if prior_ok else "上一项没有完成，但按你的要求继续，"
                self._emit_speech_event(
                    {
                        "skill_name": SEQUENCE_TOOL_NAME,
                        "kind": "progress",
                        "text": prefix + f"现在{task_future_phrase(task['name'], task['arguments'])}。",
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
        summaries = [
            str((item.get("result") or {}).get("spoken_summary") or "").strip()
            for item in completed
            if str((item.get("result") or {}).get("spoken_summary") or "").strip()
        ]
        failed = next((item for item in completed if not item.get("succeeded")), None)
        skipped_count = sum(1 for item in records if item.get("skipped"))
        if dry_run:
            spoken = "安全模拟校验通过，但这组任务没有实际执行。"
        elif all_succeeded:
            spoken = "；".join(summaries) or f"这{len(tasks)}项任务已经按顺序完成。"
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
        clean = {str(key): value for key, value in arguments.items() if str(key) in allowed}
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
