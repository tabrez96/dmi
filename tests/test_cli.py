from hq import cli, policy


def run(hq, monkeypatch, *argv):
    monkeypatch.setattr(cli, "HQ", lambda: hq)
    return cli.main(list(argv))


def test_new_files_a_ticket_and_board_lists_it(hq, monkeypatch, capsys):
    pid = hq.board.create_product("Nudge", "reminders")
    assert run(hq, monkeypatch, "new", hq.board.pref(pid), "Add export", "--desc", "CSV") == 0
    assert "created (approved)" in capsys.readouterr().out
    assert run(hq, monkeypatch, "board") == 0
    assert "Add export" in capsys.readouterr().out


def test_show_prints_a_ticket_and_its_thread(hq, monkeypatch, capsys):
    tid = hq.board.create("Add export", "CSV", policy.APPROVED, actor="pm")
    hq.board.post("ticket", tid, "pm", "Keep it small.")
    assert run(hq, monkeypatch, "show", str(tid)) == 0
    out = capsys.readouterr().out
    assert "Add export" in out and "Keep it small." in out


def test_an_unknown_ref_is_an_error_not_a_crash(hq, monkeypatch, capsys):
    assert run(hq, monkeypatch, "show", "T-999") == 1
    assert "error:" in capsys.readouterr().err


def test_digest_counts_tickets_and_lists_what_needs_you(hq, monkeypatch):
    hq.board.create("Add export", "", policy.APPROVED, actor="pm")
    text = cli.digest(hq)
    assert "Tickets: approved 1" in text and "Agent runs: 0" in text
