#!/usr/bin/env python3
"""
AgentOS Dashboard — dark-mode web UI
Run: python dashboard.py  →  opens http://localhost:5001
"""

import io
import json
import re
import sys
import webbrowser
from contextlib import redirect_stdout
from datetime import date
from html import escape as esc
from pathlib import Path
from threading import Timer

sys.path.insert(0, str(Path(__file__).resolve().parent))

import state_layer as sl
import loop as loop_mod

from flask import Flask, jsonify, request

app  = Flask(__name__)
ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
PORT  = 5002


# ═══════════════════════════════════════════════════════════════════ helpers ══

def _load():
    try:
        return sl.load_state()
    except SystemExit:
        return {}

def _dl_cls(days):
    if days is None: return "d-unknown"
    if days < 0:     return "d-past"
    if days <= 3:    return "d-red"
    if days <= 7:    return "d-yellow"
    return "d-green"

def _dl_label(days):
    if days is None: return "?"
    if days < 0:     return f"{-days}d overdue"
    if days == 0:    return "TODAY"
    return f"{days}d"

def _cap_badge(color):
    c = (color or "unknown").lower()
    cls = c if c in ("green","yellow","red") else "gray"
    return f'<span class="badge badge-{cls}">{esc(c.upper())}</span>'

def _bool_badge(val, yes_label, no_label):
    if val:
        return f'<span class="badge badge-green">{yes_label}</span>'
    return f'<span class="badge badge-gray">{no_label}</span>'

def _parse_card():
    p = STATE / "trigger_card.md"
    if not p.exists():
        return None
    t = p.read_text(encoding="utf-8")
    move   = re.search(r"THE MOVE:\s+(.+)",                         t)
    why    = re.search(r"WHY NOW:\s+(.+)",                          t)
    action = re.search(r"THE ACTION:\n(.*?)(?:\n\nTIME:|\nTIME:)", t, re.DOTALL)
    time_m = re.search(r"TIME:\s+(.+)",                             t)
    date_m = re.search(r"THE ONE MOVE\s+-\s+(\S+)",                 t)
    return {
        "move":    move.group(1).strip()   if move    else "",
        "why_now": why.group(1).strip()    if why     else "",
        "action":  action.group(1).strip() if action  else "",
        "time":    time_m.group(1).strip() if time_m  else "",
        "date":    date_m.group(1)         if date_m  else str(date.today()),
    }

def _search_corpus(q, top_k=5):
    terms = set(sl.tokenize(q))
    if not terms:
        return []
    manifest = sl.load_manifest()
    results  = []
    for entry in manifest.get("files", []):
        p = sl.CORPUS_DIR / entry["path"]
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        s = sl.score_text(terms, text, entry["title"], entry["headings"])
        if s > 0:
            s *= sl._importance_weight(entry["path"])
            results.append({
                "score":    round(s, 2),
                "title":    entry["title"],
                "path":     entry["path"],
                "excerpts": sl.best_excerpts(terms, text)[:2],
            })
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]

def _read_log(filename):
    p = STATE / filename
    if not p.exists():
        return f"({filename} not yet created — will appear after first run)"
    text = p.read_text(encoding="utf-8", errors="ignore")
    return text[-8000:] if len(text) > 8000 else text


# ══════════════════════════════════════════════════════════════════════ CSS ══

CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:14px}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:#111113;color:#e4e4e7;min-height:100vh;display:flex
}

/* ── sidebar ── */
.sidebar{
  width:210px;min-height:100vh;background:#0d0d0f;
  border-right:1px solid #1f1f23;display:flex;flex-direction:column;
  position:fixed;top:0;left:0;bottom:0;z-index:10
}
.sidebar-logo{padding:20px 16px 14px;border-bottom:1px solid #1f1f23}
.sidebar-logo h1{font-size:13px;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;color:#6366f1}
.sidebar-logo p{font-size:11px;color:#3f3f46;margin-top:2px}
.nav{padding:10px 8px;flex:1}
.nav a{
  display:flex;align-items:center;gap:9px;padding:7px 10px;
  border-radius:6px;font-size:13px;color:#71717a;text-decoration:none;
  transition:background .12s,color .12s;margin-bottom:2px
}
.nav a:hover{background:#1c1c1f;color:#e4e4e7}
.nav a.active{background:#1c1c1f;color:#f4f4f5}
.nav .icon{font-size:13px;opacity:.75;width:16px;text-align:center}
.sidebar-foot{padding:12px 14px;border-top:1px solid #1f1f23;
  font-size:11px;color:#3f3f46}

/* ── main ── */
.main{margin-left:210px;flex:1;padding:32px 40px;max-width:960px}
.page-title{font-size:21px;font-weight:600;color:#f4f4f5;margin-bottom:4px}
.page-sub{font-size:13px;color:#52525b;margin-bottom:26px}
h2{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;
  color:#52525b;margin-bottom:10px}
.section{margin-bottom:30px}

/* ── north star ── */
.north-star{
  padding:22px 26px;
  background:linear-gradient(135deg,#0f1322 0%,#0d0d0f 100%);
  border:1px solid #1e1b4b;border-radius:10px;margin-bottom:22px
}
.ns-label{font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.12em;color:#6366f1;margin-bottom:8px}
.ns-text{font-size:15px;color:#f4f4f5;line-height:1.55}
.ns-obj{margin-top:10px;font-size:13px;color:#818cf8;line-height:1.4}
.ns-matters{margin-top:12px;display:flex;flex-wrap:wrap;gap:6px}
.ns-pill{font-size:11px;background:#1e1b4b;color:#818cf8;
  padding:3px 10px;border-radius:12px}

/* ── capacity ── */
.capacity-row{display:flex;align-items:center;gap:12px;margin-bottom:26px}
.cap-label{font-size:12px;color:#52525b}

/* ── badges ── */
.badge{
  display:inline-flex;align-items:center;padding:3px 10px;
  border-radius:20px;font-size:11px;font-weight:600;letter-spacing:.04em
}
.badge-green{background:#14532d;color:#4ade80}
.badge-yellow{background:#422006;color:#fbbf24}
.badge-red{background:#450a0a;color:#f87171}
.badge-blue{background:#1e1b4b;color:#818cf8}
.badge-gray{background:#27272a;color:#a1a1aa}

/* ── deadlines ── */
.dl-list{display:flex;flex-direction:column;gap:7px}
.dl-item{
  display:flex;align-items:center;gap:12px;
  padding:10px 14px;background:#18181b;
  border:1px solid #27272a;border-radius:6px
}
.dl-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.d-green .dl-dot{background:#22c55e}
.d-yellow .dl-dot{background:#eab308}
.d-red .dl-dot{background:#ef4444}
.d-past .dl-dot,.d-unknown .dl-dot{background:#3f3f46}
.dl-what{flex:1;font-size:13px;color:#e4e4e7}
.dl-date{font-size:11px;color:#52525b}
.dl-pill{font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px}
.d-green .dl-pill{background:#14532d;color:#4ade80}
.d-yellow .dl-pill{background:#422006;color:#fbbf24}
.d-red .dl-pill{background:#450a0a;color:#f87171}
.d-past .dl-pill,.d-unknown .dl-pill{background:#27272a;color:#71717a}

/* ── shipped ── */
.shipped-item{
  display:flex;align-items:flex-start;gap:12px;
  padding:12px 14px;background:#18181b;
  border:1px solid #27272a;border-radius:6px;margin-bottom:8px
}
.check-icon{color:#22c55e;font-size:15px;flex-shrink:0;margin-top:1px}
.shipped-what{font-size:13px;color:#f4f4f5;margin-bottom:3px}
.shipped-url a{font-size:12px;color:#6366f1;text-decoration:none}
.shipped-url a:hover{text-decoration:underline}
.shipped-meta{font-size:11px;color:#52525b;margin-top:4px}
.seen-ok{color:#22c55e}.seen-no{color:#eab308}

/* ── cards (pending) ── */
.card{
  background:#18181b;border:1px solid #27272a;
  border-radius:8px;padding:14px 18px;margin-bottom:9px
}
.card-index{font-size:10px;color:#52525b;margin-bottom:4px}
.card-title{font-size:14px;font-weight:500;color:#f4f4f5;margin-bottom:3px}
.card-age{font-size:11px;color:#52525b}
.card-blocked{
  margin-top:9px;font-size:12px;color:#a1a1aa;
  padding:4px 10px;border-left:2px solid #eab308
}
.card-blocked strong{color:#fbbf24}
.card-trigger{
  margin-top:9px;padding:10px 12px;
  background:#111113;border:1px solid #1f1f23;border-radius:5px;
  font-size:12px;color:#a1a1aa;line-height:1.55
}

/* ── owed ── */
.owed-item{
  display:flex;align-items:center;gap:12px;
  padding:11px 14px;background:#18181b;
  border:1px solid #27272a;border-radius:6px;margin-bottom:7px
}
.owed-who{font-size:13px;font-weight:500;color:#f4f4f5;min-width:200px}
.owed-what{font-size:12px;color:#71717a;flex:1;line-height:1.4}
.owed-age{font-size:12px;font-weight:600;color:#eab308;white-space:nowrap}

/* ── outcome ── */
.outcome-box{
  padding:14px 16px;background:#18181b;
  border:1px solid #27272a;border-radius:6px
}
.outcome-trigger{font-size:13px;color:#f4f4f5;margin-bottom:10px}
.outcome-flags{display:flex;gap:8px;align-items:center}
.outcome-date{font-size:11px;color:#52525b;margin-left:8px}

/* ── buttons ── */
.btn-run{
  display:inline-flex;align-items:center;gap:8px;
  padding:9px 20px;background:#6366f1;color:#fff;
  font-size:13px;font-weight:600;border:none;border-radius:7px;
  cursor:pointer;transition:background .15s,transform .1s;letter-spacing:.02em
}
.btn-run:hover{background:#4f46e5}
.btn-run:active{transform:scale(.98)}
.btn-run:disabled{background:#27272a;color:#52525b;cursor:not-allowed}
.btn-done{
  display:inline-flex;align-items:center;gap:8px;
  padding:9px 20px;background:#14532d;color:#4ade80;
  font-size:13px;font-weight:600;border:1px solid #166534;
  border-radius:7px;cursor:pointer;transition:background .15s
}
.btn-done:hover{background:#166534}
.btn-sm{
  padding:5px 12px;font-size:12px;background:#1c1c1f;
  color:#a1a1aa;border:1px solid #27272a;border-radius:5px;
  cursor:pointer;transition:all .15s
}
.btn-sm:hover{border-color:#6366f1;color:#818cf8}

/* ── modal ── */
.modal-overlay{
  position:fixed;inset:0;background:rgba(0,0,0,.72);
  z-index:50;display:none;align-items:center;justify-content:center
}
.modal-overlay.open{display:flex}
.modal{
  background:#18181b;border:1px solid #27272a;border-radius:10px;
  width:700px;max-height:78vh;overflow:hidden;
  display:flex;flex-direction:column
}
.modal-hd{
  padding:14px 18px;border-bottom:1px solid #27272a;
  display:flex;justify-content:space-between;align-items:center
}
.modal-hd h3{font-size:13px;font-weight:600;color:#f4f4f5}
.modal-x{
  background:none;border:none;color:#71717a;
  cursor:pointer;font-size:17px;padding:3px;line-height:1
}
.modal-x:hover{color:#f4f4f5}
.modal-bd{flex:1;overflow-y:auto;padding:18px}
.loop-out{
  font-family:'Fira Code','JetBrains Mono','Courier New',monospace;
  font-size:11.5px;line-height:1.65;color:#a1a1aa;
  white-space:pre-wrap;word-break:break-word
}
.modal-ft{
  padding:12px 18px;border-top:1px solid #27272a;
  display:flex;justify-content:flex-end;gap:8px
}

/* ── trigger card page ── */
.tc-wrap{max-width:680px}
.tc-card{
  background:#18181b;border:1px solid #27272a;
  border-radius:10px;overflow:hidden
}
.tc-head{padding:24px 26px 20px;border-bottom:1px solid #27272a}
.tc-datelabel{
  font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.12em;color:#52525b;margin-bottom:8px
}
.tc-move{font-size:19px;font-weight:600;color:#f4f4f5;line-height:1.35}
.tc-body{padding:22px 26px}
.tc-sec{margin-bottom:22px}
.tc-lbl{
  font-size:10px;font-weight:700;text-transform:uppercase;
  letter-spacing:.12em;color:#52525b;margin-bottom:7px
}
.tc-val{font-size:13px;color:#e4e4e7;line-height:1.55}
.tc-action-wrap{
  background:#111113;border:1px solid #1f1f23;border-radius:6px;overflow:hidden
}
.tc-action-bar{
  display:flex;justify-content:space-between;align-items:center;
  padding:7px 12px;border-bottom:1px solid #1f1f23
}
.tc-lang{font-size:10px;color:#52525b}
.tc-foot{
  padding:14px 26px 18px;border-top:1px solid #27272a;
  display:flex;align-items:center;justify-content:space-between
}
pre.tc-code{
  font-family:'Fira Code','JetBrains Mono',monospace;
  font-size:12px;line-height:1.7;color:#a1a1aa;
  padding:14px 16px;white-space:pre;overflow-x:auto;margin:0
}

/* ── corpus ── */
.search-wrap{position:relative;margin-bottom:22px;max-width:540px}
.search-icon{
  position:absolute;left:13px;top:50%;transform:translateY(-50%);
  color:#52525b;font-size:14px;pointer-events:none
}
.search-input{
  width:100%;padding:10px 14px 10px 38px;
  background:#18181b;border:1px solid #27272a;border-radius:8px;
  font-size:13px;color:#f4f4f5;outline:none;
  transition:border-color .15s
}
.search-input:focus{border-color:#6366f1}
.result-item{
  padding:14px 18px;background:#18181b;
  border:1px solid #27272a;border-radius:8px;margin-bottom:9px
}
.result-title{font-size:13px;font-weight:500;color:#f4f4f5;margin-bottom:2px}
.result-meta{font-size:11px;color:#52525b;margin-bottom:9px}
.result-excerpt{
  font-size:12px;color:#a1a1aa;line-height:1.6;
  padding:9px 12px;background:#111113;
  border-left:2px solid #27272a;border-radius:0 4px 4px 0;
  margin-bottom:6px;white-space:pre-wrap;word-break:break-word
}
.search-hint{font-size:13px;color:#52525b;text-align:center;padding:40px 0}

/* ── logs ── */
.logs-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.log-panel{
  background:#18181b;border:1px solid #27272a;
  border-radius:8px;overflow:hidden
}
.log-hd{
  padding:9px 14px;border-bottom:1px solid #27272a;
  display:flex;justify-content:space-between;align-items:center
}
.log-name{font-size:12px;font-weight:600;color:#f4f4f5}
.log-status{font-size:11px;color:#52525b}
.log-body{
  font-family:'Fira Code','JetBrains Mono',monospace;
  font-size:11px;line-height:1.65;color:#71717a;
  padding:14px;white-space:pre-wrap;word-break:break-word;
  max-height:580px;overflow-y:auto
}

/* ── toast ── */
.toast{
  position:fixed;bottom:22px;right:22px;
  background:#18181b;border:1px solid #27272a;color:#f4f4f5;
  padding:11px 18px;border-radius:8px;font-size:13px;
  box-shadow:0 8px 32px rgba(0,0,0,.5);opacity:0;
  transition:opacity .25s;z-index:99;pointer-events:none
}
.toast.show{opacity:1}

/* ── misc ── */
.divider{border:none;border-top:1px solid #1f1f23;margin:24px 0}
.empty{color:#3f3f46;font-size:13px;padding:14px 0}
.refresh-note{
  font-size:11px;color:#3f3f46;display:flex;align-items:center;gap:6px
}
.pulse{
  display:inline-block;width:6px;height:6px;
  background:#22c55e;border-radius:50%;
  animation:pulse 2s infinite
}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.spinner{
  display:inline-block;width:13px;height:13px;
  border:2px solid #27272a;border-top-color:#6366f1;
  border-radius:50%;animation:spin .65s linear infinite;vertical-align:middle
}
@keyframes spin{to{transform:rotate(360deg)}}
"""

# ═══════════════════════════════════════════════════════════════ JavaScript ══

JS_COMMON = """
function showToast(msg, isErr) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.style.borderColor = isErr ? '#ef4444' : '#22c55e';
  t.classList.add('show');
  setTimeout(function(){ t.classList.remove('show'); }, 3200);
}
"""

JS_BOARD = """
var runBtn = document.getElementById('run-btn');
var modal  = document.getElementById('loop-modal');
var loopOut = document.getElementById('loop-out');

runBtn.addEventListener('click', function() {
  runBtn.disabled = true;
  runBtn.innerHTML = '<span class="spinner"></span> Running...';
  modal.classList.add('open');
  loopOut.textContent = 'Running loop cycle…';

  fetch('/api/run_loop', {method:'POST'})
    .then(function(r){ return r.json(); })
    .then(function(d){
      loopOut.textContent = d.output || d.error || '(no output)';
      runBtn.disabled = false;
      runBtn.innerHTML = '&#9654;&nbsp; Run Loop';
    })
    .catch(function(e){
      loopOut.textContent = 'Error: ' + e.message;
      runBtn.disabled = false;
      runBtn.innerHTML = '&#9654;&nbsp; Run Loop';
    });
});

document.getElementById('modal-close').addEventListener('click', function(){
  modal.classList.remove('open');
});
document.getElementById('modal-refresh').addEventListener('click', function(){
  window.location.reload();
});

// auto-refresh every 60 s
setTimeout(function(){ window.location.reload(); }, 60000);
"""

JS_TRIGGER = """
var doneBtn = document.getElementById('done-btn');
if (doneBtn) {
  doneBtn.addEventListener('click', function() {
    var move = this.getAttribute('data-move');
    this.disabled = true;
    this.textContent = 'Recording…';
    fetch('/api/mark_done', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({trigger: move})
    })
    .then(function(r){ return r.json(); })
    .then(function(d){
      showToast(d.ok ? 'Outcome recorded.' : ('Error: ' + d.error), !d.ok);
      if (d.ok) setTimeout(function(){ window.location.reload(); }, 1200);
      else { doneBtn.disabled=false; doneBtn.textContent='Mark as Done'; }
    });
  });
}

var copyBtn = document.getElementById('copy-btn');
if (copyBtn) {
  copyBtn.addEventListener('click', function(){
    var code = document.getElementById('action-code').textContent;
    navigator.clipboard.writeText(code).then(function(){
      copyBtn.textContent = 'Copied!';
      setTimeout(function(){ copyBtn.textContent = 'Copy'; }, 1800);
    });
  });
}
"""

JS_CORPUS = """
function escH(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
var timer;
document.getElementById('q').addEventListener('input', function(){
  clearTimeout(timer);
  var q = this.value.trim();
  var res = document.getElementById('results');
  if (q.length < 3) {
    res.innerHTML = '<p class="search-hint">Type at least 3 characters to search…</p>';
    return;
  }
  res.innerHTML = '<p class="search-hint"><span class="spinner"></span> Searching…</p>';
  timer = setTimeout(function(){
    fetch('/api/search?q=' + encodeURIComponent(q))
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (!d.results || d.results.length === 0) {
          res.innerHTML = '<p class="search-hint">No matches found in corpus.</p>';
          return;
        }
        var html = '';
        d.results.forEach(function(r){
          html += '<div class="result-item">';
          html += '<div class="result-title">' + escH(r.title) + '</div>';
          html += '<div class="result-meta">' + escH(r.path) + ' &middot; score ' + r.score + '</div>';
          r.excerpts.forEach(function(ex){
            html += '<div class="result-excerpt">' + escH(ex.slice(0,600)) + '</div>';
          });
          html += '</div>';
        });
        res.innerHTML = html;
      })
      .catch(function(e){
        res.innerHTML = '<p class="search-hint">Error: ' + e.message + '</p>';
      });
  }, 360);
});
"""

JS_LOGS = """
// auto-refresh every 30 s
setTimeout(function(){ window.location.reload(); }, 30000);
"""


# ══════════════════════════════════════════════════════════════════ layout ══

def _nav(active):
    items = [
        ("/",        "▣", "Board",        "board"),
        ("/trigger", "▷", "Trigger Card", "trigger"),
        ("/corpus",  "◈", "Corpus",       "corpus"),
        ("/logs",    "≡", "Logs",         "logs"),
    ]
    rows = ""
    for href, icon, label, key in items:
        cls = " active" if key == active else ""
        rows += (f'<a href="{href}" class="{cls}">'
                 f'<span class="icon">{icon}</span>{esc(label)}</a>\n')
    return rows


def _layout(title, content, active, js=""):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} — AgentOS</title>
<style>{CSS}</style>
</head>
<body>
<nav class="sidebar">
  <div class="sidebar-logo">
    <h1>AgentOS</h1>
    <p>Personal Operating System</p>
  </div>
  <div class="nav">{_nav(active)}</div>
  <div class="sidebar-foot">{date.today()}</div>
</nav>
<main class="main">
{content}
</main>
<div class="toast" id="toast"></div>
<script>{JS_COMMON}{js}</script>
</body>
</html>"""


# ════════════════════════════════════════════════════════════════ page: board ══

def _board_html():
    s          = _load()
    goal       = s.get("goal", {})
    capacity   = s.get("state_color", "unknown")
    deadlines  = sorted(s.get("deadlines", []), key=lambda x: x.get("date","9999"))
    shipped    = s.get("shipped", [])
    pending    = s.get("pending", [])
    owed       = s.get("owed", [])
    outcome    = s.get("last_outcome", {})
    updated    = s.get("last_updated", "")[:10]

    # ── north star ──
    matters = "".join(
        f'<span class="ns-pill">{esc(m)}</span>'
        for m in goal.get("what_matters", [])
    )
    ns = f"""
<div class="north-star">
  <div class="ns-label">North Star</div>
  <div class="ns-text">{esc(goal.get('north_star','(unset)'))}</div>
  <div class="ns-obj">&#8627; {esc(goal.get('active_objective',''))}</div>
  {f'<div class="ns-matters">{matters}</div>' if matters else ''}
</div>"""

    # ── capacity ──
    cap = f"""
<div class="capacity-row section">
  <span class="cap-label">Capacity</span>
  {_cap_badge(capacity)}
  <span class="cap-label" style="margin-left:auto;font-size:11px">
    updated {esc(updated)}
  </span>
</div>"""

    # ── deadlines ──
    if deadlines:
        rows = ""
        for d in deadlines:
            days = sl.days_until(d.get("date"))
            cls  = _dl_cls(days)
            lbl  = _dl_label(days)
            rows += f"""
<div class="dl-item {cls}">
  <div class="dl-dot"></div>
  <div class="dl-what">{esc(d.get('what','?'))}</div>
  <div class="dl-date">{esc(d.get('date',''))}</div>
  <div class="dl-pill">{esc(lbl)}</div>
</div>"""
        dl_section = f'<div class="section"><h2>Deadlines</h2><div class="dl-list">{rows}</div></div>'
    else:
        dl_section = '<div class="section"><h2>Deadlines</h2><p class="empty">No deadlines set.</p></div>'

    # ── shipped ──
    if shipped:
        rows = ""
        for item in shipped:
            seen    = item.get("seen_by") or []
            seen_tag = (f'<span class="seen-ok">seen by {esc(", ".join(seen))}</span>'
                        if seen else '<span class="seen-no">not yet seen</span>')
            url_html = (f'<div class="shipped-url"><a href="{esc(item["url"])}" target="_blank">'
                        f'{esc(item["url"])}</a></div>' if item.get("url") else "")
            rows += f"""
<div class="shipped-item">
  <div class="check-icon">&#10003;</div>
  <div>
    <div class="shipped-what">{esc(item.get('what','?'))}</div>
    {url_html}
    <div class="shipped-meta">{esc(item.get('date',''))} &middot; {seen_tag}</div>
  </div>
</div>"""
        ship_section = f'<div class="section"><h2>Shipped ({len(shipped)})</h2>{rows}</div>'
    else:
        ship_section = '<div class="section"><h2>Shipped</h2><p class="empty">Nothing shipped yet.</p></div>'

    # ── pending ──
    if pending:
        rows = ""
        for i, item in enumerate(pending):
            age     = sl.age_days(item.get("created"))
            age_str = f"{age}d old" if age is not None else ""
            blocked = (f'<div class="card-blocked"><strong>Blocked:</strong> '
                       f'{esc(item["blocked_on"])}</div>' if item.get("blocked_on") else "")
            trigger = (f'<div class="card-trigger">{esc(item["trigger"])}</div>'
                       if item.get("trigger") else "")
            rows += f"""
<div class="card">
  <div class="card-index">#{i}</div>
  <div class="card-title">{esc(item.get('what','?'))}</div>
  <div class="card-age">{esc(age_str)}</div>
  {blocked}{trigger}
</div>"""
        pend_section = f'<div class="section"><h2>Pending ({len(pending)})</h2>{rows}</div>'
    else:
        pend_section = '<div class="section"><h2>Pending</h2><p class="empty">No pending items.</p></div>'

    # ── owed ──
    if owed:
        rows = ""
        for item in owed:
            age     = sl.age_days(item.get("since"))
            age_str = f"{age}d ago" if age is not None else ""
            rows += f"""
<div class="owed-item">
  <div class="owed-who">{esc(item.get('who','?'))}</div>
  <div class="owed-what">{esc(item.get('what',''))}</div>
  <div class="owed-age">{esc(age_str)}</div>
</div>"""
        owed_section = f'<div class="section"><h2>Owed ({len(owed)})</h2>{rows}</div>'
    else:
        owed_section = ""

    # ── last outcome ──
    if outcome:
        pulled = _bool_badge(outcome.get("did_you_pull_it"), "Pulled", "Not pulled")
        moved  = _bool_badge(outcome.get("ground_moved"), "Ground moved", "No movement")
        notes  = (f'<span style="font-size:12px;color:#71717a;margin-left:8px">'
                  f'{esc(outcome["notes"])}</span>' if outcome.get("notes") else "")
        out_section = f"""
<div class="section">
  <h2>Last Outcome</h2>
  <div class="outcome-box">
    <div class="outcome-trigger">{esc(outcome.get('trigger_issued','(none)'))}</div>
    <div class="outcome-flags">
      {pulled}{moved}{notes}
      <span class="outcome-date">{esc(outcome.get('date',''))}</span>
    </div>
  </div>
</div>"""
    else:
        out_section = ""

    # ── run loop button + modal ──
    run_block = """
<div class="section">
  <button class="btn-run" id="run-btn">&#9654;&nbsp; Run Loop</button>
</div>
<div class="modal-overlay" id="loop-modal">
  <div class="modal">
    <div class="modal-hd">
      <h3>Loop Cycle Output</h3>
      <button class="modal-x" id="modal-close">&#10005;</button>
    </div>
    <div class="modal-bd"><pre class="loop-out" id="loop-out">Running…</pre></div>
    <div class="modal-ft">
      <button class="btn-run" id="modal-refresh">&#8635;&nbsp; Refresh Board</button>
    </div>
  </div>
</div>"""

    return (f'<h1 class="page-title">The Board</h1>'
            f'<p class="page-sub">Live state as of {date.today()}</p>'
            + ns + cap + dl_section + ship_section + pend_section + owed_section
            + out_section + run_block)


@app.route("/")
def board():
    return _layout("Board", _board_html(), "board", js=JS_BOARD)


# ═══════════════════════════════════════════════════════ page: trigger card ══

@app.route("/trigger")
def trigger():
    card = _parse_card()
    if card is None:
        content = ('<h1 class="page-title">Trigger Card</h1>'
                   '<p class="page-sub">No trigger card yet. Run the loop first.</p>'
                   '<a href="/" class="btn-run" style="text-decoration:none">'
                   '&#9654;&nbsp; Go to Board</a>')
        return _layout("Trigger Card", content, "trigger")

    action_escaped = esc(card["action"])
    content = f"""
<h1 class="page-title">Trigger Card</h1>
<p class="page-sub">Your one move for today.</p>
<div class="tc-wrap">
  <div class="tc-card">
    <div class="tc-head">
      <div class="tc-datelabel">{esc(card['date'])}</div>
      <div class="tc-move">{esc(card['move'])}</div>
    </div>
    <div class="tc-body">
      <div class="tc-sec">
        <div class="tc-lbl">Why Now</div>
        <div class="tc-val">{esc(card['why_now'])}</div>
      </div>
      <div class="tc-sec">
        <div class="tc-lbl">The Action</div>
        <div class="tc-action-wrap">
          <div class="tc-action-bar">
            <span class="tc-lang">bash</span>
            <button class="btn-sm" id="copy-btn">Copy</button>
          </div>
          <pre class="tc-code" id="action-code">{action_escaped}</pre>
        </div>
      </div>
    </div>
    <div class="tc-foot">
      <span class="badge badge-blue">{esc(card['time'])}</span>
      <button class="btn-done" id="done-btn" data-move="{esc(card['move'])}">
        &#10003;&nbsp; Mark as Done
      </button>
    </div>
  </div>
</div>"""
    return _layout("Trigger Card", content, "trigger", js=JS_TRIGGER)


# ══════════════════════════════════════════════════════════════ page: corpus ══

@app.route("/corpus")
def corpus():
    manifest = sl.load_manifest()
    count    = manifest.get("file_count", 0)
    built    = manifest.get("built", "")[:10]
    content  = f"""
<h1 class="page-title">Corpus Search</h1>
<p class="page-sub">{count} dossiers indexed &middot; built {esc(built)}</p>
<div class="search-wrap">
  <span class="search-icon">&#128269;</span>
  <input class="search-input" id="q" type="text"
         placeholder="Search your world model…" autofocus>
</div>
<div id="results">
  <p class="search-hint">Type a question to search your dossiers.</p>
</div>"""
    return _layout("Corpus", content, "corpus", js=JS_CORPUS)


# ══════════════════════════════════════════════════════════════ page: logs ══

@app.route("/logs")
def logs():
    bb  = _read_log("backbone_runlog.md")
    gov = _read_log("governor_log.md")
    content = f"""
<h1 class="page-title">Logs</h1>
<p class="page-sub">
  <span class="refresh-note"><span class="pulse"></span>Auto-refreshes every 30 s</span>
</p>
<div class="logs-grid">
  <div class="log-panel">
    <div class="log-hd">
      <span class="log-name">backbone_runlog.md</span>
      <span class="log-status">last 8 KB</span>
    </div>
    <pre class="log-body">{esc(bb)}</pre>
  </div>
  <div class="log-panel">
    <div class="log-hd">
      <span class="log-name">governor_log.md</span>
      <span class="log-status">last 8 KB</span>
    </div>
    <pre class="log-body">{esc(gov)}</pre>
  </div>
</div>"""
    return _layout("Logs", content, "logs", js=JS_LOGS)


# ════════════════════════════════════════════════════════════════ API routes ══

@app.route("/api/run_loop", methods=["POST"])
def api_run_loop():
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            loop_mod.run()
        return jsonify({"ok": True, "output": buf.getvalue()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "output": buf.getvalue()})


@app.route("/api/mark_done", methods=["POST"])
def api_mark_done():
    data    = request.get_json(force=True)
    trigger = data.get("trigger", "")
    try:
        sl.cmd_outcome(trigger, pulled=True, moved=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if len(q) < 3:
        return jsonify({"results": []})
    try:
        results = _search_corpus(q)
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)})


# ═══════════════════════════════════════════════════════════════════ main ══

if __name__ == "__main__":
    Timer(2.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    print(f"AgentOS Dashboard -> http://localhost:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
