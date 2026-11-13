"""Per-product Discord channels: names, descriptions and layout rules (pure functions, tested).

Every product in the pipeline gets a channel named after the product, whose description is the product's
tagline plus the problem it solves. HQ keeps them in sync as the title and brief change.
"""

import re

from .refs import split_title

RESERVED = {"hq", "needs-you", "crew", "ideas", "general", "water-cooler", "research", "design", "engineering", "black-site", "products", "tickets"}


def is_discovery(product):
    return (product.get("title") or "").lower().startswith("discovery:")


def product_name(product):
    """The product's own name (the title without its tagline); a placeholder while it's still being discovered."""
    if is_discovery(product):
        return f"discovery {product['id']}"
    if (product.get("title") or "").startswith("Untitled idea"):      # not named yet: the PM is still choosing
        return f"idea {product['id']}"
    return split_title(product["title"])[0] or f"product {product['id']}"


def channel_slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:90].strip("-") or "product"


def channel_name(product, taken):
    """A Discord channel name for the product that no other channel has; `taken` = other channels' names."""
    occupied = set(taken) | RESERVED
    slug = channel_slug(product_name(product))
    name, extra = (slug if slug not in occupied else f"{slug}-p{product['id']}"), 1
    while name in occupied:                                     # another product is literally called "ferret-p4"
        extra += 1
        name = f"{slug}-p{product['id']}-{extra}"
    return name


def _plain(md):
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)           # links -> their text
    md = re.sub(r"^\s*[-*•]\s+", "", md, flags=re.MULTILINE)        # bullets
    md = re.sub(r"[*_`#>|]", "", md)
    return " ".join(md.split())


def _clip(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    if stop > limit * 0.5:
        return cut[:stop + 1]
    return text[:limit - 1].rsplit(" ", 1)[0] + "…"            # the ellipsis counts towards the limit too


def summary_from_brief(brief, limit=800):
    """The 'Problem' section of the brief (or, failing that, its opening), as plain text."""
    brief = brief or ""
    found = re.search(r"^##\s+Problem[^\n]*\n(.*?)(?=^##\s|\Z)", brief, re.MULTILINE | re.DOTALL)
    section = found.group(1) if found else re.sub(r"^#\s.*\n", "", brief, count=1)
    return _clip(_plain(section), limit)


def description(product, limit=1000):
    """The channel description: tagline, then what problem the product solves. Discord allows 1024 characters."""
    if is_discovery(product):
        return f"Discovery session: {product.get('focus') or ''}"[:limit]
    tagline = split_title(product["title"])[1]
    summary = summary_from_brief(product.get("brief"), limit - len(tagline) - 4)
    text = f"{tagline} — {summary}" if tagline and summary else (tagline or summary or product["title"])
    return _clip(text, limit)


def lead_for(pcfg, state):
    """Who answers an un-mentioned message in a product channel: the person who owns the product's current stage."""
    return (pcfg.get("lead_by_state") or {}).get(state) or pcfg.get("lead")


def wants_channel(product):
    """Every product has its own channel from the moment it exists (an idea is `idea-5` until it has a name),
    and everything about it lives there: the pipeline thread, its tickets' threads, and chat. Dropped products
    that never got one don't get one."""
    return product.get("state") != "rejected" or bool(product.get("channel_id"))


ORDER = ["discovering", "brief_review", "ux_designing", "ux_review", "designing", "design_review",
         "ticket_review", "building", "ready_to_launch", "launched"]


def _stage(product):
    """Where the product is in ORDER (a stuck product counts as the stage it got stuck at)."""
    state = product["state"]
    if state == "blocked":
        state = product.get("blocked_from") or "ux_designing"
    return ORDER.index(state) if state in ORDER else 2


def checklist(product, tickets, names):
    """The product channel's 'what's next' message. `names`: role -> crew member's first name."""
    crew = {"designer": "Pam", "architect": "Jim", "engineer": "Darryl", "qa": "Dwight", "manager": "Michael", **names}
    title = product_name(product)
    if product["state"] == "rejected":
        return f"**{title}** was dropped. This channel is archived."
    stage = _stage(product)
    live = [ticket for ticket in tickets if ticket["state"] != "rejected"]
    shipped = sum(ticket["state"] == "shipped" for ticket in live)
    build = f"Build: {shipped} of {len(live)} tickets shipped" if live else "Build: tickets are created when you approve them"
    steps = [
        ("Brief approved", stage >= 2, False),
        (f"UX designed by {crew['designer']} (screens in the design library)", stage >= 3, False),
        ("**You:** review and approve the UX", stage >= 4, product["state"] == "ux_review"),
        (f"System design and tickets by {crew['architect']}", stage >= 5, False),
        ("**You:** review and approve the design", stage >= 6, product["state"] == "design_review"),
        (f"**You:** go through the tickets {crew['architect']} drafted, comment or approve", stage >= 7,
         product["state"] == "ticket_review"),
        (f"{build} ({crew['engineer']} builds, {crew['qa']} tests, {crew['designer']} checks screens)", stage >= 8, False),
        ("**You:** approve the launch", stage >= 9, product["state"] == "ready_to_launch"),
    ]
    lines, marked = [f"**Where {title} stands**"], False
    for text, done, waiting in steps:
        mark = "\u2611" if done else "\u2610"
        tail = ""
        if not done and not marked:
            tail, marked = "  \u25c0 now", True
        lines.append(f"{mark} {text}{tail}")
    next_step = {
        "ux_designing": f"{crew['designer']} is designing the UX. You'll get the screens here with a button to approve.",
        "ux_review": f"The UX is ready. Look at the screens, then approve it or tell {crew['designer']} what to change "
                     f"(for example \"S02: ...\").",
        "designing": f"{crew['architect']} is writing the system design and splitting the work into tickets.",
        "design_review": f"The design is ready. Approve it to get the drafted tickets, or tell {crew['architect']} what to change.",
        "ticket_review": f"{crew['architect']} drafted the tickets above. Comment in this thread (split, merge, reorder, drop, "
                         "add) and he revises them, or approve to start building.",
        "building": f"{crew['engineer']} builds ticket by ticket and {crew['qa']} tests them (each ticket has a thread in this channel). "
                    "You're only needed if something gets stuck, then at launch.",
        "ready_to_launch": "Everything has shipped. Approve the launch.",
        "launched": "Launched. \U0001f680",
    }.get(product["state"] if product["state"] != "blocked" else ORDER[stage])
    if product["state"] == "blocked":
        lines.append("\n\u26a0\ufe0f **Stuck: it needs you.** Use the buttons on the latest message.")
    elif next_step:
        lines.append(f"\n**Next:** {next_step}")
    return "\n".join(lines)
