"""The design library on its own: layout rules, status from the board, ingesting a designer's workspace, sizes."""

import shutil
from pathlib import Path

from hq import gitops, library


def write_screen(tree, screen_id, slug, name="Board", html="<h1>x</h1>", spec=True):
    folder = Path(tree) / "screens" / f"{screen_id}-{slug}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "screen.html").write_text(html)
    if spec:
        (folder / "spec.md").write_text(f"---\nid: {screen_id}\nname: {name}\npurpose: Do the thing\n---\n\n## Content\nx\n")
    return folder


def write_logo(tree):
    (Path(tree) / "brand").mkdir(parents=True, exist_ok=True)
    for name in library.BRAND_FILES:
        (Path(tree) / "brand" / name).write_text("<svg viewBox='0 0 8 8'><rect width='8' height='8'/></svg>")


def test_validate_tree(tmp_path):
    write_logo(tmp_path)
    assert library.validate_tree(tmp_path) == ["ux.md is missing or empty"]
    (tmp_path / "ux.md").write_text("# UX")
    write_screen(tmp_path, "S01", "board")
    assert library.validate_tree(tmp_path) == []
    write_screen(tmp_path, "S02", "no-spec", spec=False)
    (tmp_path / "screens" / "Dashboard").mkdir()
    write_screen(tmp_path, "S01", "duplicate")
    errors = " | ".join(library.validate_tree(tmp_path))
    assert "S02-no-spec: spec.md is missing" in errors and "Dashboard" in errors and "ID S01 is already used" in errors
    bad = write_screen(tmp_path, "S09", "mismatch")
    (bad / "spec.md").write_text("---\nid: S10\n---\n")
    assert "spec.md says id S10" in " | ".join(library.validate_tree(tmp_path))



def test_ingest_is_additive_and_retires_only_by_id(tmp_path):
    repo, work = tmp_path / "repo", tmp_path / "work"
    gitops.init_product(repo)
    work.mkdir()
    (work / "ux.md").write_text("# UX")
    write_screen(work, "S01", "board")
    write_screen(work, "S02", "order")
    assert library.ingest(work, repo)["new"] == ["S01", "S02"]
    assert library.ingest(work, repo) == {"new": [], "changed": [], "retired": [], "ux_changed": False,
                                          "brand_changed": False}
    shutil.rmtree(work / "screens" / "S02-order")                 # the designer "forgot" S02
    write_screen(work, "S01", "board", html="<h1>edited</h1>")
    result = library.ingest(work, repo)
    assert result["changed"] == ["S01"] and (library.screens_dir(repo) / "S02-order").exists()   # not deleted
    result = library.ingest(work, repo, retire=["S02", "S99"])
    assert result["retired"] == ["S02"]                           # unknown IDs are ignored
    assert (library.screens_dir(repo) / "_retired" / "S02-order" / "screen.html").exists()
    screens = {screen["id"]: screen for screen in library.list_screens(repo)}
    assert screens["S02"]["retired"] and not screens["S01"]["retired"] and screens["S01"]["name"] == "Board"
    write_screen(work, "S02", "order")
    library.ingest(work, repo, retire=["S02"])                    # retired twice: replaced, not nested
    assert not (library.screens_dir(repo) / "_retired" / "S02-order" / "S02-order").exists()
    shutil.move(work / "screens" / "S01-board", work / "screens" / "S01-kanban")
    library.ingest(work, repo)                                    # renamed: still one S01
    assert [screen["dir"] for screen in library.list_screens(repo) if screen["id"] == "S01"] == ["S01-kanban"]


def test_status_is_computed_from_tickets():
    screen = {"id": "S02", "retired": False}

    def ticket(state, text):
        return {"id": 1, "state": state, "title": text, "description": ""}
    assert library.status_of(screen, [], False) == "draft" and library.status_of(screen, [], True) == "approved"
    assert library.status_of(screen, [ticket("in_qa", "Builds S02")], True) == "building"
    assert library.status_of(screen, [ticket("shipped", "Builds S02"), ticket("approved", "More S02")], True) == "building"
    assert library.status_of(screen, [ticket("shipped", "Builds S02"), ticket("rejected", "S02 idea")], True) == "built"
    assert library.status_of({"id": "S02", "retired": True}, [], True) == "retired"
    assert library.status_of(screen, [ticket("shipped", "Builds S20")], True) == "approved"      # S20 is not S02


def test_legacy_mockups_become_numbered_screens(tmp_path):
    old = tmp_path / "mockups"
    old.mkdir()
    (old / "board.html").write_text("<title>Order board</title><h1>x</h1>")
    (old / "new-order.html").write_text("<h1>New order</h1>")
    dest = tmp_path / "design"
    assert library.legacy_to_screens(old, dest) == ["S01", "S02"]
    write_logo(dest)
    assert library.validate_tree(dest) == ["ux.md is missing or empty"]       # screens themselves are valid
    meta, _ = library.parse_spec(dest / "screens" / "S01-board" / "spec.md")
    assert meta["id"] == "S01" and meta["name"] == "Order board"


def test_screen_size_from_spec_and_script_stripping(tmp_path):
    assert library.screen_size({"size": "390x844"}) == (390, 844)
    assert library.screen_size({"size": "1440 × 900"}) == (1440, 900)
    assert library.screen_size({}) == library.DEFAULT_SIZE
    assert library.screen_size({"size": "huge"}) == library.DEFAULT_SIZE
    if library.chrome_path():
        page = tmp_path / "page.html"
        page.write_text("<!doctype html><body onload=\"document.body.remove()\"><script>document.body.remove()</script>"
                        "<h1>Hi</h1></body>")
        assert library.render_png(page, tmp_path / "page.png", (300, 200)) and (tmp_path / "page.png").stat().st_size


def test_screen_sources_go_to_a_design_ticket_and_her_edits_come_back(tmp_path):
    product, ticket = tmp_path / "product", tmp_path / "ticket"
    for checkout in (product, ticket):
        (checkout / "design" / "screens" / "S01-menu" / "states").mkdir(parents=True)
        (checkout / "design" / "screens" / "S01-menu" / "spec.md").write_text("---\nid: S01\n---\n")
    (product / "design" / "screens" / "S01-menu" / "screen.html").write_text("<p>old</p>")
    (product / "design" / "screens" / "S01-menu" / "states" / "empty.html").write_text("<p>empty</p>")
    shutil.rmtree(ticket / "design" / "screens" / "S01-menu" / "states")       # a state folder is made when needed
    assert not library.has_sources(ticket / "design")
    assert library.copy_sources(product, ticket) == 2 and library.has_sources(ticket / "design")
    edited = ticket / "design" / "screens" / "S01-menu" / "screen.html"
    edited.write_text("<p>new</p>")
    assert library.copy_sources(product, ticket) == 0 and edited.read_text() == "<p>new</p>"   # her edit is kept
    library.copy_sources(ticket, product, overwrite=True)                                      # merged: it comes back
    assert (product / "design" / "screens" / "S01-menu" / "screen.html").read_text() == "<p>new</p>"


def test_screenshots_run_no_scripts_even_encoded_ones(tmp_path, monkeypatch):
    pages = []
    monkeypatch.setattr(library, "chrome_path", lambda: "chrome")
    monkeypatch.setattr(library.subprocess, "run", lambda args, **kw: pages.append(Path(args[-1][7:]).read_text()))
    (tmp_path / "page.html").write_text("<!doctype html><iframe srcdoc='&lt;script&gt;x()&lt;/script&gt;'></iframe>")
    library.render_png(tmp_path / "page.html", tmp_path / "page.png")
    assert pages[0].startswith("<!doctype html>" + library.NO_SCRIPTS)     # the policy covers the frame's script too
