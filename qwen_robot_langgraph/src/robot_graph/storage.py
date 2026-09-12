from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
import threading
import time


class Store:
    """Atomic action ledger, leases, outbox and user memory; separate from checkpoints."""
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript('''
        PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,task TEXT,epoch INTEGER,kind TEXT,args TEXT,status TEXT,result TEXT,started REAL,finished REAL);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,session TEXT,request_key TEXT UNIQUE,plan TEXT,status TEXT,command TEXT DEFAULT '',epoch INTEGER DEFAULT 0,created REAL);
        CREATE TABLE IF NOT EXISTS leases(resource TEXT PRIMARY KEY,task TEXT,epoch INTEGER);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,task TEXT,type TEXT,data TEXT,ts REAL,mono REAL);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,task TEXT,epoch INTEGER,text TEXT,status TEXT);
        CREATE TABLE IF NOT EXISTS memory(id TEXT PRIMARY KEY,session TEXT,text TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY,session TEXT,text TEXT,reply TEXT,task TEXT,created REAL);
        ''')

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def one(self, sql, args=()):
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.lock:
            return [dict(x) for x in self.db.execute(sql, args)]

    def event(self, task, event_type, **data):
        with self.tx() as db:
            db.execute("INSERT INTO events(task,type,data,ts,mono) VALUES(?,?,?,?,?)", (task, event_type, json.dumps(data, ensure_ascii=False), time.time(), time.monotonic()))

    def claim(self, task, epoch, resources):
        with self.tx() as db:
            for resource in sorted(set(resources)):
                row = db.execute("SELECT task,epoch FROM leases WHERE resource=?", (resource,)).fetchone()
                if row and (row[0], row[1]) != (task, epoch):
                    return False
            for resource in sorted(set(resources)):
                db.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?)", (resource, task, epoch))
        return True

    def release(self, task):
        with self.tx() as db:
            db.execute("DELETE FROM leases WHERE task=?", (task,))

    def close(self):
        self.db.close()
