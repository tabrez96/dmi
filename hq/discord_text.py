"""The Discord layer's pure logic, with no network: how a board message reads in Discord, who answers a message,
thread titles, button ids, permissions and the system member's token. `discord_bot` runs the bots on top of it."""

import os
import random
import re
from collections import defaultdict

import discord

from . import policy, refs, team

LIMIT = 1900          # Discord allows 2000 characters per message
PREVIEW = 1400        # longer pipeline bodies get a preview + the full text as a .md file

VERDICT_BADGE = {
    "pass": "✅ **PASS**",
    "fail": "❌ **FAIL**",
    "done": "✅ **Done**",
    "changes_requested": "↩️ **Changes requested**",
    "question": "❓ **Question**",
    "answer": "💡 **Answer**",
    "ready_for_qa": "📦 **Ready for review**",
    "reject_all": "🚫 **Nothing worth building**",
}
STYLES = {"primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
          "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger}
INBOX = "📥"                        # the live "needs you / hanging" message in #needs-you
TICKET_DONE = (policy.SHIPPED, policy.REJECTED)   # merged, or closed without merging: the thread is archived
SYSTEM = "🏢"                       # marks mechanical notices from the system bot
NEEDS_TEXT = {"guidance"}           # these buttons open a text box first
OPTIONAL_TEXT = {"reject"}          # these open one, but you can leave it empty

HQ_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, send_messages_in_threads=True,
    create_public_threads=True, manage_threads=True, manage_channels=True,
    manage_webhooks=True, attach_files=True, embed_links=True, read_message_history=True,
    add_reactions=True, use_application_commands=True,
    manage_roles=True,   # needed to make a channel private (editing who can see it)
    pin_messages=True)   # pins each product channel's checklist
AGENT_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, send_messages_in_threads=True,
    attach_files=True, embed_links=True, read_message_history=True, add_reactions=True,
    change_nickname=True)


def ref(kind, rid):
    return f"{'P' if kind == 'product' else 'T'}-{rid}"


def render(cfg, m, mention=None):
    """A board message -> (display name, content, attachment_text or None).
    `mention(role)` returns a Discord mention for a role's bot, or None."""
    head = []
    if m["verdict"] in VERDICT_BADGE:
        badge = VERDICT_BADGE[m["verdict"]]
        if m["round"] is not None and m["verdict"] in ("fail", "changes_requested", "question"):
            badge += f" · round {m['round']}/{cfg['conversation']['max_rounds']}"
        head.append(badge)
    if m["recipient"] and m["recipient"] not in ("all", "pm") and m["kind"] == "say":
        to = (mention and mention(m["recipient"])) or team.label(cfg, m["recipient"]).split(" · ")[0]
        head.append(f"→ {to}")
    body = m["body"].strip()
    if m["kind"] == "status":
        body = f"*{body}*"
    prefix = " ".join(head)
    text = f"{prefix}\n{body}" if prefix else body
    crew = m["author"] not in ("hq", "pm") and m["kind"] == "say"
    inline = min(LIMIT, (cfg.get("discord") or {}).get("inline_chars", 900)) if crew else LIMIT
    if len(text) <= inline:
        return team.label(cfg, m["author"]), text, None
    cut = text[:min(PREVIEW, inline)]
    preview = cut.rsplit("\n\n", 1)[0] if "\n\n" in cut[len(cut) // 3:] else cut.rsplit("\n", 1)[0]
    return team.label(cfg, m["author"]), f"{preview}\n\n*…full text attached*", m["body"]


def chunks(text, size=LIMIT):
    """Split on line boundaries into Discord-sized messages."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) > size:
            out.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out or [""]


def linkify(cfg, text, ids, extra_mentions=()):
    """'@Rover' -> '<@id>' for crew members with a live bot; append any listed
    mention the text forgot."""
    for key, uid in ids.items():
        text = re.sub(rf"@{team.name(key)}\b", f"<@{uid}>", text, flags=re.IGNORECASE)
    missing = [k for k in extra_mentions if k in ids and f"<@{ids[k]}>" not in text]
    if missing:
        text += "\n" + " ".join(f"<@{ids[k]}>" for k in missing)
    return text


class Rounds:
    """One crew member's own loop guard: how many times *they* have replied in each channel or
    thread since the PM last spoke there. Every member has their own, so one chatty bot reaching
    its limit never pauses the others."""

    def __init__(self):
        self.count = defaultdict(int)     # channel/thread id -> replies since the PM last spoke
        self.paused = set()               # channel/thread ids where the PM was already told

    def bump(self, ch_id):
        self.count[ch_id] += 1

    def reset(self, ch_id):
        self.count[ch_id] = 0
        self.paused.discard(ch_id)

    def at_limit(self, ch_id, limit):
        return self.count[ch_id] >= limit

    def newly_paused(self, ch_id):
        """True the first time this member is paused in a channel (so the PM is told once)."""
        if ch_id in self.paused:
            return False
        self.paused.add(ch_id)
        return True


def route(cfg, channel, author, mentioned, rng=random):
    """Who should answer a message in a team channel.

    author: "pm" or a member key. mentioned: member keys @mentioned (not the author).
    The caller then drops anyone who has used up their own rounds."""
    if author != "pm":
        return [k for k in mentioned if k != author][:3]
    if mentioned:
        return mentioned[:3]
    ch = cfg["channels"].get(channel)
    if not ch:
        return []
    members = team.channel_members(cfg, channel)
    lead = ch.get("lead")
    if lead == "random" or lead not in cfg["team"]:
        return [rng.choice(members)] if members else []
    return [lead]


def chat_thread_name(text, fallback="Chat"):
    """A readable thread title from the message that started it."""
    t = re.sub(r"<@!?\d+>", "", text or "")              # drop raw mentions
    t = " ".join(t.split()).strip(" -:,.") or fallback
    return t if len(t) <= 80 else t[:77].rsplit(" ", 1)[0] + "…"


def allowed_targets(cfg, channel, targets):
    """In a private channel only its members can be asked to answer; anywhere else, everyone can."""
    ch = cfg["channels"].get(channel)
    if ch and ch.get("private"):
        members = set(team.channel_members(cfg, channel))
        return [k for k in targets if k in members]
    return list(targets)


def private_plan(cfg, channel, ids, system_key):
    """{bot user id: may see the channel}. Members and the system bot (which routes messages) may; every
    other crew bot is denied explicitly, because their roles would otherwise let them in."""
    members = set(team.channel_members(cfg, channel))
    return {uid: (key in members or key == system_key) for key, uid in ids.items()}


def thread_title(kind, row):
    if kind == "product":     # an approved product's title is already "Name: tagline"; ideas get their I-number
        return (row["title"] if row.get("slug") else f"{refs.product_ref(row)} · {row['title']}")[:100]
    return f"T-{row['id']} · {row['title']}"[:100]


def channel_thread_title(row):
    """A ticket's thread inside a product channel has no tags, so its state is a mark in the title."""
    state = row["state"]
    mark = ("✅" if state == policy.SHIPPED else "🚫" if state == policy.REJECTED else "⛔" if state == policy.BLOCKED
            else "🔔" if state in policy.NEEDS_PM else "🔧")
    return f"{mark} T-{row['id']} · {row['title']}"[:100]


def custom_id(kind, rid, action):
    return f"dmi|{kind}|{rid}|{action}"


def parse_custom_id(cid):
    parts = cid.split("|")
    if len(parts) != 4 or parts[0] != "dmi":
        return None
    return parts[1], int(parts[2]), parts[3]


def tag_name(state):
    return state.replace("_", " ")


def invite_url(app_id, perms):
    return discord.utils.oauth_url(app_id, permissions=perms, scopes=("bot", "applications.commands"))


def system_member(cfg):
    key = cfg.get("discord", {}).get("system_member")
    return key if key in cfg["team"] else None


def system_token(cfg):
    key = system_member(cfg)
    return (team.token(key) if key else None) or os.environ.get("DISCORD_TOKEN")
