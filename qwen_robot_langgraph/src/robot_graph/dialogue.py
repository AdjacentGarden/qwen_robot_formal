from __future__ import annotations

import json
import re
import threading
import time
import uuid

import httpx
from langgraph.graph import END, START, StateGraph

from .contracts import Action, DialogueState, PolicyError
from .workflows import atomic_plan, build_plan


INTENT_INSTRUCTIONS = '''你是机器人意图解析器。只输出一个严格JSON对象；不要Markdown、解释或执行结果。
type只能是action、workflow、control、chat。禁止把具体动作名写进type。数字必须用半角阿拉伯数字，布尔值用true/false。
参考以下合法格式，按请求选择动作和参数，不要复制不相关参数：
抬头：{"type":"action","kind":"head.move","args":{"pose":"up"}}
低头pose为down。回正、请回正、保持水平、摆正、归中都必须返回head.move且pose为level。不支持指定角度。
前置拍照：{"type":"action","kind":"camera.capture","args":{"camera":"front"}}
后置、后面、后摄拍照camera必须为back；前置、前面、前摄必须为front。未说明前后应澄清，不能自行选择front。
开灯：{"type":"action","kind":"light.set","args":{"enabled":true}}
关灯enabled为false。
查询时间：{"type":"action","kind":"system.time","args":{}}
查询设备状态：{"type":"action","kind":"system.status","args":{}}
投食二十克：{"type":"action","kind":"feeder.feed","args":{"grams":20}}
投食必须明确克数，且为10到100之间的10的倍数。七十克输出70，八十克输出80；不得输出英文数字、null、None或单位文字。
会议投影：{"type":"workflow","name":"meeting","parameters":{}}
原地投影会议PPT：{"type":"workflow","name":"meeting_stationary","parameters":{}}
会议流程的parameters必须是空对象，不得添加action、content、camera、head.move等字段；流程自身已包含雷达、抬头和投影步骤。
只要请求出现原地、这里、当前位置、当前地点、原处、就地、不导航或不用移动，name必须为meeting_stationary，与“会议/投影/PPT”的词序和P P T之间是否有空格无关。不得在这些请求中返回meeting。
没有当前位置含义的会议请求才返回meeting，由执行层禁止底轮运动。
原地做十个深蹲：{"type":"workflow","name":"exercise_stationary","parameters":{"exercise":"squat","count":10}}
俯卧撑exercise为push_up，引体向上为pull_up；数量1到200，“十个”输出count为10，不明确数量应返回chat，不能输出null或None。
暂停任务：{"type":"control","command":"pause"}
继续任务command为resume，取消/结束任务command为cancel。关闭/关掉投影、结束/关闭会议也表示cancel。
多动作、否定、假设、过去记录、能力询问、参数不明、未列出的功能都返回chat，不要选相反动作或编造参数。
例如“不要开灯”“如果开会会怎样”“你能抬头吗”输出：{"type":"chat","reply":"请明确是否要执行一个具体操作。"}
普通聊天也返回chat及简短中文reply。不要声称硬件已经完成。'''

class JsonModel:
    """Small, per-node input. Full graph state and tool catalogs are never dumped here."""
    def __init__(self, url, model, api_key="", timeout=30, budget=None):
        self.url, self.model, self.api_key, self.timeout = url, model, api_key, timeout
        self.budget = budget

    def classify(self, text):
        self.last_response_info = {}
        instruction = INTENT_INSTRUCTIONS
        from urllib.parse import urlparse
        if urlparse(self.url).hostname not in {'127.0.0.1', 'localhost', '::1'}:
            if self.budget is None:
                raise RuntimeError('cloud_budget_not_configured')
            self.budget.reserve('chat_completion')
        response = httpx.post(self.url.rstrip("/") + "/chat/completions", headers={"Authorization": "Bearer " + self.api_key} if self.api_key else {}, json={"model": self.model, "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": text}], "temperature": 0, "max_tokens": 384, "chat_template_kwargs": {"enable_thinking": False}}, timeout=self.timeout)
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        self.last_response_info = {'usage': body.get('usage'), 'raw': content}
        from .intent_codec import decode_intent
        return decode_intent(content, text)


class ConservativeRouter:
    """Route intents through optional shortcuts or entirely through a model."""
    def __init__(self, model=None, local_matching=True):
        self.model = model
        self.local_matching_enabled = local_matching

    def classify(self, text):
        # Model-only mode disables every local intent shortcut. Safety and
        # grounding validation still run after the model proposes an intent.
        if not self.local_matching_enabled:
            if self.model:
                return self.model.classify(text)
            return {"type": "chat", "reply": "意图模型未配置，当前未执行操作。"}
        if text.startswith(('记住：', '记住:')):
            return {'type':'memory_save','text':text[3:].strip()}
        if text.strip() == '查看记忆':
            return {'type':'memory_query'}
        from .intent_policy import request_needs_clarification
        if request_needs_clarification(text):
            return {"type": "chat", "reply": "请明确一个需要执行的操作及其参数；当前未执行硬件操作。"}
        t = re.sub(r"[，。！？!?,\s]", "", text)
        controls = {"暂停": "pause", "暂停任务": "pause", "继续": "resume", "继续任务": "resume", "取消": "cancel", "停止": "cancel", "结束会议": "cancel"}
        if t in controls:
            return {"type": "control", "command": controls[t]}
        control = re.fullmatch(r'(?:机器人)?(?:请你|请|麻烦你|麻烦|帮我)?(暂停|继续|恢复|取消|结束|停止)(?:当前|目前|正在执行的|正在进行的)?(?:任务|会议)(?:一下|吧)?', t)
        if control:
            return {"type": "control", "command": {'暂停':'pause','继续':'resume','恢复':'resume','取消':'cancel','结束':'cancel','停止':'cancel'}[control[1]]}
        commands = {
            "抬头": ("head.move", {"pose": "up"}), "低头": ("head.move", {"pose": "down"}), "回正": ("head.move", {"pose": "level"}),
            "打开灯": ("light.set", {"enabled": True}), "关灯": ("light.set", {"enabled": False}),
            "前摄拍照": ("camera.capture", {"camera": "front"}), "后摄拍照": ("camera.capture", {"camera": "back"}),
            "现在几点": ("system.time", {}), "设备状态": ("system.status", {}),
        }
        if t in commands:
            kind, args = commands[t]
            return {"type": "action", "kind": kind, "args": args}
        # Common read-only requests are deterministic; do not spend an LLM call on them.
        if re.search(r'几点|几点钟|几点几分|当前时间|现在的?时间|北京时间|报[个一下]*时|查询.*时间', t):
            return {"type": "action", "kind": "system.time", "args": {}}
        if re.search(r'(设备|机器人|系统).*(状态|运行情况)|状态.*(设备|机器人|系统)', t):
            return {"type": "action", "kind": "system.status", "args": {}}
        if t in {"启动会议投影", "开始会议投影", "我要开会了"}:
            return {"type": "workflow", "name": "meeting", "parameters": {}}
        if t in {"原地启动会议投影", "原地会议投影", "原地投影"}:
            return {"type": "workflow", "name": "meeting_stationary", "parameters": {}}
        from .command_grammar import explicit_command
        explicit = explicit_command(t)
        if explicit is not None:
            return explicit
        if self.model:
            return self.model.classify(text)
        return {"type": "chat", "reply": "请明确要执行的操作；当前支持原地会议投影、抬头、回正、拍照和任务暂停/继续/取消。底轮已锁定。"}


class Dialogue:
    def __init__(self, runtime, router=None):
        self.runtime = runtime
        self.router = router or ConservativeRouter()
        self.lock = threading.RLock()
        self.graph = self._build()

    def _build(self):
        runtime = self.runtime

        def understand(s):
            try:
                intent = self.router.classify(s["text"])
                return {"intent": intent}
            except Exception as exc:
                return {"intent": {"type": "chat", "reply": "模型调用失败，未执行硬件操作。"}, "reply": "模型调用失败，未执行硬件操作。", "error": type(exc).__name__}

        def validate(s):
            i = s["intent"]
            schemas = {"action": {"type", "kind", "args"}, "workflow": {"type", "name", "parameters"}, "control": {"type", "command"}, "chat": {"type", "reply"}, "memory_save": {"type", "text"}, "memory_query": {"type"}}
            if not isinstance(i, dict) or i.get("type") not in schemas or set(i) - schemas[i["type"]]:
                return {"error": "invalid_intent_schema", "reply": "请求解析结果不完整，未执行操作。"}
            try:
                if i["type"] == "action":
                    if i.get("kind") not in {"head.move", "camera.capture", "light.set", "feeder.feed", "system.time", "system.status"}:
                        raise PolicyError("model_action_not_advertised")
                    runtime.policy.validate(Action(i["kind"], i.get("args", {})))
                elif i["type"] == "workflow":
                    plan = build_plan(i["name"], i.get("parameters"))
                    for item in plan["steps"] + plan["cleanup"]:
                        runtime.policy.validate(Action.parse(item))
                elif i["type"] == "control" and i.get("command") not in {"pause", "resume", "cancel"}:
                    raise PolicyError("invalid_control")
                elif i["type"] == "chat" and (not isinstance(i.get("reply"), str) or len(i["reply"]) > 1000):
                    raise PolicyError("invalid_chat_reply")
                elif i["type"] == "memory_save" and (not isinstance(i.get("text"), str) or not 0 < len(i["text"]) <= 2000 or not s["text"].startswith("记住")):
                    raise PolicyError("memory_requires_explicit_request")
                from .intent_policy import validate_current_turn
                validate_current_turn(s["text"], i)
                return {}
            except (ValueError, KeyError, TypeError) as exc:
                return {"error": str(exc), "reply": "底轮当前禁止运动，包含导航的场景未启动。" if "base_motion_forbidden" in str(exc) else "请求需要进一步明确，未执行操作。"}

        def dispatch(s):
            if s.get("error"):
                return {}
            i = s["intent"]
            try:
                if i["type"] == "chat":
                    return {"reply": i["reply"]}
                if i["type"] in {"action", "workflow"}:
                    plan = atomic_plan(Action(i["kind"], i.get("args", {}))) if i["type"] == "action" else build_plan(i["name"], i.get("parameters"))
                    plan["announce_result"] = True
                    task = runtime.submit(s["session_id"], s["turn_id"], plan)
                    return {"task_id": task, "reply": "任务已提交，正在等待执行结果。"}
                if i["type"] == "control":
                    rows = runtime.store.all("SELECT id FROM tasks WHERE session=? AND id NOT LIKE 'voice:%' AND status NOT IN ('completed','failed','cancelled','blocked','unknown')", (s["session_id"],))
                    if len(rows) != 1:
                        return {"reply": "没有唯一的活动任务，请在任务列表中指定要控制的任务。"}
                    result = runtime.control(s["session_id"], rows[0]["id"], i["command"])
                    return {"task_id": rows[0]["id"], "reply": {"pause": "已请求暂停，等待设备确认。", "resume": "已请求继续。", "cancel": "已请求结束，等待设备停止和清理。"}[i["command"]]}
                if i["type"] == "memory_save":
                    with runtime.store.tx() as db:
                        db.execute("INSERT OR IGNORE INTO memory VALUES(?,?,?,?)", (s["session_id"] + ":" + s["turn_id"], s["session_id"], i["text"], time.time()))
                    return {"reply": "已保存这条记忆。"}
                if i["type"] == "memory_query":
                    rows = runtime.store.all("SELECT text FROM memory WHERE session=? ORDER BY created DESC LIMIT 10", (s["session_id"],))
                    return {"reply": "；".join(x["text"] for x in rows) if rows else "没有找到保存的记忆。"}
            except Exception as exc:
                return {"error": str(exc), "reply": "任务未启动：" + str(exc)[:200]}
            return {}

        graph = StateGraph(DialogueState)
        graph.add_node("understand", understand)
        graph.add_node("validate", validate)
        graph.add_node("dispatch", dispatch)
        graph.add_edge(START, "understand")
        graph.add_edge("understand", "validate")
        graph.add_edge("validate", "dispatch")
        graph.add_edge("dispatch", END)
        return graph.compile()

    def turn(self, session, turn, text):
        if not isinstance(session, str) or not session or len(session) > 128 or not isinstance(turn, str) or not turn or len(turn) > 128:
            raise PolicyError("invalid_turn_identity")
        if not isinstance(text, str) or not 0 < len(text) <= 4000:
            raise PolicyError("invalid_user_text")
        with self.lock:
            key = session + ":" + turn
            old = self.runtime.store.one("SELECT * FROM turns WHERE id=?", (key,))
            if old:
                if old["text"] != text:
                    raise PolicyError("turn_id_payload_mismatch")
                return {"reply": old["reply"], "task_id": old["task"], "deduplicated": True}
            result = self.graph.invoke({"session_id": session, "turn_id": turn, "text": text, "error": "", "task_id": ""})
            with self.runtime.store.tx() as db:
                db.execute("INSERT INTO turns VALUES(?,?,?,?,?,?)", (key, session, text, result.get("reply", ""), result.get("task_id", ""), time.time()))
            return result
