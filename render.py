"""HTML/SVG rendering for the git-monitor dashboard.

Server-side rendered — no build step, no CDN, no client framework. The heatmap
is a plain SVG grid (GitHub-style); tooltips are native `title` elements. The
only JavaScript is a tiny fetch for the Refresh button.
"""

import html
import json
from datetime import date, datetime, timedelta, timezone

# GitHub-ish green scale on a dark card.
LEVELS = ["#1b1f24", "#0e4429", "#006d32", "#26a641", "#39d353"]
WEEKDAY_LABELS = {1: "Mon", 3: "Wed", 5: "Fri"}
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _level(count):
    if not count:
        return 0
    if count <= 2:
        return 1
    if count <= 5:
        return 2
    if count <= 9:
        return 3
    return 4


def esc(s):
    return html.escape("" if s is None else str(s))


def rel_time(iso):
    """Human 'x ago' from an ISO-8601 Z timestamp."""
    if not iso:
        return "never"
    try:
        t = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return esc(iso)
    delta = datetime.now(timezone.utc) - t
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return "%dm ago" % (s // 60)
    if s < 86400:
        return "%dh ago" % (s // 3600)
    d = s // 86400
    if d < 30:
        return "%dd ago" % d
    if d < 365:
        return "%dmo ago" % (d // 30)
    return "%dy ago" % (d // 365)


def render_heatmap(commit_days):
    """Return an SVG string for a 53-week commit heatmap ending today (UTC)."""
    cell, gap = 12, 3
    pitch = cell + gap
    left, top = 30, 18

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=364)
    # Back up to the preceding Sunday so weeks line up in columns.
    grid_start = start - timedelta(days=(start.weekday() + 1) % 7)

    weeks = []
    d = grid_start
    while d <= today:
        col = []
        for _ in range(7):
            col.append(d)
            d += timedelta(days=1)
        weeks.append(col)

    width = left + len(weeks) * pitch + 10
    height = top + 7 * pitch + 24

    parts = ['<svg viewBox="0 0 %d %d" width="%d" height="%d" '
             'class="heatmap" xmlns="http://www.w3.org/2000/svg">'
             % (width, height, width, height)]

    # Month labels along the top.
    last_month = None
    for wi, col in enumerate(weeks):
        m = col[0].month
        if m != last_month and col[0].day <= 7:
            x = left + wi * pitch
            parts.append('<text x="%d" y="%d" class="mlabel">%s</text>'
                         % (x, 12, MONTHS[m]))
            last_month = m

    # Weekday labels down the left.
    for row, label in WEEKDAY_LABELS.items():
        y = top + row * pitch + cell - 2
        parts.append('<text x="0" y="%d" class="wlabel">%s</text>' % (y, label))

    # Day cells.
    total = 0
    for wi, col in enumerate(weeks):
        for row, day in enumerate(col):
            if day > today or day < start:
                continue
            iso = day.isoformat()
            c = commit_days.get(iso, 0)
            total += c
            x = left + wi * pitch
            y = top + row * pitch
            noun = "commit" if c == 1 else "commits"
            parts.append(
                '<rect x="%d" y="%d" width="%d" height="%d" rx="2" fill="%s">'
                '<title>%d %s on %s</title></rect>'
                % (x, y, cell, cell, LEVELS[_level(c)], c, noun, iso))

    # Legend.
    ly = top + 7 * pitch + 14
    lx = width - (len(LEVELS) * pitch + 60)
    parts.append('<text x="%d" y="%d" class="legend">Less</text>' % (lx - 34, ly + cell - 2))
    for i, color in enumerate(LEVELS):
        parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="2" fill="%s"/>'
                     % (lx + i * pitch, ly, cell, cell, color))
    parts.append('<text x="%d" y="%d" class="legend">More</text>'
                 % (lx + len(LEVELS) * pitch + 4, ly + cell - 2))

    parts.append('</svg>')
    return "".join(parts), total


def _machine_totals(repos):
    agg = {}
    for r in repos:
        a = agg.setdefault(r["machine"], {"repos": 0, "dirty": 0, "unpushed": 0})
        a["repos"] += 1
        if (r["dirty"] or 0) > 0:
            a["dirty"] += 1
        if (r["unpushed"] or 0) > 0:
            a["unpushed"] += 1
    return agg


def render_machines(machines, repos):
    totals = _machine_totals(repos)
    cards = []
    for m in machines:
        t = totals.get(m["name"], {"repos": 0, "dirty": 0, "unpushed": 0})
        online = bool(m["reachable"])
        dot = "on" if online else "off"
        status = "online" if online else "offline"
        sub = ("%d repos · %d dirty · %d unpushed"
               % (t["repos"], t["dirty"], t["unpushed"]))
        seen = rel_time(m["last_scanned"])
        err = ""
        if not online and m["error"]:
            err = '<div class="merr" title="%s">%s</div>' % (
                esc(m["error"]), esc(m["error"][:60]))
        cards.append(
            '<div class="mcard %s"><div class="mrow"><span class="dot %s"></span>'
            '<span class="mname">%s</span><span class="mstatus">%s</span></div>'
            '<div class="msub">%s</div><div class="mseen">scanned %s</div>%s</div>'
            % (dot, dot, esc(m["name"]), status, sub, seen, err))
    return '<div class="machines">%s</div>' % "".join(cards)


def render_repos(repos, top_n=10):
    # Render every repo; a client script shows only as many as fit the viewport
    # (5-10, no scrollbar), and the "+ N more" note expands to the full list.
    rows = []
    for r in repos:
        badges = []
        if (r["dirty"] or 0) > 0:
            badges.append('<span class="badge dirty">%d dirty</span>' % r["dirty"])
        if (r["unpushed"] or 0) > 0:
            badges.append('<span class="badge unpushed">%d unpushed</span>' % r["unpushed"])
        if (r["behind"] or 0) > 0:
            badges.append('<span class="badge behind">%d behind</span>' % r["behind"])
        if r["is_bare"]:
            badges.append('<span class="badge bare">bare</span>')
        if not r["has_remote"] and not r["is_bare"]:
            badges.append('<span class="badge noremote">no remote</span>')
        if not badges:
            badges.append('<span class="badge clean">clean</span>')
        rows.append(
            '<tr><td class="rname">%s</td>'
            '<td class="rmachine">%s</td>'
            '<td class="rbranch">%s</td>'
            '<td class="rbadges">%s</td>'
            '<td class="rtime">%s</td></tr>'
            % (esc(r["name"]), esc(r["machine"]), esc(r["branch"] or "—"),
               "".join(badges), rel_time(r["last_commit"])))
    total = len(repos)
    more = '<div class="more" id="repos-more" style="display:none"></div>'
    return (
        '<table class="repos" data-total="%d"><thead><tr><th>Project</th><th>Machine</th>'
        '<th>Branch</th><th>Status</th><th>Last commit</th></tr></thead>'
        '<tbody>%s</tbody></table>%s' % (total, "".join(rows), more))


def render_page(summary, machines, repos, commit_days, top_n=12, last_scan=None):
    heatmap_svg, year_total = render_heatmap(commit_days)
    # Prefer the newest machine scan time from the DB (survives restarts);
    # fall back to the in-memory last-scan timestamp.
    times = [m["last_scanned"] for m in machines if m.get("last_scanned")]
    last_at = max(times) if times else (last_scan.get("at") if last_scan else None)
    scan_at = rel_time(last_at) if last_at else "—"

    stat = lambda label, val, cls="": (
        '<div class="stat %s"><div class="snum">%s</div>'
        '<div class="slabel">%s</div></div>' % (cls, val, label))
    stats = "".join([
        stat("repositories", summary["total_repos"]),
        stat("with uncommitted", summary["dirty_repos"], "warn" if summary["dirty_repos"] else ""),
        stat("with unpushed", summary["unpushed_repos"], "alert" if summary["unpushed_repos"] else ""),
        stat("unpushed commits", summary["unpushed_commits"], "alert" if summary["unpushed_commits"] else ""),
        stat("offline machines", summary["offline_machines"], "warn" if summary["offline_machines"] else ""),
    ])

    out = PAGE
    for key, val in {
        "stats": stats,
        "year_total": str(year_total),
        "heatmap": heatmap_svg,
        "machines": render_machines(machines, repos),
        "repos": render_repos(repos, top_n),
        "scan_at": scan_at,
    }.items():
        out = out.replace("{{%s}}" % key, val)
    return out


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Git Monitor</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxNiAxNiI+PHJlY3Qgd2lkdGg9IjE2IiBoZWlnaHQ9IjE2IiByeD0iMy41IiBmaWxsPSIjMGQxMTE3Ii8+PHJlY3QgeD0iMiIgeT0iMiIgd2lkdGg9IjMiIGhlaWdodD0iMyIgcng9Ii43IiBmaWxsPSIjMGU0NDI5Ii8+PHJlY3QgeD0iNi41IiB5PSIyIiB3aWR0aD0iMyIgaGVpZ2h0PSIzIiByeD0iLjciIGZpbGw9IiMzOWQzNTMiLz48cmVjdCB4PSIxMSIgeT0iMiIgd2lkdGg9IjMiIGhlaWdodD0iMyIgcng9Ii43IiBmaWxsPSIjMDA2ZDMyIi8+PHJlY3QgeD0iMiIgeT0iNi41IiB3aWR0aD0iMyIgaGVpZ2h0PSIzIiByeD0iLjciIGZpbGw9IiMzOWQzNTMiLz48cmVjdCB4PSI2LjUiIHk9IjYuNSIgd2lkdGg9IjMiIGhlaWdodD0iMyIgcng9Ii43IiBmaWxsPSIjMjZhNjQxIi8+PHJlY3QgeD0iMTEiIHk9IjYuNSIgd2lkdGg9IjMiIGhlaWdodD0iMyIgcng9Ii43IiBmaWxsPSIjMGU0NDI5Ii8+PHJlY3QgeD0iMiIgeT0iMTEiIHdpZHRoPSIzIiBoZWlnaHQ9IjMiIHJ4PSIuNyIgZmlsbD0iIzAwNmQzMiIvPjxyZWN0IHg9IjYuNSIgeT0iMTEiIHdpZHRoPSIzIiBoZWlnaHQ9IjMiIHJ4PSIuNyIgZmlsbD0iIzM5ZDM1MyIvPjxyZWN0IHg9IjExIiB5PSIxMSIgd2lkdGg9IjMiIGhlaWdodD0iMyIgcng9Ii43IiBmaWxsPSIjMjZhNjQxIi8+PC9zdmc+">
<style>
:root{ --bg:#0d1117; --card:#161b22; --border:#30363d; --fg:#e6edf3;
       --muted:#8b949e; --accent:#39d353; --warn:#d29922; --alert:#f85149; }
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:1000px;margin:0 auto;padding:24px 20px 20px;}
header{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:18px;}
.headright{display:flex;align-items:center;gap:14px;}
.cfglink{color:var(--muted);font-size:13px;text-decoration:none;border:1px solid var(--border);
 border-radius:6px;padding:6px 12px;}
.cfglink:hover{color:var(--fg);border-color:var(--muted);}
h1{font-size:20px;margin:0;font-weight:600;}
h1 .g{color:var(--accent);}
.sub{color:var(--muted);font-size:13px;}
button{background:#238636;color:#fff;border:0;border-radius:6px;padding:7px 14px;
 font-size:13px;font-weight:600;cursor:pointer;}
button:hover{background:#2ea043;}
button:disabled{opacity:.6;cursor:default;}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;
 padding:18px 20px;margin-bottom:18px;}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin-bottom:18px;}
.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px;}
.stat .snum{font-size:26px;font-weight:700;}
.stat .slabel{color:var(--muted);font-size:12px;margin-top:2px;}
.stat.warn .snum{color:var(--warn);} .stat.alert .snum{color:var(--alert);}
.cardhead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px;}
.cardhead h2{font-size:14px;margin:0;font-weight:600;}
.cardhead .note{color:var(--muted);font-size:12px;}
.heatwrap{overflow-x:auto;}
.heatmap{display:block;}
.heatmap text.mlabel,.heatmap text.wlabel,.heatmap text.legend{fill:var(--muted);font-size:10px;}
.machines{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;}
.mcard{border:1px solid var(--border);border-radius:8px;padding:12px 14px;background:#0d1117;}
.mcard.off{opacity:.75;}
.mrow{display:flex;align-items:center;gap:8px;}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;}
.dot.on{background:var(--accent);box-shadow:0 0 6px var(--accent);} .dot.off{background:var(--muted);}
.mname{font-weight:600;} .mstatus{color:var(--muted);font-size:12px;margin-left:auto;}
.msub{color:var(--fg);font-size:12px;margin-top:6px;} .mseen{color:var(--muted);font-size:11px;margin-top:2px;}
.merr{color:var(--warn);font-size:11px;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
table.repos{width:100%;border-collapse:collapse;}
table.repos th{text-align:left;color:var(--muted);font-size:11px;font-weight:600;
 text-transform:uppercase;letter-spacing:.04em;padding:6px 10px;border-bottom:1px solid var(--border);}
table.repos td{padding:9px 10px;border-bottom:1px solid #21262d;vertical-align:middle;word-break:break-word;}
table.repos tr:last-child td{border-bottom:0;}
.rname{font-weight:600;} .rmachine{color:var(--muted);} .rbranch{color:var(--muted);font-family:ui-monospace,monospace;font-size:12px;}
.rtime{color:var(--muted);white-space:nowrap;}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600;margin-right:5px;}
.badge.dirty{background:rgba(210,153,34,.15);color:var(--warn);}
.badge.unpushed{background:rgba(248,81,73,.15);color:var(--alert);}
.badge.behind{background:rgba(88,166,255,.15);color:#58a6ff;}
.badge.clean{background:rgba(63,185,80,.12);color:var(--accent);}
.badge.bare,.badge.noremote{background:#21262d;color:var(--muted);}
.more{color:var(--accent);font-size:12px;margin-top:10px;cursor:pointer;display:inline-block;user-select:none;}
.more:hover{text-decoration:underline;}
</style></head><body>
<div class="wrap">
<header>
  <div><h1><span class="g">git</span> monitor</h1>
  <div class="sub">uncommitted &amp; unpushed work across your machines</div></div>
  <div class="headright">
    <span class="sub">last scan {{scan_at}}</span>
    <a class="cfglink" href="/config" title="Edit monitored machines"><i></i>Config</a>
    <button id="refresh" onclick="refresh()">Refresh</button>
  </div>
</header>

<div class="stats">{{stats}}</div>

<div class="card">
  <div class="cardhead"><h2>Commit activity</h2>
  <span class="note">{{year_total}} commits in the last year</span></div>
  <div class="heatwrap">{{heatmap}}</div>
</div>

<div class="card">
  <div class="cardhead"><h2>Machines</h2></div>
  {{machines}}
</div>

<div class="card">
  <div class="cardhead"><h2>Recent projects</h2></div>
  {{repos}}
</div>
</div>
<script>
function refresh(){
  var b=document.getElementById('refresh');
  b.disabled=true; b.textContent='Scanning…';
  fetch('/api/refresh',{method:'POST'}).then(function(){location.reload();})
   .catch(function(){b.disabled=false;b.textContent='Refresh';});
}
(function(){
  var MIN=5, MAX=10;
  var table=document.querySelector('table.repos');
  if(!table) return;
  var rows=Array.prototype.slice.call(table.querySelectorAll('tbody tr'));
  var moreEl=document.getElementById('repos-more');
  var doc=document.documentElement;
  var expanded=false, fitN=rows.length;
  function showN(n){ rows.forEach(function(r,i){r.style.display = i<n ? '' : 'none';}); }
  // Largest row count (5-10) that doesn't make the page overflow.
  // Compare against clientHeight (true inner height, excludes scrollbar gutter)
  // so a few leftover pixels don't slip a scrollbar through.
  function computeFit(){
    var n=Math.min(MAX, rows.length);
    showN(n);
    var guard=0;
    while(n>MIN && doc.scrollHeight>doc.clientHeight && guard<50){ n--; showN(n); guard++; }
    return n;
  }
  function render(){
    if(!rows.length) return;
    if(expanded){
      showN(rows.length);
      if(moreEl){ moreEl.textContent='show fewer'; moreEl.style.display=(rows.length>fitN)?'':'none'; }
    } else {
      fitN=computeFit();
      if(moreEl){
        var hidden=rows.length-fitN;
        moreEl.textContent = hidden>0 ? ('+ '+hidden+' more repositories') : '';
        moreEl.style.display = hidden>0 ? '' : 'none';
      }
    }
  }
  if(moreEl) moreEl.addEventListener('click', function(){ expanded=!expanded; render(); });
  function reflow(){ if(!expanded) render(); }
  render();
  // Re-measure after the layout fully settles — the first pass can run a few
  // pixels short (before final paint), which is what leaves a scrollbar sliver.
  requestAnimationFrame(function(){ requestAnimationFrame(reflow); });
  window.addEventListener('load', reflow);
  window.addEventListener('resize', reflow);
})();
</script>
</body></html>"""


def render_config_page(raw_text, config):
    cfg_json = json.dumps(config).replace("</", "<\\/")
    out = CONFIG_PAGE
    for k, v in {"config_json": cfg_json, "raw": esc(raw_text)}.items():
        out = out.replace("{{%s}}" % k, v)
    return out


CONFIG_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Git Monitor · Config</title>
<style>
:root{ --bg:#0d1117; --card:#161b22; --border:#30363d; --fg:#e6edf3;
       --muted:#8b949e; --accent:#39d353; --warn:#d29922; --alert:#f85149; }
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:900px;margin:0 auto;padding:24px 20px 40px;}
header{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:6px;}
h1{font-size:20px;margin:0;font-weight:600;} h1 .g{color:var(--accent);}
a.back{color:var(--muted);text-decoration:none;font-size:13px;} a.back:hover{color:var(--fg);}
.hint{color:var(--muted);font-size:12px;margin:0 0 18px;}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin-bottom:16px;}
h2{font-size:14px;margin:0 0 12px;font-weight:600;}
label{display:block;color:var(--muted);font-size:12px;margin-bottom:2px;}
input,textarea,select{width:100%;background:#0d1117;color:var(--fg);border:1px solid var(--border);
 border-radius:6px;padding:6px 8px;font:13px ui-monospace,SFMono-Regular,Menlo,monospace;}
input:focus,textarea:focus{outline:none;border-color:var(--accent);}
textarea{resize:vertical;min-height:46px;}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;}
.mcard{border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:12px;background:#0d1117;}
.mhead{display:flex;gap:8px;align-items:center;margin-bottom:10px;}
.mhead .f-name{font-weight:600;max-width:200px;}
.field{margin-bottom:10px;}
.rootrow{display:flex;gap:8px;align-items:center;margin-bottom:6px;}
.rootrow .r-path{flex:1;} .rootrow .r-depth{width:70px;}
.cb{display:flex;align-items:center;gap:4px;color:var(--muted);font-size:12px;white-space:nowrap;}
.cb input{width:auto;}
button{background:#21262d;color:var(--fg);border:1px solid var(--border);border-radius:6px;
 padding:6px 12px;font-size:13px;cursor:pointer;} button:hover{border-color:var(--muted);}
button.primary{background:#238636;border-color:#238636;color:#fff;font-weight:600;} button.primary:hover{background:#2ea043;}
button.danger{color:var(--alert);border-color:transparent;padding:6px 8px;}
button.link{background:none;border:none;color:var(--accent);padding:2px 0;font-size:12px;}
.savebar{display:flex;align-items:center;gap:14px;margin:14px 0;}
.savestatus{font-size:13px;} .savestatus.ok{color:var(--accent);} .savestatus.err{color:var(--alert);}
.testresult{font-size:12px;margin-top:8px;min-height:16px;word-break:break-word;}
.testresult.ok{color:var(--accent);} .testresult.err{color:var(--alert);}
details{margin-top:8px;} summary{cursor:pointer;color:var(--muted);font-size:13px;}
#rawyaml{min-height:280px;margin-top:10px;white-space:pre;}
.mtitle{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;}
</style></head><body>
<div class="wrap">
<header>
  <h1><span class="g">git</span> monitor · config</h1>
  <a class="back" href="/">&larr; back to dashboard</a>
</header>
<p class="hint">Add or remove monitored machines. Saving regenerates the config and triggers a
background rescan. Changes affect which hosts are scanned over SSH (access is limited to your LAN/Tailscale).</p>

<div class="card">
  <h2>Global settings</h2>
  <div class="grid">
    <div><label>Scan interval (min)</label><input id="g_interval" type="number"></div>
    <div><label>Heatmap window (days)</label><input id="g_since" type="number"></div>
    <div><label>Default remote python</label><input id="g_rpy"></div>
    <div><label>Connect timeout (s)</label><input id="g_ctimeout" type="number"></div>
    <div><label>Scan timeout (s)</label><input id="g_timeout" type="number"></div>
  </div>
</div>

<div class="mtitle"><h2 style="margin:0">Machines</h2><button id="addmachine">+ Add machine</button></div>
<div id="machines"></div>

<div class="savebar">
  <button class="primary" id="save">Save changes</button>
  <span class="savestatus" id="savestatus"></span>
</div>

<div class="card">
  <details>
    <summary>Advanced: edit raw YAML</summary>
    <p class="hint">Full control (global ssh_identity/ssh_options, comments, etc.). Saving here writes exactly
    what you type. This and the form above are two views of the same file — save one, then reload to sync the other.</p>
    <textarea id="rawyaml" spellcheck="false">{{raw}}</textarea>
    <div class="savebar"><button id="saveraw">Save raw YAML</button><span class="savestatus" id="rawstatus"></span></div>
  </details>
</div>
</div>

<script>
var CONFIG = {{config_json}};
var GLOBAL_KEYS = ['scan_interval_minutes','since_days','remote_python','connect_timeout','timeout','top_n'];

function el(h){var d=document.createElement('div');d.innerHTML=h.trim();return d.firstChild;}
function v(node,sel){var e=node.querySelector(sel);return e?e.value.trim():'';}
function lines(s){return s.split(/[\\n,]/).map(function(x){return x.trim();}).filter(Boolean);}
function gi(id,d){var n=parseInt((document.getElementById(id).value||'').trim(),10);return isNaN(n)?d:n;}
function gs(id,d){var s=(document.getElementById(id).value||'').trim();return s||d;}

var ROOT_HTML =
  '<div class="rootrow">'+
  '<input class="r-path" placeholder="/path or C:/projects">'+
  '<input class="r-depth" type="number" title="depth to search">'+
  '<label class="cb"><input type="checkbox" class="r-bare">bare</label>'+
  '<button class="danger" data-action="remove-root" title="remove root">&times;</button>'+
  '</div>';

var CARD_HTML =
  '<div class="mcard" data-machine>'+
  '<div class="mhead"><input class="f-name" placeholder="name">'+
  '<button data-action="test">Test</button>'+
  '<button class="danger" data-action="remove-machine" title="remove machine">&times;</button></div>'+
  '<div class="grid field">'+
  '<div><label>SSH (user@host or "local")</label><input class="f-ssh" placeholder="user@192.168.1.50"></div>'+
  '<div><label>Remote python (optional)</label><input class="f-rpy" placeholder="python3"></div>'+
  '<div><label>Timeout s (optional)</label><input class="f-timeout" type="number" placeholder="default"></div>'+
  '</div>'+
  '<div class="field"><label>Scan roots</label><div data-roots></div>'+
  '<button class="link" data-action="add-root">+ add root</button></div>'+
  '<div class="grid field">'+
  '<div><label>Extra repo paths (one per line)</label><textarea class="f-extra"></textarea></div>'+
  '<div><label>Exclude paths (one per line)</label><textarea class="f-exclude"></textarea></div>'+
  '</div>'+
  '<div class="testresult"></div></div>';

function makeRoot(r){
  var row=el(ROOT_HTML);
  row.querySelector('.r-path').value=r.path||'';
  row.querySelector('.r-depth').value=(r.depth!=null?r.depth:2);
  row.querySelector('.r-bare').checked=!!r.bare;
  return row;
}
function makeCard(t){
  t=t||{};
  var c=el(CARD_HTML);
  c.querySelector('.f-name').value=t.name||'';
  c.querySelector('.f-ssh').value=t.ssh||'';
  c.querySelector('.f-rpy').value=t.remote_python||'';
  c.querySelector('.f-timeout').value=(t.timeout!=null?t.timeout:'');
  var rc=c.querySelector('[data-roots]');
  (t.roots||[]).forEach(function(r){rc.appendChild(makeRoot(r));});
  c.querySelector('.f-extra').value=(t.extra||[]).join('\\n');
  c.querySelector('.f-exclude').value=(t.exclude||[]).join('\\n');
  return c;
}
function collectTarget(c){
  var t={name:v(c,'.f-name'),ssh:v(c,'.f-ssh')};
  var rpy=v(c,'.f-rpy');if(rpy)t.remote_python=rpy;
  var to=v(c,'.f-timeout');if(to)t.timeout=parseInt(to,10);
  var roots=[];
  c.querySelectorAll('.rootrow').forEach(function(rr){
    var p=v(rr,'.r-path');if(!p)return;
    var root={path:p,depth:parseInt(v(rr,'.r-depth')||'2',10)};
    if(rr.querySelector('.r-bare').checked)root.bare=true;
    roots.push(root);
  });
  if(roots.length)t.roots=roots;
  var ex=lines(v(c,'.f-extra'));if(ex.length)t.extra=ex;
  var exc=lines(v(c,'.f-exclude'));if(exc.length)t.exclude=exc;
  return t;
}
function collectConfig(){
  var cfg={};
  for(var k in CONFIG){if(CONFIG.hasOwnProperty(k)&&k!=='targets'&&GLOBAL_KEYS.indexOf(k)<0)cfg[k]=CONFIG[k];}
  cfg.scan_interval_minutes=gi('g_interval',30);
  cfg.since_days=gi('g_since',365);
  cfg.remote_python=gs('g_rpy','python3');
  cfg.connect_timeout=gi('g_ctimeout',8);
  cfg.timeout=gi('g_timeout',120);
  if(CONFIG.top_n!=null)cfg.top_n=CONFIG.top_n;
  var targets=[];
  document.querySelectorAll('[data-machine]').forEach(function(c){targets.push(collectTarget(c));});
  cfg.targets=targets;
  return cfg;
}
function post(body,onok,st){
  st.textContent='Saving…';st.className='savestatus';
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
   .then(function(r){return r.json();})
   .then(function(j){
     if(j.ok){st.textContent='Saved. Rescanning in background — reloading…';st.className='savestatus ok';
       setTimeout(function(){location.reload();},1400);}
     else{st.textContent='Error: '+j.error;st.className='savestatus err';}
   }).catch(function(e){st.textContent='Error: '+e;st.className='savestatus err';});
}
function testCard(c){
  var out=c.querySelector('.testresult');
  out.textContent='Testing… (slow hosts can take a minute)';out.className='testresult';
  fetch('/api/config/test',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({target:collectTarget(c)})})
   .then(function(r){return r.json();})
   .then(function(j){
     if(j.ok){out.className='testresult ok';
       out.textContent='\\u2713 reachable \\u2014 '+j.count+' repo'+(j.count===1?'':'s')+
         (j.repos&&j.repos.length?': '+j.repos.join(', '):'');}
     else{out.className='testresult err';out.textContent='\\u2717 '+j.error;}
   }).catch(function(e){out.className='testresult err';out.textContent='\\u2717 '+e;});
}

var machines=document.getElementById('machines');
machines.addEventListener('click',function(e){
  var b=e.target.closest('[data-action]');if(!b)return;
  var a=b.getAttribute('data-action');
  if(a==='remove-machine')b.closest('[data-machine]').remove();
  else if(a==='test')testCard(b.closest('[data-machine]'));
  else if(a==='add-root')b.closest('[data-machine]').querySelector('[data-roots]').appendChild(makeRoot({}));
  else if(a==='remove-root')b.closest('.rootrow').remove();
});
document.getElementById('addmachine').onclick=function(){machines.appendChild(makeCard({}));};
document.getElementById('save').onclick=function(){post({config:collectConfig()},null,document.getElementById('savestatus'));};
document.getElementById('saveraw').onclick=function(){
  post({raw:document.getElementById('rawyaml').value},null,document.getElementById('rawstatus'));};

// init
(function(){
  document.getElementById('g_interval').value=CONFIG.scan_interval_minutes!=null?CONFIG.scan_interval_minutes:30;
  document.getElementById('g_since').value=CONFIG.since_days!=null?CONFIG.since_days:365;
  document.getElementById('g_rpy').value=CONFIG.remote_python||'python3';
  document.getElementById('g_ctimeout').value=CONFIG.connect_timeout!=null?CONFIG.connect_timeout:8;
  document.getElementById('g_timeout').value=CONFIG.timeout!=null?CONFIG.timeout:120;
  (CONFIG.targets||[]).forEach(function(t){machines.appendChild(makeCard(t));});
})();
</script>
</body></html>"""
