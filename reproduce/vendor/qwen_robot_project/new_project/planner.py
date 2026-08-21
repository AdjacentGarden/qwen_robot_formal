from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .skill_registry import SkillRegistry


class Planner:
    def __init__(self, config: dict[str, Any], registry: SkillRegistry):
        self.config = config
        self.registry = registry
        self.prompt_path = Path(config["_config_path"]).parent / "planner_prompt.md"

    def plan(self, user_text: str, history: list[dict[str, Any]] | None = None, session_context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not user_text.strip():
            return {
                "decision_type": "noop",
                "reply": "没有听清楚",
                "task_groups": [],
                "ask_user": None,
                "confidence": 0.0,
            }

        # Voice commands are planned by DoubaoRealtimeSession before reaching
        # this class. Manual text and deterministic recovery use the same local
        # safety planner used for text-only and recovery paths. Keeping it
        # explicit avoids a second model backend while preserving the existing
        # deterministic behavior of those paths.
        decision = self._local_fallback_plan(user_text)
        decision["planner_backend"] = "local_safety"
        return self._postprocess_decision(decision, user_text)

    def _normalize_decision(self, decision: dict[str, Any], user_text: str) -> dict[str, Any]:
        normalized = {
            "decision_type": decision.get("decision_type") or "task_plan",
            "interaction_type": decision.get("interaction_type") or "",
            "reply": decision.get("reply") or "",
            "task_groups": decision.get("task_groups") or [],
            "ask_user": decision.get("ask_user"),
            "confidence": float(decision.get("confidence", 0.0) or 0.0),
        }
        has_action = bool(normalized["task_groups"] or normalized["ask_user"])
        if not has_action and normalized["decision_type"] in {"task_plan", "ask_user", "noop"}:
            normalized = self._local_fallback_plan(user_text)
        return self._postprocess_decision(normalized, user_text)

    def _local_fallback_plan(self, user_text: str) -> dict[str, Any]:
        chunks = self._split_multi_command(user_text)
        task_groups = []
        ask_user: dict[str, Any] | None = None
        for chunk in chunks:
            one = self._plan_one_chunk(chunk)
            if one.get("ask_user") and ask_user is None:
                ask_user = one["ask_user"]
            if one.get("task_group"):
                task_groups.append(one["task_group"])

        if ask_user:
            return {
                "decision_type": "ask_user",
                "reply": ask_user["question"],
                "task_groups": task_groups,
                "ask_user": ask_user,
                "confidence": 0.55,
            }
        if task_groups:
            return {
                "decision_type": "task_plan",
                "reply": self._task_plan_start_reply(task_groups),
                "task_groups": task_groups,
                "ask_user": None,
                "confidence": 0.55,
            }
        if self._looks_like_ambiguous_action_request(user_text):
            return {
                "decision_type": "ask_user",
                "interaction_type": "command",
                "reply": "你想让我具体做什么？",
                "task_groups": [],
                "ask_user": {
                    "task_title": "澄清指令",
                    "question": "你想让我具体做什么？",
                    "missing_slots": ["intent"],
                    "optional_slots": [],
                    "candidate_skills": self.registry.names(),
                },
                "confidence": 0.1,
            }
        return {
            "decision_type": "answer",
            "interaction_type": "conversation",
            "reply": self._fallback_conversation_reply(user_text),
            "task_groups": [],
            "ask_user": None,
            "confidence": 0.2,
        }

    def _task_plan_start_reply(self, task_groups: list[dict[str, Any]]) -> str:
        skill_names = [
            str(step.get("skill_name") or step.get("name") or "")
            for group in task_groups
            if isinstance(group, dict)
            for step in group.get("steps") or []
            if isinstance(step, dict) and str(step.get("skill_name") or step.get("name") or "")
        ]
        if skill_names and all(not self.registry.should_speak_start_ack(name) for name in skill_names):
            return ""
        return "收到"

    @staticmethod
    def _looks_like_ambiguous_action_request(text: str) -> bool:
        compact = re.sub(r"[\s\u3000，。,.？！!?、；;：:]+", "", str(text or ""))
        if not compact or compact.startswith("请问"):
            return False
        capability_markers = ("有什么功能", "有哪些功能", "你会什么", "你能做什么", "可以做什么", "会做什么")
        if any(marker in compact for marker in capability_markers):
            return False
        if Planner._looks_like_conversational_content_request(compact):
            return False
        request_markers = (
            "请你",
            "帮我",
            "麻烦你",
            "替我",
            "给我",
            "让机器人",
            "我要你",
            "你去",
            "你来",
            "执行一下",
            "动一下",
        )
        return any(marker in compact for marker in request_markers)

    @staticmethod
    def _looks_like_conversational_content_request(text: str) -> bool:
        compact = re.sub(r"[\s\u3000，。,.？！!?、；;：:]+", "", str(text or ""))
        if not compact:
            return False
        content_patterns = (
            r"(?:讲|说)(?:一个|个|段)?(?:笑话|故事|段子)",
            r"(?:聊聊|聊天|陪我聊|和我聊)",
            r"(?:介绍|说说)(?:一下)?(?:你自己|自己)",
            r"(?:自我介绍)",
            r"(?:解释|说明|回答|告诉我|写|创作|翻译|总结)(?:一下|一个|这|那|为什么|怎么|如何)?",
        )
        return any(re.search(pattern, compact) for pattern in content_patterns)

    def _fallback_conversation_reply(self, text: str) -> str:
        compact = re.sub(r"\s+", "", str(text or ""))
        if any(marker in compact for marker in ("有什么功能", "有哪些功能", "你会什么", "你能做什么", "可以做什么", "会做什么")):
            descriptions: list[str] = []
            disabled = self.registry.disabled_names()
            for name in self.registry.names():
                if name in disabled:
                    continue
                spec = self.registry.get(name) or {}
                description = str(spec.get("description_zh") or spec.get("description") or "").strip()
                if not description or description in descriptions or not re.search(r"[\u4e00-\u9fff]", description):
                    continue
                first_sentence = re.split(r"[。；;]", description, maxsplit=1)[0].strip()
                if first_sentence:
                    descriptions.append(first_sentence)
                if len(descriptions) >= 8:
                    break
            if descriptions:
                return "我当前可以调用这些机器人功能：" + "；".join(descriptions) + "等。"
            return "我可以和你对话，也可以根据当前加载的技能控制机器人完成任务。"
        if any(marker in compact for marker in ("你好", "您好", "嗨", "哈喽")):
            return "你好，我在。"
        if any(marker in compact for marker in ("谢谢", "感谢")):
            return "不客气。"
        if any(marker in compact for marker in ("介绍一下你自己", "介绍你自己", "自我介绍", "你是谁")):
            return "我是你的机器人助手，可以和你对话，也可以根据已加载的技能控制机器人完成任务。"
        return "我明白了。"

    def _split_multi_command(self, text: str) -> list[str]:
        parts = [part.strip(" ，,。；;") for part in re.split(r"(?:然后|再|接着|之后|顺便|并且|同时)", text) if part.strip(" ，,。；;")]
        return parts or [text]

    def _plan_one_chunk(self, text: str) -> dict[str, Any]:
        declared_query = self._plan_declared_query_skill(text)
        if declared_query is not None:
            return declared_query
        steps: list[dict[str, Any]] = []
        title = text[:24]
        slots: dict[str, Any] = {}

        if any(marker in text for marker in ("\u5173\u95ed\u6295\u5f71", "\u5173\u6389\u6295\u5f71", "\u505c\u6b62\u6295\u5f71", "\u505c\u6b62\u4f1a\u8bae\u6295\u5f71")):
            return {"task_group": self._task(text, "\u5173\u95ed\u6295\u5f71", {}, [
                {"skill_name": "projector_control", "arguments": {"action": "off"}, "reason": "\u7528\u6237\u8981\u6c42\u505c\u6b62\u6295\u5f71"},
                {"skill_name": "head_control", "arguments": {"action": "level"}, "reason": "\u6295\u5f71\u7ed3\u675f\u540e\u6062\u590d\u6c34\u5e73\u89c6\u89d2"},
            ])}

        meeting_projection = "\u4f1a\u8bae" in text and not any(
            marker in text for marker in ("\u5173\u95ed", "\u505c\u6b62", "\u4e0d\u8981", "\u4e0d\u60f3", "\u522b\u6295")
        ) and any(
            marker in text for marker in ("\u6295\u5f71", "\u6295\u5c4f", "\u4f1a\u8bae\u5185\u5bb9", "\u770b\u4f1a\u8bae")
        )
        if meeting_projection:
            points = self._known_navigation_points()
            point = self._extract_point(text) or self._resolve_navigation_point_name("wall", points) or "wall"
            return {
                "task_group": self._task(
                    text,
                    "\u4f1a\u8bae\u6295\u5f71",
                    {"where": point, "projection_mode": "meeting_single_image"},
                    [
                        {"skill_name": "navigation_goto", "arguments": {"point": point}, "reason": "\u5148\u5230\u7528\u6237\u6307\u5b9a\u7684\u4f1a\u8bae\u6295\u5f71\u4f4d\u7f6e\uff0c\u672a\u6307\u5b9a\u65f6\u9ed8\u8ba4\u767d\u5899"},
                        {"skill_name": "head_control", "arguments": {"action": "up"}, "reason": "\u62ac\u5934\u5bf9\u51c6\u6295\u5f71\u5899\u9762"},
                        {"skill_name": "environment_perception", "arguments": {"purpose": "projection", "camera": "front"}, "reason": "\u6295\u5f71\u524d\u68c0\u67e5\u5899\u9762\u548c\u5149\u7ebf\u6761\u4ef6"},
                        {"skill_name": "projector_control", "arguments": {"action": "meeting_presentation_on", "hold": True}, "reason": "\u5355\u56fe\u6301\u7eed\u6295\u5f71\u4f1a\u8bae\u5185\u5bb9"},
                    ],
                )
            }

        person_follow = any(marker in text for marker in ("\u8ddf\u7740", "\u8ddf\u968f", "\u8ffd\u8e2a\u884c\u4eba", "\u8ffd\u8e2a\u90a3\u4e2a\u4eba", "\u627e\u4e00\u4e0b\u90a3\u4e2a\u4eba"))
        person_location = any(marker in text for marker in ("\u5728\u54ea", "\u54ea\u91cc")) and any(
            marker in text for marker in ("\u90a3\u4e2a\u4eba", "\u884c\u4eba", "\u4ed6", "\u5979")
        )
        if person_follow or person_location:
            arguments: dict[str, Any] = {"action": "track"}
            target_match = re.search(r"(?:\u8ddf\u7740|\u8ddf\u968f|\u627e\u4e00\u4e0b)([^\uff0c\u3002,.!?\uff01\uff1f]{1,16})", text)
            if target_match:
                arguments["target"] = target_match.group(1).strip()
            return {"task_group": self._task(text, "\u5bfb\u627e\u5e76\u8ddf\u968f\u884c\u4eba", {"target": arguments.get("target")}, [{"skill_name": "person_tracking", "arguments": arguments, "reason": "\u5148\u539f\u5730\u65cb\u8f6c\u5bfb\u627e\u884c\u4eba\uff0c\u627e\u5230\u540e\u6301\u7eed\u8ddf\u968f"}])}

        squat_words = ["\u6df1\u8e72", "\u4e0b\u8e72", "\u8e72\u4e0b"]
        push_up_words = ["\u4fef\u5367\u6491", "\u4f0f\u5730\u633a\u8eab"]
        pull_up_words = ["\u5f15\u4f53\u5411\u4e0a", "\u5f15\u4f53"]
        projector = self._projector_preference_from_text(text)
        if any(word in text for word in squat_words + push_up_words + pull_up_words):
            if any(word in text for word in squat_words):
                exercise_skill = "squat"
                exercise_title = "\u6df1\u8e72\u8ba1\u6570"
            elif any(word in text for word in push_up_words):
                exercise_skill = "push_up"
                exercise_title = "\u4fef\u5367\u6491\u8ba1\u6570"
            else:
                exercise_skill = "pull_up"
                exercise_title = "\u5f15\u4f53\u5411\u4e0a\u8ba1\u6570"
            if projector is True:
                steps.append({"skill_name": "projector_control", "arguments": {"action": "fitness_video_on"}, "reason": "\u7528\u6237\u8865\u5145\u9700\u8981\u8fd0\u52a8\u89c6\u9891\u6295\u5f71\u8f85\u52a9"})
            steps.append({"skill_name": exercise_skill, "arguments": {"action": "run"}, "reason": "\u7528\u6237\u660e\u786e\u4e86\u8fd0\u52a8\u7c7b\u578b"})
            exercise_slots = {"exercise_type": exercise_skill}
            if projector is not None:
                exercise_slots["projector"] = projector
            return {"task_group": self._task(text, exercise_title, exercise_slots, steps)}

        if any(word in text for word in ["拍照", "拍一张", "照片", "图像"]):
            camera = "back" if any(word in text for word in ["后面", "后方", "背后", "后摄"]) else "front"
            skill = "back_camera_capture" if camera == "back" else "front_camera_capture"
            return {"task_group": self._task(text, title or "拍照", slots, [{"skill_name": skill, "arguments": {}, "reason": "用户要求拍摄照片"}])}

        if any(word in text for word in ["录像", "录视频", "视频"]):
            camera = "back" if any(word in text for word in ["后面", "后方", "背后", "后摄"]) else "front"
            skill = "back_camera_record" if camera == "back" else "front_camera_record"
            return {"task_group": self._task(text, title or "录像", slots, [{"skill_name": skill, "arguments": {}, "reason": "用户要求录制视频"}])}

        if any(word in text for word in ["追踪人", "追人", "跟着人", "跟着前面", "前面那个人", "行人"]):
            return {"task_group": self._task(text, "追踪行人", slots, [{"skill_name": "person_tracking", "arguments": {"action": "track"}, "reason": "用户要求追踪行人"}])}

        if any(word in text for word in ["追踪宠物", "追宠物", "跟着狗", "跟着猫"]):
            pet = "dog" if "狗" in text else "cat" if "猫" in text else "all"
            return {"task_group": self._task(text, "追踪宠物", {"pet": pet}, [{"skill_name": "pet_tracking", "arguments": {"action": "track", "pet": pet}, "reason": "用户要求追踪宠物"}])}

        if any(word in text for word in ["注册人脸", "录入人脸", "添加人脸"]):
            name = self._extract_name(text)
            if not name:
                return {
                    "task_group": self._task(text, "人脸注册", slots, []),
                    "ask_user": {
                        "task_title": "人脸注册",
                        "question": "你想注册谁的人脸？",
                        "missing_slots": ["name"],
                        "optional_slots": ["camera"],
                        "candidate_skills": ["face_registration"],
                    },
                }
            return {"task_group": self._task(text, "人脸注册", {"name": name}, [{"skill_name": "face_registration", "arguments": {"name": name}, "reason": "用户要求注册人脸"}])}

        if self._looks_like_face_recognition_request(text):
            return {"task_group": self._task(text, "人脸识别", slots, [{"skill_name": "face_recognition", "arguments": {}, "reason": "用户要求识别人脸"}])}

        if any(word in text for word in ["打开投影", "开投影"]):
            return {"task_group": self._task(text, "打开投影", slots, [{"skill_name": "projector_control", "arguments": {"action": "internal_on"}, "reason": "用户要求打开投影"}])}

        if any(word in text for word in ["关闭投影", "关投影"]):
            return {"task_group": self._task(text, "关闭投影", slots, [{"skill_name": "projector_control", "arguments": {"action": "off"}, "reason": "用户要求关闭投影"}])}

        if "投影" in text and any(word in text for word in ["状态", "怎么样"]):
            return {"task_group": self._task(text, "查询投影", slots, [{"skill_name": "projector_control", "arguments": {"action": "status"}, "reason": "用户查询投影状态"}])}

        light_words = ("落地灯", "灯光", "灯")
        declines_light = any(word in text for word in ("不用开灯", "不要开灯", "别开灯", "无需开灯"))
        if any(word in text for word in light_words) and "投影" not in text:
            status_request = any(word in text for word in ("状态", "开着吗", "关着吗", "亮着吗", "怎么样")) or (
                "吗" in text and any(word in text for word in ("打开", "关闭", "开着", "关着", "亮着"))
            )
            if status_request:
                action = "status"
                title = "查询落地灯"
            elif any(word in text for word in ("关闭", "关掉", "关灯", "熄灭")):
                action = "off"
                title = "关闭落地灯"
            elif declines_light:
                return {}
            elif any(word in text for word in ("打开", "开启", "开灯", "亮起来")):
                action = "on"
                title = "打开落地灯"
            else:
                return {
                    "task_group": self._task(text, "落地灯控制", slots, []),
                    "ask_user": {
                        "task_title": "落地灯控制",
                        "question": "落地灯需要打开、关闭，还是查看状态？",
                        "missing_slots": ["action"],
                        "optional_slots": [],
                        "candidate_skills": ["light_control"],
                    },
                }
            return {
                "task_group": self._task(
                    text,
                    title,
                    {"action": action},
                    [{"skill_name": "light_control", "arguments": {"action": action}, "reason": "用户要求控制米家落地灯"}],
                )
            }

        if "投食机" in text and any(word in text for word in ("状态", "在线", "怎么样", "正常吗")):
            return {
                "task_group": self._task(
                    text,
                    "查询投食机",
                    {"action": "status"},
                    [{"skill_name": "feeder_control", "arguments": {"action": "status"}, "reason": "用户查询米家投食机状态"}],
                )
            }

        feeder_words = ("喂食", "投食", "出粮", "放粮", "喂猫", "喂狗", "给猫吃", "给狗吃")
        declines_feeding = any(word in text for word in ("不用喂", "不要喂", "别喂", "无需喂", "不要投食", "别投食"))
        if any(word in text for word in feeder_words) and not declines_feeding:
            arguments: dict[str, Any] = {"action": "feed"}
            grams = self._feed_grams_from_text(text)
            if grams is not None:
                arguments["grams"] = grams
            return {
                "task_group": self._task(
                    text,
                    "宠物投食",
                    dict(arguments),
                    [
                        {
                            "skill_name": "feeder_control",
                            "arguments": arguments,
                            "reason": "用户明确要求通过米家投食机出粮",
                        }
                    ],
                )
            }

        if any(word in text for word in ["深蹲", "俯卧撑", "引体向上"]):
            exercise_skill = "squat" if "深蹲" in text else "push_up" if "俯卧撑" in text else "pull_up"
            projector = self._projector_preference_from_text(text)
            if projector is True:
                steps.append({"skill_name": "projector_control", "arguments": {"action": "fitness_video_on"}, "reason": "用户提到运动视频投影辅助"})
            steps.append({"skill_name": exercise_skill, "arguments": {"action": "run"}, "reason": "用户要求运动计数"})
            if projector is not None:
                slots["projector"] = projector
            return {"task_group": self._task(text, "运动计数", slots, steps)}

        if any(word in text for word in ["运动", "锻炼", "训练"]):
            return {
                "task_group": self._task(text, "运动任务", slots, []),
                "ask_user": {
                    "task_title": "运动任务",
                    "question": "你想做深蹲、俯卧撑还是引体向上？另外，是在这里做，还是去某个已保存的地点做？需要打开投影辅助吗？",
                    "missing_slots": ["exercise_type", "where"],
                    "optional_slots": ["projector_control"],
                    "candidate_skills": ["squat", "push_up", "pull_up", "projector_control"],
                },
            }

        movement_specs = (
            (("前进", "往前", "向前"), "move_forward", "前进"),
            (("后退", "往后", "倒退"), "move_backward", "后退"),
            (("左转", "向左", "往左"), "move_left", "左转"),
            (("右转", "向右", "往右"), "move_right", "右转"),
        )
        for phrases, skill_name, movement_title in movement_specs:
            if not any(word in text for word in phrases):
                continue
            arguments: dict[str, Any] = {}
            duration = self._duration_from_text(text)
            if duration is not None:
                arguments["duration"] = duration
                slots["duration"] = duration
            return {
                "task_group": self._task(
                    text,
                    movement_title,
                    slots,
                    [{"skill_name": skill_name, "arguments": arguments, "reason": f"用户要求底盘{movement_title}"}],
                )
            }

        if any(word in text for word in ["导航", "去", "到"]):
            point = self._extract_point(text)
            if not point:
                return {
                    "task_group": self._task(text, "导航", slots, []),
                    "ask_user": {
                        "task_title": "导航",
                        "question": "你想让我去哪个位置？",
                        "missing_slots": ["point_or_coordinates"],
                        "optional_slots": ["yaw", "frame_id"],
                        "candidate_skills": ["navigation_goto", "navigation_list"],
                    },
                }
            return {"task_group": self._task(text, "导航", {"point": point}, [{"skill_name": "navigation_goto", "arguments": {"point": point}, "reason": "用户要求导航到指定点"}])}

        if any(word in text for word in ["查询提醒", "查看提醒", "什么提醒", "哪些提醒", "多少提醒", "提醒列表"]):
            return {
                "task_group": self._task(
                    text,
                    "查询提醒",
                    {},
                    [{"skill_name": "reminder_query", "arguments": {}, "reason": "用户要求查询待执行提醒"}],
                )
            }

        if "提醒" in text and any(word in text for word in ["取消", "删除", "不用", "不要", "别"]):
            query = self._reminder_cancel_query(text)
            if not query:
                return {
                    "task_group": self._task(text, "取消提醒", {}, []),
                    "ask_user": {
                        "task_title": "取消提醒",
                        "question": "你想取消哪一个提醒？",
                        "missing_slots": ["query"],
                        "optional_slots": ["reminder_id"],
                        "candidate_skills": ["reminder_cancel"],
                    },
                }
            return {
                "task_group": self._task(
                    text,
                    "取消提醒",
                    {"query": query},
                    [{"skill_name": "reminder_cancel", "arguments": {"query": query}, "reason": "用户要求取消匹配的提醒"}],
                )
            }

        if any(word in text for word in ["提醒", "叫我"]):
            reminder_arguments = self._reminder_schedule_arguments(text)
            missing_slots = []
            if not reminder_arguments.get("content"):
                missing_slots.append("content")
            if not (reminder_arguments.get("trigger_time") or reminder_arguments.get("trigger_condition")):
                missing_slots.append("trigger_time_or_condition")
            task_group = self._task(text, "设置提醒", reminder_arguments, [])
            if missing_slots:
                questions = []
                if "content" in missing_slots:
                    questions.append("提醒你什么")
                if "trigger_time_or_condition" in missing_slots:
                    questions.append("什么时候提醒")
                return {
                    "task_group": task_group,
                    "ask_user": {
                        "task_title": "设置提醒",
                        "question": "请告诉我" + "、".join(questions) + "？",
                        "missing_slots": missing_slots,
                        "optional_slots": [],
                        "candidate_skills": ["reminder_schedule"],
                    },
                }
            task_group["steps"] = [
                {"skill_name": "reminder_schedule", "arguments": reminder_arguments, "reason": "用户给出了提醒内容和触发时间"}
            ]
            return {"task_group": task_group}

        if any(word in text for word in ["环境", "看看周围", "周围"]):
            return {"task_group": self._task(text, "环境感知", slots, [{"skill_name": "environment_perception", "arguments": {"purpose": "general"}, "reason": "用户要求感知周围环境"}])}

        return {}

    def _plan_declared_query_skill(self, text: str) -> dict[str, Any] | None:
        normalized = str(text or "").strip()
        if not normalized:
            return None
        for skill_name in self.registry.names():
            spec = self.registry.get(skill_name) or {}
            rules = spec.get("query_intents")
            if not isinstance(rules, list):
                continue
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                patterns = [str(item) for item in rule.get("patterns_zh") or [] if str(item)]
                excludes = [str(item) for item in rule.get("exclude_patterns_zh") or [] if str(item)]
                try:
                    excluded = excludes and any(re.search(pattern, normalized) for pattern in excludes)
                    matched = patterns and any(re.search(pattern, normalized) for pattern in patterns)
                except re.error:
                    continue
                if excluded or not matched:
                    continue
                action = str(rule.get("action") or "query")
                arguments = dict(rule.get("arguments") or {})
                arguments.setdefault("action", action)
                arguments[str(rule.get("query_argument") or "query")] = normalized
                title = str(rule.get("title_zh") or spec.get("description_zh") or skill_name)
                reason = str(rule.get("reason_zh") or "用户请求查询实时信息")
                return {
                    "task_group": self._task(
                        normalized,
                        title,
                        {"query_action": action},
                        [{"skill_name": skill_name, "arguments": arguments, "reason": reason}],
                    )
                }
        return None

    def _task(self, user_instruction: str, title: str, slots: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
        return {"title": title, "user_instruction": user_instruction, "slots": slots, "followups": [], "steps": steps}

    @staticmethod
    def _reminder_cancel_query(text: str) -> str:
        if any(word in text for word in ["刚才", "刚刚", "最近", "上一个"]):
            return "刚才"
        cleaned = re.sub(r"请你|帮我|取消|删除|不用|不要|别|提醒|了|吧", "", text)
        return re.sub(r"[，。,.？！!?、；;：:\s]+", "", cleaned).strip()

    @staticmethod
    def _reminder_schedule_arguments(text: str) -> dict[str, Any]:
        relative_pattern = r"(?:[零一二两三四五六七八九十百\d.]+|半)\s*(?:秒钟?|分钟?|小时|天)\s*(?:以后|之后|后)"
        clock_pattern = r"(?:(?:今天|明天|后天)\s*)?(?:(?:凌晨|早上|上午|中午|下午|傍晚|晚上)\s*)?(?:\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*[点时](?:半|(?:\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*分?)?"
        absolute_pattern = r"\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?\s+\d{1,2}[:点时]\d{1,2}(?::\d{1,2})?"
        trigger_match = re.search(f"({relative_pattern}|{clock_pattern}|{absolute_pattern})", text)
        trigger = trigger_match.group(1).strip() if trigger_match else ""
        content = text
        if trigger_match:
            content = content[: trigger_match.start()] + content[trigger_match.end() :]
        content = re.sub(r"请你|麻烦你|帮我|提醒我|叫我|提醒一下|定个提醒|设置提醒", "", content)
        content = re.sub(r"[，。,.？！!?、；;：:\s]+", " ", content).strip()
        arguments: dict[str, Any] = {}
        if content:
            arguments["content"] = content
        if trigger:
            arguments["trigger_condition"] = trigger
        return arguments

    @staticmethod
    def _duration_from_text(text: str) -> float | None:
        if not text:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:秒|s|sec|second)", text, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            return min(value, 30.0) if value > 0 else None
        match = re.search(r"([零一二两三四五六七八九十]{1,3})\s*秒", text)
        if not match:
            return None
        digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        token = match.group(1)
        if "十" in token:
            before, after = token.split("十", 1)
            tens = digits.get(before, 1) if before else 1
            value = tens * 10 + (digits.get(after, 0) if after else 0)
        else:
            value = digits.get(token, 0)
        return min(float(value), 30.0) if value > 0 else None

    def _extract_name(self, text: str) -> str | None:
        match = re.search(r"(?:叫|名字是|名叫)([\u4e00-\u9fa5A-Za-z0-9_]{1,12})", text)
        return match.group(1) if match else None

    def _postprocess_decision(
        self,
        decision: dict[str, Any],
        user_text: str,
        authoritative_user_text: bool = False,
    ) -> dict[str, Any]:
        decision = dict(decision or {})
        groups = [dict(group) for group in (decision.get("task_groups") or []) if isinstance(group, dict)]
        ask_user = decision.get("ask_user")
        interaction_type = str(decision.get("interaction_type") or "").strip().lower()
        local_evidence = self._local_fallback_plan(user_text)
        intent_analysis = decision.get("intent_analysis") if isinstance(decision.get("intent_analysis"), dict) else None
        model_semantic_action = bool(
            intent_analysis
            and intent_analysis.get("actionable")
            and not intent_analysis.get("negated")
            and not intent_analysis.get("uncertain")
        )
        if not model_semantic_action and self._looks_like_conversation_or_information(user_text, local_evidence):
            return self._as_conversation_decision(decision, user_text, reason="authoritative_conversation_evidence")
        if authoritative_user_text and intent_analysis is None and not decision.get("semantic_adjudication_completed"):
            local_skills = self._decision_intent_skills(local_evidence)
            model_skills = self._decision_intent_skills({"task_groups": groups, "ask_user": ask_user})
            if local_skills and (not model_skills or local_skills.isdisjoint(model_skills)):
                recovered = dict(local_evidence)
                for key in (
                    "asr_text",
                    "asr_text_source",
                    "authoritative_user_text",
                    "recovered_from_incomplete_json",
                    "incomplete_model_text",
                    "model_error",
                ):
                    if key in decision:
                        recovered[key] = decision[key]
                recovered["semantic_grounding_recovered"] = {
                    "model_skills": sorted(model_skills),
                    "transcript_skills": sorted(local_skills),
                }
                decision = recovered
                groups = [dict(group) for group in (decision.get("task_groups") or []) if isinstance(group, dict)]
                ask_user = decision.get("ask_user")
                interaction_type = str(decision.get("interaction_type") or "").strip().lower()
        if interaction_type in {"conversation", "capability_question", "information_question", "social"}:
            return self._as_conversation_decision(decision, user_text, reason="model_conversation_type")
        if self._is_ungrounded_intent_ask(ask_user, groups) and not self._looks_like_ambiguous_action_request(user_text):
            return self._as_conversation_decision(decision, user_text, reason="ungrounded_intent_ask")
        if not groups and isinstance(ask_user, dict) and self._looks_like_fitness_ask(user_text, ask_user):
            groups = [self._task(user_text, ask_user.get("task_title") or "\u8fd0\u52a8", {"intent": "fitness"}, [])]
        groups = self._split_independent_task_groups(groups)
        points = self._known_navigation_points()

        processed_groups: list[dict[str, Any]] = []
        for group in groups:
            group = self._normalize_navigation_group(group, points)
            group = self._postprocess_head_dependent_group(group)
            group = self._postprocess_meeting_projection_group(group, user_text, points)
            pet_group = self._postprocess_pet_group(group, user_text, points)
            fitness_result = self._postprocess_fitness_group(
                pet_group,
                user_text,
                points,
                authoritative_user_text=authoritative_user_text,
            )
            processed_groups.append(self._normalize_navigation_group(fitness_result["group"], points))
            if fitness_result.get("ask_user"):
                ask_user = fitness_result["ask_user"]

        decision["task_groups"] = processed_groups
        decision["ask_user"] = ask_user
        if ask_user:
            decision["decision_type"] = "ask_user"
            decision["reply"] = ask_user.get("question") or decision.get("reply") or ""
        elif processed_groups and decision.get("decision_type") in {"ask_user", "noop", "answer"}:
            decision["decision_type"] = "task_plan"
        return self.limit_followup_slots(decision, user_text)

    def _postprocess_meeting_projection_group(
        self,
        group: dict[str, Any],
        user_text: str,
        points: list[dict[str, Any]],
    ) -> dict[str, Any]:
        source = "\n".join((str(group.get("user_instruction") or ""), str(group.get("title") or ""), str(user_text or "")))
        if "\u4f1a\u8bae" not in source or any(marker in source for marker in ("\u5173\u95ed", "\u505c\u6b62", "\u4e0d\u8981", "\u4e0d\u60f3", "\u522b\u6295")) or not any(marker in source for marker in ("\u6295\u5f71", "\u6295\u5c4f", "\u4f1a\u8bae\u5185\u5bb9", "\u770b\u4f1a\u8bae")):
            return group
        slots = dict(group.get("slots") or {})
        point = None
        for key in ("where", "location", "place", "point", "destination"):
            if slots.get(key):
                point = self._resolve_navigation_point_name(slots[key], points)
                if point:
                    break
        point = point or self._extract_point(source) or self._resolve_navigation_point_name("wall", points) or "wall"
        group["title"] = group.get("title") or "\u4f1a\u8bae\u6295\u5f71"
        slots.update({"where": point, "projection_mode": "meeting_single_image"})
        group["slots"] = slots
        group["steps"] = [
            {"skill_name": "navigation_goto", "arguments": {"point": point}, "reason": "\u5230\u8fbe\u4f1a\u8bae\u6295\u5f71\u4f4d\u7f6e"},
            {"skill_name": "head_control", "arguments": {"action": "up"}, "reason": "\u62ac\u5934\u5bf9\u51c6\u5899\u9762"},
            {"skill_name": "environment_perception", "arguments": {"purpose": "projection", "camera": "front"}, "reason": "\u68c0\u67e5\u5f53\u524d\u4f4d\u7f6e\u7684\u6295\u5f71\u6761\u4ef6"},
            {"skill_name": "projector_control", "arguments": {"action": "meeting_presentation_on", "hold": True}, "reason": "\u5355\u56fe\u6301\u7eed\u6295\u5f71\u4f1a\u8bae\u5185\u5bb9"},
        ]
        return group

    def limit_followup_slots(self, decision: dict[str, Any], user_text: str = "") -> dict[str, Any]:
        """Expose at most N slot questions while retaining the full unresolved set."""
        decision = dict(decision or {})
        ask = decision.get("ask_user")
        if not isinstance(ask, dict):
            return decision
        ask = dict(ask)
        missing = self._dedupe_slots([*(ask.get("missing_slots") or []), *(ask.get("deferred_missing_slots") or [])])
        optional = self._dedupe_slots([*(ask.get("optional_slots") or []), *(ask.get("deferred_optional_slots") or [])])
        all_slots = self._dedupe_slots([*missing, *optional])
        if not all_slots:
            return decision
        max_slots = max(1, int(self.config.get("planner", {}).get("followup_max_slots_per_turn", 2)))
        seed_text = "|".join(
            (
                str(user_text or ""),
                str(ask.get("task_title") or ""),
                ",".join(all_slots),
                str(sum(len(group.get("followups") or []) for group in decision.get("task_groups") or [] if isinstance(group, dict))),
            )
        )
        ordered = sorted(
            all_slots,
            key=lambda slot: hashlib.sha256(f"{seed_text}|{slot}".encode("utf-8")).digest(),
        )
        selected = ordered[:max_slots]
        selected_missing = [slot for slot in selected if slot in missing]
        selected_optional = [slot for slot in selected if slot in optional]
        deferred_missing = [slot for slot in missing if slot not in selected]
        deferred_optional = [slot for slot in optional if slot not in selected]
        questions = self._slot_questions_for_ask(ask)
        can_rebuild = all(slot in questions for slot in selected)
        if can_rebuild:
            ask["question"] = " ".join(questions[slot].strip() for slot in selected if questions[slot].strip())
        ask.update(
            {
                "missing_slots": selected_missing,
                "optional_slots": selected_optional,
                "deferred_missing_slots": deferred_missing,
                "deferred_optional_slots": deferred_optional,
                "all_missing_slots": missing,
                "all_optional_slots": optional,
                "asked_slots": selected,
                "question_policy": {"max_slots_per_turn": max_slots, "order": "stable_shuffled"},
            }
        )
        decision["ask_user"] = ask
        if ask.get("question"):
            decision["reply"] = ask["question"]
        return decision

    @staticmethod
    def _dedupe_slots(values: list[Any]) -> list[str]:
        return list(dict.fromkeys(str(value) for value in values if str(value).strip()))

    def _slot_questions_for_ask(self, ask: dict[str, Any]) -> dict[str, str]:
        questions = {
            "exercise_type": "你想做哪种运动？深蹲、俯卧撑还是引体向上？",
            "where": "想在这里做，还是换个地方？",
            "projector_control": "需要我打开投影辅助吗？",
            "use_projector": "需要我打开投影辅助吗？",
            "what": "具体想让我做什么？",
            "who": "这件事和谁有关？",
            "when": "你希望什么时候进行？",
            "why": "这样安排主要是为了什么？",
            "how": "你希望我怎么处理？",
            "name": "请告诉我对应的名字。",
            "duration": "需要持续多长时间？",
            "direction": "你想让我往前、往后、往左还是往右？",
            "point_or_coordinates": "你想让我去哪个位置？",
            "point": "你想让我去哪个已保存位置？",
        }
        supplied = ask.get("slot_questions_zh") or ask.get("slot_questions")
        if isinstance(supplied, dict):
            questions.update({str(key): str(value) for key, value in supplied.items() if str(value).strip()})
        for skill_name in ask.get("candidate_skills") or []:
            spec = self.registry.get(str(skill_name)) or {}
            spec_questions = spec.get("slot_questions_zh")
            if isinstance(spec_questions, dict):
                questions.update({str(key): str(value) for key, value in spec_questions.items() if str(value).strip()})
            for enhancement in spec.get("optional_enhancements") or []:
                if not isinstance(enhancement, dict):
                    continue
                related = str(enhancement.get("related_skill") or "")
                question = str(enhancement.get("question_zh") or "").strip()
                if related and question:
                    questions.setdefault(related, question)
        return questions

    def _as_conversation_decision(self, decision: dict[str, Any], user_text: str, reason: str) -> dict[str, Any]:
        converted = dict(decision)
        recovered_reply = str(converted.get("recovered_from_non_json_text") or "").strip()
        current_reply = str(converted.get("reply") or "").strip()
        generic_clarification = any(
            marker in current_reply
            for marker in (
                "你想让我执行哪个任务",
                "你想让我具体做什么",
                "你想做什么运动",
                "请明确",
                "请补充",
            )
        )
        if recovered_reply:
            reply = recovered_reply
        elif current_reply and not generic_clarification:
            reply = current_reply
        else:
            reply = self._fallback_conversation_reply(user_text)
        converted.update(
            {
                "decision_type": "answer",
                "interaction_type": "conversation",
                "reply": reply,
                "task_groups": [],
                "ask_user": None,
                "reclassified_conversation_reason": reason,
            }
        )
        return converted

    def _looks_like_conversation_or_information(self, text: str, local_decision: dict[str, Any]) -> bool:
        if local_decision.get("decision_type") != "answer" or self._decision_intent_skills(local_decision):
            return False
        compact = re.sub(r"[\s\u3000，。,.！!、；;：:]+", "", str(text or ""))
        if not compact:
            return False
        capability_markers = ("功能", "能力", "你会什么", "你能做什么", "可以做什么", "会做什么")
        social_markers = ("你好", "您好", "嗨", "哈喽", "谢谢", "感谢", "再见", "早上好", "晚上好")
        question_markers = ("？", "?", "什么", "为什么", "怎么", "如何", "哪里", "哪儿", "多少", "谁", "吗", "呢")
        return (
            self._looks_like_conversational_content_request(compact)
            or
            any(marker in compact for marker in capability_markers)
            or any(marker in compact for marker in social_markers)
            or any(marker in str(text or "") for marker in question_markers)
        )

    @staticmethod
    def _decision_intent_skills(decision: dict[str, Any]) -> set[str]:
        support_skills = {"head_control", "environment_perception", "projector_control", "navigation_list"}
        skills: set[str] = set()
        for group in decision.get("task_groups") or []:
            if not isinstance(group, dict):
                continue
            for step in group.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                skill = str(step.get("skill_name") or step.get("name") or "").strip()
                if skill:
                    skills.add(skill)
        ask_user = decision.get("ask_user")
        if isinstance(ask_user, dict):
            missing = {str(item) for item in ask_user.get("missing_slots") or []}
            if "intent" not in missing:
                skills.update(str(item) for item in ask_user.get("candidate_skills") or [] if str(item))
        primary = skills - support_skills
        return primary or skills

    @staticmethod
    def _is_ungrounded_intent_ask(ask_user: Any, groups: list[dict[str, Any]]) -> bool:
        if not isinstance(ask_user, dict):
            return False
        missing = {str(item) for item in ask_user.get("missing_slots") or []}
        if "intent" not in missing:
            return False
        return not any(
            isinstance(step, dict) and str(step.get("skill_name") or step.get("name") or "").strip()
            for group in groups
            for step in group.get("steps") or []
        )

    def _split_independent_task_groups(self, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        split_groups: list[dict[str, Any]] = []
        fitness_skills = {"squat", "push_up", "pull_up"}
        fitness_helper_skills = fitness_skills | {"head_control", "environment_perception", "projector_control"}
        movement_skills = {"move_forward", "move_backward", "move_left", "move_right"}
        for group in groups:
            steps = [dict(step) for step in (group.get("steps") or []) if isinstance(step, dict)]
            skills = [step.get("skill_name") or step.get("name") for step in steps]
            has_fitness = any(skill in fitness_skills for skill in skills) or self._extract_exercise_type(str(group.get("user_instruction") or ""))
            has_independent_motion = any(skill in movement_skills for skill in skills)
            if not (has_fitness and has_independent_motion and len(steps) > 1):
                group["steps"] = steps
                split_groups.append(group)
                continue

            current: list[dict[str, Any]] = []
            fitness_steps: list[dict[str, Any]] = []
            seen_fitness = False
            for step in steps:
                skill = step.get("skill_name") or step.get("name")
                if skill in fitness_helper_skills or seen_fitness:
                    seen_fitness = True
                    fitness_steps.append(step)
                else:
                    current.append(step)
            if current:
                first_skill = current[0].get("skill_name") or current[0].get("name") or "task"
                motion_group = dict(group)
                motion_group["title"] = self._title_for_skill(str(first_skill))
                motion_group["slots"] = {}
                motion_group["followups"] = []
                motion_group["steps"] = current
                split_groups.append(motion_group)
            if fitness_steps:
                fitness_group = dict(group)
                fitness_group["title"] = self._fitness_title(group)
                fitness_group["steps"] = fitness_steps
                split_groups.append(fitness_group)
        return split_groups

    def _postprocess_head_dependent_group(self, group: dict[str, Any]) -> dict[str, Any]:
        steps = [dict(step) for step in (group.get("steps") or []) if isinstance(step, dict)]
        skills = [step.get("skill_name") or step.get("name") for step in steps]
        if any(skill in {"face_recognition", "face_registration"} for skill in skills) and "head_control" not in skills:
            steps.insert(
                0,
                {
                    "skill_name": "head_control",
                    "arguments": {"action": "up"},
                    "reason": "raise head before face camera perception",
                },
            )
        group["steps"] = steps
        return group

    def _postprocess_pet_group(self, group: dict[str, Any], user_text: str, points: list[dict[str, Any]]) -> dict[str, Any]:
        steps = [dict(step) for step in (group.get("steps") or []) if isinstance(step, dict)]
        if not steps:
            return group
        source_text = f"{group.get('user_instruction') or ''}\n{user_text or ''}"
        point_for_text = self._find_known_point_in_text(source_text, points)
        for step in steps:
            if (step.get("skill_name") or step.get("name")) != "pet_tracking":
                continue
            arguments = dict(step.get("arguments") or {})
            pet = self._extract_pet_type(source_text) or arguments.get("pet") or "all"
            arguments["pet"] = pet
            point = point_for_text
            if point:
                arguments["action"] = "find_route"
                arguments["search_strategy"] = "current_only"
                arguments["track_after_found"] = True
                step["arguments"] = arguments
                if not any((s.get("skill_name") or s.get("name")) == "navigation_goto" for s in steps):
                    steps.insert(
                        0,
                        {
                            "skill_name": "navigation_goto",
                            "arguments": {"action": "goto", "point": point},
                            "reason": "navigate to the requested saved point before searching for the pet",
                        },
                    )
                break
        group["steps"] = steps
        if any((step.get("skill_name") or step.get("name")) == "pet_tracking" and (step.get("arguments") or {}).get("action") == "find_route" for step in steps):
            slots = dict(group.get("slots") or {})
            slots.setdefault("pet", self._extract_pet_type(source_text) or "all")
            slots.setdefault("search_strategy", "current_then_known_points")
            slots.setdefault("track_after_found", True)
            slots.setdefault("visited_points", [])
            slots.setdefault("current_point", None)
            slots.setdefault("current_point_index", -1)
            slots.setdefault("found", False)
            slots.setdefault("found_at_point", None)
            slots.setdefault("last_pose", None)
            slots.setdefault("last_search_result", None)
            slots.setdefault("tracking_started", False)
            slots.setdefault("tracking_completed", False)
            slots.setdefault("video_path", None)
            group["slots"] = slots
        return group

    def _postprocess_fitness_group(
        self,
        group: dict[str, Any],
        user_text: str,
        points: list[dict[str, Any]],
        authoritative_user_text: bool = False,
    ) -> dict[str, Any]:
        steps = [dict(step) for step in (group.get("steps") or []) if isinstance(step, dict)]
        slots = dict(group.get("slots") or {})
        group_source_text = self._fitness_source_text(group, "")
        source_text = self._fitness_source_text(group, user_text)
        step_skills = {str(step.get("skill_name") or step.get("name") or "") for step in steps}
        fitness_skills = {"squat", "push_up", "pull_up"}
        fitness_helper_skills = fitness_skills | {"head_control", "environment_perception", "projector_control"}
        independent_non_fitness_skills = {
            "camera_capture",
            "camera_record",
            "face_recognition",
            "face_registration",
            "move_backward",
            "move_forward",
            "move_left",
            "move_right",
            "person_tracking",
            "pet_tracking",
        }
        group_exercise_type = self._extract_exercise_type(group_source_text)
        transcript_exercise_type = self._extract_exercise_type(user_text) if authoritative_user_text else None
        if authoritative_user_text and (self._looks_like_fitness_request(user_text) or transcript_exercise_type):
            exercise_type = transcript_exercise_type
            slots.pop("exercise_type", None)
            group["user_instruction"] = user_text
            if exercise_type:
                slots["exercise_type"] = exercise_type
            else:
                group["title"] = "运动"
        else:
            exercise_type = self._normalize_exercise_type(slots.get("exercise_type")) or self._exercise_from_steps(steps) or group_exercise_type
        has_fitness_step = bool(step_skills & fitness_skills)
        has_fitness_helper_step = bool(step_skills & fitness_helper_skills)
        group_looks_like_fitness = bool(group_exercise_type or self._looks_like_fitness_request(group_source_text))
        if authoritative_user_text:
            group_looks_like_fitness = bool(transcript_exercise_type or self._looks_like_fitness_request(user_text))
        if (
            steps
            and step_skills
            and step_skills <= independent_non_fitness_skills
            and not has_fitness_step
            and not has_fitness_helper_step
        ):
            group["steps"] = steps
            return {"group": group, "ask_user": None}
        if not exercise_type and (not steps or has_fitness_helper_step) and not group_looks_like_fitness:
            exercise_type = self._extract_exercise_type(source_text)
        is_fitness = bool(
            exercise_type
            or slots.get("intent") in {"fitness", "exercise"}
            or group_looks_like_fitness
            or (has_fitness_helper_step and self._looks_like_fitness_request(source_text))
        )
        if not is_fitness:
            group["steps"] = steps
            return {"group": group, "ask_user": None}

        if authoritative_user_text:
            where = self._extract_where(user_text, points)
            projector = self._projector_preference_from_text(user_text)
            for key in ("where", "location", "place", "projector", "projector_control", "need_projector"):
                slots.pop(key, None)
        else:
            where = self._normalize_where(slots.get("where") or slots.get("location") or slots.get("place"), points)
            if where is None:
                where = self._extract_where(source_text, points)
            projector = self._extract_projector_preference(source_text, slots, steps)
        missing: list[str] = []
        optional: list[str] = []
        if not exercise_type:
            missing.append("exercise_type")
        if where is None:
            missing.append("where")
        if projector is None:
            optional.append("projector_control")

        if missing or optional:
            group["steps"] = []
            if exercise_type:
                slots["exercise_type"] = exercise_type
            if where is not None:
                slots["where"] = where
            group["slots"] = slots
            return {
                "group": group,
                "ask_user": {
                    "task_title": group.get("title") or "\u8fd0\u52a8",
                    "question": self._fitness_question(missing, optional),
                    "missing_slots": missing,
                    "optional_slots": optional,
                    "candidate_skills": ["squat", "push_up", "pull_up"] + (["projector_control"] if optional else []),
                },
            }

        exercise_step = self._exercise_step(steps, exercise_type)
        exercise_args = dict(exercise_step.get("arguments") or {}) if exercise_step else {}
        exercise_args["action"] = exercise_args.get("action") or "run"
        back_camera = self.config.get("cameras", {}).get("back", {}).get("device")
        if back_camera:
            exercise_args["camera"] = back_camera

        normalized_steps: list[dict[str, Any]] = []
        if where != "here":
            normalized_steps.append(
                {
                    "skill_name": "navigation_goto",
                    "arguments": {"action": "goto", "point": where},
                    "reason": "fitness where is a saved navigation point",
                }
            )
        normalized_steps.extend(
            [
                {
                    "skill_name": "head_control",
                    "arguments": {"action": "up"},
                    "reason": "raise head so the rear camera can see the user's body and projection area",
                },
                {
                    "skill_name": "environment_perception",
                    "arguments": {"camera": "both", "purpose": "fitness_projection", "exercise_type": exercise_type},
                    "reason": "front camera checks projection conditions; rear camera checks exercise space and body framing",
                },
            ]
        )
        if projector is True:
            normalized_steps.append(
                {
                    "skill_name": "projector_control",
                    "arguments": {"action": "fitness_video_on"},
                    "reason": "user requested external video projection for exercise",
                }
            )
        normalized_steps.append(
            {
                "skill_name": exercise_type,
                "arguments": exercise_args,
                "reason": "run fitness counter after location, head pose, and environment checks are ready",
            }
        )
        slots.update({"exercise_type": exercise_type, "where": where, "projector": bool(projector)})
        group["slots"] = slots
        group["steps"] = normalized_steps
        group["title"] = group.get("title") or self._fitness_title(group)
        return {"group": group, "ask_user": None}

    def _known_navigation_points(self) -> list[dict[str, Any]]:
        path = self.config.get("robot_state", {}).get("navigation_points_path")
        if not path:
            path = str(Path(self.config.get("paths", {}).get("single_function_dir", "/home/test/qwen_single_function")) / "points" / "named_points.json")
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return []
        points = payload.get("points") if isinstance(payload, dict) and isinstance(payload.get("points"), dict) else payload
        if not isinstance(points, dict):
            return []
        result: list[dict[str, Any]] = []
        for name, value in points.items():
            item = {"id": name, "name": name, "display_name": name, "aliases": []}
            if isinstance(value, dict):
                item.update(value)
            canonical = str(item.get("name") or item.get("id") or name).strip() or str(name)
            item["id"] = canonical
            item["name"] = canonical
            item.setdefault("display_name", canonical)
            aliases = item.get("aliases")
            if isinstance(aliases, str):
                aliases = [aliases]
            if not isinstance(aliases, list):
                aliases = []
            normalized_aliases: list[str] = []
            for token in [name, item.get("display_name"), *aliases]:
                text = str(token or "").strip()
                if text and text != canonical and text not in normalized_aliases:
                    normalized_aliases.append(text)
            item["aliases"] = normalized_aliases
            result.append(item)
        return result

    def _navigation_point_tokens(self, point: dict[str, Any]) -> list[str]:
        tokens: list[str] = []
        for item in [point.get("id"), point.get("name"), point.get("display_name")]:
            text = str(item or "").strip()
            if text and text not in tokens:
                tokens.append(text)
        aliases = point.get("aliases")
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list):
            for item in aliases:
                text = str(item or "").strip()
                if text and text not in tokens:
                    tokens.append(text)
        return tokens

    def _resolve_navigation_point_name(self, value: Any, points: list[dict[str, Any]] | None = None) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        points = points if points is not None else self._known_navigation_points()
        text_lower = text.lower()
        for point in points:
            canonical = str(point.get("name") or point.get("id") or "").strip()
            if not canonical:
                continue
            for token in self._navigation_point_tokens(point):
                if text == token or text_lower == token.lower():
                    return canonical
        return None

    @staticmethod
    def _feed_grams_from_text(text: str) -> int | None:
        match = re.search(r"(\d+)\s*(?:克|g)", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        chinese = {
            "十": 10,
            "二十": 20,
            "两十": 20,
            "三十": 30,
            "四十": 40,
            "五十": 50,
            "六十": 60,
            "七十": 70,
            "八十": 80,
            "九十": 90,
            "一百": 100,
        }
        for token, grams in chinese.items():
            if f"{token}克" in text:
                return grams
        return None

    def _normalize_navigation_group(self, group: dict[str, Any], points: list[dict[str, Any]]) -> dict[str, Any]:
        steps = []
        for step in (group.get("steps") or []):
            if not isinstance(step, dict):
                continue
            step = dict(step)
            skill_name = step.get("skill_name") or step.get("name")
            if skill_name == "navigation_goto":
                arguments = dict(step.get("arguments") or {})
                for key in ("point", "destination", "name", "target", "point_or_coordinates"):
                    if key in arguments:
                        resolved = self._resolve_navigation_point_name(arguments.get(key), points)
                        if resolved:
                            arguments["point"] = resolved
                            for alias_key in ("destination", "name", "target", "point_or_coordinates"):
                                if alias_key in arguments and alias_key != "point":
                                    arguments.pop(alias_key, None)
                            break
                step["arguments"] = arguments
            steps.append(step)
        group["steps"] = steps
        slots = dict(group.get("slots") or {})
        for key in ("where", "location", "place", "point", "destination"):
            if key in slots:
                resolved = self._resolve_navigation_point_name(slots.get(key), points)
                if resolved:
                    slots[key] = resolved
        group["slots"] = slots
        return group

    def _fitness_source_text(self, group: dict[str, Any], user_text: str) -> str:
        parts = [str(group.get("user_instruction") or ""), str(group.get("title") or ""), str(user_text or "")]
        for followup in group.get("followups") or []:
            if isinstance(followup, dict):
                answer = str(followup.get("answer") or "")
                if answer:
                    parts.append(f"FOLLOWUP_ANSWER:{answer}")
        return "\n".join(part for part in parts if part)

    def _extract_projector_preference(self, text: str, slots: dict[str, Any], steps: list[dict[str, Any]]) -> bool | None:
        text_preference = self._projector_preference_from_text(text)
        if text_preference is not None:
            return text_preference
        for key in ("projector", "projector_control", "need_projector"):
            if key in slots:
                value = slots.get(key)
                value_preference = self._projector_preference_from_text(str(value))
                if value_preference is not None:
                    return value_preference
                if isinstance(value, bool):
                    if value is True and self._has_generic_yes_answer(text):
                        return True
                    if value is False and self._has_generic_no_answer(text):
                        return False
                    continue
                normalized = str(value).strip().lower()
                if normalized in {"true", "yes", "on", "1", "\u8981", "\u9700\u8981", "\u6253\u5f00"} and self._has_generic_yes_answer(text):
                    return True
                if normalized in {"false", "no", "off", "0", "\u4e0d\u8981", "\u4e0d\u9700\u8981", "\u4e0d\u7528"} and self._has_generic_no_answer(text):
                    return False
        return None

    def _projector_preference_from_text(self, text: str) -> bool | None:
        text = str(text or "").strip()
        if not text:
            return None
        negative = (
            "\u4e0d\u9700\u8981\u6295\u5f71",
            "\u4e0d\u7528\u6295\u5f71",
            "\u4e0d\u8981\u6295\u5f71",
            "\u522b\u5f00\u6295\u5f71",
            "\u4e0d\u5f00\u6295\u5f71",
            "\u4e0d\u6253\u5f00\u6295\u5f71",
            "\u4e0d\u7528\u6253\u5f00\u6295\u5f71",
            "\u4e0d\u7528\u5f00\u6295\u5f71",
            "\u4e0d\u9700\u8981\u5f00\u6295\u5f71",
            "\u65e0\u9700\u6295\u5f71",
            "\u4e0d\u6295\u5f71",
            "\u522b\u6295\u5f71",
        )
        if any(word in text for word in negative):
            return False
        positive = (
            "\u9700\u8981\u6295\u5f71",
            "\u8981\u6295\u5f71",
            "\u7528\u6295\u5f71",
            "\u5f00\u6295\u5f71",
            "\u6253\u5f00\u6295\u5f71",
            "\u6295\u5f71\u8f85\u52a9",
            "\u6295\u5f71\u8bad\u7ec3",
        )
        if any(word in text for word in positive):
            return True
        return None

    def _has_generic_yes_answer(self, text: str) -> bool:
        for line in str(text or "").splitlines():
            if not line.startswith("FOLLOWUP_ANSWER:"):
                continue
            compact = re.sub(r"[\s，,。.!！?？；;]", "", line.removeprefix("FOLLOWUP_ANSWER:"))
            if any(word in compact for word in ("\u8981", "\u9700\u8981", "\u597d", "\u597d\u7684", "\u53ef\u4ee5", "\u5f00", "\u6253\u5f00", "\u662f", "\u662f\u7684")):
                return True
        return False

    def _has_generic_no_answer(self, text: str) -> bool:
        for line in str(text or "").splitlines():
            if not line.startswith("FOLLOWUP_ANSWER:"):
                continue
            compact = re.sub(r"[\s，,。.!！?？；;]", "", line.removeprefix("FOLLOWUP_ANSWER:"))
            if any(word in compact for word in ("\u4e0d\u8981", "\u4e0d\u7528", "\u4e0d\u9700\u8981", "\u4e0d\u5f00", "\u522b\u5f00", "\u5426", "\u4e0d\u662f")):
                return True
        return False

    def _extract_exercise_type(self, text: str) -> str | None:
        if any(word in text for word in ("\u6df1\u8e72", "\u4e0b\u8e72", "\u8e72\u4e0b")):
            return "squat"
        if any(word in text for word in ("\u4fef\u5367\u6491", "\u4f0f\u5730\u633a\u8eab")):
            return "push_up"
        if any(word in text for word in ("\u5f15\u4f53\u5411\u4e0a", "\u5f15\u4f53")):
            return "pull_up"
        return None

    def _extract_pet_type(self, text: str) -> str | None:
        if any(word in text for word in ("\u72d7", "\u5c0f\u72d7")):
            return "dog"
        if any(word in text for word in ("\u732b", "\u5c0f\u732b")):
            return "cat"
        return None

    def _looks_like_fitness_request(self, text: str) -> bool:
        return any(word in text for word in ("\u8fd0\u52a8", "\u953b\u70bc", "\u8bad\u7ec3", "\u5065\u8eab"))

    @staticmethod
    def _looks_like_face_recognition_request(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or ""))
        if not compact:
            return False
        if any(token in compact for token in ("人脸识别", "识别一下", "这是谁", "看看这个人是谁")):
            return True
        identity = r"(?:是谁|什么人|哪位)"
        visible_position = r"(?:面前|前面|眼前|镜头里|画面里)"
        inspect = r"(?:看看|看一下|帮我看|识别)"
        return bool(
            re.search(visible_position + r".{0,8}" + identity, compact)
            or re.search(inspect + r".{0,12}" + identity, compact)
        )

    def _looks_like_fitness_ask(self, user_text: str, ask_user: dict[str, Any]) -> bool:
        user_text = str(user_text or "")
        task_title = str(ask_user.get("task_title") or "")
        question = str(ask_user.get("question") or "")
        missing = {str(item) for item in ask_user.get("missing_slots") or []}
        explicit_text = " ".join((user_text, task_title, question))
        has_fitness_language = self._looks_like_fitness_request(explicit_text) or any(
            token in explicit_text
            for token in ("\u6df1\u8e72", "\u4fef\u5367\u6491", "\u5f15\u4f53\u5411\u4e0a", "\u4e0b\u8e72")
        )
        has_fitness_slots = "exercise_type" in missing
        return has_fitness_language or has_fitness_slots

    def _normalize_exercise_type(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        mapping = {
            "squat": "squat",
            "push_up": "push_up",
            "pushup": "push_up",
            "pull_up": "pull_up",
            "pullup": "pull_up",
            "\u6df1\u8e72": "squat",
            "\u4e0b\u8e72": "squat",
            "\u4fef\u5367\u6491": "push_up",
            "\u5f15\u4f53\u5411\u4e0a": "pull_up",
        }
        return mapping.get(text)

    def _exercise_from_steps(self, steps: list[dict[str, Any]]) -> str | None:
        for step in steps:
            skill = step.get("skill_name") or step.get("name")
            if skill in {"squat", "push_up", "pull_up"}:
                return str(skill)
        return None

    def _exercise_step(self, steps: list[dict[str, Any]], exercise_type: str) -> dict[str, Any] | None:
        for step in steps:
            if (step.get("skill_name") or step.get("name")) == exercise_type:
                return step
        return None

    def _normalize_where(self, value: Any, points: list[dict[str, Any]]) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.lower() in {"here", "current", "current_location"} or text in {"\u8fd9\u91cc", "\u8fd9\u513f", "\u5c31\u5728\u8fd9", "\u539f\u5730"}:
            return "here"
        resolved = self._resolve_navigation_point_name(text, points)
        if resolved:
            return resolved
        return text

    def _extract_where(self, text: str, points: list[dict[str, Any]]) -> str | None:
        if any(word in text for word in ("\u8fd9\u91cc", "\u8fd9\u513f", "\u5c31\u5728\u8fd9", "\u539f\u5730")):
            return "here"
        return self._find_known_point_in_text(text, points)

    def _find_known_point_in_text(self, text: str, points: list[dict[str, Any]]) -> str | None:
        matches: list[tuple[int, str, str]] = []
        for point in points:
            canonical = str(point.get("name") or point.get("id") or "").strip()
            if not canonical:
                continue
            for token in self._navigation_point_tokens(point):
                if token and token in text:
                    matches.append((len(token), token, canonical))
        if matches:
            matches.sort(reverse=True)
            return matches[0][2]
        return None

    def _fitness_question(self, missing: list[str], optional: list[str]) -> str:
        projector_question = "\u9700\u8981\u6211\u6253\u5f00\u6295\u5f71\u8f85\u52a9\u8bad\u7ec3\u5417\uff1f" if optional else ""
        if "exercise_type" in missing and "where" in missing:
            return "\u4f60\u60f3\u505a\u4ec0\u4e48\u8fd0\u52a8\uff1f\u6df1\u8e72\u3001\u4fef\u5367\u6491\u8fd8\u662f\u5f15\u4f53\u5411\u4e0a\uff1f\u53e6\u5916\uff0c\u662f\u5728\u8fd9\u91cc\u505a\uff0c\u8fd8\u662f\u53bb\u67d0\u4e2a\u5df2\u4fdd\u5b58\u7684\u5730\u70b9\u505a\uff1f" + projector_question
        if "exercise_type" in missing:
            return "\u4f60\u60f3\u505a\u4ec0\u4e48\u8fd0\u52a8\uff1f\u6df1\u8e72\u3001\u4fef\u5367\u6491\u8fd8\u662f\u5f15\u4f53\u5411\u4e0a\uff1f" + projector_question
        if "where" in missing:
            return "\u8bf7\u95ee\u662f\u5728\u8fd9\u91cc\u505a\uff0c\u8fd8\u662f\u53bb\u67d0\u4e2a\u5df2\u4fdd\u5b58\u7684\u5730\u70b9\u505a\uff1f" + projector_question
        if optional:
            return projector_question
        return "\u8bf7\u8865\u5145\u8fd0\u52a8\u4efb\u52a1\u7684\u4fe1\u606f\u3002"

    def _fitness_title(self, group: dict[str, Any]) -> str:
        slots = group.get("slots") or {}
        exercise = self._normalize_exercise_type(slots.get("exercise_type")) or self._extract_exercise_type(str(group.get("user_instruction") or ""))
        titles = {"squat": "\u6df1\u8e72\u8ba1\u6570", "push_up": "\u4fef\u5367\u6491\u8ba1\u6570", "pull_up": "\u5f15\u4f53\u5411\u4e0a\u8ba1\u6570"}
        return titles.get(exercise or "", str(group.get("title") or "\u8fd0\u52a8"))

    def _title_for_skill(self, skill_name: str) -> str:
        return {
            "move_forward": "\u524d\u8fdb",
            "move_backward": "\u540e\u9000",
            "move_left": "\u5de6\u8f6c",
            "move_right": "\u53f3\u8f6c",
            "navigation_goto": "\u5bfc\u822a",
        }.get(skill_name, skill_name)

    def _extract_point(self, text: str) -> str | None:
        points = self._known_navigation_points()
        point = self._find_known_point_in_text(text, points)
        if point:
            return point
        aliases = {
            "start_point": "living_room",
            "end_point": "wall",
            "\u8d77\u70b9": "living_room",
            "\u7ec8\u70b9": "wall",
            "\u5ba2\u5385": "living_room",
            "\u767d\u5899": "wall",
        }
        for alias, target in aliases.items():
            if alias in text:
                return self._resolve_navigation_point_name(target, points) or target
        match = re.search(r"(?:去|到|导航到)([\u4e00-\u9fa5A-Za-z0-9_]{1,16})", text)
        if not match:
            return None
        raw = match.group(1)
        return self._resolve_navigation_point_name(raw, points) or raw
