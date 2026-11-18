#!/usr/bin/env python3
"""Search an indexed mbox archive from the command line.

    search_mbox.py --db A.sqlite --query 'invoice NEAR overdue'
    search_mbox.py --db A.sqlite --query budget --since 2024-01-01 --until 2024-12-31
    search_mbox.py --db A.sqlite --from example.com --subject renewal --tsv
    search_mbox.py --db A.sqlite --query '"project plan"' --extract out/
    search_mbox.py --db A.sqlite --stats

--query runs against the FTS5 index over subject, participants, body and labels.
Bare words are quoted so punctuation in a search term cannot become syntax. Use
AND, OR, NOT, NEAR and "quoted phrases" as normal.

Every run prints how many messages were searched alongside how many matched, so
a zero result cannot be mistaken for a clean sweep.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from email import policy
from email.parser import BytesParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index_mbox import body_text  # noqa: E402
from mboxlib import __version__  # noqa: E402

OPERATORS = {"AND", "OR", "NOT", "NEAR"}
TOKEN_RE = re.compile(r'"[^"]*"|\S+')


def fts_query(raw):
    """Quote bare terms so stray punctuation cannot break FTS5 syntax."""
    out = []
    for tok in TOKEN_RE.findall(raw or ""):
        if tok.startswith('"') or tok.upper() in OPERATORS or tok.startswith("("):
            out.append(tok)
        elif re.fullmatch(r"[A-Za-z0-9_]+\*?", tok):
            out.append(tok)
        else:
            out.append('"' + tok.replace('"', "") + '"')
    return " ".join(out)


def to_epoch(value, end_of_day=False):
    if not value:
        return None
    t = time.strptime(value, "%Y-%m-%d")
    base = time.mktime(t)
    return int(base + 86399 if end_of_day else base)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--db", required=True)
    ap.add_argument("--query", help="FTS5 query over subject, participants, body, labels")
    ap.add_argument("--subject", help="substring match on subject")
    ap.add_argument("--from", dest="sender", help="substring match on sender name or address")
    ap.add_argument("--to", dest="recipient", help="substring match on To or Cc")
    ap.add_argument("--source", help="substring match on the source mbox label")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--tsv", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stats", action="store_true", help="summary only, no message list")
    ap.add_argument("--extract", metavar="DIR", help="write each matching message as a .txt")
    a = ap.parse_args()

    if not os.path.isfile(a.db):
        sys.exit(f"No such database: {a.db}")
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    multi = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sources'").fetchone())
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    sel = ("SELECT m.id, m.source_id, m.offset, m.length, m.date_epoch, m.date_display, "
           "m.from_name, m.from_addr, m.to_addrs, m.cc_addrs, m.subject, m.labels, "
           "m.attach_count")
    src_join = ", s.label AS source, s.path AS source_path FROM messages m " \
               "LEFT JOIN sources s ON s.id = m.source_id" if multi else \
               ", '' AS source, '' AS source_path FROM messages m"
    where, params = [], []

    if a.query:
        src_join += " JOIN messages_fts f ON f.rowid = m.id"
        where.append("messages_fts MATCH ?")
        params.append(fts_query(a.query))
    for col, val in (("m.subject", a.subject), ("COALESCE(m.to_addrs,'') || ' ' || COALESCE(m.cc_addrs,'')", a.recipient)):
        if val:
            where.append(f"{col} LIKE ?")
            params.append(f"%{val}%")
    if a.sender:
        where.append("(COALESCE(m.from_addr,'') LIKE ? OR COALESCE(m.from_name,'') LIKE ?)")
        params += [f"%{a.sender}%", f"%{a.sender}%"]
    if a.source and multi:
        where.append("s.label LIKE ?")
        params.append(f"%{a.source}%")
    if a.since:
        where.append("m.date_epoch >= ?")
        params.append(to_epoch(a.since))
    if a.until:
        where.append("m.date_epoch <= ?")
        params.append(to_epoch(a.until, True))

    sql = sel + src_join
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY m.date_epoch"

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        sys.exit(f"query failed: {exc}")

    print(f"# {total:,} messages in index, {len(rows):,} matched", file=sys.stderr)
    if not rows:
        return

    from collections import Counter
    years = Counter(time.strftime("%Y", time.localtime(r["date_epoch"]))
                    for r in rows if r["date_epoch"])
    print(f"# by year: {dict(sorted(years.items()))}", file=sys.stderr)
    if multi:
        srcs = Counter(r["source"] or "?" for r in rows)
        if len(srcs) > 1:
            print(f"# by source: {dict(srcs)}", file=sys.stderr)
    if a.stats:
        senders = Counter(r["from_addr"] for r in rows if r["from_addr"])
        print("# top senders:", file=sys.stderr)
        for addr, n in senders.most_common(15):
            print(f"#   {n:>5}  {addr}", file=sys.stderr)
        return

    shown = rows[:a.limit]
    if len(rows) > a.limit:
        print(f"# showing first {a.limit}; raise --limit for the rest", file=sys.stderr)

    def day(r):
        return time.strftime("%Y-%m-%d", time.localtime(r["date_epoch"])) if r["date_epoch"] else "?"

    if a.json:
        json.dump([{k: r[k] for k in r.keys()} for r in shown], sys.stdout, indent=1, default=str)
        print()
    elif a.tsv:
        print("date\tsource\tfrom\tsubject")
        for r in shown:
            print(f"{day(r)}\t{r['source']}\t{r['from_addr']}\t{(r['subject'] or '')[:160]}")
    else:
        for r in shown:
            print(f"{day(r)} | {(r['from_addr'] or '')[:38]:<38} | {(r['subject'] or '')[:80]}")

    if a.extract:
        os.makedirs(a.extract, exist_ok=True)
        written = 0
        for r in shown:
            path = r["source_path"] or conn.execute(
                "SELECT value FROM meta WHERE key='mbox_path'").fetchone()[0]
            with open(path, "rb") as fh:
                fh.seek(r["offset"])
                raw = fh.read(r["length"])
            nl = raw.find(b"\n")
            msg = BytesParser(policy=policy.default).parsebytes(raw[nl + 1:])
            stamp = (time.strftime("%Y%m%d-%H%M", time.localtime(r["date_epoch"]))
                     if r["date_epoch"] else "unknown")
            safe = re.sub(r"[^A-Za-z0-9]+", "-", r["subject"] or "no-subject")[:65].strip("-")
            out, n = os.path.join(a.extract, f"{stamp}_{safe}.txt"), 2
            while os.path.exists(out):
                out = os.path.join(a.extract, f"{stamp}_{safe}__{n}.txt")
                n += 1
            with open(out, "w", encoding="utf-8") as fh:
                for h in ("Date", "From", "To", "Cc", "Subject"):
                    if msg.get(h):
                        fh.write(f"{h}: {msg.get(h)}\n")
                fh.write(f"X-Source: {r['source']}\n\n")
                fh.write((body_text(msg) or "").strip() + "\n")
            written += 1
        print(f"# {written} message(s) written to {a.extract}", file=sys.stderr)


if __name__ == "__main__":
    main()
