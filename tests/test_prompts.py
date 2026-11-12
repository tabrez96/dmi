"""What an agent is told (prompts) and the scripted stand-in agents the pipeline tests run on (fake_agents)."""

import json

from hq import fake_agents, prompts


def message(author, body, kind="say", recipient=None, round_number=None, verdict=None):
    return {"author": author, "body": body, "kind": kind, "recipient": recipient, "round": round_number,
            "verdict": verdict}


def test_the_transcript_skips_status_lines_and_keeps_the_latest():
    messages = [message("hq", "repo created", kind="status"), message("scout", "two ideas", round_number=1),
                message("analyst", "need pricing", recipient="scout", verdict="changes_requested")]
    text = prompts.transcript(messages)
    assert "repo created" not in text
    assert "[scout -> all | round 1]\ntwo ideas" in text
    assert "[analyst -> scout | verdict=changes_requested]" in text
    many = [message("scout", f"note {i}") for i in range(prompts.TRANSCRIPT_LIMIT + 5)]
    assert "note 4\n" not in prompts.transcript(many) and "note 5" in prompts.transcript(many)


def test_the_last_round_says_so():
    assert "final round" not in prompts.conversation_status("scout", "analyst", 3, 4)
    assert "final round" in prompts.conversation_status("scout", "analyst", 4, 4)


def test_a_prompt_has_the_charter_the_subject_and_the_result_file(tmp_path):
    (tmp_path / "agents" / "planner").mkdir(parents=True)
    (tmp_path / "agents" / "planner" / "charter.md").write_text("You plan tickets.")
    (tmp_path / "agents" / "_shared.md").write_text("<!-- for people -->\nBe brief.")
    (tmp_path / "company").mkdir()
    (tmp_path / "company" / "handbook.md").write_text("We ship small.")
    ticket = {"id": 3, "title": "Add export", "description": "", "plan": None, "qa_report": None}
    events = [{"kind": "feedback", "note": "Use CSV"}]
    system, prompt = prompts.build(tmp_path, "planner", ticket=ticket, events=events, identity="You are Jim.")
    assert system.startswith("You are Jim.") and "You plan tickets." in system and "Be brief." in system
    assert "for people" not in system
    assert '<company_doc name="handbook.md">' in prompt and "# Ticket T-3: Add export" in prompt
    assert "- Use CSV" in prompt and prompt.endswith("HQ only reads that file.")


def test_a_fake_chat_reply_carries_its_proposals(tmp_path):
    prompt = ("## The conversation (oldest first; reply to the last message)\n[Jim] hi\n[PM] can we [ticket] this?\n"
              "It's slow.\n\nAnswer with only the JSON object described in the chat rules.")
    outcome = fake_agents.run("engineer", None, prompt, tmp_path, tmp_path / "log.txt")
    reply = json.loads(outcome.text)
    assert [proposal["type"] for proposal in reply["proposals"]] == ["ticket"]      # the marker isn't on the last line


def test_a_ticket_with_a_chat_heading_is_still_a_pipeline_turn(tmp_path):
    ticket = {"id": 2, "title": "Chat panel", "description": "## Chat\nA panel.", "plan": None, "qa_report": None,
              "state": "in_progress"}
    _, prompt = prompts.build(tmp_path, "engineer", ticket=ticket)
    fake_agents.run("engineer", ticket, prompt, tmp_path, tmp_path / "log.txt")
    assert (tmp_path / ".dmi" / "result.json").exists()


def test_a_fake_engineer_asks_once_then_builds(tmp_path):
    ticket = {"id": 7, "title": "Export [ask]", "state": "in_progress"}
    fake_agents.run("engineer", ticket, f"build it\n\n{prompts.RESULT_NOTE}", tmp_path, tmp_path / "log.txt")
    assert json.loads((tmp_path / ".dmi" / "result.json").read_text())["status"] == "question"
    fake_agents.run("engineer", ticket, f"verdict=answer: use csv\n\n{prompts.RESULT_NOTE}", tmp_path,
                    tmp_path / "log.txt")
    assert (tmp_path / "feature_7.txt").read_text() == "engineer was here\n"
