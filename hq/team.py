"""The crew: named members, the roles they play, and how they appear."""

import os


def members(cfg):
    return cfg["team"]


def name(key):
    return key.title()


def member_for_role(cfg, role):
    for key, member in cfg["team"].items():
        if role in member["roles"]:
            return key
    return None


def chat_role(cfg, key):
    """The role whose model and charter a member uses when chatting."""
    return cfg["team"][key]["roles"][0]


def label(cfg, author):
    """'Rover · Engineer' for a role or member key; 'Dunder Mifflin HQ' / 'PM' otherwise."""
    if author == "hq":
        return "Dunder Mifflin HQ"
    if author == "pm":
        return "PM"
    key = author if author in cfg["team"] else member_for_role(cfg, author)
    if not key:
        return author.title()
    return f"{name(key)} · {cfg['team'][key]['title']}"


def by_name(cfg, text):
    token = text.strip().lstrip("@").lower()
    return token if token in cfg["team"] else None


def token_env(key):
    return f"DISCORD_TOKEN_{key.upper()}"


def token(key):
    return os.environ.get(token_env(key))


def roster(cfg):
    return ", ".join(f"{name(key)} ({member['title']})" for key, member in cfg["team"].items())


CHARACTER_RULES = (
    "Every message you write should sound unmistakably like your character: the way you open, your word choices, a "
    "signature phrase or a swipe, a closing line. The character is in how you say things, never in what is true: facts, "
    "verdicts, numbers, file names and decisions stay exact, and the point still comes first. Never insult or talk down "
    "to the PM. Jabs at teammates are affectionate and about the situation, never about who someone is. Keep it PG: no "
    "innuendo, nothing off-colour. If something is broken or urgent, say it straight first, then say it in your voice. If your character is funny, be actually funny: "
    "a flat, professional message is a failure.")


def sample_lines(cfg, key):
    """Example lines of how a crew member sounds (config `character_examples`), as a prompt block."""
    lines = (cfg.get("character_examples") or {}).get(key) or []
    if not lines:
        return ""
    return ("\n\nHow you sound (examples of your style, about other situations). Never reuse an example, its opening or "
            "its closing line, and never open two messages the same way: invent a fresh bit each time.\n"
            + "\n".join(f'- "{line}"' for line in lines))


def identity(cfg, role, turn="pipeline"):
    """Who an agent is, for its system prompt: its name and job, its teammates, what to call the PM and its character.
    turn="chat": the character is played up in the whole reply. turn="pipeline": only the Discord `message` gets the
    voice; everything the agent writes to files stays plain. Config `chat.character: false` turns the character down
    to a light touch."""
    key = member_for_role(cfg, role)
    if not key:
        return ""
    member = cfg["team"][key]
    text = (f"## Who you are\nYou are {name(key)}, Dunder Mifflin's {member['title']}. Your teammates: "
            f"{roster(cfg)}. The human Product Manager is \"PM\". Refer to teammates by name.")
    pm = cfg.get("pm_name")
    if pm:
        text += (f"\nThe PM's name is {pm}. When you talk to them (in Discord, in your `message`), call them {pm}, not "
                 f"\"the PM\" or \"PM\". Never write their name in files: briefs, specs, plans, reports, code and commit "
                 "messages stay name-free.")
    character = member.get("vibe")
    if not character:
        return text
    if not (cfg.get("chat") or {}).get("character", True):
        return text + (f"\n\n## Your personality\nA light touch of: {character}. Personality never gets in the way of "
                       "being accurate and useful.")
    samples = sample_lines(cfg, key)
    if turn == "chat":
        return text + f"\n\n## Your character\nPlay it up: {character}.\n{CHARACTER_RULES}{samples}"
    return text + (f"\n\n## Your voice\nThe `message` you write is posted in Discord under your name. Write it as this "
                   f"character: {character}. {CHARACTER_RULES}{samples}\n\nEverything else you write (briefs, specs, plans, reports, "
                   "code, commit messages, files of any kind) stays plain and professional, with no character voice.")


def channel_members(cfg, channel):
    ch = cfg["channels"].get(channel)
    if not ch:
        return []
    return list(cfg["team"]) if ch.get("members") == "all" else list(ch.get("members", []))
