"""End-to-end pipeline tests using the fake harness (no models called)."""

import json
import shutil

import pytest
from conftest import ROOT, drain, roles, scratch_product, thread

from hq import gitops, policy
from hq import orchestrator as hq_module
from hq.orchestrator import HQ
from hq.refs import parse_ref

# -- tickets --------------------------------------------------------------------

def test_ticket_merges_on_qa_pass(hq):
    tid = hq.board.create("Add feature", "Make a feature file.")
    drain(hq)
    assert hq.board.get(tid)["state"] == policy.SHIPPED
    assert ((hq.products_dir / "scratch") / f"feature_{tid}.txt").exists()       # standalone ticket -> sandbox repo
    assert not (hq.workdir / "scratch" / f"T-{tid}").exists()  # worktree cleaned up
    assert thread(hq, "ticket", tid) == [("planner", None), ("engineer", "ready_for_qa"), ("qa", "pass")]


def test_a_teammates_question_goes_to_michael_before_the_pm(hq, monkeypatch):
    real = hq._invoke
    asked = []

    def fake(role, system, prompt, *args, **kwargs):
        if role == "engineer" and len(asked) < 2:          # asks twice: Michael takes the first, the PM the second
            asked.append(prompt)
            return {"status": "needs_pm", "question": f"Question {len(asked)}: which tool renders the icon?"}
        if role == "manager":
            return {"status": "ok", "decision": "ruling", "next": "engineer", "message": "Render it in CI instead."}
        return real(role, system, prompt, *args, **kwargs)
    monkeypatch.setattr(hq, "_invoke", fake)
    tid = hq.board.create("Add the app icon")
    drain(hq)
    messages = hq.board.messages("ticket", tid)
    first = next(m for m in messages if m["body"].startswith("Question 1"))
    assert first["recipient"] == "manager"
    assert any(m["author"] == "manager" and m["verdict"] == "ruling" for m in messages)
    second = next(m for m in messages if m["body"].startswith("Question 2"))
    assert second["recipient"] == "pm" and hq.board.get(tid)["state"] == policy.BLOCKED
    assert "Render it in CI" in asked[1]                     # the engineer carried on with Michael's answer
    hq.act("ticket", tid, "retry", "Fine, do that.")
    drain(hq)
    assert hq.board.get(tid)["state"] == policy.SHIPPED


def test_a_question_while_planning_goes_back_to_planning(hq, monkeypatch):
    real = hq._invoke
    runs = []

    def fake(role, system, prompt, *args, **kwargs):
        runs.append(role)
        if role == "planner" and runs.count("planner") == 1:
            return {"status": "needs_pm", "question": "This looks like a duplicate of T-1. Close it?"}
        if role == "manager":
            return {"status": "ok", "decision": "ruling", "next": "engineer", "message": "Not a duplicate. Plan it."}
        return real(role, system, prompt, *args, **kwargs)
    monkeypatch.setattr(hq, "_invoke", fake)
    tid = hq.board.create("Add the export")
    drain(hq)
    assert runs[:3] == ["planner", "manager", "planner"] and hq.board.get(tid)["state"] == policy.SHIPPED


def test_a_ui_change_is_designed_and_approved_before_it_is_built(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, slug="scratch")
    build = hq.file_change(hq.board.product(pid), "S01: show the meeting that is running now.")
    design = json.loads(hq.board.get(build)["depends_on"])[0]
    assert hq.board.get(design)["builder"] == "designer" and "S01: show the meeting" in hq.board.get(design)["description"]
    assert not hq.board.deps_shipped(hq.board.get(build))            # nothing is built until the design is merged
    assert f"T-{design}" in hq.board.get(build)["description"] and hq.board.get(build)["ui"]
    plain = hq.file_change(hq.board.product(pid), "Speed up the calendar sync.", ui=False)
    assert json.loads(hq.board.get(plain)["depends_on"] or "[]") == []   # no UI, no design step


def test_design_work_never_waits_behind_held_code(hq):
    hq.cfg.setdefault("build", {})["one_at_a_time"] = True
    pid = hq.board.create_product("Nudge: warns you before meetings")
    held = hq.board.create("Built, held for its design", product_id=pid, state=policy.READY_TO_SHIP)
    design = hq.board.create("design: the Now section", product_id=pid, builder="designer")
    code = hq.board.create("Next code ticket", product_id=pid)
    assert hq._may_start(hq.board.get(design), set())               # its own lane: starts though code is held
    assert not hq._may_start(hq.board.get(code), set())             # code still waits for the held code PR
    assert held


def test_a_product_repo_title_has_no_ticket_number_or_product_name(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, slug="nudge")
    tid = hq.board.create("Nudge: design: S01 'Now' section", product_id=pid)
    assert hq._repo_title(hq.board.get(tid)) == "design: S01 'Now' section"
    assert hq._repo_title(hq.board.get(hq.board.create("Nudgers welcome", product_id=pid))) == "Nudgers welcome"


def test_ship_gate(hq):
    hq.cfg["gates"]["ship"] = True
    tid = hq.board.create("Gated ship")
    drain(hq)
    assert hq.board.get(tid)["state"] == policy.READY_TO_SHIP
    gate = hq.board.messages("ticket", tid)[-1]
    assert gate["kind"] == "gate" and "approve" in gate["actions"]
    hq.act("ticket", tid, "approve")
    assert hq.board.get(tid)["state"] == policy.SHIPPED


def test_engineer_qa_three_rounds_then_manager_then_pm(hq):
    tid = hq.board.create("Broken thing [qa-fail]")
    drain(hq)
    t = hq.board.get(tid)
    # 3 failed rounds -> Michael steps in once (fix) -> QA still fails -> PM
    assert roles(hq) == ["planner", "engineer", "qa", "engineer", "qa", "engineer", "qa", "manager", "qa"]
    assert t["state"] == policy.BLOCKED and t["escalations"] == 1
    esc = hq.board.messages("ticket", tid)[-1]
    assert esc["kind"] == "escalation" and '"manager"' in esc["actions"] and "Ask Michael" in esc["actions"]

    hq.act("ticket", tid, "manager", "Look at the plan too")    # the PM can ask him again
    drain(hq)
    assert roles(hq)[-2:] == ["manager", "qa"]
    assert hq.board.get(tid)["state"] == policy.BLOCKED

    hq.act("ticket", tid, "guidance", "Try a different approach")   # a reply unblocks
    assert hq.board.get(tid)["state"] == policy.IN_QA
    assert hq.board.get(tid)["qa_failures"] == 0


def test_manager_first_can_be_turned_off(hq):
    hq.cfg["conversation"]["manager_first"] = False
    tid = hq.board.create("Broken thing [qa-fail]")
    drain(hq)
    assert "manager" not in roles(hq) and hq.board.get(tid)["state"] == policy.BLOCKED


def test_manager_ruling_sends_back_to_qa(hq):
    tid = hq.board.create("Disputed [qa-fail] [mgr-rule]")
    drain(hq)
    t = thread(hq, "ticket", tid)
    i = t.index(("manager", "ruling"))
    assert t[i + 1][0] == "qa"                       # ruling: QA goes next, with fresh rounds


def test_manager_replan_goes_back_to_architect(hq):
    hq.board.create("Bad plan [qa-fail] [mgr-replan]")
    drain(hq)
    r = roles(hq)
    i = r.index("manager")
    assert r[i + 1] == "planner"


def test_manager_escalates_with_summary(hq):
    tid = hq.board.create("Product call [qa-fail] [mgr-escalate]")
    drain(hq)
    msgs = hq.board.messages("ticket", tid)
    assert hq.board.get(tid)["state"] == policy.BLOCKED
    assert (msgs[-2]["author"], msgs[-2]["verdict"]) == ("manager", "escalate")
    assert roles(hq).count("manager") == 1


def test_repeated_failure_escalates_early(hq):
    tid = hq.board.create("Same bug again [qa-repeat]")
    drain(hq)
    t = hq.board.get(tid)
    assert roles(hq) == ["planner", "engineer", "qa", "engineer", "qa", "manager", "qa"]  # 2 rounds, not 3
    assert t["state"] == policy.BLOCKED
    assert any("same failure" in m["body"] for m in hq.board.messages("ticket", tid))


def test_engineer_asks_architect(hq):
    tid = hq.board.create("Export CSV [ask]")
    drain(hq)
    assert hq.board.get(tid)["state"] == policy.SHIPPED
    assert thread(hq, "ticket", tid) == [
        ("planner", None), ("engineer", "question"), ("planner", "answer"),
        ("engineer", "ready_for_qa"), ("qa", "pass")]
    assert roles(hq) == ["planner", "engineer", "planner", "engineer", "qa"]


def test_plan_gate_and_reject_replans(hq):
    hq.cfg["gates"]["plan"] = True
    tid = hq.board.create("Gated feature")
    drain(hq)
    assert hq.board.get(tid)["state"] == policy.PLAN_REVIEW
    hq.reject(tid, "Use SQLite, not files")
    assert hq.board.get(tid)["state"] == policy.APPROVED
    drain(hq)
    hq.approve(tid)
    drain(hq)
    assert hq.board.get(tid)["state"] == policy.SHIPPED


# -- products -------------------------------------------------------------------

def test_product_from_discovery_to_launch(hq):
    pid = hq.discover("tools for freelancers")
    drain(hq)
    p = hq.board.product(pid)
    assert p["state"] == policy.BRIEF_REVIEW and p["title"] == "Invoice Chaser"
    assert thread(hq, "product", pid) == [("scout", None), ("analyst", "done")]

    hq.act("product", pid, "approve")
    drain(hq)
    p = hq.board.product(pid)
    assert p["state"] == policy.UX_REVIEW and "Screens" in p["ux"]

    # PM <-> designer, then approve the UX
    hq.act("product", pid, "guidance", "Make the dashboard the home screen")
    assert hq.board.product(pid)["state"] == policy.UX_DESIGNING
    drain(hq)
    assert hq.board.product(pid)["state"] == policy.UX_REVIEW
    hq.act("product", pid, "approve")
    drain(hq)
    assert hq.board.product(pid)["state"] == policy.DESIGN_REVIEW

    # PM <-> architect back-and-forth, as many rounds as the PM wants
    hq.act("product", pid, "guidance", "Can we use Postgres instead?")
    assert hq.board.product(pid)["state"] == policy.DESIGNING
    drain(hq)
    assert hq.board.product(pid)["state"] == policy.DESIGN_REVIEW
    assert [a for a, _ in thread(hq, "product", pid)][-4:] == ["pm", "architect", "pm", "architect"]
    assert "designer" in [a for a, _ in thread(hq, "product", pid)]

    hq.act("product", pid, "approve")                               # design approved: Jim posts the drafted tickets
    assert hq.board.product(pid)["state"] == policy.TICKET_REVIEW and not hq.board.list(product_id=pid)
    hq.act("product", pid, "approve")                               # tickets approved: now they are created
    repo = hq.products_dir / "invoice-chaser"
    assert hq.board.product(pid)["slug"] == "invoice-chaser" and (repo / ".git").exists()
    shot = repo / "design" / "screens" / "S01-dashboard"
    assert (repo / "design" / "ux.md").exists() and (shot / "screen.html").exists() and (shot / "spec.md").exists()
    ux_msg = next(m for m in hq.board.messages("product", pid) if m["author"] == "designer")
    assert ux_msg["files"] and ux_msg["files"].endswith('S01-dashboard/screen.html"]')
    assert "S01 Dashboard" in ux_msg["body"]
    tickets = hq.board.list(product_id=pid)
    assert [t["ui"] for t in tickets] == [0, 1]
    assert [t["title"] for t in tickets] == ["Core data model", "CLI commands"]
    assert json.loads(tickets[1]["depends_on"]) == [tickets[0]["id"]]

    hq.tick()  # only the first ticket can start; the second waits on it
    assert hq.board.get(tickets[1]["id"])["state"] == policy.APPROVED
    drain(hq)
    assert all(hq.board.get(t["id"])["state"] == policy.SHIPPED for t in tickets)
    assert hq.board.product(pid)["state"] == policy.READY_TO_LAUNCH
    assert all((repo / f"feature_{t['id']}.txt").exists() for t in tickets)  # code in its own repo
    ui_thread = thread(hq, "ticket", tickets[1]["id"])
    assert ui_thread[-2:] == [("qa", "pass"), ("designer", "pass")]          # UI ticket: design review
    assert not list((hq.products_dir / "scratch").glob("feature_*"))

    hq.act("product", pid, "approve")
    assert hq.board.product(pid)["state"] == policy.LAUNCHED
    assert gitops.git(repo, "tag", "-l", "v1").stdout.strip()                 # the release tag
    assert gitops.git(repo, "rev-parse", "master").stdout == gitops.git(repo, "rev-parse", "develop").stdout


def test_two_products_get_separate_repos(hq):
    a, b = hq.discover("one"), hq.discover("two")
    drain(hq)
    for _ in range(4):  # brief -> UX -> design -> tickets -> build
        for pid in (a, b):
            hq.act("product", pid, "approve")
        drain(hq)
    slugs = [hq.board.product(p)["slug"] for p in (a, b)]
    assert slugs == ["invoice-chaser", "invoice-chaser-2"]   # same title, distinct repos
    for s_ in slugs:
        assert len(list((hq.products_dir / s_).glob("feature_*"))) == 2


def test_analyst_pushes_back_then_accepts(hq):
    pid = hq.discover("[picky] dev tools")
    drain(hq)
    assert hq.board.product(pid)["state"] == policy.BRIEF_REVIEW
    assert thread(hq, "product", pid) == [("scout", None), ("analyst", "changes_requested"),
                                          ("scout", None), ("analyst", "done")]


def test_nothing_worth_building_is_the_pms_call(hq):
    def analyst_gives_up():
        pid = hq.discover("barcode scanners")
        hq._p_after_analyst(hq.board.product(pid), {"verdict": "reject_all", "message": "Crowded market."}, None)
        return pid
    pid = analyst_gives_up()
    gate = hq.board.messages("product", pid)[-1]
    assert hq.board.product(pid)["state"] == policy.P_BLOCKED and gate["kind"] == "gate"
    assert [action["id"] for action in json.loads(gate["actions"])] == ["reject", "guidance", "brief_anyway"]

    hq.act("product", pid, "brief_anyway")                                  # overruled: the analyst writes it
    assert hq.board.product(pid)["state"] == policy.DISCOVERING and hq.board.product(pid)["next_speaker"] == "analyst"

    pid = analyst_gives_up()
    hq.act("product", pid, "guidance", "Look at Shopify purchase-order receiving")   # keep digging
    assert hq.board.product(pid)["state"] == policy.DISCOVERING and hq.board.product(pid)["next_speaker"] == "scout"

    pid = analyst_gives_up()
    hq.act("product", pid, "reject")
    assert hq.board.product(pid)["state"] == policy.P_REJECTED


def test_agents_disagree_three_rounds_then_pm(hq):
    pid = hq.discover("[stubborn] anything")
    drain(hq)
    p = hq.board.product(pid)
    assert p["state"] == policy.P_BLOCKED and p["round"] == 3
    # 3 rounds -> Michael rules (fresh rounds, analyst decides) -> 3 more rounds -> PM
    assert roles(hq) == ["scout", "analyst"] * 3 + ["manager"] + ["analyst", "scout", "analyst", "scout", "analyst"]
    assert ("manager", "ruling") in thread(hq, "product", pid)
    assert hq.board.messages("product", pid)[-1]["kind"] == "escalation"

    hq.act("product", pid, "extra_round")
    drain(hq)
    assert hq.board.product(pid)["state"] == policy.P_BLOCKED
    assert roles(hq).count("analyst") == 7

    hq.act("product", pid, "guidance", "Focus on idea 2")   # guidance resets the rounds
    p = hq.board.product(pid)
    assert p["state"] == policy.DISCOVERING and p["round"] == 0 and p["next_speaker"] == "scout"


def test_pm_feedback_on_brief_goes_back_to_analyst(hq):
    pid = hq.discover("x")
    drain(hq)
    hq.act("product", pid, "guidance", "Narrow it to agencies")
    p = hq.board.product(pid)
    assert p["state"] == policy.DISCOVERING and p["next_speaker"] == "analyst"
    drain(hq)
    assert hq.board.product(pid)["state"] == policy.BRIEF_REVIEW


def test_discovery_runs_on_schedule(hq):
    hq.cfg["discovery"]["enabled"] = True
    hq.tick()
    assert len(hq.board.products()) == 1
    hq.tick()
    assert len(hq.board.products()) == 1   # not due again for every_hours


# -- plumbing -------------------------------------------------------------------

def test_outbox(hq):
    tid = hq.board.create("x")
    hq.board.post("ticket", tid, "hq", "hello")
    out = hq.board.outbox()
    assert [m["body"] for m in out] == ["hello"]
    hq.board.mark_delivered(out[0]["id"])
    assert hq.board.outbox() == []


def test_parse_ref():
    assert parse_ref("T-3") == ("T", 3)
    assert parse_ref("3") == ("T", 3)
    assert parse_ref("p-2") == ("P", 2)
    assert parse_ref("i-4") == ("I", 4)


def test_illegal_transition_rejected(hq):
    tid = hq.board.create("x")
    with pytest.raises(policy.IllegalTransition):
        hq.board.transition(tid, policy.SHIPPED, "pm")


# -- designer -------------------------------------------------------------------

def to_design_review(hq, focus):
    pid = hq.discover(focus)
    drain(hq)
    hq.act("product", pid, "approve")   # brief -> UX
    drain(hq)
    hq.act("product", pid, "approve")   # UX -> architect
    drain(hq)
    return pid


def test_architect_raises_ux_concern_designer_adjusts(hq):
    pid = to_design_review(hq, "[ux-concern] boards")
    assert hq.board.product(pid)["state"] == policy.DESIGN_REVIEW
    t = thread(hq, "product", pid)
    i = t.index(("architect", "changes_requested"))
    assert t[i + 1] == ("designer", "answer") and t[i + 2][0] == "architect"


def test_architect_and_designer_disagree_three_rounds_then_pm(hq):
    pid = to_design_review(hq, "[ux-stubborn] boards")
    p = hq.board.product(pid)
    assert p["state"] == policy.P_BLOCKED and p["round"] == 3
    hq.act("product", pid, "guidance", "Keep it simple: a table")
    p = hq.board.product(pid)
    assert p["state"] == policy.DESIGNING and p["next_speaker"] == "designer" and p["round"] == 0


def test_ui_ticket_fails_design_review_three_rounds(hq):
    tid = hq.board.create("Settings page [ux-fail]", ui=True)
    drain(hq)
    t = hq.board.get(tid)
    assert t["state"] == policy.BLOCKED and t["escalations"] == 1
    assert roles(hq) == ["planner"] + ["engineer", "qa", "designer"] * 3 + ["manager", "qa", "designer"]


def test_engineer_asks_designer(hq):
    tid = hq.board.create("Export button [ask-designer]", ui=True)
    drain(hq)
    assert hq.board.get(tid)["state"] == policy.SHIPPED
    assert thread(hq, "ticket", tid)[1:3] == [("engineer", "question"), ("designer", "answer")]
    assert roles(hq) == ["planner", "engineer", "designer", "engineer", "qa", "designer"]


def test_env_file(tmp_path, monkeypatch):
    from hq.orchestrator import load_env
    (tmp_path / ".env").write_text(
        "# comment\nDMI_T_EMPTY=          # explanation\nDMI_T_SET=abc123   # note\n"
        "export DMI_T_QUOTED='q v'\nDMI_T_KEEP=from-file\n")
    for k in ("DMI_T_EMPTY", "DMI_T_SET", "DMI_T_QUOTED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DMI_T_KEEP", "real")
    load_env(tmp_path)
    import os
    assert "DMI_T_EMPTY" not in os.environ
    assert os.environ["DMI_T_SET"] == "abc123" and os.environ["DMI_T_QUOTED"] == "q v"
    assert os.environ["DMI_T_KEEP"] == "real"          # real env vars win
    for k in ("DMI_T_SET", "DMI_T_QUOTED"):
        monkeypatch.delenv(k)


# -- chat -------------------------------------------------------------------------

def place(name="research"):
    return {"kind": "channel", "name": name, "topic": "code", "members": ["jim", "darryl", "dwight"]}


def test_chat_reply(hq):
    r = hq.chat_turn("darryl", place(), [{"author": "PM", "text": "how's the build?"}])
    assert "how's the build?" in r["message"] and r["mentions"] == [] and r["proposal"] is None
    run = hq.board.runs_since(0)[-1]
    assert (run["role"], run["subject"]) == ("engineer", "chat")


def test_two_requests_in_one_message_get_two_proposals(hq, monkeypatch):
    asked = [{"type": "ticket", "title": "Show the running meeting", "description": "A Now row.", "product": "Nudge"},
             {"type": "ticket", "title": "Show the join link", "description": "Meet or Zoom link.", "product": "Nudge"},
             {"type": "nonsense"}]
    monkeypatch.setattr(hq, "_invoke", lambda *args, **kwargs: {"message": "Both proposed.", "proposals": asked})
    reply = hq.chat_turn("jim", place(), [{"author": "PM", "text": "two features, separate tickets"}])
    titles = [hq.board.proposal(pid)["payload"]["title"] for pid in reply["proposals"]]
    assert titles == ["Show the running meeting", "Show the join link"] and reply["proposal"] == reply["proposals"][0]


def test_chat_mentions_teammate(hq):
    r = hq.chat_turn("jim", place(), [{"author": "PM", "text": "is it tested? [mention:dwight]"}])
    assert r["mentions"] == ["dwight"] and "@Dwight" in r["message"]


def test_chat_proposal_needs_pm_ok(hq):
    r = hq.chat_turn("darryl", place(), [{"author": "PM", "text": "export is slow [ticket]"}])
    pid = r["proposal"]
    assert "Darryl proposes a ticket" in hq.proposal_text(pid)
    assert hq.board.list() == []                      # nothing happens until the PM accepts
    with pytest.raises(ValueError, match="names no product"):
        hq.act("proposal", pid, "accept")             # a ticket is built in a product's repo
    assert hq.board.proposal(pid)["status"] == "open" and hq.board.list() == []
    prod = hq.board.create_product("Exporter: fast exports")
    hq.board.update_product(prod, slug="exporter", number=7, state="building")
    pid = hq.board.propose("darryl", "ticket", {"title": "Speed up export", "description": "It is slow.",
                                                "product": "Exporter"})
    out = hq.act("proposal", pid, "accept")
    t = hq.board.list()[0]
    assert out == f"T-{t['id']}" and t["title"] == "Speed up export" and t["state"] == policy.APPROVED
    assert t["product_id"] == prod
    with pytest.raises(policy.IllegalTransition):
        hq.act("proposal", pid, "accept")             # can't accept twice


def test_chat_discover_proposal(hq):
    r = hq.chat_turn("ryan", place("research"), [{"author": "PM", "text": "find me something [discover]"}])
    out = hq.act("proposal", r["proposal"], "accept")
    assert out.startswith("I-") and hq.board.products()[0]["focus"] == "tools for dentists"


def test_chat_uses_read_only_tools_and_personality(hq, monkeypatch):
    seen = {}
    real = hq._invoke

    def spy(role, system, prompt, cwd, kind, subject, add_dirs=(), rc=None):
        seen.update(system=system, tools=rc["tools"])
        return real(role, system, prompt, cwd, kind, subject, add_dirs, rc)
    monkeypatch.setattr(hq, "_invoke", spy)
    hq.cfg["channels"]["lounge"] = {"members": "all", "lead": "random", "playful": True}
    hq.chat_turn("dwight", place("lounge"), [{"author": "PM", "text": "beets?"}])
    assert seen["tools"] == ["Read", "Grep", "Glob"]   # chat reads the repos by default, never writes
    hq.cfg["roles"]["qa"]["harness"] = "claude"
    rc_tools = hq.READ_ONLY["claude"]
    assert rc_tools == ["Read", "Grep", "Glob"]
    assert "You are Dwight, Dunder Mifflin's QA" in seen["system"] and "Play it up" in seen["system"]
    assert hq.cfg["roles"]["qa"]["tools"] == ["Read", "Grep", "Glob", "Bash", "Write"]  # pipeline config untouched


def test_pipeline_prompts_carry_identity(hq, monkeypatch):
    seen = []
    real = hq._invoke

    def spy(role, system, *a, **kw):
        seen.append((role, system))
        return real(role, system, *a, **kw)
    monkeypatch.setattr(hq, "_invoke", spy)
    hq.board.create("x")
    drain(hq)
    assert any(r == "engineer" and "You are Darryl" in s for r, s in seen)


# -- secrets ------------------------------------------------------------------------

SECRETS = {"PATH": "/bin", "HOME": "/home/x", "DISCORD_TOKEN": "d", "DISCORD_TOKEN_DWIGHT": "dd",
           "NVIDIA_API_KEY": "n", "OPENROUTER_API_KEY": "o", "BRAVE_API_KEY": "b", "PI_OFFLINE": "1",
           "AWS_SECRET_ACCESS_KEY": "a"}


def test_agents_get_only_their_keys():
    from hq.harness import agent_env
    eng = agent_env("pi", "nvidia/nvidia/nemotron", {}, SECRETS)
    assert eng == {"PATH": "/bin", "HOME": "/home/x", "NVIDIA_API_KEY": "n", "PI_OFFLINE": "1"}
    fallback = agent_env("pi", "openrouter/x:free", {}, SECRETS)
    assert "OPENROUTER_API_KEY" in fallback and "NVIDIA_API_KEY" not in fallback
    scout = agent_env("pi", "nvidia/x", {"secrets": ["BRAVE_API_KEY"]}, SECRETS)
    assert scout["BRAVE_API_KEY"] == "b"
    qa = agent_env("claude", "claude-sonnet-5-5", {}, SECRETS)
    assert set(qa) == {"PATH", "HOME"}                      # claude uses its own login
    for env in (eng, fallback, scout, qa):
        assert not any(k.startswith(("DISCORD", "AWS")) for k in env)


def test_startup_releases_stale_claims(hq):
    tid = hq.board.create("x")
    assert hq.board.claim(tid, "engineer", 3600)
    assert hq.board.release_all() == 1
    assert hq.board.get(tid)["claimed_by"] is None


def test_old_board_gets_new_columns(tmp_path):
    import sqlite3

    from hq.board import Board
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, title TEXT NOT NULL, state TEXT NOT NULL,"
              " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    c.execute("INSERT INTO products (title, state, created_at, updated_at) VALUES ('x', 'discovering', 0, 0)")
    c.commit()
    c.close()
    b = Board(db)
    assert b.product(1)["escalations"] == 0 and b.product(1)["round"] == 0


# -- lockdown + chat models -----------------------------------------------------------

def test_agent_runs_ignore_pm_setup_and_can_have_no_tools():
    from hq.harness import build_command
    cmd, _ = build_command("claude", "claude-haiku-4-5-20251001", {"no_tools": True}, "sys")
    assert "--strict-mcp-config" in cmd and "--disable-slash-commands" in cmd
    assert cmd[cmd.index("--tools") + 1] == "" and "--allowedTools" not in cmd
    cmd, _ = build_command("claude", "claude-sonnet-5-5", {"tools": ["Read", "Bash"]}, "sys")
    assert cmd[cmd.index("--tools") + 1] == "Read,Bash" and "--strict-mcp-config" in cmd
    cmd, _ = build_command("pi", "nvidia/x", {"no_tools": True}, "sys")
    assert {"--no-tools", "--no-extensions", "--no-skills", "--no-context-files"} <= set(cmd)


def test_chat_uses_chat_model_with_read_tools_while_pipeline_is_fake(tmp_path, monkeypatch):
    for d in ("config", "agents", "company", "tools"):
        shutil.copytree(ROOT / d, tmp_path / d)
    monkeypatch.delenv("DMI_HARNESS", raising=False)
    cfg = tmp_path / "config" / "dmi.yaml"
    cfg.write_text(cfg.read_text().replace("pipeline: real", "pipeline: fake"))
    h = HQ(tmp_path)
    assert h.cfg["safety"]["pipeline"] == "fake"
    assert all(rc["harness"] == "fake" for rc in h.cfg["roles"].values())     # pipeline: stand-in
    rc = h._chat_rc("darryl", "engineer")
    assert (rc["harness"], rc["model"], rc["no_tools"]) == ("claude", "claude-haiku-5-5", False)   # chat_tools: read
    assert rc["tools"] == ["Read", "Grep", "Glob"] and "fallback" not in rc


def test_parse_reply():
    from hq.harness import parse_reply
    assert parse_reply('```json\n{"message": "False.", "mentions": []}\n```')["message"] == "False."
    assert parse_reply("just words") == {"message": "just words"}
    assert parse_reply("") is None


def test_transcript_keeps_whole_thread_or_opening_plus_recent():
    msgs = [{"author": "PM", "text": "kickoff: plan the CSV export"}] + \
           [{"author": "Jim · Architect", "text": f"point {i} " + "x" * 80} for i in range(50)]
    full = HQ.fit_transcript(msgs, 100000)
    assert full.count("\n") == 50 and full.startswith("[PM] kickoff")
    cut = HQ.fit_transcript(msgs, 2000)
    assert cut.startswith("[PM] kickoff") and "earlier messages left out" in cut and "point 49" in cut
    assert len(cut) <= 2100


def test_chat_in_a_post_gets_its_subject(hq, monkeypatch):
    pid = hq.discover("x")
    drain(hq)
    seen = {}
    real = hq._invoke
    monkeypatch.setattr(hq, "_invoke", lambda role, system, prompt, *a, **kw: seen.update(p=prompt) or real(role, system, prompt, *a, **kw))
    hq.chat_turn("jim", {"kind": "thread", "name": "P-1", "topic": "", "members": ["jim"], "subject": ("product", pid)},
                 [{"author": "PM", "text": "what do you think of this brief?"}])
    assert "What this thread is about" in seen["p"] and "Invoice Chaser" in seen["p"]


def test_repost_pending_after_post_deleted(hq):
    pid = hq.discover("x")
    drain(hq)                                            # -> brief_review
    before = len(hq.board.messages("product", pid))
    hq.repost_pending("product", pid)
    new = hq.board.messages("product", pid)[before:]
    assert [m["kind"] for m in new] == ["status", "gate"] and "Invoice Chaser" in new[0]["body"] + new[1]["body"]


def test_chat_and_pipeline_have_separate_daily_budgets(hq):
    hq.cfg["roles"]["scout"]["max_runs_per_day"] = 2
    hq.cfg["chat"]["max_replies_per_day"] = 3
    for _ in range(3):                                   # three chat replies from Ryan
        hq.chat_turn("ryan", place("research"), [{"author": "PM", "text": "hi"}])
    assert not hq._over_budget("scout")                  # chatting didn't use his pipeline runs
    assert hq._over_budget("scout", chat=True)           # but he's at his chat limit
    assert "daily chat limit" in hq.chat_turn("ryan", place("research"), [{"author": "PM", "text": "hi"}])["message"]
    for _ in range(2):                                   # two pipeline runs
        hq.board.finish_run(hq.board.start_run("product", 1, "scout", "fake", "m", "/dev/null"), True)
    assert hq._over_budget("scout") and hq.board.runs_since(0, "scout", chat=True).__len__() == 3


def test_hq_loop_stops_promptly_but_never_cuts_off_a_running_turn(hq):
    import threading
    hq.cfg["safety"]["sandbox"] = "host"
    hq.cfg["tick_seconds"] = 60
    # idle: a stop request ends the 60-second wait at once
    hq.tick = lambda wait=True: 0
    stop = threading.Event()
    t = threading.Thread(target=hq.run_forever, args=(stop,))
    t.start()
    stop.set()
    t.join(3)
    assert not t.is_alive()
    # busy: stop during a turn lets that turn finish, then ends the loop
    started, finish = threading.Event(), threading.Event()

    def slow_tick(wait=True):
        started.set()
        finish.wait(10)
        return 0
    hq.tick = slow_tick
    stop = threading.Event()
    t = threading.Thread(target=hq.run_forever, args=(stop,))
    t.start()
    assert started.wait(3)
    stop.set()
    t.join(0.5)
    assert t.is_alive()                  # the running turn is not cut off
    finish.set()
    t.join(3)
    assert not t.is_alive()


# -- design requests in chat -----------------------------------------------------------------

def test_designer_proposes_a_design_and_the_pm_starts_it(hq):
    r = hq.chat_turn("pam", place("alterations"), [{"author": "PM", "text": "design a meeting-alert app [design]"}])
    assert "starts a design" in hq.proposal_text(r["proposal"]) or "proposes starting a design" in hq.proposal_text(r["proposal"])
    assert hq.board.products() == []                              # nothing happens until the PM presses the button
    out = hq.act("proposal", r["proposal"], "accept")
    p = hq.board.products()[0]
    assert out == "MeetingAlert" and p["title"] == "MeetingAlert" and p["state"] == policy.UX_DESIGNING
    drain(hq)
    assert hq.board.product(p["id"])["state"] == policy.UX_REVIEW   # Pam delivered, waiting on the PM


def test_design_proposal_can_use_a_ticket(hq):
    hq.board.create("Build a Mac alert app", "Full-screen warning before meetings.")
    r = hq.chat_turn("pam", place("alterations"), [{"author": "PM", "text": "from T-1 [design-ticket]"}])
    hq.act("proposal", r["proposal"], "accept")
    assert "Full-screen warning" in hq.board.products()[0]["brief"]


def test_design_change_goes_back_to_pam_and_wrong_state_is_refused(hq):
    pid = hq.start_design("Alerts", "A menu-bar app.")
    drain(hq)                                                     # P-1 now waits in UX review
    r = hq.chat_turn("pam", place("alterations"), [{"author": "PM", "text": "S01 totals [design-change]"}])
    assert "S01: bigger totals" in hq.proposal_text(r["proposal"])
    hq.act("proposal", r["proposal"], "accept")
    assert hq.board.product(pid)["state"] == policy.UX_DESIGNING   # the note went to the designer
    assert any(m["author"] == "pm" and "S01: bigger totals" in m["body"] for m in hq.board.messages("product", pid))

    drain(hq)
    hq.act("product", pid, "approve")                              # UX approved: the architect's turn now
    drain(hq)                                                      # -> system design review
    r1 = hq.chat_turn("jim", place("alterations"), [{"author": "PM", "text": "split the work finer [design-change]"}])
    hq.act("proposal", r1["proposal"], "accept")                   # system-design feedback goes to the architect
    assert hq.board.product(pid)["state"] == policy.DESIGNING and hq.board.product(pid)["next_speaker"] == "architect"
    drain(hq)
    hq.act("product", pid, "approve")                              # design approved: the tickets come up for review
    hq.act("product", pid, "approve")                              # tickets approved: the build starts
    r2 = hq.chat_turn("pam", place("alterations"), [{"author": "PM", "text": "again [design-change]"}])
    hq.act("proposal", r2["proposal"], "accept")                   # too late for notes: it becomes a ticket on the product
    t = [x for x in hq.board.list(product_id=pid) if x["created_by"] == "pm"][-1]
    assert t["state"] == policy.APPROVED and t["ui"] and t["title"].startswith(hq.board.products()[0]["title"].split(":")[0])
    assert "bigger totals" in t["description"] and hq.board.proposal(r2["proposal"])["status"] == "accepted"
    t2 = hq.file_change(hq.board.product(pid), "Swap the menu bar icon for a bell. Keep the badge.", ui=False)
    assert hq.board.get(t2)["title"].endswith("Swap the menu bar icon for a bell") and not hq.board.get(t2)["ui"]
    with pytest.raises(ValueError):
        hq.file_change(hq.board.product(pid), "  ")


def test_designer_chat_sees_the_library_and_the_spec_of_a_named_screen(hq, monkeypatch):
    pid = hq.start_design("Alerts", "A menu-bar app.")
    drain(hq)
    seen = {}
    real = hq._invoke
    monkeypatch.setattr(hq, "_invoke", lambda role, system, prompt, *a, **kw: seen.update(p=prompt) or real(role, system, prompt, *a, **kw))
    hq.chat_turn("pam", place("alterations"), [{"author": "PM", "text": "what is on S01?"}])
    assert "The design library" in seen["p"] and "S01  Dashboard" in seen["p"]
    assert "### Alerts S01 Dashboard: spec.md" in seen["p"] and "A table." in seen["p"]
    seen.clear()
    hq.chat_turn("jim", place("research"), [{"author": "PM", "text": "how is the build?"}])
    assert "The design library" not in seen["p"]                  # not given in unrelated chats
    seen.clear()
    pl = {**place("alterations"), "subject": ("product", pid)}   # but in a product's channel everyone gets it, for that product
    hq.chat_turn("jim", pl, [{"author": "PM", "text": "does S01 look buildable?"}])
    assert "The design library" in seen["p"] and "S01 Dashboard: spec.md" in seen["p"]


def test_products_are_named_and_ideas_numbered(hq):
    from hq import refs
    a = hq.board.create_product("Idea A")
    b = hq.board.create_product("Nudge: A Mac menu bar app")
    hq.board.update_product(b, slug="nudge")
    c = hq.board.create_product("AirGap Docs: local-first docs")
    hq.board.update_product(c, slug="airgap-docs")
    hq.board.number_approved()
    assert refs.product_ref(hq.board.product(a)) == f"I-{a}"          # not approved: still an idea
    assert hq.board.pref(b) == "Nudge"
    for typed in ("Nudge", "nudge", "NUDGE", "nud", "P-1"):          # name, slug, prefix, or the old number
        assert hq.board.resolve_ref(typed) == ("product", b)
    for typed in ("AirGap Docs", "airgap-docs", "airgap"):
        assert hq.board.resolve_ref(typed) == ("product", c)
    assert hq.board.resolve_ref(f"I-{a}") == ("product", a)
    with pytest.raises(KeyError):
        hq.board.resolve_ref("nothing")


def test_the_architect_asks_which_stack_before_designing(hq):
    hq.cfg["ask_stack"] = True
    pid = hq.start_design("Alerts", "A menu-bar app.")
    drain(hq)
    hq.act("product", pid, "approve")                                # UX approved: the architect's turn
    drain(hq)
    p = hq.board.product(pid)
    assert p["state"] == policy.DESIGN_REVIEW and not p["design"] and not p["tickets_json"]
    ask = [m for m in hq.board.messages("product", pid) if m["author"] == "architect"][-1]
    assert ask["recipient"] == "pm" and "Option A" in ask["body"]
    with pytest.raises(policy.IllegalTransition):
        hq.act("product", pid, "approve")                            # nothing to approve until he has designed it
    hq.say_product(pid, "Go with Option A.")                          # your answer goes back to Jim
    drain(hq)
    p = hq.board.product(pid)
    assert p["state"] == policy.DESIGN_REVIEW and "SQLite" in p["design"] and json.loads(p["tickets_json"])
    hq.act("product", pid, "approve")                                # now the design can be approved
    assert hq.board.product(pid)["state"] == policy.TICKET_REVIEW
    hq.act("product", pid, "approve")                                # and the tickets, then it is built
    assert hq.board.product(pid)["state"] == policy.BUILDING


def test_a_products_first_commit_is_gitignore_then_the_readme(hq):
    pid = hq.start_design("Pingr: never miss a thing", "## MVP Scope\n- Remind you\n- Snooze\n")
    repo = hq.products_dir / hq.board.product(pid)["slug"]
    log = gitops.git(repo, "log", "--reverse", "--format=%s").stdout.splitlines()
    assert log[:2] == ["chore: .gitignore", "docs: README"]
    first = gitops.git(repo, "rev-list", "--max-parents=0", "HEAD").stdout.split()[0]
    assert gitops.git(repo, "show", "--name-only", "--format=", first).stdout.split() == [".gitignore"]
    assert "Pingr" in (repo / "README.md").read_text()


def test_an_idea_you_bring_is_validated_by_the_analyst_before_design(hq):
    hq.cfg["validate_ideas"] = True
    pid = hq.start_design("Mentora", "A study app that teaches from your own syllabus.")
    p = hq.board.product(pid)
    assert p["state"] == policy.DISCOVERING and p["next_speaker"] == "analyst"
    drain(hq)                                                         # Oscar asks; Ryan never runs
    p = hq.board.product(pid)
    assert p["state"] == policy.BRIEF_REVIEW
    asked = [m for m in hq.board.messages("product", pid) if m["author"] == "analyst"][-1]
    assert asked["recipient"] == "pm" and asked["verdict"] == "question"
    assert not [m for m in hq.board.messages("product", pid) if m["author"] == "scout"]
    with pytest.raises(policy.IllegalTransition):
        hq.act("product", pid, "approve")                             # no brief yet, nothing to approve
    hq.say_product(pid, "Answers: students first; phone.")
    drain(hq)
    p = hq.board.product(pid)
    assert p["state"] == policy.BRIEF_REVIEW and "Invoice Chaser" in p["brief"]
    hq.act("product", pid, "approve")                                # the brief is approved: design starts
    assert hq.board.product(pid)["state"] == policy.UX_DESIGNING


def test_an_idea_taken_to_ryan_is_researched_before_oscar_validates_it(hq):
    hq.cfg["validate_ideas"] = True
    pid = hq.start_design("", "A study app that teaches from your own syllabus.", research=True)
    p = hq.board.product(pid)
    assert p["next_speaker"] == "scout" and p["focus"] == hq.PM_IDEA
    drain(hq)
    authors = [m["author"] for m in hq.board.messages("product", pid)]
    assert authors.index("scout") < authors.index("analyst")          # Ryan first, then Oscar
    assert hq.board.product(pid)["state"] == policy.BRIEF_REVIEW
    assert "Ryan" in next(m for m in hq.board.messages("product", pid) if m["author"] == "hq")["body"]


def test_ryans_proposal_is_a_research_first_design(hq):
    pid = hq._chat_proposal("ryan", {"type": "design", "title": "", "brief": "An idea.", "from_ticket": None})
    assert hq.board.proposal(pid)["payload"]["research"] is True
    pid = hq._chat_proposal("oscar", {"type": "design", "title": "", "brief": "An idea.", "from_ticket": None})
    assert hq.board.proposal(pid)["payload"]["research"] is False
    assert "research" in hq._intake_rules("ryan") and "research" not in hq._intake_rules("oscar").lower().split("start")[0]


def test_the_inbox_lists_what_waits_on_you_and_what_hangs(hq):
    import time

    from hq import inbox
    hq.cfg["validate_ideas"] = True
    hq.start_design("Mentora", "A study app.")
    drain(hq)                                                         # Oscar's questions wait on you
    hung = hq.board.create_product("Quiet", "x")                      # a discovery nobody is running
    with hq.board._conn() as c:
        c.execute("UPDATE products SET updated_at = ? WHERE id = ?", (time.time() - 5 * 3600, hung))
    text, n = inbox.build(hq, link=lambda kind, row: f"https://discord.test/{row['id']}")
    assert n == {"waiting": 1, "hanging": 1}
    needs, hang = text.split("**Hanging**")
    assert "Mentora" in needs and "brief review" in needs and "Quiet" not in needs
    assert "I-" in hang and "5h" in hang
    assert "HQ's work loop has stopped" in inbox.build(hq, loop_alive=False)[0]


def test_a_slow_turn_does_not_hold_up_the_next_ticket(hq):
    import threading
    hq.cfg["safety"]["sandbox"] = "host"
    release, started = threading.Event(), []

    def slow(role, rid):
        started.append(rid)
        release.wait(10)
    a = hq.board.create("A", "x", state=policy.APPROVED, actor="pm")
    b = hq.board.create("B", "y", state=policy.APPROVED, actor="pm")
    hq.ticket_turn = slow
    hq.cfg["max_concurrent"] = 4
    jobs = lambda: hq.tick(wait=False)
    first = jobs()
    assert first == 2                                                 # B starts although A is still running
    second = jobs()                                                   # nothing is started twice while it runs
    assert second == 0
    release.set()
    hq._pool.shutdown(wait=True)
    assert sorted(started) == sorted([a, b])


def test_an_unnamed_idea_gets_its_name_from_a_discussion_not_from_the_crew(hq):
    hq.cfg["validate_ideas"] = True
    pid = hq.start_design("", "A tool that tells students what to revise next.")
    assert hq.board.product(pid)["title"] == hq.UNNAMED
    drain(hq)                                                         # Oscar asks (names included); nothing is decided
    assert hq.board.product(pid)["state"] == policy.BRIEF_REVIEW
    assert hq.board.product(pid)["title"] == hq.UNNAMED
    hq.say_product(pid, "Answers: students; phone. Name it Revisr.")
    drain(hq)
    assert hq.board.product(pid)["state"] == policy.BRIEF_REVIEW and not hq.board.product(pid)["title"].startswith("Untitled")


def test_the_ideas_channel_tells_oscar_to_put_up_the_design_proposal(hq, monkeypatch):
    seen = {}
    real = hq._invoke

    def spy(role, system, prompt, cwd, kind, subject, add_dirs=(), rc=None):
        seen["prompt"] = prompt
        return real(role, system, prompt, cwd, kind, subject, add_dirs, rc)
    monkeypatch.setattr(hq, "_invoke", spy)
    hq.cfg["channels"]["ideas"] = {"members": ["oscar"], "lead": "oscar", "forum": True, "intake": True}
    hq.chat_turn("oscar", place("ideas"), [{"author": "PM", "text": "yes proceed"}])
    assert "START THE PRODUCT" in seen["prompt"] and "Never say you can't create the brief" in seen["prompt"]
    hq.chat_turn("oscar", place("elsewhere"), [{"author": "PM", "text": "hi"}])
    assert "START THE PRODUCT" not in seen["prompt"]


def test_prompt_rules_files_are_split_by_turn_type(hq):
    from hq import prompts
    pipeline = prompts.charter(hq.root, "qa")
    chat = prompts.charter(hq.root, "qa", pipeline=False)
    assert "How conversations work at Dunder Mifflin" in pipeline and "How conversations work" not in chat
    assert "Rules for every turn" in pipeline and "Rules for every turn" in chat       # the shared rules go to both
    assert "<!--" not in pipeline and "<!--" not in chat                              # the explaining comments aren't sent
    assert "Chat rules" in prompts.read_rules(hq.root / "agents" / "_discord-chat.md")
    assert "<!--" not in prompts.read_rules(hq.root / "agents" / "_discord-chat.md")


# -- ticket review: the owner goes through Jim's drafted tickets before any is created ----------------------

def test_drafted_tickets_are_reviewed_before_any_is_created(hq):
    pid = to_design_review(hq, "boards")
    hq.act("product", pid, "approve")                                  # the design
    p = hq.board.product(pid)
    assert p["state"] == policy.TICKET_REVIEW and not hq.board.list(product_id=pid)
    plan = [m for m in hq.board.messages("product", pid) if m["author"] == "architect" and m["recipient"] == "pm"][-1]
    assert "2 tickets I drafted" in plan["body"] and "**2. CLI commands** · screens · after #1" in plan["body"]
    assert "- tables exist" in plan["body"]
    gate = [m for m in hq.board.messages("product", pid) if m["kind"] == "gate"][-1]
    assert [a["id"] for a in json.loads(gate["actions"])][:2] == ["approve", "guidance"]

    hq.say_product(pid, "Split the export out of the commands")        # a comment goes back to Jim, tickets only
    assert hq.board.product(pid)["state"] == policy.DESIGNING and hq.board.product(pid)["tickets_only"]
    drain(hq)
    p = hq.board.product(pid)
    assert p["state"] == policy.TICKET_REVIEW and not p["tickets_only"] and not hq.board.list(product_id=pid)
    assert "Export to CSV" in [m for m in hq.board.messages("product", pid) if m["author"] == "architect"][-1]["body"]

    hq.act("product", pid, "approve")                                  # the tickets: now they exist
    assert hq.board.product(pid)["state"] == policy.BUILDING
    assert [t["title"] for t in hq.board.list(product_id=pid)] == ["Core data model", "Export to CSV"]


def test_tickets_can_be_dropped_at_review(hq):
    pid = to_design_review(hq, "boards")
    hq.act("product", pid, "approve")
    hq.act("product", pid, "reject")
    assert hq.board.product(pid)["state"] == policy.P_REJECTED and not hq.board.list(product_id=pid)


def test_a_ticket_too_big_for_one_pr_is_split_into_tickets_built_in_order(hq):
    scratch_product(hq)
    big = hq.board.create("[split] Export", "Data and page.", policy.APPROVED, actor="architect")
    after = hq.board.create("Email the export", "", policy.APPROVED, actor="architect", depends_on=[big])
    drain(hq)
    tickets = {ticket["title"]: ticket for ticket in hq.board.list(None)}
    page = tickets["Export page"]
    assert hq.board.get(big)["title"] == "Export data" and json.loads(page["depends_on"]) == [big]
    assert page["id"] in json.loads(hq.board.get(after)["depends_on"])          # it waited for the whole ticket
    assert all(ticket["state"] == policy.SHIPPED for ticket in tickets.values())
    assert any("split, built in this order" in message["body"] for message in hq.board.messages("ticket", big))


def test_brief_items_and_new_features_are_numbered_together():
    brief = "# X\n## MVP scope\n- Orders\n- Invoices\n## Metrics\n- none"                    # as Oscar writes it
    grown = hq_module.add_to_brief(brief, ["Rework log"], "2026-11-15")
    assert "## Metrics" in grown and grown.index("## Added since approval") > grown.index("## MVP scope")
    assert hq_module.brief_items(grown) == [("M1", "Orders"), ("M2", "Invoices"), ("M3", "Rework log (added 2026-11-15)")]
    again = hq_module.add_to_brief(grown, ["Costs"], "2026-11-16")
    assert again.count("## Added since approval") == 1 and hq_module.brief_items(again)[-1][0] == "M4"
    assert hq_module.add_to_brief(again, ["rework  LOG", "Orders"], "2026-11-17") == again     # a retry says it again


def test_a_plan_that_leaves_out_part_of_the_brief_goes_back_to_the_architect(hq):
    pid = to_design_review(hq, "boards")
    p = hq.board.product(pid)
    hq.board.update_product(pid, brief=p["brief"] + "\n## MVP Scope\n- Send reminders\n")
    gaps = hq._coverage_gaps(hq.board.product(pid), json.loads(p["tickets_json"]))
    assert gaps == [("M1", "Send reminders")]                                    # S01 is named by a ticket
    assert "- M1: Send reminders" in hq._coverage_brief(hq.board.product(pid))


def test_a_feature_asked_for_after_the_brief_goes_into_it(hq):
    pid = hq.start_design("Tailor [feature]", "Orders for a tailoring shop.")
    drain(hq)
    brief = hq.board.product(pid)["brief"]
    assert "## Added since approval\n- Rework log: track alterations after delivery" in brief
    assert any("Added to the brief" in message["body"] for message in hq.board.messages("product", pid))


def test_a_role_waits_while_its_claude_login_is_out_of_its_usage_window(hq):
    from types import SimpleNamespace

    from hq.harness import agent_env
    original = dict(hq.cfg["roles"]["engineer"])
    hq.cfg["roles"]["engineer"].update(harness="claude", model="claude-sonnet-5-5")
    hq._sandbox = SimpleNamespace(gate=SimpleNamespace(claude_paused=lambda model: 600 if "sonnet" in model else 0))
    tid = hq.board.create("Add export", "")
    drain(hq)
    assert hq.board.get(tid)["state"] == policy.READY_FOR_DEV and "engineer" not in roles(hq)    # planned, then waits
    hq._sandbox = SimpleNamespace(gate=SimpleNamespace(claude_paused=lambda model: 0))
    hq.cfg["roles"]["engineer"] = original                                     # the scripted engineer again
    drain(hq)
    assert hq.board.get(tid)["state"] == policy.SHIPPED
    source = {"CLAUDE_CODE_OAUTH_TOKEN": "main", "CLAUDE_CODE_OAUTH_TOKEN_OPUS": "opus"}
    assert agent_env("claude", "claude-sonnet-5-5", {}, source)["CLAUDE_CODE_OAUTH_TOKEN"] == "main"
    assert agent_env("claude", "claude-opus-5-5", {}, source)["CLAUDE_CODE_OAUTH_TOKEN"] == "opus"
    assert "CLAUDE_CODE_OAUTH_TOKEN_OPUS" not in agent_env("claude", "claude-opus-5-5", {}, source)


def test_a_feature_added_to_the_brief_is_checked_against_the_stored_tickets_too(hq):
    pid = to_design_review(hq, "boards")
    hq.board.update_product(pid, brief=hq.board.product(pid)["brief"] + "\n## MVP Scope\n- Send reminders\n")
    (hq.workdir / f"P-{pid}" / ".dmi" / "tickets.json").unlink(missing_ok=True)   # only the stored tickets remain
    hq._p_after_architect(hq.board.product(pid), {"verdict": "done", "message": "Revised."}, hq.workdir / f"P-{pid}")
    assert any("Send reminders" in m["body"] for m in hq.board.messages("product", pid) if m["recipient"] == "architect")


def test_a_ticket_can_depend_on_one_listed_after_it(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, tickets_json=json.dumps([
        {"key": "ui", "title": "Settings screen", "depends_on": ["store"]},
        {"key": "store", "title": "Settings store"}]))
    screen, store = hq._create_tickets(hq.board.product(pid))
    assert json.loads(hq.board.get(screen)["depends_on"]) == [store]


def test_the_manager_gets_the_turn_asked_for_on_blocked_ux_work(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, state=policy.UX_DESIGNING, next_speaker="manager")
    assert hq._speaker(hq.board.product(pid)) == "manager"
    hq.board.update_product(pid, next_speaker=None)
    assert hq._speaker(hq.board.product(pid)) == "designer"


def test_a_change_to_a_launched_product_is_built_then_launched_again(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, state=policy.LAUNCHED, slug="nudge")
    hq.file_change(hq.board.product(pid), "Add a snooze button.", ui=False)
    assert hq.board.product(pid)["state"] == policy.BUILDING


def test_tickets_that_wait_on_each_other_in_a_circle_are_found():
    specs = [{"key": "a", "depends_on": ["b"]}, {"key": "b", "depends_on": ["a"]}, {"key": "c", "depends_on": ["c"]},
             {"key": "d", "depends_on": ["a"]}]
    assert hq_module.HQ._dependency_cycle(specs) == ["a", "b", "d"]
    assert hq_module.HQ._dependency_cycle(specs[2:3]) == []


def test_work_added_while_waiting_to_launch_is_built_first(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, state=policy.READY_TO_LAUNCH, slug="nudge")
    hq.file_change(hq.board.product(pid), "Add a snooze button.", ui=False)
    assert hq.board.product(pid)["state"] == policy.BUILDING


def test_a_ticket_with_work_on_its_branch_is_not_split(hq):
    tid = hq.board.create("Export", "", policy.APPROVED, actor="architect")
    hq.board.update(tid, pr_number=4)
    hq._split_ticket(hq.board.get(tid), {"tickets": [{"title": "Export data"}, {"title": "Export page"}]})
    ticket = hq.board.get(tid)
    assert ticket["title"] == "Export" and ticket["attempts"] == 1 and len(hq.board.list(None)) == 1


def test_approving_a_proposed_change_to_a_launched_product_builds_it_again(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, state=policy.LAUNCHED, slug="nudge")
    tid = hq.board.create("Match the new screens", "", policy.PROPOSED, actor="designer", product_id=pid)
    hq.approve(tid)
    assert hq.board.product(pid)["state"] == policy.BUILDING


def test_a_ticket_from_a_chat_on_a_launched_product_builds_it_again(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, state=policy.LAUNCHED, slug="nudge", number=3)
    proposal = hq.board.propose("jim", "ticket", {"title": "Snooze", "description": "x", "product": "nudge"})
    hq.accept_proposal(proposal)
    assert hq.board.product(pid)["state"] == policy.BUILDING


def test_an_empty_ticket_list_is_checked_against_the_brief(hq):
    pid = to_design_review(hq, "boards")
    cwd = hq.workdir / f"P-{pid}"
    (cwd / ".dmi" / "tickets.json").write_text("[]")
    (cwd / ".dmi" / "design.md").write_text(hq.board.product(pid)["design"])
    hq._p_after_architect(hq.board.product(pid), {"verdict": "done", "message": "No tickets."}, cwd)
    assert any(m["recipient"] == "architect" and "leave these out" in m["body"]
               for m in hq.board.messages("product", pid))


def test_a_launch_waits_for_work_filed_after_the_gate_opened(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, state=policy.READY_TO_LAUNCH, slug="nudge")
    tid = hq.board.create("Snooze", "", policy.APPROVED, actor="pm", product_id=pid)   # raced in past the gate
    with pytest.raises(policy.IllegalTransition, match=f"T-{tid}"):
        hq.approve_product(pid)
    assert hq.board.product(pid)["state"] == policy.BUILDING


def test_the_develop_fix_starts_even_when_the_lane_is_held(hq):
    hq.cfg.setdefault("build", {})["one_at_a_time"] = True
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.create("Stuck work", "", policy.BLOCKED, actor="pm", product_id=pid)
    fix = hq.board.create(hq.CI_TITLE, "", policy.APPROVED, actor="hq", product_id=pid)
    assert hq._may_start(hq.board.get(fix), set())


def test_nothing_is_built_while_the_approved_design_cant_be_published(hq):
    pid = to_design_review(hq, "boards")
    hq._land_system_design = lambda p: None                                      # its pull request couldn't open
    with pytest.raises(policy.IllegalTransition, match="couldn't publish"):
        hq.approve_product(pid)
    assert hq.board.product(pid)["state"] == policy.DESIGN_REVIEW


def test_a_feature_added_after_the_plan_was_checked_sends_it_back_at_approval(hq):
    pid = to_design_review(hq, "boards")
    hq.add_features(pid, ["Weekly digest email"])
    with pytest.raises(policy.IllegalTransition, match="don't cover"):
        hq.approve_product(pid)
    assert hq.board.product(pid)["state"] == policy.DESIGNING


def test_the_added_section_is_found_however_it_is_capitalised():
    brief = "# X\n## MVP scope\n- Orders\n## Added Since Approval\n- Costs (added 2026-11-01)\n"
    grown = hq_module.add_to_brief(brief, ["Rework log"], "2026-11-16")
    assert grown.lower().count("## added since approval") == 1 and "Rework log" in grown


def test_an_approved_plan_interrupted_while_filing_is_finished_at_startup(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, state=policy.BUILDING, tickets_made=0, tickets_json=json.dumps(
        [{"key": "a", "title": "Store"}, {"key": "b", "title": "Screen", "depends_on": ["a"]}]))
    store = hq.board.create("Store", "", policy.APPROVED, actor="architect", product_id=pid,   # filed before the stop
                            plan_key="a")
    hq._open_launch_gate(pid)
    assert hq.board.product(pid)["state"] == policy.BUILDING                 # no launch on half a plan
    hq._create_tickets(hq.board.product(pid))
    tickets = {t["title"]: t for t in hq.board.list(product_id=pid)}
    assert len(tickets) == 2 and json.loads(tickets["Screen"]["depends_on"]) == [store]
    assert hq.board.product(pid)["tickets_made"] == 1


def test_an_empty_added_section_at_the_end_of_the_brief_takes_the_feature():
    grown = hq_module.add_to_brief("# X\n## MVP scope\n- Orders\n## Added since approval\n", ["Costs"], "2026-11-17")
    assert hq_module.brief_items(grown)[-1] == ("M2", "Costs (added 2026-11-17)")


def test_a_fallback_attempt_doesnt_take_the_failed_attempts_plan(hq, tmp_path):
    from hq import harness
    cwd = tmp_path / "wt"
    (cwd / ".dmi").mkdir(parents=True)
    calls = []

    def run(name, model, *args, **kwargs):
        calls.append(model)
        if len(calls) == 1:
            (cwd / ".dmi" / "plan.md").write_text("half a plan")             # the first model fails after this
            return harness.RunOutcome(exit_code=1, cost_usd=None)
        (cwd / ".dmi" / "result.json").write_text("{}")
        return harness.RunOutcome(exit_code=0, cost_usd=None)
    hq.cfg["safety"]["sandbox"] = "host"
    hq.cfg["roles"]["planner"]["fallback"] = "other-model"
    import hq.orchestrator as orchestrator_module
    real, orchestrator_module.harness.run = orchestrator_module.harness.run, run
    try:
        assert hq._invoke("planner", "", "", cwd, "ticket", {"id": 1}) == {}
    finally:
        orchestrator_module.harness.run = real
    assert not (cwd / ".dmi" / "plan.md").exists()


def test_a_plan_with_a_key_used_twice_goes_back_to_the_architect(hq):
    pid = to_design_review(hq, "boards")
    cwd = hq.workdir / f"P-{pid}"
    stored = json.loads(hq.board.product(pid)["tickets_json"])
    (cwd / ".dmi" / "tickets.json").write_text(json.dumps([{**spec, "key": "part"} for spec in stored]))
    (cwd / ".dmi" / "design.md").write_text(hq.board.product(pid)["design"])
    hq._p_after_architect(hq.board.product(pid), {"verdict": "done", "message": "Revised."}, cwd)
    assert any("more than one ticket" in m["body"] for m in hq.board.messages("product", pid))


def test_a_dependency_on_no_ticket_goes_back_to_the_architect(hq):
    pid = to_design_review(hq, "boards")
    cwd = hq.workdir / f"P-{pid}"
    stored = json.loads(hq.board.product(pid)["tickets_json"])
    stored[-1]["depends_on"] = ["no-such-ticket"]
    (cwd / ".dmi" / "tickets.json").write_text(json.dumps(stored))
    (cwd / ".dmi" / "design.md").write_text(hq.board.product(pid)["design"])
    hq._p_after_architect(hq.board.product(pid), {"verdict": "done", "message": "Revised."}, cwd)
    assert any("no-such-ticket" in m["body"] for m in hq.board.messages("product", pid) if m["recipient"] == "architect")


def test_new_work_waits_for_the_setup_pull_requests(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, state=policy.DESIGN_REVIEW, setup_prs=json.dumps({"prs": {"readme": 1}, "done": False}))
    tid = hq.board.create("Snooze", "", policy.APPROVED, actor="pm", product_id=pid)     # from chat, before the build
    assert not hq._ticket_due("planner", hq.board.get(tid))
    hq.board.update_product(pid, setup_prs=json.dumps({"prs": {"readme": 1}, "done": True}))
    assert hq._ticket_due("planner", hq.board.get(tid))


def test_a_split_with_a_part_without_a_title_is_refused(hq):
    tid = hq.board.create("Export", "", policy.APPROVED, actor="architect")
    hq._split_ticket(hq.board.get(tid), {"tickets": [{"title": "Data"}, {"description": "no title"}, {"title": "Page"}]})
    assert hq.board.get(tid)["title"] == "Export" and len(hq.board.list(None)) == 1


def test_links_out_of_an_agents_workspace_are_removed(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=x")
    workspace = tmp_path / "wt"
    (workspace / ".dmi").mkdir(parents=True)
    (workspace / ".dmi" / "plan.md").symlink_to(secret)
    (workspace / "inside.md").write_text("ok")
    (workspace / "alias.md").symlink_to(workspace / "inside.md")              # a link within the workspace stays
    (workspace / "design").symlink_to(tmp_path)
    (workspace / ".venv" / "bin").mkdir(parents=True)
    (workspace / ".venv" / "bin" / "python").symlink_to("/usr/bin/python3")   # a virtualenv's: it stays
    assert sorted(hq_module.drop_outside_links(workspace)) == [".dmi/plan.md", "design"]
    assert (workspace / "alias.md").is_symlink() and (workspace / ".venv" / "bin" / "python").is_symlink()
    assert secret.exists()


def test_a_retried_clarification_goes_back_to_whoever_was_asked(hq):
    tid = hq.board.create("Export", "", policy.BLOCKED, actor="pm")
    hq.board.update(tid, blocked_from=policy.CLARIFYING, asked="designer")
    hq.retry(tid)
    ticket = hq.board.get(tid)
    assert ticket["state"] == policy.CLARIFYING and ticket["asked"] == "designer"


def test_a_ticket_from_chat_needs_a_product_with_a_repo(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    proposal = hq.board.propose("jim", "ticket", {"title": "Snooze", "description": "x", "product": hq.board.pref(pid)})
    with pytest.raises(ValueError, match="no repo yet"):
        hq.accept_proposal(proposal)
    assert not hq.board.list(product_id=pid)


def test_a_ticket_waiting_in_the_pool_holds_its_products_lane(hq):
    hq.cfg.setdefault("build", {})["one_at_a_time"] = True
    pid = hq.board.create_product("Nudge: warns you before meetings")
    queued = hq.board.create("First", "", policy.APPROVED, actor="pm", product_id=pid)
    second = hq.board.create("Second", "", policy.APPROVED, actor="pm", product_id=pid)
    hq._inflight.add(("ticket_turn", "planner", queued))
    assert not hq._may_start(hq.board.get(second), set())


def test_a_split_lands_whole_or_not_at_all(hq):
    tid = hq.board.create("Export", "", policy.APPROVED, actor="architect")
    before = len(hq.board.list(None))
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):                      # the last step fails: nothing before it stays
        hq.board.split(tid, [{"title": "Page", "description": ""}], actor="planner", no_such_column=1)
    assert len(hq.board.list(None)) == before and hq.board.get(tid)["title"] == "Export"


def test_a_plan_that_cant_be_filed_doesnt_stop_hq_starting(hq, monkeypatch):
    import threading
    hq.cfg["safety"]["sandbox"] = "host"
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, state=policy.BUILDING, tickets_made=0, tickets_json="[]")

    def broken(p):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(hq, "_create_tickets", broken)
    stop = threading.Event()
    stop.set()                                                            # start, then stop at once
    hq.run_forever(stop)
    assert any("couldn't file" in m["body"] for m in hq.board.messages("product", pid))


def test_a_later_hq_pull_request_doesnt_stop_a_product_already_building(hq):
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, state=policy.BUILDING, setup_prs=json.dumps({"prs": {"hq": 29}, "done": False}))
    tid = hq.board.create("Snooze", "", policy.APPROVED, actor="pm", product_id=pid)
    assert hq._ticket_due("planner", hq.board.get(tid))
