"""Conversation-level task controls, distinct from media playback controls."""
import re

TOOL = {
    "type": "function", "function": {
        "name": "task_control",
        "description": "暂停、继续、取消正在执行或已暂停的长任务，或查询任务状态。暂停保留进度，stop 取消且不再恢复。电影/会议画面的暂停继续仍使用对应媒体工具；查询过去的任务用记忆工具。",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["pause", "resume", "stop", "status"]}
        }, "required": ["action"], "additionalProperties": False},
    },
}

def immediate_control(text, active):
    """Small unambiguous low-latency fallback; broader phrasing stays with Qwen."""
    if not active:
        return None
    value = re.sub(r"[\s，。！？、,.!?]", "", str(text))
    if re.fullmatch(r"(?:理想同学)?(?:请|帮我)?(?:先)?(?:暂停|暂停一下|暂停一会儿)(?:当前任务|任务|运动|俯卧撑|找狗|导航)?", value):
        return {"name": "task_control", "arguments": {"action": "pause"}}
    return None
