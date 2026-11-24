"""The system member's bot: their own voice, plus HQ's mechanical jobs in Discord (server layout, the outbox,
the #needs-you inbox, the digest and slash commands)."""

import asyncio
import io
import time

import discord
from discord import app_commands

from . import pchannels, policy, refs, team

# -- the crew -----------------------------------------------------------------------
from .discord_members import MemberMixin
from .discord_places import Places
from .discord_text import (
    HQ_PERMISSIONS,
    INBOX,
    LIMIT,
    SYSTEM,
    chunks,
    custom_id,
    invite_url,
    tag_name,
)


class HQBot(Places, MemberMixin, discord.Client):
    """The system member's bot: their own voice, plus every mechanical HQ job."""

    def __init__(self, crew):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.crew, self.key = crew, crew.system_key
        self.hq, self.cfg = crew.hq, crew.cfg
        self.dcfg = self.cfg.get("discord", {})
        self.tree = app_commands.CommandTree(self)
        self.forums, self.webhooks, self.tags = {}, {}, {}
        self.hq_channel = None
        self.applied = {}          # (kind, id) -> (state, title) last synced to Discord
        self.product_by_channel = {}   # Discord channel id -> product id (each approved product has its own channel)
        self.rename_retry = {}         # channel id -> when a rate-limited rename may be tried again
        self.checklist_text = {}       # product id -> the checklist as last shown (edit the message only on change)
        self.ready_once = False
        self.setting_up = asyncio.Lock()       # on_ready can fire again while a setup is still running
        self._register_commands()

    def forum_ids(self):
        return {f.id for f in self.forums.values()}

    # -- setup --

    async def on_ready(self):
        async with self.setting_up:
            if not self.ready_once:
                await self._set_up()

    async def _set_up(self):
        guild = self.get_guild(self.crew.guild_id)
        if guild is None:
            print(f"{team.name(self.key)} (system bot) isn't in server {self.crew.guild_id}. Invite it:\n  "
                  f"{invite_url(self.application_id, HQ_PERMISSIONS)}", flush=True)
            return
        self.guild = guild
        self.crew.pm_id = self.crew.pm_id or guild.owner_id
        self.hq_channel = await self._text_channel(guild, self.dcfg.get("hq_channel", "needs-you"),
                                                   "What is waiting on you and what is hanging, plus approvals, alerts and the daily digest")
        for name, ch in self.cfg["channels"].items():
            if ch.get("forum"):      # a forum, so every conversation is its own post and the list stays tidy
                await self._forum(guild, name, ch.get("topic", ""))
            else:
                await self._text_channel(guild, name, ch.get("topic", ""))
        self.inbox_channel = self.hq_channel          # the inbox is the pinned message at the top of #needs-you
        self.inbox_text = None
        for kind, key, states in [("ticket", "tickets_forum", policy.STATES)]:    # (products live in their own channels)
            forum = await self._forum(guild, self.dcfg.get(key, kind + "s"))
            self.forums[kind] = forum
            self.tags[kind] = await self._ensure_tags(forum, states)
            self.webhooks[forum.id] = await self._webhook(forum)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        label = await self.become_member(guild)
        missing = [team.name(k) for k in self.cfg["team"] if k not in self.crew.agents and k != self.key]
        print(f"Discord: {label} online (system bot) in {guild.name}; PM is user {self.crew.pm_id}"
              + (f"; no bot token for {', '.join(missing)} ({team.name(self.key)} posts for them)"
                 if missing else ""), flush=True)
        await self.crew.apply_privacy()      # before anything is posted: a new private channel starts out public
        self.loop.create_task(self._outbox_loop())
        self.ready_once = True          # only now: a setup that failed half way runs again on the next on_ready

    async def _text_channel(self, guild, name, topic):
        ch = discord.utils.get(guild.text_channels, name=name)
        return ch or await guild.create_text_channel(name, topic=topic)

    async def _forum(self, guild, name, topic=""):
        ch = discord.utils.get(guild.forums, name=name)
        return ch or await guild.create_forum(name, topic=topic or f"Dunder Mifflin {name}: one post each")

    async def _ensure_tags(self, forum, states):
        have = {t.name: t for t in forum.available_tags}
        for s in states:
            if tag_name(s) not in have:
                have[tag_name(s)] = await forum.create_tag(name=tag_name(s))
        return have

    async def _webhook(self, channel):
        for wh in await channel.webhooks():
            if wh.user and wh.user.id == self.user.id and wh.name in ("Dunder Mifflin Crew",):
                return wh
        return await channel.create_webhook(name="Dunder Mifflin Crew")

    async def notice(self, channel, text, files=(), view=discord.utils.MISSING):
        """A 🏢 system notice: something HQ did or needs, not the member's own opinion."""
        ch = self.get_channel(channel.id) or await self.fetch_channel(channel.id)
        return await ch.send(f"{SYSTEM} {text}"[:2000], files=list(files) or discord.utils.MISSING, view=view,
                             allowed_mentions=discord.AllowedMentions(users=True))

    async def post_as(self, channel, name, text, files=()):
        """Post under a display name: a webhook where possible, else a bold prefix."""
        parent = channel.parent if isinstance(channel, discord.Thread) else channel
        parts, sent = chunks(text), 0
        attachments = []           # read once: a failed send closes its File objects, so each send gets fresh ones
        for file in files:
            attachments.append((file.filename, file.fp.read()))
            file.close()

        def fresh(last):
            if not (last and attachments):
                return discord.utils.MISSING
            return [discord.File(io.BytesIO(data), filename=filename) for filename, data in attachments]
        try:
            wh = self.webhooks.get(parent.id) or await self._webhook(parent)
            self.webhooks[parent.id] = wh
            kw = {"thread": channel} if isinstance(channel, discord.Thread) else {}
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                await wh.send(part, username=name[:80], files=fresh(last),
                              allowed_mentions=discord.AllowedMentions(users=True), wait=True, **kw)
                sent += 1
        except (discord.HTTPException, AttributeError):
            ch = self.get_channel(channel.id) or await self.fetch_channel(channel.id)
            parts = parts[sent:] if sent else chunks(f"**{name}:** {text}")     # never repeat what already went out
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                await ch.send(part, files=fresh(last))

    async def post_proposal(self, channel, pid, via=None):
        text = await asyncio.to_thread(self.hq.proposal_text, pid)
        kind = (await asyncio.to_thread(self.hq.board.proposal, pid))["kind"]
        designer = team.name(team.member_for_role(self.cfg, "designer") or "the designer")
        send_to = designer
        built = False
        if kind == "design_change":     # the note goes to whoever owns the product's current stage
            try:
                ref = (await asyncio.to_thread(self.hq.board.proposal, pid))["payload"]["product"]
                ref_kind, ref_id = await asyncio.to_thread(self.hq.board.resolve_ref, ref)
                if ref_kind != "product":
                    raise KeyError(f"{ref} is not a product")
                state = (await asyncio.to_thread(self.hq.board.product, ref_id))["state"]
                send_to = team.name(pchannels.lead_for(self.cfg.get("product_channel", {}), state))
                built = state in policy.BUILT_STATES
            except (ValueError, KeyError):
                pass
        label = {"design": "Start design", "design_change": "Create ticket" if built else f"Send to {send_to}"}.get(kind, "Create")
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.success,
                                        custom_id=custom_id("proposal", pid, "accept")))
        view.add_item(discord.ui.Button(label="Dismiss", style=discord.ButtonStyle.secondary,
                                        custom_id=custom_id("proposal", pid, "dismiss")))
        if via is None:
            return await self.notice(channel, f"<@{self.crew.pm_id}> {text}", view=view)
        ch = via.get_channel(channel.id) or await via.fetch_channel(channel.id)
        await ch.send(text[:2000], view=view)

    # -- outbox: board -> Discord (delivering a message is in Places) --

    async def _outbox_loop(self):
        every = self.dcfg.get("outbox_seconds", 3)
        held = {}                        # message id -> when to try it again, so failing ones don't block the rest
        while not self.is_closed():
            try:
                now = time.monotonic()
                held = {mid: until for mid, until in held.items() if until > now}
                for message in await asyncio.to_thread(self.hq.board.outbox, 20 + len(held)):
                    if message["id"] in held:
                        continue
                    try:
                        await self.deliver(message)
                    except Exception as e:  # noqa: BLE001 - one bad message stays queued; the rest still go out
                        print(f"Discord: couldn't deliver message {message['id']}: {e!r}", flush=True)
                        held[message["id"]] = now + 300
                        continue
                    await asyncio.to_thread(self.hq.board.mark_delivered, message["id"])
                while self.hq.announcements and self.hq_channel:          # HQ-wide notices (a Claude login switch)
                    await self.notice(self.hq_channel, f"ℹ️ {self.hq.announcements[0]}")
                    self.hq.announcements.popleft()                       # only once it went out
                await self._sync_threads()
                await self._sync_product_channels()
                await self._maybe_digest()
                await self._refresh_inbox()
            except Exception as e:  # keep delivering; the message stays in the outbox
                print(f"Discord outbox error: {e!r}", flush=True)
                await asyncio.sleep(10)
            await asyncio.sleep(every)

    def inbox_text_now(self):
        from . import inbox
        return inbox.build(self.hq, stale_hours=self.cfg.get("inbox", {}).get("stale_hours", 2),
                           link=self.thread_link, loop_alive=self.hq.loop_alive())[0]

    async def _refresh_inbox(self):
        """Keep one message in #needs-you current (edited in place; nothing is posted unless it changed)."""
        now = time.time()
        if now - getattr(self, "_inbox_at", 0) < 60:
            return
        self._inbox_at = now
        full = await asyncio.to_thread(self.inbox_text_now)
        body = "\n".join(full.splitlines()[:-1])[:LIMIT]   # the 'Updated' line alone shouldn't cause an edit
        text = full[:LIMIT]
        if body == self.inbox_text and now - getattr(self, "_inbox_checked", 0) < 900:
            return                            # unchanged; every 15 minutes it is checked anyway, in case it was deleted
        ch = self.get_channel(self.inbox_channel.id) or await self.fetch_channel(self.inbox_channel.id)
        mine = next((m for m in await ch.pins() if m.author.id == self.user.id and m.content.startswith(INBOX)), None)
        content = f"{INBOX} {text}"[:2000]
        if mine:
            await mine.edit(content=content)
        else:
            sent = await ch.send(content, allowed_mentions=discord.AllowedMentions.none())
            try:
                await sent.pin()
            except discord.HTTPException as e:    # pins are a nicety; without one a new message is posted each time
                print(f"Discord: couldn't pin the inbox: {e}", flush=True)
        self.inbox_text, self._inbox_checked = body, now

    async def _maybe_digest(self):
        hour = self.dcfg.get("digest_hour")
        if hour is None or not self.hq_channel:
            return
        today = time.strftime("%Y-%m-%d")
        if time.localtime().tm_hour == hour and getattr(self, "_digest_day", None) != today:
            from .cli import digest
            await self.notice(self.hq_channel, (await asyncio.to_thread(digest, self.hq))[:LIMIT])
            self._digest_day = today            # after it went out, so a failed send is tried again this hour

    # -- input --

    async def on_message(self, msg):
        if isinstance(msg.channel, discord.DMChannel):
            return await self.handle_dm(msg)
        if msg.webhook_id or not self.ready_once:
            return
        if msg.author.id == self.user.id and msg.content.startswith(SYSTEM):
            return  # our own system notices never trigger anyone
        await self.crew.on_team_message(msg)

    async def on_interaction(self, interaction):
        await self.crew.on_component(interaction)

    # -- slash commands --

    def _register_commands(self):
        tree, hq = self.tree, self.hq

        async def pm_only(interaction):
            if not self.crew.is_pm(interaction.user):
                await interaction.response.send_message("Only the PM can do that.", ephemeral=True)
                return False
            return True

        def product_id(text):
            """A product's id from its reference or name; a ticket reference is refused, not read as a product id."""
            kind, pid = hq.board.resolve_ref(text)
            if kind != "product":
                raise KeyError(f"{text} is a ticket, not a product")
            return pid

        scout, analyst = (team.name(team.member_for_role(self.cfg, r) or r) for r in ("scout", "analyst"))

        @tree.command(name="discover", description=f"Start a {scout} ⇄ {analyst} discovery session")
        async def discover(interaction: discord.Interaction, focus: str = ""):
            if await pm_only(interaction):
                pid = await asyncio.to_thread(hq.discover, focus or None)
                await interaction.response.send_message(f"{hq.board.pref(pid)} discovery started.", ephemeral=True)

        @tree.command(name="new", description="Add a ticket to a product directly")
        async def new(interaction: discord.Interaction, product: str, title: str, description: str = ""):
            if await pm_only(interaction):
                try:
                    pid = product_id(product)
                except KeyError as e:
                    return await interaction.response.send_message(str(e.args[0]), ephemeral=True)
                await interaction.response.defer(ephemeral=True)     # the product's lock can take past Discord's 3s
                try:
                    tid = await asyncio.to_thread(hq.new_ticket, pid, title, description)
                except ValueError as e:                        # an idea: no repo to build in yet
                    return await interaction.followup.send(str(e), ephemeral=True)
                await asyncio.to_thread(hq.board.post, "ticket", tid, "pm", f"New ticket: {title}",
                                        None, "status")
                await interaction.followup.send(f"T-{tid} created.", ephemeral=True)

        @tree.command(name="change", description="Change a product that is already built or launched (files a ticket on its repo)")
        async def change(interaction: discord.Interaction, product: str, what: str, ui: bool = True,
                         designer: bool = False):
            if await pm_only(interaction):
                await interaction.response.defer(ephemeral=True)     # filing it on GitHub can take past Discord's 3s
                try:
                    pid = product_id(product) if product.strip() else None
                    prod = hq.board.product(pid)
                    tid = await asyncio.to_thread(hq.file_change, prod, what, "", ui, designer)
                except (KeyError, ValueError, TypeError) as e:
                    return await interaction.followup.send(f"Couldn't do that: {e.args[0] if e.args else e}", ephemeral=True)
                who = "Pam draws it next" if designer else "Jim plans it next"
                await interaction.followup.send(f"T-{tid} created on {refs.product_ref(prod)}. {who}.", ephemeral=True)

        @tree.command(name="digest", description="What happened in the last 24h, and what needs you")
        async def digest_cmd(interaction: discord.Interaction, hours: int = 24):
            if not await pm_only(interaction):
                return
            from .cli import digest
            await interaction.response.send_message((await asyncio.to_thread(digest, hq, hours))[:LIMIT],
                                                    ephemeral=True)

        @tree.command(name="inbox", description="What is waiting on you, and what is hanging")
        async def inbox_cmd(interaction: discord.Interaction):
            if not await pm_only(interaction):
                return
            await interaction.response.send_message((await asyncio.to_thread(self.inbox_text_now))[:LIMIT],
                                                    ephemeral=True)

        @tree.command(name="screens", description="The design library: every screen, its status and ID")
        async def screens_cmd(interaction: discord.Interaction, product: str = ""):
            if not await pm_only(interaction):
                return
            try:
                pid = product_id(product) if product.strip() else None
            except KeyError as e:
                return await interaction.response.send_message(str(e.args[0]), ephemeral=True)
            text = await asyncio.to_thread(hq.screens_text, pid)
            base = (hq.cfg.get("dashboard") or {}).get("public_url", "").rstrip("/")
            await interaction.response.send_message(
                (text + (f"\n\nGallery: {base}/design/" if base else ""))[:LIMIT], ephemeral=True)

        @tree.command(name="crew", description="Who's on the team, and which model each runs on")
        async def crew_cmd(interaction: discord.Interaction):
            if not await pm_only(interaction):
                return
            ids = self.crew.ids()
            lines = []
            for k, m in self.cfg["team"].items():
                who = f"<@{ids[k]}>" if k in ids else f"**{team.name(k)}** (no bot yet)"
                model = self.cfg["roles"][m["roles"][0]]["model"].split("/")[-1]
                lines.append(f"{m['emoji']} {who} · {m['title']} · `{model}`")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
