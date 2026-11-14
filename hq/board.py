"""The board: products, tickets, their conversations, and agent runs (SQLite).

`messages` is both the conversation transcript agents read and the outbox the
Discord bot delivers from.
"""

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import policy, refs

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id           INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    slug         TEXT UNIQUE,              -- set when the design is approved; repo = products/<slug>
    focus        TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL,
    next_speaker TEXT,
    round        INTEGER NOT NULL DEFAULT 0,
    max_rounds   INTEGER,
    brief        TEXT,
    ux           TEXT,
    design       TEXT,
    tickets_json TEXT,
    escalations  INTEGER NOT NULL DEFAULT 0,  -- times the manager stepped in
    attempts     INTEGER NOT NULL DEFAULT 0,
    blocked_from TEXT,
    tickets_only INTEGER NOT NULL DEFAULT 0,   -- 1: the architect is revising the drafted tickets the PM commented on
    thread_id    TEXT,
    number       INTEGER,                    -- P-<number>, given when the product is approved (ideas have none)
    channel_id   TEXT,                       -- the product's own Discord channel
    github_repo  TEXT,                       -- owner/name of the product's GitHub repo
    setup_prs    TEXT NOT NULL DEFAULT '',   -- JSON: the README / design pull requests a new repo starts with
    ci_cursor    INTEGER NOT NULL DEFAULT 0, -- id of the last failed develop build HQ has acted on
    canvas_url   TEXT,                       -- the Claude Design canvas (artifact) PM reviews the screens in
    checklist_msg_id TEXT,                   -- the "what's next" message in that channel (edited as stages complete)
    claimed_by   TEXT,
    lease_until  REAL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id             INTEGER PRIMARY KEY,
    product_id     INTEGER,
    title          TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL,
    depends_on     TEXT NOT NULL DEFAULT '[]',
    ui             INTEGER NOT NULL DEFAULT 0,  -- 1 = designer reviews it after QA
    builder        TEXT NOT NULL DEFAULT '',    -- 'designer' = design work (logo, screens): Pam builds it, no plan or QA; '' = the engineer
    asked          TEXT,                        -- who the engineer's open question is for
    plan           TEXT,
    qa_report      TEXT,
    qa_failures    INTEGER NOT NULL DEFAULT 0,
    clarify_rounds INTEGER NOT NULL DEFAULT 0,
    escalations    INTEGER NOT NULL DEFAULT 0,
    attempts       INTEGER NOT NULL DEFAULT 0,
    blocked_from   TEXT,
    branch         TEXT,
    issue_number   INTEGER,                     -- the GitHub issue mirroring this ticket
    issue_url      TEXT,
    pr_number      INTEGER,                     -- the GitHub pull request for this ticket
    pr_url         TEXT,
    design_pins    TEXT,                        -- JSON {screen ID: design commit} the ticket was planned against
    owner_cursor   TEXT NOT NULL DEFAULT '',     -- time of the last of the owner's PR comments HQ has read
    review_cursor  INTEGER NOT NULL DEFAULT 0,  -- id of the last CodeRabbit review HQ has looked at
    kody_cursor    TEXT NOT NULL DEFAULT '',     -- time of the last of Kody's comments HQ has read
    ci_run         INTEGER NOT NULL DEFAULT 0,  -- the last failed PR build already sent back to the engineer
    review_rounds  INTEGER NOT NULL DEFAULT 0,  -- times the reviewers' comments (CodeRabbit, Kody) sent the ticket back for fixes
    pr_feedback    TEXT,                        -- JSON: reviewer comments (CodeRabbit, Kody, the owner) waiting for the planner's triage
    worktree       TEXT,
    thread_id      TEXT,
    created_by     TEXT NOT NULL,
    claimed_by     TEXT,
    lease_until    REAL,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY,
    subject    TEXT NOT NULL,             -- 'product' | 'ticket'
    subject_id INTEGER NOT NULL,
    ts         REAL NOT NULL,
    author     TEXT NOT NULL,             -- a role, 'pm' or 'hq'
    recipient  TEXT,
    kind       TEXT NOT NULL DEFAULT 'say',  -- say | gate | escalation | status
    verdict    TEXT,
    body       TEXT NOT NULL,
    actions    TEXT,                      -- JSON list of buttons for the PM
    files      TEXT,                      -- JSON list of local file paths to attach
    round      INTEGER,
    delivered  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ticket_id  INTEGER NOT NULL,
    ts         REAL NOT NULL,
    actor      TEXT NOT NULL,
    kind       TEXT NOT NULL,             -- created | transition | note | feedback | run_failed
    from_state TEXT,
    to_state   TEXT,
    note       TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY,
    subject   TEXT,
    subject_id INTEGER,
    role      TEXT NOT NULL,
    harness   TEXT NOT NULL,
    model     TEXT NOT NULL,
    started   REAL NOT NULL,
    ended     REAL,
    ok        INTEGER,
    cost_usd  REAL,
    log_path  TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id         INTEGER PRIMARY KEY,           -- work an agent suggested in chat; the PM confirms
    ts         REAL NOT NULL,
    author     TEXT NOT NULL,                 -- team member key
    kind       TEXT NOT NULL,                 -- ticket | discover
    payload    TEXT NOT NULL,                 -- JSON
    status     TEXT NOT NULL DEFAULT 'open'   -- open | accepted | dismissed
);
"""

# Run after _migrate: an older board gains the indexed columns there first.
INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_products_slug ON products(slug);   -- a migrated slug column has no UNIQUE
CREATE INDEX IF NOT EXISTS idx_tickets_state ON tickets(state);
CREATE INDEX IF NOT EXISTS idx_products_state ON products(state);
CREATE INDEX IF NOT EXISTS idx_messages_subject ON messages(subject, subject_id);
CREATE INDEX IF NOT EXISTS idx_messages_outbox ON messages(delivered);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id);
"""

TABLES = {"ticket": "tickets", "product": "products"}


class Board:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.executescript(INDEXES)

    @staticmethod
    def _migrate(conn):
        """Add columns that newer versions of SCHEMA define to an existing board."""
        ref = sqlite3.connect(":memory:")
        ref.executescript(SCHEMA)
        for (table,) in ref.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for _, col, typ, notnull, default, _ in ref.execute(f"PRAGMA table_info({table})"):
                if col not in have:
                    ddl = f"ALTER TABLE {table} ADD COLUMN {col} {typ}"
                    if default is not None:
                        ddl += f" DEFAULT {default}"
                    if notnull and default is not None:
                        ddl += " NOT NULL"
                    conn.execute(ddl)
        ref.close()

    @contextmanager
    def _conn(self):
        # Autocommit; a single statement is atomic. Writes that belong together use _transaction.
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self):
        """One connection whose writes all land or none do (a state change and its history event)."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _get(self, kind, rid):
        with self._conn() as conn:
            row = conn.execute(f"SELECT * FROM {TABLES[kind]} WHERE id = ?", (rid,)).fetchone()
        if row is None:
            raise KeyError(f"no {kind} {'T' if kind == 'ticket' else 'id '}{rid}")
        return dict(row)

    def _list(self, kind, states=None, **where):
        query, args, conds = f"SELECT * FROM {TABLES[kind]}", [], []
        if states:
            conds.append(f"state IN ({','.join('?' * len(states))})")
            args += list(states)
        for column, value in where.items():
            conds.append(f"{column} = ?")
            args.append(value)
        if conds:
            query += " WHERE " + " AND ".join(conds)
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(query + " ORDER BY id", args)]

    def _update(self, kind, rid, **fields):
        if not fields:
            return
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{column} = ?" for column in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE {TABLES[kind]} SET {cols} WHERE id = ?", (*fields.values(), rid))

    # -- tickets --------------------------------------------------------------

    def create(self, title, description="", state=policy.APPROVED, actor="pm",
               product_id=None, depends_on=(), ui=False, builder=""):
        now = time.time()
        with self._transaction() as conn:
            tid = conn.execute(
                "INSERT INTO tickets (product_id, title, description, state, depends_on, ui, builder,"
                " created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (product_id, title, description, state, json.dumps(list(depends_on)), int(ui), builder,
                 actor, now, now),
            ).lastrowid
            self._insert_event(conn, tid, actor, "created", to_state=state)
        self._emit("ticket_event", ticket=tid, actor=actor, event="created", from_state=None, to_state=state, note=None)
        return tid

    def get(self, tid):
        return self._get("ticket", tid)

    def list(self, states=None, **where):
        return self._list("ticket", states, **where)

    def update(self, tid, **fields):
        self._update("ticket", tid, **fields)

    def transition(self, tid, to_state, actor, note=None, **fields):
        with self._transaction() as conn:     # the check, the move and its history event: all or nothing
            row = conn.execute("SELECT state FROM tickets WHERE id = ?", (tid,)).fetchone()
            if row is None:
                raise KeyError(f"no ticket T{tid}")
            from_state = row["state"]
            policy.check(from_state, to_state)
            if to_state == policy.BLOCKED:
                fields.setdefault("blocked_from", from_state)
            fields.update(state=to_state, attempts=0, updated_at=time.time())
            cols = ", ".join(f"{column} = ?" for column in fields)
            conn.execute(f"UPDATE tickets SET {cols} WHERE id = ?", (*fields.values(), tid))
            self._insert_event(conn, tid, actor, "transition", from_state, to_state, note)
        self._emit("ticket_event", ticket=tid, actor=actor, event="transition", from_state=from_state,
                   to_state=to_state, note=note)

    def deps_shipped(self, ticket):
        deps = json.loads(ticket["depends_on"] or "[]")
        return all(self.get(dep)["state"] == policy.SHIPPED for dep in deps)

    # -- products -------------------------------------------------------------

    def create_product(self, title, focus="", next_speaker="scout", max_rounds=None):
        now = time.time()
        with self._conn() as conn:
            return conn.execute(
                "INSERT INTO products (title, focus, state, next_speaker, max_rounds,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, focus, policy.DISCOVERING, next_speaker, max_rounds, now, now),
            ).lastrowid

    def product(self, pid):
        return self._get("product", pid)

    def products(self, states=None, **where):
        return self._list("product", states, **where)

    # -- references: T-3, P-2 (approved product), I-5 (idea) --------------------------------

    def next_number(self):
        with self._conn() as conn:
            return (conn.execute("SELECT MAX(number) FROM products").fetchone()[0] or 0) + 1

    def number_approved(self):
        """Give every approved product (one with a repo) its number, oldest first. Idempotent."""
        for product in self.products():
            if product["slug"] and not product["number"]:
                self.update_product(product["id"], number=self.next_number())

    def pref(self, pid):
        return refs.product_ref(self.product(pid))

    def ref(self, kind, rid):
        return f"T-{rid}" if kind == "ticket" else self.pref(rid)

    def resolve_ref(self, text):
        """'P-2' / 'I-5' / 'T-3' -> ('product'|'ticket', internal id); KeyError if it names nothing."""
        parsed = refs.parse_ref(text)
        if not parsed:
            return "product", self._product_by_name(text)
        letter, number = parsed
        if letter == "T":
            self.get(number)
            return "ticket", number
        with self._conn() as conn:
            row = conn.execute("SELECT id FROM products WHERE number = ?" if letter == "P" else
                            "SELECT id FROM products WHERE id = ? AND number IS NULL", (number,)).fetchone()
        if row is None:
            raise KeyError(f"no {'product' if letter == 'P' else 'idea'} {letter}-{number}")
        return "product", row["id"]

    def _product_by_name(self, text):
        """A product by its name or directory ('Nudge', 'nudge', 'airgap-docs', 'AirGap Docs'); a unique prefix works."""
        def key(name):          # letters and digits in any script ('天气' too), case and punctuation ignored
            return re.sub(r"[\W_]+", "", (name or "").casefold())
        want = key(text)
        if not want:
            raise KeyError(f"can't tell what {text!r} refers to")
        named = [(product["id"], key(refs.split_title(product["title"])[0]), key(product["slug"]))
                 for product in self.products() if product["slug"]]
        exact = [pid for pid, name, slug in named if want in (name, slug)]
        hits = exact or [pid for pid, name, slug in named if name.startswith(want) or slug.startswith(want)]
        if len(hits) != 1:
            raise KeyError(f"no product called {text!r}" if not hits else f"{text!r} matches more than one product")
        return hits[0]

    def update_product(self, pid, **fields):
        self._update("product", pid, **fields)

    def transition_product(self, pid, to_state, **fields):
        product = self.product(pid)
        policy.check(product["state"], to_state, product=True)
        if to_state == policy.P_BLOCKED:
            fields.setdefault("blocked_from", product["state"])
        self.update_product(pid, state=to_state, attempts=0, **fields)

    # -- claiming: one agent per ticket/product at a time ------------------------

    def claim(self, rid, who, lease_seconds, kind="ticket"):
        now = time.time()
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE {TABLES[kind]} SET claimed_by = ?, lease_until = ?"
                " WHERE id = ? AND (claimed_by IS NULL OR lease_until < ?)",
                (who, now + lease_seconds, rid, now),
            )
            return cur.rowcount == 1

    def release_all(self):
        """Free every claim. Only safe when no agent is running, i.e. at HQ startup."""
        with self._conn() as conn:
            freed = 0
            for table in TABLES.values():
                freed += conn.execute(f"UPDATE {table} SET claimed_by = NULL, lease_until = NULL"
                               " WHERE claimed_by IS NOT NULL").rowcount
            return freed

    def release(self, rid, who, kind="ticket"):
        """Free `who`'s claim. If their lease ran out and someone else claimed it since, that claim stays."""
        with self._conn() as conn:
            conn.execute(f"UPDATE {TABLES[kind]} SET claimed_by = NULL, lease_until = NULL"
                         " WHERE id = ? AND claimed_by = ?", (rid, who))

    # -- conversation messages (and Discord outbox) ------------------------------

    def post(self, subject, subject_id, author, body, recipient=None, kind="say",
             verdict=None, actions=None, round=None, delivered=False, files=None):
        self._emit("message", subject=subject, subject_id=subject_id, author=author, recipient=recipient,
                   message_kind=kind, verdict=verdict, body=body)
        with self._conn() as conn:
            return conn.execute(
                "INSERT INTO messages (subject, subject_id, ts, author, recipient, kind, verdict,"
                " body, actions, round, delivered, files) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (subject, subject_id, time.time(), author, recipient, kind, verdict, body,
                 json.dumps(actions) if actions else None, round, int(delivered),
                 json.dumps([str(f) for f in files]) if files else None),
            ).lastrowid

    def messages(self, subject, subject_id):
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM messages WHERE subject = ? AND subject_id = ? ORDER BY id",
                (subject, subject_id))]

    def outbox(self, limit=20):
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM messages WHERE delivered = 0 ORDER BY id LIMIT ?", (limit,))]

    def mark_delivered(self, mid):
        with self._conn() as conn:
            conn.execute("UPDATE messages SET delivered = 1 WHERE id = ?", (mid,))

    def by_thread(self, thread_id):
        for kind, table in TABLES.items():
            with self._conn() as conn:
                row = conn.execute(f"SELECT id FROM {table} WHERE thread_id = ?", (str(thread_id),)).fetchone()
            if row:
                return kind, row["id"]
        return None

    # -- ticket history ------------------------------------------------------------

    def event(self, tid, actor, kind, from_state=None, to_state=None, note=None):
        self._emit("ticket_event", ticket=tid, actor=actor, event=kind, from_state=from_state, to_state=to_state,
                   note=note)
        with self._conn() as conn:
            self._insert_event(conn, tid, actor, kind, from_state, to_state, note)

    @staticmethod
    def _insert_event(conn, tid, actor, kind, from_state=None, to_state=None, note=None):
        conn.execute("INSERT INTO events (ticket_id, ts, actor, kind, from_state, to_state, note)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (tid, time.time(), actor, kind, from_state, to_state, note))

    def events(self, tid):
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM events WHERE ticket_id = ? ORDER BY id", (tid,))]

    # -- runs (cost + budget tracking) -----------------------------------------------

    sink = None      # optional callable(kind, **fields): the activity log (hq/obs.py)

    def _emit(self, kind, **fields):
        if self.sink:
            try:
                self.sink(kind, **fields)
            except Exception:  # noqa: BLE001, S110 - the activity log must never break the board
                pass

    def start_run(self, subject, subject_id, role, harness, model, log_path):
        run_id = self._start_run(subject, subject_id, role, harness, model, log_path)
        self._emit("run_start", run=run_id, role=role, model=model, harness=harness, subject=subject,
                   subject_id=subject_id, log_path=str(log_path), running=self._running())
        return run_id

    def _running(self):
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM runs WHERE ended IS NULL").fetchone()[0]

    def _start_run(self, subject, subject_id, role, harness, model, log_path):
        with self._conn() as conn:
            return conn.execute(
                "INSERT INTO runs (subject, subject_id, role, harness, model, started, log_path)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (subject, subject_id, role, harness, model, time.time(), str(log_path)),
            ).lastrowid

    def finish_run(self, run_id, ok, cost_usd=None):
        now = time.time()
        with self._conn() as conn:
            conn.execute("UPDATE runs SET ended = ?, ok = ?, cost_usd = ? WHERE id = ?",
                      (now, int(ok), cost_usd, run_id))
            row = conn.execute("SELECT role, model, started, subject, subject_id FROM runs WHERE id = ?",
                               (run_id,)).fetchone()
        if row:
            self._emit("run_end", run=run_id, role=row["role"], model=row["model"], ok=int(ok), cost_usd=cost_usd,
                       duration_s=round(now - row["started"], 1), running=self._running(), subject=row["subject"],
                       subject_id=row["subject_id"])

    def close_orphan_runs(self):
        """Runs still open when HQ starts belong to a previous HQ that crashed or was stopped: close them as failed."""
        now = time.time()
        with self._conn() as conn:
            return conn.execute("UPDATE runs SET ended = ?, ok = 0 WHERE ended IS NULL", (now,)).rowcount

    def runs_since(self, since_ts, role=None, chat=None):
        """Runs since a time; chat=True/False keeps only chat replies / only pipeline work."""
        query, args = "SELECT * FROM runs WHERE started >= ?", [since_ts]
        if role:
            query += " AND role = ?"
            args.append(role)
        if chat is True:
            query += " AND subject = 'chat'"
        elif chat is False:
            query += " AND (subject IS NULL OR subject != 'chat')"
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(query + " ORDER BY id", args)]

    def propose(self, author, kind, payload):
        self._emit("proposal", author=author, proposal_kind=kind, payload=json.dumps(payload, default=str))
        with self._conn() as conn:
            return conn.execute("INSERT INTO proposals (ts, author, kind, payload) VALUES (?, ?, ?, ?)",
                             (time.time(), author, kind, json.dumps(payload))).lastrowid

    def proposal(self, pid):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        if row is None:
            raise KeyError(f"no proposal {pid}")
        return {**dict(row), "payload": json.loads(row["payload"])}

    def reopen_proposal(self, pid):
        with self._conn() as conn:
            conn.execute("UPDATE proposals SET status = 'open' WHERE id = ?", (pid,))

    def close_proposal(self, pid, status):
        with self._conn() as conn:
            return conn.execute("UPDATE proposals SET status = ? WHERE id = ? AND status = 'open'",
                             (status, pid)).rowcount == 1

    def last_product(self):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM products ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
