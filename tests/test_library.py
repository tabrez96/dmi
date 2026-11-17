"""The design library: layout rules, status, ingestion, and the full lifecycle through HQ."""

import shutil
from pathlib import Path

import pytest
from conftest import scratch_product

from hq import brand, gallery, gitops, library, policy
from hq.orchestrator import HQ

ROOT = Path(__file__).resolve().parents[1]


def write_screen(tree, sid, slug, name="Board", html="<h1>x</h1>", spec=True):
    d = Path(tree) / "screens" / f"{sid}-{slug}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "screen.html").write_text(html)
    if spec:
        (d / "spec.md").write_text(f"---\nid: {sid}\nname: {name}\npurpose: Do the thing\n---\n\n## Content\nx\n")
    return d


# -- unit ---------------------------------------------------------------------------------

def write_logo(tree):
    (Path(tree) / "brand").mkdir(parents=True, exist_ok=True)
    for name in library.BRAND_FILES:
        (Path(tree) / "brand" / name).write_text("<svg viewBox='0 0 8 8'><rect width='8' height='8'/></svg>")










# -- through HQ -------------------------------------------------------------------------------

@pytest.fixture
def hq(tmp_path, monkeypatch):
    for d in ("config", "agents", "company", "tools"):
        shutil.copytree(ROOT / d, tmp_path / d)
    monkeypatch.setenv("DMI_HARNESS", "fake")
    h = HQ(tmp_path)
    h.cfg["discovery"]["enabled"] = False
    h.cfg["ask_stack"] = False
    h.cfg["validate_ideas"] = False
    h.cfg["require_repo"] = False
    h.cfg["safety"]["sandbox"] = "host"
    h.cfg["roles"]["designer"]["logo_review_rounds"] = 0     # an extra designer turn; its own tests turn it on
    scratch_product(h)
    return h


def drain(hq, n=40):
    for _ in range(n):
        if hq.tick() == 0:
            return
    raise AssertionError("did not settle")


def status(hq, pid):
    return {s["id"]: s["status"] for s in hq.screens_for(hq.board.product(pid))}


def test_library_lifecycle_from_draft_to_built(hq):
    pid = hq.discover("tools")
    drain(hq)
    assert hq.board.product(pid)["slug"] is None
    hq.act("product", pid, "approve")                          # brief approved: the repo exists from here on
    repo = hq.design_repo(hq.board.product(pid))
    assert (repo / ".git").exists()
    drain(hq)                                                  # Pam's first turn
    assert status(hq, pid) == {"S01": "draft"}
    log = gitops.git(repo, "log", "--format=%an|%s").stdout
    assert "Pam (Designer)|design:" in log
    assert "| S01 |" in (repo / "design" / "README.md").read_text()

    hq.act("product", pid, "guidance", "S01: make the totals bigger")     # edited in place, same ID
    drain(hq)
    page = repo / "design" / "screens" / "S01-dashboard" / "screen.html"
    assert "<!-- rev 1 -->" in page.read_text()
    assert [s["id"] for s in library.list_screens(repo)] == ["S01"]
    # the HTML is untracked (the repo carries screenshots + specs); an edit with no visible change leaves no commit
    assert gitops.git(repo, "ls-files", "design/screens/S01-dashboard").stdout.split() != []
    assert not [f for f in gitops.git(repo, "ls-files").stdout.split() if f.endswith(".html")]
    assert gitops.git(repo, "log", "--oneline", "--", "design/screens/S01-dashboard").stdout.count("design:") >= 1

    hq.act("product", pid, "approve")                          # UX approved
    assert status(hq, pid) == {"S01": "approved"}
    drain(hq)
    hq.act("product", pid, "approve")                          # design approved: the drafted tickets come up
    hq.act("product", pid, "approve")                          # tickets approved: created, S01 named by one
    assert status(hq, pid) == {"S01": "building"}
    drain(hq)                                                  # the ticket ships
    assert status(hq, pid) == {"S01": "built"}
    readme = (repo / "design" / "README.md").read_text()
    assert "built" in readme and "T-2" in readme and (repo / "design" / "system.md").exists()
    assert gitops.git(repo, "status", "--porcelain").stdout.strip() == ""      # nothing left uncommitted

    entries = hq.gallery_entries()
    index = gallery.gallery_index(entries)
    page_html = gallery.gallery_product(entries[0])
    assert "Invoice Chaser" in index and "S01" in page_html and "Dashboard" in page_html and "built" in page_html
    assert "S01  Dashboard  [built]" in hq.screens_text(pid)


def test_retiring_a_screen_keeps_it(hq):
    pid = hq.discover("[retire] tools")
    drain(hq)
    hq.act("product", pid, "approve")
    drain(hq)
    repo = hq.design_repo(hq.board.product(pid))
    assert {s["id"] for s in library.list_screens(repo)} == {"S01", "S02"}
    hq.act("product", pid, "guidance", "drop the extra screen")
    drain(hq)
    by_id = {s["id"]: s for s in library.list_screens(repo)}
    assert by_id["S02"]["retired"] and (repo / "design" / "screens" / "_retired" / "S02-extra").exists()
    assert "Retired screens" in (repo / "design" / "README.md").read_text()
    assert "S02" not in hq.screens_text(pid)


def test_bad_layout_is_refused_and_explained(hq):
    pid = hq.discover("[bad-layout] tools")
    drain(hq)
    hq.act("product", pid, "approve")
    drain(hq)
    p = hq.board.product(pid)
    repo = hq.design_repo(p)
    assert p["state"] == policy.P_BLOCKED                         # two bad attempts, then the PM is asked
    assert not list((repo / "design" / "screens").glob("S*"))     # nothing broken reached the library
    assert any("spec.md is missing" in m["body"] for m in hq.board.messages("product", pid))


def test_older_product_is_migrated_into_the_library(hq):
    pid = hq.board.create_product("Alterations app")
    hq.board.update_product(pid, state=policy.UX_REVIEW, title="Alterations app", ux="# UX\n\nOld spec.",
                            brief="# Brief")
    scratch = hq.workdir / f"P-{pid}" / ".dmi" / "mockups"
    scratch.mkdir(parents=True)
    (scratch / "board.html").write_text("<title>Order board</title><h1>b</h1>")
    (scratch / "payroll.html").write_text("<h1>Payroll</h1>")
    hq.migrate_designs()
    p = hq.board.product(pid)
    repo = hq.design_repo(p)
    assert p["slug"] == "alterations-app" and (repo / ".git").exists()
    assert [(s["id"], s["name"]) for s in library.list_screens(repo)] == [("S01", "Order board"), ("S02", "Payroll")]
    assert (repo / "design" / "ux.md").read_text().startswith("# UX")
    assert not scratch.exists() and (scratch.parent / "mockups.migrated").exists()
    assert "Order board" in gallery.gallery_product(hq.gallery_entries()[0])


def test_start_design_skips_discovery(hq):
    pid = hq.start_design("MeetingAlert", "A menu-bar app that warns before meetings.")
    p = hq.board.product(pid)
    assert p["state"] == policy.UX_DESIGNING and p["slug"] == "meetingalert" and p["brief"].startswith("A menu-bar")
    drain(hq)
    assert hq.board.product(pid)["state"] == policy.UX_REVIEW                      # Pam delivered, waiting on the PM
    assert [r["role"] for r in hq.board.runs_since(0)] == ["designer"]               # no scout, no analyst
    repo = hq.design_repo(hq.board.product(pid))
    assert "menu-bar" in (repo / "design" / "brief.md").read_text()
    assert [s["id"] for s in library.list_screens(repo)] == ["S01"]


def test_start_design_from_a_ticket(hq):
    tid = hq.board.create("Build macOS app: meeting alerts", "Full-screen warning before meetings.")
    hq.board.update(tid, plan="# Plan\nUse EventKit.")
    pid = hq.start_design(from_ticket=tid)
    p = hq.board.product(pid)
    assert p["title"] == "Build macOS app: meeting alerts"
    assert "Full-screen warning" in p["brief"] and "Use EventKit" in p["brief"] and "design the UX" in p["brief"]
    with pytest.raises(ValueError):
        hq.start_design("No brief")


def test_claude_md_created_never_overwritten_and_stack_filled():
    from hq import claudemd
    text = claudemd.render("Nudge: A Mac menu bar app", "# Brief\n## Problem\nPeople miss meetings.\n")
    assert text.startswith("# Nudge") and "A Mac menu bar app" in text and claudemd.PENDING in text
    design = "# D\n## Overview\nx\n## Stack\nSwift 6, SwiftUI, EventKit.\n## Components\ny\n"
    filled = claudemd.fill_stack(text, design)
    assert "Swift 6, SwiftUI" in filled and claudemd.PENDING not in filled
    assert claudemd.fill_stack(filled, design) is None            # idempotent
    assert claudemd.fill_stack("PM rewrote everything", design) is None   # no markers: leave it alone




def test_commits_are_authored_by_the_crew(hq):
    assert hq._author("designer").startswith("Pam (Designer) <designer@")
    assert hq._author("engineer").startswith("Darryl (Engineer) <engineer@")
    assert hq._author("architect").startswith("Jim (Architect) <architect@")


def test_readme_says_what_the_product_does_and_keeps_the_crews_edits():
    from hq import readme
    brief = "# X\n## Problem\nPeople miss meetings.\n## MVP Scope\n- Alerts before meetings\n- A menu bar icon\n"
    design = "# D\n## Overview\nNudge is a menu bar app.\nIt warns you.\n## Stack\nSwift.\n## Components\nx\n"
    shots = [{"id": "S01", "name": "Menu", "purpose": "See what's next", "png": "design/screens/S01-menu/screen.png"}]
    about = readme.render_about("Nudge: Warns before meetings", "design_review", brief, design, shots)
    assert "# Nudge" in about and "**Warns before meetings**" in about and "Nudge is a menu bar app. It warns you." in about
    assert "- Alerts before meetings" in about and "![S01 Menu](design/screens/S01-menu/screen.png)" in about
    assert "Status" not in about and "## Built with" in about          # no status line in a README
    first = readme.merge("# Nudge\n\nBuilt by Dunder Mifflin agents.\n", about)     # the old stub is replaced
    assert first.rstrip().endswith("Created with ❤️ by Dunder Mifflin agents</sub>") and "## Development" in first
    edited = first.replace("TODO: build and test instructions, added with the first code.", "Run `swift test`.")
    again = readme.merge(edited, readme.render_about("Nudge: Warns before meetings", "building", brief, design, shots))
    assert "Run `swift test`." in again and "Status" not in again   # her edit stays, ours refreshes
    assert readme.merge("My own README\n", about) == "My own README\n"            # no markers: leave it alone






# -- the designer looks at her own logo before the PM does -------------------------------------------

def design_with_looks(hq, title, looks=2):
    hq.cfg["roles"]["designer"]["logo_review_rounds"] = looks
    pid = hq.start_design(title, "A menu-bar app that warns before meetings.")
    drain(hq)
    return pid, hq.design_repo(hq.board.product(pid))


def test_the_designer_looks_at_her_logo_and_leaves_it_when_it_works(hq):
    pid, repo = design_with_looks(hq, "MeetingAlert")
    assert [r["role"] for r in hq.board.runs_since(0)] == ["designer", "designer"]      # the design, then one look
    assert (hq.workdir / f"P-{pid}" / ".dmi" / "logo-preview.png").stat().st_size > 0   # HQ rendered what she looked at
    assert 'fill=\'#2a6\'' in (repo / "design" / "brand" / "mark.svg").read_text()      # unchanged
    assert hq.board.product(pid)["state"] == policy.UX_REVIEW


def test_a_logo_she_fixes_gets_another_look_and_the_fix_is_what_the_pm_sees(hq):
    pid, repo = design_with_looks(hq, "MeetingAlert [logo-fix]")
    assert [r["role"] for r in hq.board.runs_since(0)] == ["designer"] * 3              # draw, look (fixes), look (fine)
    assert "#26a" in (repo / "design" / "brand" / "mark.svg").read_text()
    assert hq.board.product(pid)["state"] == policy.UX_REVIEW


def test_the_number_of_looks_is_capped(hq):
    design_with_looks(hq, "MeetingAlert [logo-fix]", looks=1)
    assert [r["role"] for r in hq.board.runs_since(0)] == ["designer"] * 2              # draw, one look that fixes it


def test_a_look_that_breaks_the_logo_files_is_undone(hq):
    pid, repo = design_with_looks(hq, "MeetingAlert [logo-break]")
    assert "<circle" in (repo / "design" / "brand" / "mark.svg").read_text()            # the broken edit never landed
    assert hq.board.product(pid)["state"] == policy.UX_REVIEW


def test_no_look_when_the_logo_is_unchanged(hq, tmp_path):
    pid, repo = design_with_looks(hq, "MeetingAlert")
    before = len(hq.board.runs_since(0))
    tree = tmp_path / "workspace"
    shutil.copytree(repo / "design", tree)
    assert not brand.brand_differs(tree, repo)                                        # same files as committed
    (tree / "brand" / "mark.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 8'/>")
    assert brand.brand_differs(tree, repo)
    hq._review_logo(hq.board.product(pid), hq.workdir / f"P-{pid}", repo / "design")    # nothing differs from itself
    assert len(hq.board.runs_since(0)) == before








def test_a_merged_design_ticket_doesnt_propose_a_redundant_follow_up(hq):
    import json

    from hq import gitops
    pid = hq.board.create_product("Nudge: warns you before meetings")
    hq.board.update_product(pid, slug="nudge")
    repo = hq.products_dir / "nudge"
    gitops.init_product(repo, "Nudge")
    spec = repo / "design" / "screens" / "S01-menu" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("---\nid: S01\nname: Menu\n---\n")
    gitops.commit_main(repo, "design: S01")
    built = hq.board.create("Build S01", product_id=pid)
    hq.board.update(built, design_pins=json.dumps({"S01": library.screen_version(repo, "design/screens/S01-menu")}))

    def change(text):
        spec.write_text(spec.read_text() + text)
        gitops.commit_main(repo, "design: change S01")
    change("A Now section.\n")                                  # made by a design ticket: its build tickets cover it
    hq.check_design_drift(hq.board.product(pid), propose=False)
    assert [t["id"] for t in hq.board.list(product_id=pid)] == [built]
    change("Something else.\n")                                 # changed some other way: worth a follow-up
    hq.check_design_drift(hq.board.product(pid))
    follow_up = hq.board.list(product_id=pid)[-1]
    assert follow_up["state"] == "proposed" and follow_up["title"].startswith("Update S01")
