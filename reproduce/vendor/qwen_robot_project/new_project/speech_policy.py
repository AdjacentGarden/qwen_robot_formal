from __future__ import annotations

import re
from typing import Any


class SpeechPolicy:
    """Render user-facing speech from semantic state, without extra model calls."""

    _INTERNAL_TERMS = {
        "TaskGroup": "任务",
        "task group": "任务",
        "slot": "信息",
        "硬件清理": "设备恢复",
        "语音决策": "语音理解",
        "待执行": "还没开始",
    }

    _EXERCISE_NAMES = {
        "squat": "深蹲",
        "push_up": "俯卧撑",
        "pull_up": "引体向上",
    }

    def __init__(self, config: dict[str, Any] | None = None):
        speech = (config or {}).get("speech", {})
        self.face_confident_score_threshold = float(speech.get("face_confident_score_threshold", 0.70))

    def task_name(self, task_group: Any, default: str = "刚才的事") -> str:
        title = str(getattr(task_group, "title", "") or "").strip()
        if not title:
            return default
        title = re.sub(r"(?:完整)?任务(?:整体)?$", "", title).strip()
        return title or default

    def clean(self, text: str) -> str:
        value = " ".join(str(text or "").split()).strip()
        for source, target in self._INTERNAL_TERMS.items():
            value = value.replace(source, target)
        value = value.replace("你也可以说就在这里继续", "也可以仍然在这里进行")
        value = value.replace("请直接说继续或取消", "告诉我是继续还是先放一放")
        value = value.replace("请直接回答刚才的问题", "再告诉我刚才那个问题的答案")
        return value

    def environment_override_question(self, task_group: Any, blockers: list[str]) -> str:
        blockers = {str(item).lower() for item in blockers}
        fitness_bad = "fitness_not_suitable" in blockers or any(
            marker in item for item in blockers for marker in ("space", "body", "person", "exercise", "fitness")
        )
        projection_bad = "projection_not_suitable" in blockers or any(
            marker in item for item in blockers for marker in ("projection", "wall", "screen", "light")
        )
        if projection_bad and not fitness_bad:
            return "这里的投影条件可能不太理想。要换个地方，还是仍然在这里投影？"
        exercise = self._exercise_name(task_group)
        if fitness_bad and projection_bad:
            return f"这里的活动空间和投影条件都不太理想。要换个地方，还是仍然在这里做{exercise}？"
        if fitness_bad:
            return f"这里的空间可能不太适合做{exercise}。要换个地方，还是仍然在这里做？"
        if projection_bad:
            return f"这里可以继续做{exercise}，但投影效果可能不好。要关闭投影继续，还是换个地方？"
        return f"我对这里是否适合做{exercise}不太确定。要换个地方，还是仍然在这里进行？"

    def followup_retry(self, question: str, *, final: bool = False) -> str:
        question = self.clean(question).strip("。！？? ")
        if final:
            return f"这次还是没听清。我先记着这件事，之后可以接着聊{f'：{question}' if question else '。'}"
        return f"刚才这句话我没听清。{question + '？' if question else '请再说一次。'}"

    def interrupted_command_retry(self, task_group: Any | None) -> str:
        name = self.task_name(task_group)
        return f"新的安排我没听清，{name}已经暂停了。你可以再告诉我接下来想做什么。"

    def resume_question(self, task_group: Any) -> str:
        name = self.task_name(task_group)
        if self._repeat_requires_confirmation(task_group):
            return f"刚才的{name}被打断了，我不能确定设备是否已经执行。还要再执行一次吗？"
        count = self._saved_count(task_group)
        if count is not None:
            return f"刚才的{name}数到{count}个了，还要接着做吗？"
        return f"刚才的{name}还没做完，要接着来吗？"

    def resume_ack(self, task_group: Any, *, restart: bool = False) -> str:
        name = self.task_name(task_group)
        if self._repeat_requires_confirmation(task_group):
            return f"好，我再执行一次{name}。"
        if restart:
            return f"好，{name}从头开始。"
        count = self._saved_count(task_group)
        if count is not None:
            return f"好，{name}从第{count + 1}个接着来。"
        return f"好，我们接着{name}。"

    def cancelled(self, task_group: Any) -> str:
        return f"好，{self.task_name(task_group)}就先不做了。"

    def paused(self, task_group: Any) -> str:
        return f"好，{self.task_name(task_group)}先停在这里。"

    def scene_restore_question(self, task_group: Any) -> str:
        return f"机器人现在的位置和刚才不一样。要先回到原来的位置，再接着{self.task_name(task_group)}吗？"

    def step_summary(self, step: Any, parsed: dict[str, Any]) -> str:
        skill = str(getattr(step, "skill_name", "") or "")
        arguments = dict(getattr(step, "arguments", {}) or {})
        result = parsed.get("result") if isinstance(parsed.get("result"), dict) else {}
        status = str(result.get("status") or parsed.get("status") or "").lower()
        if skill == "realtime_information":
            message = parsed.get("message") or result.get("message")
            return str(message).strip() if message else "实时信息已经查询完成。"
        if skill == "face_recognition":
            name = result.get("name") or parsed.get("name")
            score = result.get("score")
            if status == "matched" and name:
                if isinstance(score, (int, float)) and float(score) < self.face_confident_score_threshold:
                    return f"看起来像{name}，不过我不太确定。"
                return f"看起来是{name}。"
            if status in {"no_face", "not_detected"}:
                return "我没有看清人脸，可以面向摄像头再试一次。"
            if status == "empty_db":
                return "现在还没有录入过人脸。"
            return "我看到了人脸，但没有认出是谁。"
        if skill == "face_registration":
            name = result.get("name") or arguments.get("name")
            if status == "success":
                return f"已经记住{name}了。" if name else "人脸已经录入好了。"
            if status in {"no_face", "not_detected"}:
                return "我没有看清人脸，请面向摄像头再试一次。"
            return "人脸录入没有成功，请稍后再试一次。"
        if skill in {"front_camera_capture", "back_camera_capture", "camera_capture"}:
            camera = str(arguments.get("camera_name") or arguments.get("camera") or "")
            return "后面的照片拍好了。" if camera == "back" or skill.startswith("back_") else "照片拍好了。"
        if skill in {"front_camera_record", "back_camera_record", "camera_record"}:
            return "录像已经保存好了。"
        if skill == "projector_control":
            action = str(arguments.get("action") or parsed.get("action") or "").lower()
            if action in {"off", "close", "disable"}:
                return "投影已经关好了。"
            if action in {"fitness_video_on", "external_video_on"}:
                return "运动视频已经放到墙上了，我们可以开始了。"
            if action in {"meeting_presentation_on", "meeting_on"}:
                return "会议内容已经投影好了。"
            if action in {"on", "internal_on", "external_on", "open", "enable"}:
                return "投影已经打开了。"
            return "投影设置好了。"
        if skill == "light_control":
            action = str(arguments.get("action") or parsed.get("action") or "").lower()
            if action in {"off", "close", "disable"}:
                return "灯关好了。"
            if action in {"status", "query", "check"}:
                power = result.get("power")
                return "灯现在开着。" if power is True else "灯现在关着。" if power is False else "我已经看过了，灯的状态暂时没有读出来。"
            return "灯打开了，屋里亮堂多了。"
        if skill == "fan_control":
            action = str(arguments.get("action") or parsed.get("action") or "").lower()
            return "风扇已经关了。" if action in {"off", "close", "disable"} else "风扇已经打开了。"
        if skill == "feeder_control":
            action = str(arguments.get("action") or parsed.get("action") or "feed").lower()
            if action in {"status", "query", "check"}:
                message = parsed.get("message") or result.get("message")
                return str(message).strip() if isinstance(message, str) and message else "投食机现在可以正常使用。"
            grams = result.get("actual_grams") or arguments.get("grams")
            return f"已经放好{grams}克宠物粮了。" if grams is not None else "宠物粮已经放好了。"
        if skill in self._EXERCISE_NAMES:
            count = result.get("count") if result.get("count") is not None else parsed.get("count")
            name = self._EXERCISE_NAMES[skill]
            return f"这次一共做了{count}个{name}。" if count is not None else f"这次{name}结束了。"
        if skill in {"person_tracking", "pet_tracking"}:
            action = str(arguments.get("action") or parsed.get("mode") or "").strip().lower()
            found = result.get("found") if "found" in result else parsed.get("found")
            if skill == "pet_tracking":
                pet = str(arguments.get("pet") or parsed.get("pet") or "").lower()
                target = "小狗" if pet == "dog" else "小猫" if pet == "cat" else "宠物"
            else:
                target = "人员"
            if status in {"not_found", "no_target", "timeout"} or found is False:
                return f"这次没有找到{target}。"
            if status in {"cancelled", "interrupted"} or action == "stop":
                return f"已经停止跟随{target}。"
            if action == "find":
                return f"已经找到{target}。" if found is True else f"{target}查找已经结束。"
            if skill == "pet_tracking":
                video_path = parsed.get("video_path") or result.get("video_path")
                return f"{target}的跟随已经结束，录像也保存好了。" if video_path else f"{target}的跟随已经结束。"
            return "人员跟随已经结束。"
        return ""

    def _exercise_name(self, task_group: Any) -> str:
        slots = dict(getattr(task_group, "slots", {}) or {})
        value = str(slots.get("exercise_type") or "").lower()
        return self._EXERCISE_NAMES.get(value, "运动")

    @staticmethod
    def _repeat_requires_confirmation(task_group: Any) -> bool:
        context = dict(getattr(task_group, "resume_context", {}) or {})
        recovery = context.get("recovery") if isinstance(context.get("recovery"), dict) else {}
        return bool(recovery.get("repeat_requires_confirmation"))

    @staticmethod
    def _saved_count(task_group: Any) -> int | None:
        context = dict(getattr(task_group, "resume_context", {}) or {})
        progress = context.get("last_progress")
        if isinstance(progress, dict) and isinstance(progress.get("payload"), dict):
            progress = progress["payload"]
        if not isinstance(progress, dict):
            return None
        for key in ("current_count", "count", "initial_count"):
            try:
                if progress.get(key) is not None:
                    return int(progress[key])
            except (TypeError, ValueError):
                continue
        return None
