"""Conservative, process-safe cloud request budget. Reservations survive restarts."""
import sqlite3
import time
from pathlib import Path


class CloudBudget:
    def __init__(self, path, limit=100):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.limit=min(100,int(limit))
    def reserve(self, kind):
        with sqlite3.connect(str(self.path),timeout=10) as db:
            db.execute('CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY,kind TEXT,ts REAL)')
            db.execute('BEGIN IMMEDIATE')
            used=db.execute('SELECT count(*) FROM calls').fetchone()[0]
            if used>=self.limit:raise RuntimeError('cloud_test_budget_exhausted')
            db.execute('INSERT INTO calls(kind,ts) VALUES(?,?)',(kind,time.time()))
            return used+1
    def status(self):
        if not self.path.exists():return {'used':0,'limit':self.limit}
        with sqlite3.connect(str(self.path)) as db:
            used=db.execute('SELECT count(*) FROM calls').fetchone()[0]
        return {'used':used,'limit':self.limit,'remaining':max(0,self.limit-used)}
