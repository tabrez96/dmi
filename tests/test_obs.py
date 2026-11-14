"""The activity log: events are queued without ever blocking, and Grafana gets a copy of the board without the text."""

import sqlite3

import pytest

from hq import obs


def test_events_are_queued_only_with_a_log_and_dropped_when_it_is_full():
    silent = obs.Obs("")
    silent.emit("run_start", role="engineer")
    assert silent.pending.empty()
    log = obs.Obs("http://logs.test/")
    log.emit("run_end", role="qa", ok=1, cost_usd=None)
    record = log.pending.get_nowait()
    assert record["kind"] == record["_msg"] == "run_end" and record["app"] == "dmi"
    assert record["role"] == "qa" and "cost_usd" not in record          # empty fields are left out
    log.pending.maxsize = 1
    log.emit("message")
    log.emit("message")                                                 # full: dropped, never blocks
    assert log.pending.qsize() == 1 and log.dropped == 1


def test_the_snapshot_keeps_only_the_safe_columns(tmp_path):
    board = tmp_path / "board.db"
    with sqlite3.connect(board) as conn:
        conn.execute("CREATE TABLE runs (id INTEGER, cost_usd REAL)")
        conn.execute("CREATE TABLE events (id INTEGER, ticket_id INTEGER, ts REAL, actor TEXT, kind TEXT, "
                     "from_state TEXT, to_state TEXT, note TEXT)")
        conn.execute("INSERT INTO events VALUES (1, 52, 0, 'pm', 'feedback', NULL, NULL, 'private feedback')")
        conn.execute("CREATE TABLE messages (id INTEGER, ts REAL, author TEXT, recipient TEXT, kind TEXT, verdict TEXT, "
                     "round INTEGER, body TEXT)")
        conn.execute("CREATE TABLE tickets (id INTEGER, product_id INTEGER, state TEXT, pr_number INTEGER, attempts "
                     "INTEGER, qa_failures INTEGER, created_at REAL, updated_at REAL, title TEXT)")
        conn.execute("CREATE TABLE products (id INTEGER, state TEXT, created_at REAL, updated_at REAL, brief TEXT)")
        conn.execute("INSERT INTO messages VALUES (1, 0, 'jim', 'pm', 'say', NULL, 1, 'private words')")
        conn.execute("INSERT INTO runs VALUES (1, 0.25)")
    snapshot = tmp_path / "out" / "snapshot.db"
    obs.write_snapshot(board, snapshot)
    with sqlite3.connect(snapshot) as conn:
        assert "body" not in [column[1] for column in conn.execute("PRAGMA table_info(messages)")]
        assert "title" not in [column[1] for column in conn.execute("PRAGMA table_info(tickets)")]
        assert conn.execute("SELECT * FROM events").fetchone() == (1, 52, 0.0, "pm", "feedback", None, None)
        assert conn.execute("SELECT author FROM messages").fetchone() == ("jim",)
        assert conn.execute("SELECT cost_usd FROM runs").fetchone() == (0.25,)


def test_the_history_is_replayed_with_the_live_fields(tmp_path):
    board = tmp_path / "board.db"
    with sqlite3.connect(board) as conn:
        conn.execute("CREATE TABLE runs (id, role, model, harness, subject, subject_id, log_path, started, ended, ok, "
                     "cost_usd)")
        conn.execute("CREATE TABLE events (id, ticket_id, ts, actor, kind, from_state, to_state, note)")
        conn.execute("CREATE TABLE messages (id, ts, subject, subject_id, author, recipient, kind, verdict, body)")
        conn.execute("CREATE TABLE proposals (id, ts, author, kind, payload)")
        conn.execute("INSERT INTO runs VALUES (1, 'qa', 'm', 'pi', 'ticket', 52, 'x.log', 10, 25, 1, 0.5)")
        conn.execute("INSERT INTO proposals VALUES (1, 30, 'jim', 'ticket', '{}')")
    log = obs.Obs("http://logs.test")
    with pytest.raises(RuntimeError):                                   # nothing drains the queue yet
        obs.backfill(board, log)
    log._ship = lambda: None                                            # started, but this test reads the queue itself
    assert obs.backfill(board, log.start()) == 3
    records = [log.pending.get_nowait() for _ in range(3)]
    assert [record["kind"] for record in records] == ["run_start", "run_end", "proposal"]
    assert records[1]["subject"] == "ticket" and records[1]["subject_id"] == 52      # the finish keeps its ticket
    assert records[2]["proposal_kind"] == "ticket" and records[2]["author"] == "jim"
