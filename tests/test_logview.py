from datetime import UTC, datetime

from hq import logview


def test_filters_become_logsql():
    query = logview.build_query({"kind": "run_end,bogus", "role": "designer", "model": "a,b", "ref": "t-52", "q": 'say "hi"', "bad": "1"})
    assert query.startswith('app:dmi kind:in("run_end") (role:in("designer") OR author:in("designer") '
                            'OR actor:in("designer")) model:in("a","b")')                # messages and events too
    assert 'ticket:="52"' in query and '^T-52 ' in query
    assert '*:"say \\"hi\\""' in query
    assert 'ok:="0"' in query and 'status:~"^([45]|error)"' in query                 # a gate "error ..." is trouble too
    assert logview.build_query({}) == "app:dmi"
    assert 'subject_id:="5"' in logview.build_query({"ref": "I-5"})                 # an idea: its id
    assert 'subject_id:="17"' in logview.build_query({"ref": "P-5"}, lambda ref: ("product", 17))  # a product: its number
    assert "subject_id" not in logview.build_query({"ref": "P-5"})                   # no board to ask: the label only
    assert 'event:="run_failed"' in logview.build_query({"bad": "1"})
    assert 'exit_code:~"^-?[1-9]"' in logview.build_query({"bad": "1"})              # a killed VM exits -9


def test_window_defaults_and_custom_range():
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    start, end = logview.window({"range": "6h"}, now)
    assert (end - start).total_seconds() == 6 * 3600 and end == now
    start, end = logview.window({"from": "2026-11-05T10:00:00Z", "to": "2026-11-05T11:00:00Z"}, now)
    assert start.hour == 10 and end.hour == 11
    assert (now - logview.window({"range": "nonsense"}, now)[0]).total_seconds() == 3600


def test_records_become_readable_rows():
    run = logview.summarise({"kind": "run_end", "role": "designer", "subject": "product", "subject_id": "5",
                             "ok": "1", "duration_s": "143.6", "cost_usd": "0.334", "model": "claude-sonnet-5-5"})
    assert run["what"] == "finished in 143.6s, $0.33" and run["outcome"] == "ok" and run["ref"] == "product 5"
    event = logview.summarise({"kind": "ticket_event", "ticket": "52", "actor": "qa", "event": "transition",
                            "from_state": "in_qa", "to_state": "ready_for_dev"})
    assert event["what"] == "in_qa → ready_for_dev" and event["ref"] == "T-52" and event["who"] == "qa"
    call = logview.summarise({"kind": "api_call", "role": "engineer", "ref": "T-52", "method": "POST",
                              "target": "anthropic/v1/messages", "status": "200"})
    assert call["what"] == "POST anthropic/v1/messages" and call["outcome"] == "200"
    assert logview.summarise({"kind": "run_end", "subject": "chat", "ok": "1"})["ref"] == "chat"    # no subject_id
    msg = logview.summarise({"kind": "message", "author": "hq", "recipient": "pm", "body": "a\n b", "message_kind": "gate"})
    assert msg["what"] == "→ pm: a b" and msg["outcome"] == "gate"


def test_the_gate_feeds_every_call_to_the_log():
    from hq.gate import Gate
    seen = []
    gate = Gate("127.0.0.1", 0)
    gate.sink = lambda kind, **fields: seen.append((kind, fields))
    gate.log("T-52 engineer", "POST", "anthropic/v1/messages?x=1", 200, "anthropic")
    gate.log("P-5 designer", "CONNECT", "pypi.org", 200, "registry")
    assert seen[0][0] == "api_call" and seen[0][1]["role"] == "engineer" and seen[0][1]["ref"] == "T-52"
    assert seen[0][1]["target"] == "anthropic/v1/messages" and seen[0][1]["provider"] == "anthropic"
    assert seen[1][1]["method"] == "CONNECT" and seen[1][1]["provider"] == "registry"


def test_page_script_is_valid_javascript(tmp_path):
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        return
    script = re.search(r"<script>(.*?)</script>", logview.page(), re.DOTALL).group(1)
    script_file = tmp_path / "logs.js"
    script_file.write_text(script)
    checked = subprocess.run([node, "--check", str(script_file)], capture_output=True, text=True, check=False)
    assert checked.returncode == 0, checked.stderr
