"""The design library: every product's screens as files in its own git repo, plus a generated index.

Layout inside a product repo (all of it committed, so history shows what changed and when):

  design/
    README.md                  generated index: ID, name, status, purpose, tickets, last changed
    brief.md  ux.md  system.md the brief, the UX spec (flows, design system), the system design
    brand/
      logo.svg  mark.svg       the logo (mark + name) and the mark alone, drawn as SVG; logo.png is rendered
    screens/
      S01-board/
        screen.html            the mockup: self-contained HTML, the editable source of truth
        spec.md                front matter (id, name, purpose) + content, actions, states, change log
        states/empty.html ...  optional extra states
      _retired/S04-old/        screens taken out of the plan are moved here, never deleted

Screen IDs (S01, S02, ...) are stable: they're how the PM asks for changes ("S02: move the chips under
the title") and how tickets name the screens they build. Status is computed, never stored by hand.
"""

import re
import shutil
import subprocess
from pathlib import Path

import yaml

ID_RE = re.compile(r"^(S\d{2,3})-([a-z0-9][a-z0-9-]*)$")
REF_RE = re.compile(r"\bS\d{2,3}\b")
RETIRED = "_retired"
MAX_HTML_BYTES = 600_000
SCREEN_FILES = ("screen.html", "screen.svg")     # a screen's source: HTML (default) or a static SVG


def design_dir(repo):
    return Path(repo) / "design"


def screens_dir(repo):
    return design_dir(repo) / "screens"


def slugify(text, fallback="screen"):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40].strip("-") or fallback


# -- reading ----------------------------------------------------------------------------

def parse_spec(path):
    """(front matter dict, body) of a spec.md."""
    spec = Path(path)
    text = spec.read_text() if spec.exists() else ""
    front = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
    meta = {}
    if front:
        try:
            loaded = yaml.safe_load(front.group(1))
            meta = loaded if isinstance(loaded, dict) else {}
        except yaml.YAMLError:
            meta = {}
    return meta, (text[front.end():] if front else text)


def screen_file(folder):
    """The screen's source file inside its folder, or None."""
    return next((Path(folder) / name for name in SCREEN_FILES if (Path(folder) / name).exists()), None)


CHROME_GLOB = "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
DEFAULT_SIZE = (1280, 1000)
NO_SCRIPTS = "<meta http-equiv=\"Content-Security-Policy\" content=\"script-src 'none'\">"


def chrome_path():
    import glob
    import os
    found = sorted(glob.glob(os.path.expanduser(CHROME_GLOB)))
    return found[-1] if found else None


def screen_size(meta):
    """(width, height) from a spec's `size: 390x844` front matter, else a desktop window."""
    size = re.match(r"^\s*(\d{2,4})\s*[x×]\s*(\d{2,4})\s*$", str(meta.get("size") or ""))
    return (int(size.group(1)), int(size.group(2))) if size else DEFAULT_SIZE


def render_png(html_file, png_file, size=DEFAULT_SIZE):
    """Screenshot a screen's HTML with headless Chromium. The page is agent-written, so it is rendered from a copy
    with scripts and event handlers stripped (mockups are static) and with no network (every hostname fails to
    resolve). Returns True if a PNG was written."""
    chrome = chrome_path()
    if not chrome:
        return False
    import tempfile
    png_file = Path(png_file)
    tmp = png_file.with_suffix(".tmp.png")
    src = Path(html_file).read_text(errors="replace")
    src = re.sub(r"<script\b.*?</script\s*>", "", src, flags=re.DOTALL | re.IGNORECASE)
    src = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", src, flags=re.IGNORECASE)
    src = re.sub(r"javascript:", "", src, flags=re.IGNORECASE)
    # The stripping above is plain text, so an encoded script (an iframe's srcdoc) gets past it; this policy is what
    # stops scripts running, in the page and in every frame inside it. After the doctype, so the page keeps it.
    src = re.sub(r"^(\s*<!doctype[^>]*>)?", lambda found: (found.group(1) or "") + NO_SCRIPTS, src, count=1,
                 flags=re.IGNORECASE)
    try:
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "screen.html"
            page.write_text(src)
            subprocess.run([chrome, "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                            "--host-resolver-rules=MAP * ~NOTFOUND", "--run-all-compositor-stages-before-draw",
                            f"--window-size={size[0]},{size[1]}", f"--screenshot={tmp}", page.as_uri()],
                           check=False, capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        tmp.unlink(missing_ok=True)
        return False
    if tmp.exists() and tmp.stat().st_size:
        tmp.replace(png_file)
        return True
    tmp.unlink(missing_ok=True)
    return False


PAGE_FILE = "page.md"      # design/page.md: the product's public page on the website, in this template
PAGE_TEMPLATE = """# <Name>

## The problem
<2-3 sentences: the everyday problem, as the people who have it would describe it.>

## How it helps
<2-3 sentences or up to 4 bullets: what the product does about it. Plain words, no tech.>

## Who it's for
<1-2 sentences or up to 3 bullets: the kind of people it's made for.>
"""


def copy_sources(from_repo, to_repo, overwrite=False):
    """Copy the screens' HTML sources (kept out of git, so a ticket's checkout doesn't have them) from one checkout to
    another, keeping their file times so only what gets edited is rendered again. Only into screen folders that exist
    there; `overwrite=False` keeps what is already there (the designer's edits). Returns the number copied."""
    source, target = design_dir(from_repo), design_dir(to_repo)
    copied = 0
    for source_file in sorted(source.glob("screens/**/*.html")):
        rel = source_file.relative_to(source)
        dest = target / rel
        if (target / rel.parts[0] / rel.parts[1]).is_dir() and (overwrite or not dest.exists()):  # screens/S01-x exists
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, dest)
            copied += 1
    return copied


def has_sources(tree):
    """True if every live screen in a design/ folder has its source (HTML or SVG), so the screens can be checked."""
    base = Path(tree) / "screens"
    folders = [folder for folder in base.glob("S*-*") if folder.is_dir()] if base.is_dir() else []
    return all(screen_file(folder) for folder in folders)


def render_screens(repo):
    """PNGs next to every screen's source (screen.png, states/<name>.png), only where the HTML is newer.
    Returns the number rendered."""
    rendered = 0
    for screen in list_screens(repo):
        if screen["retired"]:
            continue
        src = screen["path"] / screen["file"]
        if src.suffix != ".html":
            continue
        spec = screen["path"] / "spec.md"
        size = screen_size(parse_spec(spec)[0])
        spec_time = spec.stat().st_mtime if spec.exists() else 0          # its size: line changes the screenshot too
        jobs = [(src, screen["path"] / "screen.png")] + [(state, state.with_suffix(".png"))
                                                         for state in sorted((screen["path"] / "states").glob("*.html"))]
        for html_file, png in jobs:
            if not png.exists() or png.stat().st_mtime < max(html_file.stat().st_mtime, spec_time):
                rendered += render_png(html_file, png, size)
    return rendered


def _git_date(repo, rel):
    found = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%cs", "--", rel],
                           check=False, capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 else ""


def list_screens(repo):
    """Every screen in a product's library (live ones first, then retired), as dicts."""
    base, screens = screens_dir(repo), []
    for retired, parent in ((False, base), (True, base / RETIRED)):
        for folder in sorted(parent.glob("S*-*")) if parent.is_dir() else []:
            named = ID_RE.match(folder.name)
            if not folder.is_dir() or not named:
                continue
            meta, body = parse_spec(folder / "spec.md")
            states_dir = folder / "states"
            states = sorted(state.name for state in states_dir.glob("*.html")) if states_dir.is_dir() else []
            rel = str(folder.relative_to(repo))
            src = screen_file(folder)
            screens.append({"id": named.group(1), "dir": folder.name, "path": folder, "rel": rel, "retired": retired,
                            "file": src.name if src else "screen.html",
                            "name": str(meta.get("name") or named.group(2).replace("-", " ").title()),
                            "purpose": " ".join(str(meta.get("purpose") or "").split()),
                            "body": body, "states": states, "updated": _git_date(repo, rel)})
    return screens


def screen_version(repo, rel):
    """The commit that last changed a screen's files (screenshots excluded: they only follow the HTML)."""
    found = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%h", "--", rel, ":(exclude)*.png"],
                           check=False, capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 else ""


def ticket_refs(ticket):
    return set(REF_RE.findall(f"{ticket.get('title', '')}\n{ticket.get('description', '')}"))


def status_of(screen, tickets, ux_approved):
    """retired | built | building | approved | draft: computed from the board, never typed by hand."""
    if screen["retired"]:
        return "retired"
    # only work that was approved: a rejected ticket, or one only proposed, builds nothing
    mine = [ticket for ticket in tickets
            if screen["id"] in ticket_refs(ticket) and ticket["state"] not in ("rejected", "proposed")]
    if mine:
        return "built" if all(ticket["state"] == "shipped" for ticket in mine) else "building"
    return "approved" if ux_approved else "draft"


def annotate(screens, tickets, ux_approved):
    for screen in screens:
        screen["status"] = status_of(screen, tickets, ux_approved)
        screen["tickets"] = sorted(ticket["id"] for ticket in tickets
                                   if screen["id"] in ticket_refs(ticket) and ticket["state"] != "rejected")
    return screens


# -- checking and importing a designer's workspace -----------------------------------------

BRAND_FILES = ("logo.svg", "mark.svg")
MAX_SVG_BYTES = 100_000


def brand_problems(tree):
    """What's wrong with the logo files in `brand/` (the logo is part of every product's design)."""
    errors = []
    for name in BRAND_FILES:
        svg = Path(tree) / "brand" / name
        text = svg.read_text(errors="replace") if svg.is_file() else ""
        if "<svg" not in text:
            errors.append(f"brand/{name} is missing (draw the logo as an SVG: logo.svg is the mark with the name, "
                          "mark.svg the mark alone)")
        elif re.search(r"<script\b|\son\w+\s*=|<image\b|javascript:", text, re.IGNORECASE):
            errors.append(f"brand/{name} must be plain SVG shapes and text: no scripts, handlers or images")
        elif len(text.encode()) > MAX_SVG_BYTES:
            errors.append(f"brand/{name} is over {MAX_SVG_BYTES // 1000} KB")
    return errors


def validate_tree(tree, screens=True):
    """Problems with a designer's `design/` workspace (empty list = fine). `screens=False` checks only the logo and the
    UX spec: a design ticket works in a git checkout, where screens carry their screenshots and specs but not their
    source HTML (that stays in the designer's workspace), so they can't be checked there."""
    tree, errors = Path(tree), []
    errors += brand_problems(tree)
    if not (tree / "ux.md").exists() or not (tree / "ux.md").read_text().strip():
        errors.append("ux.md is missing or empty")
    if not screens:
        return errors
    seen = {}
    base = tree / "screens"
    for folder in sorted(base.iterdir()) if base.is_dir() else []:
        if not folder.is_dir() or folder.name == RETIRED:
            continue
        named = ID_RE.match(folder.name)
        if not named:
            errors.append(f"screens/{folder.name}: folder names look like S01-board (ID, dash, lowercase words)")
            continue
        screen_id = named.group(1)
        if screen_id in seen:
            errors.append(f"{folder.name}: ID {screen_id} is already used by {seen[screen_id]}")
        seen[screen_id] = folder.name
        src = screen_file(folder)
        if src is None or not src.read_text().strip():
            errors.append(f"{folder.name}: screen.html (or screen.svg) is missing or empty")
        elif src.stat().st_size > MAX_HTML_BYTES:
            errors.append(f"{folder.name}: {src.name} is over {MAX_HTML_BYTES // 1000} KB")
        if not (folder / "spec.md").exists():
            errors.append(f"{folder.name}: spec.md is missing")
        else:
            meta, _ = parse_spec(folder / "spec.md")
            if meta.get("id") and str(meta["id"]) != screen_id:
                errors.append(f"{folder.name}: spec.md says id {meta['id']}, the folder says {screen_id}")
    return errors


def same_file(one, other):
    return one.exists() and other.exists() and one.read_bytes() == other.read_bytes()


def ingest(src, repo, retire=()):
    """Copy a validated designer workspace into the repo's design/ folder.

    Additive: a screen the designer left out of the workspace is kept. Only IDs listed in `retire`
    are taken out of the plan, and those are moved to screens/_retired/ (history keeps everything)."""
    src, dest = Path(src), design_dir(repo)
    base = dest / "screens"
    base.mkdir(parents=True, exist_ok=True)
    result = {"new": [], "changed": [], "retired": [], "ux_changed": False, "brand_changed": False}
    if (src / "brand").is_dir():
        for svg in sorted((src / "brand").glob("*.svg")):
            if not same_file(svg, dest / "brand" / svg.name):
                (dest / "brand").mkdir(parents=True, exist_ok=True)
                shutil.copy2(svg, dest / "brand" / svg.name)
                result["brand_changed"] = True
    if (src / "ux.md").exists() and not same_file(src / "ux.md", dest / "ux.md"):
        shutil.copy2(src / "ux.md", dest / "ux.md")
        result["ux_changed"] = True
    for folder in sorted((src / "screens").iterdir()) if (src / "screens").is_dir() else []:
        if not folder.is_dir() or folder.name == RETIRED or not ID_RE.match(folder.name):
            continue
        target = base / folder.name
        renamed = next((old for old in base.glob(f"{ID_RE.match(folder.name).group(1)}-*")
                        if old.is_dir() and old != target), None)
        if renamed and not target.exists():          # the same screen under a new name: one screen per ID
            renamed.rename(target)
        existed = target.exists()
        files = [file for file in folder.rglob("*") if file.is_file()]
        differs = (not existed) or any(not same_file(file, target / file.relative_to(folder)) for file in files)
        if existed:                                   # remove files the designer deleted inside this screen
            for old in [file for file in target.rglob("*") if file.is_file()]:
                if not (folder / old.relative_to(target)).exists():
                    old.unlink()
                    differs = True
        shutil.copytree(folder, target, dirs_exist_ok=True)
        if differs:
            result["changed" if existed else "new"].append(ID_RE.match(folder.name).group(1))
    for screen_id in retire:
        screen_id = str(screen_id).strip().upper()
        match = next((folder for folder in base.glob(f"{screen_id}-*") if folder.is_dir()), None)
        if match:
            (base / RETIRED).mkdir(exist_ok=True)
            shutil.rmtree(base / RETIRED / match.name, ignore_errors=True)   # retired again: git history has the old one
            shutil.move(str(match), str(base / RETIRED / match.name))
            result["retired"].append(screen_id)
    return result


def legacy_to_screens(mockups_dir, dest_design, note="Migrated from a flat mockup file."):
    """Convert an old flat folder of mockups (board.html, order.html ...) into numbered screens."""
    made = []
    for i, mockup in enumerate(sorted(Path(mockups_dir).glob("*.html")), 1):
        screen_id, slug = f"S{i:02d}", slugify(mockup.stem)
        folder = Path(dest_design) / "screens" / f"{screen_id}-{slug}"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mockup, folder / "screen.html")
        heading = re.search(r"<title>(.*?)</title>|<h1[^>]*>(.*?)</h1>", mockup.read_text(), re.DOTALL | re.IGNORECASE)
        title = re.sub(r"<[^>]+>", "", (heading.group(1) or heading.group(2)) if heading else "").strip()
        title = re.split(r"\s+[\u2014\u2013|]\s+", title)[0].strip() or slug.replace("-", " ").title()   # "Board \u2014 AppName" -> "Board"
        meta = {"id": screen_id, "name": title[:80],
                "purpose": "Not written yet; the designer fills this in on her next pass."}
        (folder / "spec.md").write_text(f"---\n{yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)}---\n\n"
                                        f"{note}\nSee `design/ux.md` for the flows this screen belongs to.\n")
        made.append(screen_id)
    return made
