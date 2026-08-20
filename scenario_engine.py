from __future__ import annotations

import copy
import json
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

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
    "push_up_companion": ("俯卧撑", "做运动", "锻炼", "健身"),
    "pull_up_companion": ("引体向上", "引体", "单杠"),
    "squat_companion": ("深蹲", "下蹲", "蹲起"),
    "find_pet": ("豆豆", "小狗", "宠物", "找狗"),
    "find_pet_at": ("豆豆", "小狗", "宠物", "找狗"),
    "find_pet_here": ("豆豆", "小狗", "宠物", "找狗"),
    "find_and_feed_doudou": ("喂饭", "喂食", "吃饭", "饿了", "狗粮", "开饭"),
    "meeting_projection": ("会议", "开会", "开个会", "讨论", "汇报", "演示", "投屏", "投影", "ppt", "幻灯"),
    "meeting_projection_stop": ("会议", "投屏", "投影", "ppt", "幻灯"),
    "homecoming_welcome": ("回家", "到家", "回来了", "下班", "欢迎回家", "理想同学"),
    "rest_lighting": ("休息", "歇一会", "歇会", "睡一会", "困了", "躺一会", "放松一下"),
}
SHORT_AFFIRMATIONS = {
    "好", "好的", "好啊", "可以", "行", "没问题", "开始吧", "那就开始吧", "就这样吧",
}
SCENARIO_START_SPEECH = {
    "homecoming_welcome": (
        "欢迎回家。我先调整投影角度，马上播放欢迎画面。",
        "欢迎回来，我现在准备欢迎画面。",
    ),
    "push_up_companion": (
        "来吧，我先去客厅白墙，准备好后陪你做俯卧撑。",
        "好，我们活动一下。我先去客厅白墙准备俯卧撑计数。",
    ),
    "pull_up_companion": (
        "来吧，我先去客厅白墙，准备好后陪你做引体向上。",
        "好，我先到客厅白墙准备引体向上计数。",
    ),
    "squat_companion": (
        "来吧，我先去客厅白墙，准备好后陪你做深蹲。",
        "好，我先到客厅白墙准备深蹲计数。",
    ),
    "find_pet": (
        "收到，我现在去找豆豆，找到后马上告诉你。",
        "好，我去各个保存的位置找找豆豆，有结果就告诉你。",
    ),
    "find_pet_at": ("收到，我去你指定的地方找找豆豆。", "好，我只去你说的地点找豆豆。"),
    "find_pet_here": ("收到，我先在当前位置找找豆豆。", "好，我就在这里转一圈找找豆豆。"),
    "find_and_feed_doudou": (
        "收到，我先去找豆豆，找到后再给它投食。",
        "明白，我先找豆豆，确认找到后再投食。",
    ),
    "meeting_projection": (
        "收到，我先去书房，到了就为你准备会议投影。",
        "好，我现在前往书房，随后准备会议内容。",
    ),
    "meeting_projection_here": (
        "收到，我就在当前位置抬头并准备会议投影。",
        "好，我不移动位置，现在为你调整角度并打开会议内容。",
    ),
    "meeting_projection_stop": (
        "收到，我现在结束会议投影并恢复平视。",
        "好，我来关闭会议投影，然后让头部回到水平。",
    ),
    "rest_lighting": (
        "你先休息一下，我现在去客厅并把灯光调好。",
        "好，你放松一会儿，我去客厅调整灯光。",
    ),
    "living_room_light_service": (
        "收到，我现在去客厅，同时帮你调整灯光。",
        "好，我去客厅并把灯光一起调好。",
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
                        "duration": {"type": "number", "minimum": 1, "maximum": 600},
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
            r"理想(?:同学)?(?:呀|啊)?"
            r"(?:我)?(?:回来了|回家了|下班回来了)?",
            text,
        )
        direct_return = re.fullmatch(
            r"理想(?:同学)?(?:我)?(?:回来了|回家了|下班回来了)",
            text,
        )
        return bool(wake_greeting or direct_return)

    def infer_arguments(self, name: str, transcript: str) -> dict[str, Any]:
        text = _normalize_text(transcript)
        inferred: dict[str, Any] = {}
        if name in {"push_up_companion", "pull_up_companion", "squat_companion"}:
            if any(word in text for word in ("不用身份", "不要身份", "不识别人脸", "不用人脸", "不要人脸", "不用reid", "不要reid", "匿名")):
                inferred["identity_policy"] = "anonymous"
        if name == "find_pet_at":
            if _contains_term(text, "书房"):
                inferred["point"] = "study_projection"
            elif _contains_term(text, "餐厅") or _contains_term(text, "原点"):
                inferred["point"] = "origin"
            elif _contains_term(text, "客厅") or _contains_term(text, "白墙"):
                inferred["point"] = "white_wall"
        if name == "meeting_projection" and self._meeting_stay_put_requested(text):
            inferred["stay_put"] = True
        if name == "meeting_projection" and not inferred.get("stay_put"):
            if _contains_term(text, "书房"):
                inferred["point"] = "study_projection"
            elif _contains_term(text, "客厅") or _contains_term(text, "白墙"):
                inferred["point"] = "white_wall"
            elif _contains_term(text, "餐厅") or _contains_term(text, "原点"):
                inferred["point"] = "origin"
        return inferred

    @staticmethod
    def _meeting_stay_put_requested(transcript: str) -> bool:
        text = _normalize_text(transcript)
        meeting_topic = bool(
            re.search(r"会议|开(?:个|场|一下)?会|投影|投屏|ppt|幻灯|演示|汇报|会议内容", text)
        )
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
                r"(?:找|寻找|搜索|看看|去看)(?:一下)?(?:豆豆|小狗|狗狗|宠物|狗)",
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
        if name in {"push_up_companion", "pull_up_companion", "squat_companion"}:
            return self._fitness_negated(text)
        return False

    @staticmethod
    def _meeting_stop_negated(transcript: str) -> bool:
        text = _normalize_text(transcript)
        return bool(
            re.search(
                r"(?:不要|别|不用|无需|不需要)(?:帮我|给我)?"
                r"(?:关闭|关掉|停止|结束)(?:会议)?(?:投影|投屏|ppt|幻灯)",
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
                for term in ("找", "看看", "在哪", "去看", "瞧瞧", "寻找")
            )
        if name == "find_and_feed_doudou":
            pet = any(
                _contains_term(transcript, term)
                for term in ("豆豆", "小狗", "宠物", "狗")
            )
            feeding = any(_contains_term(transcript, term) for term in terms)
            return pet and feeding
        if name == "meeting_projection_stop":
            topic = any(_contains_term(transcript, term) for term in terms)
            closing = any(
                _contains_term(transcript, term)
                for term in (
                    "关闭", "关掉", "关上", "关投影", "停止", "停掉", "停播", "停下来",
                    "结束", "收起", "收起来", "不投了", "别播了", "取消", "退出",
                )
            )
            return topic and closing
        return any(_contains_term(transcript, term) for term in terms)

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
        if not re.match(r"^(?:你)?(?:会|能|可以|支持)", text):
            return False
        if any(term in text for term in ("帮我", "陪我", "给我", "现在", "马上", "开始")):
            return False
        return text.endswith(("吗", "么")) or any(term in text for term in ("什么功能", "会不会", "能不能做到"))

    @staticmethod
    def _informational_question(transcript: str) -> bool:
        text = _normalize_text(transcript)
        if re.match(r"^(?:(?:先|然后|再|最后|顺便))?(?:什么是|为什么|怎么|如何|介绍|讲讲|说说)", text):
            return True
        informational = any(
            term in text
            for term in ("是什么", "什么意思", "怎么做", "如何做", "有什么好处", "几点开始", "是否支持")
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
    ) -> tuple[bool, str]:
        """Validate Qwen's semantic scene choice without re-classifying it.

        Qwen already performs the broad semantic classification through the
        enum-constrained function call.  The local compiler keeps authority
        over conflicts, cancellations and minimum topic evidence, while exact,
        fuzzy and phonetic routes remain deterministic overrides.
        """
        requested = self.normalize_scenario_name(requested, transcript)
        if requested not in self.procedures:
            return False, "unknown_scenario"
        resolved = self.match(transcript) if matched is None else matched
        if resolved:
            if requested == resolved:
                return True, "local_match"
            if not allow_additional_intents:
                return False, "local_conflict"
        text = _normalize_text(transcript)
        if not text:
            return False, "empty_transcript"
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
        if not self._has_topic_evidence(requested, text):
            return False, "missing_topic_evidence"
        return True, "qwen_semantic_with_local_evidence"

    def match(self, transcript: str) -> str | None:
        text = _normalize_text(transcript)
        if not text:
            return None
        # Questions about capability describe a function; they are not an
        # instruction to operate hardware.  Requests such as “你能不能帮我…”
        # are excluded by _capability_question and continue normally.
        if self._capability_question(text):
            return None
        if self._informational_question(text):
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
        pet = "豆豆" in text or "狗" in text or "宠物" in text
        if pet and not feeding_negated and any(word in text for word in ("喂", "吃饭", "吃东西", "该吃", "饿了", "狗粮", "开饭")):
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
        meeting_topic = bool(
            re.search(r"会议|开(?:个|场|一下)?会|投影|投屏|ppt|幻灯|演示|汇报", text)
        )
        if not projection_stop_negated and meeting_topic and any(word in text for word in (*close, "别播", "取消")):
            return "meeting_projection_stop"
        if not fitness_negated and "引体" in text:
            return "pull_up_companion"
        if not fitness_negated and ("深蹲" in text or "下蹲" in text):
            return "squat_companion"
        if not fitness_negated and ("俯卧撑" in text or (("运动" in text or "锻炼" in text) and any(word in text for word in ("陪", "开始", "一起")))):
            return "push_up_companion"
        if (
            not projection_start_negated
            and not projection_stop_negated
            and meeting_topic
            and any(word in text for word in ("开", "开始", "投影", "投屏", "播放", "内容", "陪", "准备", "展示", "放出来"))
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

    def compile(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        procedure = self.procedures.get(name)
        if procedure is None:
            raise ScenarioError(f"unknown_scenario:{name}")
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
        count = self._speech_variant_counts.get(name, 0)
        self._speech_variant_counts[name] = count + 1
        return options[count % len(options)]

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
            if announce:
                speech_name = name
                fallback = f"收到，我现在开始{plan['description']}。"
                if name == "meeting_projection" and arguments.get("stay_put"):
                    speech_name = "meeting_projection_here"
                elif name == "meeting_projection" and arguments.get("point"):
                    point = POINT_SPOKEN_NAMES.get(
                        str(arguments.get("point")),
                        str(arguments.get("point")),
                    )
                    fallback = f"收到，我先去{point}，到了就为你准备会议投影。"
                self._emit_progress(
                    name,
                    "acknowledgement",
                    (
                        fallback
                        if name == "meeting_projection" and arguments.get("point") and not arguments.get("stay_put")
                        else self._scenario_start_speech(speech_name, fallback)
                    ),
                    step_count=len(plan["steps"]),
                )
            records: dict[str, dict[str, Any]] = {}
            for index, step in enumerate(plan["steps"]):
                if not self._argument_condition(step.get("enabled_if"), arguments):
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
                    records[step["id"]] = {
                        "id": step["id"], "skill": step["skill"], "action": step["action"],
                        "finished": True, "succeeded": False, "skipped": True,
                        "error": "prerequisite_not_satisfied",
                    }
                    continue
                call_args = dict(step["arguments"])
                if step["action"]:
                    call_args["action"] = step["action"]
                progress_text = self._step_progress_text(
                    name,
                    step,
                    {**arguments, **call_args},
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
                result = self.invoke_atomic(step["skill"], call_args)
                succeeded = bool(result.get("ok") or result.get("validation_ok"))
                records[step["id"]] = {
                    "id": step["id"], "skill": step["skill"], "action": step["action"],
                    "finished": True, "succeeded": succeeded, "skipped": False,
                    "result": result, "error": result.get("error"),
                }

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
                spoken = selected["text"]
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
                spoken = spoken.rstrip("。！!") + "。辛苦啦，先喝口水缓一缓。"
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
