"""The offline stand-in for every agent: scripted, deterministic role behaviour with no model calls.

HQ's tests drive the real pipeline with it, and `safety.pipeline: fake` (or DMI_HARNESS=fake) runs HQ on it with no model
spend. It only does what is scripted here, so a test passing against it shows the pipeline logic is right, not that a
real agent in a real repo copes.
"""

import json
import re

from .harness import RunOutcome
from .prompts import RESULT_NOTE


def run(role, subject, prompt, cwd, log_path):
    """Deterministic stand-in for every role, so the pipelines can be tested offline.

    Markers in a ticket title / product focus steer it:
      [qa-fail]   QA always fails the ticket        [qa-repeat] QA flags a repeated failure
      [ask]       engineer asks the architect once  [stubborn]  analyst never accepts
      [picky]     analyst pushes back once before accepting
      [ask-designer]  engineer asks the designer once   [ux-fail]  designer fails the UI review
      [cr-skip]   planner skips every CodeRabbit comment (default: fixes the first, skips the rest)
      [ux-concern]    architect pushes back on the UX once   [ux-stubborn]  ...and never stops
    """
    out = cwd / ".dmi"
    out.mkdir(exist_ok=True)
    tag = (subject or {}).get("title", "") + " " + (subject or {}).get("focus", "")
    answered = "verdict=answer" in prompt
    is_product = subject is not None and "focus" in subject
    result = {"status": "ok", "summary": f"fake {role} run", "message": f"fake {role} turn"}
    if not prompt.rstrip().endswith(RESULT_NOTE):          # a chat turn: only pipeline prompts end with the note
        conversation = prompt.split("reply to the last message)\n", 1)[-1].split("\n\nAnswer with only", 1)[0]
        last = re.split(r"\n(?=\[)", conversation)[-1]       # every message starts "[author] ", and may run on
        result = {"message": f"({role}) on it: {last[:80]}", "mentions": [], "proposals": []}
        if "[mention:" in last:
            name = last.split("[mention:", 1)[1].split("]", 1)[0]
            result.update(message=f"@{name.title()} can you take this?", mentions=[name])
        if "[ticket]" in last:
            result["proposals"].append({"type": "ticket", "title": "Speed up export", "description": "CSV export is slow."})
        if "[discover]" in last:
            result["proposals"].append({"type": "discover", "focus": "tools for dentists"})
        if "[design]" in last:
            result["proposals"].append({"type": "design", "title": "MeetingAlert", "brief": "A menu-bar app."})
        if "[design-ticket]" in last:
            result["proposals"].append({"type": "design", "from_ticket": 1})
        if "[design-change]" in last:
            result["proposals"].append({"type": "design_change", "product": "P-1", "note": "S01: bigger totals"})
        log_path.write_text(f"[fake chat {role}] {result}\n", encoding="utf-8")
        return RunOutcome(exit_code=0, cost_usd=0.0, text=json.dumps(result))   # chat replies on stdout
    if role == "scout":
        result["message"] = "Found 2 ideas: (1) invoice chaser, (2) uptime pager. Sources attached."
    elif role == "analyst":
        pushed_back = "verdict=changes_requested" in prompt
        if "Mode: validate the PM's own idea" in prompt and "Answers:" not in prompt:
            result.update(verdict="question", message="1. Who is the first user?\n2. Phone or desktop?")
        elif "[stubborn]" in tag or ("[picky]" in tag and not pushed_back):
            result.update(verdict="changes_requested", message="Need pricing evidence for idea 1.")
        else:
            (out / "brief.md").write_text("# Invoice Chaser\n\nProblem, users, MVP scope, metrics.\n")
            result.update(verdict="done", title="Invoice Chaser", message="Brief ready: Invoice Chaser.")
    elif role == "designer" and "Mode: design work on a product" in prompt:
        brand = cwd / "design" / "brand"                             # a design ticket: the library is in the branch
        brand.mkdir(parents=True, exist_ok=True)
        for name in ("logo.svg", "mark.svg"):
            (brand / name).write_text("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
                                      "<circle cx='32' cy='32' r='28' fill='#2a6'/></svg>")
        (cwd / "design" / "ux.md").write_text("# UX spec\n\nLogo: a green circle that says calm.\n")
        result.update(message="Drew the logo: a green circle.", summary="logo and mark")
    elif role == "designer" and "Mode: look at your own logo" in prompt:
        mark = (out / "design" if is_product else cwd / "design") / "brand" / "mark.svg"
        if "[logo-fix]" in tag and "look 1 of" in prompt and mark.exists():   # she sees a problem on the first look and fixes it
            mark.write_text(mark.read_text().replace("#2a6", "#26a"))
            result["message"] = "The mark was too generic; redrew it."
        elif "[logo-break]" in tag and "look 1 of" in prompt:     # her edit leaves the mark unusable
            mark.write_text("oops")
            result["message"] = "Redrew the mark."
        else:
            result["message"] = "The logo works; no changes."
    elif role == "designer" and is_product:
        design = out / "design"
        if subject["state"] == "ux_designing":
            (design / "screens" / "S01-dashboard").mkdir(parents=True, exist_ok=True)
            (design / "ux.md").write_text("# UX spec\n\nScreens: S01 dashboard.\n")
            (design / "brand").mkdir(parents=True, exist_ok=True)
            for name in ("logo.svg", "mark.svg"):        # a revision keeps the logo she already fixed
                if not (design / "brand" / name).exists():
                    (design / "brand" / name).write_text("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
                                                        "<circle cx='32' cy='32' r='28' fill='#2a6'/></svg>")
            page = design / "screens" / "S01-dashboard" / "screen.html"
            rev = page.read_text().count("<!-- rev ") if page.exists() else 0
            page.write_text("<h1>Overdue invoices</h1>" + "".join(f"<!-- rev {i} -->" for i in range(rev + 1)))
            (design / "screens" / "S01-dashboard" / "spec.md").write_text(
                "---\nid: S01\nname: Dashboard\npurpose: See what is overdue\n---\n\n## Content and actions\nA table.\n")
            if "[retire]" in tag:
                if (design / "screens" / "S02-extra").exists():
                    result["retire"] = ["S02"]
                else:
                    (design / "screens" / "S02-extra").mkdir()
                    (design / "screens" / "S02-extra" / "screen.html").write_text("<p>extra</p>")
                    (design / "screens" / "S02-extra" / "spec.md").write_text("---\nid: S02\nname: Extra\n---\n")
            if "[bad-layout]" in tag and not (design / "screens" / "S03-broken").exists() and "verdict=changes_requested" not in prompt:
                (design / "screens" / "S03-broken").mkdir()
                (design / "screens" / "S03-broken" / "screen.html").write_text("<p>no spec</p>")
            result["message"] = "UX ready: 1 screen."
        else:
            result["message"] = "OK, a plain table instead of drag-and-drop."
    elif role == "designer":
        if subject["state"] == "clarifying":
            result["message"] = "Use the secondary button style."
        else:
            failing = "[ux-fail]" in tag
            result.update(verdict="fail" if failing else "pass",
                          report="Empty state copy missing." if failing else "Matches the mockups.")
    elif role == "architect" and ("[ux-stubborn]" in tag or ("[ux-concern]" in tag and not answered)):
        result.update(verdict="ux_concern", message="Drag-and-drop board is too costly for the MVP.")
    elif role == "architect" and "Do NOT write the design yet" in prompt:
        result.update(verdict="question", message="Option A: Python + SQLite. Option B: Node + Postgres. I'd pick A. Which one?")
    elif role == "architect" and "going through the drafted tickets" in prompt:
        (out / "tickets.json").write_text(json.dumps([
            {"key": "core", "title": "Core data model", "description": "Tables.", "acceptance_criteria": [], "depends_on": []},
            {"key": "export", "title": "Export to CSV", "description": "Split out of the commands.",
             "acceptance_criteria": ["file written"], "depends_on": ["core"]},
        ]))
        result.update(verdict="done", message="Split the export into its own ticket.")
    elif role == "architect":
        (out / "design.md").write_text("# Design\n\nPython CLI + SQLite.\n")
        (out / "tickets.json").write_text(json.dumps([
            {"key": "core", "title": "Core data model", "description": "Tables.",
             "acceptance_criteria": ["tables exist"], "depends_on": []},
            {"key": "cli", "title": "CLI commands", "description": "Commands. Builds screen S01.",
             "acceptance_criteria": ["cli runs"], "depends_on": ["core"], "ui": True},
        ]))
        result.update(verdict="done", message="Design ready: 2 tickets.")
    elif role == "planner" and "Mode: triage" in prompt:
        comment_count = prompt.count("### [")
        skip_all = "[cr-skip]" in tag
        result.update(fix=[] if skip_all else [1], message="Triaged.",
                      skip=[{"id": i, "reason": "style only"} for i in range(1 if skip_all else 2, comment_count + 1)])
    elif role == "planner":
        (out / "plan.md").write_text(f"# Plan for T-{subject['id']}\n\n1. Create feature file.\n\n"
                                     "## Acceptance criteria\n- feature file exists\n")
        result["message"] = "Use the stdlib csv module." if "Mode: answer" in prompt else "Plan ready."
    elif role == "manager":
        is_ticket = not is_product
        if "[mgr-escalate]" in tag:
            result.update(decision="escalate", message="Needs a product call: which behaviour is right?")
        elif "[mgr-replan]" in tag and is_ticket:
            result.update(decision="replan", message="The plan is wrong; Jim, use the stdlib csv module.")
        elif "[mgr-rule]" in tag or is_product:
            result.update(decision="ruling", next="qa" if is_ticket else None,
                          message="Ruling: ship the simple version; move on.")
        else:
            feature = cwd / f"feature_{subject['id']}.txt"
            feature.write_text((feature.read_text() if feature.exists() else "") + "manager was here\n")
            result.update(decision="fix", message="Root cause: missing header row. Fixed.")
    elif role == "engineer":
        if "[ask]" in tag and not answered:
            result.update(status="question", message="Which CSV library should I use?")
        elif "[ask-designer]" in tag and not answered:
            result.update(status="question", to="designer", message="Which button style for Export?")
        else:
            feature = cwd / f"feature_{subject['id']}.txt"
            feature.write_text((feature.read_text() if feature.exists() else "") + f"{role} was here\n")
    elif role == "qa":
        failing = "[qa-fail]" in tag or "[qa-repeat]" in tag
        result.update(verdict="fail" if failing else "pass",
                      repeated="[qa-repeat]" in tag and "verdict=fail" in prompt,
                      report="Feature file missing behaviour." if failing else "All criteria met.")
    (out / "result.json").write_text(json.dumps(result))
    log_path.write_text(f"[fake {role}] {result}\n", encoding="utf-8")
    return RunOutcome(exit_code=0, cost_usd=0.0)
