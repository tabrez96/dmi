"""What every product repo and channel gets: CLAUDE.md, the README's about block, and the Discord channel's name,
description and checklist."""

from hq import claudemd, pchannels, readme

BRIEF = """# Brief

## Problem
Card statements arrive as **password-protected PDFs** and nobody reads them. See [the survey](https://x.test).

## MVP Scope
- Upload a statement
- Totals   per currency
"""
DESIGN = """## Overview
Ferret reads your card statements so you can see where the money went.

## Stack
| Layer | Choice |
| Web | FastAPI |

## Screens
S01
"""


def test_claudemd_has_the_product_and_fills_the_stack_and_canvas_later():
    text = claudemd.render("Ferret: card spending, no spreadsheets", BRIEF)
    assert text.startswith("# Ferret\n\ncard spending, no spreadsheets\n")
    assert "password-protected PDFs and nobody reads them. See the survey." in text
    assert claudemd.PENDING in text and "not published yet" in text
    filled = claudemd.fill_stack(text, DESIGN)
    assert "| Web | FastAPI |" in filled and claudemd.PENDING not in filled
    assert claudemd.fill_stack(filled, DESIGN) is None                  # nothing new: no change
    assert "https://claude.ai/artifact/1" in claudemd.fill_canvas(filled, "https://claude.ai/artifact/1")
    assert claudemd.fill_canvas("no markers here", "https://claude.ai/artifact/1") is None


def test_readme_about_block_is_refreshed_and_the_crews_text_kept():
    about = readme.render_about("Ferret: card spending", "building", BRIEF, DESIGN,
                                [{"id": "S01", "name": "Home", "purpose": "", "png": "design/screens/S01/screen.png"}])
    assert "## What it does\n\nFerret reads your card statements" in about
    assert "- Totals per currency" in about and "![S01 Home](design/screens/S01/screen.png)" in about
    assert "## Built with" in about
    fresh = readme.merge("", about)
    assert fresh.startswith(readme.OPEN) and fresh.rstrip().endswith(readme.FOOTER) and readme.DEV_STUB in fresh
    edited = fresh.replace(readme.DEV_STUB, "## Development\n\nuv run pytest\n")
    refreshed = readme.merge(edited, readme.render_about("Ferret: card totals", "building"))
    assert "card totals" in refreshed and "uv run pytest" in refreshed and "Upload a statement" not in refreshed
    assert readme.merge("# Hand written, no markers", about) == "# Hand written, no markers"
    assert readme.merge("# Ferret\n\nBuilt by Dunder Mifflin agents.\n", about).startswith(readme.OPEN)   # the old stub
    kept = "# Ferret\n\nBuilt by Dunder Mifflin agents.\n\nRun `make`.\n"
    assert readme.merge(kept, about) == kept                            # the stub plus someone's notes


def test_channel_names_avoid_reserved_and_taken_ones():
    product = {"id": 4, "title": "Ferret: card spending", "state": "building"}
    assert pchannels.channel_name(product, []) == "ferret"
    assert pchannels.channel_name(product, ["ferret"]) == "ferret-p4"
    assert pchannels.channel_name(product, ["ferret", "ferret-p4"]) == "ferret-p4-2"
    assert pchannels.channel_name({"id": 5, "title": "Design: tools"}, []) == "design-p5"
    assert pchannels.channel_name({"id": 6, "title": "Untitled idea"}, []) == "idea-6"
    assert pchannels.channel_name({"id": 7, "title": "Discovery: receipts"}, []) == "discovery-7"


def test_channel_description_is_the_tagline_then_the_problem():
    product = {"id": 4, "title": "Ferret: card spending", "brief": BRIEF}
    text = pchannels.description(product)
    assert text.startswith("card spending — Card statements arrive as password-protected PDFs")
    assert "**" not in text and "](" not in text
    assert len(pchannels.description({**product, "brief": "## Problem\n" + "word " * 400}, 200)) <= 200
    unbroken = pchannels.description({"id": 4, "title": "x" * 201}, 200)
    assert len(unbroken) == 200 and unbroken.endswith("…")


def test_checklist_marks_where_the_product_is_and_what_is_next():
    tickets = [{"state": "shipped"}, {"state": "approved"}, {"state": "rejected"}]
    text = pchannels.checklist({"id": 4, "title": "Ferret: x", "state": "building"}, tickets, {"engineer": "Darryl"})
    assert "Build: 1 of 2 tickets shipped" in text and "◀ now" in text.split("Build:")[1].split("\n")[0]
    assert "**Next:** Darryl builds ticket by ticket" in text and "thread in this channel" in text
    stuck = pchannels.checklist({"id": 4, "title": "Ferret: x", "state": "blocked", "blocked_from": "ux_review"}, [], {})
    assert "Stuck: it needs you" in stuck and "**Next:**" not in stuck
    assert "dropped" in pchannels.checklist({"id": 4, "title": "Ferret: x", "state": "rejected"}, [], {})
