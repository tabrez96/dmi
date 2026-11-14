"""The log explorer: the activity log (VictoriaLogs) as a readable, filterable table, for the operator.

Grafana's log panel dumps raw lines and can't be filtered on a public dashboard, so the dashboard server has its own
page (/dashboard/logs, behind the same login as the logs card). The page asks /dashboard/logs/data; this module turns
the filters into a LogsQL query, runs it, and turns each record into one readable row. Nothing here writes."""

import json
import re
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

KINDS = {"run_start": "run started", "run_end": "run finished", "vm_run": "VM", "ticket_event": "ticket",
         "message": "message", "proposal": "proposal", "api_call": "API call"}
RANGES = {"15m": 900, "1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
MAX_ROWS = 1000


def _literal(text):
    """A LogsQL string literal."""
    return json.dumps(str(text))


def _in(field, values):
    values = [value for value in values if value]
    return f"{field}:in({','.join(_literal(value) for value in values)})" if values else ""


def parse_time(text):
    """'2026-11-05T10:30' (UTC) or an RFC 3339 time -> aware datetime, or None."""
    try:
        moment = datetime.fromisoformat(text or "")
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def window(params, now=None):
    """(start, end) datetimes from ?range=1h or ?from=...&to=..."""
    now = now or datetime.now(UTC)
    start, end = parse_time(params.get("from")), parse_time(params.get("to"))
    if start:
        return start, end or now
    return now - timedelta(seconds=RANGES.get(params.get("range"), RANGES["1h"])), now


def build_query(params, resolve=None):
    """LogsQL for the filters: kind, role, model (comma separated), ref (T-52, I-5), q (any word), bad (only trouble)."""
    parts = ["app:dmi"]
    kinds = [kind for kind in (params.get("kind") or "").split(",") if kind in KINDS]
    for field in ("kind", "role", "model"):
        values = kinds if field == "kind" else [value.strip() for value in (params.get(field) or "").split(",")]
        if values and _in(field, values):          # a crew member is the role on runs, the author or actor elsewhere
            parts.append(f"({_in('role', values)} OR {_in('author', values)} OR {_in('actor', values)})"
                         if field == "role" else _in(field, values))
    ref = re.fullmatch(r"\s*([A-Za-z])-?(\d+)\s*", params.get("ref") or "")
    if ref:
        letter, number = ref.group(1).upper(), ref.group(2)
        if letter == "T":
            parts.append(f'(ticket:={_literal(number)} OR (subject:=ticket subject_id:={_literal(number)}) '
                         f'OR label:~{_literal("^T-" + number + " ")})')
        else:       # I-5 is an idea's board id; P-5 a product's number, which `resolve` (Board.resolve_ref) maps to its id
            try:
                product_id = resolve(f"{letter}-{number}")[1] if resolve else (number if letter == "I" else None)
            except KeyError:
                product_id = None
            by_id = f"(subject:=product subject_id:={_literal(product_id)}) OR " if product_id is not None else ""
            parts.append(f'({by_id}label:~{_literal("^" + letter + "-" + number + " ")})')
    text = (params.get("q") or "").strip()
    if text:
        parts.append(f"*:{_literal(text[:200])}")
    if params.get("bad") in ("1", "true", "on"):
        parts.append('(ok:="0" OR status:~"^([45]|error)" OR exit_code:~"^-?[1-9]" OR to_state:in("blocked","escalated") OR event:="run_failed")')
    return " ".join(parts)


def summarise(record):
    """One readable row from a log record: (who, ref, what, outcome, detail)."""
    kind = record.get("kind", "")
    ref = ""
    who = record.get("role") or record.get("author") or record.get("actor") or ""
    if kind in ("run_start", "run_end", "vm_run"):
        ref = (f"{record['subject']} {record.get('subject_id', '')}".strip() if record.get("subject")
               else record.get("label", ""))
    elif kind == "ticket_event":
        ref = f"T-{record.get('ticket')}"
    elif kind == "message":
        ref = f"{record.get('subject', '')} {record.get('subject_id', '')}".strip()
    elif kind == "api_call":
        ref = record.get("ref", "")
    if kind == "run_start":
        what, outcome, detail = f"started on {record.get('model', '?')}", "", ""
    elif kind == "run_end":
        ok = record.get("ok") == "1"
        cost = f"${float(record['cost_usd']):.2f}" if record.get("cost_usd") not in (None, "") else ""
        what = f"finished in {record.get('duration_s', '?')}s" + (f", {cost}" if cost else "")
        outcome, detail = ("ok" if ok else "failed"), record.get("model", "")
    elif kind == "vm_run":
        what, outcome = f"VM {record.get('label', '')}", ("ok" if record.get("exit_code") in ("0", 0) else f"exit {record.get('exit_code')}")
        detail = ""
    elif kind == "ticket_event":
        what = f"{record.get('from_state') or ''} → {record.get('to_state') or record.get('event', '')}".strip(" →")
        outcome, detail = record.get("event", ""), record.get("note", "")
        who = record.get("actor", "")
    elif kind == "message":
        what = f"→ {record.get('recipient') or 'all'}: " + " ".join((record.get("body") or "").split())[:240]
        outcome, detail = record.get("message_kind", ""), record.get("verdict", "") or ""
    elif kind == "proposal":
        what, outcome, detail = f"proposed a {record.get('proposal_kind', '')}", "", record.get("payload", "")
    elif kind == "api_call":
        what = f"{record.get('method', '')} {record.get('target', '')}"
        outcome, detail = str(record.get("status", "")), record.get("provider", "")
    else:
        what, outcome, detail = record.get("_msg", ""), "", ""
    return {"who": who, "ref": ref, "what": what, "outcome": outcome, "detail": detail}


def _get(base, path, params):
    url = f"{base.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=20) as response:
        return response.read().decode()


def rows(base, params, now=None, resolve=None):
    """Matching records, newest first, as readable rows (plus the raw record for the details view)."""
    start, end = window(params, now)
    limit = max(1, min(int(params.get("limit") or 300), MAX_ROWS))
    query = build_query(params, resolve) + f" | sort by (_time) desc | limit {limit}"
    body = _get(base, "/select/logsql/query", {"query": query, "start": start.isoformat(), "end": end.isoformat()})
    found = []
    for line in body.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        row = {"time": record.get("_time", ""), "kind": record.get("kind", ""), **summarise(record)}
        row["raw"] = {name: value for name, value in record.items() if name not in ("_stream", "_stream_id", "app")}
        found.append(row)
    return found


def histogram(base, params, now=None, buckets=48, resolve=None):
    """Events per time bucket, split by kind: {"step": seconds, "times": [...], "series": {kind: [n, ...]}}."""
    start, end = window(params, now)
    span = max(60, int((end - start).total_seconds()))
    step = max(60, span // buckets)
    step = next((size for size in (60, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400) if size >= step), 86400)
    body = _get(base, "/select/logsql/hits", {"query": build_query(params, resolve), "start": start.isoformat(),
                                               "end": end.isoformat(), "step": f"{step}s", "field": "kind"})
    data = json.loads(body or "{}").get("hits", [])
    times = sorted({moment for hit in data for moment in hit.get("timestamps", [])})
    series = {}
    for hit in data:
        if not hit.get("fields", {}).get("kind"):
            continue                 # a record with no kind (not one of ours)
        counts = dict(zip(hit.get("timestamps", []), hit.get("values", [])))
        series[hit["fields"].get("kind", "?")] = [counts.get(moment, 0) for moment in times]
    return {"step": step, "times": times, "series": series}


def options(base, params, now=None):
    """Values to offer in the filters (kinds, roles, models) within the time window."""
    start, end = window(params, now)
    found = {}
    for field in ("kind", "role", "author", "actor", "model"):
        try:
            body = _get(base, "/select/logsql/field_values", {"query": "app:dmi", "field": field,
                                                               "start": start.isoformat(), "end": end.isoformat()})
            found[field] = [entry["value"] for entry in json.loads(body).get("values", []) if entry.get("value")]
        except (OSError, ValueError):            # the log is down or answered nonsense: offer nothing
            found[field] = []
    crew = sorted(set(found["role"] + found["author"] + found["actor"]))    # who did it, whichever field says so
    return {"kind": found["kind"], "role": crew, "model": found["model"]}


PAGE = r"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Log explorer</title>
<style>
:root{color-scheme:dark;--bg:#0d0d10;--fg:#eceae4;--mute:#97938a;--card:#16161b;--line:#2a2a31;--acc:#f5a524;--ok:#34d399;--bad:#f87171}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;padding:18px max(14px,3vw)}
a{color:var(--acc)}h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 14px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
button,select,input{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
button{cursor:pointer}button.on{border-color:var(--acc);color:var(--acc)}
input[type=search],input[type=text]{min-width:150px}input[type=datetime-local]{color-scheme:dark}
.chip{padding:3px 10px;border-radius:99px;font-size:12px}
#chart{display:flex;align-items:flex-end;gap:2px;height:90px;margin:8px 0 4px;border-bottom:1px solid var(--line)}
#chart button{all:unset;flex:1;display:flex;flex-direction:column-reverse;min-width:3px;cursor:pointer}#chart button:hover,#chart button:focus-visible{outline:1px solid var(--acc)}
#axis{display:flex;justify-content:space-between;color:var(--mute);font-size:12px;margin-bottom:10px}
#legend span{margin-right:12px;font-size:12px;color:var(--mute)}#legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}
table{width:100%;border-collapse:collapse}th{text-align:left;color:var(--mute);font-weight:500;position:sticky;top:0;background:var(--bg);padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:5px 8px;border-bottom:1px solid #1d1d23;vertical-align:top}tr.r{cursor:pointer}tr.r:hover td{background:#17171d}
td.t{white-space:nowrap;color:var(--mute)}td.t button{all:unset;cursor:pointer}td.t button:focus-visible{outline:1px solid var(--acc)}td.what{word-break:break-word}.k{white-space:nowrap}.bad{color:var(--bad)}.good{color:var(--ok)}
pre{margin:0;white-space:pre-wrap;word-break:break-word;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px}
#count{color:var(--mute);margin:6px 0}@media(max-width:700px){.hide-s{display:none}}
</style>
<h1>Log explorer</h1>
<p class=sub>Everything HQ did, newest first. Pick a time range, narrow it down, click a row for the full record, click a bar to zoom in. <a href="/dashboard">Dashboard</a> · <a href="/dashboard/logout">Log out</a></p>
<div class=bar id=ranges></div>
<div class=bar>
 <input type=datetime-local id=from title="From (UTC)"> to <input type=datetime-local id=to title="To (UTC)"><button id=go>Apply</button>
</div>
<div class=bar>
 <span id=kinds></span>
 <select id=role><option value="">any crew member</option></select>
 <select id=model><option value="">any model</option></select>
 <input type=text id=ref placeholder="T-52 or I-5" size=10>
 <input type=search id=q placeholder="search any word" size=22>
 <label><input type=checkbox id=bad> only trouble (failed, errors, blocked)</label>
 <label><input type=checkbox id=auto> auto-refresh</label>
 <button id=csv>Download JSON</button>
</div>
<div id=chart></div><div id=axis><span id=a1></span><span id=a2></span></div><div id=legend></div>
<div id=count></div>
<table><thead><tr><th>Time</th><th>Kind</th><th>Who</th><th>About</th><th>What happened</th><th>Result</th></tr></thead><tbody id=rows></tbody></table>
<script>
const $=s=>document.querySelector(s),COL={run_start:"#7aa7ff",run_end:"#34d399",vm_run:"#a78bfa",ticket_event:"#f5a524",message:"#94a3b8",proposal:"#f472b6",api_call:"#22d3ee"};
const NAMES=__KINDS__,RANGES=["15m","1h","6h","24h","7d","30d"];
const S={range:"1h",kind:new Set(),from:"",to:""};
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function params(){const p=new URLSearchParams();if(S.from){p.set("from",S.from);if(S.to)p.set("to",S.to)}else p.set("range",S.range);
 if(S.kind.size)p.set("kind",[...S.kind].join(","));for(const k of ["role","model","ref","q"])if($("#"+k).value.trim())p.set(k,$("#"+k).value.trim());
 if($("#bad").checked)p.set("bad","1");return p}
function drawRanges(){$("#ranges").innerHTML=RANGES.map(r=>`<button data-r=${r} class="${!S.from&&S.range==r?"on":""}">last ${r}</button>`).join("")}
function drawKinds(avail){$("#kinds").innerHTML=Object.keys(NAMES).filter(k=>!avail||avail.includes(k)||S.kind.has(k)).map(k=>
 `<button class="chip ${S.kind.has(k)?"on":""}" data-k=${k} style="border-color:${COL[k]}">${NAMES[k]}</button>`).join(" ")}
function fill(sel,vals){const cur=sel.value,first=sel.options[0].outerHTML;if(cur&&!vals.includes(cur))vals=[cur,...vals];sel.innerHTML=first+vals.map(v=>`<option>${esc(v)}</option>`).join("");sel.value=cur}
function ago(t){return new Date(t).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit",second:"2-digit"})}
let generation=0,loading=0;
async function load(){
 const p=params(),rows=$("#rows"),mine=++generation,get=u=>fetch(u).then(r=>r.json()).catch(e=>({error:"Could not load: "+e}));
 loading++;const [d,h]=await Promise.all([get("logs/data?"+p),get("logs/hist?"+p)]).finally(()=>loading--);
 if(mine!==generation)return;
 if(d.error){$("#count").textContent=d.error;return}
 rows.innerHTML=d.rows.map((r,i)=>`<tr class=r data-i=${i}><td class=t><button aria-expanded=false title="Show the full record">${esc(ago(r.time))}</button></td><td class=k><span style="color:${COL[r.kind]||"#aaa"}">${esc(NAMES[r.kind]||r.kind)}</span></td>
  <td>${esc(r.who)}</td><td>${esc(r.ref)}</td><td class=what>${esc(r.what)}</td><td class="${["failed","blocked"].includes(r.outcome)||/^(exit|[45]\d\d)/.test(r.outcome)?"bad":r.outcome=="ok"||r.outcome=="200"?"good":""}">${esc(r.outcome)}</td></tr>`).join("");
 window.ROWS=d.rows;
 $("#count").textContent=`${d.rows.length}${d.rows.length>=d.limit?"+":""} records (newest first)`+(d.rows.length>=d.limit?` — showing the latest ${d.limit}; narrow the filters or the time range to see others`:"");
 if(h.error||!h.times){$("#chart").innerHTML=$("#legend").innerHTML="";$("#a1").textContent="Chart: "+(h.error||"no data");return}
 const ser=h.series||{},n=h.times.length,max=Math.max(1,...h.times.map((_,i)=>Object.values(ser).reduce((a,v)=>a+v[i],0)));
 $("#chart").innerHTML=h.times.map((t,i)=>`<button data-t="${t}" aria-label="Zoom in on ${esc(ago(t))}" title="${esc(ago(t))}: ${Object.entries(ser).filter(([,v])=>v[i]).map(([k,v])=>NAMES[k]+" "+v[i]).join(", ")}">`+
  Object.entries(ser).map(([k,v])=>`<i style="height:${v[i]/max*88}px;background:${COL[k]||"#888"};display:block"></i>`).join("")+`</button>`).join("");
 $("#chart").dataset.step=h.step;$("#a1").textContent=n?ago(h.times[0]):"";$("#a2").textContent=n?ago(h.times[n-1]):"";
 $("#legend").innerHTML=Object.keys(ser).map(k=>`<span><i style="background:${COL[k]||"#888"}"></i>${NAMES[k]||k}</span>`).join("");
}
let optsGeneration=0;
async function opts(){const mine=++optsGeneration,o=await fetch("logs/options?"+params()).then(r=>r.json()).catch(()=>null);if(!o||mine!==optsGeneration)return;fill($("#role"),o.role);fill($("#model"),o.model);drawKinds(o.kind)}
$("#ranges").onclick=e=>{const r=e.target.dataset.r;if(!r)return;S.range=r;S.from=S.to="";$("#from").value=$("#to").value="";drawRanges();opts();load()};
$("#kinds").onclick=e=>{const k=e.target.dataset.k;if(!k)return;S.kind.has(k)?S.kind.delete(k):S.kind.add(k);drawKinds();load()};
$("#go").onclick=()=>{S.from=$("#from").value?$("#from").value+":00Z":"";S.to=$("#to").value?$("#to").value+":00Z":"";drawRanges();opts();load()};
for(const k of ["role","model","bad"])$("#"+k).onchange=load;
for(const k of ["ref","q"])$("#"+k).onkeydown=e=>{if(e.key=="Enter")load()};
$("#chart").onclick=e=>{const b=e.target.closest("[data-t]");if(!b)return;const t=new Date(b.dataset.t),step=+$("#chart").dataset.step*1000;
 const f=x=>new Date(x).toISOString().slice(0,16);S.from=new Date(t).toISOString().slice(0,19)+"Z";S.to=new Date(+t+step).toISOString().slice(0,19)+"Z";
 $("#from").value=f(t);$("#to").value=f(+t+step);drawRanges();load()};
$("#rows").onclick=e=>{const tr=e.target.closest("tr.r");if(!tr)return;const nx=tr.nextElementSibling,b=tr.querySelector("button");
 if(nx&&nx.classList.contains("raw")){nx.remove();b.setAttribute("aria-expanded","false");return}
 const x=document.createElement("tr");x.className="raw";x.innerHTML=`<td colspan=6><pre>${esc(JSON.stringify(window.ROWS[+tr.dataset.i].raw,null,2))}</pre></td>`;tr.after(x);b.setAttribute("aria-expanded","true")};
$("#csv").onclick=()=>{const b=new Blob([JSON.stringify(window.ROWS||[],null,1)],{type:"application/json"}),a=document.createElement("a");a.href=URL.createObjectURL(b);a.download="dmi-log.json";a.click()};
let timer;$("#auto").onchange=e=>{clearInterval(timer);if(e.target.checked)timer=setInterval(()=>{if(!loading)load()},10000)};
drawRanges();drawKinds();opts();load();
</script>
"""


def page():
    return PAGE.replace("__KINDS__", json.dumps(KINDS))
