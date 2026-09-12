from __future__ import annotations

import time

from .contracts import Action
from .execution import Unavailable


class SpeechOutbox:
    def __init__(self, store, executor):
        self.store, self.executor = store, executor

    def enqueue(self, task, epoch, key, text):
        with self.store.tx() as db:
            db.execute("INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?)", (key, task, epoch, text, "pending"))
        self.store.event(task, "speech_enqueued", key=key)

    def recover(self):
        # Repair historical running rows from durable operation results, never
        # replay audio or release a resource whose physical result is unknown.
        for row in self.store.all("SELECT id FROM tasks WHERE id LIKE 'voice:%' AND status NOT IN ('completed','failed','cancelled','blocked','unknown')"):
            key=row['id'][len('voice:'):]
            op=self.executor.status('speech:'+key)
            status=op['status'] if op and op['status'] in {'completed','failed','cancelled'} else 'unknown'
            with self.store.tx() as db:
                db.execute('UPDATE tasks SET status=? WHERE id=?',(status,row['id']))
                db.execute('UPDATE outbox SET status=? WHERE id=?',(status,key))
            if status != 'unknown':
                self.store.release(row['id'])
            self.store.event(row['id'],'speech_recovered',status=status,operation_id=op['id'] if op else None)

    def invalidate(self, task):
        with self.store.tx() as db:
            db.execute("UPDATE outbox SET status='cancelled' WHERE task=? AND status='pending'", (task,))
        for row in self.store.all("SELECT id FROM outbox WHERE task=? AND status='playing'", (task,)):
            self.executor.cancel("speech:" + row["id"])

    def tick(self):
        for row in self.store.all("SELECT * FROM outbox WHERE status='playing'"):
            op = self.executor.status("speech:" + row["id"])
            if op and op["status"] != "accepted":
                with self.store.tx() as db:
                    db.execute("UPDATE outbox SET status=? WHERE id=?", (op["status"], row["id"]))
                    db.execute("UPDATE tasks SET status=? WHERE id=?", (op["status"], "voice:" + row["id"]))
                if op['status'] != 'unknown':
                    self.store.release("voice:" + row["id"])
        for row in self.store.all("SELECT * FROM outbox WHERE status='pending' ORDER BY rowid LIMIT 16"):
            owner = self.store.one("SELECT * FROM tasks WHERE id=?", (row["task"],))
            if not owner or owner["epoch"] != row["epoch"] or owner["status"] in {"cancelled", "failed", "unknown", "blocked"} or owner["command"] in {"cancel", "pause"}:
                with self.store.tx() as db:
                    db.execute("UPDATE outbox SET status='cancelled' WHERE id=?", (row["id"],))
                continue
            voice_task = "voice:" + row["id"]
            if not self.store.claim(voice_task, 0, ["speaker"]):
                break
            with self.store.tx() as db:
                db.execute("INSERT OR IGNORE INTO tasks(id,session,request_key,plan,status,created) VALUES(?,?,?,?,?,?)", (voice_task, owner["session"], voice_task, "{}", "running", time.time()))
                db.execute("UPDATE outbox SET status='playing' WHERE id=?", (row["id"],))
            try:
                self.executor.issue(voice_task, 0, "speech:" + row["id"], Action("speech.say", {"text": row["text"]}))
            except (Unavailable, ValueError) as exc:
                with self.store.tx() as db:
                    db.execute("UPDATE outbox SET status='failed' WHERE id=?", (row["id"],))
                    db.execute("UPDATE tasks SET status='failed' WHERE id=?", (voice_task,))
                self.store.release(voice_task)
                self.store.event(row["task"], "speech_failed", error=str(exc))
