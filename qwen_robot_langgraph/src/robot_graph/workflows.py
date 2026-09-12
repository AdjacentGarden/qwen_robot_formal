"""Reviewed, deterministic plans. Navigation is never silently skipped."""
from .contracts import Action, PolicyError


def step(kind, **args):
    return Action(kind, args, 45.0 if kind == 'head.move' else 30.0).to_dict()


def build_plan(name, parameters=None):
    p = dict(parameters or {})
    allowed = {
        "meeting": {"point", "hold_seconds"},
        "meeting_stationary": {"hold_seconds"},
        "exercise_stationary": {"exercise", "count"},
        "observe_and_feed": {"grams"},
    }
    if name not in allowed or set(p) - allowed[name]:
        raise PolicyError("unknown_workflow_or_parameter")
    hold = p.get("hold_seconds", 0)
    if type(hold) not in (float, int) or not 0 <= hold <= 3600:
        raise PolicyError("invalid_hold_seconds")
    if name.startswith("meeting") or name == "exercise_stationary":
        steps = []
        if name == "meeting":
            steps.append(step("navigation.goto", point=p.get("point", "study_projection")))
        steps += [step("sensor.gate", enabled=False), step("head.move", pose="up"), step("projector.start", mode="meeting" if name.startswith("meeting") else "exercise")]
        if name == "exercise_stationary":
            steps.append(step("exercise.count", exercise=p.get("exercise", "push_up"), count=p.get("count", 5)))
        return {"name": name, "steps": steps,
                "cleanup": [step("projector.off"), step("head.move", pose="level"), step("sensor.gate", enabled=True)],
                "hold": name.startswith("meeting"), "hold_seconds": hold,
                "speech": "正在当前位置准备投影。" if name != "meeting" else "已到达投影位置，正在准备投影。",
                "speech_after": 1 if name == "meeting" else 0,
                "active_speech": "投影已启动。" if name.startswith("meeting") else "运动计数已完成。"}
    return {"name": name, "steps": [step("pet.observe"), step("feeder.feed", grams=p.get("grams", 10))], "cleanup": [], "require_pet": True, "hold": False}


def atomic_plan(action):
    if action.kind == 'head.move' and action.timeout == 30.0:
        action = Action(action.kind, action.args, 45.0)
    return {"name": action.kind, "steps": [action.to_dict()], "cleanup": [], "hold": False}
