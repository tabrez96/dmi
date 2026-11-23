"""The crew bots that speak as one member each: a 'Name · Role' nickname, a status line and DMs with the PM."""


import discord

from . import team
from .discord_text import (
    AGENT_PERMISSIONS,
    invite_url,
)

# -- the crew -----------------------------------------------------------------------


class MemberMixin:
    """What every crew bot does: a 'Name · Role' nickname, a status line, DMs with the PM."""

    async def become_member(self, guild):
        label = team.label(self.crew.cfg, self.key)
        try:
            if guild.me.nick != label:
                await guild.me.edit(nick=label)
        except discord.HTTPException:
            pass
        await self.set_activity(None)
        return label

    async def set_activity(self, activity):
        await self.change_presence(status=discord.Status.online if activity is None else discord.Status.dnd,
                                   activity=discord.CustomActivity(name=(activity or "available")[:120]))

    async def handle_dm(self, msg):
        if msg.author.id == self.user.id or msg.author.bot:
            return
        if not self.crew.is_pm(msg.author):
            return await msg.channel.send("I only take requests from Dunder Mifflin's PM.")
        place = {"kind": "dm", "name": "dm", "topic": "", "members": [self.key]}
        await self.crew.respond(self.key, msg.channel, place, reader=self)


class AgentBot(MemberMixin, discord.Client):
    """One crew member's own bot: posts as itself, shows its status, answers DMs."""

    def __init__(self, crew, key):
        super().__init__(intents=discord.Intents.default())  # DMs carry content without the privileged intent
        self.crew, self.key = crew, key

    async def on_ready(self):
        guild = self.get_guild(self.crew.guild_id)
        if guild is None:
            print(f"{team.name(self.key)} isn't in the server yet. Invite it:\n  "
                  f"{invite_url(self.application_id, AGENT_PERMISSIONS)}", flush=True)
            return
        print(f"Discord: {await self.become_member(guild)} online", flush=True)

    async def on_interaction(self, interaction):      # buttons on this member's own messages (e.g. in a DM)
        await self.crew.on_component(interaction)

    async def on_message(self, msg):
        if isinstance(msg.channel, discord.DMChannel):
            await self.handle_dm(msg)
