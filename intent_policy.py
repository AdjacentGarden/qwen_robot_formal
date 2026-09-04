from __future__ import annotations

import re
from typing import Any


_QUERY_WORDS = (
    "什么", "哪些", "哪一", "多少", "几个", "几次", "几点", "什么时候",
    "记得", "记不记得", "有没有", "是否", "查一下", "查询", "告诉我", "回忆",
)

_HISTORY_ANCHORS = (
    "上一轮", "上一条", "上一次", "上次", "前一轮", "前两轮", "前几轮",
    "往前数", "最开始", "最早", "第一条", "第二条", "第三条", "第一个指令",
    "刚才", "之前", "以前", "今天", "昨天", "前天",
)

_COMMAND_CONTEXT = (
    "指令", "命令", "让你", "叫你", "执行", "操作", "任务", "说过", "问过",
    "做过", "完成", "结果", "个俯卧撑", "次俯卧撑", "运动了", "计数",
)

_CURRENT_ACTION_WORDS = (
    "现在", "马上", "立刻", "开始", "继续", "接着", "再做", "重做", "重新",
    "帮我", "陪我", "给我", "请你", "去做", "来一组", "再来", "恢复任务",
)

_TASK_TOPICS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("欢迎回家", "欢迎画面", "回家画面"), "欢迎回家"),
    (("会议", "开会", "ppt", "幻灯", "会议投影", "会议内容"), "会议"),
    (("豆豆", "找狗", "找宠物", "宠物", "小狗", "喂狗", "喂食", "投食"), "豆豆"),
    (("导航", "原点", "书房", "客厅白墙", "白墙"), "导航"),
    (("电影", "音乐", "歌曲", "视频", "媒体"), "播放"),
    (("抬头", "低头", "平视", "回正", "头部"), "头"),
    (("灯光", "客厅灯", "打开灯", "开灯", "关灯", "风扇", "投食器"), "控制"),
    (("俯卧撑",), "俯卧撑"),
    (("深蹲",), "深蹲"),
    (("引体向上", "引体"), "引体"),
    (("运动", "锻炼", "健身"), "运动"),
)

_RESULT_QUERY_MARKERS = (
    "结果", "怎么样", "如何", "为什么", "是否", "成功", "失败", "完成", "结束",
    "找到", "找着", "到达", "到了", "到没到", "有没有", "了吗", "没", "多少",
    "几个", "几次", "哪里", "哪儿", "什么时候", "几点", "是什么", "播放的什么",
)

_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _compact(value: Any) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;]", "", str(value or "").lower())


def _spoken_integer(value: str) -> int | None:
    text = _compact(value)
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[text]
    if "十" in text:
        left, right = text.split("十", 1)
        tens = _CHINESE_DIGITS.get(left, 1) if left else 1
        ones = _CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return None


def _explicit_current_execution(text: str) -> bool:
    """Return true only when historical context is an argument to a new action."""

    action_continuation = bool(
        re.search(
            r"(?:继续|接着|再做|重做|重新|开始|再来|恢复).{0,12}"
            r"(?:运动|俯卧撑|深蹲|引体|任务|操作)|"
            r"(?:上次|上一轮|刚才).{0,12}(?:继续|接着|再做|重做|重新|再来|恢复)",
            text,
        )
    )
    historical_result_question = bool(
        any(anchor in text for anchor in _HISTORY_ANCHORS)
        and (
            text.endswith(("吗", "么"))
            or any(marker in text for marker in _RESULT_QUERY_MARKERS)
        )
    )
    strong_result_question = bool(
        any(anchor in text for anchor in _HISTORY_ANCHORS)
        and text.endswith(("吗", "么"))
        and any(
            marker in text
            for marker in ("成功", "失败", "结果", "完成", "结束", "找到", "到达", "到了", "播放")
        )
    )
    # A past "resume" is not a new command: "上次继续俯卧撑后做了几个".
    # Require a retrospective result at the end; a new action after the query
    # (e.g. "上次做了几个，我现在接着做") keeps the existing action path.
    past_count_question = bool(
        any(anchor in text for anchor in _HISTORY_ANCHORS)
        and re.search(r"(?:做了|完成了|数了|计了)(?:多少个|几个|多少次|几次)$", text)
    )
    if strong_result_question or past_count_question or (historical_result_question and not action_continuation):
        return False
    current = any(word in text for word in _CURRENT_ACTION_WORDS)
    if not current:
        return False
    # “告诉我/帮我查上次……” is still a read-only request.  Current execution
    # needs an action continuation, not merely a polite query prefix.
    query_only = bool(
        re.search(r"(?:告诉我|帮我|请你)?(?:查|查询|回忆|说说).{0,8}(?:上次|上一|刚才|之前|最早)", text)
    )
    return action_continuation or (current and not query_only and not any(word in text for word in _QUERY_WORDS))


def _history_query(text: str) -> bool:
    if not any(anchor in text for anchor in _HISTORY_ANCHORS):
        return False
    if _explicit_current_execution(text):
        return False
    prospective = bool(
        re.search(
            r"(?:应该|适合|合适|准备|计划|打算|想要|想|要)"
            r".{0,8}(?:做|练|开始).{0,8}(?:多少|几个|俯卧撑|深蹲|引体|运动)",
            text,
        )
    )
    explicit_past = bool(re.search(r"(?:让你|叫你).{0,8}(?:执行过|做过)|(?:做了|完成了|数了|计了|执行过|做过)", text))
    if prospective and not explicit_past:
        return False
    has_question = any(word in text for word in _QUERY_WORDS) or text.endswith(("吗", "么"))
    has_command_context = any(word in text for word in _COMMAND_CONTEXT)
    task_result_question = bool(
        any(any(alias in text for alias in aliases) for aliases, _query in _TASK_TOPICS)
        and (has_question or any(marker in text for marker in _RESULT_QUERY_MARKERS))
    )
    # Result questions such as “上次俯卧撑做了九个吗” may contain neither a
    # question word nor “指令”, but are still plainly about a completed run.
    completed_result = bool(
        re.search(
            r"(?:上次|上一轮|刚才|之前).{0,12}"
            r"(?:做了|完成了|数了|计了).{0,8}(?:俯卧撑|深蹲|引体|个|次)",
            text,
        )
    )
    return (has_question and has_command_context) or task_result_question or completed_result


def _memory_query_arguments(text: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "scope": "command_history",
        "query_type": "latest",
        "limit": 1,
    }
    topic = next(
        (
            query
            for aliases, query in _TASK_TOPICS
            if any(alias in text for alias in aliases)
        ),
        "",
    )
    if "前天" in text:
        arguments.update(query_type="time_range", date_period="day_before_yesterday", limit=50)
    elif "昨天" in text:
        arguments.update(query_type="time_range", date_period="yesterday", limit=50)
    elif "今天" in text:
        arguments.update(query_type="time_range", date_period="today", limit=50)
    elif any(word in text for word in ("最开始", "最早", "第一条", "第一个指令")):
        arguments.update(query_type="first")
    elif re.search(r"最近(?:的)?(?:前|这)?两轮|最近两条", text):
        arguments.update(query_type="recent", limit=2)
    elif re.search(r"往前数(?:两|二|2)轮|前两轮(?:的)?(?:那一|那条|指令)", text):
        arguments.update(query_type="offset", offset=1)
    else:
        ordinal = re.search(r"第([零〇一二两三四五六七八九十\d]+)(?:条|个)(?:指令|命令|任务)?", text)
        if ordinal:
            position = _spoken_integer(ordinal.group(1))
            if position is not None and position > 1:
                arguments.update(query_type="ordinal", position=position)
        elif any(word in text for word in ("上一轮", "上一条", "上一次", "上次", "刚才", "前一轮")):
            arguments.update(query_type="latest")
    # “上一次俯卧撑做了几个” means the latest matching exercise, not the
    # latest unrelated robot command.  Positional command questions remain
    # strictly positional and therefore do not receive a topic filter.
    positional = arguments.get("query_type") in {"first", "ordinal", "offset", "recent"}
    if topic and not positional:
        arguments["query"] = topic
        if arguments.get("query_type") == "latest":
            arguments.update(query_type="search", limit=1)
    return arguments


def normalize_user_intent(value: Any) -> dict[str, Any]:
    """Classify the turn's authority before selecting a concrete tool.

    This layer deliberately answers only the high-risk boundary between a
    retrospective query and a present execution request.  Qwen remains the
    broad semantic planner for ordinary chat and device commands.
    """

    original = str(value or "").strip()
    text = _compact(original)
    if _history_query(text):
        return {
            "domain": "memory",
            "intent": "query_history",
            "operation": "query",
            "parameters": _memory_query_arguments(text),
            "constraints": {
                "allowed_tools": ["memory_query"],
                "forbid_hardware": True,
                "forbid_state_change": True,
            },
            "confidence": 1.0,
            "evidence": original,
        }
    return {
        "domain": "current" if _explicit_current_execution(text) else "conversation",
        "intent": "execute_or_chat",
        "operation": "execute" if _explicit_current_execution(text) else "respond",
        "parameters": {},
        "constraints": {},
        "confidence": 0.75,
        "evidence": original,
    }


def is_retrospective_query(value: Any) -> bool:
    intent = value if isinstance(value, dict) else normalize_user_intent(value)
    return intent.get("domain") == "memory" and intent.get("operation") == "query"


def enforce_turn_tool_policy(
    intent: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any], str]:
    """Apply final per-turn invariants immediately before dispatch."""

    arguments = dict(arguments or {})
    if not is_retrospective_query(intent):
        return str(tool_name), arguments, "unchanged"
    canonical = dict(intent.get("parameters") or {})
    if str(tool_name) == "memory_query" and arguments == canonical:
        return "memory_query", canonical, "allowed_memory_query"
    return "memory_query", canonical, "retrospective_query_forced_read_only"
