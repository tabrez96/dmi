"""Assemble what an agent sees: its charter (system prompt) and the task prompt.

Agents have no memory of their own. Everything they need comes from the
company docs, the subject (product or ticket), the conversation transcript,
and their role's notes file.
"""

import re
from pathlib import Path

from . import gitops, refs

COMPANY_DOCS = ["handbook.md", "product-spec.md", "decisions.md"]
TRANSCRIPT_LIMIT = 40  # most recent messages an agent sees
RESULT_NOTE = "When you are done, write .dmi/result.json as described in your charter. HQ only reads that file."


def _read(path):
    path = Path(path)
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def read_rules(path):
    """A rules file for an agent's prompt, without the HTML comment at its top that explains the file to people."""
    return re.sub(r"^\s*<!--.*?-->\s*", "", _read(path), count=1, flags=re.DOTALL)


def charter(root, role, pipeline=True):
    """The system prompt body: the role's charter, then the rules for the turn type (pipeline turns only; chat brings its
    own), then the rules every turn follows."""
    agents = Path(root) / "agents"
    parts = [_read(agents / role / "charter.md"), read_rules(agents / "_pipeline-turn.md") if pipeline else "",
             read_rules(agents / "_shared.md")]
    return "\n\n".join(part for part in parts if part)


def company_context(root):
    parts = []
    for name in COMPANY_DOCS:
        text = _read(Path(root) / "company" / name)
        if text:
            parts.append(f"<company_doc name=\"{name}\">\n{text}\n</company_doc>")
    return "\n\n".join(parts)


def transcript(messages):
    recent = [message for message in messages if message["kind"] != "status"][-TRANSCRIPT_LIMIT:]
    if not recent:
        return ""
    lines = ["## Conversation so far (oldest first)"]
    for message in recent:
        head = f"[{message['author']} -> {message['recipient'] or 'all'}"
        if message["round"] is not None:
            head += f" | round {message['round']}"
        if message["verdict"]:
            head += f" | verdict={message['verdict']}"
        lines.append(f"{head}]\n{message['body']}")
    return "\n\n".join(lines)


def conversation_status(you, other, round_number, max_rounds):
    status = f"## Your turn\nYou are `{you}`, talking with `{other}`. Round {round_number} of {max_rounds}."
    if round_number >= max_rounds:          # HQ escalates after this round
        status += (" This is the final round: settle it now. If you still can't agree, "
                   "HQ will escalate to the PM.")
    return status


def ticket_context(ticket, events, worktree=None):
    lines = [f"# Ticket T-{ticket['id']}: {ticket['title']}", "", ticket["description"] or "(no description)"]
    if ticket.get("plan"):
        lines += ["", "## Approved plan", ticket["plan"]]
    if ticket.get("qa_report"):
        lines += ["", f"## Latest QA report (QA failures so far: {ticket['qa_failures']})",
                  ticket["qa_report"]]
    feedback = [event for event in events if event["kind"] == "feedback" and event["note"]]
    if feedback:
        lines += ["", "## Feedback from the PM (most recent last)"]
        lines += [f"- {event['note']}" for event in feedback[-5:]]
    if worktree and ticket.get("branch"):
        try:
            stat = gitops.diff_stat(worktree)
        except gitops.GitError:
            stat = ""
        if stat:
            lines += ["", f"## Changes on branch {ticket['branch']} vs develop", "```", stat, "```"]
    return "\n".join(lines)


def product_context(product):
    lines = [f"# Product {refs.product_ref(product)}: {product['title']}"]
    if product.get("focus"):
        lines += ["", f"Discovery focus: {product['focus']}"]
    if product.get("brief"):
        lines += ["", "## Product brief", product["brief"]]
    if product.get("ux"):
        lines += ["", "## UX spec (from the designer)", product["ux"]]
    if product.get("design"):
        lines += ["", "## Current design", product["design"]]
    return "\n".join(lines)


def build(root, role, *, ticket=None, events=(), worktree=None, product=None,
          messages=(), extra="", identity=""):
    system = f"{identity}\n\n{charter(root, role)}".strip()
    notes = _read(Path(root) / "agents" / role / "notes.md")
    prompt = [company_context(root)]
    if notes:
        prompt.append(f"<your_notes>\n{notes}\n</your_notes>")
    if product is not None:
        prompt.append(product_context(product))
    if ticket is not None:
        prompt.append(ticket_context(ticket, events, worktree))
    prompt.append(transcript(messages))
    if extra:
        prompt.append(extra)
    prompt.append(RESULT_NOTE)              # every pipeline prompt ends with it (a chat prompt never does)
    return system, "\n\n".join(part for part in prompt if part)
