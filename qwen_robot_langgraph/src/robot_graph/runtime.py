from __future__ import annotations

import fcntl
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .contracts import Action, CAPABILITIES, PolicyError, StationaryPolicy
from .execution import Executor, Unavailable
from .graphs import TERMINAL, task_graph
from .speech import SpeechOutbox
from .storage import Store


class Runtime:
    def __init__(self, path, backend, policy=None):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.owner_lock = (self.path / "owner.lock").open("a")
        try:
            fcntl.flock(self.owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner_lock.close()
            raise RuntimeError("runtime_already_owned")
        if hasattr(backend, "acquire_owner"):
            try:
                backend.acquire_owner()
            except BaseException:
                self.owner_lock.close()
                raise
        self.store = Store(self.path / "ledger.sqlite")
        self.policy = policy or StationaryPolicy()
        self.executor = Executor(self.store, self.policy, backend)
        self.speech = SpeechOutbox(self.store, self.executor)
        self.connection = sqlite3.connect(str(self.path / "checkpoints.sqlite"), check_same_thread=False)
        self.checkpointer = SqliteSaver(self.connection)
        self.graph = task_graph(self, self.checkpointer)
        self.locks = {}
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.worker = None
        # Restart never assumes a lost process/socket means an action did not happen.
        with self.store.tx() as db:
            db.execute("UPDATE operations SET status='unknown',result=? WHERE status='accepted'", (json.dumps({"ok": False, "status": "unknown", "executed": None, "error": "process_restarted_reconcile_required"}),))
            db.execute("UPDATE outbox SET status='cancelled' WHERE status IN ('pending','playing')")
        self.speech.recover()
        self._recover()

    def _recover(self):
        for row in self.store.all("SELECT * FROM tasks WHERE status NOT IN ('completed','failed','cancelled','blocked','unknown') AND id NOT LIKE 'voice:%'"):
            state = self.graph.get_state(self.config(row["id"]))
            if not state.values:
                # Submitted before first checkpoint is safe only if no dispatch exists.
                if self.store.one("SELECT id FROM operations WHERE task=?", (row["id"],)):
                    self._status(row["id"], "unknown")
                continue
            unknown = self.store.one("SELECT id FROM operations WHERE task=? AND status='unknown'", (row["id"],))
            if unknown:
                self.graph.update_state(self.config(row["id"]), {"status": "unknown", "error": "process_restarted_reconcile_required"})
                self._status(row["id"], "unknown")
            elif state.values.get("status") in TERMINAL:
                self._status(row["id"], state.values["status"])
            else:
                # Hardware may have changed while the orchestrator was absent. Keep
                # leases and require reconciliation, never label it physically paused.
                self.graph.update_state(self.config(row["id"]), {"status": "unknown", "error": "restart_requires_fresh_device_reconciliation"})
                self._status(row["id"], "unknown")

    @staticmethod
    def config(task):
        return {"configurable": {"thread_id": "task:" + task}, "recursion_limit": 200}

    def _status(self, task, status):
        with self.store.tx() as db:
            db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task))

    def submit(self, session, request_key, plan):
        if not session or not request_key or len(session) > 128 or len(request_key) > 128:
            raise PolicyError("invalid_request_identity")
        with self.lock:
            key = session + ":" + request_key
            existing = self.store.one("SELECT * FROM tasks WHERE request_key=?", (key,))
            if existing:
                if json.loads(existing["plan"]) != plan:
                    raise PolicyError("request_key_payload_mismatch")
                return existing["id"]
            if not isinstance(plan, dict) or not plan.get("steps") or len(plan["steps"]) > 32:
                raise PolicyError("invalid_plan")
            # Validate the entire plan and its cleanup before the first side effect.
            actions = [Action.parse(x) for x in plan["steps"] + plan.get("cleanup", [])]
            for action in actions:
                self.policy.validate(action)
                self.executor.backend.preflight(action)
            task = uuid.uuid4().hex
            with self.store.tx() as db:
                db.execute("INSERT INTO tasks(id,session,request_key,plan,status,created) VALUES(?,?,?,?,?,?)", (task, session, key, json.dumps(plan, ensure_ascii=False, sort_keys=True), "queued", time.time()))
            self.store.event(task, "task_submitted", name=plan["name"])
            return task

    def control(self, session, task, command):
        if command not in {"pause", "resume", "cancel"}:
            raise PolicyError("unknown_control")
        with self.store.tx() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=? AND session=?", (task, session)).fetchone()
            if not row:
                raise PolicyError("task_not_found_in_session")
            if row["status"] in TERMINAL:
                return {"task_id": task, "status": row["status"], "accepted": False}
            if command == "resume" and row["status"] != "paused":
                raise PolicyError("task_not_paused")
            if row["command"] == "cancel" and command != "cancel":
                raise PolicyError("task_cancelling")
            db.execute("UPDATE tasks SET command=? WHERE id=?", (command, task))
        if command in {"pause", "cancel"}:
            self.speech.invalidate(task)
            for op in self.store.all("SELECT id FROM operations WHERE task=? AND status='accepted'", (task,)):
                self.executor.cancel(op["id"])
        self.store.event(task, "control_requested", command=command)
        return {"task_id": task, "accepted": True, "status": "pausing" if command == "pause" else "cancelling" if command == "cancel" else "resuming"}

    def tick(self, task):
        with self.lock:
            lock = self.locks.setdefault(task, threading.RLock())
        if not lock.acquire(blocking=False):
            return
        try:
            row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
            if not row or row["status"] in TERMINAL:
                return
            config = self.config(task)
            snapshot = self.graph.get_state(config)
            if not snapshot.values:
                plan = json.loads(row["plan"])
                resources = sorted({r for x in plan["steps"] + plan.get("cleanup", []) for r in CAPABILITIES[x["kind"]].resources})
                if row["command"] == "cancel":
                    self._status(task, "cancelled")
                    return
                if row["command"] != "pause" and not self.store.claim(task, row["epoch"], resources):
                    self._status(task, "waiting_resources")
                    return
                self._status(task, "running")
                data = {"task_id": task, "session_id": row["session"], "plan": plan, "index": 0, "phase": "main", "epoch": row["epoch"], "results": [], "status": "running"}
            elif snapshot.next:
                state = snapshot.values
                node = snapshot.next[0]
                if node == "wait":
                    op = self.executor.status(state["operation_id"])
                    if op and op["status"] == "accepted":
                        if time.time() - op["started"] > state["current_action"].get("timeout", 30):
                            self.executor.cancel(op["id"])
                            self.graph.update_state(config, {"status": "unknown", "error": "operation_deadline_waiting_for_device_result"})
                            self._status(task, "unknown")
                            self.speech.invalidate(task)
                            self.store.event(task, "operation_deadline", operation_id=op["id"])
                            return
                        if row["command"] in {"pause", "cancel"}:
                            self.executor.cancel(op["id"])
                        return
                if node == "paused" and row["command"] not in {"resume", "cancel"}:
                    self._status(task, "paused")
                    return
                if node == "paused" and row["command"] == "resume":
                    plan = state["plan"]
                    resources = {r for x in plan["steps"] + plan.get("cleanup", []) for r in CAPABILITIES[x["kind"]].resources}
                    if not self.store.claim(task, state["epoch"], resources):
                        return
                if node == "holding" and row["command"] not in {"pause", "cancel"} and (state.get("hold_until", -1) < 0 or time.time() < state["hold_until"]):
                    self._status(task, "active")
                    return
                data = Command(resume={"event": "poll"}) if snapshot.interrupts else None
            else:
                self._status(task, snapshot.values["status"])
                return
            for update in self.graph.stream(data, config, stream_mode="updates"):
                for node, changes in update.items():
                    if node != "__interrupt__":
                        self.store.event(task, "graph_node", node=node, status=(changes or {}).get("status"), index=(changes or {}).get("index"))
            state = self.graph.get_state(config).values
            self._status(task, state.get("status", "running"))
        except Exception as exc:
            self._status(task, "unknown")
            self.store.event(task, "runtime_error", error=f"{type(exc).__name__}:{exc}")
        finally:
            lock.release()

    def tick_all(self):
        for row in self.store.all("SELECT id FROM tasks WHERE status NOT IN ('completed','failed','cancelled','blocked','unknown') AND id NOT LIKE 'voice:%'"):
            self.tick(row["id"])
        self.speech.tick()

    def status(self, task, session=None):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task,))
        if not row or (session is not None and row["session"] != session):
            raise PolicyError("task_not_found_in_session")
        state = self.graph.get_state(self.config(task)).values
        return {"task_id": task, "status": row["status"], "command": row["command"], "state": dict(state)}

    def start(self):
        def loop():
            while not self.stopping.wait(0.02):
                self.tick_all()
        self.worker = threading.Thread(target=loop, name="graph-runner", daemon=True)
        self.worker.start()

    def close(self, cancel_tasks=False):
        self.stopping.set()
        if self.worker:
            self.worker.join(timeout=5)
        if cancel_tasks:
            for row in self.store.all("SELECT id,session FROM tasks WHERE status NOT IN ('completed','failed','cancelled','blocked','unknown') AND id NOT LIKE 'voice:%'"):
                self.control(row["session"], row["id"], "cancel")
            # A confirmed projector-off + slow head-level + lidar recovery can
            # legitimately take a little over 30 seconds on this robot.
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                self.tick_all()
                active = self.store.one("SELECT id FROM tasks WHERE status NOT IN ('completed','failed','cancelled','blocked','unknown') AND id NOT LIKE 'voice:%'")
                if not active:
                    break
                time.sleep(.02)
        self.executor.close()
        if hasattr(self.executor.backend, "release_owner"):
            self.executor.backend.release_owner()
        self.connection.close()
        self.store.close()
        fcntl.flock(self.owner_lock, fcntl.LOCK_UN)
        self.owner_lock.close()
