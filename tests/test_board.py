"""The board (SQLite), the state machines and how people refer to things."""

import pytest

from hq import policy, refs
from hq.board import Board


@pytest.fixture
def board(tmp_path):
    return Board(tmp_path / "board.db")


def test_refs_parse_and_name_things():
    assert refs.parse_ref("T-3") == ("T", 3) and refs.parse_ref("i5") == ("I", 5) and refs.parse_ref("4") == ("T", 4)
    assert refs.parse_ref("nudge") is None
    assert refs.split_title("Nudge: warns you before meetings") == ("Nudge", "warns you before meetings")
    assert refs.product_ref({"id": 7, "title": "Nudge: x", "slug": "nudge"}) == "Nudge"
    assert refs.product_ref({"id": 7, "title": "an idea", "slug": None}) == "I-7"


def test_only_legal_transitions_are_allowed(board):
    tid = board.create("Add export")
    board.transition(tid, policy.READY_FOR_DEV, "planner")
    with pytest.raises(policy.IllegalTransition):
        board.transition(tid, policy.SHIPPED, "engineer")           # QA first
    board.transition(tid, policy.BLOCKED, "engineer", "stuck")
    assert board.get(tid)["blocked_from"] == policy.READY_FOR_DEV  # remembered, to resume there
    assert [event["to_state"] for event in board.events(tid)] == [policy.APPROVED, policy.READY_FOR_DEV, policy.BLOCKED]


def test_dependencies_wait_for_shipped_tickets(board):
    first = board.create("First")
    second = board.create("Second", depends_on=[first])
    assert not board.deps_shipped(board.get(second))
    board.update(first, state=policy.SHIPPED)
    assert board.deps_shipped(board.get(second))


def test_products_are_found_by_name_number_or_idea_ref(board):
    approved = board.create_product("Nudge: warns you before meetings")
    board.update_product(approved, slug="nudge")
    idea = board.create_product("Something new")
    board.number_approved()
    assert board.resolve_ref("nudge") == ("product", approved) and board.resolve_ref("Nud") == ("product", approved)
    assert board.resolve_ref("P-1") == ("product", approved) and board.resolve_ref(f"I-{idea}") == ("product", idea)
    with pytest.raises(KeyError):
        board.resolve_ref("P-9")


def test_a_claim_is_held_until_released(board):
    tid = board.create("x")
    assert board.claim(tid, "worker-1", 60) and not board.claim(tid, "worker-2", 60)
    board.release(tid, "worker-1")
    assert board.claim(tid, "worker-2", 60)
    assert board.release_all() == 1


def test_a_late_release_leaves_the_next_claim_alone(board):
    tid = board.create("x")
    board.claim(tid, "worker-1", -1)                                       # its lease has already run out
    assert board.claim(tid, "worker-2", 60)
    board.release(tid, "worker-1")                                         # worker 1 finishes late
    assert board.get(tid)["claimed_by"] == "worker-2"


def test_a_move_and_its_history_land_together(board, monkeypatch):
    tid = board.create("x")
    def broken_insert(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(board, "_insert_event", broken_insert)
    with pytest.raises(RuntimeError):
        board.transition(tid, policy.READY_FOR_DEV, "planner")
    assert board.get(tid)["state"] == policy.APPROVED                     # not moved without its event


def test_a_name_in_any_script_finds_its_product(board):
    weather = board.create_product("天气: the weather")
    board.update_product(weather, slug="weather")
    assert board.resolve_ref("天气") == ("product", weather)


def test_a_new_column_is_added_to_an_existing_board(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    Board(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE products DROP COLUMN tickets_only")      # an older board without it
        conn.execute("DROP INDEX idx_messages_outbox")
        conn.execute("ALTER TABLE messages DROP COLUMN delivered")         # an indexed column: indexes come after
    Board(path)                                                            # opening it migrates
    with sqlite3.connect(path) as conn:
        assert "tickets_only" in {row[1] for row in conn.execute("PRAGMA table_info(products)")}
        assert "idx_messages_outbox" in {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
