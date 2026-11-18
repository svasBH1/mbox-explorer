#!/usr/bin/env python3
"""
Offline web viewer for an indexed mbox archive.

Stdlib only. Binds 127.0.0.1, serves read-only, fetches nothing from the
internet. Message bodies are rendered as escaped text, never as live HTML.

Run:
    python3 mail_explorer.py --db archive.sqlite
    python3 mail_explorer.py --db DB --mbox /path/to.mbox --port 8770

Each message is read back from the mbox recorded for its source in the
database. --mbox overrides the path for a single-source database.
"""

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mboxlib import __version__  # noqa: E402

BATCH_MAX = 500          # most rows one /api/search call returns
TOP_SENDERS = 12         # sender domains listed above the results

# Snippet markers: control characters that cannot survive into the escaped
# output, so a highlight is added only where FTS5 put one.
HL_OPEN, HL_CLOSE = '\x02', '\x03'

LIGHT = ('--bg:#ffffff; --fg:#1c1917; --panel:#fafaf9; --line:#e7e5e4; --muted:#6b7280; '
         '--accent:#1d4ed8; --sel:#dbeafe; --hl:rgba(250,204,21,.45); --chip:#f5f5f4;')
DARK = ('--bg:#18181b; --fg:#e4e4e7; --panel:#202024; --line:#3f3f46; --muted:#a1a1aa; '
        '--accent:#93c5fd; --sel:#1e3a5f; --hl:rgba(250,204,21,.30); --chip:#27272a;')

CSS = """
:root { __LIGHT__ color-scheme: light; }
/* Dark palette: follows the OS until the toggle stores a choice. */
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { __DARK__ color-scheme: dark; } }
:root[data-theme="dark"] { __DARK__ color-scheme: dark; }
* { box-sizing:border-box; }
html, body { height:100%; }
body { font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0;
       background:var(--bg); color:var(--fg); }
header { padding:10px 16px; border-bottom:1px solid var(--line); display:flex;
         gap:16px; align-items:center; flex-wrap:wrap; background:var(--panel); }
header h1 { font-size:16px; margin:0; font-weight:600; }
header h1 a { color:inherit; }
header .meta { color:var(--muted); font-size:12px; }
.goto { margin-left:auto; }
.goto input { width:110px; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
input, select, button { padding:5px 8px; border:1px solid var(--line); border-radius:4px;
       background:var(--bg); color:inherit; font:inherit; }
button { cursor:pointer; }
button:hover { background:var(--chip); }
#bar { height:3px; position:relative; overflow:hidden; background:transparent; }
#bar.on { background:var(--line); }
#bar span { position:absolute; top:0; bottom:0; left:0; width:0; background:var(--accent);
            transition:width .2s; }
#bar.slide span { width:30%; animation:slide 1.1s ease-in-out infinite; }
@keyframes slide { from { left:-30%; } to { left:100%; } }
form.search { display:flex; gap:6px; flex-wrap:wrap; padding:10px 16px;
              border-bottom:1px solid var(--line); }
form.search input[name=q] { flex:1 1 300px; }
.senders { padding:6px 16px; border-bottom:1px solid var(--line); font-size:12px;
           color:var(--muted); display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.chip { display:inline-flex; align-items:center; gap:4px; border:1px solid var(--line);
        border-radius:12px; padding:0 4px 0 9px; background:var(--chip); color:var(--fg); }
.chip.ex { text-decoration:line-through; opacity:.7; }
.chip button { border:0; background:transparent; padding:0 5px; color:var(--muted); }
.chip button:hover { color:var(--fg); }
.split { display:flex; height:calc(100vh - var(--top, 150px)); }
#list { flex:0 0 46%; overflow-y:auto; border-right:1px solid var(--line); }
#pane { flex:1; overflow-y:auto; padding:14px 18px; }
table { width:100%; border-collapse:collapse; }
td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
tr.row { cursor:pointer; }
tr.row:hover { background:var(--chip); }
tr.row.sel { background:var(--sel); }
td.date { white-space:nowrap; color:var(--muted); font-size:12px; width:1%; }
td.who { max-width:170px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
td.subj { word-break:break-word; }
.src { color:var(--muted); font-size:11px; }
.snippet { color:var(--muted); font-size:12px; }
.snippet b { color:inherit; background:var(--hl); font-weight:600; }
.status { color:var(--muted); font-size:12px; padding:10px 8px; }
.headers { border:1px solid var(--line); border-radius:6px; padding:10px 12px;
           margin-bottom:14px; background:var(--panel); }
.headers div { margin:2px 0; word-break:break-word; }
.headers .k { display:inline-block; width:70px; color:var(--muted); }
pre.body { white-space:pre-wrap; word-wrap:break-word; border:1px solid var(--line);
           border-radius:6px; padding:12px; font:13px/1.55 ui-monospace,Menlo,Consolas,monospace; }
.empty { color:var(--muted); padding:24px 0; }
.tag { display:inline-block; font-size:11px; border:1px solid var(--line);
       border-radius:10px; padding:1px 8px; margin-right:4px; color:var(--muted); }
main.full { padding:16px 20px; max-width:1100px; }
""".replace('__LIGHT__', LIGHT).replace('__DARK__', DARK)

THEME_JS = """
function currentTheme() {
  return document.documentElement.dataset.theme
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const b = document.getElementById('themeBtn');
  if (b) b.textContent = t === 'dark' ? 'Light mode' : 'Dark mode';
}
function toggleTheme() {
  const t = currentTheme() === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('mbx-theme', t); } catch (e) {}
  applyTheme(t);
}
(function () {
  let t = null;
  try { t = localStorage.getItem('mbx-theme'); } catch (e) {}
  if (t) document.documentElement.dataset.theme = t;
  addEventListener('DOMContentLoaded', () => applyTheme(currentTheme()));
})();
"""

APP_JS = """
const FIELDS = ['q', 'from', 'xfrom', 'label', 'after', 'before', 'source', 'sort'];
const BATCH = 200;
let S = { seq: 0, offset: 0, more: false, loading: false, total: null, loaded: 0, sel: -1, ids: [] };

const $ = id => document.getElementById(id);
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function params() {
  const p = new URLSearchParams();
  for (const f of FIELDS) { const el = document.forms.search[f]; if (el && el.value.trim()) p.set(f, el.value.trim()); }
  return p;
}
function setTop() {
  const r = $('list').getBoundingClientRect();
  document.documentElement.style.setProperty('--top', Math.ceil(r.top) + 'px');
}

// Loading bar: determinate when the share loaded is known, sliding otherwise.
let BUSY = 0;
function bar() {
  const b = $('bar'), s = b.firstElementChild;
  if (!BUSY) { b.className = ''; s.style.width = '0'; return; }
  if (S.total) { b.className = 'on'; s.style.width = Math.min(100, 100 * S.loaded / S.total) + '%'; }
  else b.className = 'on slide';
}
async function getJSON(url) {
  BUSY++; bar();
  try {
    const r = await fetch(url);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  } finally { BUSY--; bar(); }
}

function meta() {
  const t = S.total == null ? 'counting…' : S.total.toLocaleString() + ' matched';
  $('count').textContent = S.loaded.toLocaleString() + ' loaded · ' + t;
}

async function run() {
  const seq = ++S.seq;
  const p = params();
  history.replaceState(null, '', '/?' + p.toString());
  S = { ...S, offset: 0, more: true, total: null, loaded: 0, sel: -1, ids: [] };
  $('rows').innerHTML = ''; $('status').textContent = ''; meta();
  loadSenders(p, seq);
  getJSON('/api/count?' + p).then(d => { if (seq === S.seq) { S.total = d.total; meta(); bar(); } })
    .catch(e => { if (seq === S.seq) $('status').textContent = 'Count failed: ' + e.message; });
  await more(seq);
}

async function more(seq) {
  if (S.loading || !S.more || seq !== S.seq) return;
  S.loading = true;
  const p = params(); p.set('offset', S.offset); p.set('limit', BATCH);
  try {
    const d = await getJSON('/api/search?' + p);
    if (seq !== S.seq) return;
    const multi = d.multi;
    const html = d.rows.map(r => {
      S.ids.push(r.id);
      return '<tr class="row" data-i="' + (S.ids.length - 1) + '" data-id="' + r.id + '">'
        + '<td class="date">' + esc(r.date) + '</td>'
        + '<td class="who" title="' + esc(r.addr) + '">' + esc(r.from) + '</td>'
        + '<td class="subj"><a href="/msg?id=' + r.id + '">' + esc(r.subject || '(no subject)') + '</a>'
        + (r.att ? ' 📎' : '') + (multi ? ' <span class="src">' + esc(r.source) + '</span>' : '')
        + (r.snippet ? '<div class="snippet">' + r.snippet + '</div>' : '') + '</td></tr>';
    }).join('');
    $('rows').insertAdjacentHTML('beforeend', html);
    S.offset += d.rows.length; S.loaded = S.offset; S.more = d.more;
    meta(); bar();
    if (!S.loaded) $('status').textContent = 'No matches.';
    else if (!S.more) $('status').textContent = 'End of results.';
  } catch (e) {
    if (seq === S.seq) { $('status').textContent = 'Search failed: ' + e.message; S.more = false; }
  } finally {
    S.loading = false;
  }
  // Fill the list if the first batch did not reach the bottom of the pane.
  if (seq === S.seq && S.more && $('list').scrollHeight <= $('list').clientHeight + 200) more(seq);
}

async function loadSenders(p, seq) {
  const box = $('senders');
  try {
    const d = await getJSON('/api/senders?' + p);
    if (seq !== S.seq) return;
    const ex = (document.forms.search.xfrom.value || '').split(',').map(s => s.trim()).filter(Boolean);
    const chips = ex.map(x => '<span class="chip ex" title="excluded">' + esc(x)
        + '<button type="button" title="Include again" data-un="' + esc(x) + '">✕</button></span>');
    chips.push(...d.senders.map(s => '<span class="chip" title="' + s.count.toLocaleString() + ' messages">'
        + '<a href="#" data-only="' + esc(s.domain) + '">' + esc(s.domain) + '</a> '
        + s.count.toLocaleString()
        + '<button type="button" title="Exclude this sender" data-ex="' + esc(s.domain) + '">✕</button></span>'));
    const label = d.by === 'address' ? 'Top sender addresses:' : 'Top sender domains:';
    box.innerHTML = '<span>' + label + '</span>' + (chips.join('') || '<span>none</span>');
  } catch (e) { box.textContent = ''; }
  setTop();
}

function setField(name, value) { document.forms.search[name].value = value; run(); }
document.addEventListener('click', e => {
  const t = e.target;
  if (t.dataset.ex) {
    const f = document.forms.search.xfrom;
    const list = f.value.split(',').map(s => s.trim()).filter(Boolean);
    if (!list.includes(t.dataset.ex)) list.push(t.dataset.ex);
    setField('xfrom', list.join(', '));
  } else if (t.dataset.un) {
    const f = document.forms.search.xfrom;
    setField('xfrom', f.value.split(',').map(s => s.trim()).filter(s => s && s !== t.dataset.un).join(', '));
  } else if (t.dataset.only) {
    e.preventDefault(); setField('from', t.dataset.only);
  } else {
    const row = t.closest('tr.row');
    if (!row) return;
    if (t.tagName === 'A' && (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1)) return;
    e.preventDefault();
    select(+row.dataset.i);
  }
});

let MSG_SEQ = 0;
async function select(i) {
  if (i < 0 || i >= S.ids.length) return;
  const rows = $('rows').children;
  if (S.sel >= 0 && rows[S.sel]) rows[S.sel].classList.remove('sel');
  S.sel = i; rows[i].classList.add('sel');
  rows[i].scrollIntoView({ block: 'nearest' });
  await showMessage(S.ids[i]);
  if (i > S.ids.length - 20 && S.more) more(S.seq);
}

async function showMessage(id) {
  const seq = ++MSG_SEQ;
  BUSY++; bar();
  try {
    const r = await fetch('/msg?pane=1&id=' + encodeURIComponent(id));
    const h = await r.text();
    if (seq === MSG_SEQ) { $('pane').innerHTML = h; $('pane').scrollTop = 0; }
  } catch (e) {
    if (seq === MSG_SEQ) $('pane').textContent = 'Could not load the message: ' + e.message;
  } finally { BUSY--; bar(); }
}

// Go to a message by its id, whether or not it is in the current results.
function goTo(id) {
  id = String(id).replace(/[^0-9]/g, '');
  if (!id) return;
  const rows = $('rows').children;
  if (S.sel >= 0 && rows[S.sel]) rows[S.sel].classList.remove('sel');
  const i = S.ids.indexOf(+id);
  S.sel = i;
  if (i >= 0) { rows[i].classList.add('sel'); rows[i].scrollIntoView({ block: 'nearest' }); }
  showMessage(id);
}

document.addEventListener('keydown', e => {
  if (/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
  if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); select(S.sel + 1); }
  else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); select(Math.max(0, S.sel - 1)); }
  else if (e.key === '/') { e.preventDefault(); document.forms.search.q.focus(); }
});

addEventListener('DOMContentLoaded', () => {
  const p = new URLSearchParams(location.search);
  for (const f of FIELDS) if (p.has(f) && document.forms.search[f]) document.forms.search[f].value = p.get(f);
  document.forms.search.addEventListener('submit', e => { e.preventDefault(); run(); });
  document.forms.search.source && document.forms.search.source.addEventListener('change', run);
  document.forms.search.sort.addEventListener('change', run);
  document.forms.goto.addEventListener('submit', e => { e.preventDefault(); goTo(document.forms.goto.msgid.value); });
  if (p.get('id')) goTo(p.get('id'));
  new IntersectionObserver(es => { if (es[0].isIntersecting) more(S.seq); },
                           { root: $('list'), rootMargin: '600px' }).observe($('sentinel'));
  addEventListener('resize', setTop);
  setTop();
  run();
});
"""


def esc(value):
    return html.escape('' if value is None else str(value), quote=True)


def fmt_date(epoch, fallback=''):
    if not epoch:
        return (fallback or '')[:32]
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(epoch))


def snippet_html(raw):
    """Escape an FTS5 snippet, then turn only the highlight markers into <b>."""
    if not raw:
        return ''
    return esc(raw).replace(HL_OPEN, '<b>').replace(HL_CLOSE, '</b>')


def theme_button():
    return ('<button id="themeBtn" class="themebtn" type="button" onclick="toggleTheme()" '
            'title="Switch between light and dark">Dark mode</button>')


def page(title, body, meta=''):
    """A full page: used for a message opened on its own and for errors."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><link rel="icon" href="data:,">
<title>{esc(title)}</title><style>{CSS}</style><script>{THEME_JS}</script></head><body>
<header><h1><a href="/">Mail Archive</a></h1><span class="meta">{meta}</span>{theme_button()}</header>
<main class="full">{body}</main></body></html>""".encode('utf-8')


def fts_query(raw):
    """Pass through FTS5 syntax but neutralise a lone quote that would error."""
    q = (raw or '').strip()
    if not q:
        return None
    if q.count('"') % 2:
        q = q.replace('"', ' ')
    return q


def split_list(value):
    return [v.strip() for v in (value or '').split(',') if v.strip()]


class Store:
    def __init__(self, db_path, mbox_path=None):
        self.db_path = db_path
        self.local = threading.local()
        conn = self.conn
        self.multi = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sources'").fetchone())
        self.sources = {}
        if self.multi:
            self.sources = {r['id']: (r['label'], r['path'])
                            for r in conn.execute('SELECT id, label, path FROM sources ORDER BY label')}
        row = conn.execute("SELECT value FROM meta WHERE key='mbox_path'").fetchone()
        self.mbox = mbox_path or (row['value'] if row else None)
        self.total = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]

    @property
    def conn(self):
        """One read-only connection per server thread; requests run in parallel."""
        c = getattr(self.local, 'conn', None)
        if c is None:
            uri = 'file:' + os.path.abspath(self.db_path).replace('?', '%3F') + '?mode=ro'
            c = sqlite3.connect(uri, uri=True)
            c.row_factory = sqlite3.Row
            self.local.conn = c
        return c

    def where(self, f):
        """Filters -> (FROM/WHERE clause, params). f holds the request's search fields."""
        params = []
        q = fts_query(f.get('q'))
        if q:
            sql = 'FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid WHERE messages_fts MATCH ?'
            params.append(q)
        else:
            sql = 'FROM messages m WHERE 1=1'
        if f.get('label'):
            sql += ' AND m.labels LIKE ?'
            params.append(f"%{f['label']}%")
        if f.get('from'):
            sql += " AND (COALESCE(m.from_addr,'') LIKE ? OR COALESCE(m.from_name,'') LIKE ?)"
            params += [f"%{f['from']}%"] * 2
        for x in split_list(f.get('xfrom')):
            sql += " AND NOT (COALESCE(m.from_addr,'') LIKE ? OR COALESCE(m.from_name,'') LIKE ?)"
            params += [f'%{x}%'] * 2
        if f.get('source') and self.multi:
            sql += ' AND m.source_id IN (SELECT id FROM sources WHERE label = ?)'
            params.append(f['source'])
        after, before = to_epoch(f.get('after')), to_epoch(f.get('before'), end_of_day=True)
        if after:
            sql += ' AND m.date_epoch >= ?'
            params.append(after)
        if before:
            sql += ' AND m.date_epoch <= ?'
            params.append(before)
        return sql, params, bool(q)

    def search(self, f, offset, limit):
        sql, params, has_q = self.where(f)
        snip = (f"snippet(messages_fts, 2, '{HL_OPEN}', '{HL_CLOSE}', '…', 12)" if has_q else "''")
        order = {'old': 'm.date_epoch ASC', 'rank': 'messages_fts.rank' if has_q else 'm.date_epoch DESC'}
        sql = (f'SELECT m.id, m.source_id, m.date_epoch, m.date_display, m.from_name, m.from_addr, '
               f'm.subject, m.attach_count, {snip} AS snip {sql} '
               f"ORDER BY {order.get(f.get('sort'), 'm.date_epoch DESC')}, m.id LIMIT ? OFFSET ?")
        return self.conn.execute(sql, params + [limit + 1, offset]).fetchall()

    def count(self, f):
        sql, params, _ = self.where(f)
        return self.conn.execute(f'SELECT COUNT(*) {sql}', params).fetchone()[0]

    def senders(self, f):
        """Top senders for the current filters: by domain, or by address once a from filter is set."""
        sql, params, _ = self.where(f)
        if f.get('from'):
            key, by = "LOWER(COALESCE(m.from_addr,''))", 'address'
        else:
            key, by = ("LOWER(CASE WHEN INSTR(COALESCE(m.from_addr,''), '@') > 0 "
                       "THEN SUBSTR(m.from_addr, INSTR(m.from_addr, '@') + 1) "
                       "ELSE COALESCE(m.from_addr,'') END)"), 'domain'
        rows = self.conn.execute(f'SELECT {key} AS d, COUNT(*) AS n {sql} GROUP BY d '
                                 f'ORDER BY n DESC LIMIT ?', params + [TOP_SENDERS]).fetchall()
        return by, [{'domain': r['d'] or '(none)', 'count': r['n']} for r in rows]

    def source_of(self, row):
        """Path of the mbox a row came from. Falls back to the single-source path."""
        if self.multi and row['source_id'] in self.sources:
            return self.sources[row['source_id']][1]
        return self.mbox

    def source_label(self, row):
        if self.multi and row['source_id'] in self.sources:
            return self.sources[row['source_id']][0]
        return ''

    def message(self, mid):
        return self.conn.execute('SELECT * FROM messages WHERE id=?', (mid,)).fetchone()

    def attachments(self, mid):
        return self.conn.execute('SELECT * FROM attachments WHERE message_id=? '
                                 'ORDER BY part_index', (mid,)).fetchall()

    def raw(self, row):
        with open(self.source_of(row), 'rb') as fh:
            fh.seek(row['offset'])
            return fh.read(row['length'])

    def parsed(self, row):
        raw = self.raw(row)
        nl = raw.find(b'\n')
        return BytesParser(policy=policy.default).parsebytes(raw[nl + 1:])


def body_of(msg):
    plain, htmls = [], []
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart' or part.get_filename():
            continue
        if part.get_content_type() not in ('text/plain', 'text/html'):
            continue
        try:
            content = part.get_content()
        except Exception:                                     # noqa: BLE001
            raw = part.get_payload(decode=True) or b''
            content = raw.decode(part.get_content_charset() or 'utf-8', 'replace')
        (plain if part.get_content_type() == 'text/plain' else htmls).append(content)
    if plain:
        return '\n'.join(plain)
    text = '\n'.join(htmls)
    text = re.sub(r'(?is)<(script|style).*?</\1>', ' ', text)
    text = re.sub(r'(?i)<br\s*/?>|</p>', '\n', text)
    return re.sub(r'<[^>]+>', '', text)


class Handler(BaseHTTPRequestHandler):
    store = None
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *a):        # quiet by default
        pass

    def send_bytes(self, payload, ctype, code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, payload, code=200):
        self.send_bytes(payload, 'text/html; charset=utf-8', code)

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj).encode('utf-8'), 'application/json', code)

    def do_GET(self):                                          # noqa: N802
        # Answer only requests addressed to this machine by name or loopback address, so a
        # web page cannot reach the viewer through DNS rebinding.
        port = self.server.server_address[1]
        if self.headers.get('Host', '') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
            return self.send_html(page('Forbidden', '<p class="empty">Unexpected Host header.</p>'), 403)
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == '/':
                self.send_html(self.view_app())
            elif url.path == '/api/search':
                self.api_search(qs)
            elif url.path == '/api/count':
                self.send_json({'total': self.store.count(qs)})
            elif url.path == '/api/senders':
                by, senders = self.store.senders(qs)
                self.send_json({'by': by, 'senders': senders})
            elif url.path == '/msg':
                self.send_html(self.view_message(qs))
            elif url.path == '/raw':
                self.send_raw(qs)
            elif url.path == '/attach':
                self.send_attachment(qs)
            else:
                self.send_html(page('Not found', '<p class="empty">No such page.</p>'), 404)
        except BrokenPipeError:
            pass
        except sqlite3.OperationalError as exc:
            # Usually FTS5 syntax in the query box; say so rather than fail silently.
            if url.path.startswith('/api/'):
                self.send_json({'error': f'query not understood ({exc})'}, 400)
            else:
                self.send_html(page('Error', f'<p class="empty">{esc(exc)}</p>'), 400)
        except Exception as exc:                               # noqa: BLE001
            if url.path.startswith('/api/'):
                self.send_json({'error': str(exc)}, 500)
            else:
                self.send_html(page('Error', f'<p class="empty">{esc(exc)}</p>'), 500)

    # views ---------------------------------------------------------------

    def view_app(self):
        picker = ''
        if len(self.store.sources) > 1:
            opts = ['<option value="">all sources</option>'] + [
                f'<option value="{esc(lab)}">{esc(lab)}</option>'
                for lab, _p in sorted(self.store.sources.values())]
            picker = f'<select name="source" title="Source">{"".join(opts)}</select>'
        body = f"""<header><h1><a href="/">Mail Archive</a></h1>
<span class="meta" id="count"></span>
<span class="meta">{self.store.total:,} messages indexed</span>
<form name="goto" class="goto" autocomplete="off" title="Open a message by its id (shown on every message)">
<input name="msgid" inputmode="numeric" placeholder="go to id #" size="10"></form>{theme_button()}</header>
<div id="bar"><span></span></div>
<form class="search" name="search" autocomplete="off">
<input name="q" placeholder="Search subject, body, people…  (AND, OR, NOT, NEAR(a b), &quot;phrases&quot;)" autofocus>
<input name="from" placeholder="from" size="14" title="Sender name or address contains">
<input name="xfrom" placeholder="exclude senders" size="18" title="Comma-separated; hides senders containing any of these">
<input name="label" placeholder="label" size="10">
<input name="after" placeholder="after YYYY-MM-DD" size="15">
<input name="before" placeholder="before YYYY-MM-DD" size="15">
{picker}
<select name="sort" title="Order"><option value="">newest first</option>
<option value="old">oldest first</option><option value="rank">best match (with a search)</option></select>
<button type="submit">Search</button></form>
<div class="senders" id="senders"></div>
<div class="split"><div id="list"><table><tbody id="rows"></tbody></table>
<div class="status" id="status"></div><div id="sentinel"></div></div>
<div id="pane"><p class="empty">Select a message. ↑/↓ or j/k move through the list; / jumps to search.
Ctrl-click a subject to open it in a new tab.</p></div></div>"""
        return f"""<!doctype html><html><head><meta charset="utf-8"><link rel="icon" href="data:,"><title>Mail Archive</title>
<style>{CSS}</style><script>{THEME_JS}</script><script>{APP_JS}</script></head>
<body>{body}</body></html>""".encode('utf-8')

    def api_search(self, qs):
        offset = max(0, int(qs.get('offset', 0) or 0))
        limit = min(BATCH_MAX, max(1, int(qs.get('limit', 200) or 200)))
        rows = self.store.search(qs, offset, limit)
        out = [{'id': r['id'], 'date': fmt_date(r['date_epoch'], r['date_display']),
                'from': r['from_name'] or r['from_addr'] or '', 'addr': r['from_addr'] or '',
                'subject': r['subject'] or '', 'att': bool(r['attach_count']),
                'source': self.store.source_label(r), 'snippet': snippet_html(r['snip'])}
               for r in rows[:limit]]
        self.send_json({'rows': out, 'more': len(rows) > limit, 'multi': len(self.store.sources) > 1})

    def view_message(self, qs):
        row = self.store.message(int(qs.get('id', 0) or 0))
        pane = qs.get('pane') == '1'
        if not row:
            body = '<p class="empty">No such message.</p>'
            return body.encode('utf-8') if pane else page('Not found', body)
        msg = self.store.parsed(row)

        labels = ''.join(f'<span class="tag">{esc(l.strip())}</span>'
                         for l in (row['labels'] or '').split(',') if l.strip())
        atts = self.store.attachments(row['id'])
        att_html = ''
        if atts:
            links = ' · '.join(
                f'<a href="/attach?id={row["id"]}&amp;part={a["part_index"]}">'
                f'{esc(a["filename"] or "attachment")}</a>' for a in atts)
            att_html = f'<div><span class="k">Files</span>{links}</div>'

        head = f"""<div class="headers">
<div><span class="k">Date</span>{esc(fmt_date(row['date_epoch'], row['date_display']))}</div>
<div><span class="k">From</span>{esc(row['from_name'])} &lt;{esc(row['from_addr'])}&gt;</div>
<div><span class="k">To</span>{esc(row['to_addrs'])}</div>
{f'<div><span class="k">Cc</span>{esc(row["cc_addrs"])}</div>' if row['cc_addrs'] else ''}
<div><span class="k">Subject</span><strong>{esc(row['subject'])}</strong></div>
{f'<div><span class="k">Labels</span>{labels}</div>' if labels else ''}
{att_html}
<div><span class="k">Source</span>{esc(self.store.source_label(row) or 'mbox')} ·
id {row['id']} · byte {row['offset']:,} · {row['length']:,} bytes ·
<a href="/raw?id={row['id']}">raw</a>{'' if not pane else f' · <a href="/msg?id={row["id"]}" target="_blank">open in new tab</a>'}</div>
</div>"""
        body = head + f'<pre class="body">{esc(body_of(msg))}</pre>'
        return body.encode('utf-8') if pane else page(row['subject'] or 'Message', body)

    # downloads -----------------------------------------------------------

    def send_raw(self, qs):
        row = self.store.message(int(qs.get('id', 0) or 0))
        if not row:
            return self.send_html(page('Not found', ''), 404)
        data = self.store.raw(row)
        data = data[data.find(b'\n') + 1:]         # drop the mbox separator line: a plain .eml
        self.send_response(200)
        self.send_header('Content-Type', 'message/rfc822')
        self.send_header('Content-Disposition', f'attachment; filename="message-{row["id"]}.eml"')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_attachment(self, qs):
        row = self.store.message(int(qs.get('id', 0) or 0))
        want = int(qs.get('part', -1))
        if not row or want < 0:
            return self.send_html(page('Not found', ''), 404)
        msg = self.store.parsed(row)
        for i, part in enumerate(msg.walk()):
            if i != want:
                continue
            data = part.get_payload(decode=True) or b''
            name = os.path.basename(part.get_filename() or f'part-{i}')
            name = re.sub(r'["\r\n\\]', '_', name)
            self.send_response(200)
            self.send_header('Content-Type', part.get_content_type())
            self.send_header('Content-Disposition', f'attachment; filename="{name}"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_html(page('Not found', '<p class="empty">No such part.</p>'), 404)


def to_epoch(value, end_of_day=False):
    if not value:
        return None
    try:
        t = time.strptime(value.strip(), '%Y-%m-%d')
    except ValueError:
        return None
    epoch = int(time.mktime(t))
    return epoch + 86399 if end_of_day else epoch


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    ap.add_argument('--db', required=True)
    ap.add_argument('--mbox', help='override the mbox path stored in a single-source database')
    ap.add_argument('--port', type=int, default=8770)
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        sys.exit(f'No database at {args.db} — run index_mbox.py first.')

    store = Store(args.db, args.mbox)
    if not store.multi:
        sys.exit(f'{args.db} was written by an older version. Upgrade it in place (seconds, '
                 f'no reindex): python3 index_mbox.py --db {args.db} --migrate')
    if args.mbox:
        if len(store.sources) != 1:
            sys.exit('--mbox only applies to a database with one source.')
        sid = next(iter(store.sources))
        store.sources[sid] = (store.sources[sid][0], args.mbox)
    if store.sources:
        missing = [p for _l, p in store.sources.values() if not os.path.isfile(p)]
        if missing:
            sys.exit(f'Mbox not found: {missing[0]!r} '
                     f'({len(missing)} of {len(store.sources)} sources missing).')
        where = f'{len(store.sources)} source(s)'
    elif not store.mbox or not os.path.isfile(store.mbox):
        sys.exit(f'Mbox not found: {store.mbox!r} — pass --mbox.')
    else:
        where = store.mbox

    Handler.store = store
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'{store.total:,} messages indexed from {where}')
    print(f'Serving http://127.0.0.1:{args.port}  (Ctrl-C to stop)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
