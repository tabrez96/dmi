"""Pure logic of the Discord layer (no network)."""

import random
from pathlib import Path

import pytest

pytest.importorskip("discord")

from hq import team
from hq.discord_text import chunks, custom_id, linkify, parse_custom_id, render, route, thread_title
from hq.orchestrator import load_config

CFG = load_config(Path(__file__).resolve().parents[1])


def msg(**kw):
    m = {"id": 1, "subject": "ticket", "subject_id": 3, "author": "qa", "recipient": "engineer", "kind": "say",
         "verdict": None, "body": "hello", "actions": None, "round": None}
    m.update(kw)
    return m


def test_labels_use_crew_names():
    assert team.label(CFG, "engineer") == "Darryl · Engineer"
    assert team.label(CFG, "planner") == team.label(CFG, "architect") == "Jim · Architect"
    assert team.label(CFG, "hq") == "Dunder Mifflin HQ"
    assert team.member_for_role(CFG, "qa") == "dwight"


def test_render_badge_round_and_recipient():
    name, content, full = render(CFG, msg(verdict="fail", round=2, body="criterion 4 fails"))
    assert name == "Dwight · QA"
    assert content.splitlines()[0] == "❌ **FAIL** · round 2/3 → Darryl"
    assert full is None
    _, content, _ = render(CFG, msg(), mention=lambda role: "<@42>")
    assert content.startswith("→ <@42>")


def test_long_body_is_attached():
    body = "line\n" * 1000
    _, content, full = render(CFG, msg(body=body))
    assert len(content) < 2000 and "attached" in content and full == body


def test_chunks_respect_limit():
    parts = chunks("a" * 4500 + "\nshort\n")
    assert all(len(p) <= 1900 for p in parts) and "".join(parts) == "a" * 4500 + "\nshort\n"


def test_linkify():
    ids = {"dwight": 1, "darryl": 2}
    assert linkify(CFG, "@dwight check this", ids) == "<@1> check this"
    assert linkify(CFG, "done", ids, ["darryl"]) == "done\n<@2>"
    assert linkify(CFG, "@Pam hi", ids) == "@Pam hi"     # no bot for Pam: left as text


def test_route_pm_lead_mentions_and_water_cooler():
    assert route(CFG, "ideas", "pm", ["dwight", "darryl"]) == ["dwight", "darryl"]
    assert route(CFG, "ideas", "pm", []) == ["oscar"]                 # no mention needed: only Oscar is in there
    pick = route({**CFG, "channels": {"lounge": {"members": "all", "lead": "random"}}}, "lounge", "pm", [], rng=random.Random(1))
    assert len(pick) == 1 and pick[0] in CFG["team"]


def test_route_agents_only_answer_mentions():
    assert route(CFG, "ideas", "ryan", []) == []                   # no mention: nobody answers
    assert route(CFG, "ideas", "ryan", ["darryl", "ryan"]) == ["darryl"]


def test_each_member_has_their_own_rounds():
    from hq.discord_text import Rounds
    mine = {k: Rounds() for k in CFG["team"]}
    limit = CFG["chat"]["max_agent_turns"]
    for _ in range(limit):                                    # Ryan replies `limit` times in thread 7
        mine["ryan"].bump(7)
    assert mine["ryan"].at_limit(7, limit)
    assert not mine["oscar"].at_limit(7, limit)               # Oscar's own count is untouched
    assert not mine["ryan"].at_limit(8, limit)                # and so are Ryan's other threads
    assert mine["ryan"].newly_paused(7) and not mine["ryan"].newly_paused(7)   # PM told once
    for r in mine.values():                                   # the PM speaks: everyone starts over
        r.reset(7)
    assert not mine["ryan"].at_limit(7, limit) and mine["ryan"].newly_paused(7)


def test_custom_id_roundtrip_and_titles():
    assert parse_custom_id(custom_id("proposal", 7, "accept")) == ("proposal", 7, "accept")
    assert parse_custom_id("something|else") is None
    assert thread_title("product", {"id": 2, "title": "Invoice Chaser"}) == "I-2 · Invoice Chaser"
    assert thread_title("product", {"id": 2, "slug": "chaser", "title": "Chaser: Invoice Chaser"}) == "Chaser: Invoice Chaser"


def test_character_is_played_up_with_guard_rails():
    text = team.identity(CFG, "qa", turn="chat")
    assert "Play it up" in text and "Dwight" in text and "Never insult or talk down to the PM" in text
    quiet = {**CFG, "chat": {**CFG.get("chat", {}), "character": False}}
    assert "light touch" in team.identity(quiet, "qa", turn="chat")


def test_pipeline_notes_get_a_voice_but_files_stay_plain():
    voiced = team.identity(CFG, "qa")
    assert "## Your voice" in voiced and "Dwight" in voiced and "stays plain and professional" in voiced
    assert "## Your voice" not in team.identity(CFG, "qa", turn="chat")


def test_system_member_token(monkeypatch):
    from hq.discord_text import system_member, system_token
    assert system_member(CFG) == "michael"
    monkeypatch.delenv("DISCORD_TOKEN_MICHAEL", raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", "legacy")
    assert system_token(CFG) == "legacy"                 # falls back to the old HQ variable
    monkeypatch.setenv("DISCORD_TOKEN_MICHAEL", "mike")
    assert system_token(CFG) == "mike"


def test_chat_thread_name():
    from hq.discord_text import chat_thread_name
    assert chat_thread_name("<@123> how should we test the CSV export?") == "how should we test the CSV export?"
    assert chat_thread_name("   ", "Chat with Jim") == "Chat with Jim"
    long = chat_thread_name("word " * 40)
    assert len(long) <= 80 and long.endswith("…")


def test_channels_are_simple_and_product_channels_follow_the_stage():
    from hq import pchannels as pc
    from hq import policy
    assert set(CFG["channels"]) == {"ideas"}              # design and engineering live in product channels
    pcfg = CFG["product_channel"]
    assert pcfg["members"] == "all"
    for state in policy.PRODUCT_STATES:                                         # every stage has an owner who answers
        assert pc.lead_for(pcfg, state) in CFG["team"], state
    assert pc.lead_for(pcfg, "discovering") == "oscar" and pc.lead_for(pcfg, "ux_review") == "pam"
    assert pc.lead_for(pcfg, "building") == "jim" and pc.lead_for(pcfg, "blocked") == "michael"
    assert pc.lead_for({"lead": "oscar"}, "whatever") == "oscar"


def test_private_channels_still_work():
    from hq.discord_text import HQ_PERMISSIONS, allowed_targets, private_plan
    cfg = {**CFG, "channels": {**CFG["channels"], "vault": {"members": ["pam", "jim"], "lead": "pam", "private": True}}}
    assert allowed_targets(cfg, "vault", ["pam", "ryan", "jim", "dwight"]) == ["pam", "jim"]
    assert allowed_targets(CFG, "ideas", ["pam", "oscar", "ryan"]) == ["oscar", "ryan"]
    ids = {k: i for i, k in enumerate(cfg["team"], 100)}
    plan = private_plan(cfg, "vault", ids, "michael")
    assert {k for k, i in ids.items() if plan[i]} == {"pam", "jim", "michael"}
    assert not plan[ids["ryan"]] and HQ_PERMISSIONS.manage_roles


def test_product_channels_routing_and_content():
    from hq import pchannels as pc
    assert "product" not in CFG["channels"]                                   # the old shared channel is gone
    p2 = {"id": 2, "title": "AlterationsOS — Vertical SaaS for Solo Alterations Shops", "state": "ux_review",
          "focus": "", "brief": "# AlterationsOS\n\n## Problem\n**Who**: 2,460 shops. **Pain**: spreadsheets.\n\n## Users\nx"}
    p3 = {"id": 3, "title": "AirGap Docs: Local-First Screen-to-Documentation for Regulated Teams",
          "state": "ux_review", "focus": "", "brief": ""}
    assert pc.product_name(p2) == "AlterationsOS" and pc.channel_name(p2, {"design"}) == "alterationsos"
    assert pc.product_name(p3) == "AirGap Docs" and pc.channel_name(p3, set()) == "airgap-docs"
    d = pc.description(p2)
    assert d.startswith("Vertical SaaS for Solo Alterations Shops — Who: 2,460 shops") and "**" not in d and len(d) <= 1000
    assert pc.description(p3) == "Local-First Screen-to-Documentation for Regulated Teams"
    assert pc.channel_name({"id": 7, "title": "Design", "state": "x"}, set()) == "design-p7"      # never clash with a team channel
    assert pc.channel_name({"id": 8, "title": "Notes", "state": "x"}, {"notes"}) == "notes-p8"
    disc = {"id": 4, "title": "Discovery: tools for dentists", "focus": "tools for dentists", "state": "discovering"}
    assert pc.channel_name(disc, set()) == "discovery-4" and pc.description(disc) == "Discovery session: tools for dentists"
    assert pc.wants_channel(p2)                                              # every product has its channel from the start
    assert pc.wants_channel({**p2, "slug": "alterationsos"})                 # approved: its repo exists, so it gets a channel
    assert not pc.wants_channel({**p2, "state": "rejected", "slug": None})   # dropped before approval: never gets one
    long = {"id": 5, "title": "X: tag", "state": "x", "brief": "## Problem\n" + "word. " * 400}
    assert len(pc.description(long)) <= 1000
    # a product channel is routed like a team channel (lead answers), via the same config shape
    cfg = {**CFG, "channels": {**CFG["channels"], "p-2": {k: CFG["product_channel"][k] for k in ("members", "lead")}}}
    assert route(cfg, "p-2", "pm", []) == ["oscar"] and route(cfg, "p-2", "pm", ["ryan"]) == ["ryan"]
    cfg["channels"]["p-2"]["lead"] = pc.lead_for(CFG["product_channel"], "ux_review")
    assert route(cfg, "p-2", "pm", []) == ["pam"]                                              # the stage owner answers


def test_products_table_has_a_channel_column(tmp_path):
    from hq.board import Board
    b = Board(tmp_path / "b.db")
    pid = b.create_product("X", "")
    assert b.product(pid)["channel_id"] is None
    assert b.product(pid)["checklist_msg_id"] is None
    b.update_product(pid, channel_id="123", checklist_msg_id="456")
    assert b.product(pid)["channel_id"] == "123" and b.product(pid)["checklist_msg_id"] == "456"


def test_checklist_follows_the_stage():
    from hq import pchannels as pc
    p = {"id": 4, "title": "MeetingAlert: warns before meetings", "slug": "meetingalert", "number": 4, "state": "ux_designing",
         "blocked_from": None}
    c = pc.checklist(p, [], {})
    assert c.startswith("**Where MeetingAlert stands**") and "\u2611 Brief approved" in c
    assert "\u2610 UX designed by Pam (screens in the design library)  \u25c0 now" in c and "**Next:** Pam is designing" in c
    c = pc.checklist({**p, "state": "ux_review"}, [], {})
    assert "\u2611 UX designed by Pam" in c and "\u2610 **You:** review and approve the UX  \u25c0 now" in c
    c = pc.checklist({**p, "state": "building"}, [{"state": "shipped"}, {"state": "in_qa"}, {"state": "rejected"}], {"engineer": "Rover"})
    assert "Build: 1 of 2 tickets shipped (Rover builds" in c and "\u2611 **You:** review and approve the plan" in c
    c = pc.checklist({**p, "state": "blocked", "blocked_from": "design_review"}, [], {})
    assert "\u2610 **You:** review and approve the plan  \u25c0 now" in c and "Stuck: it needs you" in c
    assert pc.checklist({**p, "state": "launched"}, [], {}).count("\u2611") == 8
    assert "dropped" in pc.checklist({**p, "state": "rejected"}, [], {})
    assert pc.checklist({**p, "title": "Discovery: x"}, [], {}).startswith("**Where discovery 4 stands**")


def test_a_tickets_thread_in_a_product_channel_shows_its_state_in_the_title():
    from hq.discord_text import channel_thread_title
    row = {"id": 36, "title": "Nudge: S01: menu bar icon"}
    assert channel_thread_title({**row, "state": "in_qa"}).startswith("🔧 T-36")
    assert channel_thread_title({**row, "state": "ready_to_ship"}).startswith("🔔")
    assert channel_thread_title({**row, "state": "shipped"}).startswith("✅")
    assert channel_thread_title({**row, "state": "blocked"}).startswith("⛔")
    assert len(channel_thread_title({**row, "state": "shipped", "title": "x" * 300})) <= 100


def test_agents_are_told_the_pms_name():
    cfg = {**CFG, "pm_name": "Tabrez"}
    text = team.identity(cfg, "qa")
    assert "The PM's name is Tabrez" in text and "Never write their name in files" in text
    assert "name is" not in team.identity({**CFG, "pm_name": ""}, "qa")


def test_characters_come_with_sample_lines():
    for key in CFG["team"]:
        assert len((CFG.get("character_examples") or {}).get(key) or []) >= 3, f"{key} has no sample lines"
    chat = team.identity(CFG, "qa", turn="chat")
    assert "How you sound" in chat and "grading his own homework" in chat
    assert "How you sound" in team.identity(CFG, "qa")                     # pipeline notes too
    bare = {**CFG, "character_examples": {}}
    assert "How you sound" not in team.identity(bare, "qa", turn="chat")


def test_a_long_crew_message_shows_its_start_and_attaches_the_rest():
    body = "Plan ready: three steps.\n\n" + "- a detail that belongs in the file\n" * 40
    _, content, full = render(CFG, msg(body=body))
    assert len(content) < 1000 and "\nPlan ready" in content and "attached" in content and full == body
    _, content, full = render(CFG, {**msg(body=body), "author": "hq", "kind": "status"})
    assert full is None                       # HQ's own notices aren't cut short
