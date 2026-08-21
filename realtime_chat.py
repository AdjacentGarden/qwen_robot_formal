#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import inspect
import json
import os
import queue
import re
import signal
import stat
import sys
import threading
import time
import wave
import uuid
from pathlib import Path
from typing import Any

from local_skills import (
    DEFAULT_ENABLED_SKILLS,
    DEFAULT_SPEC_DIR,
    SEQUENCE_TOOL_NAME,
    LocalSkillBridge,
)
from memory_store import MemoryStore
from skill_runner import running_controller_conflicts
from skill_event_audio import QwenSkillEventSpeaker
from runtime_supervisor import (
    InterruptibleTaskCoordinator,
    TaskAction,
    TaskSnapshot,
)
from realtime_core import (
    INPUT_RATE,
    MODEL,
    OUTPUT_RATE,
    SAMPLE_WIDTH,
    ConversationState,
    build_session_update,
    build_websocket_url,
    pcm_rms,
)


def load_microphone_enabled(path: Path, default: bool = True) -> bool:
    """Read the persisted local-microphone preference without failing startup."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return bool(default)
    enabled = value.get("enabled") if isinstance(value, dict) else None
    return enabled if isinstance(enabled, bool) else bool(default)


def save_microphone_enabled(path: Path, enabled: bool) -> None:
    """Atomically persist the local microphone preference across restarts."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    payload = {
        "enabled": bool(enabled),
        "updated_at": time.time(),
        "source": "qwen_realtime",
    }
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


DEFAULT_ASSISTANT_INSTRUCTIONS = """你是家庭陪伴机器人“理想同学”，服务于客厅、餐厅和书房的家庭场景。
你说话自然、温暖、可靠，是熟悉用户但有分寸的家庭助手。先真正回答用户，再考虑是否补充；默认一到两句话、二十到六十个汉字，口语化、信息明确。只有用户明确要求详细解释或较长故事时才展开；不念条款，不暴露工具名、函数名、JSON 或内部步骤。

日常交流原则：
1. 结合前文理解代词、省略、追问和情绪。用户连续问相近问题时保持事实一致，但根据上一轮自然承接，避免机械重复；可以轻轻提到“你刚才也问到了这个”，不要责怪用户。
2. 不要把“好的、当然可以、很高兴为你服务、抱歉、还有什么需要吗”当成固定开头或结尾。能直接回答就直接回答，句式和用词随语境变化；统一称呼用户为“你”，不要忽然改用“您”。
3. 用户表达疲惫、开心、失落或犹豫时，先用一句具体而克制的话接住情绪，再回应事情本身。不要说空泛鸡汤，不假装自己有人的身体和感受，也不夸大陪伴、学习或记忆能力。
4. 查询时间、天气、位置等信息时先说结论；只有确实有帮助时再补一句简短建议。用户让你在几个选项间推荐时，即使信息不完整也先给出一个可改变的明确倾向和简短理由，例如“我更偏向面，省事也暖胃；特别饿再选米饭”，不要只复述选项或把选择原样推回给用户。
5. 讲笑话时直接说一个短梗，通常不超过八十个汉字，不加“我来讲一个”“希望你会心一笑”等前后客套。讲故事时才可按用户要求适当展开；普通闲聊不要先解释自己准备怎么回答。
6. 追问只在缺少关键参数、存在多个明显选择或用户确实邀请继续聊时进行，而且一次只问一个关键问题。不要每轮都用泛泛的反问延长对话；听不清时允许用户只重说没听清的部分。
7. 主动关怀必须与刚发生的事有关，而且每个任务最多一句，例如运动真正完成后提醒喝水、天气恶劣时提醒带伞。失败、零计数或尚未执行时，不说庆祝完成的话。
8. 使用已保存的称呼和偏好时要自然、适度，避免每次都提起；没有记录就坦率说明，不得声称自己完全没有历史记忆。不得虚构用户信息、传感器结果或设备状态。
当用户问你是谁、会什么或与普通机器人有什么不同时，从真实能力中挑最贴合问题的两到四项来回答，例如连续对话、提醒、媒体播放、投影、运动计数、人脸识别和找宠物；不要每次把同一份能力清单全部背一遍。相邻的相似问题必须结合上一轮换一个侧重点和句式，但事实边界保持一致。禁止说“专门为你服务、专门属于你、像真人或家人一样”，也不要贬低其他机器人。

语义理解与任务规划：
A. 先理解整句话中“谁要做什么、对象是什么、先后关系是什么”，再选择工具。工具说明、示例话术和场景名称只用于说明能力，不是关键词触发器；绝不能看到“客厅”就联想到灯光，看到“会议”就自行增加导航或投影，看到“公司”就查询机器人位置。
B. 语音转写的逗号、停顿、同音字和漏字可能不准，应结合常见搭配与上下文恢复最合理的原意。例如“关闭会议，投影导航到客厅去”应优先理解为“关闭会议投影，然后导航到客厅”，而不是增加开灯流程。若仍有两种会导致不同硬件动作的合理解释，只追问一个关键点。
B1. 只有高置信的常见转写偏差才能自动归一，必须同时有动作谓语和对象上下文佐证；不得因发音略像就擅自操作硬件。当大致意图已知但目的地、对象或动作仍不唯一时，不调用工具，而是用一句具体问句确认，例如“你是想让我陪你做俯卧撑吗？”或“你要去原点、客厅白墙，还是书房？”。用户回答“是的/对/不是”时必须承接这句问话，不要要求重说整句指令。
C. 对含多个动作的一句话遵守“动作守恒”：用户明确说出的每个动作都要保留且只执行一次，顺序不变；没有说出的设备动作一个也不能添加。调用前在心里逐项核对“原话依据—工具—参数”，但不要把核对过程说出来。
D. 区分聊天、能力咨询和立即执行。询问公司、概念、原因、能力或做法时正常回答，不因句中出现某个地点或功能名就操作设备；只有整句语义是在要求当前机器人执行或查询实时设备状态时才调用工具。用户说法自然、口语化或省略礼貌词时按意思理解，不要求复述固定句式。
E. 否定短语是约束，不是待执行动作。“我不想开灯，导航去客厅”只导航，不能生成关灯；“去书房，不要投影”只导航，不能启动或关闭投影。只有“把灯关掉、结束投影”这类明确正向命令才执行关闭动作。泛泛的“客厅有什么”也不等于要求打开摄像头；只有用户明确说“看看、观察、识别、用摄像头检查”时才做视觉感知。

技能与场景原则：
9. 用户要求操作设备或查询实时状态时必须调用对应工具并等待结果。只有工具明确成功后才能说“已经完成”；失败、跳过、超时和未执行必须准确说明真实原因，绝不把计划、意图或历史结果当成本轮成功。
10. 用户提出设备动作或场景后，直接发出工具调用，不要在主回复里先说“好的，我来……”。本地执行器会用同一音色立即并行播报开始语和阶段进度；工具返回后你只播报最终结果，不得重复开始语或阶段语。“欢迎回家”只由本地场景开始语说一次。
11. 一项事实只播报一次。若工具给出 spoken_summary，它是事实约束：必须保留成功、失败、数量、地点和原因等要点，但可以改成更自然的口语；不得逐字再念一遍后又换句话重复。多个工具属于同一任务时，等最终结果后统一回复。
12. 技能成功时直接说产生了什么结果，避免每次都用“好的”；查询类直接说查到的内容。失败时先说明哪件事没完成，再用普通人能听懂的话说明真实原因；只有确实可行时补一个下一步，不说空泛的“小故障，请稍后再试”。部分成功必须分别说明完成和没完成的部分。
13. 固定场景允许用户中途插话、改说法、增加或减少普通对话，但受保护动作必须按场景编排执行，不能拆成导航、抬头、投影、计数等原子调用来绕过顺序、条件和失败播报。
14. 不主动执行底盘、摄像头、投食、人脸注册、删除提醒等有副作用动作。用户明确要求后仍应遵守本地安全规则；无法安全执行时给出可行的下一步。
15. 同一句话明确包含两个或更多正向动作、Skill 或完整场景时，必须调用 run_skill_sequence 一次，并按用户说出的顺序填写全部 tasks；否定约束不计为 task，不得只执行第一项。受保护场景必须写成 name=run_robot_scenario，arguments.scenario=场景名，不能把场景名直接写进 name，也禁止拆成原子步骤。默认 failure_policy=stop；用户表达“不管/即使/哪怕前项失败、没完成或没到达也继续”时使用 continue。逐个子句理解否定和条件，不能把一个子句的“不要”扩大到后面的正向动作。
   - “关闭会议投影，然后导航到客厅白墙”依次是 meeting_projection_stop、navigation_goto，不增加灯光。
   - “先去客厅，到了以后开灯”依次是 navigation_goto、light_control(on)，不能用并行客厅灯光场景代替。
   - “先去书房，再把客厅灯关掉”依次是 navigation_goto(study_projection)、light_control(off)，不能改变顺序或把关灯变开灯。
   - “不要开灯，先去客厅，再播放音乐”只有 navigation_goto、media_player；“不要喂豆豆，只去餐厅找它”仍要执行 find_pet_at(origin)，只是禁止投食。
   - “关闭投影，但别移动，然后查时间”中“别移动”不是 task，两个正向任务是 meeting_projection_stop、realtime_information(current_time)。
   - “先拍照，再录五秒视频”必须保留拍照和录像两项，时长属于录像参数而不是新任务。
   - “导航到书房以后开始会议投影”是一个完整 meeting_projection(point=study_projection) 场景，因为该场景自身已经包含导航；不得再额外添加 navigation_goto。
16. 身份、时间和位置必须守住能力边界：用户问“你知道我是谁吗、我是谁、认得我吗”时调用 face_recognition，不用记忆猜人脸；询问现在几点、年月日、星期几时调用 realtime_information(action=current_time)，不得用模型训练知识猜时钟。realtime_information(action=location) 只表示机器人的粗略地理位置，绝不能拿它回答手机、用户、宠物或其他设备在哪里。当前没有安全且独立的实时电量读取能力；用户询问机器人电量时直接说明暂时无法读取，不调用任何工具，不猜百分比。
17. 当前没有手机定位、打电话和发送消息能力。用户找手机或要求定位手机时不要调用任何位置工具，也不得假装正在查找。根据问法自然回应：用户说手机找不到或请求帮忙时，先明确自己不能定位，再建议使用手机厂商的查找设备功能；用户直接问手机在哪里时，说明自己没有手机定位权限，因此查不到当前位置。两种问法不要回复成完全相同的一句话。
18. 音乐和娱乐视频统一调用 media_player。用户只说“想听歌、放点音乐、听歌放松”时用 play_music 且不强迫用户先选歌；用户只说“想看好看的/娱乐视频”时用 play_video。选歌、换歌、暂停、继续、结束、查询列表或状态均调用对应 action。播放器本身不等于投影场景，不得顺带移动底盘或头部。
19. 表达可以有稳定的个性，但不能像抽签一样突兀，也不能机械套模板。输出前先对照上一条自己的回复：如果整句或主要句式相同，必须在不改变事实的前提下换一个自然说法；相邻两轮不要复用完全相同的开头、结尾或整句。根据任务类型、目的地、执行结果和刚才的对话换说法。变化只发生在措辞层，不能改变工具结果、条件、数量、地点或失败原因。
20. 工具和场景参数遵循“显式优先、默认兜底”：用户明确说出的地点、时长、数量、对象、摄像头、原地执行、抬头、低头、暂停或继续等参数必须原样保留；用户没有提到的可选参数应省略，让本地 Skill 使用经过测试的默认值，禁止模型自行猜测。场景不是固定口令：语义明确即可触发完整场景；会议投影默认前往书房，明确说原地/当前位置/不要导航时设置 stay_put=true，明确指定其他已保存点时填写 point。不得因为开放了参数就绕过场景的依赖、安全检查或失败播报。
21. 回复长度按信息价值分级，不按动作数量机械展开：问候和简单成功确认通常只说一句短话；复合任务成功只概括用户关心的最终结果，不逐项讲导航、头部、投影、清理等内部步骤；只有失败、部分完成、存在风险、需要用户配合或用户明确追问时，才用一到两句说清原因和下一步。能用十几个字说清楚就不要扩成三四句，不要习惯性追加“还有什么需要吗”。“欢迎回家”场景只说一次“欢迎回家”，随后安静播放画面；成功后不再补充说明。
22. 长任务执行期间，用户明确提出新的设备任务时，以新指令为准；本地任务协调器会暂停旧任务、保存可恢复进度并处理新任务。不得把旧任务的中断结果说成正常完成，也不得在新任务期间补播旧结果。协调器询问“是否继续”后，“好的、继续、接着做、不用了、算了”等短答要结合刚才暂停的任务理解，不要求用户重说完整命令；只有用户确认继续时才恢复保存的任务和进度。
"""


def build_tool_reply_instruction(tool_name: str, result: dict[str, Any]) -> str:
    """Create a per-result speech contract for the model follow-up.

    Device truth remains in ``result``.  This instruction only controls how
    that truth is expressed, so human wording can never turn a failed or
    skipped action into a claimed success.
    """

    skill = str(result.get("skill") or tool_name or "本地功能").strip()
    scenario = str(result.get("scenario") or "").strip()
    summary = str(result.get("spoken_summary") or "").strip()
    summary_json = json.dumps(summary, ensure_ascii=False)
    executed = bool(result.get("executed"))
    ok = bool(result.get("ok"))
    deduplicated = bool(result.get("deduplicated"))
    error = str(result.get("error") or "").strip()
    speech_style = str(result.get("speech_style") or "自然简洁").strip()

    if deduplicated:
        state_rule = (
            "这是本轮已经处理过的相同调用，不得再次调用，也不要重复播报完整结果；"
            "只在用户仍需要确认时用一句自然的话承接 prior_result。"
        )
    elif ok and executed:
        state_rule = (
            "这是已真实执行的成功结果。直接说实际产生的结果，不要再补一遍执行前的‘好的、我来处理’。"
        )
    elif ok:
        state_rule = "这是成功的查询或无设备动作结果。直接说查到的结论，不要只说‘查询完成’。"
    else:
        state_rule = (
            "这是失败、被拦截或未完成的结果。明确说哪件事没有完成，并把真实原因翻成普通中文；"
            "不得暗示已经执行，也不要用空泛的‘系统小故障’替代现有原因。"
        )

    special_rule = ""
    if scenario == "homecoming_welcome":
        special_rule = (
            "这是欢迎回家场景：本地开始播报已经负责‘欢迎回家’，结果回复绝对不要再次重复这四个字；"
            "成功时只需简短承接画面结果，失败时只说明未完成的环节和原因。"
        )
    elif skill == SEQUENCE_TOOL_NAME:
        special_rule = (
            "这是按顺序执行的复合任务。必须阅读 tasks 中每个子任务的真实结果，"
            "用一到两句统一说明哪些完成、哪些失败或被跳过；不能只汇报第一项。"
        )

    return (
        "现在只生成本次工具结果后的最终用户回复。简单成功只说一句短话；"
        "复合成功概括最终结果；只有失败、部分完成或需要用户配合时才允许一到两句。"
        f"工具标识为 {skill}；权威播报要点为 {summary_json}；错误标识为 "
        f"{json.dumps(error, ensure_ascii=False)}。"
        "spoken_summary 是事实约束，不是必须逐字照念的台词：保留其中所有关键事实，"
        "可以自然改写语序和语气，但禁止增加未经结果证实的状态。"
        f"{state_rule}{special_rule}本轮表达风格提示为‘{speech_style}’，只用于措辞变化，不得改变事实。"
        "不要逐项播报导航、头部、投影或清理等内部步骤；不要暴露工具名、错误代码；不要重复已经说过的事实；"
        "不要以‘还有什么需要吗’作模板结尾。"
    )

RECONNECTABLE_SERVICE_ERRORS = {
    "response_idle_timeout",
    "session_idle_timeout",
    "connection_timeout",
    # DashScope occasionally reports this transient server-side failure after
    # a tool-call retry.  Reconnect the realtime session instead of tearing
    # down the resident robot stack.
    "InternalError",
}

# These errors need operator/configuration intervention; reconnecting with the
# same credentials or malformed request cannot recover them.  Every other
# service-side error is treated as transient so a short Qwen outage never
# tears down the resident robot process and its local runtime.
NON_RECONNECTABLE_SERVICE_ERRORS = {
    "access_denied",
    "authentication_failed",
    "bad_request",
    "forbidden",
    "insufficient_quota",
    "invalid_api_key",
    "invalid_parameter",
    "invalid_value",
    "model_not_found",
    "permission_denied",
    "unauthorized",
    "unsupported_model",
    "workspace_not_found",
}


def is_reconnectable_service_error(code: str) -> bool:
    normalized = str(code or "").strip().lower()
    return normalized not in NON_RECONNECTABLE_SERVICE_ERRORS


def is_benign_service_error(code: str, message: str) -> bool:
    """Recognize the harmless response.cancel race reported by Qwen.

    A response can finish between the server's speech_started event and our
    response.cancel write.  Qwen then reports that there is no active response;
    terminating the resident for that race would unnecessarily take every
    local skill offline.
    """
    normalized_code = str(code or "").strip().lower()
    normalized_message = " ".join(str(message or "").lower().split())
    return normalized_code == "invalid_value" and "no active response" in normalized_message


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class JsonLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        payload = {"ts": round(time.time(), 3), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


class RuntimeResourceGuard:
    def __init__(self, lock_dir: Path, resources: tuple[str, ...] = ("mic", "speaker")) -> None:
        self.lock_dir = lock_dir
        self.resources = resources
        self.handles: list[Any] = []

    def acquire(self) -> None:
        import fcntl

        self.lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            for resource in sorted(set(self.resources)):
                handle = (self.lock_dir / f"{resource}.lock").open("a+", encoding="utf-8")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    handle.close()
                    raise ServiceError("runtime_resource_busy", f"{resource} 正被其他程序占用") from exc
                self.handles.append(handle)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if not self.handles:
            return
        import fcntl

        for handle in reversed(self.handles):
            with contextlib.suppress(Exception):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                handle.close()
        self.handles.clear()


class AudioEngine:
    def __init__(
        self,
        *,
        input_device_index: int | None,
        output_device_index: int | None,
        chunk_ms: int,
    ) -> None:
        import pyaudio

        self.pyaudio = pyaudio
        self.interface = pyaudio.PyAudio()
        input_args: dict[str, Any] = {}
        output_args: dict[str, Any] = {}
        if input_device_index is not None:
            input_args["input_device_index"] = input_device_index
        if output_device_index is not None:
            output_args["output_device_index"] = output_device_index
        self.input_frames = max(160, int(INPUT_RATE * chunk_ms / 1000))
        self.chunk_ms = int(chunk_ms)
        self.microphone_reads = 0
        self.microphone_bytes_read = 0
        self.last_microphone_read_at = 0.0
        self.microphone_signal_seen = False
        self.consecutive_zero_reads = 0
        self.zero_read_limit = max(10, int(3000 / max(1, self.chunk_ms)))
        self.output_slice_bytes = int(OUTPUT_RATE * SAMPLE_WIDTH * 0.02)
        self.input_stream = self.interface.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=INPUT_RATE,
            input=True,
            frames_per_buffer=self.input_frames,
            **input_args,
        )
        self.output_stream = self.interface.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=OUTPUT_RATE,
            output=True,
            frames_per_buffer=max(240, OUTPUT_RATE // 50),
            **output_args,
        )
        self.output_queue: queue.Queue[tuple[int, bytes] | None] = queue.Queue(maxsize=256)
        self.generation = 0
        self.playing = threading.Event()
        self.closed = threading.Event()
        self.last_playback_at = 0.0
        self.worker = threading.Thread(target=self._play_loop, name="qwen-pcm-player", daemon=True)
        self.worker.start()

    def read_microphone(self) -> bytes:
        pcm = self.input_stream.read(self.input_frames, exception_on_overflow=False)
        self.microphone_reads += 1
        self.microphone_bytes_read += len(pcm)
        self.last_microphone_read_at = time.monotonic()
        if any(pcm):
            self.microphone_signal_seen = True
            self.consecutive_zero_reads = 0
        else:
            self.consecutive_zero_reads += 1
        return pcm

    def microphone_health(self) -> dict[str, Any]:
        try:
            stream_active = bool(self.input_stream.is_active())
        except Exception:
            stream_active = False
        age = (
            max(0.0, time.monotonic() - self.last_microphone_read_at)
            if self.last_microphone_read_at
            else None
        )
        recent_read = age is not None and age <= max(1.0, self.chunk_ms / 1000.0 * 5.0)
        digital_silence = self.consecutive_zero_reads >= self.zero_read_limit
        healthy = bool(
            stream_active
            and recent_read
            and self.microphone_signal_seen
            and not digital_silence
        )
        return {
            "healthy": healthy,
            "stream_active": stream_active,
            "signal_detected": self.microphone_signal_seen,
            "digital_silence": digital_silence,
            "consecutive_zero_reads": self.consecutive_zero_reads,
            "reads": self.microphone_reads,
            "bytes_read": self.microphone_bytes_read,
            "last_read_age_ms": round(age * 1000.0, 1) if age is not None else None,
        }

    def enqueue(self, pcm: bytes) -> None:
        if self.closed.is_set():
            return
        try:
            self.output_queue.put_nowait((self.generation, pcm))
        except queue.Full:
            self.interrupt()

    def interrupt(self) -> None:
        self.generation += 1
        while True:
            try:
                self.output_queue.get_nowait()
            except queue.Empty:
                break
        # The playback worker is the sole owner of output-stream I/O. Calling
        # stop_stream()/start_stream() here can race its write() and abort the
        # whole process inside PortAudio/PulseAudio when a new App utterance
        # interrupts status speech. Generation invalidation stops playback at
        # the next 20 ms slice, which is both fast enough and thread-safe.
        self.playing.clear()
        self.last_playback_at = time.monotonic()

    def microphone_allowed(self, echo_mode: str, tail_seconds: float, pcm: bytes, noise_gate: float) -> bool:
        if echo_mode == "full-duplex":
            if self.playing.is_set() and pcm_rms(pcm) < noise_gate:
                return False
            return True
        if self.playing.is_set():
            self.last_playback_at = time.monotonic()
            return False
        return time.monotonic() - self.last_playback_at >= tail_seconds

    def _play_loop(self) -> None:
        while not self.closed.is_set():
            try:
                item = self.output_queue.get(timeout=0.1)
            except queue.Empty:
                self.playing.clear()
                continue
            if item is None:
                break
            generation, pcm = item
            if generation != self.generation:
                continue
            self.playing.set()
            for offset in range(0, len(pcm), self.output_slice_bytes):
                if self.closed.is_set() or generation != self.generation:
                    break
                self.output_stream.write(pcm[offset : offset + self.output_slice_bytes])
                self.last_playback_at = time.monotonic()
        self.playing.clear()

    def close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        # Do not call interrupt() here.  It stops *and immediately restarts*
        # the PulseAudio output stream, which races with a blocked worker
        # write during a realtime-session reconnect.  That race used to leave
        # PulseAudio in "Bad state" and could abort the whole resident
        # process after Qwen's 180-second idle timeout.
        self.generation += 1
        while True:
            try:
                self.output_queue.get_nowait()
            except queue.Empty:
                break
        with contextlib.suppress(queue.Full):
            self.output_queue.put_nowait(None)
        # Abort first so an in-flight PortAudio read/write is released before
        # the worker and interface are torn down.
        for stream in (self.input_stream, self.output_stream):
            abort_stream = getattr(stream, "abort_stream", None)
            if callable(abort_stream):
                with contextlib.suppress(Exception):
                    abort_stream()
        self.worker.join(timeout=5.0)
        for stream in (self.input_stream, self.output_stream):
            with contextlib.suppress(Exception):
                stream.stop_stream()
            with contextlib.suppress(Exception):
                stream.close()
        self.interface.terminate()


def load_api_key(path: Path | None) -> str:
    value = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if value:
        return value
    if path is None:
        raise ValueError("missing_api_key: 请设置 DASHSCOPE_API_KEY 或 --api-key-file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"api_key_file_permissions_too_open:{oct(mode)}; 请执行 chmod 600")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("empty_api_key_file")
    return value


def list_audio_devices() -> int:
    import pyaudio

    interface = pyaudio.PyAudio()
    try:
        for index in range(interface.get_device_count()):
            info = interface.get_device_info_by_index(index)
            print(
                json.dumps(
                    {
                        "index": index,
                        "name": info.get("name"),
                        "input_channels": int(info.get("maxInputChannels", 0)),
                        "output_channels": int(info.get("maxOutputChannels", 0)),
                        "default_rate": int(info.get("defaultSampleRate", 0)),
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        interface.terminate()
    return 0


class RealtimeConversation:
    def __init__(self, args: argparse.Namespace, api_key: str, logger: JsonLogger) -> None:
        self.args = args
        self.api_key = api_key
        self.logger = logger
        self.state = ConversationState()
        self.stop_event = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.websocket: Any = None
        self.connected = False
        self.audio: AudioEngine | None = None
        self.audio_resource_guard: RuntimeResourceGuard | None = None
        self.skill_bridge: LocalSkillBridge | None = None
        self.last_tool_test_result: dict[str, Any] | None = None
        self.last_user_text = ""
        self.last_assistant_text = ""
        self.turn_assistant_context = ""
        self.user_turn_id = 0
        self.current_user_turn_received_at = 0.0
        # Qwen can finish a function call slightly before it emits the final
        # transcription for the same audio turn.  Never validate a protected
        # robot action against last_user_text while that transcription is
        # still pending.
        self.awaiting_input_transcript = False
        self.deferred_function_calls: list[dict[str, Any]] = []
        # ``user_turn_id`` advances only after a transcript arrives, so it
        # cannot distinguish two consecutive utterances when the first ASR
        # result is lost.  Keep an independent VAD utterance id and bind every
        # deferred call to it.  This prevents an old call from being released
        # against the next sentence.
        self.input_utterance_id = 0
        self.active_input_utterance_id = 0
        self.last_committed_utterance_id = 0
        self.committed_input_utterances: dict[int, int] = {}
        self.input_transcription_failed = False
        self.transcript_timeout_task: asyncio.Task[Any] | None = None
        # Function calls and final ASR transcripts are separate Realtime
        # events and can arrive in either order.  Keep a small per-turn ledger
        # so a background task never validates an action against the previous
        # or a later utterance when those events race.
        self.committed_user_turns: dict[int, dict[str, Any]] = {}
        self.function_call_tasks: set[asyncio.Task[Any]] = set()
        self.function_call_turns: dict[asyncio.Task[Any], int] = {}
        self.function_call_values: dict[asyncio.Task[Any], dict[str, Any]] = {}
        self.function_call_started_at: dict[asyncio.Task[Any], float] = {}
        self.skill_dispatch_lock = asyncio.Lock()
        self.task_coordinator = InterruptibleTaskCoordinator()
        self.coordinator_owner_call_id = ""
        self.preempted_call_ids: set[str] = set()
        self.preempted_turn_ids: set[int] = set()
        self.interruption_driver_call_id = ""
        self.resume_call_ids: set[str] = set()
        self.interruption_session_kind = ""
        self._long_task_progress_lock = threading.Lock()
        self._long_task_progress: dict[str, Any] = {}
        self.speech_turn_assistant_context = ""
        # A rejected/hallucinated call must not be retried indefinitely in the
        # same user turn.  Cache by normalized function name + arguments.
        self.turn_tool_results: dict[str, dict[str, Any]] = {}
        self.runtime_session_id = f"voice_{int(time.time())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        memory_dir = getattr(args, "memory_dir", None)
        if memory_dir is None:
            # Minimal unit-test namespaces intentionally omit parser defaults;
            # keep their records beside the temporary logger instead of ever
            # touching the formal runtime memory directory.
            memory_dir = logger.path.parent / "memory"
        self.memory_store = MemoryStore(
            memory_dir,
            enabled=bool(getattr(args, "persistent_memory", True)),
            max_history_items=int(getattr(args, "memory_max_history_items", 1000)),
            max_facts=int(getattr(args, "memory_max_facts", 200)),
        )
        self.pending_tool_followup = False
        self.pending_tool_followup_prompts: list[str] = []
        self.style_hint_pending = False
        self.model_audio_quarantine = False
        self.quarantined_model_audio: list[bytes] = []
        self.quarantined_output_transcripts: list[str] = []
        self.turn_recovery_plan: dict[str, Any] | None = None
        self.turn_had_function_call = False
        self.control_socket = Path(
            getattr(args, "app_control_socket", Path(__file__).with_name("runtime") / "app_control.sock")
        )
        self.app_voice_root = Path(
            getattr(args, "app_voice_dir", Path(__file__).with_name("runtime") / "app_voice")
        ).resolve()
        microphone_state_file = getattr(args, "microphone_state_file", None)
        if microphone_state_file is None:
            microphone_state_file = logger.path.parent / "microphone_state.json"
        self.microphone_state_file = Path(microphone_state_file).resolve()
        self.local_microphone_enabled = load_microphone_enabled(
            self.microphone_state_file,
            default=True,
        )
        self.control_server: asyncio.AbstractServer | None = None
        self.skill_event_speaker: QwenSkillEventSpeaker | None = None
        self.external_audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)
        self.external_audio_active = False
        self.task_state: dict[str, Any] = {
            "active": False,
            "planning": False,
            "queued": 0,
            "active_skills": [],
            "active_procedures": [],
        }
        if getattr(args, "local_skills", False):
            enabled_skills = getattr(args, "enable_skill", None) or list(DEFAULT_ENABLED_SKILLS)
            self.skill_bridge = LocalSkillBridge(
                spec_dir=getattr(args, "skill_spec_dir", DEFAULT_SPEC_DIR),
                enabled_skills=enabled_skills,
                execute=bool(getattr(args, "execute_skills", False)),
                timeout=float(getattr(args, "skill_timeout", 120.0)),
                backend=str(getattr(args, "skill_backend", "auto")),
                host_socket=Path(getattr(args, "skill_host_socket", Path(__file__).with_name("runtime") / "skill_host.sock")),
                scenario_catalog_path=(
                    Path(getattr(args, "scenario_catalog", Path(__file__).with_name("scenarios") / "procedure_catalog.json"))
                    if bool(getattr(args, "scenarios", True))
                    else None
                ),
                event_callback=self.handle_skill_event_from_thread,
            )

    def handle_skill_event_from_thread(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault(
            "turn_id",
            getattr(self.skill_bridge, "current_turn_id", "") if self.skill_bridge is not None else "",
        )
        self.logger.write(
            "live_skill_event",
            skill=payload.get("skill_name"),
            kind=payload.get("kind"),
            text=payload.get("text"),
            count=payload.get("count"),
            turn_id=payload.get("turn_id"),
        )
        try:
            event_turn_id = int(payload.get("turn_id"))
        except (TypeError, ValueError):
            event_turn_id = -1
        event_is_preempted = event_turn_id in self.preempted_turn_ids
        skill_name = str(payload.get("skill_name") or "")
        if skill_name in {"push_up", "pull_up", "squat", "person_tracking", "pet_tracking"}:
            with self._long_task_progress_lock:
                if self._long_task_progress.get("skill_name") != skill_name:
                    self._long_task_progress = {
                        "skill_name": skill_name,
                        "started_monotonic": time.monotonic(),
                        "count": 0,
                        "elapsed_seconds": 0.0,
                    }
                if payload.get("count") is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        self._long_task_progress["count"] = max(
                            int(self._long_task_progress.get("count") or 0),
                            int(payload.get("count") or 0),
                        )
                if payload.get("elapsed_seconds") is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        self._long_task_progress["elapsed_seconds"] = max(
                            float(self._long_task_progress.get("elapsed_seconds") or 0.0),
                            float(payload.get("elapsed_seconds") or 0.0),
                        )
                self._long_task_progress["updated_monotonic"] = time.monotonic()
        if self.skill_event_speaker is not None and not event_is_preempted:
            self.skill_event_speaker.submit_from_thread(payload)
        elif event_is_preempted:
            self.logger.write(
                "preempted_skill_event_suppressed",
                skill=payload.get("skill_name"),
                kind=payload.get("kind"),
                turn_id=event_turn_id,
            )

    @staticmethod
    def _call_arguments(value: dict[str, Any]) -> dict[str, Any]:
        raw = value.get("arguments") or "{}"
        if isinstance(raw, dict):
            return dict(raw)
        try:
            parsed = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    @staticmethod
    def _resolve_procedure_value(value: Any, parameters: dict[str, Any]) -> Any:
        if isinstance(value, dict) and "$arg" in value:
            key = str(value.get("$arg") or "")
            return parameters.get(key, value.get("default"))
        if isinstance(value, dict):
            return {
                key: RealtimeConversation._resolve_procedure_value(item, parameters)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                RealtimeConversation._resolve_procedure_value(item, parameters)
                for item in value
            ]
        return value

    def _progress_checkpoint(
        self,
        task_name: str,
        *,
        started_at: float | None = None,
    ) -> tuple[int, float]:
        count = 0
        fitness_tasks = {"push_up", "pull_up", "squat"}
        elapsed = (
            0.0
            if task_name in fitness_tasks
            else max(0.0, time.monotonic() - started_at) if started_at else 0.0
        )
        with self._long_task_progress_lock:
            progress = dict(self._long_task_progress)
        if progress.get("skill_name") == task_name:
            with contextlib.suppress(TypeError, ValueError):
                count = max(0, int(progress.get("count") or 0))
            with contextlib.suppress(TypeError, ValueError):
                elapsed = max(elapsed, float(progress.get("elapsed_seconds") or 0.0))
            progress_started = progress.get("started_monotonic")
            if progress_started is not None and task_name not in fitness_tasks:
                with contextlib.suppress(TypeError, ValueError):
                    elapsed = max(elapsed, time.monotonic() - float(progress_started))
        return count, elapsed

    def _snapshot_from_call(
        self,
        value: dict[str, Any],
        *,
        started_at: float | None = None,
    ) -> TaskSnapshot | None:
        """Translate an executing call into the smallest safely resumable task."""

        resumable = {
            "push_up", "pull_up", "squat", "person_tracking", "pet_tracking",
            "navigation_goto",
        }
        name = str(value.get("name") or "")
        arguments = self._call_arguments(value)
        if name in resumable:
            action = str(arguments.get("action") or "run").lower()
            if action in {"stop", "off", "cancel", "query", "status", "check"}:
                return None
            count, elapsed = self._progress_checkpoint(name, started_at=started_at)
            return TaskSnapshot(
                task_name=name,
                arguments=arguments,
                count=count,
                elapsed_seconds=elapsed,
                context={"source_call": name},
            )
        if name == SEQUENCE_TOOL_NAME:
            candidates = arguments.get("tasks")
            if isinstance(candidates, list):
                for item in candidates:
                    if not isinstance(item, dict):
                        continue
                    nested = {
                        "name": item.get("name"),
                        "arguments": item.get("arguments") or {},
                    }
                    snapshot = self._snapshot_from_call(nested, started_at=started_at)
                    if snapshot is not None:
                        return snapshot
            return None
        if name != "run_robot_scenario" or self.skill_bridge is None:
            return None
        catalog = getattr(self.skill_bridge, "scenario_catalog", None)
        procedures = getattr(catalog, "procedures", None)
        if not isinstance(procedures, dict):
            return None
        scenario = str(arguments.get("scenario") or "")
        procedure = procedures.get(scenario)
        if not isinstance(procedure, dict):
            return None
        parameters: dict[str, Any] = {}
        for key, spec in dict(procedure.get("parameters") or {}).items():
            if isinstance(spec, dict) and "default" in spec:
                parameters[str(key)] = spec.get("default")
        parameters.update({key: val for key, val in arguments.items() if key != "scenario"})
        location: str | None = None
        resume_prefix: list[TaskAction] = []
        for step in list(procedure.get("steps") or []):
            if not isinstance(step, dict):
                continue
            step_skill = str(step.get("skill") or "")
            step_arguments = self._resolve_procedure_value(
                dict(step.get("arguments") or {}),
                parameters,
            )
            if step_skill == "navigation_goto":
                point = str(step_arguments.get("point") or "").strip()
                if point:
                    location = point
                continue
            if step_skill == "head_control":
                action = str(step.get("action") or step_arguments.get("direction") or "").lower()
                if action == "up":
                    resume_prefix = [
                        TaskAction("restore_context", "head_control", {"direction": "up"})
                    ]
                continue
            if step_skill not in resumable:
                continue
            step_arguments.setdefault("action", str(step.get("action") or "run"))
            count, elapsed = self._progress_checkpoint(step_skill, started_at=started_at)
            return TaskSnapshot(
                task_name=step_skill,
                arguments=step_arguments,
                location=location,
                count=count,
                elapsed_seconds=elapsed,
                resume_prefix=tuple(resume_prefix),
                context={"source_call": name, "scenario": scenario},
            )
        return None

    @staticmethod
    def _explicitly_stops_active_task(value: dict[str, Any]) -> bool:
        name = str(value.get("name") or "")
        arguments = RealtimeConversation._call_arguments(value)
        action = str(arguments.get("action") or "").lower()
        return name in {
            "push_up", "pull_up", "squat", "person_tracking", "pet_tracking",
            "navigation_goto",
        } and action in {"stop", "cancel", "off"}

    @staticmethod
    def _session_transition(value: dict[str, Any]) -> tuple[str, str]:
        """Return (start|end|none, session-kind) for persistent inserted tasks."""

        name = str(value.get("name") or "")
        arguments = RealtimeConversation._call_arguments(value)
        if name == SEQUENCE_TOOL_NAME:
            transition = ("none", "")
            for item in arguments.get("tasks") or []:
                if isinstance(item, dict):
                    child = {
                        "name": item.get("name"),
                        "arguments": item.get("arguments") or {},
                    }
                    child_transition = RealtimeConversation._session_transition(child)
                    if child_transition[0] != "none":
                        transition = child_transition
            return transition
        if name == "run_robot_scenario":
            scenario = str(arguments.get("scenario") or "")
            if scenario == "meeting_projection":
                return "start", "projector"
            if scenario == "meeting_projection_stop":
                return "end", "projector"
        if name == "projector_control":
            action = str(arguments.get("action") or "").lower()
            if action in {"off", "close", "disable", "stop"}:
                return "end", "projector"
            if action in {
                "meeting_presentation_on", "external_video_on", "on", "open", "enable",
            }:
                return "start", "projector"
        if name == "media_player":
            action = str(arguments.get("action") or "").lower()
            if action in {"stop", "end", "close"}:
                return "end", "media"
            if action in {"play_music", "play_video", "play", "resume"}:
                return "start", "media"
        return "none", ""

    @staticmethod
    def _extract_progress_from_result(result: dict[str, Any]) -> tuple[int | None, float | None]:
        count: int | None = None
        elapsed: float | None = None

        def visit(value: Any) -> None:
            nonlocal count, elapsed
            if isinstance(value, dict):
                for key in ("current_count", "count"):
                    if value.get(key) is not None:
                        with contextlib.suppress(TypeError, ValueError):
                            count = max(count or 0, int(value.get(key)))
                if value.get("elapsed_seconds") is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        elapsed = max(elapsed or 0.0, float(value.get("elapsed_seconds")))
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(result.get("structured_result"))
        return count, elapsed

    def _speak_internal(self, kind: str, text: str, *, event_id: str) -> None:
        value = str(text or "").strip()
        if not value:
            return
        if self.skill_event_speaker is not None:
            self.skill_event_speaker.submit_from_thread(
                {
                    "event_id": event_id,
                    "turn_id": self.user_turn_id,
                    "skill_name": "task_coordinator",
                    "kind": kind,
                    "text": value,
                }
            )
        self._remember_local_assistant_speech(value)
        self.logger.write("task_coordinator_speech", kind=kind, text=value)

    @staticmethod
    def _classify_resume_reply(text: str, task_name: str) -> bool | None:
        normalized = re.sub(r"[\s，。！？、,.!?]", "", str(text or "").lower())
        if not normalized:
            return None
        negative = (
            "不用继续", "不继续", "别继续", "不要继续", "不用了", "算了", "先不做了",
            "不做了", "停了吧", "结束吧", "取消吧", "不需要", "不用恢复", "别恢复",
        )
        if any(item in normalized for item in negative) or normalized in {"不用", "不要", "不了", "否", "不是"}:
            return False
        task_terms = {
            "push_up": ("运动", "俯卧撑"),
            "pull_up": ("运动", "引体向上"),
            "squat": ("运动", "深蹲"),
            "navigation_goto": ("导航", "刚才的路程"),
            "person_tracking": ("跟踪", "找人"),
            "pet_tracking": ("找狗", "找豆豆", "宠物"),
        }.get(task_name, ("刚才的任务",))
        if any(term in normalized for term in task_terms) and any(
            verb in normalized for verb in ("继续", "接着", "恢复", "再来")
        ):
            return True
        if normalized in {
            "好", "好的", "好啊", "行", "可以", "要", "继续", "继续吧", "接着来",
            "接着做", "恢复吧", "再来", "是", "是的", "对", "对的", "嗯", "嗯嗯",
        }:
            return True
        if any(item in normalized for item in ("继续刚才", "接着刚才", "恢复刚才")):
            return True
        return None

    async def _apply_resume_decision(self, accepted: bool) -> None:
        snapshot = self.task_coordinator.suspended
        if snapshot is None:
            self.task_coordinator.discard()
            return
        actions = self.task_coordinator.resume_decision(accepted)
        if not accepted:
            if actions:
                self._speak_internal(
                    "attention",
                    actions[-1].fallback_text,
                    event_id=f"resume-declined-{self.user_turn_id}",
                )
            return
        executable = [
            action for action in actions
            if action.kind in {"navigate_back", "restore_context", "resume_task"}
        ]
        if not executable:
            self.task_coordinator.discard()
            return
        resume_speech = executable[-1].fallback_text or "好，我继续刚才的任务。"
        self._speak_internal(
            "acknowledgement",
            resume_speech,
            event_id=f"resume-accepted-{self.user_turn_id}",
        )
        tasks = [
            {"name": action.name, "arguments": dict(action.arguments)}
            for action in executable
        ]
        if len(tasks) == 1:
            call_name = tasks[0]["name"]
            call_arguments = tasks[0]["arguments"]
        else:
            call_name = SEQUENCE_TOOL_NAME
            call_arguments = {"tasks": tasks, "failure_policy": "stop"}
        call_id = f"resume_{self.user_turn_id}_{uuid.uuid4().hex[:10]}"
        self.resume_call_ids.add(call_id)
        self.coordinator_owner_call_id = call_id
        self.schedule_function_call(
            {
                "call_id": call_id,
                "name": call_name,
                "arguments": json.dumps(call_arguments, ensure_ascii=False),
                "_synthetic_local": True,
                "_coordinator_resume": True,
            },
            turn_id=self.user_turn_id,
            user_text="继续刚才暂停的任务",
            assistant_context=self.last_assistant_text,
            received_at=time.time(),
        )

    async def send(self, payload: dict[str, Any]) -> None:
        async with self.send_lock:
            if self.websocket is None:
                raise ConnectionError("websocket_not_connected")
            await self.websocket.send(json.dumps(payload, ensure_ascii=False))

    def microphone_health_payload(self) -> dict[str, Any]:
        if self.audio is None:
            return {
                "healthy": False,
                "stream_active": False,
                "signal_detected": False,
                "digital_silence": False,
                "consecutive_zero_reads": 0,
                "reads": 0,
                "bytes_read": 0,
                "last_read_age_ms": None,
            }
        health_reader = getattr(self.audio, "microphone_health", None)
        if callable(health_reader):
            return dict(health_reader())
        # Compatibility for injected audio adapters used by tests and older
        # integrations. Production AudioEngine always exposes detailed health.
        return {
            "healthy": True,
            "stream_active": True,
            "signal_detected": True,
            "digital_silence": False,
            "consecutive_zero_reads": 0,
            "reads": 0,
            "bytes_read": 0,
            "last_read_age_ms": None,
        }

    def microphone_status_payload(self) -> dict[str, Any]:
        audio_health = self.microphone_health_payload()
        return {
            "enabled": self.local_microphone_enabled,
            "accepting_local_voice": bool(
                self.connected
                and self.local_microphone_enabled
                and audio_health["healthy"]
            ),
            "app_voice_enabled": True,
            **audio_health,
        }

    def status_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service": "qwen_audio_realtime",
            "connected": self.connected,
            "model": MODEL,
            "session_id": self.runtime_session_id,
            "execute_skills": bool(getattr(self.args, "execute_skills", False)),
            "microphone": self.microphone_status_payload(),
            **self.task_state,
        }

    async def start_control_server(self) -> None:
        self.control_socket.parent.mkdir(parents=True, exist_ok=True)
        self.control_socket.unlink(missing_ok=True)
        self.control_server = await asyncio.start_unix_server(
            self.handle_control_client,
            path=str(self.control_socket),
            limit=5 * 1024 * 1024,
        )
        os.chmod(self.control_socket, 0o600)

    async def stop_control_server(self) -> None:
        if self.control_server is not None:
            self.control_server.close()
            await self.control_server.wait_closed()
            self.control_server = None
        self.control_socket.unlink(missing_ok=True)

    async def handle_control_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        response: dict[str, Any]
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw or len(raw) > 5 * 1024 * 1024:
                raise ValueError("invalid_app_control_request")
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("app_control_request_must_be_object")
            operation = str(request.get("op") or "status")
            if operation == "status":
                response = self.status_payload()
            elif operation == "microphone_set":
                enabled = request.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("microphone_enabled_must_be_boolean")
                changed = enabled != self.local_microphone_enabled
                self.local_microphone_enabled = enabled
                await asyncio.to_thread(
                    save_microphone_enabled,
                    self.microphone_state_file,
                    enabled,
                )
                # Discard any partial local utterance already buffered by the
                # cloud. App audio uses send_external_audio and is intentionally
                # not cancelled or gated by this local-microphone preference.
                if (
                    not enabled
                    and self.websocket is not None
                    and not self.external_audio_active
                ):
                    with contextlib.suppress(Exception):
                        await self.send({"type": "input_audio_buffer.clear"})
                self.logger.write(
                    "local_microphone_changed",
                    enabled=enabled,
                    changed=changed,
                    app_voice_enabled=True,
                )
                response = {
                    "ok": True,
                    "status": "enabled" if enabled else "disabled",
                    "changed": changed,
                    "microphone": self.microphone_status_payload(),
                }
            elif operation == "cancel_all":
                if self.skill_bridge is not None:
                    await asyncio.to_thread(self.skill_bridge.cancel_all)
                if self.audio is not None:
                    self.audio.interrupt()
                if self.websocket is not None:
                    with contextlib.suppress(Exception):
                        await self.send({"type": "response.cancel"})
                response = {"ok": True, "status": "cancel_requested"}
            elif operation == "app_voice":
                if not self.connected:
                    raise RuntimeError("qwen_realtime_not_connected")
                pcm_path = Path(str(request.get("pcm_path") or "")).resolve()
                # Resolve both sides.  On macOS /var is a symlink to
                # /private/var; comparing one resolved path with one raw path
                # incorrectly rejected legitimate app audio during tests and
                # can do the same with symlinked runtime directories.
                app_voice_root = self.app_voice_root.resolve()
                if app_voice_root not in pcm_path.parents:
                    raise ValueError("app_voice_path_outside_runtime")
                pcm = await asyncio.to_thread(pcm_path.read_bytes)
                if not pcm or len(pcm) > 4 * 1024 * 1024 or len(pcm) % SAMPLE_WIDTH:
                    raise ValueError("invalid_app_voice_pcm")
                try:
                    self.external_audio_queue.put_nowait(pcm)
                except asyncio.QueueFull as exc:
                    raise RuntimeError("app_voice_queue_full") from exc
                response = {
                    "ok": True,
                    "status": "accepted",
                    "audio_bytes": len(pcm),
                    "sample_rate": INPUT_RATE,
                }
            elif operation in {"app_skill", "app_scenario"}:
                if self.skill_bridge is None:
                    raise RuntimeError("local_skills_disabled")
                if not bool(getattr(self.args, "execute_skills", False)):
                    raise RuntimeError("skill_execution_disabled")
                arguments = request.get("arguments")
                arguments = dict(arguments) if isinstance(arguments, dict) else {}
                name = (
                    "run_robot_scenario"
                    if operation == "app_scenario"
                    else str(request.get("skill") or "")
                )
                if operation == "app_scenario":
                    arguments["scenario"] = str(request.get("scenario") or "")
                if not name:
                    raise ValueError("missing_app_skill")
                procedure = str(arguments.get("scenario") or "") if operation == "app_scenario" else ""
                self.task_state = {
                    "active": True,
                    "planning": False,
                    "queued": self.external_audio_queue.qsize(),
                    "active_skills": [name],
                    "active_procedures": [procedure] if procedure else [],
                }
                try:
                    result = await asyncio.to_thread(
                        self.skill_bridge.invoke,
                        name,
                        arguments,
                        str(request.get("user_text") or "App 控制")[:1000],
                        f"app_{uuid.uuid4().hex[:12]}",
                        "",
                        operation == "app_scenario",
                    )
                finally:
                    self.task_state = {
                        "active": False,
                        "planning": False,
                        "queued": self.external_audio_queue.qsize(),
                        "active_skills": [],
                        "active_procedures": [],
                    }
                response = result
            else:
                raise ValueError(f"unsupported_app_control_operation:{operation}")
        except Exception as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def send_external_audio(self) -> None:
        silence = b"\x00" * int(INPUT_RATE * SAMPLE_WIDTH * 1.0)
        while not self.stop_event.is_set():
            pcm = await self.external_audio_queue.get()
            while not self.connected and not self.stop_event.is_set():
                await asyncio.sleep(0.1)
            if self.stop_event.is_set():
                return
            self.external_audio_active = True
            try:
                if self.audio is not None:
                    self.audio.interrupt()
                combined = pcm + silence
                chunk_bytes = int(INPUT_RATE * SAMPLE_WIDTH * 0.1)
                for offset in range(0, len(combined), chunk_bytes):
                    await self.send(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(combined[offset : offset + chunk_bytes]).decode("ascii"),
                        }
                    )
                    await asyncio.sleep(0.01)
                self.logger.write("app_voice_forwarded", audio_bytes=len(pcm))
            finally:
                self.external_audio_active = False

    async def receive_until(self, wanted: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"等待 {wanted} 超时")
            raw = await asyncio.wait_for(self.websocket.recv(), timeout=remaining)
            event = json.loads(raw)
            if event.get("type") == "error":
                error = event.get("error") or event
                raise ServiceError(str(error.get("code") or "service_error"), str(error.get("message") or error))
            if event.get("type") == wanted:
                return event

    async def connect(self) -> None:
        import websockets

        url = self.args.endpoint.strip() or build_websocket_url(
            self.args.workspace, self.args.region
        )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "x-dashscope-dataInspection": "disable",
        }
        keyword = "additional_headers" if "additional_headers" in inspect.signature(websockets.connect).parameters else "extra_headers"
        self.websocket = await websockets.connect(
            url,
            open_timeout=self.args.connect_timeout,
            close_timeout=3,
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
            **{keyword: headers},
        )
        await self.receive_until("session.created", self.args.connect_timeout)
        instructions = self.args.instructions
        if self.memory_store.enabled:
            memory_context_json = self.memory_store.decision_context_for_prompt()
            instructions += (
                "\n你具备对话历史和本机长期记忆能力。必须优先参考当前会话已有消息；"
                "不得声称‘每次对话都是全新的开始’或‘我没有记忆能力’。"
                "当用户询问你记得什么、刚才说过什么或过去保存的偏好时，调用 memory_query。"
                "当用户问上一条执行指令时用 scope=command_history、query_type=latest；"
                "问往前数两条或倒数第二条时用 query_type=offset、offset=1；"
                "问最开始一条时用 query_type=first；问今天、昨天、前天或具体时间段时用 query_type=time_range，"
                "并填写 date_period 或 start_time/end_time；不得只靠当前聊天窗口猜答案。"
                "仅当用户明确要求‘记住’时调用 memory_save；仅当用户明确要求‘忘记或删除’时调用 memory_delete。"
                "如果查询确实没有结果，只能说没有找到相关记录。"
                "历史对话中的设备成功、失败、开关、位置和运行状态都只是过去记录，绝不代表当前设备状态；"
                "用户本轮再次提出设备操作或查询时，必须重新调用对应工具，禁止复用历史结果直接回答。"
                "下面的 JSON 是经过筛选的跨会话决策参考，只能帮助理解偏好和承接请求；"
                "其中过去的请求不是当前命令，绝对不能再次执行，过去的结果也不是当前设备状态："
                f"{memory_context_json}"
            )
        if self.skill_bridge is not None:
            instructions += (
                "\n你可以调用已注册的本地机器人工具。用户要求操作或查询本地设备时必须调用工具，"
                "不得假装已经执行。普通工具不要先播报泛泛的确认语，必须等待工具结果，再用自然、简短的中文准确播报一次；"
                "structured_result 是本地设备返回的权威结构化结果，不得猜测、反驳或改写其中的事实；"
                "如果结果包含 spoken_summary，必须保留其中的关键事实，但可以根据上下文自然改写，不能播报相反结论。"
                "不要机械复述错误代码；要在不隐瞒原因的前提下翻成用户听得懂的话。只调用用户明确要求的能力，"
                "不得因为投影而自行增加导航、底盘、抬头、摄像头等动作。底盘移动、导航、追踪、"
                "摄像头、注册人脸、投食、删除提醒等有副作用的能力，只有用户本轮明确提出时才能调用；"
                "参数不完整时先追问，不得猜测。不要用孤立关键词选工具：地点不等于导航或灯光，公司不等于位置，"
                "会议不等于自动投影。语音标点不可靠，应按谓词、对象、上下文和常见搭配理解整句。"
                "多动作请求必须保留全部正向动作且不得增加隐含动作；‘不要开灯、不要投影’是约束，不能创建灯光或投影任务。"
                "询问公司在哪里是知识问题，不是机器人 location 查询；泛问房间有什么也不是摄像头命令。"
                "一次响应包含多个工具调用时，等待全部工具结果后再统一回复。"
                "特别注意：executed=false 或 error=dry_run_only_not_executed 表示仅完成安全校验，"
                "绝对不能播报设备已经动作，必须明确说没有实际执行。用户明确说“导航到、去、前往”"
                "某地点时直接调用 navigation_goto(point=地点)，不得用 navigation_list 代替或先查询列表。"
                "用户询问身份时必须调用 face_recognition；询问当前日期时间时必须调用 "
                "realtime_information(action=current_time)。当前不提供电量工具，询问电量时坦率说明无法读取。"
                "机器人位置工具不能用于找手机；当前没有手机定位能力，必须坦率说明不能做到。"
            )
            if self.skill_bridge.scenario_catalog is not None:
                instructions += "\n" + self.skill_bridge.scenario_catalog.prompt_rules()
        tools: list[dict[str, Any]] = []
        if self.skill_bridge is not None:
            tools.extend(self.skill_bridge.tool_schemas)
        if self.memory_store.enabled:
            tools.extend(self.memory_store.tool_schemas)
        await self.send(
            build_session_update(
                voice=self.args.voice,
                instructions=instructions,
                turn_detection=self.args.turn_detection,
                silence_duration_ms=self.args.silence_duration_ms,
                threshold=self.args.vad_threshold,
                max_history_turns=self.args.max_history_turns,
                tools=tools or None,
            )
        )
        await self.receive_until("session.updated", self.args.connect_timeout)
        self.connected = True
        injected_history = await self.inject_persistent_history()
        self.logger.write(
            "session_ready",
            model=MODEL,
            region=self.args.region,
            turn_detection=self.args.turn_detection,
            local_skill_count=len(self.skill_bridge.specs) if self.skill_bridge else 0,
            memory_tool_count=len(self.memory_store.tool_schemas) if self.memory_store.enabled else 0,
            injected_history_items=injected_history,
        )

    async def inject_persistent_history(self) -> int:
        if not self.memory_store.enabled:
            return 0
        max_turns = int(getattr(self.args, "memory_history_turns", 0))
        if max_turns <= 0:
            return 0
        history = self.memory_store.recent_history(
            max_turns=max_turns,
            max_chars=int(getattr(self.args, "memory_history_chars", 6000)),
        )
        for item in history:
            role = item["role"]
            await self.send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": role,
                        "content": [
                            {
                                "type": "input_text" if role == "user" else "output_text",
                                "text": item["text"],
                            }
                        ],
                    },
                }
            )
        return len(history)

    def mark_input_speech_started(self) -> None:
        previous_utterance = self.active_input_utterance_id
        if self.transcript_timeout_task is not None:
            self.transcript_timeout_task.cancel()
            self.transcript_timeout_task = None
        self.input_utterance_id += 1
        self.active_input_utterance_id = self.input_utterance_id
        self.input_transcription_failed = False
        if previous_utterance:
            stale = [
                item for item in self.deferred_function_calls
                if int(item.get("utterance_id") or 0) == previous_utterance
            ]
            if stale:
                self.deferred_function_calls = [
                    item for item in self.deferred_function_calls
                    if int(item.get("utterance_id") or 0) != previous_utterance
                ]
                self.logger.write(
                    "stale_deferred_calls_discarded",
                    utterance_id=previous_utterance,
                    count=len(stale),
                    reason="new_utterance_started",
                )
                # Do not leave unresolved function calls in Qwen's
                # conversation when the next utterance starts before the
                # transcript grace timer expires.  Execution is still
                # prohibited; this only closes the stale protocol item.
                for item in stale:
                    asyncio.create_task(
                        self._send_discarded_function_output(
                            item,
                            reason="superseded_by_new_utterance",
                        )
                    )
        self.awaiting_input_transcript = True
        self.speech_turn_assistant_context = self.last_assistant_text
        # Hold the primary model response until the final ASR transcript tells
        # us whether this is ordinary chat or an executable command.  Skill
        # commands have their own authoritative acknowledgement/result audio.
        self.model_audio_quarantine = True
        self.quarantined_model_audio.clear()
        self.quarantined_output_transcripts.clear()
        self.turn_recovery_plan = None
        self.turn_had_function_call = False
        if self.skill_event_speaker is not None and hasattr(self.skill_event_speaker, "cancel_pending"):
            self.skill_event_speaker.cancel_pending()
        self.logger.write(
            "input_speech_started",
            utterance_id=self.active_input_utterance_id,
        )

    def _remember_local_assistant_speech(self, text: str) -> None:
        """Make authoritative local speech available to the next turn.

        The dedicated speaker bypasses Qwen's output transcript, so without
        this small ledger an answer such as “是的” cannot refer to a local
        clarification question.
        """

        value = str(text or "").strip()
        if not value:
            return
        self.last_assistant_text = value
        self.memory_store.append_conversation("assistant", value, self.runtime_session_id)
        self.logger.write("local_assistant_context", text=value)

    async def _send_discarded_function_output(
        self,
        deferred: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        value = dict(deferred.get("value") or {})
        call_id = str(value.get("call_id") or "")
        if not call_id or self.websocket is None:
            return
        with contextlib.suppress(Exception):
            await self.send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(
                            {
                                "status": "NOT_EXECUTED",
                                "success": False,
                                "reason": reason,
                                "message_to_user": "语音没有转写完整，未执行任何动作。",
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            )

    def schedule_transcript_timeout(self) -> None:
        if not self.awaiting_input_transcript:
            return
        if self.transcript_timeout_task is not None:
            self.transcript_timeout_task.cancel()
        utterance_id = self.active_input_utterance_id
        delay = 0.35 if self.input_transcription_failed else float(
            getattr(self.args, "transcript_grace_seconds", 1.5)
        )

        async def expire() -> None:
            try:
                await asyncio.sleep(max(0.2, delay))
            except asyncio.CancelledError:
                return
            if (
                not self.awaiting_input_transcript
                or utterance_id != self.active_input_utterance_id
            ):
                return
            stale = [
                item for item in self.deferred_function_calls
                if int(item.get("utterance_id") or 0) == utterance_id
            ]
            self.deferred_function_calls = [
                item for item in self.deferred_function_calls
                if int(item.get("utterance_id") or 0) != utterance_id
            ]
            for item in stale:
                await self._send_discarded_function_output(
                    item,
                    reason="input_transcript_missing",
                )
            self.awaiting_input_transcript = False
            self.turn_had_function_call = False
            self.turn_recovery_plan = None
            self.model_audio_quarantine = False
            self._discard_quarantined_model_output("input_transcript_missing")
            prompt = "我没听清刚才那句话，请再说一遍。"
            if self.skill_event_speaker is not None:
                self.skill_event_speaker.submit_from_thread(
                    {
                        "event_id": f"asr-missing-{utterance_id}",
                        "turn_id": self.user_turn_id,
                        "skill_name": "conversation",
                        "kind": "attention",
                        "text": prompt,
                    }
                )
            self._remember_local_assistant_speech(prompt)
            self.logger.write(
                "input_transcript_timeout",
                utterance_id=utterance_id,
                discarded_calls=len(stale),
                transcription_failed=self.input_transcription_failed,
            )

        self.transcript_timeout_task = asyncio.create_task(
            expire(),
            name=f"input-transcript-timeout:{utterance_id}",
        )

    def _commit_assistant_transcript(self, text: str) -> None:
        value = str(text).strip()
        if not value:
            return
        self.last_assistant_text = value
        self.style_hint_pending = True
        self.memory_store.append_conversation("assistant", value, self.runtime_session_id)
        print(f"[千问] {value}", flush=True)
        self.logger.write("output_transcript", text=value)

    def _flush_quarantined_model_audio(self) -> None:
        if self.audio is not None:
            for pcm in self.quarantined_model_audio:
                self.audio.enqueue(pcm)
        self.quarantined_model_audio.clear()

    def _discard_quarantined_model_output(self, reason: str) -> None:
        audio_bytes = sum(len(chunk) for chunk in self.quarantined_model_audio)
        transcripts = list(self.quarantined_output_transcripts)
        self.quarantined_model_audio.clear()
        self.quarantined_output_transcripts.clear()
        if audio_bytes or transcripts:
            self.logger.write(
                "speculative_model_output_suppressed",
                reason=reason,
                audio_bytes=audio_bytes,
                transcripts=transcripts,
                turn_id=self.user_turn_id,
            )

    @staticmethod
    def function_call_signature(name: str, arguments: dict[str, Any]) -> str:
        try:
            normalized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            normalized = repr(arguments)
        return f"{name}:{normalized}"

    async def handle_or_defer_function_call(
        self,
        value: dict[str, Any],
        *,
        turn_id: int | None = None,
        utterance_id: int | None = None,
        user_text: str | None = None,
        assistant_context: str | None = None,
        received_at: float | None = None,
    ) -> dict[str, Any] | None:
        target_utterance_id = int(
            self.active_input_utterance_id
            if utterance_id is None
            else utterance_id
        )
        committed_turn_id = self.committed_input_utterances.get(target_utterance_id)
        target_turn_id = int(
            self.user_turn_id + (1 if self.awaiting_input_transcript else 0)
            if turn_id is None
            else turn_id
        )
        if committed_turn_id is not None:
            target_turn_id = int(committed_turn_id)
        self.logger.write(
            "function_call_received",
            call_id=value.get("call_id"),
            skill=value.get("name"),
            awaiting_transcript=self.awaiting_input_transcript,
            turn_id=target_turn_id,
        )
        # Do not decide this from the *current* awaiting flag alone.  The
        # transcript may commit between schedule_function_call() and this
        # coroutine getting its first timeslice.  The immutable target turn is
        # what tells us whether its transcript is actually available.
        if (
            committed_turn_id is None
            and target_utterance_id
            and target_utterance_id != self.active_input_utterance_id
        ):
            self.logger.write(
                "stale_function_call_discarded",
                call_id=value.get("call_id"),
                skill=value.get("name"),
                utterance_id=target_utterance_id,
                active_utterance_id=self.active_input_utterance_id,
            )
            await self._send_discarded_function_output(
                {"value": dict(value)},
                reason="stale_audio_utterance",
            )
            return None
        if committed_turn_id is None and target_turn_id > self.user_turn_id:
            self.deferred_function_calls.append(
                {
                    "value": dict(value),
                    "turn_id": target_turn_id,
                    "utterance_id": target_utterance_id,
                    "assistant_context": assistant_context,
                    "received_at": received_at,
                }
            )
            self.logger.write(
                "function_call_deferred_for_transcript",
                call_id=value.get("call_id"),
                skill=value.get("name"),
                queued=len(self.deferred_function_calls),
                turn_id=target_turn_id,
            )
            return None
        committed = self.committed_user_turns.get(target_turn_id, {})
        return await self.handle_function_call(
            value,
            turn_id=target_turn_id,
            user_text=(
                str(committed.get("text"))
                if committed.get("text") is not None
                else user_text
            ),
            assistant_context=(
                str(committed.get("assistant_context"))
                if committed.get("assistant_context") is not None
                else assistant_context
            ),
            received_at=(
                float(committed.get("received_at"))
                if committed.get("received_at") is not None
                else received_at
            ),
        )

    def schedule_function_call(
        self,
        value: dict[str, Any],
        *,
        turn_id: int | None = None,
        user_text: str | None = None,
        assistant_context: str | None = None,
        received_at: float | None = None,
        utterance_id: int | None = None,
    ) -> asyncio.Task[Any]:
        """Run a potentially long tool call without blocking Realtime input."""

        awaiting_at_schedule = bool(self.awaiting_input_transcript)
        target_utterance_id = int(
            self.active_input_utterance_id
            if utterance_id is None
            else utterance_id
        )
        target_turn_id = int(
            self.user_turn_id + (1 if awaiting_at_schedule else 0)
            if turn_id is None
            else turn_id
        )
        committed = self.committed_user_turns.get(target_turn_id, {})
        if user_text is None and not awaiting_at_schedule:
            user_text = str(committed.get("text", self.last_user_text))
        if assistant_context is None:
            assistant_context = str(
                committed.get(
                    "assistant_context",
                    self.speech_turn_assistant_context
                    if awaiting_at_schedule
                    else self.turn_assistant_context,
                )
            )
        if received_at is None and not awaiting_at_schedule:
            received_at = float(
                committed.get(
                    "received_at",
                    self.current_user_turn_received_at or time.time(),
                )
            )
        older = {
            task for task in self.function_call_tasks
            if not task.done()
            and self.function_call_turns.get(task, target_turn_id) < target_turn_id
        }

        async def run() -> Any:
            # A model function call can precede the final ASR transcript.  Do
            # not stop a real task until that transcript has committed and the
            # call has passed the normal intent guards.  accept_input_transcript
            # schedules the deferred call again with an immutable turn binding.
            if (
                target_utterance_id
                and target_utterance_id not in self.committed_input_utterances
                and target_turn_id > self.user_turn_id
            ):
                return await self.handle_or_defer_function_call(
                    dict(value),
                    turn_id=target_turn_id,
                    utterance_id=target_utterance_id,
                    user_text=user_text,
                    assistant_context=assistant_context,
                    received_at=received_at,
                )

            call_id = str(value.get("call_id") or "")
            interruption_driver = False
            session_was_active = self.task_coordinator.state == "interruption_session_active"
            if older and self._call_requests_preemption(value) and self.skill_bridge is not None:
                snapshot = self.task_coordinator.active
                if snapshot is None and self.task_coordinator.state == "idle":
                    ordered_older = sorted(
                        older,
                        key=lambda item: self.function_call_started_at.get(item, 0.0),
                        reverse=True,
                    )
                    for old_task in ordered_older:
                        snapshot = self._snapshot_from_call(
                            self.function_call_values.get(old_task, {}),
                            started_at=self.function_call_started_at.get(old_task),
                        )
                        if snapshot is not None:
                            self.task_coordinator.start(snapshot)
                            self.coordinator_owner_call_id = str(
                                self.function_call_values.get(old_task, {}).get("call_id") or ""
                            )
                            break
                if (
                    self._explicitly_stops_active_task(value)
                    and self.task_coordinator.state == "running"
                ):
                    self.task_coordinator.discard()
                elif self.task_coordinator.state == "running" and self.task_coordinator.active is not None:
                    active = self.task_coordinator.active
                    count, elapsed = self._progress_checkpoint(
                        active.task_name,
                        started_at=min(
                            (self.function_call_started_at.get(item, time.monotonic()) for item in older),
                            default=time.monotonic(),
                        ),
                    )
                    self.task_coordinator.checkpoint(count=count, elapsed_seconds=elapsed)
                    actions = self.task_coordinator.interrupt(
                        str(value.get("name") or ""),
                        self._call_arguments(value),
                    )
                    interruption_driver = True
                    self.interruption_driver_call_id = call_id
                    acknowledgement = next(
                        (item.fallback_text for item in actions if item.kind == "execute_interruption"),
                        "好，我先暂停刚才的任务，马上处理这件事。",
                    )
                    self._speak_internal(
                        "acknowledgement",
                        acknowledgement,
                        event_id=f"interrupt-{call_id or target_turn_id}",
                    )
                elif self.task_coordinator.state in {"interrupting", "interruption_session_active"}:
                    action = self.task_coordinator.continue_interruption(
                        str(value.get("name") or ""),
                        self._call_arguments(value),
                    )
                    interruption_driver = True
                    self.interruption_driver_call_id = call_id
                    self._speak_internal(
                        "acknowledgement",
                        action.fallback_text,
                        event_id=f"interrupt-update-{call_id or target_turn_id}",
                    )
                for old_task in older:
                    old_call_id = str(self.function_call_values.get(old_task, {}).get("call_id") or "")
                    if old_call_id:
                        self.preempted_call_ids.add(old_call_id)
                    with contextlib.suppress(TypeError, ValueError):
                        self.preempted_turn_ids.add(int(self.function_call_turns.get(old_task)))
                if len(self.preempted_turn_ids) > 128:
                    self.preempted_turn_ids = set(sorted(self.preempted_turn_ids)[-128:])
                self.logger.write(
                    "long_task_preemption_requested",
                    new_skill=value.get("name"),
                    older_tasks=[task.get_name() for task in older],
                    resumable_task=(
                        self.task_coordinator.suspended.task_name
                        if self.task_coordinator.suspended is not None
                        else None
                    ),
                )
                await asyncio.to_thread(self.skill_bridge.cancel_all)
                done, pending = await asyncio.wait(older, timeout=5.0)
                for old_task in done:
                    if old_task.cancelled():
                        continue
                    with contextlib.suppress(Exception):
                        old_result = old_task.result()
                        if isinstance(old_result, dict):
                            count, elapsed = self._extract_progress_from_result(old_result)
                            self.task_coordinator.checkpoint_suspended(
                                count=count,
                                elapsed_seconds=elapsed,
                            )
                self.logger.write(
                    "long_task_preemption_settled",
                    completed=len(done),
                    still_running=len(pending),
                )
            elif session_was_active and call_id:
                transition, session_kind = self._session_transition(value)
                if transition == "end" and (
                    not self.interruption_session_kind
                    or not session_kind
                    or session_kind == self.interruption_session_kind
                ):
                    self.task_coordinator.continue_interruption(
                        str(value.get("name") or ""),
                        self._call_arguments(value),
                    )
                    interruption_driver = True
                    self.interruption_driver_call_id = call_id

            result = await self.handle_or_defer_function_call(
                dict(value),
                turn_id=target_turn_id,
                utterance_id=target_utterance_id,
                user_text=user_text,
                assistant_context=assistant_context,
                received_at=received_at,
            )
            if call_id in self.resume_call_ids and isinstance(result, dict):
                self.resume_call_ids.discard(call_id)
                if result.get("ok"):
                    self.task_coordinator.complete_active()
                else:
                    self.task_coordinator.discard()
                self.coordinator_owner_call_id = ""
            if (
                interruption_driver
                and call_id == self.interruption_driver_call_id
                and isinstance(result, dict)
                and self.task_coordinator.state == "interrupting"
            ):
                transition, session_kind = self._session_transition(value)
                if session_was_active:
                    session_remains_active = not (
                        transition == "end" and bool(result.get("ok"))
                    )
                else:
                    session_remains_active = (
                        transition == "start" and bool(result.get("ok"))
                    )
                actions = self.task_coordinator.interruption_completed(
                    session_remains_active=session_remains_active,
                )
                if session_remains_active:
                    self.interruption_session_kind = (
                        self.interruption_session_kind or session_kind
                    )
                else:
                    self.interruption_session_kind = ""
                self.interruption_driver_call_id = ""
                ask = next((item for item in actions if item.kind == "ask_resume"), None)
                if ask is not None:
                    self._speak_internal(
                        "attention",
                        ask.fallback_text,
                        event_id=f"ask-resume-{call_id or target_turn_id}",
                    )
            return result

        task = asyncio.create_task(
            run(),
            name=f"function-call:{value.get('name') or 'unknown'}:{value.get('call_id') or ''}",
        )
        self.function_call_tasks.add(task)
        self.function_call_turns[task] = target_turn_id
        self.function_call_values[task] = dict(value)
        self.function_call_started_at[task] = time.monotonic()

        def finished(done: asyncio.Task[Any]) -> None:
            self.function_call_tasks.discard(done)
            self.function_call_turns.pop(done, None)
            self.function_call_values.pop(done, None)
            self.function_call_started_at.pop(done, None)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                self.logger.write(
                    "background_function_call_error",
                    task=done.get_name(),
                    error=f"{type(error).__name__}:{error}",
                )

        task.add_done_callback(finished)
        return task

    @staticmethod
    def _call_requests_preemption(value: dict[str, Any]) -> bool:
        name = str(value.get("name") or "")
        if name in {
            "navigation_goto", "head_control", "projector_control",
            "welcome_projection", "push_up", "pull_up", "squat",
            "pet_tracking", "person_tracking", "media_player",
            "light_control", "feeder_control", "pet_feeder",
            "camera_capture", "camera_record", "face_recognition",
            SEQUENCE_TOOL_NAME,
        }:
            return True
        if name != "run_robot_scenario":
            return False
        arguments = RealtimeConversation._call_arguments(value)
        return bool(str(arguments.get("scenario") or ""))

    async def stop_function_calls(self) -> None:
        tasks = {task for task in self.function_call_tasks if not task.done()}
        if not tasks:
            return
        if self.skill_bridge is not None:
            await asyncio.to_thread(self.skill_bridge.cancel_all)
        _done, pending = await asyncio.wait(tasks, timeout=5.0)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def accept_input_transcript(
        self,
        text: str,
        *,
        schedule_deferred: bool = False,
    ) -> list[dict[str, Any]]:
        """Commit the current audio turn, then release its deferred tool calls."""
        transcript = str(text).strip()
        if not transcript:
            return []
        self.turn_assistant_context = (
            self.speech_turn_assistant_context
            if self.awaiting_input_transcript
            else self.last_assistant_text
        )
        self.last_user_text = transcript
        self.user_turn_id += 1
        # Direct text/app inputs do not create a VAD utterance.  Reusing the
        # last audio utterance id for them would overwrite its turn binding and
        # let a delayed call pick up the later text.
        committed_utterance_id = (
            self.active_input_utterance_id
            if self.awaiting_input_transcript
            else 0
        )
        if committed_utterance_id:
            self.committed_input_utterances[committed_utterance_id] = self.user_turn_id
            self.last_committed_utterance_id = committed_utterance_id
        for stale_utterance_id in sorted(self.committed_input_utterances)[:-64]:
            self.committed_input_utterances.pop(stale_utterance_id, None)
        self.current_user_turn_received_at = time.time()
        self.committed_user_turns[self.user_turn_id] = {
            "text": transcript,
            "assistant_context": self.turn_assistant_context,
            "received_at": self.current_user_turn_received_at,
        }
        # This is only a race-proofing ledger, not conversational memory.
        # Bound it so a long-running resident process cannot grow forever.
        for stale_turn_id in sorted(self.committed_user_turns)[:-64]:
            self.committed_user_turns.pop(stale_turn_id, None)
        self.turn_tool_results.clear()
        self.awaiting_input_transcript = False
        if self.transcript_timeout_task is not None:
            self.transcript_timeout_task.cancel()
            self.transcript_timeout_task = None
        self.input_transcription_failed = False
        self.speech_turn_assistant_context = ""
        self.memory_store.append_conversation("user", transcript, self.runtime_session_id)
        print(f"[用户] {transcript}", flush=True)
        self.logger.write("input_transcript", text=transcript, turn_id=self.user_turn_id)

        resume_decision: bool | None = None
        if self.task_coordinator.state == "awaiting_resume" and self.task_coordinator.suspended is not None:
            resume_decision = self._classify_resume_reply(
                transcript,
                self.task_coordinator.suspended.task_name,
            )
        self.turn_recovery_plan = (
            {"internal_resume_decision": resume_decision}
            if resume_decision is not None
            else (
                self.skill_bridge.recover_explicit_plan(transcript)
                if self.skill_bridge is not None
                and hasattr(self.skill_bridge, "recover_explicit_plan")
                else None
            )
        )
        if (
            self.turn_recovery_plan is None
            and self.skill_bridge is not None
            and hasattr(self.skill_bridge, "recover_contextual_plan")
        ):
            self.turn_recovery_plan = self.skill_bridge.recover_contextual_plan(
                transcript,
                self.turn_assistant_context,
            )
        if self.turn_recovery_plan is None and not self.turn_had_function_call:
            self._flush_quarantined_model_audio()
            for assistant_text in self.quarantined_output_transcripts:
                self._commit_assistant_transcript(assistant_text)
            self.quarantined_output_transcripts.clear()
            self.model_audio_quarantine = False
        else:
            self._discard_quarantined_model_output(
                "function_call" if self.turn_had_function_call else "explicit_action_requires_tool"
            )

        ready_deferred = [
            item
            for item in self.deferred_function_calls
            if int(item.get("utterance_id") or 0) == committed_utterance_id
        ]
        stale_deferred = [
            item
            for item in self.deferred_function_calls
            if int(item.get("utterance_id") or 0) not in {0, committed_utterance_id}
            and int(item.get("utterance_id") or 0) < committed_utterance_id
        ]
        self.deferred_function_calls = [
            item
            for item in self.deferred_function_calls
            if item not in ready_deferred and item not in stale_deferred
        ]
        for stale in stale_deferred:
            await self._send_discarded_function_output(
                stale,
                reason="stale_audio_utterance",
            )
        if stale_deferred:
            self.logger.write(
                "stale_deferred_calls_discarded",
                utterance_id=committed_utterance_id,
                count=len(stale_deferred),
                reason="later_transcript_committed",
            )
        if resume_decision is not None:
            for deferred in ready_deferred:
                await self._send_discarded_function_output(
                    deferred,
                    reason="task_resume_decision_handled_locally",
                )
            self.logger.write(
                "task_resume_decision",
                accepted=resume_decision,
                transcript=transcript,
                discarded_model_calls=len(ready_deferred),
            )
            await self._apply_resume_decision(resume_decision)
            return []
        results: list[dict[str, Any]] = []
        for deferred in ready_deferred:
            value = dict(deferred.get("value") or {})
            deferred_turn_id = int(deferred.get("turn_id", self.user_turn_id))
            committed = self.committed_user_turns.get(deferred_turn_id, {})
            deferred_user_text = str(committed.get("text", transcript))
            deferred_assistant_context = str(
                committed.get(
                    "assistant_context",
                    deferred.get("assistant_context") or self.turn_assistant_context,
                )
            )
            deferred_received_at = float(
                committed.get(
                    "received_at",
                    deferred.get("received_at") or self.current_user_turn_received_at,
                )
            )
            if schedule_deferred:
                self.schedule_function_call(
                    value,
                    turn_id=deferred_turn_id,
                    utterance_id=committed_utterance_id,
                    user_text=deferred_user_text,
                    assistant_context=deferred_assistant_context,
                    received_at=deferred_received_at,
                )
            else:
                results.append(
                    await self.handle_function_call(
                        value,
                        turn_id=deferred_turn_id,
                        user_text=deferred_user_text,
                        assistant_context=deferred_assistant_context,
                        received_at=deferred_received_at,
                    )
                )
        if ready_deferred:
            self.logger.write(
                "deferred_function_calls_released",
                turn_id=self.user_turn_id,
                count=len(ready_deferred),
                transcript=transcript,
            )
            # Usually response.done follows the transcript.  If it arrived
            # first, explicitly continue the tool-result response here.
            if not self.state.response_active:
                await self.create_tool_followup_if_needed()
        return results

    async def handle_function_call(
        self,
        value: dict[str, Any],
        *,
        turn_id: int | None = None,
        user_text: str | None = None,
        assistant_context: str | None = None,
        received_at: float | None = None,
    ) -> dict[str, Any]:
        synthetic_local = bool(value.get("_synthetic_local"))
        call_turn_id = int(self.user_turn_id if turn_id is None else turn_id)
        call_user_text = self.last_user_text if user_text is None else str(user_text)
        call_assistant_context = (
            self.turn_assistant_context if assistant_context is None else str(assistant_context)
        )
        call_received_at = float(
            self.current_user_turn_received_at if received_at is None else received_at
        )
        call_id = str(value.get("call_id") or "")
        name = str(value.get("name") or "")
        raw_arguments = str(value.get("arguments") or "{}")
        signature = ""
        scoped_signature = ""
        duplicate = False
        try:
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("arguments_not_object")
        except Exception as exc:
            result = {
                "ok": False,
                "error": f"invalid_function_arguments:{type(exc).__name__}",
                "spoken_summary": "本地功能参数不正确，没有执行。",
            }
        else:
            if not call_id:
                result = {
                    "ok": False,
                    "error": "missing_call_id",
                    "spoken_summary": "本地功能调用缺少编号，没有执行。",
                }
            elif (
                scoped_signature := (
                    f"{call_turn_id}:"
                    f"{(signature := self.function_call_signature(name, arguments))}"
                )
            ) in self.turn_tool_results:
                previous = self.turn_tool_results[scoped_signature]
                duplicate = True
                result = {
                    "ok": bool(previous.get("ok")),
                    "validation_ok": bool(previous.get("validation_ok")),
                    "executed": False,
                    "device_state_changed": False,
                    "mode": "deduplicated",
                    "deduplicated": True,
                    "skill": name,
                    "prior_result": previous,
                    "spoken_summary": previous.get("spoken_summary")
                    or "本轮相同操作已经处理过，请直接依据上一次结果回复。",
                }
                self.pending_tool_followup_prompts.append(
                    "本轮完全相同的工具调用已经处理过。严禁再次调用该工具；"
                    "请直接依据 function_call_output 中的 prior_result 回复用户。"
                )
            elif self.memory_store.is_tool(name):
                result = await asyncio.to_thread(self.memory_store.invoke, name, arguments)
            elif self.skill_bridge is None:
                result = {
                    "ok": False,
                    "error": f"local_skills_disabled:{name}",
                    "spoken_summary": "本地功能目前没有启用。",
                }
            else:
                if hasattr(self.skill_bridge, "current_turn_id"):
                    self.skill_bridge.current_turn_id = str(call_turn_id)
                scenario_catalog = getattr(self.skill_bridge, "scenario_catalog", None)
                matched_procedure = (
                    scenario_catalog.match(call_user_text)
                    if scenario_catalog is not None
                    else None
                )
                if self.task_coordinator.state == "idle" and call_id not in self.resume_call_ids:
                    snapshot = self._snapshot_from_call(
                        {
                            "name": name,
                            "arguments": arguments,
                        },
                        started_at=time.monotonic(),
                    )
                    if snapshot is not None:
                        self.task_coordinator.start(snapshot)
                        self.coordinator_owner_call_id = call_id
                self.task_state = {
                    "active": True,
                    "planning": False,
                    "queued": self.external_audio_queue.qsize(),
                    "active_skills": [name],
                    "active_procedures": [matched_procedure] if matched_procedure else [],
                }
                try:
                    dispatch_started = time.monotonic()
                    self.logger.write(
                        "skill_dispatch_started",
                        skill=name,
                        call_id=call_id,
                        turn_id=call_turn_id,
                    )
                    async with self.skill_dispatch_lock:
                        result = await asyncio.to_thread(
                            self.skill_bridge.invoke,
                            name,
                            arguments,
                            call_user_text,
                            str(call_turn_id),
                            call_assistant_context,
                            bool(value.get("_coordinator_resume")),
                        )
                    self.logger.write(
                        "skill_dispatch_completed",
                        skill=name,
                        call_id=call_id,
                        turn_id=call_turn_id,
                        dispatch_elapsed_ms=round((time.monotonic() - dispatch_started) * 1000.0, 1),
                    )
                    style_options = ("温暖直接", "简洁轻快", "自然关切", "从容清楚")
                    style_key = sum(ord(char) for char in str(call_turn_id or name))
                    result.setdefault("speech_style", style_options[style_key % len(style_options)])
                finally:
                    self.task_state = {
                        "active": False,
                        "planning": False,
                        "queued": self.external_audio_queue.qsize(),
                        "active_skills": [],
                        "active_procedures": [],
                    }
        was_preempted = bool(call_id and call_id in self.preempted_call_ids)
        if was_preempted:
            self.preempted_call_ids.discard(call_id)
            final_count, final_elapsed = self._extract_progress_from_result(result)
            self.task_coordinator.checkpoint_suspended(
                count=final_count,
                elapsed_seconds=final_elapsed,
            )
            result = {
                **dict(result),
                "ok": False,
                "validation_ok": True,
                "executed": bool(result.get("executed", True)),
                "interrupted": True,
                "mode": "interrupted",
                "error": "interrupted_by_new_user_task",
                "spoken_summary": "刚才的任务已经暂停。",
            }
        elif (
            call_id
            and call_id == self.coordinator_owner_call_id
            and call_id not in self.resume_call_ids
            and self.task_coordinator.state == "running"
        ):
            self.task_coordinator.complete_active()
            self.coordinator_owner_call_id = ""
        if call_id and signature and not duplicate:
            self.turn_tool_results[scoped_signature or f"{call_turn_id}:{signature}"] = dict(result)
        if call_id and not duplicate and not self.memory_store.is_tool(name):
            await asyncio.to_thread(
                self.memory_store.record_command,
                user_text=call_user_text,
                session_id=self.runtime_session_id,
                turn_id=call_turn_id,
                skill=name,
                arguments=arguments if "arguments" in locals() and isinstance(arguments, dict) else {},
                result=result,
                received_at=call_received_at or None,
            )
        self.logger.write(
            "local_skill_result",
            skill=name,
            call_id=call_id,
            ok=bool(result.get("ok")),
            validation_ok=bool(result.get("validation_ok")),
            executed=bool(result.get("executed")),
            mode=result.get("mode"),
            elapsed_ms=result.get("elapsed_ms"),
            error=result.get("error"),
        )
        print(
            f"[本地Skill] {name} -> "
            f"{'已执行' if result.get('executed') else ('校验通过' if result.get('validation_ok') else '失败')}"
            f" ({result.get('mode') or 'blocked'})",
            flush=True,
        )
        if call_id:
            if was_preempted:
                model_output = {
                    **dict(result),
                    "status": "INTERRUPTED",
                    "success": False,
                    "speech_already_delivered": True,
                    "speech_owner": "task_coordinator",
                    "next_turn_rule_zh": "旧任务已被用户的新任务打断，不得再播报旧任务的完成或失败总结。",
                }
                self.pending_tool_followup_prompts.clear()
                self.pending_tool_followup = False
            elif (
                not result.get("ok")
                and result.get("validation_ok")
                and not result.get("executed")
            ):
                model_output: Any = {
                    "status": "NOT_EXECUTED",
                    "success": False,
                    "message_to_user": result.get("spoken_summary"),
                    "mandatory_rule_zh": (
                        "本次操作没有实际执行。回复必须明确包含“没有实际执行”或“未实际执行”，"
                        "禁止使用“已设置、已开启、已完成、成功执行”等完成态表述。"
                    ),
                }
                self.pending_tool_followup_prompts.append(
                    "下一条回复必须逐字回复这一句，不得增加、删除或改写任何内容："
                    "安全模拟校验通过，但本次操作没有实际执行。"
                )
            else:
                model_output = result
                self.pending_tool_followup_prompts.append(
                    build_tool_reply_instruction(name, result)
                )
            if self.skill_event_speaker is not None and not was_preempted:
                model_output = {
                    **dict(model_output),
                    "speech_already_delivered": True,
                    "speech_owner": "authoritative_local_result_speaker",
                    "next_turn_rule_zh": "结果已经向用户播报；除非用户追问，否则下一轮不要重复这项结果。",
                }
            if not synthetic_local:
                await self.send(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(model_output, ensure_ascii=False),
                        },
                    }
                )
            if self.skill_event_speaker is not None and not was_preempted:
                summary = str(result.get("spoken_summary") or "").strip()
                if summary:
                    self.skill_event_speaker.submit_from_thread(
                        {
                            "event_id": call_id,
                            "turn_id": call_turn_id,
                            "skill_name": name,
                            "kind": "result",
                            "text": summary,
                        }
                    )
                    self.logger.write(
                        "authoritative_result_speech_queued",
                        skill=name,
                        call_id=call_id,
                        turn_id=call_turn_id,
                        text=summary,
                    )
                    self._remember_local_assistant_speech(summary)
                # The result is still written to Qwen's conversation so later
                # turns can reason over it, but the dedicated event speaker is
                # the sole owner of this turn's final status.  This prevents a
                # second model response from guessing or restating the result.
                self.pending_tool_followup_prompts.clear()
                self.pending_tool_followup = False
            elif not was_preempted:
                self.pending_tool_followup = not synthetic_local
                if self.pending_tool_followup and not self.state.response_active:
                    await self.create_tool_followup_if_needed()
        return result

    def schedule_recovered_plan(self, plan: dict[str, Any]) -> asyncio.Task[Any]:
        value = {
            "call_id": f"local_recovery_{self.user_turn_id}_{uuid.uuid4().hex[:10]}",
            "name": str(plan.get("name") or ""),
            "arguments": json.dumps(dict(plan.get("arguments") or {}), ensure_ascii=False),
            "_synthetic_local": True,
        }
        self.logger.write(
            "missing_tool_call_recovered",
            turn_id=self.user_turn_id,
            skill=value["name"],
            arguments=dict(plan.get("arguments") or {}),
        )
        return self.schedule_function_call(value)

    async def create_tool_followup_if_needed(self) -> bool:
        if not self.pending_tool_followup:
            return False
        self.pending_tool_followup = False
        prompts = list(self.pending_tool_followup_prompts)
        self.pending_tool_followup_prompts.clear()
        if self.skill_event_speaker is not None:
            await self.skill_event_speaker.wait_idle(timeout=6.0)
        if prompts:
            await self.send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "system",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "\n".join(prompts),
                            }
                        ],
                    },
                }
            )
        await self.send(
            {
                "type": "response.create",
                "response": {"modalities": ["audio", "text"]},
            }
        )
        return True

    async def inject_next_turn_style_hint(self) -> bool:
        """Consume the legacy flag without creating a new Realtime turn."""
        if not self.style_hint_pending:
            return False
        self.style_hint_pending = False
        # Do not append a standalone system item here.  Qwen Realtime treats
        # such an item as a new conversational turn and may immediately speak
        # an unsolicited answer.  The permanent session prompt already owns
        # wording diversity, so a per-turn mutation is unnecessary and unsafe.
        self.logger.write("style_hint_skipped", reason="realtime_unsolicited_response_guard")
        return False

    async def finish_response_turn(self) -> None:
        """Close one primary-model response without leaking speculative speech."""

        await self.create_tool_followup_if_needed()
        await self.inject_next_turn_style_hint()
        if self.awaiting_input_transcript:
            return
        if self.turn_recovery_plan is not None and not self.turn_had_function_call:
            plan = self.turn_recovery_plan
            self.turn_recovery_plan = None
            self._discard_quarantined_model_output("missing_tool_call_recovered")
            self.schedule_recovered_plan(plan)
        elif not self.turn_had_function_call:
            self._flush_quarantined_model_audio()
            for assistant_text in self.quarantined_output_transcripts:
                self._commit_assistant_transcript(assistant_text)
            self.quarantined_output_transcripts.clear()
        self.model_audio_quarantine = False

    async def send_microphone(self) -> None:
        assert self.audio is not None
        while not self.stop_event.is_set():
            pcm = await asyncio.to_thread(self.audio.read_microphone)
            if not self.local_microphone_enabled:
                continue
            health = self.microphone_health_payload()
            if health["digital_silence"]:
                raise ServiceError(
                    "microphone_digital_silence",
                    "麦克风输入连续三秒只有数字零，重新建立音频会话",
                )
            if self.external_audio_active:
                continue
            if not self.audio.microphone_allowed(
                self.args.echo_mode,
                self.args.echo_tail_seconds,
                pcm,
                self.args.noise_gate,
            ):
                continue
            await self.send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode("ascii"),
                }
            )

    async def receive_events(self) -> None:
        assert self.audio is not None
        async for raw in self.websocket:
            event = json.loads(raw)
            event_type = str(event.get("type") or "unknown")
            if event_type == "input_audio_buffer.speech_started":
                self.mark_input_speech_started()
            elif event_type == "input_audio_buffer.speech_stopped":
                self.logger.write(
                    "input_speech_stopped",
                    utterance_id=self.active_input_utterance_id,
                )
                self.schedule_transcript_timeout()
            if event_type != "response.audio.delta":
                self.logger.write("service_event", type=event_type)
            for action in self.state.process(event):
                if action.kind == "play_audio":
                    if self.model_audio_quarantine:
                        self.quarantined_model_audio.append(action.value)
                    else:
                        self.audio.enqueue(action.value)
                elif action.kind == "interrupt_playback":
                    self.audio.interrupt()
                    print("[系统] 检测到用户说话，停止当前播放", flush=True)
                elif action.kind == "cancel_response":
                    await self.send({"type": "response.cancel"})
                elif action.kind == "input_transcript":
                    await self.accept_input_transcript(str(action.value), schedule_deferred=True)
                elif action.kind == "input_transcription_failed":
                    self.input_transcription_failed = True
                    self.logger.write(
                        "input_transcription_failed",
                        utterance_id=self.active_input_utterance_id,
                        **dict(action.value or {}),
                    )
                    # speech_stopped normally follows. If the service reports
                    # failure afterwards, this reschedules with the shorter
                    # failure grace period.
                    self.schedule_transcript_timeout()
                elif action.kind == "output_transcript":
                    if self.model_audio_quarantine:
                        self.quarantined_output_transcripts.append(str(action.value))
                        if not self.awaiting_input_transcript and (
                            self.turn_had_function_call or self.turn_recovery_plan is not None
                        ):
                            self._discard_quarantined_model_output(
                                "function_call"
                                if self.turn_had_function_call
                                else "explicit_action_requires_tool"
                            )
                    else:
                        self._commit_assistant_transcript(str(action.value))
                elif action.kind == "function_call":
                    self.turn_had_function_call = True
                    self.model_audio_quarantine = True
                    self._discard_quarantined_model_output("function_call")
                    self.schedule_function_call(action.value)
                elif action.kind == "error":
                    code = str(action.value["code"])
                    message = str(action.value["message"])
                    if is_benign_service_error(code, message):
                        self.logger.write("benign_cancel_race_ignored", code=code, message=message)
                        continue
                    raise ServiceError(code, message)
            if event_type == "response.done":
                await self.finish_response_turn()

    async def run_tool_test(self, text: str, output_path: Path, timeout: float) -> None:
        if self.skill_bridge is None:
            raise ValueError("tool_test_requires_local_skills")
        self.last_user_text = text
        self.turn_assistant_context = self.last_assistant_text
        self.user_turn_id += 1
        # Text injection has no VAD/transcript event, so mirror the real audio
        # turn boundary and reset per-turn duplicate protection here as well.
        self.turn_tool_results.clear()
        self.logger.write("tool_test_input_sent", text=text, turn_id=self.user_turn_id)
        await self.send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self.send(
            {
                "type": "response.create",
                "response": {"modalities": ["audio", "text"]},
            }
        )
        deadline = time.monotonic() + max(5.0, timeout)
        response_count = 0
        response_done_count = 0
        tool_results: list[dict[str, Any]] = []
        audio_by_response: dict[int, bytearray] = {}
        transcripts: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError("tool_test_timeout")
            raw = await asyncio.wait_for(self.websocket.recv(), timeout=remaining)
            event = json.loads(raw)
            event_type = str(event.get("type") or "unknown")
            if event_type == "response.created":
                response_count += 1
                audio_by_response.setdefault(response_count, bytearray())
            if event_type != "response.audio.delta":
                self.logger.write("tool_test_service_event", type=event_type)
            for action in self.state.process(event):
                if action.kind == "play_audio":
                    audio_by_response.setdefault(max(1, response_count), bytearray()).extend(action.value)
                elif action.kind == "function_call":
                    tool_results.append(await self.handle_function_call(action.value))
                elif action.kind == "output_transcript":
                    self.last_assistant_text = str(action.value)
                    transcripts.append(str(action.value))
                    self.style_hint_pending = True
                elif action.kind == "error":
                    code = str(action.value["code"])
                    message = str(action.value["message"])
                    if is_benign_service_error(code, message):
                        self.logger.write("benign_cancel_race_ignored", code=code, message=message)
                        continue
                    raise ServiceError(code, message)
            if event_type == "response.done":
                response_done_count += 1
                followup_created = await self.create_tool_followup_if_needed()
                await self.inject_next_turn_style_hint()
                if not tool_results:
                    break
                if response_done_count >= 2 and not followup_created:
                    break
        all_audio = b"".join(bytes(audio_by_response[key]) for key in sorted(audio_by_response))
        followup_audio = bytes(audio_by_response.get(2, b""))
        transcript_text = " ".join(transcripts)
        dry_run_result = bool(
            tool_results
            and tool_results[-1].get("validation_ok")
            and not tool_results[-1].get("executed")
        )
        contains_non_execution = any(
            marker in transcript_text
            for marker in ("没有实际", "未实际", "未执行", "无法", "不能", "安全模拟")
        )
        contains_false_completion = any(
            marker in transcript_text
            for marker in ("已设置", "已开启", "已打开", "已完成", "已经设置", "成功执行", "已经执行")
        )
        truthful_transcript = not dry_run_result or (
            contains_non_execution and not contains_false_completion
        )
        if all_audio:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(output_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(SAMPLE_WIDTH)
                wav.setframerate(OUTPUT_RATE)
                wav.writeframes(all_audio)
        expect_tool = bool(getattr(self.args, "tool_test_expect_tool", True))
        result = {
            "ok": bool(
                (
                    tool_results
                    and (tool_results[-1].get("ok") or tool_results[-1].get("validation_ok"))
                    and followup_audio
                    and truthful_transcript
                )
                if expect_tool
                else (not tool_results and all_audio and transcript_text.strip())
            ),
            "input_text": text,
            "expect_tool": expect_tool,
            "response_count": response_count,
            "response_done_count": response_done_count,
            "tool_results": tool_results,
            "transcripts": transcripts,
            "audio_bytes": len(all_audio),
            "followup_audio_bytes": len(followup_audio),
            "audio_seconds": round(len(all_audio) / (OUTPUT_RATE * SAMPLE_WIDTH), 3),
            "truthful_transcript": truthful_transcript,
            "output_wav": str(output_path) if all_audio else None,
            "speaker_opened": False,
            "microphone_opened": False,
        }
        self.last_tool_test_result = result
        self.logger.write("tool_test_result", **result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if not result["ok"]:
            raise RuntimeError("tool_call_or_followup_audio_test_failed")

    async def run_connected(self, *, preflight: bool) -> None:
        await self.connect()
        print(f"已连接 {MODEL}。", flush=True)
        if self.skill_bridge is not None:
            summary = self.skill_bridge.catalog_summary()
            print(
                f"已注册 {summary['enabled_count']} 个机器人 Skill；"
                f"{summary['unavailable_count']} 个按现有配置禁用。",
                flush=True,
            )
        if self.memory_store.enabled:
            print(
                f"已启用 3 个本地记忆工具；启动时恢复最近 {self.args.memory_history_turns} 轮对话。",
                flush=True,
            )
        if preflight:
            print("预检成功：未打开麦克风或扬声器。", flush=True)
            return
        if self.args.tool_test_text:
            if self.args.execute_skills:
                print(
                    "开始本地 Skill 云端真实执行测试；不会打开麦克风或扬声器，但工具可能操作真实硬件。",
                    flush=True,
                )
            else:
                print("开始本地 Skill 云端安全模拟测试；不会打开麦克风、扬声器或真实硬件。", flush=True)
            await self.run_tool_test(
                self.args.tool_test_text,
                self.args.tool_test_output,
                self.args.tool_test_timeout,
            )
            return
        conflicts = running_controller_conflicts()
        if conflicts:
            pids = ",".join(str(item.get("pid")) for item in conflicts)
            raise ServiceError(
                "existing_robot_controller_running",
                f"检测到其他机器人控制程序（PID {pids}），为避免冲突未打开音频设备",
            )
        # Keep one PortAudio/PulseAudio engine for the entire resident process.
        # Only the Qwen websocket session is renewed on an idle timeout.  Closing
        # and reopening the hardware streams on every cloud reconnect caused a
        # PortAudio/PulseAudio teardown race and brought down the full project.
        if self.audio_resource_guard is None:
            self.audio_resource_guard = RuntimeResourceGuard(self.args.resource_lock_dir)
            self.audio_resource_guard.acquire()
        if self.audio is None:
            self.audio = AudioEngine(
                input_device_index=self.args.input_device_index,
                output_device_index=self.args.output_device_index,
                chunk_ms=self.args.chunk_ms,
            )
        self.skill_event_speaker = QwenSkillEventSpeaker(
            api_key=self.api_key,
            voice=self.args.voice,
            workspace=self.args.workspace,
            region=self.args.region,
            endpoint=self.args.endpoint,
            connect_timeout=self.args.connect_timeout,
            enqueue_pcm=self.audio.enqueue,
            log=self.logger.write,
            cache_dir=Path(__file__).with_name("runtime") / "skill_speech_cache",
        )
        await self.skill_event_speaker.start()
        print("持续对话已开始，直接说话即可；按 Ctrl+C 结束。", flush=True)
        if self.args.echo_mode == "speaker-safe":
            print("当前为扬声器安全模式：机器人说话时暂停上行，播完后自动恢复监听。", flush=True)
        else:
            print("当前为全双工模式：支持插话，请确保系统具备回声消除或使用耳机。", flush=True)
        tasks = {
            asyncio.create_task(self.send_microphone(), name="microphone"),
            asyncio.create_task(self.send_external_audio(), name="app-audio"),
            asyncio.create_task(self.receive_events(), name="receiver"),
            asyncio.create_task(self.stop_event.wait(), name="stop"),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.get_name() != "stop":
                task.result()

    async def run(self, *, preflight: bool) -> None:
        backoff = 1.0
        await self.start_control_server()
        try:
            while not self.stop_event.is_set():
                try:
                    await self.run_connected(preflight=preflight)
                    if preflight or self.args.tool_test_text or self.stop_event.is_set():
                        break
                    raise ConnectionError("realtime_session_ended")
                except ServiceError as exc:
                    self.logger.write("service_error", code=exc.code, message=exc.message)
                    if (
                        preflight
                        or self.args.tool_test_text
                        or not self.args.reconnect
                        or not is_reconnectable_service_error(exc.code)
                    ):
                        raise
                    # Qwen closes an otherwise healthy idle session after 180
                    # seconds. Treat this as routine session rotation, not a
                    # worsening network failure: reconnect almost immediately
                    # and reset exponential backoff so users do not face a
                    # growing 1/2/4/8/15-second deaf window every three minutes.
                    reconnect_delay = 0.2 if exc.code == "response_idle_timeout" else backoff
                    if exc.code == "response_idle_timeout":
                        backoff = 1.0
                    print(
                        f"千问会话暂时中断（{exc.code}），{reconnect_delay:.1f} 秒后恢复持续监听……",
                        flush=True,
                    )
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=reconnect_delay)
                    except asyncio.TimeoutError:
                        pass
                    if exc.code != "response_idle_timeout":
                        backoff = min(15.0, backoff * 2.0)
                except Exception as exc:
                    status = getattr(exc, "status_code", None) or getattr(
                        getattr(exc, "response", None), "status_code", None
                    )
                    body = bytes(getattr(getattr(exc, "response", None), "body", b"") or b"").decode("utf-8", "replace")[:1000]
                    self.logger.write("connection_error", error=f"{type(exc).__name__}: {exc}", http_status=status, body=body)
                    if preflight or self.args.tool_test_text or status in {400, 401, 403, 404} or not self.args.reconnect:
                        raise
                    print(f"连接中断，{backoff:.0f} 秒后自动重连……", flush=True)
                    try:
                        await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(15.0, backoff * 2.0)
                finally:
                    self.connected = False
                    if self.transcript_timeout_task is not None:
                        self.transcript_timeout_task.cancel()
                        await asyncio.gather(
                            self.transcript_timeout_task,
                            return_exceptions=True,
                        )
                        self.transcript_timeout_task = None
                    # A tool call belongs to one realtime connection and one
                    # captured audio utterance.  Carrying either across a
                    # reconnect can execute an obsolete command after the user
                    # says something unrelated.
                    self.awaiting_input_transcript = False
                    self.input_transcription_failed = False
                    self.deferred_function_calls.clear()
                    self.turn_recovery_plan = None
                    self.turn_had_function_call = False
                    self.model_audio_quarantine = False
                    self.quarantined_model_audio.clear()
                    self.quarantined_output_transcripts.clear()
                    if self.websocket is not None:
                        with contextlib.suppress(Exception):
                            await self.websocket.close()
                        self.websocket = None
                    if self.skill_event_speaker is not None:
                        await self.skill_event_speaker.close()
                        self.skill_event_speaker = None
        finally:
            await self.stop_function_calls()
            if self.skill_bridge is not None:
                self.skill_bridge.cancel_all()
            if self.skill_event_speaker is not None:
                await self.skill_event_speaker.close()
                self.skill_event_speaker = None
            if self.audio is not None:
                self.audio.close()
                self.audio = None
            if self.audio_resource_guard is not None:
                self.audio_resource_guard.close()
                self.audio_resource_guard = None
            await self.stop_control_server()
            self.logger.write("program_stopped")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Qwen Audio 3.0 Realtime Flash 独立持续语音对话")
    value.add_argument("--workspace", default=os.environ.get("DASHSCOPE_WORKSPACE_ID", ""))
    value.add_argument("--endpoint", default=os.environ.get("DASHSCOPE_REALTIME_URL", ""))
    value.add_argument("--api-key-file", type=Path)
    value.add_argument("--region", default="cn-beijing", choices=("cn-beijing", "ap-southeast-1"))
    value.add_argument("--voice", default="longanqian")
    value.add_argument("--instructions", default=DEFAULT_ASSISTANT_INSTRUCTIONS)
    value.add_argument("--turn-detection", default="smart_turn", choices=("smart_turn", "server_vad"))
    value.add_argument("--silence-duration-ms", type=int, default=800)
    value.add_argument("--vad-threshold", type=float, default=0.5)
    value.add_argument("--max-history-turns", type=int, default=50)
    value.add_argument("--persistent-memory", action=argparse.BooleanOptionalAction, default=True)
    value.add_argument("--memory-dir", type=Path, default=Path("runtime/memory"))
    value.add_argument(
        "--memory-history-turns",
        type=int,
        default=0,
        help="启动或重连时注入的历史对话轮数；默认 0，避免把过去设备结果当作当前状态",
    )
    value.add_argument("--memory-history-chars", type=int, default=6000)
    value.add_argument("--memory-max-history-items", type=int, default=1000)
    value.add_argument("--memory-max-facts", type=int, default=200)
    value.add_argument("--chunk-ms", type=int, default=100, choices=(20, 40, 50, 100, 200))
    value.add_argument("--echo-mode", default="speaker-safe", choices=("speaker-safe", "full-duplex"))
    value.add_argument("--echo-tail-seconds", type=float, default=0.5)
    value.add_argument("--noise-gate", type=float, default=500.0)
    value.add_argument(
        "--transcript-grace-seconds",
        type=float,
        default=float(os.environ.get("QWEN_TRANSCRIPT_GRACE_SECONDS", "1.5")),
        help="speech_stopped 后等待最终转写的宽限；超时后丢弃未确认动作并请用户重说",
    )
    value.add_argument("--input-device-index", type=int)
    value.add_argument("--output-device-index", type=int)
    value.add_argument(
        "--resource-lock-dir",
        type=Path,
        default=Path("/home/test/qwen_robot_project/runtime/locks"),
    )
    value.add_argument("--connect-timeout", type=float, default=15.0)
    value.add_argument("--reconnect", action=argparse.BooleanOptionalAction, default=True)
    value.add_argument("--local-skills", action=argparse.BooleanOptionalAction, default=True)
    value.add_argument("--enable-skill", action="append", default=[], help="只注册指定本地 skill，可重复；默认注册全部可用 skill")
    value.add_argument("--skill-spec-dir", type=Path, default=DEFAULT_SPEC_DIR)
    value.add_argument("--skill-timeout", type=float, default=120.0)
    value.add_argument("--skill-backend", choices=("auto", "host", "subprocess"), default="auto")
    value.add_argument("--skill-host-socket", type=Path, default=Path(__file__).with_name("runtime") / "skill_host.sock")
    value.add_argument("--app-control-socket", type=Path, default=Path(__file__).with_name("runtime") / "app_control.sock")
    value.add_argument("--app-voice-dir", type=Path, default=Path(__file__).with_name("runtime") / "app_voice")
    value.add_argument("--microphone-state-file", type=Path, default=Path(__file__).with_name("runtime") / "microphone_state.json")
    value.add_argument("--scenarios", action=argparse.BooleanOptionalAction, default=True)
    value.add_argument("--scenario-catalog", type=Path, default=Path(__file__).with_name("scenarios") / "procedure_catalog.json")
    value.add_argument("--execute-skills", action="store_true", help="真实执行本地 skill；默认只做 dry-run")
    value.add_argument("--catalog-test", action="store_true", help="逐项 dry-run 全部本地 skill，不联网、不打开硬件")
    value.add_argument("--preflight", action="store_true", help="只检查鉴权和会话配置，不打开音频设备")
    value.add_argument("--tool-test-text", default="", help="用文字测试 Function Calling 和后续语音生成，不打开音频设备")
    value.add_argument(
        "--tool-test-expect-tool",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="文字测试是否必须调用工具；--no-tool-test-expect-tool 用于验证普通或偏离剧本的话不会误动作",
    )
    value.add_argument("--tool-test-output", type=Path, default=Path("runtime/tool_test_response.wav"))
    value.add_argument("--tool-test-timeout", type=float, default=45.0)
    value.add_argument("--list-devices", action="store_true")
    value.add_argument("--log-file", type=Path, default=Path("runtime/realtime_chat.jsonl"))
    return value


async def async_main(args: argparse.Namespace) -> int:
    if args.list_devices:
        return list_audio_devices()
    if args.catalog_test:
        bridge = LocalSkillBridge(
            spec_dir=args.skill_spec_dir,
            enabled_skills=args.enable_skill or list(DEFAULT_ENABLED_SKILLS),
            execute=False,
            timeout=args.skill_timeout,
            backend=args.skill_backend,
            host_socket=args.skill_host_socket,
            scenario_catalog_path=args.scenario_catalog if args.scenarios else None,
        )
        results = []
        catalog_test_utterances = {
            "back_camera_capture": "请用后摄像头拍张照片",
            "back_camera_record": "请用后摄像头录一段视频",
            "camera_capture": "请用前摄像头拍张照片",
            "camera_record": "请用前摄像头录一段视频",
            "environment_perception": "请看看你面前有什么",
            "face_recognition": "请看看面前的人是谁",
            "face_registration": "请把我的人脸注册下来",
            "feeder_control": "请启动投食器喂一次豆豆",
            "front_camera_capture": "请用前摄像头拍张照片",
            "front_camera_record": "请用前摄像头录一段视频",
            "head_control": "请抬头",
            "light_control": "请打开落地灯",
            "media_player": "请查询播放器状态",
            "move_backward": "请往后移动一秒",
            "move_forward": "请往前移动一秒",
            "move_left": "请往左移动一秒",
            "move_right": "请往右移动一秒",
            "navigation_goto": "请导航到书房",
            "navigation_list": "请告诉我现在有哪些导航点",
            "person_tracking": "请开始跟踪面前的人",
            "pet_tracking": "请寻找豆豆",
            "projector_control": "请打开投影",
            "pull_up": "请帮我数引体向上",
            "push_up": "请帮我数俯卧撑",
            "realtime_information": "请查询今天的天气",
            "reminder_cancel": "请删除下午三点的提醒",
            "reminder_query": "请查询我的提醒",
            "reminder_schedule": "请提醒我十分钟后开会",
            "squat": "请帮我数深蹲",
            "welcome_projection": "请播放欢迎回家投影",
        }
        for name, spec in sorted(bridge.specs.items()):
            example = spec.get("example_function_call")
            arguments = example.get("arguments") if isinstance(example, dict) else {}
            arguments = arguments if isinstance(arguments, dict) else {}
            user_text = catalog_test_utterances.get(name, f"请执行{name}")
            result = await asyncio.to_thread(bridge.invoke, name, arguments, user_text)
            results.append(result)
        memory_store = MemoryStore(
            args.memory_dir,
            enabled=args.persistent_memory,
            max_history_items=args.memory_max_history_items,
            max_facts=args.memory_max_facts,
        )
        memory_tools = [item["function"]["name"] for item in memory_store.tool_schemas]
        report = {
            **bridge.catalog_summary(),
            "memory_tool_count": len(memory_tools),
            "memory_tools": memory_tools,
            "total_registered_tool_count": len(bridge.specs) + len(memory_tools),
            "tested_count": len(results),
            "passed_count": sum(1 for item in results if item.get("validation_ok") or item.get("ok")),
            "failed": [item for item in results if not (item.get("validation_ok") or item.get("ok"))],
            "hardware_opened": False,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return 0 if report["tested_count"] == report["passed_count"] else 1
    api_key = load_api_key(args.api_key_file)
    logger = JsonLogger(args.log_file)
    conversation = RealtimeConversation(args, api_key, logger)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, conversation.stop_event.set)
    await conversation.run(preflight=args.preflight)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        response = getattr(exc, "response", None)
        body = bytes(getattr(response, "body", b"") or b"").decode("utf-8", "replace")
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "http_status": getattr(exc, "status_code", None)
            or getattr(response, "status_code", None),
            "service_body": body[:1000] or None,
        }
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
