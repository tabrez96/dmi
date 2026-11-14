"""The activity log: one JSON line per thing that happens (run started/finished, VM, ticket state change,
message, proposal), sent to VictoriaLogs, plus a small read-only copy of the run and event tables for Grafana.

Everything here is best effort: a log that is down or slow must never slow down or break the pipeline."""

import http.client
import json
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Tables copied for Grafana. None = the whole table; a list = only those columns (no message text, titles or briefs).
SNAPSHOT_TABLES = {
    "runs": None,
    "events": ["id", "ticket_id", "ts", "actor", "kind", "from_state", "to_state"],      # not the note: feedback text
    "messages": ["id", "ts", "author", "recipient", "kind", "verdict", "round"],
    "tickets": ["id", "product_id", "state", "pr_number", "attempts", "qa_failures", "created_at", "updated_at"],
    "products": ["id", "state", "created_at", "updated_at"],
}


class Obs:
    def __init__(self, url, snapshot=None, board_path=None, interval=60):
        self.url = (url or "").rstrip("/")
        self.snapshot, self.board_path, self.interval = snapshot, board_path, interval
        self.pending = queue.Queue(maxsize=5000)
        self.sent = self.dropped = 0
        self._started = False

    def start(self):
        if self._started:
            return self
        self._started = True
        if self.url:
            threading.Thread(target=self._ship, daemon=True, name="obs-ship").start()
        if self.snapshot and self.board_path:
            threading.Thread(target=self._snap_loop, daemon=True, name="obs-snapshot").start()
        return self

    def emit(self, kind, **fields):
        """Queue one event. Never blocks, never raises."""
        if not self.url:
            return
        record = {"_time": time.time(), "_msg": kind, "kind": kind, "app": "dmi"}
        record.update({name: value for name, value in fields.items() if value is not None})
        try:
            self.pending.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def _ship(self):
        endpoint = (f"{self.url}/insert/jsonline?_stream_fields=app,kind&_msg_field=_msg&_time_field=_time")
        while True:
            batch = [self.pending.get()]
            time.sleep(0.5)                      # let a burst collect into one request
            while len(batch) < 500:
                try:
                    batch.append(self.pending.get_nowait())
                except queue.Empty:
                    break
            data = "\n".join(json.dumps(record, default=str) for record in batch).encode()
            try:
                urllib.request.urlopen(urllib.request.Request(endpoint, data=data, method="POST",
                                                                 headers={"Content-Type": "application/stream+json"}), timeout=10).read()
                self.sent += len(batch)
            except (urllib.error.URLError, http.client.HTTPException, OSError):
                self.dropped += len(batch)        # the log is down: drop, don't pile up
                time.sleep(5)

    def _snap_loop(self):
        while True:
            try:
                write_snapshot(self.board_path, self.snapshot)
            except (sqlite3.Error, OSError):
                pass                              # the next round tries again
            time.sleep(self.interval)


def write_snapshot(board_path, dest):
    """Copy the runs and events tables into a separate file (atomically), so Grafana never opens the live board."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{board_path}?mode=ro", uri=True, timeout=10)
    copy = sqlite3.connect(tmp)
    try:
        source.execute("BEGIN")                  # one read of the board: a ticket's state and its history agree
        for table, keep in SNAPSHOT_TABLES.items():
            ddl = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            columns = [column[1] for column in source.execute(f"PRAGMA table_info({table})")]
            if keep:
                columns = [column for column in columns if column in keep]
                copy.execute(f"CREATE TABLE {table} ({','.join(columns)})")
            else:
                copy.execute(ddl[0])
            copy.executemany(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' * len(columns))})",
                             source.execute(f"SELECT {','.join(columns)} FROM {table}"))
        copy.commit()
    finally:
        source.close()
        copy.close()
    tmp.chmod(0o644)
    tmp.replace(dest)


def backfill(board_path, obs):
    """Send the history already on the board (runs, ticket events, messages, proposals) once, with the live fields.
    `obs` must be started with a log URL: only its shipper drains the queue, so a full one would wait forever."""
    if not (obs.url and obs._started):
        raise RuntimeError("start the activity log (with a VictoriaLogs URL) before replaying history into it")
    source = sqlite3.connect(f"file:{board_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    count = 0

    def put(when, kind, **fields):
        nonlocal count
        obs.pending.put({"_time": when, "_msg": kind, "kind": kind, "app": "dmi", "backfill": 1, **fields})
        count += 1
    for row in source.execute("SELECT * FROM runs ORDER BY id"):
        run = {"run": row["id"], "role": row["role"], "model": row["model"], "subject": row["subject"],
               "subject_id": row["subject_id"]}
        put(row["started"], "run_start", **run, harness=row["harness"], log_path=row["log_path"])
        if row["ended"]:
            put(row["ended"], "run_end", **run, ok=row["ok"], cost_usd=row["cost_usd"],
                duration_s=round(row["ended"] - row["started"], 1))
    for row in source.execute("SELECT * FROM events ORDER BY id"):
        put(row["ts"], "ticket_event", ticket=row["ticket_id"], actor=row["actor"], event=row["kind"],
            from_state=row["from_state"], to_state=row["to_state"], note=row["note"])
    for row in source.execute("SELECT * FROM messages ORDER BY id"):
        put(row["ts"], "message", subject=row["subject"], subject_id=row["subject_id"], author=row["author"],
            recipient=row["recipient"], message_kind=row["kind"], verdict=row["verdict"], body=row["body"])
    for row in source.execute("SELECT * FROM proposals ORDER BY id"):
        put(row["ts"], "proposal", author=row["author"], proposal_kind=row["kind"], payload=row["payload"])
    source.close()
    return count
