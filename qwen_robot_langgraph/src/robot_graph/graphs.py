from __future__ import annotations

import json
import time

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .contracts import Action, ExecutionState
from .execution import Unavailable


TERMINAL = {"completed", "failed", "cancelled", "blocked", "unknown"}


def task_graph(runtime, checkpointer):
    store, executor = runtime.store, runtime.executor

    def command(s):
        row = store.one("SELECT command FROM tasks WHERE id=?", (s["task_id"],))
        return row["command"] if row else "cancel"

    def select(s):
        phase = s.get("phase", "main")
        cmd = command(s)
        if phase == "main" and cmd == "cancel":
            return {"phase": "cleanup", "phase_index": 0, "outcome": "cancelled", "status": "cancelling", "route": "select"}
        if phase == "main" and cmd == "pause":
            return {"phase": "pause", "phase_index": 0, "pause_started": time.time(), "status": "pausing", "route": "select"}
        if phase in {"pause", "resume"}:
            steps = ([Action("projector.pause" if phase == "pause" else "projector.resume").to_dict()] if s.get("started_projection") else [])
            if s.get("phase_index", 0) >= len(steps):
                if phase == "pause":
                    return {"status": "paused", "route": "paused"}
                with store.tx() as db:
                    db.execute("UPDATE tasks SET command='' WHERE id=? AND command='resume'", (s["task_id"],))
                extra = time.time() - s.get("pause_started", time.time())
                return {"phase": "main", "status": "running", "hold_until": s.get("hold_until", 0) + extra if s.get("hold_until", 0) > 0 else s.get("hold_until", 0), "route": "select"}
            return {"current_action": steps[s.get("phase_index", 0)], "route": "issue"}
        if phase == "cleanup":
            rows = store.all("SELECT kind,result FROM operations WHERE task=? AND id LIKE ?", (s["task_id"], s["task_id"] + ":%:main:%"))
            attempted = {row["kind"] for row in rows if json.loads(row["result"]).get("executed") is not False or json.loads(row["result"]).get("simulated")}
            def needed(action):
                if action["kind"] == "projector.off": return "projector.start" in attempted
                if action["kind"] == "head.move": return "head.move" in attempted
                if action["kind"] == "sensor.gate": return bool({"sensor.gate", "head.move"} & attempted)
                return True
            steps = [action for action in s["plan"].get("cleanup", []) if needed(action)]
            if s.get("phase_index", 0) >= len(steps):
                return {"status": "failed" if s.get("cleanup_errors") else s.get("outcome", "completed"), "route": "finish"}
            return {"current_action": steps[s.get("phase_index", 0)], "route": "issue", "status": "cleanup"}
        index = s.get("index", 0)
        if index >= len(s["plan"]["steps"]):
            if s["plan"].get("hold"):
                until = s.get("hold_until", 0)
                if until == 0:
                    seconds = s["plan"].get("hold_seconds", 0)
                    runtime.speech.enqueue(s["task_id"], s["epoch"], s["task_id"] + ":active", s["plan"]["active_speech"])
                    return {"hold_until": time.time() + seconds if seconds else -1, "status": "active", "route": "holding"}
                if until < 0 or time.time() < until:
                    return {"status": "active", "route": "holding"}
            return {"phase": "cleanup", "phase_index": 0, "outcome": "completed", "route": "select"}
        if s["plan"].get("require_pet") and index == 1 and not s.get("result", {}).get("pet_visible"):
            return {"phase": "cleanup", "phase_index": 0, "outcome": "failed", "error": "pet_not_confirmed_no_feeding", "route": "select"}
        if index == s["plan"].get("speech_after", -1) and s["plan"].get("speech"):
            runtime.speech.enqueue(s["task_id"], s["epoch"], s["task_id"] + ":prepare", s["plan"]["speech"])
        action = dict(s["plan"]["steps"][index])
        if action["kind"] == "exercise.count" and s.get("partial_count"):
            action["args"] = {**action["args"], "count": max(1, action["args"]["count"] - s["partial_count"])}
        return {"current_action": action, "route": "issue", "status": "running"}

    def issue(s):
        phase = s.get("phase", "main")
        index = s.get("index", 0) if phase == "main" else s.get("phase_index", 0)
        op = f'{s["task_id"]}:{s["epoch"]}:{phase}:{index}'
        action = Action.parse(s["current_action"])
        # Keep non-idempotent dispatch in the durable ledger. Node replays reuse op.
        try:
            executor.issue(s["task_id"], s["epoch"], op, action, control_phase=phase != "main")
        except (Unavailable, ValueError) as exc:
            return {"result": {"ok": False, "status": "cancelled" if str(exc) == "dispatch_cancelled_by_control" else "failed", "executed": False, "error": str(exc)}, "operation_id": "", "route": "advance"}
        return {"operation_id": op, "route": "wait"}

    def wait(s):
        interrupt({"kind": "operation", "operation_id": s["operation_id"]})
        row = executor.status(s["operation_id"])
        if row is None or row["status"] == "accepted":
            return {"route": "wait"}
        return {"result": row["result"], "route": "advance"}

    def advance(s):
        result = s["result"]
        phase = s.get("phase", "main")
        status = result.get("status", "failed")
        if status == "unknown":
            return {"status": "unknown", "error": result.get("error", "unknown_hardware_outcome"), "route": "finish"}
        if not result.get("ok"):
            if phase == "cleanup":
                return {"phase_index": s.get("phase_index", 0) + 1, "cleanup_errors": s.get("cleanup_errors", []) + [result.get("error", status)], "route": "select"}
            if status == "cancelled" and command(s) == "pause":
                return {"partial_count": s.get("partial_count", 0) + result.get("completed_count", 0), "phase": "pause", "phase_index": 0, "pause_started": time.time(), "route": "select"}
            return {"phase": "cleanup", "phase_index": 0, "outcome": "cancelled" if command(s) == "cancel" else "failed", "error": result.get("error", status), "route": "select"}
        if phase != "main":
            return {"phase_index": s.get("phase_index", 0) + 1, "route": "select"}
        return {"index": s.get("index", 0) + 1, "partial_count": 0,
                "started_projection": s.get("started_projection", False) or s["current_action"]["kind"] == "projector.start",
                "results": s.get("results", []) + [result], "route": "select"}

    def holding(s):
        interrupt({"kind": "active_session", "task_id": s["task_id"]})
        return {"route": "select"}

    def paused(s):
        interrupt({"kind": "paused", "task_id": s["task_id"]})
        cmd = command(s)
        if cmd == "cancel":
            return {"phase": "cleanup", "phase_index": 0, "outcome": "cancelled", "route": "select"}
        if cmd != "resume":
            return {"route": "paused"}
        epoch = s["epoch"] + 1
        with store.tx() as db:
            db.execute("UPDATE tasks SET epoch=? WHERE id=?", (epoch, s["task_id"]))
            db.execute("UPDATE leases SET epoch=? WHERE task=?", (epoch, s["task_id"]))
        return {"epoch": epoch, "phase": "resume", "phase_index": 0, "route": "select"}

    def finish(s):
        if s["status"] != "unknown":
            store.release(s["task_id"])
        runtime.speech.invalidate(s["task_id"])
        if s["status"] == "completed" and s["plan"].get("announce_result"):
            from .rendering import result_speech
            runtime.speech.enqueue(s["task_id"], s["epoch"], s["task_id"] + ":final", result_speech(s))
        store.event(s["task_id"], "task_finished", status=s["status"], cleanup_errors=s.get("cleanup_errors", []))
        return {}

    graph = StateGraph(ExecutionState)
    nodes = {"select": select, "issue": issue, "wait": wait, "advance": advance, "holding": holding, "paused": paused, "finish": finish}
    for name, fn in nodes.items():
        graph.add_node(name, fn)
    graph.add_edge(START, "select")
    for name in nodes:
        if name == "finish":
            graph.add_edge(name, END)
        else:
            graph.add_conditional_edges(name, lambda s: s["route"], {key: key for key in nodes})
    return graph.compile(checkpointer=checkpointer)
