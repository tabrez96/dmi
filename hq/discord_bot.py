"""Discord front-end for Dunder Mifflin: one bot per crew member.

- Each crew member (DISCORD_TOKEN_<NAME>, e.g. DISCORD_TOKEN_DWIGHT) is its own
  bot: it posts its pipeline work and chat replies as itself, shows what it's
  working on as its status, and answers DMs from the PM.
- One member, the system member (`discord.system_member`, Michael), also does
  HQ's mechanical jobs: server layout (#needs-you, team channels, product/ticket
  forums), routing (it reads every message and decides who answers), approval
  buttons, alerts, the digest and slash commands. Its system notices start with
  🏢 so they're never mistaken for the member's own judgement. Its token is
  DISCORD_TOKEN_<NAME>, or DISCORD_TOKEN if that's unset, and its application
  needs the Message Content intent.
- A member without a token still works: the system bot posts for it under its name.

Agents never hold a token: every bot lives in this process, and agent jobs only
see the text HQ gives them.

Environment: DISCORD_GUILD_ID, DISCORD_PM_USER_ID (default: the server owner),
DISCORD_CO_OWNER_IDS (optional, comma-separated user IDs with the same say as the PM)
and DISCORD_TOKEN_<NAME> per crew member (DISCORD_TOKEN for the system member).
"""

import asyncio
import os
import signal
import threading

import discord

from . import pchannels, policy, team
from .discord_hq import HQBot
from .discord_members import AgentBot
from .discord_text import (
    HQ_PERMISSIONS,
    NEEDS_TEXT,
    OPTIONAL_TEXT,
    SYSTEM,
    Rounds,
    allowed_targets,
    chat_thread_name,
    chunks,
    invite_url,
    linkify,
    parse_custom_id,
    private_plan,
    route,
    system_member,
    system_token,
)

# -- the crew -----------------------------------------------------------------------

class NoteModal(discord.ui.Modal):
    def __init__(self, crew, kind, rid, action, label):
        super().__init__(title=f"{crew.hq.board.ref(kind, rid)}: {label}"[:45])
        self.crew, self.kind, self.rid, self.action = crew, kind, rid, action
        self.note = discord.ui.TextInput(
            label="Message to the agents", style=discord.TextStyle.paragraph,
            required=action in NEEDS_TEXT, max_length=2000)
        self.add_item(self.note)

    async def on_submit(self, interaction):
        await self.crew.run_action(interaction, self.kind, self.rid, self.action,
                                   str(self.note.value).strip() or None)


class Crew:
    """All the bots, and the logic that connects Discord to HQ."""

    def __init__(self, hq):
        self.hq, self.cfg = hq, hq.cfg
        self.guild_id = int(os.environ["DISCORD_GUILD_ID"])
        self.pm_id = int(os.environ["DISCORD_PM_USER_ID"]) if os.environ.get("DISCORD_PM_USER_ID") else None
        # people with the same say as the PM; HQ's pings still go to the PM only
        self.co_owner_ids = {int(uid) for uid in os.environ.get("DISCORD_CO_OWNER_IDS", "").replace(",", " ").split()}
        self.system_key = system_member(self.cfg)
        self.hqbot = HQBot(self)
        self.agents = {k: AgentBot(self, k) for k in self.cfg["team"]
                       if k != self.system_key and team.token(k)}
        self.rounds = {k: Rounds() for k in self.cfg["team"]}   # each member tracks their own
        self.tasks = set()                 # chat replies in flight, so a shutdown can wait for them
        self.loop = None

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def close(self):
        await asyncio.gather(self.hqbot.close(), *(b.close() for b in self.agents.values()),
                             return_exceptions=True)

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.hq.listeners.append(self._presence)
        await asyncio.gather(self.hqbot.start(system_token(self.cfg)),
                             *(self._run_member(k, bot) for k, bot in list(self.agents.items())))

    async def _run_member(self, key, bot):
        """A crew bot with a bad token is dropped (the system bot posts for it); it never takes the others down."""
        try:
            await bot.start(team.token(key))
        except (discord.LoginFailure, discord.ConnectionClosed) as e:
            if isinstance(e, discord.ConnectionClosed) and e.code != 4004:
                raise
            self.agents.pop(key, None)
            print(f"Discord: {team.name(key)}'s token was rejected ({team.token_env(key)}); "
                  f"{team.name(self.system_key or 'the system bot')} will post for them", flush=True)
        except discord.PrivilegedIntentsRequired:
            self.agents.pop(key, None)
            print(f"Discord: {team.name(key)}'s bot asked for an intent it doesn't have; skipped", flush=True)

    # -- identities --

    def bot_for(self, key):
        """The live bot that speaks as a member (the system bot for the system member)."""
        if key and key == self.system_key:
            return self.hqbot
        return self.agents.get(key) if key else None

    def ids(self):
        ids = {k: b.user.id for k, b in self.agents.items() if b.user}
        if self.system_key and self.hqbot.user:
            ids[self.system_key] = self.hqbot.user.id
        return ids

    def key_of(self, user_id):
        return next((k for k, uid in self.ids().items() if uid == user_id), None)

    def mention_for_role(self, role):
        key = team.member_for_role(self.cfg, role)
        uid = self.ids().get(key)
        return f"<@{uid}>" if uid else None

    def is_pm(self, user):
        return (self.pm_id is not None and user.id == self.pm_id) or user.id in self.co_owner_ids

    def author_label(self, msg):
        if self.is_pm(msg.author):
            return "PM"
        if self.hqbot.user and msg.author.id == self.hqbot.user.id and msg.content.startswith(SYSTEM):
            return "Dunder Mifflin HQ (system notice)"
        key = self.key_of(msg.author.id)
        if key:
            return team.label(self.cfg, key)
        if msg.webhook_id:
            return msg.author.display_name
        return None  # anyone else is ignored

    def _presence(self, key, activity):
        """Called from HQ's worker threads when a member starts or stops a job."""
        bot = self.bot_for(key)
        if bot and bot.is_ready() and self.loop:
            asyncio.run_coroutine_threadsafe(bot.set_activity(activity), self.loop)

    # -- speaking --

    async def speak(self, key, channel, text, files=(), label=None):
        """Post as a crew member (their own bot), or via the system bot under their name."""
        bot = self.bot_for(key)
        if bot and bot.is_ready():
            ch = bot.get_channel(channel.id) or await bot.fetch_channel(channel.id)
            parts = chunks(text)
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                await ch.send(part, files=list(files) if last and files else discord.utils.MISSING,
                              allowed_mentions=discord.AllowedMentions(users=True))
            return
        await self.hqbot.post_as(channel, label or team.label(self.cfg, key), text, files)

    # -- chat --

    async def respond(self, key, channel, place, reader=None):
        """A crew member reads the recent conversation and replies."""
        reader = reader or self.hqbot          # HQ can read message content everywhere
        ch = reader.get_channel(channel.id) or await reader.fetch_channel(channel.id)
        bot = self.bot_for(key)
        typing_ch = (bot.get_channel(channel.id) if bot else None) or ch
        async with typing_ch.typing():
            history = []
            async for m in ch.history(limit=self.cfg["chat"]["history"]):
                who = self.author_label(m)
                if who and m.clean_content.strip():
                    history.append({"author": who, "text": m.clean_content.strip()})
            history.reverse()
            starter = await self._thread_starter(ch)     # a thread's first message lives in the parent channel
            if starter and not (history and history[0]["text"] == starter["text"]):
                history.insert(0, starter)
            result = await asyncio.to_thread(self.hq.chat_turn, key, place, history)
        text = linkify(self.cfg, result["message"], self.ids(), result["mentions"])
        await self.speak(key, channel, text)
        for proposal in result["proposals"]:   # in a DM only the member's own bot can post, so its buttons come from them
            await self.hqbot.post_proposal(channel, proposal, via=bot if isinstance(ch, discord.DMChannel) else None)

    async def _thread_starter(self, ch):
        if not isinstance(ch, discord.Thread) or not ch.parent:
            return None
        try:
            if isinstance(ch.parent, discord.ForumChannel):    # a forum post's first message lives inside the post
                m = ch.starter_message or await ch.fetch_message(ch.id)
            else:
                m = ch.starter_message or await ch.parent.fetch_message(ch.id)
        except discord.HTTPException:
            return None
        who = self.author_label(m)
        return {"author": who, "text": m.clean_content.strip()} if who and m.clean_content.strip() else None

    async def _thread_for(self, msg, targets):
        """Replies to a top-level message go in a thread on that message, so each topic stays together."""
        ch = msg.channel
        if isinstance(ch, discord.Thread) or not self.cfg["chat"].get("threads", True):
            return ch
        who = team.name(targets[0]) if targets else "the crew"
        try:
            return await msg.create_thread(name=chat_thread_name(msg.clean_content, f"Chat with {who}"),
                                           auto_archive_duration=1440)
        except discord.HTTPException as e:
            print(f"Discord: couldn't open a thread ({e}); replying in the channel", flush=True)
            return ch

    async def on_team_message(self, msg):
        """Routing for team channels, pipeline threads and other server channels."""
        author_key = self.key_of(msg.author.id)
        pm = self.is_pm(msg.author)
        if not (pm or author_key) or not msg.content.strip():
            return
        ch = msg.channel
        mentioned = []
        for u in msg.mentions:
            k = self.key_of(u.id)
            if k and k != author_key and k not in mentioned:
                mentioned.append(k)

        # Pipeline posts (products/tickets forums): the PM steers the work; agents answer mentions.
        in_pipeline = isinstance(ch, discord.Thread) and (ch.parent_id in self.hqbot.forum_ids()
                                                          or ch.parent_id in self.hqbot.product_by_channel)
        found = await asyncio.to_thread(self.hq.board.by_thread, ch.id) if in_pipeline else None
        if in_pipeline and (found or ch.parent_id in self.hqbot.forum_ids()):
            if not pm:
                return
            if found:
                kind, rid = found
                try:
                    await asyncio.to_thread(self.hq.act, kind, rid, "guidance", msg.content.strip(), True)
                    await msg.add_reaction("📨")
                except (policy.IllegalTransition, KeyError, ValueError) as e:
                    await msg.reply(f"Couldn't route that: {e}", mention_author=False)
            for k in mentioned[:3]:
                place = {"kind": "thread", "name": ch.name, "topic": "pipeline post", "members": mentioned,
                         "subject": found}
                self.spawn(self.respond(k, ch, place))
            return

        base = ch.parent if isinstance(ch, discord.Thread) else ch
        name = getattr(base, "name", "")
        pid = self.hqbot.product_by_channel.get(base.id)
        if pid:        # a product's own channel: the owner of its current stage answers, and everyone knows the product
            name = f"p-{pid}"
            state = (await asyncio.to_thread(self.hq.board.product, pid))["state"]
            self.cfg["channels"][name] = {"members": self.cfg["product_channel"].get("members", "all"),
                                          "lead": pchannels.lead_for(self.cfg["product_channel"], state)}
        if pm:
            for r in self.rounds.values():      # you spoke: everyone's rounds here start over
                r.reset(ch.id)
        else:
            self.rounds[author_key].bump(ch.id)  # that member's own count, nobody else's
        if name in self.cfg["channels"]:
            targets = route(self.cfg, name, "pm" if pm else author_key, mentioned)
        else:
            targets = mentioned[:3]             # #hq or other channels: mentions only
        limit = self.cfg["chat"]["max_agent_turns"]
        for k in [k for k in targets if self.rounds[k].at_limit(ch.id, limit)]:
            targets.remove(k)                   # this member has used up their own rounds here
            if self.rounds[k].newly_paused(ch.id):
                await self.hqbot.notice(ch, f"<@{self.pm_id}> {team.name(k)} has replied {limit} times here "
                                            "without you, so they're paused in this thread. "
                                            "Reply to keep them going.")
        targets = allowed_targets(self.cfg, name, targets)
        if not targets:
            return
        where = await self._thread_for(msg, targets)
        topic = (getattr(base, "topic", None) if pid else None) or self.cfg["channels"].get(name, {}).get("topic", "")
        place = {"kind": "thread" if isinstance(where, discord.Thread) else "channel",
                 "name": getattr(base, "name", name), "topic": topic,
                 "members": team.channel_members(self.cfg, name), "subject": ("product", pid) if pid else None}
        for k in targets:
            self.spawn(self.respond(k, where, place))

    async def apply_privacy(self):
        """Make channels marked `private` visible to the PM, their members and the system bot only."""
        for _ in range(45):                      # wait until every crew bot is connected (their IDs are needed)
            if all(b.is_ready() for b in self.agents.values()):
                break
            await asyncio.sleep(2)
        guild = self.hqbot.guild
        for name, ch in self.cfg["channels"].items():
            if not ch.get("private"):
                continue
            channel = discord.utils.get(guild.channels, name=name)    # text channels and forums alike
            if channel is None:
                continue
            allow = discord.PermissionOverwrite(view_channel=True, send_messages=True, send_messages_in_threads=True,
                                                create_public_threads=True, read_message_history=True,
                                                attach_files=True, embed_links=True, add_reactions=True)
            deny = discord.PermissionOverwrite(view_channel=False)
            overwrites = {guild.default_role: deny, discord.Object(id=self.pm_id): allow}
            for uid in self.co_owner_ids:
                overwrites[discord.Object(id=uid)] = allow
            for uid, ok in private_plan(self.cfg, name, self.ids(), self.system_key).items():
                overwrites[discord.Object(id=uid)] = allow if ok else deny
            try:
                await channel.edit(overwrites=overwrites, reason="Dunder Mifflin: private channel")
                members = ", ".join(team.name(k) for k in team.channel_members(self.cfg, name))
                print(f"Discord: #{name} is private (you, {members}, and {team.name(self.system_key)} as system bot)",
                      flush=True)
            except discord.Forbidden:
                print(f"Discord: couldn't make #{name} private: the system bot needs the Manage Roles permission. "
                      f"Re-invite it with: {invite_url(self.hqbot.application_id, HQ_PERMISSIONS)}", flush=True)

    # -- buttons --

    async def on_component(self, interaction):
        """A button press, from whichever bot's message it was on."""
        if interaction.type != discord.InteractionType.component:
            return
        parsed = parse_custom_id((interaction.data or {}).get("custom_id", ""))
        if not parsed:
            return
        if not self.is_pm(interaction.user):
            return await interaction.response.send_message("Only the PM can do that.", ephemeral=True)
        kind, rid, action = parsed
        if action in NEEDS_TEXT | OPTIONAL_TEXT:
            label = "Give guidance" if action == "guidance" else "Reject"
            return await interaction.response.send_modal(NoteModal(self, kind, rid, action, label))
        await self.run_action(interaction, kind, rid, action, None)

    async def run_action(self, interaction, kind, rid, action, text):
        await interaction.response.defer()
        try:
            out = await asyncio.to_thread(self.hq.act, kind, rid, action, text)
        except (policy.IllegalTransition, KeyError, ValueError) as e:
            return await interaction.followup.send(f"Couldn't do that: {e}", ephemeral=True)
        if interaction.message:
            extra = f" → {out}" if isinstance(out, str) else ""
            done = f"\n\n— **{action.replace('_', ' ')}** by {interaction.user.mention}{extra}"
            await interaction.message.edit(content=(interaction.message.content + done)[:2000], view=None)


def run_bot(hq, with_hq=True):
    """Run the crew and HQ until stopped. SIGTERM/SIGINT (systemctl stop, Ctrl-C) drain gracefully:
    no new agent turns start, running ones finish and their results are posted, then the bots
    disconnect. A second signal stops immediately."""
    if not system_token(hq.cfg) or not os.environ.get("DISCORD_GUILD_ID"):
        raise SystemExit("Set the system bot's token (DISCORD_TOKEN_<NAME> or DISCORD_TOKEN) and "
                         "DISCORD_GUILD_ID in .env (see README: Discord setup)")
    stop = threading.Event()
    hq_thread = None
    if with_hq:
        hq_thread = threading.Thread(target=hq.run_forever, args=(stop,), daemon=True, name="hq")
        hq_thread.start()
        hq.thread = hq_thread
    discord.utils.setup_logging(level=40)  # errors only
    crew = Crew(hq)

    async def main():
        loop = asyncio.get_running_loop()
        stopping = asyncio.Event()

        def on_signal():
            if stopping.is_set():
                print("Second signal: stopping now.", flush=True)
                os._exit(1)
            print("Stopping: no new work will start; waiting for running agents to finish "
                  "(signal again to stop immediately)...", flush=True)
            stop.set()
            stopping.set()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, on_signal)

        runner = asyncio.create_task(crew.start())
        stopper = asyncio.create_task(stopping.wait())
        done, _ = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if runner in done:               # the bots ended on their own (e.g. a login failure)
            stopper.cancel()
            return runner.result()
        if hq_thread:                    # draining: Discord stays up so results still reach you
            await asyncio.to_thread(hq_thread.join)
        if crew.tasks:
            await asyncio.wait(list(crew.tasks))
        await asyncio.sleep(hq.cfg.get("discord", {}).get("outbox_seconds", 3) * 2 + 1)   # last posts out
        await crew.close()
        await asyncio.gather(runner, return_exceptions=True)
        print("Dunder Mifflin stopped cleanly.", flush=True)

    try:
        asyncio.run(main())
    except (discord.LoginFailure, discord.ConnectionClosed) as e:
        if isinstance(e, discord.ConnectionClosed) and e.code != 4004:
            raise
        raise SystemExit(f"Discord rejected {team.name(system_member(hq.cfg) or 'the system')}'s token. "
                         "Run `dmi doctor`.")
    except discord.PrivilegedIntentsRequired:
        name = team.name(system_member(hq.cfg) or "the system")
        raise SystemExit(f"Turn on 'Message Content Intent' for {name}'s bot in the Developer Portal.")
    finally:
        stop.set()
