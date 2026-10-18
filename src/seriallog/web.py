"""A read-only web dashboard over the log folder.

    seriallog web                        http://127.0.0.1:8777
    seriallog web --bind 0.0.0.0:8777    reachable from the network

One HTML page (inline CSS and JS, nothing fetched from elsewhere) and a few
JSON endpoints over the files the daemon writes: status.json, alerts.jsonl
and the day files. It never writes anything and never talks to ser2net or
the daemon, so it runs as a separate unprivileged service.

    GET /api/status                 status.json with every configured port present
    GET /api/tail?port=usb0&n=200   last n entries
    GET /api/follow?port=usb0       new lines as server-sent events
    GET /api/grep?q=PANIC&since=2h&until=&ports=usb0,acm0&events=1&i=1&max=1000
    GET /api/alerts?since=24h

Entries are JSON objects like `seriallog tail --json`. Bad input gets a 400
with a one-line reason. There is no authentication: keep it on localhost or
a trusted network (an SSH port forward, Tailscale).
"""

import json
import re
import select
import socket
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from seriallog import cli, config as cfg, logfile, when
from seriallog.daemon import STATUS_FILE

DEFAULT_BIND = "127.0.0.1:8777"
MAX_TAIL = 5000
MAX_GREP = 100000


# ---------- Endpoints ----------

def number(q, key, default, lo, hi):
    """An integer query parameter, clamped to [lo, hi]."""
    try:
        return max(lo, min(hi, int(q.get(key, default))))
    except ValueError:
        raise ValueError(f"{key} must be a whole number") from None


def flag(q, key):
    return q.get(key, "") not in ("", "0", "false", "no")


def page(h, conf, q):
    h.reply(200, PAGE, "text/html")


def api_status(h, conf, q):
    """status.json, ports in config order (missing ones too), plus whether it is stale."""
    status = cli.load_json(conf.log_dir / STATUS_FILE, {})
    known = status.get("ports", {})
    updated = status.get("updated")
    try:
        age = (datetime.now().astimezone() - logfile.parse_stamp(updated)).total_seconds()
    except (TypeError, ValueError):
        age = None
    h.json({
        "updated": updated,
        "stale": age is None or age > cli.STALE_STATUS,
        "ports": {p.name: known.get(p.name, {"endpoint": f"{p.host}:{p.port}", "state": None})
                  for p in conf.ports},
    })


def api_tail(h, conf, q):
    name = conf.port(q.get("port", "")).name
    n = number(q, "n", 200, 1, MAX_TAIL)
    h.json([cli.entry_dict(e) for e in logfile.last_lines(conf.log_dir, name, n)])


def api_follow(h, conf, q):
    """Server-sent events: one `data:` line per new log line, until the browser goes away."""
    name = conf.port(q.get("port", "")).name
    lines = logfile.follow(conf.log_dir, name)
    next(lines)                      # open today's file and skip what /api/tail has shown
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    try:
        for e in lines:
            if e:
                h.wfile.write(f"data: {json.dumps(cli.entry_dict(e), ensure_ascii=False)}\n\n"
                              .encode("utf-8"))
            elif h.client_gone():
                return
    except OSError:                  # browser closed the tab mid-write
        return
    finally:
        lines.close()


def api_grep(h, conf, q):
    rx = re.compile(q.get("q", ""), re.IGNORECASE if flag(q, "i") else 0)
    since = when.parse(q.get("since") or "7d")
    until = when.parse(q["until"]) if q.get("until") else None
    names = cli.ports_named(conf, [p for p in q.get("ports", "").split(",") if p])
    events, limit = flag(q, "events"), number(q, "max", 1000, 1, MAX_GREP)
    found = []
    for e in logfile.read(conf.log_dir, names, since, until):
        if (events or e.kind != logfile.EVENT) and rx.search(e.text):
            found.append(cli.entry_dict(e))
            if len(found) >= limit:
                break
    h.json(found)


def api_alerts(h, conf, q):
    h.json(cli.read_alerts(conf, when.parse(q.get("since") or "24h")))


ROUTES = {"/": page, "/api/status": api_status, "/api/tail": api_tail,
          "/api/follow": api_follow, "/api/grep": api_grep, "/api/alerts": api_alerts}


# ---------- Server ----------

class Handler(BaseHTTPRequestHandler):
    server_version, sys_version = "seriallog", ""

    def log_message(self, *args):    # the page polls every 2 s; that is noise under systemd
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        route = ROUTES.get(url.path)
        if route is None:
            self.reply(404, "not found")
            return
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            route(self, self.server.conf, q)
        except (cfg.ConfigError, ValueError, re.error) as e:
            self.reply(400, f"bad request: {e}")

    def reply(self, code, text, ctype="text/plain"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, doc):
        self.reply(200, json.dumps(doc, ensure_ascii=False), "application/json")

    def client_gone(self):
        """True once the browser has hung up: the socket is readable with nothing left to say."""
        return bool(select.select([self.connection], [], [], 0)[0])


class Server(ThreadingHTTPServer):
    """One thread per request; a follow stream must not hold up shutdown (daemon threads)."""

    def __init__(self, address, conf):
        self.conf = conf
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)


def parse_bind(text):
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"--bind wants HOST:PORT, not {text!r}")
    return host.strip("[]"), int(port)


def serve(conf, bind=DEFAULT_BIND):
    srv = Server(parse_bind(bind), conf)
    host, port = srv.server_address[:2]
    shown = f"[{host}]" if ":" in host else host
    print(f"seriallog web: http://{shown}:{port}/ over {conf.log_dir} (read-only, no authentication)",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


# ---------- The page ----------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<title>seriallog</title>
<style>
:root{--bg:#14171c;--panel:#1b1f25;--line:#2a3038;--sel:#242b34;--fg:#d8dde3;--dim:#7d8590;
      --green:#4cc38a;--amber:#e5b454;--red:#f07070}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);font:13px/1.45 ui-monospace,Menlo,Consolas,monospace;
     display:grid;grid-template-rows:auto 1fr;grid-template-columns:240px 1fr;
     grid-template-areas:"top top" "side main"}
[hidden]{display:none!important}
.dim{color:var(--dim)}.amber{color:var(--amber)}.red{color:var(--red)}.green{color:var(--green)}
input,button{font:inherit;color:var(--fg);background:#0f1216;border:1px solid var(--line);
             border-radius:4px;padding:2px 6px}
button{background:var(--sel);cursor:pointer}button:hover{background:#2e3844}
label{white-space:nowrap}
header{grid-area:top;display:flex;align-items:center;gap:12px;padding:5px 10px;
       background:var(--panel);border-bottom:1px solid var(--line);min-width:0}
header h1{font-size:14px;margin:0;white-space:nowrap}
#alerts{flex:1;display:flex;gap:6px;overflow-x:auto;min-width:0;align-items:center}
.alert{flex:none;max-width:460px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
       background:#2a2418;border-color:#5b4a22;text-align:left}
nav{grid-area:side;overflow-y:auto;background:var(--panel);border-right:1px solid var(--line)}
.port{display:block;width:100%;text-align:left;border:0;border-bottom:1px solid var(--line);
      border-radius:0;background:none;padding:7px 10px}
.port.sel{background:var(--sel)}
.port .head{display:flex;align-items:center;gap:6px}
.port .head b{flex:1}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.connected .dot{background:var(--green)}.unreachable .dot{background:var(--amber)}
.absent,.unknown{color:var(--dim)}.unreachable .st{color:var(--amber)}.connected .st{color:var(--green)}
.port .last{color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
main{grid-area:main;display:grid;grid-template-rows:auto 1fr;min-height:0}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:5px 10px;
     border-bottom:1px solid var(--line)}
#search{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-left:auto}
#tail,#results{overflow-y:auto;padding:4px 10px}
.line{display:flex;gap:10px}
.line .t{color:var(--dim);flex:none}
.line .p{color:var(--red);flex:none;min-width:5ch}
.line .x{white-space:pre-wrap;word-break:break-all}
.line.ev .x{color:var(--amber)}
.line.ev .x::before{content:"! "}
mark{background:#5a3a1a;color:#ffd58a}
</style>
</head>
<body>
<header>
  <h1>seriallog</h1>
  <span id="daemon" class="dim">...</span>
  <div id="alerts"></div>
</header>
<nav id="ports"></nav>
<main>
  <div class="bar">
    <b id="title">-</b>
    <span id="stream" class="dim"></span>
    <button id="pause" title="stop adding lines; they are kept">Pause</button>
    <button id="jump" hidden>&darr; follow</button>
    <button id="clear">Clear</button>
    <form id="search">
      <input id="q" placeholder="regex" size="26" autocomplete="off">
      <label>since <input id="since" value="2h" size="5"></label>
      <label>until <input id="until" size="5" placeholder="now"></label>
      <label><input id="icase" type="checkbox" checked> ignore case</label>
      <label><input id="events" type="checkbox"> events</label>
      <label><input id="only" type="checkbox"> this port</label>
      <button>Search</button>
    </form>
  </div>
  <div id="tail"></div>
  <div id="results" hidden></div>
</main>
<script>
"use strict";
const $ = s => document.querySelector(s);
const MAX_LINES = 2000, TAIL = 200, GREP_MAX = 1000;
const st = {port: null, es: null, paused: false, pending: [], follow: true};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
const pad = n => String(n).padStart(2, "0");
function today() { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
// "19:40:01.123" today, "10-17 19:40:01.123" on another day; stamps are local time already
function when(iso) { return iso.slice(0, 10) === today() ? iso.slice(11, 23) : iso.slice(5, 23).replace("T", " "); }

// ----- ports and daemon state, every 2 s -----
async function refreshStatus() {
  const d = $("#daemon");
  let s;
  try { s = await (await fetch("/api/status")).json(); }
  catch (e) { d.textContent = "dashboard unreachable"; d.className = "red"; return; }
  d.textContent = !s.updated ? "no status: is the seriallog service running?"
    : s.stale ? `status last written ${when(s.updated)}: service stopped?` : `daemon ok ${when(s.updated).slice(0, 8)}`;
  d.className = s.stale ? "amber" : "dim";
  const names = Object.keys(s.ports), list = $("#ports");
  list.replaceChildren(...names.map(name => {
    const p = s.ports[name], state = p.state || "unknown";
    const b = el("button", `port ${state}` + (name === st.port ? " sel" : ""));
    b.onclick = () => select(name);
    const head = el("div", "head");
    head.append(el("span", "dot"), el("b", null, name), el("span", "st", state));
    b.append(head);
    b.append(el("div", "dim", p.since ? `since ${when(p.since).slice(0, -4)}` : p.endpoint || ""));
    if (p.last_line_at) b.append(el("div", "last", `${when(p.last_line_at).slice(0, 8)} ${p.last_line}`));
    b.title = p.detail ? `${p.endpoint}: ${p.detail}` : p.endpoint || "";
    return b;
  }));
  if (!st.port && names.length) select(names[0]);
}

// ----- alerts strip, every 10 s -----
async function refreshAlerts() {
  let a;
  try { a = await (await fetch("/api/alerts?since=24h")).json(); } catch (e) { return; }
  const strip = $("#alerts"), shown = a.slice(-8).reverse();
  strip.replaceChildren(...shown.map(r => {
    const b = el("button", "alert");
    b.append(el("span", "dim", when(r.time).slice(0, -4) + " "), el("b", null, r.port),
             el("span", "amber", ` ${r.alert}: `), document.createTextNode(r.text));
    b.title = r.text;
    b.onclick = () => select(r.port);
    return b;
  }));
  if (!a.length) strip.append(el("span", "dim", "no alerts in the last 24 h"));
  else if (a.length > shown.length) strip.append(el("span", "dim", `+${a.length - shown.length} more`));
}

// ----- live tail of one port -----
function line(e, withPort) {
  const d = el("div", "line" + (e.kind === "event" ? " ev" : ""));
  if (withPort) d.append(el("span", "p", e.port));
  d.append(el("span", "t", when(e.time)), el("span", "x", e.text));
  return d;
}
function scrollEnd() { const t = $("#tail"); t.scrollTop = t.scrollHeight; }
function addLine(e) {
  const t = $("#tail");
  t.append(line(e));
  while (t.childElementCount > MAX_LINES) t.firstElementChild.remove();
  if (st.follow) scrollEnd();
}
function push(e) {
  if (!st.paused) return addLine(e);
  st.pending.push(e);
  $("#pause").textContent = `Resume (${st.pending.length})`;
}
function select(name) {
  if (st.es) st.es.close();
  st.port = name; st.paused = false; st.pending = []; st.follow = true;
  $("#pause").textContent = "Pause"; $("#jump").hidden = true;
  $("#title").textContent = name; document.title = `${name} - seriallog`;
  for (const b of document.querySelectorAll(".port")) b.classList.toggle("sel", b.querySelector("b").textContent === name);
  showTail();
  $("#tail").replaceChildren();
  // Open the stream first so nothing slips between the history and the live lines; events that
  // arrive before the history is in are kept and appended if they are newer than its last line.
  const early = [];
  let ready = false;
  const es = st.es = new EventSource(`/api/follow?port=${encodeURIComponent(name)}`);
  es.onmessage = ev => { const e = JSON.parse(ev.data); ready ? push(e) : early.push(e); };
  es.onopen = () => { $("#stream").textContent = ""; };
  es.onerror = () => { $("#stream").textContent = "stream lost, retrying"; $("#stream").className = "amber"; };
  fetch(`/api/tail?port=${encodeURIComponent(name)}&n=${TAIL}`).then(r => r.json()).then(rows => {
    if (st.es !== es) return;
    rows.forEach(addLine);
    const last = rows[rows.length - 1];
    for (const e of early)
      if (!last || e.time > last.time || (e.time === last.time && e.text !== last.text)) addLine(e);
    ready = true;
    scrollEnd();
  });
}
$("#pause").onclick = () => {
  st.paused = !st.paused;
  if (!st.paused) { st.pending.forEach(addLine); st.pending = []; }
  $("#pause").textContent = st.paused ? "Resume" : "Pause";
};
$("#clear").onclick = () => $("#tail").replaceChildren();
$("#jump").onclick = () => { st.follow = true; $("#jump").hidden = true; scrollEnd(); };
$("#tail").onscroll = () => {          // scrolling up stops autoscroll, scrolling back down resumes it
  const t = $("#tail");
  st.follow = t.scrollTop + t.clientHeight >= t.scrollHeight - 30;
  $("#jump").hidden = st.follow;
};

// ----- search -----
function showTail() { $("#tail").hidden = false; $("#results").hidden = true; }
function highlight(span, rx) {
  if (!rx) return;
  const text = span.textContent, parts = [];
  let i = 0;
  for (const m of text.matchAll(rx)) {
    if (!m[0]) continue;
    parts.push(text.slice(i, m.index), el("mark", null, m[0]));
    i = m.index + m[0].length;
  }
  parts.push(text.slice(i));
  span.replaceChildren(...parts);
}
$("#search").onsubmit = async ev => {
  ev.preventDefault();
  const q = $("#q").value, params = new URLSearchParams({
    q, since: $("#since").value || "7d", until: $("#until").value, max: GREP_MAX,
    i: $("#icase").checked ? 1 : 0, events: $("#events").checked ? 1 : 0});
  if ($("#only").checked && st.port) params.set("ports", st.port);
  const res = $("#results"), head = el("div", "bar");
  const back = el("button", null, "back to live");
  back.onclick = showTail;
  res.replaceChildren(head);
  $("#tail").hidden = true; res.hidden = false;
  const r = await fetch(`/api/grep?${params}`);
  if (!r.ok) { head.append(el("span", "red", await r.text()), back); return; }
  const rows = await r.json();
  head.append(el("b", null, `${rows.length}${rows.length >= GREP_MAX ? "+" : ""} matches for /${q}/`), back);
  let rx = null;                       // Python and JS regex syntax mostly agree; if not, no marks
  try { rx = new RegExp(q, $("#icase").checked ? "gi" : "g"); } catch (e) {}
  for (const e of rows) { const d = line(e, true); highlight(d.lastChild, rx); res.append(d); }
};

refreshStatus(); refreshAlerts();
setInterval(refreshStatus, 2000);
setInterval(refreshAlerts, 10000);
</script>
</body>
</html>
"""
