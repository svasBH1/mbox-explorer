#!/usr/bin/env python3
"""
Offline web viewer for an indexed mbox archive.

Stdlib only. Binds 127.0.0.1, serves read-only, fetches nothing from the
internet. Message bodies are rendered as escaped text, never as live HTML.

Run:
    python3 mail_explorer.py --db archive.sqlite
    python3 mail_explorer.py --db DB --mbox /path/to.mbox --port 8770

The mbox path is read from the database unless --mbox overrides it.
"""

import argparse
import html
import os
import re
import sqlite3
import sys
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mboxlib import __version__  # noqa: E402

PAGE_SIZE = 50

CSS = """
:root { color-scheme: light dark; --line:#d6d3d1; --muted:#6b7280; --accent:#1d4ed8; }
@media (prefers-color-scheme: dark) { :root { --line:#3f3f46; --muted:#a1a1aa; --accent:#93c5fd; } }
* { box-sizing:border-box; }
body { font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin:0; }
header { padding:12px 20px; border-bottom:1px solid var(--line); display:flex;
         gap:16px; align-items:baseline; flex-wrap:wrap; }
header h1 { font-size:16px; margin:0; font-weight:600; }
header .meta { color:var(--muted); font-size:12px; }
main { padding:16px 20px; max-width:1100px; }
form.search { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:16px; }
input, select { padding:6px 8px; border:1px solid var(--line); border-radius:4px;
                background:transparent; color:inherit; font:inherit; }
input[name=q] { flex:1 1 320px; }
button { padding:6px 14px; border:1px solid var(--line); border-radius:4px;
         background:transparent; color:inherit; cursor:pointer; font:inherit; }
table { width:100%; border-collapse:collapse; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line);
         vertical-align:top; }
th { font-size:12px; color:var(--muted); font-weight:600; }
td.date { white-space:nowrap; color:var(--muted); font-size:12px; width:1%; }
td.who { max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.snippet { color:var(--muted); font-size:12px; }
.snippet b { color:inherit; background:rgba(250,204,21,.35); font-weight:600; }
.headers { border:1px solid var(--line); border-radius:6px; padding:12px;
           margin-bottom:16px; }
.headers div { margin:2px 0; }
.headers .k { display:inline-block; width:70px; color:var(--muted); }
pre.body { white-space:pre-wrap; word-wrap:break-word; border:1px solid var(--line);
           border-radius:6px; padding:12px; font:13px/1.55 ui-monospace,Menlo,Consolas,monospace; }
.pager { margin:16px 0; display:flex; gap:12px; }
.empty { color:var(--muted); padding:24px 0; }
.tag { display:inline-block; font-size:11px; border:1px solid var(--line);
       border-radius:10px; padding:1px 8px; margin-right:4px; color:var(--muted); }
"""


def esc(value):
    return html.escape('' if value is None else str(value), quote=True)


def fmt_date(epoch, fallback=''):
    if not epoch:
        return esc(fallback[:32])
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(epoch))


def page(title, body, meta=''):
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{esc(title)}</title><style>{CSS}</style></head><body>
<header><h1><a href="/">Mail Archive</a></h1><span class="meta">{meta}</span></header>
<main>{body}</main></body></html>""".encode('utf-8')


def fts_query(raw):
    """Pass through FTS5 syntax but neutralise a lone quote that would error."""
    q = raw.strip()
    if not q:
        return None
    if q.count('"') % 2:
        q = q.replace('"', ' ')
    return q


class Store:
    def __init__(self, db_path, mbox_path=None):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.multi = bool(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sources'").fetchone())
        self.sources = {}
        if self.multi:
            self.sources = {r['id']: (r['label'], r['path'])
                            for r in self.conn.execute(
                                'SELECT id, label, path FROM sources ORDER BY label')}
        row = self.conn.execute("SELECT value FROM meta WHERE key='mbox_path'").fetchone()
        self.mbox = mbox_path or (row['value'] if row else None)
        self.total = self.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]

    def search(self, q, label, sender, after, before, offset, source=''):
        where, params = [], []
        if q:
            sql = ('SELECT m.*, snippet(messages_fts, 2, "<b>", "</b>", "…", 12) AS snip '
                   'FROM messages_fts f JOIN messages m ON m.id = f.rowid '
                   'WHERE messages_fts MATCH ?')
            params.append(q)
        else:
            sql = 'SELECT m.*, "" AS snip FROM messages m WHERE 1=1'
        if label:
            where.append('m.labels LIKE ?'); params.append(f'%{label}%')
        if sender:
            where.append('(m.from_addr LIKE ? OR m.from_name LIKE ?)')
            params += [f'%{sender}%', f'%{sender}%']
        if source and self.multi:
            where.append('m.source_id IN (SELECT id FROM sources WHERE label LIKE ?)')
            params.append(f'%{source}%')
        if after:
            where.append('m.date_epoch >= ?'); params.append(after)
        if before:
            where.append('m.date_epoch <= ?'); params.append(before)
        if where:
            sql += ' AND ' + ' AND '.join(where)
        sql += ' ORDER BY m.date_epoch DESC LIMIT ? OFFSET ?'
        params += [PAGE_SIZE + 1, offset]
        return self.conn.execute(sql, params).fetchall()

    def source_of(self, row):
        """Path of the mbox a row came from. Falls back to the single-source path."""
        if self.multi and 'source_id' in row.keys() and row['source_id'] in self.sources:
            return self.sources[row['source_id']][1]
        return self.mbox

    def source_label(self, row):
        if self.multi and 'source_id' in row.keys() and row['source_id'] in self.sources:
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

    def send_html(self, payload, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):                                          # noqa: N802
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == '/':
                self.send_html(self.view_search(qs))
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
        except Exception as exc:                               # noqa: BLE001
            self.send_html(page('Error', f'<p class="empty">{esc(exc)}</p>'), 500)

    # views ---------------------------------------------------------------

    def view_search(self, qs):
        q = fts_query(qs.get('q', ''))
        label, sender = qs.get('label', ''), qs.get('from', '')
        source = qs.get('source', '')
        after = to_epoch(qs.get('after', ''))
        before = to_epoch(qs.get('before', ''), end_of_day=True)
        offset = max(0, int(qs.get('offset', 0) or 0))

        rows = self.store.search(q, label, sender, after, before, offset, source)
        more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]

        multi = len(self.store.sources) > 1
        picker = ''
        if multi:
            opts = ['<option value="">all sources</option>']
            for _, (lab, _p) in sorted(self.store.sources.items(), key=lambda kv: kv[1][0]):
                sel = ' selected' if lab == source else ''
                opts.append(f'<option value="{esc(lab)}"{sel}>{esc(lab)}</option>')
            picker = f'<select name="source">{"".join(opts)}</select>'

        form = f"""<form class="search" method="get" action="/">
<input name="q" value="{esc(qs.get('q',''))}" placeholder="Search subject, body, people…" autofocus>
<input name="from" value="{esc(sender)}" placeholder="from" size="14">
<input name="label" value="{esc(label)}" placeholder="label" size="12">
<input name="after" value="{esc(qs.get('after',''))}" placeholder="after YYYY-MM-DD" size="14">
<input name="before" value="{esc(qs.get('before',''))}" placeholder="before YYYY-MM-DD" size="14">
{picker}
<button type="submit">Search</button></form>"""

        if not rows:
            return page('Mail Archive', form + '<p class="empty">No matches.</p>',
                        f'{self.store.total:,} messages indexed')

        src_th = '<th>Source</th>' if multi else ''
        out = [form, f'<table><tr><th>Date</th>{src_th}<th>From</th><th>Subject</th></tr>']
        for r in rows:
            snip = f'<div class="snippet">{r["snip"]}</div>' if r['snip'] else ''
            paper = ' 📎' if r['attach_count'] else ''
            src_td = (f'<td class="who">{esc(self.store.source_label(r))}</td>') if multi else ''
            out.append(
                f'<tr><td class="date">{fmt_date(r["date_epoch"], r["date_display"])}</td>'
                f'{src_td}'
                f'<td class="who">{esc(r["from_name"] or r["from_addr"])}</td>'
                f'<td><a href="/msg?id={r["id"]}">{esc(r["subject"] or "(no subject)")}</a>'
                f'{paper}{snip}</td></tr>')
        out.append('</table>')

        nav = []
        base = {k: v for k, v in qs.items() if k != 'offset'}
        if offset:
            prev = dict(base, offset=max(0, offset - PAGE_SIZE))
            nav.append(f'<a href="/?{urlencode(prev)}">← previous</a>')
        if more:
            nxt = dict(base, offset=offset + PAGE_SIZE)
            nav.append(f'<a href="/?{urlencode(nxt)}">next →</a>')
        if nav:
            out.append(f'<div class="pager">{" ".join(nav)}</div>')

        shown = f'{offset + 1}–{offset + len(rows)}'
        return page('Mail Archive', ''.join(out),
                    f'showing {shown} · {self.store.total:,} messages indexed')

    def view_message(self, qs):
        row = self.store.message(int(qs.get('id', 0) or 0))
        if not row:
            return page('Not found', '<p class="empty">No such message.</p>', '')
        msg = self.store.parsed(row)

        labels = ''.join(f'<span class="tag">{esc(l.strip())}</span>'
                         for l in (row['labels'] or '').split(',') if l.strip())
        atts = self.store.attachments(row['id'])
        att_html = ''
        if atts:
            links = ' '.join(
                f'<a href="/attach?id={row["id"]}&part={a["part_index"]}">'
                f'{esc(a["filename"] or "attachment")}</a>' for a in atts)
            att_html = f'<div><span class="k">Files</span>{links}</div>'

        head = f"""<div class="headers">
<div><span class="k">Date</span>{fmt_date(row['date_epoch'], row['date_display'])}</div>
<div><span class="k">From</span>{esc(row['from_name'])} &lt;{esc(row['from_addr'])}&gt;</div>
<div><span class="k">To</span>{esc(row['to_addrs'])}</div>
{f'<div><span class="k">Cc</span>{esc(row["cc_addrs"])}</div>' if row['cc_addrs'] else ''}
<div><span class="k">Subject</span><strong>{esc(row['subject'])}</strong></div>
{f'<div><span class="k">Labels</span>{labels}</div>' if labels else ''}
{att_html}
<div><span class="k">Source</span>{esc(self.store.source_label(row) or 'mbox')} ·
byte {row['offset']:,} · {row['length']:,} bytes ·
<a href="/raw?id={row['id']}">raw</a></div>
</div>"""
        return page(row['subject'] or 'Message',
                    head + f'<pre class="body">{esc(body_of(msg))}</pre>', '')

    # downloads -----------------------------------------------------------

    def send_raw(self, qs):
        row = self.store.message(int(qs.get('id', 0) or 0))
        if not row:
            return self.send_html(page('Not found', ''), 404)
        data = self.store.raw(row)
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
            self.send_response(200)
            self.send_header('Content-Type', part.get_content_type())
            self.send_header('Content-Disposition',
                             f'attachment; filename="{name}"'.replace('\n', ''))
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
    ap.add_argument('--mbox', help='override the mbox path stored in the database')
    ap.add_argument('--port', type=int, default=8770)
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        sys.exit(f'No database at {args.db} — run index_mbox.py first.')

    store = Store(args.db, args.mbox)
    if store.sources:
        missing = [p for _l, p in store.sources.values() if not os.path.isfile(p)]
        if missing:
            sys.exit(f'Mbox not found: {missing[0]!r} ({len(missing)} of {len(store.sources)} sources missing).')
    elif not store.mbox or not os.path.isfile(store.mbox):
        sys.exit(f'Mbox not found: {store.mbox!r} — pass --mbox.')

    Handler.store = store
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'{store.total:,} messages indexed from {store.mbox}')
    print(f'Serving http://127.0.0.1:{args.port}  (Ctrl-C to stop)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
