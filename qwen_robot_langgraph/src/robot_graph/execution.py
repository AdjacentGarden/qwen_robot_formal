from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

from .contracts import Action, CAPABILITIES, PolicyError


class Unavailable(RuntimeError):
    """Failure known to happen before any hardware dispatch."""


class SimulatedBackend:
    """Fault-injectable driver, never imported by the real hardware implementation."""
    def __init__(self, delays=None, failures=None):
        self.delays = delays or {}
        self.failures = failures or {}
        self.calls = []
        self.lock = threading.Lock()
        self.pet_visible = True

    def preflight(self, action):
        pass

    def run(self, action, cancel):
        with self.lock:
            self.calls.append((action.kind, dict(action.args), time.monotonic()))
        started = time.monotonic()
        duration = self.delays.get(action.kind, 0.015)
        while time.monotonic() - started < duration:
            if cancel.is_set() and CAPABILITIES[action.kind].cancellable:
                progress = int(action.args.get("count", 0) * min(1, (time.monotonic() - started) / duration))
                return {"ok": False, "status": "cancelled", "executed": True, "completed_count": progress}
            time.sleep(0.003)
        if action.kind in self.failures:
            failure = self.failures[action.kind]
            if isinstance(failure, Exception):
                raise failure
            return {"ok": False, "status": failure, "executed": True, "error": "injected_failure"}
        result = {"ok": True, "status": "completed", "executed": False, "simulated": True}
        if action.kind == "pet.observe":
            result["pet_visible"] = self.pet_visible
        if action.kind == "exercise.count":
            result["completed_count"] = action.args["count"]
        return result


class Executor:
    def __init__(self, store, policy, backend, workers=8):
        self.store, self.policy, self.backend = store, policy, backend
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hardware")
        self.active = {}
        self.lock = threading.RLock()

    def issue(self, task, epoch, operation_id, action, control_phase=False):
        self.policy.validate(action)  # Defense at the last common execution boundary.
        with self.lock:
            row = self.store.one("SELECT * FROM operations WHERE id=?", (operation_id,))
            if row:
                if row["kind"] != action.kind or json.loads(row["args"]) != action.args:
                    raise PolicyError("operation_id_payload_mismatch")
                return row
            self.backend.preflight(action)
            with self.store.tx() as db:
                taskrow = db.execute("SELECT epoch,status,command FROM tasks WHERE id=?", (task,)).fetchone()
                if not taskrow or taskrow[0] != epoch or taskrow[1] in {"completed", "failed", "cancelled", "blocked", "unknown"}:
                    raise PolicyError("stale_task_fence")
                if not control_phase and taskrow[2] in {"pause", "cancel"}:
                    raise PolicyError("dispatch_cancelled_by_control")
                resources = CAPABILITIES[action.kind].resources
                for resource in resources:
                    lease = db.execute("SELECT task,epoch FROM leases WHERE resource=?", (resource,)).fetchone()
                    if not lease or tuple(lease) != (task, epoch):
                        raise PolicyError("missing_resource_lease:" + resource)
                db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)", (operation_id, task, epoch, action.kind, json.dumps(action.args, sort_keys=True), "accepted", "{}", time.time(), None))
            cancel = threading.Event()
            self.active[operation_id] = (cancel, action, time.monotonic())
            self.store.event(task, "operation_dispatched", operation_id=operation_id, kind=action.kind)
            self.pool.submit(self._run, operation_id, task, action, cancel)
            return self.status(operation_id)

    def _run(self, op, task, action, cancel):
        try:
            result = ({"ok": False, "status": "cancelled", "executed": False} if cancel.is_set() else self.backend.run(action, cancel))
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise ValueError("invalid_driver_result")
            status = "completed" if result["ok"] else result.get("status", "failed")
            if status not in {"completed", "failed", "cancelled", "unknown"}:
                status = "failed"
        except Unavailable as exc:
            result = {"ok": False, "status": "failed", "executed": False, "error": str(exc)}
            status = "failed"
        except BaseException as exc:
            result = {"ok": False, "status": "unknown", "error": f"{type(exc).__name__}:{exc}", "executed": None}
            status = "unknown"  # Cannot infer physical outcome from a socket/process exception.
        with self.store.tx() as db:
            db.execute("UPDATE operations SET status=?,result=?,finished=? WHERE id=?", (status, json.dumps(result, ensure_ascii=False), time.time(), op))
        self.store.event(task, "operation_finished", operation_id=op, kind=action.kind, status=status, result=result)
        with self.lock:
            self.active.pop(op, None)

    def status(self, op):
        row = self.store.one("SELECT * FROM operations WHERE id=?", (op,))
        if row:
            row["result"] = json.loads(row["result"])
        return row

    def cancel(self, op):
        with self.lock:
            item = self.active.get(op)
            if item:
                item[0].set()
                return CAPABILITIES[item[1].kind].cancellable
        return False

    def close(self):
        self.pool.shutdown(wait=True)
