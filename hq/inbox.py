"""The inbox: what is waiting on the PM, and what is hanging with nobody working on it.

Pure board reading, so Discord (`#needs-you`, `/inbox`) and the CLI show the same thing.
"""

import time

from . import policy, refs

DONE_TICKETS = (policy.SHIPPED, policy.REJECTED)
PRODUCT_WORKING = (policy.DISCOVERING, policy.UX_DESIGNING, policy.DESIGNING)


def ago(seconds):
    m = int(seconds // 60)
    if m < 60:
        return f"{max(m, 1)}m"
    h = m // 60
    return f"{h}h" if h < 48 else f"{h // 24}d"


def _plabel(p):
    return p["title"] if p.get("slug") else f"{refs.product_ref(p)} {p['title']}"


def build(hq, now=None, stale_hours=2, link=None, loop_alive=True):
    """Return (text, counts). `link(kind, row)` gives the thread URL or ''."""
    now = now or time.time()
    link = link or (lambda kind, row: "")
    stale = stale_hours * 3600
    waiting, hanging = [], []

    def item(kind, row, label, state, extra=""):
        url = link(kind, row)
        age = ago(now - (row["updated_at"] or now))
        line = f"• **{label[:70]}** · {policy.state_label(state)} · {age}{extra}"
        return (now - (row["updated_at"] or now), line + (f"\n  {url}" if url else ""))

    for p in hq.board.products():
        if p["state"] in (policy.P_REJECTED, policy.LAUNCHED):
            continue
        if p["state"] in policy.PRODUCT_NEEDS_PM:
            waiting.append(item("product", p, _plabel(p), p["state"]))
        elif p["state"] in PRODUCT_WORKING and not p["claimed_by"] and now - p["updated_at"] > stale:
            hanging.append(item("product", p, _plabel(p), p["state"], f" · next: {p['next_speaker'] or 'nobody'}"))
    for t in hq.board.list():
        if t["state"] in DONE_TICKETS:
            continue
        label = f"T-{t['id']} {t['title']}"
        if t["state"] in policy.NEEDS_PM:
            waiting.append(item("ticket", t, label, t["state"]))
        elif not t["claimed_by"] and now - t["updated_at"] > stale:
            hanging.append(item("ticket", t, label, t["state"]))

    out = ["**Needs you**"]
    out += [x[1] for x in sorted(waiting, key=lambda x: -x[0])] or ["Nothing is waiting on you. ✅"]
    out += ["", f"**Hanging** (nobody working on it for over {stale_hours}h)"]
    out += [x[1] for x in sorted(hanging, key=lambda x: -x[0])] or ["Nothing is hanging. ✅"]
    if not loop_alive:
        out = ["⚠️ **HQ's work loop has stopped.** Nothing moves until it's restarted: `sudo systemctl restart dmi`.", ""] + out
    out += ["", f"_Updated {time.strftime('%H:%M', time.localtime(now))}_"]
    return "\n".join(out), {"waiting": len(waiting), "hanging": len(hanging)}
