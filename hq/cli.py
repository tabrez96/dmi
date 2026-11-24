"""`dmi` — the PM's console for Dunder Mifflin. Everything here is also available in Discord."""

import argparse
import sys
import time
from collections import Counter

from . import policy, refs
from .orchestrator import HQ


def _age(ts):
    s = int(time.time() - ts)
    return f"{s // 86400}d" if s >= 86400 else f"{s // 3600}h" if s >= 3600 else f"{s // 60}m"


def resolve(hq, ref):
    """'T-3' / '3' -> ('ticket', id); 'P-2' (approved product) / 'I-5' (idea) -> ('product', id)."""
    return hq.board.resolve_ref(ref)


def cmd_init(hq, a):
    hq.products_dir.mkdir(parents=True, exist_ok=True)
    hq.workdir.mkdir(parents=True, exist_ok=True)
    print(f"Products live in {hq.products_dir}/<slug>/ (one git repo each)")
    print(f"Board at {hq.board.path}")


def cmd_new(hq, a):
    state = policy.PROPOSED if a.propose else policy.APPROVED
    kind, pid = resolve(hq, a.product)         # every ticket belongs to a product (its repo is where it's built)
    if kind != "product":
        raise ValueError(f"{a.product!r} is a ticket, not a product")
    tid = hq.new_ticket(pid, a.title, a.desc or "", state, builder="designer" if a.designer else "")
    hq.board.post("ticket", tid, "pm", f"New ticket: {a.title}\n\n{a.desc or ''}".strip(), kind="status")
    print(f"T-{tid} created ({state})")


def cmd_canvas(hq, a):
    pid = resolve(hq, a.ref)[1]
    hq.set_canvas(pid, a.url)
    print(f"{hq.board.pref(pid)}: canvas linked and posted")


def _plabel(p):
    """'Nudge: tagline' for an approved product (its title already starts with its name); 'I-3 title' for an idea."""
    return p["title"] if p.get("slug") else f"{refs.product_ref(p)} {p['title']}"


def cmd_repo(hq, a):
    pid = resolve(hq, a.ref)[1]
    slug = hq.set_repo(pid, a.repo)
    print(f"{hq.board.pref(pid)} -> github.com/{slug} (history pushed; each ticket now opens a pull request)")


def cmd_rename(hq, a):
    pid = resolve(hq, a.ref)[1]
    title, slug = hq.rename_product(pid, a.name, a.tagline)
    print(f"{hq.board.pref(pid)}: {title}" + (f"   (products/{slug})" if slug else ""))


def cmd_tidy(hq, a):
    pid = resolve(hq, a.ref)[1]
    old, pushed = hq.tidy_history(pid, push=not a.no_push)
    print(f"{hq.board.pref(pid)}: {old} commits -> 2" + ("; force-pushed to GitHub" if pushed else ""))


def cmd_branches(hq, a):
    pid = resolve(hq, a.ref)[1]
    if not hq.board.product(pid).get("github_repo"):
        raise SystemExit(f"{hq.board.pref(pid)} has no GitHub repo yet: dmi repo {hq.board.pref(pid)} owner/name")
    hq.publish_branches(pid)
    print(f"{hq.board.pref(pid)}: develop and master pushed")


def cmd_issues(hq, a):
    pid = resolve(hq, a.ref)[1]
    print(f"{hq.board.pref(pid)}: {hq.sync_issues(pid)} issue(s) created")


def cmd_design(hq, a):
    pid = hq.start_design(a.title or "", a.desc or "", a.from_ticket)
    if hq.board.product(pid)["state"] == policy.DISCOVERING:
        print(f"{hq.board.pref(pid)} created; the analyst validates the idea and talks to you about it on the next tick")
    else:
        print(f"{hq.board.pref(pid)} created at the UX stage; the designer starts on the next tick")


def cmd_discover(hq, a):
    pid = hq.discover(a.focus)
    print(f"{hq.board.pref(pid)} discovery started; the scout runs on the next tick")


def _print_group(title, items, needs_pm, fmt):
    if items:
        flag = "  <- needs you" if needs_pm else ""
        print(f"\n{title.upper()} ({len(items)}){flag}")
        for x in items:
            print("  " + fmt(x))


def cmd_board(hq, a):
    done_p = (policy.LAUNCHED, policy.P_REJECTED)
    done_t = (policy.SHIPPED, policy.REJECTED)
    products = [p for p in hq.board.products() if a.all or p["state"] not in done_p]
    tickets = [t for t in hq.board.list() if a.all or t["state"] not in done_t]
    if not products and not tickets:
        print("Board is empty. Start with: dmi discover \"focus\"  or  dmi new \"title\"")
        return
    if products:
        print("== PRODUCTS ==")
        for state in policy.PRODUCT_STATES:
            _print_group(state, [p for p in products if p["state"] == state],
                         state in policy.PRODUCT_NEEDS_PM,
                         lambda p: _plabel(p)[:72]
                                   + (f"  [{p['next_speaker']}'s turn, round {p['round'] + 1}]"
                                      if p["state"] == policy.DISCOVERING else "")
                                   + f"  ({_age(p['updated_at'])})")
    if tickets:
        print("\n== TICKETS ==")
        for state in policy.STATES:
            _print_group(state, [t for t in tickets if t["state"] == state], state in policy.NEEDS_PM,
                         lambda t: f"T-{t['id']:<4} {t['title'][:60]}"
                                   + (f"  ({hq.board.pref(t['product_id'])})" if t["product_id"] else "")
                                   + (f"  qa-round:{t['qa_failures']}" if t["qa_failures"] else "")
                                   + (f"  [{t['claimed_by']} working]" if t["claimed_by"] else "")
                                   + f"  ({_age(t['updated_at'])})")


def _print_thread(messages):
    print("\n--- conversation ---")
    for m in messages:
        meta = " ".join(x for x in [f"-> {m['recipient']}" if m["recipient"] else "",
                                    f"r{m['round']}" if m["round"] is not None else "",
                                    m["verdict"] or "", f"[{m['kind']}]" if m["kind"] != "say" else ""] if x)
        body = m["body"] if len(m["body"]) < 600 else m["body"][:600] + " …"
        print(f"\n{time.strftime('%m-%d %H:%M', time.localtime(m['ts']))}  {m['author']} {meta}\n{body}")


def cmd_show(hq, a):
    kind, rid = resolve(hq, a.ref)
    if kind == "product":
        p = hq.board.product(rid)
        repo = f"{hq.cfg['products_dir']}/{p['slug']}" if p["slug"] else "(created when the design is approved)"
        print(f"{_plabel(p)}\nstate: {p['state']}   round: {p['round']}   repo: {repo}")
        if p.get("github_repo"):
            print(f"github: https://github.com/{p['github_repo']}")
        if p.get("canvas_url"):
            print(f"canvas: {p['canvas_url']}")
        if p["brief"]:
            print(f"\n--- brief ---\n{p['brief']}")
        if p["ux"]:
            print(f"\n--- UX spec ---\n{p['ux']}")
        if p["slug"]:
            print(f"\n--- screens ({hq.cfg['products_dir']}/{p['slug']}/design/) ---\n{hq.screens_text(rid)}")
        if p["design"]:
            print(f"\n--- design ---\n{p['design']}")
        ts = hq.board.list(product_id=rid)
        if ts:
            print("\n--- tickets ---")
            for t in ts:
                print(f"  T-{t['id']:<4} [{t['state']}] {t['title']}")
        return _print_thread(hq.board.messages("product", rid))
    t = hq.board.get(rid)
    print(f"T-{t['id']}: {t['title']}\nstate: {t['state']}   branch: {t['branch'] or '-'}   "
          f"qa rounds failed: {t['qa_failures']}   questions: {t['clarify_rounds']}\n")
    print(t["description"] or "(no description)")
    if t["plan"]:
        print(f"\n--- plan ---\n{t['plan']}")
    _print_thread(hq.board.messages("ticket", rid))


def _action(name):
    def fn(hq, a):
        kind, rid = resolve(hq, a.ref)
        hq.act(kind, rid, name, getattr(a, "note", None) or getattr(a, "text", None))
        row = hq.board.product(rid) if kind == "product" else hq.board.get(rid)
        print(f"{a.ref.upper()} -> {row['state']}")
    return fn


def cmd_screens(hq, a):
    print(hq.screens_text(resolve(hq, a.ref)[1] if a.ref else None))


def cmd_library(hq, a):
    hq.migrate_designs()
    base = (hq.cfg.get("dashboard") or {}).get("public_url", "").rstrip("/")
    print(f"Design library: each product's design/ folder ({hq.cfg['products_dir']}/<name>/design/)")
    print(f"Gallery: {base}/design/" if base else "Gallery: the dashboard's /design/ page")


def cmd_ci_webhook(hq, a):
    """Point every product repo's GitHub webhook at HQ, so a failed build reaches HQ the moment it happens."""
    import os

    from .github import GitHubError
    base = (hq.cfg.get("dashboard") or {}).get("public_url", "").rstrip("/")
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not (base and secret and hq.github):
        raise SystemExit("needs dashboard.public_url in config, GITHUB_WEBHOOK_SECRET and GITHUB_TOKEN in .env")
    url = f"{base}/hooks/github"
    for p in hq.board.products():
        if p.get("github_repo"):
            try:
                print(f"{p['github_repo']}: {hq.github.ensure_webhook(p['github_repo'], url, secret)}")
            except GitHubError as e:
                print(f"{p['github_repo']}: {e}\n  add it by hand: repo Settings > Webhooks > Add webhook\n"
                      f"  URL {url}, content type application/json, your secret, event 'Workflow runs'")


def cmd_tick(hq, a):
    n = hq.tick()
    print(f"{n} job(s) run" if n else "Nothing to do.")


def cmd_run(hq, a):
    try:
        hq.run_forever()
    except KeyboardInterrupt:
        print("\nHQ stopped.")


def cmd_digest(hq, a):
    print(digest(hq, a.hours))


def digest(hq, hours=24):
    since = time.time() - hours * 3600
    tickets, products = hq.board.list(), hq.board.products()
    tc = Counter(t["state"] for t in tickets)
    pc = Counter(p["state"] for p in products)
    runs = hq.board.runs_since(since)
    cost = sum(r["cost_usd"] or 0 for r in runs)
    out = [f"**Dunder Mifflin digest: last {hours}h**",
           "Products: " + (", ".join(f"{s} {pc[s]}" for s in policy.PRODUCT_STATES if pc[s]) or "none"),
           "Tickets: " + (", ".join(f"{s} {tc[s]}" for s in policy.STATES if tc[s]) or "none"),
           f"Agent runs: {len(runs)} ({sum(1 for r in runs if r['ok'])} ok), Claude spend ${cost:.2f}"]
    by_role = Counter(r["role"] for r in runs)
    if by_role:
        out.append("  " + ", ".join(f"{role} {n}" for role, n in by_role.items()))
    waiting = [f"  [{p['state']}] {_plabel(p)[:70]}" for p in products
               if p["state"] in policy.PRODUCT_NEEDS_PM]
    waiting += [f"  T-{t['id']} [{t['state']}] {t['title'][:60]}" for t in tickets
                if t["state"] in policy.NEEDS_PM]
    if waiting:
        out += ["", "Needs you:", *waiting]
    shipped = [f"  T-{t['id']} {t['title'][:60]}" for t in tickets
               if t["state"] == policy.SHIPPED and t["updated_at"] >= since]
    if shipped:
        out += ["", "Shipped:", *shipped]
    return "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(prog="dmi", description="Dunder Mifflin HQ — you are the PM.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the product repo and board").set_defaults(fn=cmd_init)

    s = sub.add_parser("new", help="add a ticket to a product directly (approved unless --propose)")
    s.add_argument("product", help="the product it belongs to, e.g. nudge")
    s.add_argument("title")
    s.add_argument("-d", "--desc")
    s.add_argument("--propose", action="store_true", help="park it in 'proposed' for later")
    s.add_argument("--designer", action="store_true", help="design work (logo, screens, UX): Pam builds it, not the engineer")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("design", help="skip discovery: send an idea straight to the designer")
    s.add_argument("title", nargs="?")
    s.add_argument("-d", "--desc", help="the brief")
    s.add_argument("--from-ticket", type=int, help="take the title and brief from this ticket (and its plan)")
    s.set_defaults(fn=cmd_design)

    s = sub.add_parser("discover", help="start a scout <-> analyst discovery session")
    s.add_argument("focus", nargs="?", help="what to look for (default: discovery.focus)")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("board", help="show products and tickets")
    s.add_argument("-a", "--all", action="store_true", help="include finished items")
    s.set_defaults(fn=cmd_board)

    s = sub.add_parser("show", help="details + conversation, e.g. `dmi show P-1` or `dmi show 4`")
    s.add_argument("ref")
    s.set_defaults(fn=cmd_show)

    for name, help_, note in [
        ("approve", "approve a brief / design / launch / plan / ship", "optional"),
        ("reject", "reject or drop, with feedback", "optional"),
        ("retry", "unblock a ticket with a fresh round count", "optional"),
        ("manager", "ask the Manager to step in on a blocked ticket or product", "optional"),
        ("extra_round", "give a blocked product conversation one more round", "optional"),
        ("brief_anyway", "overrule the analyst's 'nothing worth building' and get the brief", "optional"),
    ]:
        s = sub.add_parser(name.replace("_", "-"), help=help_)
        s.add_argument("ref")
        s.add_argument("-n", "--note", help="message the agents will see")
        s.set_defaults(fn=_action(name))

    s = sub.add_parser("say", help="post in a thread, e.g. `dmi say P-1 \"use Postgres\"`")
    s.add_argument("ref")
    s.add_argument("text")
    s.set_defaults(fn=_action("guidance"))

    s = sub.add_parser("screens", help="list design-library screens (all products, or e.g. `dmi screens P-2`)")
    s.add_argument("ref", nargs="?")
    s.set_defaults(fn=cmd_screens)
    s = sub.add_parser("canvas", help="link a product's Claude Design canvas, e.g. `dmi canvas P-1 https://claude.ai/artifact/...`")
    s.add_argument("ref")
    s.add_argument("url")
    s.set_defaults(fn=cmd_canvas)
    s = sub.add_parser("repo", help="connect a product to its GitHub repo, e.g. `dmi repo P-3 tabrez96/cadence`")
    s.add_argument("ref")
    s.add_argument("repo")
    s.set_defaults(fn=cmd_repo)
    s = sub.add_parser("rename", help="rename a product and its directory, e.g. `dmi rename P-3 Nudge` "
                                      "(no name: keep it and just shorten the directory)")
    s.add_argument("ref")
    s.add_argument("name", nargs="?")
    s.add_argument("--tagline")
    s.set_defaults(fn=cmd_rename)
    s = sub.add_parser("tidy", help="squash a product's design history into one commit (before any ticket has built on it)")
    s.add_argument("ref")
    s.add_argument("--no-push", action="store_true", help="don't force-push the rewritten history to GitHub")
    s.set_defaults(fn=cmd_tidy)
    s = sub.add_parser("branches", help="push develop + master to a product's GitHub repo")
    s.add_argument("ref")
    s.set_defaults(fn=cmd_branches)
    s = sub.add_parser("issues", help="mirror a product's tickets as GitHub issues (those without one yet)")
    s.add_argument("ref")
    s.set_defaults(fn=cmd_issues)
    sub.add_parser("library", help="bring older products into the design library and print where the gallery is").set_defaults(fn=cmd_library)

    sub.add_parser("ci-webhook", help="register the GitHub webhook that reports failed builds to HQ").set_defaults(fn=cmd_ci_webhook)
    sub.add_parser("tick", help="run one pass of agent turns, then exit").set_defaults(fn=cmd_tick)
    sub.add_parser("run", help="run HQ continuously (no Discord)").set_defaults(fn=cmd_run)

    s = sub.add_parser("digest", help="summary of the last N hours")
    s.add_argument("--hours", type=int, default=24)
    s.set_defaults(fn=cmd_digest)

    a = p.parse_args(argv)
    hq = HQ()
    try:
        a.fn(hq, a)
    except (KeyError, ValueError, policy.IllegalTransition) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
