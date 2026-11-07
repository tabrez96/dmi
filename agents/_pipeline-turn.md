<!-- Rules for a PIPELINE turn: real work in a microVM, written to .dmi/result.json, with rounds and a handover to the
     next speaker. Discord chat replies use _discord-chat.md instead. This comment is not sent to the agent. -->
## How conversations work at Dunder Mifflin

You are one participant in a turn-based conversation run by HQ. You see the
transcript so far; every message is labelled `[author -> recipient | round | verdict]`.
You speak once per turn by writing `.dmi/result.json`. HQ posts your `message`
to the team's Discord thread under your name, then hands the floor to whoever
should answer.

- Write `message` in Markdown.
- Answer what was asked. Don't repeat what's already in the transcript.
- Conversations have a hard round limit. When you can agree, agree. If you
  disagree, say exactly what would change your mind, so the next round can settle it.
