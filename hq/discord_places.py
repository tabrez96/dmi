"""Where HQ's messages land in Discord: each product's channel and its checklist, the ticket and product threads
with their forum tags, and delivering one board message to its place. Mixed into the HQ bot."""

import asyncio
import io
import json
import os
import time

import discord

from . import pchannels, policy, refs, team
from .discord_text import (
    LIMIT,
    STYLES,
    SYSTEM,
    TICKET_DONE,
    channel_thread_title,
    custom_id,
    render,
    tag_name,
    thread_title,
)

# -- the crew -----------------------------------------------------------------------


class Places:
    """The HQ bot's channels and threads (it sets `self.guild`, `self.forums`, `self.applied` and the rest)."""

    async def _category(self, name):
        cat = discord.utils.get(self.guild.categories, name=name)
        return cat or await self.guild.create_category(name, reason="Dunder Mifflin: product channels")

    async def _ensure_product_channel(self, p):
        """The approved product's channel: created if missing (with its checklist), renamed and re-described when
        the product changes, moved to Archive when dropped. Returns the channel."""
        pcfg = self.cfg.get("product_channel", {})
        dropped = p["state"] == policy.P_REJECTED
        ch = self.guild.get_channel(int(p["channel_id"])) if p["channel_id"] else None
        taken = {c.name for c in self.guild.channels if ch is None or c.id != ch.id}
        name = pchannels.channel_name(p, taken)
        topic = ("[dropped] " if dropped else "") + pchannels.description(p, 1000 - (10 if dropped else 0))
        cat = await self._category(pcfg.get("archive_category", "Archive") if dropped else pcfg.get("category", "Products"))
        if ch is None:
            ch = await self.guild.create_text_channel(name, topic=topic, category=cat,
                                                      reason=f"Dunder Mifflin: {refs.product_ref(p)} approved")
            await asyncio.to_thread(self.hq.board.update_product, p["id"], channel_id=str(ch.id), checklist_msg_id=None)
            p = {**p, "channel_id": str(ch.id), "checklist_msg_id": None}
            self.checklist_text.pop(p["id"], None)
        elif (ch.name, ch.topic or "", ch.category_id) != (name, topic, cat.id):
            await self._edit_channel(ch, name, topic, cat, f"Dunder Mifflin: {refs.product_ref(p)}")
        self.product_by_channel[ch.id] = p["id"]
        await self._update_checklist(ch, p)
        await self._pipeline_thread(ch, p)
        return ch

    async def _edit_channel(self, ch, name, topic, cat, reason):
        """Discord allows two renames per ten minutes and makes the caller wait out the rest. Waiting here would
        stall every message behind it, so a rename that doesn't go through quickly is retried in five minutes."""
        if time.time() < self.rename_retry.get(ch.id, 0):
            name = ch.name
        try:
            await asyncio.wait_for(ch.edit(name=name, topic=topic, category=cat, reason=reason), timeout=10)
        except TimeoutError:
            self.rename_retry[ch.id] = time.time() + 300
            print(f"Discord: renaming #{ch.name} to #{name} is rate-limited; will retry in 5 minutes", flush=True)

    async def _pipeline_thread(self, ch, p):
        """The product's "Pipeline" thread inside its channel: the crew's turns, the gates and their buttons. What you
        write there steers the work; chat with the crew happens in the channel itself. A post left in the old
        products forum is archived and replaced."""
        p = await asyncio.to_thread(self.hq.board.product, p["id"])
        old = None
        if p["thread_id"]:
            old = self.get_channel(int(p["thread_id"]))
            if old is None:
                try:
                    old = await self.fetch_channel(int(p["thread_id"]))
                except discord.NotFound:
                    old = None
            if old is not None and old.parent_id == ch.id:
                return old
        thread = await ch.create_thread(name="Pipeline", type=discord.ChannelType.public_thread,
                                        auto_archive_duration=10080, reason="Dunder Mifflin: the product's pipeline")
        await asyncio.to_thread(self.hq.board.update_product, p["id"], thread_id=str(thread.id))
        if old is not None:
            await self._archive_old_post(old, ch)
        return thread

    async def _archive_old_post(self, thread, home):
        try:
            if not (thread.archived and thread.locked):
                await thread.send(f"{SYSTEM} This product now lives in {home.mention}; this post is archived.")
                await thread.edit(archived=True, locked=True, reason="Dunder Mifflin: moved to the product's channel")
        except discord.HTTPException as e:
            print(f"Discord: couldn't archive an old post: {e}", flush=True)

    async def _update_checklist(self, ch, p):
        """Post (and pin) the 'what's next' checklist, then edit it in place as the product moves along."""
        tickets = await asyncio.to_thread(self.hq.board.list, None, product_id=p["id"])
        names = {r: team.name(team.member_for_role(self.cfg, r)) for r in ("designer", "architect", "engineer", "qa", "manager")
                 if team.member_for_role(self.cfg, r)}
        text = pchannels.checklist(p, tickets, names)
        if self.checklist_text.get(p["id"]) == text:
            return
        msg = None
        if p.get("checklist_msg_id"):
            try:
                msg = await ch.fetch_message(int(p["checklist_msg_id"]))
            except discord.NotFound:
                msg = None
        if msg is not None and msg.author.id == self.user.id:
            await msg.edit(content=text[:2000])
            if not msg.pinned:                    # (retry: the permission may have been granted since)
                try:
                    await msg.pin()
                except discord.HTTPException:
                    pass
        else:
            msg = await ch.send(f"{SYSTEM} {text}"[:2000], allowed_mentions=discord.AllowedMentions.none())
            await asyncio.to_thread(self.hq.board.update_product, p["id"], checklist_msg_id=str(msg.id))
            try:
                await msg.pin()
            except discord.HTTPException:
                print("Discord: couldn't pin the checklist (give the system bot the Pin Messages permission); "
                      "it's the first message in the channel", flush=True)
        self.checklist_text[p["id"]] = text

    async def _sync_product_channels(self):
        """Every product has its own channel (with its pipeline thread) from the moment it exists."""
        try:
            for p in await asyncio.to_thread(self.hq.board.products):
                if pchannels.wants_channel(p):
                    await self._ensure_product_channel(p)
        except Exception as e:  # noqa: BLE001 - must never block delivering messages
            print(f"Discord: product channel sync error: {e!r}", flush=True)

    async def _destination(self, kind, rid):
        """Where a board message goes: a product's pipeline thread (in its channel); a ticket's thread (in its
        product's channel, or the tickets forum when it has no product)."""
        if kind == "product":
            p = await asyncio.to_thread(self.hq.board.product, rid)
            ch = await self._ensure_product_channel(p)
            return await self._pipeline_thread(ch, p)
        return await self._thread(kind, rid)

    def _row(self, kind, rid):
        return self.hq.board.product(rid) if kind == "product" else self.hq.board.get(rid)

    async def _thread(self, kind, rid):
        row = await asyncio.to_thread(self._row, kind, rid)
        if row["thread_id"]:
            t = self.get_channel(int(row["thread_id"]))
            if t is None:
                try:
                    t = await self.fetch_channel(int(row["thread_id"]))
                except discord.NotFound:
                    t = None
            if t is not None:
                return t
        intro = row.get("description") or row.get("focus") or row["title"]
        prod = await asyncio.to_thread(self.hq.board.product, row["product_id"]) \
            if kind == "ticket" and row.get("product_id") else None
        if prod is not None:         # a ticket of a product: its thread lives in the product's channel
            home = await self._ensure_product_channel(prod)
            thread = await home.create_thread(name=channel_thread_title(row), type=discord.ChannelType.public_thread,
                                              auto_archive_duration=4320, reason=f"Dunder Mifflin: T-{rid}")
            await thread.send(intro[:LIMIT])
        else:
            tag = self.tags[kind].get(tag_name(row["state"]))
            created = await self.forums[kind].create_thread(
                name=thread_title(kind, row), content=intro[:LIMIT], applied_tags=[tag] if tag else [])
            thread = created.thread
        upd = self.hq.board.update_product if kind == "product" else self.hq.board.update
        await asyncio.to_thread(upd, rid, thread_id=str(thread.id))
        self.applied[(kind, rid)] = (row["state"], thread.name)
        return thread

    async def deliver(self, m):
        kind, rid = m["subject"], m["subject_id"]
        thread = await self._destination(kind, rid)        # (a channel for approved products, else a forum post)
        if isinstance(thread, discord.Thread) and thread.archived:
            await thread.edit(archived=False)
        name, content, full = render(self.cfg, m, self.crew.mention_for_role)
        files = [discord.File(io.BytesIO(full.encode()), filename=f"{self.hq.board.ref(kind, rid)}-{m['author']}-{m['id']}.md")] \
            if full else []
        for path in json.loads(m.get("files") or "[]")[:9]:   # Discord: max 10 attachments
            if os.path.isfile(path) and os.path.getsize(path) < 8_000_000:
                fname = os.path.basename(path)
                if fname.startswith("screen."):      # every screen file has the same name; use its folder's
                    fname = f"{os.path.basename(os.path.dirname(path))}{os.path.splitext(fname)[1]}"
                files.append(discord.File(path, filename=fname))
        actions = json.loads(m["actions"]) if m["actions"] else None
        if actions:
            # Approvals and escalations come from HQ itself so the buttons are ours.
            await self.notice(thread, f"<@{self.crew.pm_id}> {content}", files,
                              view=self._view(kind, rid, actions))
            if self.hq_channel:
                await self.notice(self.hq_channel, f"🔔 **{self.hq.board.ref(kind, rid)}** {m['body'][:300]}\n{thread.jump_url}")
            return
        if m["author"] == "hq":
            await self.notice(thread, content, files)
        else:
            await self.crew.speak(team.member_for_role(self.cfg, m["author"]), thread, content, files, label=name)
        await self._archive_if_done(kind, rid, thread)

    async def _archive_if_done(self, kind, rid, thread):
        """A ticket whose pull request was merged or closed is done: its thread (in a product's channel or the old
        tickets forum) is archived so only live work is listed. Archived, not deleted: it stays searchable, and a
        reply reopens it."""
        if kind != "ticket" or not isinstance(thread, discord.Thread):
            return
        row = await asyncio.to_thread(self.hq.board.get, rid)
        if row["state"] in TICKET_DONE and not thread.archived:
            try:
                await thread.edit(archived=True, reason="Dunder Mifflin: the ticket is done")
            except discord.HTTPException as e:
                print(f"Discord: couldn't archive T-{rid}'s thread: {e}", flush=True)

    def _view(self, kind, rid, actions):
        view = discord.ui.View(timeout=None)
        for a in actions:
            view.add_item(discord.ui.Button(label=a["label"], custom_id=custom_id(kind, rid, a["id"]),
                                            style=STYLES.get(a.get("style"), discord.ButtonStyle.secondary)))
        return view

    async def _sync_threads(self):
        """Keep each post's tag and title in step with the board."""
        rows = [("product", p) for p in await asyncio.to_thread(self.hq.board.products)] + \
               [("ticket", t) for t in await asyncio.to_thread(self.hq.board.list)]
        for kind, row in rows:
            if not row["thread_id"] or kind == "product":
                continue                       # (a product's pipeline thread keeps its name)
            want = (row["state"], thread_title(kind, row))
            if self.applied.get((kind, row["id"])) == want:
                continue
            self.applied[(kind, row["id"])] = want
            self.loop.create_task(self._retag(kind, row, want))

    async def _retag(self, kind, row, want):
        try:
            thread = self.get_channel(int(row["thread_id"])) or await self.fetch_channel(int(row["thread_id"]))
            forum = isinstance(thread.parent, discord.ForumChannel)
            if kind == "ticket" and want[0] in TICKET_DONE:
                # Done: archive it, in a forum or a product channel. No rename: Discord allows two renames per thread
                # every ten minutes, and a finished ticket's thread was usually just renamed; archiving isn't limited.
                if not thread.archived:
                    tag = self.tags[kind].get(tag_name(want[0])) if forum else None
                    await thread.edit(archived=True, **({"applied_tags": [tag] if tag else []} if forum else {}),
                                      reason="Dunder Mifflin: the ticket is done")
            elif forum:
                tag = self.tags[kind].get(tag_name(want[0]))
                await thread.edit(applied_tags=[tag] if tag else [], name=want[1])
            else:                              # in a product channel: the state is a mark in the title
                new = channel_thread_title(row)
                if thread.name != new:
                    await thread.edit(name=new, archived=False) if thread.archived else await thread.edit(name=new)
        except discord.NotFound:
            # The post was deleted in Discord: forget it, and re-post what's waiting in a fresh one.
            upd = self.hq.board.update_product if kind == "product" else self.hq.board.update
            await asyncio.to_thread(upd, row["id"], thread_id=None)
            await asyncio.to_thread(self.hq.repost_pending, kind, row["id"])
            print(f"Discord: {self.hq.board.ref(kind, row['id'])}'s post was deleted; re-creating it", flush=True)
        except discord.HTTPException as e:
            print(f"Discord: couldn't retag {self.hq.board.ref(kind, row['id'])}: {e}", flush=True)
            self.applied.pop((kind, row["id"]), None)

    def thread_link(self, kind, row):
        return f"https://discord.com/channels/{self.guild.id}/{row['thread_id']}" if row.get("thread_id") else ""
