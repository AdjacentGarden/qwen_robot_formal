from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

from intent_policy import is_retrospective_query

try:
    from pypinyin import Style, lazy_pinyin
except ImportError:  # pragma: no cover - requirements.txt installs this in production.
    Style = None
    lazy_pinyin = None

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - exact routing remains available during recovery.
    fuzz = None


SCENARIO_TOOL_NAME = "run_robot_scenario"
CANONICAL_POINTS = {"origin", "white_wall", "study_projection"}
LEGACY_POINT_ALIASES = {
    "living_room_entry_a": "white_wall",
    "living_room_projection_b": "white_wall",
    "wall": "white_wall",
    "study": "study_projection",
    "dining_room": "origin",
}
SCENARIO_TOPIC_TERMS = {
    "living_room_light_service": ("客厅", "灯光", "照明", "开灯", "亮起来", "太暗", "太黑"),
    "push_up_companion": (
        "俯卧撑", "做运动", "锻炼", "健身", "活动筋骨", "活动一下", "热热身", "练一组",
    ),
    "pull_up_companion": ("引体向上", "引体", "单杠"),
    "squat_companion": ("深蹲", "下蹲", "蹲起"),
    "find_pet": ("豆豆", "豆儿", "小狗", "宠物", "找狗", "毛孩子", "小家伙"),
    "find_pet_at": ("豆豆", "豆儿", "小狗", "宠物", "找狗", "毛孩子", "小家伙"),
    "find_pet_here": ("豆豆", "豆儿", "小狗", "宠物", "找狗", "毛孩子", "小家伙"),
    "find_and_feed_doudou": (
        "喂饭", "喂食", "吃饭", "饿了", "狗粮", "开饭", "加餐", "添粮",
    ),
    "meeting_projection": (
        "会议", "开会", "开个会", "讨论", "汇报", "演示", "投屏", "投影", "ppt", "幻灯",
        "会议画面", "墙上内容", "大屏内容",
    ),
    "meeting_projection_stop": (
        "会议", "投屏", "投影", "ppt", "幻灯", "会议画面", "墙上内容", "大屏内容",
    ),
    "movie_projection": (
        "电影", "影片", "看电影", "放电影", "电影投影", "电影画面",
    ),
    "movie_projection_pause": (
        "电影", "影片", "电影投影", "电影画面",
    ),
    "movie_projection_resume": (
        "电影", "影片", "电影投影", "电影画面",
    ),
    "movie_projection_stop": (
        "电影", "影片", "电影投影", "电影画面",
    ),
    "homecoming_welcome": ("回家", "到家", "回来了", "下班", "欢迎回家", "理想同学"),
    "rest_lighting": (
        "休息", "歇一会", "歇会", "睡一会", "困了", "躺一会", "放松一下", "缓一缓", "眯一会",
    ),
}
SHORT_AFFIRMATIONS = {
    "好", "好的", "好啊", "可以", "行", "没问题", "开始吧", "那就开始吧", "就这样吧",
}
SCENARIO_START_SPEECH = {
    "homecoming_welcome": (
        "欢迎回家。",
    ),
    "push_up_companion": (
        "好，我们开始运动。",
        "来吧，活动一下。",
        "好，准备做俯卧撑。",
    ),
    "pull_up_companion": (
        "好，我们开始运动。",
        "来吧，准备做引体向上。",
    ),
    "squat_companion": (
        "好，我们开始运动。",
        "来吧，准备做深蹲。",
    ),
    "find_pet": (
        "好，我去找豆豆。",
        "我去看看豆豆在哪儿。",
        "收到，这就去找豆豆。",
    ),
    "find_pet_at": ("好，我去那里找豆豆。", "收到，我去指定地点看看。"),
    "find_pet_here": ("好，我就在这里找。", "收到，我在原地看看。"),
    "find_and_feed_doudou": (
        "好，我去找豆豆，找到就喂它。",
        "收到，我去看看豆豆。",
    ),
    "meeting_projection": (
        "好，我去准备会议投影。",
        "收到，开始准备会议。",
        "好，会议投影马上准备。",
    ),
    "meeting_projection_here": (
        "好，就在这里开始投影。",
        "收到，我在当前位置准备会议。",
    ),
    "meeting_projection_stop": (
        "好，我来关闭投影。",
        "好的，结束投影。",
    ),
    "movie_projection": (
        "好，我来准备电影投影。",
        "行，我们看会儿电影放松一下。",
    ),
    "movie_projection_here": (
        "好，就在这里播放电影。",
        "行，我在当前位置准备电影投影。",
    ),
    "movie_projection_pause": ("好，电影先暂停。",),
    "movie_projection_resume": ("好，继续播放电影。",),
    "movie_projection_stop": ("好，我来结束电影播放。",),
    "rest_lighting": (
        "好的，我去客厅帮你把灯关了。",
        "好，你先休息，我去客厅把灯光调好。",
        "你先放松一下，我这就去客厅帮你关灯。",
        "好的，我去到客厅帮你把灯关好。",
    ),
    "living_room_light_service": (
        "好，我去客厅调灯。",
        "收到，我来处理客厅灯光。",
    ),
}

# Successful results may vary in tone, but every option carries exactly the
# same device facts.  Failures stay catalog-driven so their reason can never
# be softened, omitted, or accidentally changed by stylistic variation.
SCENARIO_OUTCOME_SPEECH = {
    ("homecoming_welcome", "completed"): ("",),
    ("meeting_projection", "all_success_here"): (
        "会议内容投好了。", "投影准备好了。", "会议画面已经出来了。",
    ),
    ("meeting_projection", "all_success"): (
        "会议内容投好了。", "投影准备好了。", "会议画面已经出来了。",
    ),
    ("meeting_projection_stop", "all_success"): (
        "投影关好了。", "会议投影已经结束。", "好，投影已关闭。",
    ),
    ("movie_projection", "all_success_here"): (
        "电影已经在当前位置播放了。", "电影画面已经投出来了。",
    ),
    ("movie_projection", "all_success"): (
        "电影已经开始播放了。", "电影画面已经投好了。",
    ),
    ("movie_projection_pause", "all_success"): ("电影已经暂停了。",),
    ("movie_projection_resume", "all_success"): ("电影继续播放了。",),
    ("movie_projection_stop", "all_success"): (
        "电影播放已经结束，投影已关闭。", "电影收好了，头部也恢复平视了。",
    ),
    ("rest_lighting", "success"): (
        "灯光调好了，休息一会儿吧。", "已经调好了，你放松一下。", "灯光好了，安心休息吧。",
    ),
    ("find_pet", "found_living"): (
        "找到豆豆了，在客厅，视频正传到手机。", "豆豆在客厅，已经找到啦，视频正在同步。",
    ),
    ("find_pet", "found_study"): (
        "找到豆豆了，在书房，视频正传到手机。", "豆豆在书房，已经找到啦，视频正在同步。",
    ),
    ("find_pet", "found_dining"): (
        "找到豆豆了，在餐厅，视频正传到手机。", "豆豆在餐厅，已经找到啦，视频正在同步。",
    ),
    ("find_pet_at", "found"): (
        "找到豆豆了，视频正传到手机。", "豆豆在这里，视频正在同步。",
    ),
    ("find_pet_here", "found"): (
        "找到豆豆了，视频正传到手机。", "豆豆就在这里，视频正在同步。",
    ),
}
POINT_SPOKEN_NAMES = {
    "origin": "原点",
    "white_wall": "客厅白墙",
    "study_projection": "书房",
}


class ScenarioError(RuntimeError):
    pass


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _resolve(value: Any, arguments: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if "$arg" in value:
            name = str(value["$arg"])
            if name in arguments:
                return copy.deepcopy(arguments[name])
            if "default" in value:
                return copy.deepcopy(value["default"])
            raise ScenarioError(f"missing_scenario_argument:{name}")
        return {str(key): _resolve(item, arguments) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, arguments) for item in value]
    return copy.deepcopy(value)


def _normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = re.sub(r"[\s，,。.!！?？、;；：:]", "", value)
    return value


_SPOKEN_NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两俩三四五六七八九十百千点半]+)"
_FITNESS_WORDS = ("俯卧撑", "引体向上", "引体", "深蹲", "下蹲", "运动", "锻炼", "健身")


def _spoken_number_value(value: str) -> float | None:
    """Parse the compact number forms commonly produced by Mandarin ASR."""

    token = unicodedata.normalize("NFKC", str(value or "")).lower()
    if not token:
        return None
    if token == "半":
        return 0.5
    try:
        return float(token)
    except ValueError:
        pass
    token = token.replace("两", "二").replace("俩", "二").replace("〇", "零")
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if "点" in token:
        whole, fraction = token.split("点", 1)
        whole_value = _spoken_number_value(whole or "零")
        if whole_value is None or not fraction or any(char not in digits for char in fraction):
            return None
        return whole_value + float("0." + "".join(str(digits[char]) for char in fraction))
    units = {"十": 10, "百": 100, "千": 1000}
    total = 0
    pending: int | None = None
    for char in token:
        if char in digits:
            pending = digits[char]
            continue
        unit = units.get(char)
        if unit is None:
            return None
        total += (1 if pending is None else pending) * unit
        pending = None
    if pending is not None:
        total += pending
    return float(total) if total > 0 else None


def _fitness_duration_seconds(transcript: str) -> float | None:
    """Extract only an explicit time duration attached to an exercise request.

    A bare repetition target such as “做六十个俯卧撑” is intentionally not a
    duration. A future offset such as “六十秒后提醒我运动” is also excluded.
    """

    text = _normalize_text(transcript)
    fitness_positions = [
        match.start()
        for word in _FITNESS_WORDS
        for match in re.finditer(re.escape(word), text)
    ]
    if not fitness_positions:
        return None
    candidates: list[tuple[int, int, float]] = []
    occupied: list[tuple[int, int]] = []
    minute_pattern = re.compile(
        rf"(?P<minutes>{_SPOKEN_NUMBER})(?:分钟|分)(?:(?P<half>半)|(?P<seconds>{_SPOKEN_NUMBER})秒)?"
    )
    for match in minute_pattern.finditer(text):
        if text[match.end():].startswith(("后", "以后", "之后")):
            continue
        minutes = _spoken_number_value(match.group("minutes"))
        seconds = _spoken_number_value(match.group("seconds")) if match.group("seconds") else 0.0
        if minutes is None:
            continue
        duration = minutes * 60.0 + (30.0 if match.group("half") else float(seconds or 0.0))
        occupied.append(match.span())
        distance = min(abs(match.start() - position) for position in fitness_positions)
        candidates.append((distance, match.start(), duration))
    second_pattern = re.compile(rf"(?P<seconds>{_SPOKEN_NUMBER})(?:秒钟?|s(?:ec(?:ond)?s?)?)")
    for match in second_pattern.finditer(text):
        if any(left <= match.start() < right for left, right in occupied):
            continue
        if text[match.end():].startswith(("后", "以后", "之后")):
            continue
        duration = _spoken_number_value(match.group("seconds"))
        if duration is None:
            continue
        distance = min(abs(match.start() - position) for position in fitness_positions)
        candidates.append((distance, match.start(), duration))
    if not candidates:
        return None
    duration = min(candidates, key=lambda item: (item[0], item[1]))[2]
    if not math.isfinite(duration) or not 1.0 <= duration <= 600.0:
        return None
    return int(duration) if duration.is_integer() else round(duration, 3)


def _phonetic_text(text: str) -> str:
    """Return library-produced Mandarin syllables for ASR homophone repair.

    pypinyin's phrase dictionaries choose context-aware readings for common
    polyphonic words.  Non-Chinese content (for example PPT) is retained so
    the phonetic score never replaces the original character score.
    """
    value = _normalize_text(text)
    if not value or lazy_pinyin is None or Style is None:
        return value
    return "".join(
        lazy_pinyin(
            value,
            style=Style.NORMAL,
            neutral_tone_with_five=False,
            errors=lambda chars: list(chars),
        )
    )


def _contains_term(text: str, term: str) -> bool:
    normalized = _normalize_text(text)
    target = _normalize_text(term)
    if not normalized or not target:
        return False
    if target in normalized:
        return True
    # A one-character pinyin match is too broad (会/回/绘, for example).
    return len(target) >= 2 and _phonetic_text(target) in _phonetic_text(normalized)


def _deep_find(value: Any, path: str) -> Any:
    parts = [item for item in str(path or "").split(".") if item]
    if parts:
        current = value
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                break
            current = current[part]
        else:
            return current
    if isinstance(value, dict):
        if path in value:
            return value[path]
        for key in ("structured_result", "result", "data", "skill_output", "parsed_json", "payload"):
            if key in value:
                found = _deep_find(value[key], path)
                if found is not None:
                    return found
    if isinstance(value, list):
        for item in value:
            found = _deep_find(item, path)
            if found is not None:
                return found
    return None


class ScenarioCatalog:
    """A copied, deterministic view of the fixed-scene procedure catalog."""

    def __init__(self, catalog_path: Path) -> None:
        payload = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        overlay_path = Path(catalog_path).with_name("home_scene_catalog.json")
        if overlay_path.is_file():
            overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
            payload["scene"] = _merge(dict(payload.get("scene") or {}), dict(overlay.get("scene") or {}))
            payload["skill_resources"] = {
                **dict(payload.get("skill_resources") or {}),
                **dict(overlay.get("skill_resources") or {}),
            }
            payload["procedures"] = {
                **dict(payload.get("procedures") or {}),
                **dict(overlay.get("procedures") or {}),
            }
            payload["planner_rules"] = [
                *list(payload.get("planner_rules") or []),
                *list(overlay.get("planner_rules") or []),
            ]
        self.path = Path(catalog_path)
        self.procedures: dict[str, dict[str, Any]] = dict(payload.get("procedures") or {})
        self.skill_resources: dict[str, tuple[str, ...]] = {
            str(name): tuple(str(item) for item in resources or [])
            for name, resources in dict(payload.get("skill_resources") or {}).items()
        }
        # The resident demonstration has exactly three saved points.  Normalize
        # inherited catalog aliases at load time so no legacy point can leak
        # through a model-selected procedure.
        for procedure_name, procedure in self.procedures.items():
            if procedure_name in {"push_up_companion", "pull_up_companion", "squat_companion"}:
                procedure.setdefault("parameters", {}).setdefault(
                    "identity_policy",
                    {"type": "str", "default": "face_and_reid"},
                )
                for step in procedure.get("steps") or []:
                    if str(step.get("skill")) in {"push_up", "pull_up", "squat"}:
                        step.setdefault("arguments", {}).setdefault(
                            "identity_policy",
                            {"$arg": "identity_policy", "default": "face_and_reid"},
                        )
            for step in procedure.get("steps") or []:
                if str(step.get("skill")) != "navigation_goto":
                    continue
                arguments = step.get("arguments") or {}
                point = arguments.get("point")
                if isinstance(point, str):
                    arguments["point"] = LEGACY_POINT_ALIASES.get(point, point)
                    if arguments["point"] not in CANONICAL_POINTS:
                        raise ScenarioError(
                            f"non_canonical_navigation_point:{procedure_name}:{arguments['point']}"
                        )
        self.planner_rules = [str(item) for item in payload.get("planner_rules") or []]
        if not self.procedures:
            raise ScenarioError("scenario_catalog_is_empty")

    @property
    def tool_schema(self) -> dict[str, Any]:
        # This legacy convenience scene performs navigation and lighting in
        # parallel.  Keep it available to the deterministic local router for
        # a cohesive request such as “去客厅帮我开灯”, but do not let a realtime
        # model use it as a generic replacement for explicit ordered or
        # conditional navigation + lighting commands.
        model_scenarios = {
            name: value
            for name, value in self.procedures.items()
            if name != "living_room_light_service"
        }
        descriptions = []
        for name, value in model_scenarios.items():
            examples = "、".join(str(item) for item in value.get("semantic_examples") or [])
            descriptions.append(f"{name}：{value.get('description', '')}；例如：{examples}")
        return {
            "type": "function",
            "function": {
                "name": SCENARIO_TOOL_NAME,
                "description": (
                    "执行一个完整且不可拆分的机器人场景流程。用户表达场景意图时必须调用本工具，"
                    "禁止自行组合导航、头部、投影、运动计数、寻找宠物等原子工具。"
                    "场景是带默认参数的模板：未说参数时保留默认流程；原地、不要导航、指定地点、"
                    "不要抬头等用户明示参数只覆盖对应默认项，禁止用默认值反向覆盖。"
                    "会议 start 才能启动完整场景；pause/resume/stop/status 不得生成 start 或导航。"
                    "只把用户正向要求的完整目标选成场景；否定短语是约束而不是场景。"
                    "例如‘去书房，不要投影’只应导航，不得调用会议投影场景。\n"
                    + "\n".join(descriptions)
                )[:3900],
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scenario": {
                            "type": "string",
                            "enum": sorted(model_scenarios),
                            "description": "要执行的完整场景名称。",
                        },
                        "name": {"type": "string", "description": "明确提供的人员名称；未提供则省略。"},
                        "duration": {
                            "type": "number",
                            "minimum": 1,
                            "maximum": 600,
                            "description": "持续时长（秒）。运动计数默认30秒；用户说60秒或一分钟时填60。",
                        },
                        "grams": {"type": "integer", "minimum": 1, "maximum": 100},
                        "point": {
                            "type": "string",
                            "enum": ["origin", "white_wall", "study_projection"],
                            "description": (
                                "可选目标点。指定地点找宠物或指定地点会议投影时使用；"
                                "会议投影省略时默认书房，用户明确说原地/当前位置时不要填写。"
                            ),
                        },
                        "identity_policy": {
                            "type": "string",
                            "enum": ["face_and_reid", "anonymous"],
                            "description": "运动默认face_and_reid；用户明确拒绝身份识别时才anonymous。",
                        },
                        "stay_put": {
                            "type": "boolean",
                            "description": "仅当用户明确要求原地、当前位置或不要导航时设为true。",
                        },
                        "operation": {
                            "type": "string",
                            "enum": ["start", "pause", "resume", "stop", "status"],
                            "default": "start",
                            "description": "开始场景与暂停、继续、结束、查询必须明确区分。",
                        },
                        "navigate": {
                            "type": "boolean",
                            "description": "用户明确要求原地或不要导航时为false；未明确时不要填写。",
                        },
                        "head": {
                            "type": "string",
                            "enum": ["up", "keep"],
                            "description": "投影默认抬头；用户明确说不要抬头时为keep。",
                        },
                        "content": {
                            "type": "string",
                            "enum": ["meeting"],
                            "description": "会议场景内容类型。",
                        },
                        "constraints": {
                            "type": "object",
                            "properties": {
                                "forbid_base_motion": {"type": "boolean"},
                                "forbidden": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "allowed_skills": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "description": "只记录用户明确说出的禁止项或‘只做某事’白名单。",
                        },
                        "evidence": {
                            "type": "string",
                            "description": "支持本次意图和参数的用户原话片段。",
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "语义判断置信度；不影响本地安全不变量。",
                        },
                    },
                    "required": ["scenario"],
                },
            },
        }

    def prompt_rules(self) -> str:
        names = "、".join(
            sorted(name for name in self.procedures if name != "living_room_light_service")
        )
        rules = "\n".join(f"- {item}" for item in self.planner_rules)
        return (
            f"可用完整场景为：{names}。只要用户表达的是这些场景之一，即使话术口语化或省略了非安全参数，"
            f"也必须调用 {SCENARIO_TOOL_NAME}，使用场景默认值；绝对禁止拆成多个原子工具。\n"
            f"固定场景与交互规则：\n{rules}"
        )

    @staticmethod
    def explicit_homecoming_replay(transcript: str) -> bool:
        text = _normalize_text(transcript)
        welcome_topic = any(word in text for word in ("欢迎回家", "欢迎画面", "欢迎投影", "回家画面"))
        replay = any(word in text for word in ("重播", "再播", "再播放", "再来一次", "重新播放"))
        return bool(welcome_topic and (replay or "画面" in text or "投影" in text))

    @staticmethod
    def is_homecoming_greeting(transcript: str) -> bool:
        """Match short wake greetings while tolerating common ASR omissions.

        Qwen sometimes transcribes “哈啰理想同学” as “Hello，理想”，
        dropping the honorific.  Keep this deliberately anchored to a short
        greeting addressed to 理想 so ordinary mentions of hello/理想 cannot
        start the protected welcome projection.
        """
        text = _normalize_text(transcript)
        wake_greeting = re.fullmatch(
            r"(?:hello|哈喽|哈啰|哈罗|嗨|你好)(?:呀|啊)?"
            # Qwen occasionally drops only the final “学”, producing
            # “Hello，理想同”。  Accept that one-syllable truncation while
            # keeping unrelated names such as “Hello 李想/李晓东” outside the
            # automatic hardware scene.
            r"理想(?:同学|同)?(?:呀|啊)?"
            r"(?:我)?(?:回来了|回家了|下班回来了)?",
            text,
        )
        direct_return = re.fullmatch(
            r"理想(?:同学|同)?(?:我)?(?:回来了|回家了|下班回来了)",
            text,
        )
        return bool(wake_greeting or direct_return)

    @staticmethod
    def _ambiguous_movie_followup(transcript: str, prior_context: str) -> bool:
        """Detect a likely lost negation after the robot offers a movie.

        A field utterance “不想看，我今天坐了一天了” was transcribed as
        “想看我今天做了”.  Treating the surviving “想看” as permission starts
        hardware in the opposite direction.  This gate is deliberately
        contextual and only asks for clarification when the object “电影” is
        absent and the same fragment also contains fatigue/rejection residue.
        """

        text = _normalize_text(transcript)
        context = _normalize_text(prior_context)
        movie_offer = bool(
            re.search(
                r"(?:需要|要不要|想不想).{0,10}(?:放|看|播放).{0,4}(?:电影|影片)",
                context,
            )
        )
        if not movie_offer or not re.search(r"想看|要看", text):
            return False
        if re.search(r"电影|影片|看吧|就看|那就看|放吧|播放吧", text):
            return False
        return bool(re.search(r"一天|坐了|做了|今天.{0,6}(?:累|做|坐)|已经.{0,6}(?:累|做|坐)", text))

    def parameter_defaults(self, name: str) -> dict[str, Any]:
        """Return catalog-authored defaults without treating them as user input."""

        procedure = self.procedures.get(name) or {}
        values: dict[str, Any] = {}
        for key, spec in dict(procedure.get("parameters") or {}).items():
            if isinstance(spec, dict) and "default" in spec:
                values[str(key)] = copy.deepcopy(spec["default"])
        return values

    @staticmethod
    def explicit_constraints(transcript: str) -> dict[str, Any]:
        """Translate explicit prohibitions and ``only`` language into constraints."""

        text = _normalize_text(transcript)
        raw_text = unicodedata.normalize("NFKC", str(transcript or "")).lower()
        forbidden: set[str] = set()
        allowed: list[str] | None = None
        no_navigation = bool(
            re.search(r"不要导航|不用导航|无需导航|不需要导航|别导航|不要去|不用去|别去", text)
            or any(item in text for item in ("原地", "就在这里", "就在这", "当前位置"))
        )
        # “原地找豆豆”在既有产品语义中是允许底盘原地旋转搜寻，不能把
        # “原地”机械地解释成禁止一切 base 资源。只有投影/通用动作中的
        # 原地，或用户明确说底盘不动，才成为 forbid_base_motion。
        local_pet_search = bool(
            re.search(r"(?:原地|就在这里|就在这|当前位置).{0,8}(?:找|寻找|看看|搜).{0,5}(?:豆豆|狗|宠物)", text)
            or re.search(r"(?:找|寻找|看看|搜).{0,5}(?:豆豆|狗|宠物).{0,8}(?:原地|就在这里|就在这|当前位置)", text)
        )
        forbid_base_motion = bool(
            (no_navigation and not local_pet_search)
            or re.search(r"不要移动|不用移动|别移动|底盘(?:不要|别|不许)动|原地不动", text)
        )
        if no_navigation:
            forbidden.add("navigation_goto")
        if re.search(
            r"不要抬头|不用抬头|别抬头|不需要抬头|保持当前角度|"
            r"(?:头|头部|镜头).{0,4}(?:不要|别|不用|无需|不需要)动|"
            r"(?:不要|别|不用|无需|不需要)动.{0,3}(?:头|头部|镜头)",
            text,
        ):
            forbidden.add("head_control:up")
        if re.search(r"不要找|不用找|别找|不要寻找|不用寻找|别寻找", text):
            forbidden.update(("pet_tracking", "pet_map_search"))
        if re.search(r"不要投影|不用投影|别投影|不要播放|不用播放|别播放", text):
            forbidden.update(("projector_control:start", "media_player:start"))
        # Parse ``只`` from its own clause.  Searching the punctuation-free
        # whole sentence made ``只设置提醒，不要投食`` span across the comma
        # and become ``only feeder_control``.  Defaults may fill missing
        # parameters, but a later negative clause must never redefine the
        # positive allow-list.
        only_clauses = [
            clause.strip()
            for clause in re.split(r"[，,。.!！?？、;；：:]", raw_text)
            if "只" in clause
        ]
        for clause in only_clauses:
            compact = _normalize_text(clause)
            only_body = compact.split("只", 1)[1]
            if re.search(r"(?:设置|新增|创建).{0,5}(?:提醒|闹钟)|提醒我|设.{0,3}提醒", only_body):
                allowed = ["reminder_schedule"]
                break
            if re.search(r"(?:查|查询|查看|列出|念|读).{0,5}(?:提醒|闹钟)|提醒(?:列表|有哪些)", only_body):
                allowed = ["reminder_query"]
                break
            if re.search(r"(?:删|删除|取消|撤销).{0,5}(?:提醒|闹钟)", only_body):
                allowed = ["reminder_cancel"]
                break
            if re.search(r"(?:开|打开|开启|关|关闭).{0,5}(?:灯|照明)|(?:灯|照明).{0,5}(?:开|关)", only_body):
                allowed = ["light_control"]
                break
            feeder_match = re.search(r"(?P<prefix>.{0,12})(?:喂|投食|出粮)", only_body)
            if feeder_match and not re.search(
                r"不要|不用|无需|不需要|别|禁止", feeder_match.group("prefix")
            ):
                allowed = ["feeder_control"]
                break
            if re.search(r"(?:打开|开启|关闭|关掉)?.{0,4}(?:投影仪|投影光源)", only_body):
                allowed = ["projector_control"]
                break
        return {
            "forbid_base_motion": forbid_base_motion,
            "forbidden": sorted(forbidden),
            "allowed_skills": allowed,
        }

    def normalize_intent(
        self,
        name: str,
        arguments: dict[str, Any],
        transcript: str,
    ) -> dict[str, Any]:
        """Build the canonical scene intent before compiling executable steps.

        Precedence is fixed: explicit prohibitions, explicit transcript
        parameters, model/context parameters, then catalog defaults.  Guarded
        motion/head parameters are accepted only when grounded by transcript
        inference; defaults can fill blanks but can never overwrite a user
        prohibition.
        """

        scenario = self.normalize_scenario_name(name, transcript)
        if scenario not in self.procedures:
            raise ScenarioError(f"unknown_scenario:{scenario}")
        raw = dict(arguments or {})
        allowed_parameters = set(
            dict((self.procedures.get(scenario) or {}).get("parameters") or {})
        )
        model_parameters = {
            key: copy.deepcopy(value)
            for key, value in raw.items()
            if key in allowed_parameters
        }
        explicit = self.infer_arguments(scenario, transcript)
        parameters = {
            **self.parameter_defaults(scenario),
            **model_parameters,
            **explicit,
        }
        constraints = self.explicit_constraints(transcript)
        if scenario in {"meeting_projection", "movie_projection"}:
            # Location and motion are safety-relevant. Ignore an invented
            # model point/stay_put value unless user words grounded it.
            if "point" not in explicit:
                parameters["point"] = self.parameter_defaults(scenario).get(
                    "point", "study_projection"
                )
            if constraints["forbid_base_motion"] or explicit.get("stay_put"):
                parameters["stay_put"] = True
                parameters["navigate"] = False
                parameters.pop("point", None)
            else:
                parameters["stay_put"] = False
                parameters["navigate"] = True
            if "head_control:up" in constraints["forbidden"]:
                parameters["head"] = "keep"
            else:
                parameters["head"] = "up"
            if scenario == "meeting_projection":
                parameters["content"] = "meeting"
        operation = str(raw.get("operation") or "start").strip().lower()
        # 用户原话中的传输控制动词优先于模型字段；这可以阻止模型把
        # “暂停/继续/结束”错误输出成 start 后重新启动整个会议场景。
        normalized_transcript = _normalize_text(transcript)
        explicit_stop = re.search(
            r"(?:关闭|关掉|停止|停掉|停播|结束|收起|撤掉|取消).{0,8}"
            r"(?:会议|投影|投屏|ppt|幻灯|画面|内容)|"
            r"(?:会议|投影|投屏|ppt|幻灯|画面|内容).{0,8}"
            r"(?:关闭|关掉|停止|停掉|停播|结束|收起|撤掉|取消|到这里|到这)|"
            r"(?:不投了|别播了|不用继续|不用放了)",
            normalized_transcript,
        )
        if explicit_stop:
            operation = "stop"
        elif re.search(r"暂停|停一下|先停", normalized_transcript):
            operation = "pause"
        elif re.search(r"继续|恢复|接着(?:播放|投影|放)", normalized_transcript):
            operation = "resume"
        elif re.search(r"状态|是否(?:正在|还在)|开着吗|关着吗", normalized_transcript):
            operation = "status"
        if operation not in {"start", "pause", "resume", "stop", "status"}:
            operation = "start"
        evidence = str(raw.get("evidence") or "").strip()
        if not evidence or _normalize_text(evidence) not in _normalize_text(transcript):
            evidence = str(transcript or "").strip()
        try:
            confidence = float(raw.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 1.0
        return {
            "intent": scenario,
            "operation": operation,
            "parameters": parameters,
            "constraints": constraints,
            "evidence": evidence,
            "confidence": max(0.0, min(1.0, confidence)),
        }

    def infer_arguments(self, name: str, transcript: str) -> dict[str, Any]:
        text = _normalize_text(transcript)
        inferred: dict[str, Any] = {}
        if name in {"push_up_companion", "pull_up_companion", "squat_companion"}:
            duration = _fitness_duration_seconds(text)
            if duration is not None:
                inferred["duration"] = duration
            if any(word in text for word in ("不用身份", "不要身份", "不识别人脸", "不用人脸", "不要人脸", "不用reid", "不要reid", "匿名")):
                inferred["identity_policy"] = "anonymous"
        if name == "find_pet_at":
            if _contains_term(text, "书房"):
                inferred["point"] = "study_projection"
            elif _contains_term(text, "餐厅") or _contains_term(text, "原点"):
                inferred["point"] = "origin"
            elif _contains_term(text, "客厅") or _contains_term(text, "白墙"):
                inferred["point"] = "white_wall"
        if name == "find_and_feed_doudou":
            grams_match = re.search(rf"(?P<grams>{_SPOKEN_NUMBER})克", text)
            if grams_match:
                grams = _spoken_number_value(grams_match.group("grams"))
                if grams is not None and float(grams).is_integer():
                    inferred["grams"] = int(grams)
        if name == "movie_projection" and self._movie_stay_put_requested(text):
            inferred["stay_put"] = True
        if name == "movie_projection" and not inferred.get("stay_put"):
            if _contains_term(text, "书房"):
                inferred["point"] = "study_projection"
            elif _contains_term(text, "客厅") or _contains_term(text, "白墙"):
                inferred["point"] = "white_wall"
            elif _contains_term(text, "餐厅") or _contains_term(text, "原点"):
                inferred["point"] = "origin"
        if name == "meeting_projection" and self._meeting_stay_put_requested(text):
            inferred["stay_put"] = True
            inferred["navigate"] = False
        if name == "meeting_projection" and not inferred.get("stay_put"):
            if _contains_term(text, "书房"):
                inferred["point"] = "study_projection"
            elif _contains_term(text, "客厅") or _contains_term(text, "白墙"):
                inferred["point"] = "white_wall"
            elif _contains_term(text, "餐厅") or _contains_term(text, "原点"):
                inferred["point"] = "origin"
        if name == "meeting_projection" and re.search(
            r"不要抬头|不用抬头|别抬头|不需要抬头|保持当前角度|"
            r"(?:头|头部|镜头).{0,4}(?:不要|别|不用|无需|不需要)动|"
            r"(?:不要|别|不用|无需|不需要)动.{0,3}(?:头|头部|镜头)",
            text,
        ):
            inferred["head"] = "keep"
        return inferred

    @staticmethod
    def _movie_stay_put_requested(transcript: str) -> bool:
        text = _normalize_text(transcript)
        if not re.search(r"电影|影片", text):
            return False
        return any(
            phrase in text
            for phrase in (
                "原地", "就在这里", "就在这", "当前位置",
                "不要导航", "不用导航", "无需导航", "不需要导航", "别导航",
                "不要去书房", "不用去书房", "别去书房",
            )
        )

    @staticmethod
    def _meeting_stay_put_requested(transcript: str) -> bool:
        text = _normalize_text(transcript)
        meeting_topic = ScenarioCatalog._meeting_content_evidence(text)
        if not meeting_topic:
            return False
        if any(
            phrase in text
            for phrase in (
                "不要导航", "不用导航", "无需导航", "不需要导航", "别导航",
                "不要去书房", "不用去书房", "别去书房",
            )
        ):
            return True
        if any(phrase in text for phrase in ("原地", "就在这里", "就在这", "在这里")):
            return True
        # Location words elsewhere in a compound sentence are not meeting
        # parameters.  “先查当前位置，再去书房投影” queries location first;
        # only a location phrase attached to the projection clause means that
        # the meeting must stay put.
        location = re.compile(r"(?:原地|就在这里|就在这|在这里|当前位置|现在的位置)")
        projection = re.compile(
            r"(?:(?:开始|打开|播放|进行|准备)?(?:会议)?(?:投影|投屏)|"
            r"(?:会议|开会).{0,6}(?:投影|投屏|开始))"
        )
        locations = list(location.finditer(text))
        projections = list(projection.finditer(text))
        for left in locations:
            for right in projections:
                gap_start = min(left.end(), right.end())
                gap_end = max(left.start(), right.start())
                if gap_end < gap_start or gap_end - gap_start > 10:
                    continue
                between = text[gap_start:gap_end]
                if re.search(r"导航|前往|去(?:客厅|书房|餐厅|原点|白墙)|回(?:到|原点)", between):
                    continue
                return True
        return False

    @staticmethod
    def _lighting_negated(transcript: str) -> bool:
        text = _normalize_text(transcript)
        return bool(
            re.search(
                r"(?:不要|别|不用|无需|不需要|不想)(?:帮我|给我)?"
                r"(?:打开|开启|开|调整)?(?:客厅)?(?:的)?灯|"
                r"(?:灯光?|照明)(?:不要|别|不用|无需|不需要)(?:打开|开启|开|调整)?",
                text,
            )
            or re.search(r"(?:不开灯|灯不用开|灯不要开|灯别开)", text)
        )

    @staticmethod
    def _feeding_negated(transcript: str) -> bool:
        text = _normalize_text(transcript)
        return bool(
            re.search(
                r"(?:不要|别|不用|无需|不需要|不想)(?:帮我|给我)?"
                r"(?:喂|投食|出粮|给.{0,3}(?:吃|饭|狗粮))",
                text,
            )
            or re.search(r"(?:别喂|不喂|不要投食|投食器别开)", text)
        )

    @staticmethod
    def _pet_search_negated(transcript: str) -> bool:
        text = _normalize_text(transcript)
        return bool(
            re.search(
                r"(?:不要|别|不用|无需|不需要|不想)(?:帮我|给我)?"
                r"(?:去)?(?:找|寻找|搜索|看看|去看)(?:一下)?(?:豆豆|小狗|狗狗|宠物|狗)",
                text,
            )
        )

    @staticmethod
    def _fitness_negated(transcript: str) -> bool:
        text = _normalize_text(transcript)
        return bool(
            re.search(
                r"(?:不要|别|不用|不想|先不)(?:做|练|开始|陪我)?"
                r"(?:俯卧撑|引体|深蹲|运动|锻炼|健身)",
                text,
            )
        )

    @staticmethod
    def living_light_requires_atomic_sequence(transcript: str) -> bool:
        """Whether lighting must preserve explicit ordering/conditions.

        The convenience scene is deliberately parallel.  It is therefore not
        equivalent when the user says navigation and lighting are separate
        ordered tasks or attaches a success/failure condition to lighting.
        """

        text = _normalize_text(transcript)
        navigation = bool(
            re.search(r"导航|前往|过去|去往|回到|回原点|去(?:客厅|书房|餐厅|原点|白墙)", text)
            or any(
                _contains_term(text, term)
                for term in ("导航到客厅", "导航到书房", "去客厅", "去书房", "回原点")
            )
        )
        lighting = bool(
            re.search(r"灯|照明|光线|太暗|太黑|看不清", text)
            or any(_contains_term(text, term) for term in ("打开灯", "关闭灯", "客厅灯"))
        )
        ordered_or_conditional = bool(
            re.search(
                r"然后|再|最后|之后|以后|到达后|到了以后|成功后|成功以后|"
                r"如果|假如|只要|即使|哪怕|不管|不论|无论",
                text,
            )
        )
        return navigation and lighting and ordered_or_conditional

    def scenario_explicitly_negated(self, name: str, transcript: str) -> bool:
        text = _normalize_text(transcript)
        if name == "living_room_light_service":
            return self._lighting_negated(text)
        if name in {"find_pet", "find_pet_at", "find_pet_here"}:
            return self._pet_search_negated(text)
        if name == "find_and_feed_doudou":
            return self._feeding_negated(text)
        if name == "meeting_projection":
            return self._projection_start_negated(text)
        if name == "meeting_projection_stop":
            return self._meeting_stop_negated(text)
        if name == "movie_projection":
            return bool(re.search(r"(?:不看|不要看|别看|不放|不要放|别放|不用放).{0,4}(?:电影|影片)|(?:电影|影片).{0,4}(?:不看|别放|不放)", text))
        if name == "movie_projection_pause":
            return bool(re.search(r"(?:不要|别|不用).{0,4}暂停", text))
        if name == "movie_projection_resume":
            return bool(re.search(r"(?:不要|别|不用).{0,4}(?:继续|恢复)", text))
        if name == "movie_projection_stop":
            return bool(re.search(r"(?:不要|别|不用).{0,4}(?:结束|停止|关闭|关掉)", text))
        if name in {"push_up_companion", "pull_up_companion", "squat_companion"}:
            return self._fitness_negated(text)
        return False

    @staticmethod
    def _meeting_stop_negated(transcript: str) -> bool:
        text = _normalize_text(transcript)
        return bool(
            re.search(
                r"(?:不要|别|先别|暂时别|不用|无需|不需要)(?:帮我|给我)?"
                r"(?:关闭|关掉|停止|停掉|结束|收起|撤掉)(?:会议)?(?:投影|投屏|ppt|幻灯|画面|内容)",
                text,
            )
            or re.search(
                r"(?:投影|投屏|ppt|幻灯|会议画面|墙上内容|大屏内容)"
                r"(?:不要|别|先别|暂时别)(?:关闭|关掉|停止|停掉|结束|收起|撤掉|停)",
                text,
            )
            or re.search(
                r"(?:不要|别|先别|暂时别).{0,8}"
                r"(?:投影|投屏|ppt|幻灯|会议画面|墙上内容|大屏内容).{0,6}"
                r"(?:关闭|关掉|停止|停掉|结束|收起|撤掉|停)",
                text,
            )
        )

    def normalize_scenario_name(self, requested: str, transcript: str) -> str:
        """Normalize harmless realtime-model aliases without widening intent."""

        value = str(requested or "").strip()
        if value in self.procedures:
            return value
        compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", value.lower()).strip("_")
        text = _normalize_text(transcript)
        meeting_evidence = self._has_topic_evidence("meeting_projection", text)
        closing = any(
            _contains_term(text, term)
            for term in ("关闭投影", "停止投影", "结束投影", "不投了")
        )
        if meeting_evidence and ("meeting" in compact or "projection" in compact or "会议" in compact or "投影" in compact):
            return "meeting_projection_stop" if closing else "meeting_projection"
        aliases = {
            "find_pet_at_location": "find_pet_at",
            "find_pet_in_place": "find_pet_here",
            "stop_meeting_projection": "meeting_projection_stop",
            "end_meeting_projection": "meeting_projection_stop",
            "start_meeting_projection": "meeting_projection",
            "start_movie_projection": "movie_projection",
            "play_movie": "movie_projection",
            "pause_movie": "movie_projection_pause",
            "resume_movie": "movie_projection_resume",
            "continue_movie": "movie_projection_resume",
            "stop_movie": "movie_projection_stop",
            "end_movie": "movie_projection_stop",
        }
        candidate = aliases.get(compact, value)
        return candidate if candidate in self.procedures else value

    def _fuzzy_example_match(self, transcript: str) -> str | None:
        """Repair small ASR edits and homophones against authored examples.

        RapidFuzz supplies the edit-distance implementation and pypinyin
        supplies context-aware Mandarin readings.  Requiring a strong score,
        reasonable length coverage and a unique winner prevents this repair
        path from becoming a general intent classifier.
        """
        if fuzz is None:
            return None
        text = _normalize_text(transcript)
        if len(text) < 4:
            return None
        phonetic = _phonetic_text(text)
        best_by_scenario: dict[str, float] = {}
        for name, procedure in self.procedures.items():
            for example in procedure.get("semantic_examples") or []:
                normalized = _normalize_text(str(example))
                if len(normalized) < 4:
                    continue
                if (
                    name == "homecoming_welcome"
                    and "理想" not in text
                    and "同学" not in text
                    and not text.startswith(("哈", "嗨", "你好"))
                ):
                    # “Hello 李想” is a plausible reference to a person, not
                    # necessarily the robot wake greeting.  Chinese greeting
                    # homophones or an explicit “同学” remain repairable.
                    continue
                coverage = min(len(text), len(normalized)) / max(len(text), len(normalized))
                if coverage < 0.5:
                    continue
                char_score = float(fuzz.ratio(text, normalized))
                example_phonetic = _phonetic_text(normalized)
                phonetic_score = float(fuzz.ratio(phonetic, example_phonetic))
                score = max(char_score, phonetic_score)
                if example_phonetic in phonetic or phonetic in example_phonetic:
                    # A complete authored phrase surrounded only by polite
                    # padding is stronger evidence than its global ratio.
                    score = max(score, 96.0)
                # Very different lengths can obtain an optimistic phonetic
                # score, so taper rather than discard moderate polite padding.
                if coverage < 0.8:
                    score *= 0.85 + 0.15 * (coverage / 0.8)
                best_by_scenario[name] = max(best_by_scenario.get(name, 0.0), score)
        ranked = sorted(
            ((score, name) for name, score in best_by_scenario.items()),
            reverse=True,
        )
        if not ranked or ranked[0][0] < 88.0:
            return None
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        if ranked[0][0] < 97.0 and ranked[0][0] - runner_up < 4.0:
            return None
        return ranked[0][1]

    def _has_topic_evidence(self, name: str, transcript: str) -> bool:
        terms = SCENARIO_TOPIC_TERMS.get(name) or ()
        if name == "living_room_light_service":
            # A room is a destination, not permission to operate its light.
            # Require actual lighting semantics so “去客厅” cannot expand into
            # the fixed navigate-and-light scene by association.
            return bool(re.search(r"灯|照明|光线|亮(?:一点|起来|些)?|暗|太黑|看不清", transcript))
        if name in {"find_pet", "find_pet_at", "find_pet_here"}:
            pet = any(_contains_term(transcript, term) for term in terms)
            return pet and any(
                _contains_term(transcript, term)
                for term in ("找", "看看", "在哪", "跑哪", "去看", "瞧瞧", "寻找", "找找")
            )
        if name == "find_and_feed_doudou":
            pet = any(
                _contains_term(transcript, term)
                for term in ("豆豆", "豆儿", "小狗", "宠物", "狗", "毛孩子", "小家伙")
            )
            feeding = any(_contains_term(transcript, term) for term in terms)
            searching = any(
                _contains_term(transcript, term)
                for term in ("找", "找到", "寻找", "搜索", "看看", "瞧瞧", "在哪", "去看")
            )
            return pet and feeding and searching
        if name == "meeting_projection":
            return self._meeting_content_evidence(transcript)
        if name == "meeting_projection_stop":
            topic = any(_contains_term(transcript, term) for term in terms)
            closing = any(
                _contains_term(transcript, term)
                for term in (
                    "关闭", "关掉", "关上", "关投影", "停止", "停掉", "停播", "停下来",
                    "结束", "收起", "收起来", "撤掉", "到这里", "就到这", "不投了", "别播了",
                    "不用继续", "不用放了", "取消", "退出",
                )
            )
            return topic and closing
        if name in {
            "movie_projection", "movie_projection_pause", "movie_projection_resume", "movie_projection_stop",
        }:
            topic = any(_contains_term(transcript, term) for term in terms)
            if not topic:
                return False
            actions = {
                "movie_projection": ("看", "放", "播放", "投影", "来一部", "来个"),
                "movie_projection_pause": ("暂停", "停一下", "先停"),
                "movie_projection_resume": ("继续", "恢复", "接着播", "接着看"),
                "movie_projection_stop": ("结束", "停止", "关闭", "关掉", "收起", "不看了"),
            }[name]
            return any(_contains_term(transcript, item) for item in actions)
        return any(_contains_term(transcript, term) for term in terms)

    @staticmethod
    def _meeting_content_evidence(transcript: str) -> bool:
        """Require meeting content, not the bare word “projector/projection”."""

        text = _normalize_text(transcript)
        return bool(
            re.search(r"会议|开(?:个|场|一下)?会|ppt|幻灯|汇报|演示|讨论", text)
            or re.search(r"(?:投影|投屏).{0,6}(?:会议|内容|文档|材料)", text)
            or re.search(r"(?:会议|内容|文档|材料).{0,6}(?:投影|投屏)", text)
        )

    @staticmethod
    def _meeting_start_evidence(transcript: str) -> bool:
        """Require a positive start predicate attached to meeting content."""

        text = _normalize_text(transcript)
        return bool(
            re.search(r"(?:我要|我想|准备|一起|陪我)?开(?:个|场|一下)?会", text)
            or re.search(
                r"(?:开始|播放|投影(?!仪)|投屏|展示|放出来|投出来).{0,8}"
                r"(?:会议|ppt|幻灯|会议内容|会议材料|汇报内容)",
                text,
            )
            or re.search(
                r"(?:会议|ppt|幻灯|会议内容|会议材料|汇报内容).{0,8}"
                r"(?:开始|播放|投影(?!仪)|投屏|展示|放出来|投出来)",
                text,
            )
        )

    def _context_corroborates_model_scene(
        self,
        name: str,
        transcript: str,
        prior_context: str,
    ) -> bool:
        """Use dialogue context only to resolve an omitted object.

        Qwen has already selected an enum-constrained scene.  This helper does
        not classify an unrelated sentence: the current turn must still carry
        the scene's action direction, while the immediately preceding robot
        sentence may provide an omitted object such as “会议投影”.
        """

        text = _normalize_text(transcript)
        context = _normalize_text(prior_context)
        if not text or not context or name == "homecoming_welcome":
            return False
        topic_name = (
            "meeting_projection" if name == "meeting_projection_stop"
            else "movie_projection" if name.startswith("movie_projection_")
            else name
        )
        if not self._has_topic_evidence(topic_name, context):
            return False
        continuation = any(
            word in text
            for word in ("这个", "那个", "刚才", "前面", "它", "先", "继续", "就", "不用", "别再")
        ) or len(text) <= 8
        if not continuation:
            return False
        actions = {
            "meeting_projection_stop": (
                "关闭", "关掉", "停", "结束", "收起", "撤掉", "到这里", "到这", "不用继续",
                "不用放", "别播", "取消",
            ),
            "meeting_projection": ("开始", "打开", "播放", "投出来", "放出来", "展示"),
            "movie_projection": ("开始", "看", "播放", "放", "投影"),
            "movie_projection_pause": ("暂停", "停一下", "先停"),
            "movie_projection_resume": ("继续", "恢复", "接着"),
            "movie_projection_stop": ("关闭", "关掉", "停", "结束", "收起", "不看了"),
            "push_up_companion": ("开始", "来一组", "活动", "锻炼", "练", "数"),
            "pull_up_companion": ("开始", "来一组", "练", "数"),
            "squat_companion": ("开始", "来一组", "练", "数"),
            "find_pet": ("找", "看看", "瞧瞧", "在哪", "跑哪"),
            "find_pet_at": ("找", "看看", "瞧瞧", "在哪"),
            "find_pet_here": ("找", "看看", "瞧瞧", "在哪"),
            "find_and_feed_doudou": ("喂", "吃饭", "开饭", "添粮", "加餐"),
            "rest_lighting": ("休息", "歇", "躺", "眯", "放松", "缓缓"),
            "living_room_light_service": ("打开", "调亮", "照亮", "太暗", "太黑"),
        }.get(name, ())
        return bool(actions) and any(_contains_term(text, item) for item in actions)

    @staticmethod
    def _explicitly_cancelled(transcript: str) -> bool:
        text = _normalize_text(transcript)
        return bool(
            re.match(r"^(?:算了|不用了|不要了|先别|别动|取消|不看了)", text)
            or re.fullmatch(r"(?:不用|不要|别|停|取消)(?:了|吧|一下)?", text)
        )

    @staticmethod
    def _capability_question(transcript: str) -> bool:
        text = _normalize_text(transcript)
        if not re.match(r"^(?:你)?(?:会不会|能不能|能否|会|能|可以|支持)", text):
            return False
        if any(term in text for term in ("帮我", "陪我", "给我", "现在", "马上", "开始")):
            return False
        # “你能不能帮我现在关掉投影”是当前指令；“你能不能在会议
        # 结束时自动收起投影”是在询问自动化能力，不能据此立刻操作。
        future_or_automatic = bool(
            re.search(r"(?:在|等)(?:会议|视频|音乐|投影|播放)?.{0,8}(?:结束|完成|播完|以后|之后|时)|每次|自动", text)
        )
        return (
            text.endswith(("吗", "么"))
            or any(term in text for term in ("什么功能", "会不会", "能不能做到", "是否支持"))
            or future_or_automatic
        )

    @staticmethod
    def _informational_question(transcript: str) -> bool:
        text = _normalize_text(transcript)
        if re.match(r"^(?:(?:先|然后|再|最后|顺便))?(?:什么是|为什么|怎么|如何|介绍|讲讲|说说)", text):
            return True
        informational = any(
            term in text
            for term in (
                "是什么", "什么意思", "怎么做", "如何做", "怎么用", "如何使用", "怎样用",
                "有什么好处", "几点开始", "是否支持",
            )
        )
        if not informational:
            return False
        explicit_action = bool(
            re.search(
                r"陪我|帮我(?:找|开|关|投|播|数|喂|调整)|"
                r"给我(?:找|开|关|投|播|喂)|现在(?:开始|打开|关闭)|马上(?:开始|打开|关闭)",
                text,
            )
        )
        return not explicit_action

    @staticmethod
    def _negates_hardware_action(transcript: str) -> bool:
        text = _normalize_text(transcript)
        if any(term in text for term in ("不用身份", "不要身份", "不用人脸", "不要人脸", "不用reid", "不要reid")):
            return False
        return bool(
            re.match(
                r"^(?:请)?(?:不要|别|先别|不用|不想)(?:帮我|给我|陪我)?"
                r"(?:做|找|喂|打开|开启|开始|播放|投影|投屏|导航|去|前往|调整|关|关闭|停止)",
                text,
            )
            or re.match(r"^(?:不做|不找|不开|不播放|不投影|不导航)", text)
        )

    @staticmethod
    def _projection_start_negated(transcript: str) -> bool:
        text = _normalize_text(transcript)
        return bool(
            re.search(
                r"(?:不要|别|不用|无需|不需要|不想)(?:帮我|给我)?(?:打开|开启|开始|播放)?"
                r"(?:会议)?(?:投影|投屏|ppt|幻灯)",
                text,
            )
            or re.search(r"(?:投影|投屏|ppt|幻灯)(?:不要|别|不用|无需|不需要)(?:打开|开启|开始|播放)?", text)
        )

    def model_scenario_supported(
        self,
        requested: str,
        transcript: str,
        *,
        matched: str | None = None,
        allow_additional_intents: bool = False,
        prior_context: str = "",
    ) -> tuple[bool, str]:
        """Validate Qwen's semantic scene choice without re-classifying it.

        Qwen already performs the broad semantic classification through the
        enum-constrained function call.  The local compiler keeps authority
        over conflicts, cancellations and minimum topic evidence, while exact,
        fuzzy and phonetic routes remain deterministic overrides.
        """
        if is_retrospective_query(transcript):
            return False, "retrospective_query_must_use_memory"
        requested = self.normalize_scenario_name(requested, transcript)
        if requested not in self.procedures:
            return False, "unknown_scenario"
        if requested == "movie_projection" and self._ambiguous_movie_followup(
            transcript,
            prior_context,
        ):
            return False, "ambiguous_movie_polarity"
        resolved = self.match(transcript) if matched is None else matched
        if resolved:
            if requested == resolved:
                return True, "local_match"
            if not allow_additional_intents:
                return False, "local_conflict"
        text = _normalize_text(transcript)
        if not text:
            return False, "empty_transcript"
        if "提醒" in text or "闹钟" in text:
            return False, "reminder_content"
        if text in {_normalize_text(item) for item in SHORT_AFFIRMATIONS}:
            return False, "context_required"
        if self._explicitly_cancelled(text):
            return False, "explicit_cancellation"
        if self._capability_question(text):
            return False, "capability_question"
        if self._informational_question(text):
            return False, "informational_question"
        if self.scenario_explicitly_negated(requested, text):
            if requested == "meeting_projection":
                return False, "negated_projection_start"
            return False, "negated_action"
        if requested == "living_room_light_service" and self.living_light_requires_atomic_sequence(text):
            return False, "ordered_lighting_requires_atomic_sequence"
        if not self._has_topic_evidence(requested, text) and not self._context_corroborates_model_scene(
            requested,
            text,
            prior_context,
        ):
            return False, "missing_topic_evidence"
        return True, "qwen_semantic_with_local_evidence"

    def match(self, transcript: str) -> str | None:
        text = _normalize_text(transcript)
        if not text:
            return None
        # A completed action mentioned in a history question is evidence for
        # memory retrieval, not authority to execute that action again.
        if is_retrospective_query(transcript):
            return None
        # Questions about capability describe a function; they are not an
        # instruction to operate hardware.  Requests such as “你能不能帮我…”
        # are excluded by _capability_question and continue normally.
        if self._capability_question(text):
            return None
        if self._informational_question(text):
            return None
        # Movie projection is a stateful protected scene, distinct from the
        # standalone entertainment-video player.  Resolve explicit transport
        # controls before the generic projection rule so “原地播放电影” can
        # never be mistaken for a meeting presentation.
        movie_topic = bool(re.search(r"电影|影片", text))
        movie_start_negated = bool(
            re.search(
                r"(?:不看|不要看|别看|不放|不要放|别放|不用放|"
                r"不播放|不要播放|别播放|不用播放|不开始|不要开始|别开始)"
                r".{0,4}(?:电影|影片)",
                text,
            )
        )
        if movie_topic:
            if re.search(r"暂停|停一下|先停", text) and not re.search(r"(?:不要|别|不用).{0,4}暂停", text):
                return "movie_projection_pause"
            if re.search(r"继续|恢复|接着播|接着看", text) and not re.search(r"(?:不要|别|不用).{0,4}(?:继续|恢复)", text):
                return "movie_projection_resume"
            explicit_movie_stop = bool(
                re.search(
                    r"(?:结束|停止|关闭|关掉|收起).{0,6}(?:电影|影片)|"
                    r"(?:电影|影片).{0,6}(?:结束|停止|关闭|关掉|收起)",
                    text,
                )
            )
            if explicit_movie_stop and not re.search(r"(?:不要|别|不用).{0,4}(?:结束|停止|关闭|关掉)", text):
                return "movie_projection_stop"
            if (
                not movie_start_negated
                and re.search(r"看|放|播放|投影|来一部|来个", text)
            ):
                return "movie_projection"
        if (
            self._meeting_content_evidence(text)
            and re.search(r"暂停|停一下|先停|继续|恢复|接着", text)
            and not self._meeting_start_evidence(text)
        ):
            # Meeting transport is handled by projector_control. Do not let a
            # fuzzy authored start example turn pause/resume into a full start.
            return None
        # “不要导航，直接在这里投影” only negates the navigation stage;
        # it explicitly requests the remaining meeting projection stages.
        if self._meeting_stay_put_requested(text):
            return "meeting_projection"
        projection_start_negated = self._projection_start_negated(text)
        projection_stop_negated = self._meeting_stop_negated(text)
        # Content inside a reminder is future text, not an instruction to run
        # that scene now.  For example “提醒我十分钟后开会” must schedule one
        # reminder instead of navigating and starting the meeting projector.
        # “提醒”可能出现在句首或句尾（例如“帮我设个下午三点开会的提醒”），
        # 所以不要依赖相邻的固定短语。场景目录只负责即时硬件场景；只要整句
        # 明确谈到提醒/闹钟，就把意图留给 reminder skill 或多任务编排器。
        if "提醒" in text or "闹钟" in text:
            return None
        light_negated = self._lighting_negated(text)
        feeding_negated = self._feeding_negated(text)
        pet_search_negated = self._pet_search_negated(text)
        fitness_negated = self._fitness_negated(text)
        if self.explicit_constraints(text).get("allowed_skills") == ["feeder_control"]:
            # A scene matcher cannot represent this whitelist without adding
            # forbidden patrol/navigation steps. Leave the utterance to the
            # direct feeder skill.
            return None

        # Explicit deterministic routes authored in the original scene catalog.
        for name, procedure in self.procedures.items():
            if name == "living_room_light_service" and light_negated:
                continue
            if name == "meeting_projection" and (projection_start_negated or projection_stop_negated):
                continue
            if name == "meeting_projection_stop" and projection_stop_negated:
                continue
            routing = procedure.get("deterministic_routing")
            if not isinstance(routing, dict) or routing.get("enabled") is not True:
                continue
            if any(re.search(str(pattern), text) for pattern in routing.get("exclude_patterns") or []):
                continue
            if any(re.fullmatch(str(pattern), text) for pattern in routing.get("patterns") or []):
                return name

        # This narrow deterministic route precedes semantic examples so both
        # “哈啰/哈喽” spellings and the observed “Hello，理想” ASR truncation
        # receive the same protected homecoming procedure.
        if self.is_homecoming_greeting(text):
            return "homecoming_welcome"

        # Location- and feeding-specific pet requests must outrank generic
        # authored examples such as “找一下豆豆”.  Otherwise a longer utterance
        # like “只去书房找一下豆豆” is incorrectly widened to all rooms.
        # “豆儿”是实际日志中“豆豆”的稳定 ASR 别名，但只在
        # 找/看/位置/喂食语境中接受，避免一个名词单独触发硬件。
        pet = "豆豆" in text or "豆儿" in text or "狗" in text or "宠物" in text
        pet_search_requested = any(
            word in text for word in ("找", "找到", "寻找", "搜索", "看看", "瞧瞧", "在哪", "去看")
        )
        if (
            pet
            and pet_search_requested
            and not pet_search_negated
            and not feeding_negated
            and any(word in text for word in ("喂", "吃饭", "吃东西", "该吃", "饿了", "狗粮", "开饭"))
        ):
            return "find_and_feed_doudou"
        if pet and not pet_search_negated and any(word in text for word in ("找", "看看", "在哪")):
            if any(word in text for word in ("这里", "当前位置", "原地")) and "find_pet_here" in self.procedures:
                return "find_pet_here"
            if any(word in text for word in ("客厅", "书房", "餐厅", "原点", "白墙")) and "find_pet_at" in self.procedures:
                return "find_pet_at"

        # Exact/contained authored examples are safe high-confidence routes.
        candidates: list[tuple[int, str]] = []
        for name, procedure in self.procedures.items():
            if name == "living_room_light_service" and light_negated:
                continue
            if name == "meeting_projection" and (projection_start_negated or projection_stop_negated):
                continue
            if name == "meeting_projection_stop" and projection_stop_negated:
                continue
            for example in procedure.get("semantic_examples") or []:
                normalized = _normalize_text(str(example))
                exact = normalized and text == normalized
                # Short wake aliases such as “Hello理想” must match the whole
                # utterance.  Otherwise a request like “翻译Hello理想” would
                # incorrectly operate the head and projector.
                contained = (
                    normalized
                    and name != "homecoming_welcome"
                    and len(normalized) >= 5
                    and normalized in text
                )
                if exact or contained:
                    candidates.append((len(normalized), name))
        if candidates:
            return max(candidates)[1]

        repaired = self._fuzzy_example_match(text)
        if repaired and not (
            (repaired == "living_room_light_service" and light_negated)
            or (repaired == "meeting_projection" and (projection_start_negated or projection_stop_negated))
            or (repaired == "meeting_projection_stop" and projection_stop_negated)
        ):
            return repaired

        close = ("关闭", "关掉", "关了", "停止", "结束", "不投了")
        meeting_topic = self._meeting_content_evidence(text)
        if not projection_stop_negated and meeting_topic and any(word in text for word in (*close, "别播", "取消")):
            return "meeting_projection_stop"
        if not fitness_negated and "引体" in text:
            return "pull_up_companion"
        if not fitness_negated and ("深蹲" in text or "下蹲" in text):
            return "squat_companion"
        if not fitness_negated and ("俯卧撑" in text or (("运动" in text or "锻炼" in text) and any(word in text for word in ("陪", "开始", "一起", "做", "练")))):
            return "push_up_companion"
        if (
            not projection_start_negated
            and not projection_stop_negated
            and meeting_topic
            and self._meeting_start_evidence(text)
        ):
            return "meeting_projection"
        if self.explicit_homecoming_replay(text):
            return "homecoming_welcome"
        if pet and not pet_search_negated and any(word in text for word in ("找", "看看", "在哪")):
            return "find_pet"
        if (
            not light_negated
            and "客厅" in text
            and "灯" in text
            and any(word in text for word in ("开", "亮", "暗"))
            and not self.living_light_requires_atomic_sequence(text)
        ):
            return "living_room_light_service"
        if any(word in text for word in ("休息", "睡一会", "睡觉")) and "rest_lighting" in self.procedures:
            return "rest_lighting"
        return None

    @staticmethod
    def _step_enabled(step: dict[str, Any], arguments: dict[str, Any]) -> bool:
        condition = step.get("enabled_if")
        if not condition:
            return True
        if not isinstance(condition, dict):
            return False
        key = str(condition.get("argument") or "")
        value = arguments.get(key)
        if "equals" in condition:
            return value == condition["equals"]
        if "truthy" in condition:
            return bool(value) is bool(condition["truthy"])
        return key in arguments

    def validate_compiled_plan(
        self,
        plan: dict[str, Any],
        arguments: dict[str, Any],
        constraints: dict[str, Any] | None = None,
        operation: str = "start",
    ) -> None:
        """Enforce final invariants on active steps immediately before dispatch."""

        constraints = dict(constraints or {})
        forbidden = {str(item) for item in constraints.get("forbidden") or []}
        allowed_value = constraints.get("allowed_skills")
        allowed = {str(item) for item in allowed_value or []} if allowed_value else None
        active_steps = [
            step for step in plan.get("steps") or []
            if self._step_enabled(step, arguments)
        ]
        violations: list[str] = []
        start_actions = {
            ("projector_control", "on"),
            ("projector_control", "meeting_presentation_on"),
            ("media_player", "play_movie"),
            ("media_player", "play_video"),
        }
        for step in active_steps:
            skill = str(step.get("skill") or "")
            action = str(step.get("action") or "")
            resources = set(self.skill_resources.get(skill, ()))
            if constraints.get("forbid_base_motion") and "base" in resources:
                violations.append(f"forbid_base_motion:{skill}")
            if allowed is not None and skill not in allowed:
                violations.append(f"only_constraint:{skill}")
            if skill in forbidden or f"{skill}:{action}" in forbidden:
                violations.append(f"forbidden:{skill}:{action}")
            if "projector_control:start" in forbidden and (skill, action) in start_actions:
                violations.append(f"forbidden_projector_start:{skill}:{action}")
            if "media_player:start" in forbidden and skill == "media_player" and action.startswith("play"):
                violations.append(f"forbidden_media_start:{action}")
            if operation in {"pause", "resume", "stop"} and (skill, action) in start_actions:
                violations.append(f"transport_must_not_start:{operation}:{skill}:{action}")
            if operation in {"pause", "resume"} and "base" in resources:
                violations.append(f"transport_must_not_move:{operation}:{skill}")
        if violations:
            raise ScenarioError("intent_constraint_violation:" + ",".join(sorted(set(violations))))

    def compile_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        name = str(intent.get("intent") or "")
        parameters = dict(intent.get("parameters") or {})
        plan = self.compile(name, parameters)
        self.validate_compiled_plan(
            plan,
            parameters,
            dict(intent.get("constraints") or {}),
            str(intent.get("operation") or "start"),
        )
        plan["normalized_intent"] = copy.deepcopy(intent)
        return plan

    def compile(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        procedure = self.procedures.get(name)
        if procedure is None:
            raise ScenarioError(f"unknown_scenario:{name}")
        allowed_parameters = set(dict(procedure.get("parameters") or {}))
        arguments = {
            **self.parameter_defaults(name),
            **{
                key: copy.deepcopy(value)
                for key, value in dict(arguments or {}).items()
                if key in allowed_parameters
            },
        }
        if name in {"meeting_projection", "movie_projection"}:
            if arguments.get("stay_put") is True:
                arguments["navigate"] = False
            if arguments.get("navigate") is False:
                arguments["stay_put"] = True
        if name in {"push_up_companion", "pull_up_companion", "squat_companion"} and "duration" in arguments:
            try:
                duration = float(arguments["duration"])
            except (TypeError, ValueError) as exc:
                raise ScenarioError(f"invalid_fitness_duration:{arguments['duration']}") from exc
            if not math.isfinite(duration) or not 1.0 <= duration <= 600.0:
                raise ScenarioError(f"invalid_fitness_duration:{arguments['duration']}")
            arguments["duration"] = int(duration) if duration.is_integer() else duration
        local_steps = list(procedure.get("steps") or [])
        if not local_steps:
            raise ScenarioError(f"scenario_has_no_steps:{name}")
        ids = {str(step.get("id") or "") for step in local_steps}
        if "" in ids or len(ids) != len(local_steps):
            raise ScenarioError(f"invalid_scenario_step_ids:{name}")
        steps = []
        for raw in local_steps:
            depends = [str(item) for item in raw.get("depends_on") or []]
            missing = sorted(set(depends) - ids)
            if missing:
                raise ScenarioError(f"missing_scenario_dependencies:{name}:{','.join(missing)}")
            steps.append(
                {
                    "id": str(raw["id"]),
                    "skill": str(raw.get("skill") or ""),
                    "action": str(raw.get("action") or ""),
                    "arguments": _resolve(dict(raw.get("arguments") or {}), arguments),
                    "depends_on": depends,
                    "dependency_policy": str(raw.get("dependency_policy") or "success"),
                    "enabled_if": copy.deepcopy(raw.get("enabled_if")),
                    "run_if": copy.deepcopy(raw.get("run_if")),
                    "silent_result": bool(raw.get("silent_result", True)),
                }
            )
        outcomes = []
        for raw in procedure.get("outcomes") or []:
            text = str(raw.get("text") or "").strip()
            if text:
                outcomes.append({"name": str(raw.get("name") or ""), "when": copy.deepcopy(raw.get("when") or {}), "text": text})
        return {
            "scenario": name,
            "description": str(procedure.get("description") or name),
            "steps": steps,
            "outcome_groups": [{"procedure": name, "rules": outcomes}],
        }

    @staticmethod
    def protected_scenario(skill: str, arguments: dict[str, Any]) -> str | None:
        if skill in {"push_up", "pull_up", "squat"}:
            return f"{skill}_companion"
        if skill == "welcome_projection":
            return "homecoming_welcome"
        if skill == "projector_control":
            action = str(arguments.get("action") or "").lower()
            if action == "meeting_presentation_on":
                return "meeting_projection"
            if action == "off":
                return "meeting_projection_stop"
        return None


class ScenarioExecutor:
    def __init__(
        self,
        catalog: ScenarioCatalog,
        invoke_atomic: Callable[[str, dict[str, Any]], dict[str, Any]],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.catalog = catalog
        self.invoke_atomic = invoke_atomic
        self.progress_callback = progress_callback
        self._lock = threading.Lock()
        self._speech_variant_counts: dict[str, int] = {}

    def _scenario_start_speech(self, name: str, fallback: str) -> str:
        options = SCENARIO_START_SPEECH.get(name)
        if not options:
            return fallback
        return self._cycle_speech(f"start:{name}", options)

    def _cycle_speech(self, key: str, options: tuple[str, ...]) -> str:
        count = self._speech_variant_counts.get(key, 0)
        self._speech_variant_counts[key] = count + 1
        return options[count % len(options)]

    def _scenario_outcome_speech(self, scenario: str, outcome: str, fallback: str) -> str:
        options = SCENARIO_OUTCOME_SPEECH.get((scenario, outcome))
        if not options:
            return fallback
        return self._cycle_speech(f"outcome:{scenario}:{outcome}", options)

    @staticmethod
    def _argument_condition(condition: Any, arguments: dict[str, Any]) -> bool:
        """Evaluate a compile-time step gate against trusted scene arguments."""
        if not condition:
            return True
        if not isinstance(condition, dict):
            return False
        key = str(condition.get("argument") or "")
        if not key:
            return False
        value = arguments.get(key, False)
        if "equals" in condition:
            return value == condition["equals"]
        if "truthy" in condition:
            return bool(value) is bool(condition["truthy"])
        return key in arguments

    def _emit_progress(self, scenario: str, kind: str, text: str, **extra: Any) -> None:
        if self.progress_callback is None or not str(text or "").strip():
            return
        self.progress_callback(
            {
                "skill_name": SCENARIO_TOOL_NAME,
                "scenario": scenario,
                "kind": kind,
                "text": str(text).strip(),
                **extra,
            }
        )

    @staticmethod
    def _step_progress_text(
        scenario: str,
        step: dict[str, Any],
        arguments: dict[str, Any],
        index: int,
    ) -> str:
        if index == 0:
            return ""
        # Most scenes already have one acknowledgement, one authoritative
        # result, and (for fitness) realtime count/attention events. Narrating
        # hidden head, projector, light and cleanup steps makes the robot sound
        # mechanical and can delay the useful result. Long pet searches and
        # the one meaningful fitness-location handoff are the exceptions.
        fitness_scenarios = {"push_up_companion", "pull_up_companion", "squat_companion"}
        if scenario in fitness_scenarios:
            if str(step.get("skill") or "") == "head_control" and str(step.get("action") or "") == "up":
                return "这里比较合适做运动。"
            return ""
        if scenario not in {"find_pet", "find_pet_at", "find_pet_here", "find_and_feed_doudou"}:
            return ""
        skill = str(step.get("skill") or "")
        action = str(step.get("action") or arguments.get("action") or "").lower()
        point = POINT_SPOKEN_NAMES.get(str(arguments.get("point") or ""), str(arguments.get("point") or "目标位置"))
        if skill == "navigation_goto":
            if scenario in {"find_pet", "find_and_feed_doudou"}:
                return f"这里还没有找到豆豆，我继续去{point}。"
            return f"我现在继续去{point}。"
        if skill == "pet_tracking":
            return f"我到{point if point != '目标位置' else '这个位置'}了，现在转一圈找找豆豆。"
        if skill == "feeder_control" and action == "feed":
            return "找到豆豆了，我现在给它投食。"
        if skill == "head_control" and action == "up":
            if scenario == "meeting_projection" and arguments.get("stay_put"):
                return "我就在当前位置调整摄像和投影角度。"
            return "已经到位置了，我来调整摄像和投影角度。"
        if skill == "head_control" and action == "level":
            return "前面的流程结束了，我把头部恢复平视。"
        if skill in {"push_up", "pull_up", "squat"}:
            label = {"push_up": "俯卧撑", "pull_up": "引体向上", "squat": "深蹲"}[skill]
            return f"位置准备好了，我先确认身份，然后开始{label}计数。"
        if skill == "welcome_projection" and action == "play":
            return "角度准备好了，欢迎画面现在开始播放。"
        if skill == "projector_control" and action == "meeting_presentation_on":
            return "角度准备好了，现在打开会议投影。"
        if skill == "projector_control" and action == "off":
            return "演示已经结束，我现在关闭投影。"
        if skill == "light_control":
            return "我现在帮你调整灯光。"
        return ""

    def _invoke_step_record(self, step: dict[str, Any], call_args: dict[str, Any]) -> dict[str, Any]:
        """Run one atomic step without allowing its exception to cancel siblings.

        Device adapters are expected to return structured failures, but an
        upstream SDK can still raise (for example, an expired cloud login).
        Treat that as this step's result so an independent navigation branch
        can continue and the final scene report remains truthful.
        """
        try:
            result = self.invoke_atomic(step["skill"], call_args)
            if not isinstance(result, dict):
                raise TypeError("atomic_result_not_object")

            # A level command can physically reach a safe horizontal pose just
            # before the strict stability window expires.  In that case the
            # atomic adapter deliberately keeps lidar gated and returns
            # head_level_not_safe_for_lidar.  Long scenarios used to treat the
            # bounded confirmation timeout as a terminal scene failure even
            # though a second confirmation immediately succeeds.  Retry this
            # one idempotent cleanup action once; never retry navigation,
            # projector startup, or any other state-changing scene step.
            succeeded = bool(result.get("ok") or result.get("validation_ok"))
            structured = result.get("structured_result")
            if not isinstance(structured, dict):
                structured = {}
            first_error = str(result.get("error") or structured.get("error") or "")
            recoverable_level_timeout = (
                step.get("skill") == "head_control"
                and step.get("action") == "level"
                and not succeeded
                and first_error in {
                    "head_level_not_safe_for_lidar",
                    "head_target_unconfirmed",
                }
            )
            if recoverable_level_timeout:
                time.sleep(6.5)
                retry_result = self.invoke_atomic(step["skill"], call_args)
                if not isinstance(retry_result, dict):
                    raise TypeError("atomic_retry_result_not_object")
                retry_result = dict(retry_result)
                retry_result["scenario_recovery"] = {
                    "attempted": True,
                    "reason": first_error,
                    "first_ok": bool(result.get("ok")),
                    "first_validation_ok": bool(result.get("validation_ok")),
                    "first_error": first_error,
                }
                result = retry_result
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
            result = {
                "ok": False,
                "validation_ok": False,
                "executed": False,
                "skill": step["skill"],
                "error": error,
                "structured_result": {
                    "failure_reason": self._classify_step_exception(step["skill"], error),
                    "detail": str(exc)[:500],
                },
                "spoken_summary": "这项操作刚才没有完成。",
            }
        succeeded = bool(result.get("ok") or result.get("validation_ok"))
        return {
            "id": step["id"], "skill": step["skill"], "action": step["action"],
            "finished": True, "succeeded": succeeded, "skipped": False,
            "result": result, "error": result.get("error"),
        }

    @staticmethod
    def _classify_step_exception(skill: str, error: str) -> str:
        value = str(error or "").lower()
        if skill == "light_control":
            if "offline" in value:
                return "device_offline"
            if any(token in value for token in (
                "token", "login", "登录", "凭证", "unauthorized", "forbidden", "401", "403",
            )):
                return "auth_expired_or_invalid"
            if any(token in value for token in ("timeout", "timed out")):
                return "service_timeout"
            if any(token in value for token in (
                "network", "dns", "name resolution", "connection refused", "connection reset",
            )):
                return "network_unavailable"
            return "control_failed"
        return "step_exception"

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        announce: bool = True,
    ) -> dict[str, Any]:
        started = time.monotonic()
        if not self._lock.acquire(blocking=False):
            return {
                "ok": False,
                "validation_ok": False,
                "executed": False,
                "skill": SCENARIO_TOOL_NAME,
                "scenario": name,
                "error": "another_scenario_is_running",
                "spoken_summary": "另一个机器人场景正在执行，请先等待它结束。",
            }
        try:
            plan = self.catalog.compile(name, arguments)
            condition_arguments = {
                **self.catalog.parameter_defaults(name),
                **dict(arguments or {}),
            }
            if name in {"meeting_projection", "movie_projection"} and condition_arguments.get("stay_put"):
                condition_arguments["navigate"] = False
            if announce:
                speech_name = name
                fallback = f"收到，我现在开始{plan['description']}。"
                if name in {"meeting_projection", "movie_projection"} and arguments.get("stay_put"):
                    if name == "movie_projection":
                        speech_name = "movie_projection_here"
                    else:
                        speech_name = "meeting_projection_here"
                elif name in {"meeting_projection", "movie_projection"} and arguments.get("point"):
                    point = POINT_SPOKEN_NAMES.get(
                        str(arguments.get("point")),
                        str(arguments.get("point")),
                    )
                    purpose = "电影投影" if name == "movie_projection" else "会议投影"
                    fallback = f"好，我去{point}准备{purpose}。"
                self._emit_progress(
                    name,
                    "acknowledgement",
                    (
                        fallback
                        if name in {"meeting_projection", "movie_projection"} and arguments.get("point") and not arguments.get("stay_put")
                        else self._scenario_start_speech(speech_name, fallback)
                    ),
                    step_count=len(plan["steps"]),
                )
            records: dict[str, dict[str, Any]] = {}
            for index, step in enumerate(plan["steps"]):
                if not self._argument_condition(step.get("enabled_if"), condition_arguments):
                    records[step["id"]] = {
                        "id": step["id"],
                        "skill": step["skill"],
                        "action": step["action"],
                        "finished": True,
                        "succeeded": True,
                        "skipped": True,
                        "intentional_skip": True,
                        "skip_reason": "scenario_argument",
                        "error": None,
                    }
                    continue
                dependencies = [records[item] for item in step["depends_on"]]
                dependency_ok = all(item.get("succeeded") for item in dependencies)
                if step["dependency_policy"] == "completion":
                    dependency_ok = all(item.get("finished") for item in dependencies)
                validation_branch = bool(dependencies) and all(
                    item.get("succeeded")
                    and bool((item.get("result") or {}).get("validation_ok"))
                    and not bool((item.get("result") or {}).get("executed"))
                    for item in dependencies
                )
                condition_ok = self._condition(step.get("run_if"), records)
                if not dependency_ok or (not condition_ok and not validation_branch):
                    intentional_condition_skip = bool(dependency_ok and not condition_ok)
                    records[step["id"]] = {
                        "id": step["id"], "skill": step["skill"], "action": step["action"],
                        "finished": True,
                        "succeeded": intentional_condition_skip,
                        "skipped": True,
                        "intentional_skip": intentional_condition_skip,
                        "skip_reason": "condition_not_met" if intentional_condition_skip else "prerequisite_not_satisfied",
                        "error": None if intentional_condition_skip else "prerequisite_not_satisfied",
                    }
                    continue
                call_args = dict(step["arguments"])
                if step["action"]:
                    call_args["action"] = step["action"]
                progress_text = self._step_progress_text(
                    name,
                    step,
                    {**condition_arguments, **call_args},
                    index,
                )
                if progress_text:
                    self._emit_progress(
                        name,
                        "progress",
                        progress_text,
                        step_id=step["id"],
                        step_index=index,
                    )
                records[step["id"]] = self._invoke_step_record(step, call_args)

            # Meeting projection intentionally remains active after a normal
            # start, so cleanup must not appear in (or alter) its default plan.
            # If the head was physically raised but projector startup failed,
            # however, restore the safe neutral state exactly once.  This is a
            # runtime rollback, not a second authored scene, and it never runs
            # for dry-run validation, navigation failure, or a successful start.
            if name == "meeting_projection":
                raised = records.get("head_up") or {}
                projected = records.get("project") or {}
                raised_physically = bool(
                    raised.get("succeeded")
                    and (raised.get("result") or {}).get("executed")
                )
                projector_failed = bool(
                    projected.get("finished")
                    and not projected.get("skipped")
                    and not projected.get("succeeded")
                )
                if raised_physically and projector_failed:
                    off_step = {
                        "id": "runtime_rollback_off",
                        "skill": "projector_control",
                        "action": "off",
                    }
                    off_record = self._invoke_step_record(
                        off_step, {"action": "off"}
                    )
                    records[off_step["id"]] = off_record
                    level_step = {
                        "id": "runtime_rollback_level",
                        "skill": "head_control",
                        "action": "level",
                    }
                    records[level_step["id"]] = self._invoke_step_record(
                        level_step, {"action": "level"}
                    )

            selected = None
            for rule in plan["outcome_groups"][0]["rules"]:
                if self._condition(rule.get("when"), records):
                    selected = rule
                    break
            step_values = list(records.values())
            validation_ok = all(item.get("succeeded") or item.get("skipped") for item in step_values)
            executed = any(bool((item.get("result") or {}).get("executed")) for item in step_values)
            all_succeeded = all(item.get("succeeded") for item in step_values)
            if selected:
                spoken = self._scenario_outcome_speech(
                    name,
                    str(selected.get("name") or ""),
                    selected["text"],
                )
            else:
                visible = [
                    str((item.get("result") or {}).get("spoken_summary") or "").strip()
                    for item, step in zip(step_values, plan["steps"])
                    if not step.get("silent_result")
                ]
                spoken = next((item for item in reversed(visible) if item), "")
                if not spoken:
                    spoken = f"{plan['description']}已完成。" if all_succeeded else f"{plan['description']}没有完整执行。"
            if (
                name in {"push_up_companion", "pull_up_companion", "squat_companion"}
                and all_succeeded
                and executed
                and spoken
                and "喝口水" not in spoken
            ):
                care = self._cycle_speech(
                    f"care:{name}",
                    ("辛苦了，喝口水吧。", "完成得不错，先补点水。", "做完啦，歇一下再喝口水。"),
                )
                spoken = spoken.rstrip("。！!") + "。" + care
            video_warnings = []
            if name in {"find_pet", "find_pet_at", "find_pet_here", "find_and_feed_doudou"} and executed:
                for item in step_values:
                    payload = (item.get("result") or {}).get("structured_result") or {}
                    if item.get("skill") == "pet_tracking" and payload.get("found") and payload.get("video_status") == "failed":
                        video_warnings.append(str(payload.get("video_error") or "pet_video_unavailable"))
                if video_warnings:
                    spoken = "已经找到豆豆，不过这次视频录制失败，没有视频同步到手机。"
                    for item in step_values:
                        if item.get("skill") == "feeder_control" and not item.get("skipped"):
                            if item.get("succeeded"):
                                spoken += "已经开始给它喂食了。"
                            else:
                                detail = str((item.get("result") or {}).get("spoken_summary") or "")
                                spoken += "投食没有成功。" + detail
            dry_run = validation_ok and not executed
            return {
                "ok": bool(all_succeeded and executed),
                "validation_ok": validation_ok,
                "executed": executed,
                "device_state_changed": False if dry_run else None,
                "skill": SCENARIO_TOOL_NAME,
                "scenario": name,
                "mode": "dry_run" if dry_run else "execute",
                "steps": step_values,
                "outcome_groups": [
                    {
                        "procedure": name,
                        "matched_outcome": selected.get("name") if selected else None,
                        "message": spoken,
                    }
                ],
                "error": None if all_succeeded else self._first_error(step_values),
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
                "video_warnings": video_warnings,
                "spoken_summary": (
                    "安全模拟校验通过，但本次场景没有实际执行。" if dry_run else spoken
                ),
            }
        except Exception as exc:
            return {
                "ok": False, "validation_ok": False, "executed": False,
                "skill": SCENARIO_TOOL_NAME, "scenario": name,
                "error": f"{type(exc).__name__}:{exc}",
                "spoken_summary": "场景流程校验失败，没有执行任何后续动作。",
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 1),
            }
        finally:
            self._lock.release()

    @staticmethod
    def _first_error(records: list[dict[str, Any]]) -> str:
        for item in records:
            if not item.get("succeeded") and not item.get("skipped"):
                return str(item.get("error") or "scenario_step_failed")
        return "scenario_prerequisite_failed"

    @classmethod
    def _condition(cls, condition: Any, records: dict[str, dict[str, Any]]) -> bool:
        if not condition:
            return True
        if not isinstance(condition, dict):
            return False
        if "all_ok" in condition and not all(records.get(str(item), {}).get("succeeded") for item in condition["all_ok"]):
            return False
        if "all_finished" in condition and not all(records.get(str(item), {}).get("finished") for item in condition["all_finished"]):
            return False
        if "any_failed" in condition and not any(
            records.get(str(item), {}).get("finished") and not records.get(str(item), {}).get("succeeded")
            for item in condition["any_failed"]
        ):
            return False
        if "any_executed_failed" in condition and not any(
            records.get(str(item), {}).get("finished")
            and not records.get(str(item), {}).get("skipped")
            and not records.get(str(item), {}).get("succeeded")
            for item in condition["any_executed_failed"]
        ):
            return False
        if "all_executed_failed" in condition and not all(
            records.get(str(item), {}).get("finished")
            and not records.get(str(item), {}).get("skipped")
            and not records.get(str(item), {}).get("succeeded")
            for item in condition["all_executed_failed"]
        ):
            return False
        field = condition.get("field")
        if isinstance(field, dict) and not cls._field_matches(field, records):
            return False
        fields = condition.get("any_fields")
        if isinstance(fields, list) and not any(cls._field_matches(item, records) for item in fields if isinstance(item, dict)):
            return False
        return True

    @staticmethod
    def _field_matches(field: dict[str, Any], records: dict[str, dict[str, Any]]) -> bool:
        record = records.get(str(field.get("step") or ""), {})
        value = _deep_find(record, str(field.get("path") or ""))
        if "equals" in field:
            return value == field["equals"]
        if "truthy" in field:
            return bool(value) is bool(field["truthy"])
        return value is not None
