"""A product's README.md: what it does (not a placeholder), what it looks like, and where it stands.

HQ owns the block between `<!-- about -->` and `<!-- /about -->` and refreshes it as the brief, design and screens
change. Everything outside it is the crew's: build and test instructions go under "Development" as the code lands.
The sign-off line is always last.
"""
import re

from .claudemd import stack_section
from .refs import split_title

OPEN, CLOSE = "<!-- about -->", "<!-- /about -->"
FOOTER = "<sub>Created with ❤️ by Dunder Mifflin agents</sub>"
DEV_STUB = ("## Development\n\nTODO: build and test instructions, added with the first code.\n")
OLD_STUB = re.compile(r"# [^\n]*\n\nBuilt by Dunder Mifflin agents\.\n?")      # the one-line README HQ used to write


def _section(md, heading):
    found = re.search(rf"^##\s+{heading}[^\n]*\n(.*?)(?=^##\s|\Z)", md or "", re.MULTILINE | re.DOTALL)
    return found.group(1).strip() if found else ""


def _paragraph(text, limit=900):
    first = re.split(r"\n\s*\n", (text or "").strip())[0]
    flat = " ".join(first.split()).replace("the PM's ", "your ").replace("The PM's ", "Your ")
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    return cut[:max(cut.rfind(". "), 0) + 1] if ". " in cut else cut.rsplit(" ", 1)[0] + "…"


def _features(brief, limit=8):
    features = []
    for line in _section(brief, "MVP Scope").splitlines():
        bullet = re.match(r"^\s*[-*]\s+(.*)", line)
        if bullet:
            features.append(re.sub(r"\s+", " ", bullet.group(1)).strip())
    return features[:limit]


def render_about(title, state, brief="", design="", screens=()):
    """The HQ-managed block. `screens`: [{id, name, purpose, png (path relative to the repo)}]."""
    name, tagline = split_title(title)
    lines = [f"# {name}", ""]
    if tagline:
        lines += [f"**{tagline}**", ""]
    what = _paragraph(_section(design, "Overview"))   # the architect writes it for users; a brief is market research, not a README
    if what:
        lines += ["## What it does", "", what, ""]
    features = _features(brief)
    if features:
        lines += ["## Features", ""] + [f"- {feature}" for feature in features] + [""]
    if screens:
        lines += ["## Screens", ""]
        for screen in screens:
            lines += [f"### {screen['id']} · {screen['name']}", ""] + ([screen["purpose"], ""] if screen.get("purpose") else []) + \
                     [f"![{screen['id']} {screen['name']}]({screen['png']})", ""]
    stack = stack_section(design)
    if stack:
        lines += ["## Built with", "", stack, ""]
    return "\n".join(lines).rstrip() + "\n"


def merge(existing, about):
    """The README with its HQ block refreshed. A missing README, or the one-line stub HQ used to write, becomes a
    full one; a README the crew has written is only touched inside its markers (or left alone if it has none)."""
    block = f"{OPEN}\n{about}{CLOSE}"
    if not existing or OLD_STUB.fullmatch(existing):
        return f"{block}\n\n{DEV_STUB}\n---\n\n{FOOTER}\n"
    if OPEN not in existing or CLOSE not in existing:
        return existing
    head, rest = existing.split(OPEN, 1)
    _, tail = rest.split(CLOSE, 1)
    text = f"{head}{block}{tail}"
    if FOOTER not in text:
        text = text.rstrip() + f"\n\n---\n\n{FOOTER}\n"
    return text
