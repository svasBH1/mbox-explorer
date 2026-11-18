#!/usr/bin/env python3
"""Index one or many mbox files into SQLite + FTS5.

The mbox is opened read-only and never modified. The database holds headers and
the plain-text body of each message (for full-text search), plus its source, byte
offset and length, so the viewer reads the original message back on demand.

    index_mbox.py TAKEOUT_DIR --db archive.sqlite
    index_mbox.py PROFILE_DIR --db mine.sqlite
    index_mbox.py ONE.mbox --db one.sqlite --limit 500000000   # trial run: first 500 MB
    index_mbox.py DIR --db DB --resume                         # continue after a stop
    index_mbox.py --db OLD.sqlite --migrate                    # upgrade a single-source DB

Nothing from a message body is printed. Progress lines carry counts only.
"""

import argparse
import os
import re
import sqlite3
import sys
import time
from email import policy
from email.parser import BytesParser
from datetime import timezone
from email.utils import getaddresses, parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mboxlib import __version__, detect_flavour, discover, find_last_boundary, human, iter_messages  # noqa: E402

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\r\f\v]+")
SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sources (
    id       INTEGER PRIMARY KEY,
    label    TEXT NOT NULL,
    path     TEXT NOT NULL UNIQUE,
    flavour  TEXT,
    size     INTEGER,
    good_bytes INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    offset        INTEGER NOT NULL,
    length        INTEGER NOT NULL,
    message_id    TEXT,
    thread_id     TEXT,
    date_epoch    INTEGER,
    date_display  TEXT,
    from_name     TEXT,
    from_addr     TEXT,
    to_addrs      TEXT,
    cc_addrs      TEXT,
    subject       TEXT,
    labels        TEXT,
    attach_count  INTEGER DEFAULT 0,
    parse_error   TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_date   ON messages(date_epoch);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_from   ON messages(from_addr);
CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_loc ON messages(source_id, offset);

CREATE TABLE IF NOT EXISTS attachments (
    message_id   INTEGER NOT NULL REFERENCES messages(id),
    part_index   INTEGER NOT NULL,
    filename     TEXT,
    content_type TEXT,
    size_encoded INTEGER
);
CREATE INDEX IF NOT EXISTS idx_attachments_msg ON attachments(message_id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, participants, body, labels,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def clean_text(text, cap=200000):
    """Collapse whitespace and drop tags so one huge message cannot blow up the index."""
    if not text:
        return ""
    text = SCRIPT_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    return WS_RE.sub(" ", text)[:cap]


def body_text(msg):
    """Best-effort plain text. Prefers text/plain, falls back to de-tagged HTML."""
    plain, html = [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_content()
        except Exception:                                       # noqa: BLE001
            try:
                raw = part.get_payload(decode=True) or b""
                payload = raw.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:                                   # noqa: BLE001
                continue
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        (plain if ctype == "text/plain" else html).append(payload)
    return clean_text(" ".join(plain) if plain else " ".join(html))


def addr_list(msg, header):
    vals = msg.get_all(header, [])
    if not vals:
        return ""
    try:
        pairs = getaddresses([str(v) for v in vals])
    except Exception:                                           # noqa: BLE001
        return ""
    return ", ".join(a for _, a in pairs if a)


def parse_one(raw):
    """raw includes the separator line."""
    nl = raw.find(b"\n")
    msg = BytesParser(policy=policy.default).parsebytes(raw[nl + 1:])

    epoch, display = None, ""
    if msg.get("Date"):
        display = str(msg.get("Date"))[:60]
        try:
            dt = parsedate_to_datetime(display)
            if dt:
                if dt.tzinfo is None:   # no zone or "-0000": UTC, not this machine's local time
                    dt = dt.replace(tzinfo=timezone.utc)
                epoch = int(dt.timestamp())
        except Exception:                                       # noqa: BLE001
            pass

    from_name = from_addr = ""
    if msg.get("From"):
        got = getaddresses([str(msg.get("From"))])
        if got:
            from_name, from_addr = got[0][0], got[0][1]

    attachments = []
    for i, part in enumerate(msg.walk()):
        fn = part.get_filename()
        if not fn:
            continue
        try:
            payload = part.get_payload(decode=False)
            size = len(payload) if isinstance(payload, str) else 0
        except Exception:                                       # noqa: BLE001
            size = 0
        attachments.append((i, str(fn)[:255], part.get_content_type(), size))

    return {
        "message_id": str(msg.get("Message-ID") or "")[:255],
        "thread_id": str(msg.get("X-GM-THRID") or "")[:64],
        "date_epoch": epoch,
        "date_display": display,
        "from_name": from_name[:255],
        "from_addr": from_addr[:255],
        "to_addrs": addr_list(msg, "To")[:2000],
        "cc_addrs": addr_list(msg, "Cc")[:2000],
        "subject": str(msg.get("Subject") or "")[:500],
        "labels": str(msg.get("X-Gmail-Labels") or "")[:500],
        "attachments": attachments,
        "body": body_text(msg),
    }


def migrate(conn):
    """Upgrade a single-source database written before multi-source support."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if not cols:
        sys.exit("No messages table — this is not an index database.")
    if "source_id" in cols:
        print("Already multi-source. Nothing to do.")
        return
    path = conn.execute("SELECT value FROM meta WHERE key='mbox_path'").fetchone()
    good = conn.execute("SELECT value FROM meta WHERE key='good_bytes'").fetchone()
    path = path[0] if path else "(unknown)"
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY, label TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
            flavour TEXT, size INTEGER, good_bytes INTEGER);
        ALTER TABLE messages ADD COLUMN source_id INTEGER REFERENCES sources(id);
    """)
    conn.execute("INSERT OR IGNORE INTO sources (id, label, path, flavour, size, good_bytes) "
                 "VALUES (1,?,?,?,?,?)",
                 (os.path.basename(path), path, "gmail",
                  os.path.getsize(path) if os.path.isfile(path) else None,
                  int(good[0]) if good else None))
    conn.execute("UPDATE messages SET source_id=1 WHERE source_id IS NULL")
    conn.executescript("""
        DROP INDEX IF EXISTS idx_messages_offset;
        CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_loc ON messages(source_id, offset);
    """)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print(f"Migrated {n:,} messages onto source 1 ({os.path.basename(path)}).")


def index_source(conn, src_id, path, end, start, commit_every):
    indexed = failed = 0
    t0 = time.time()
    cur = conn.cursor()
    for offset, raw in iter_messages(path, end, start):
        try:
            rec, err = parse_one(raw), None
        except Exception as exc:                                # noqa: BLE001
            rec, err = None, f"{type(exc).__name__}: {exc}"[:300]

        if rec is None:
            cur.execute("INSERT OR IGNORE INTO messages (source_id, offset, length, parse_error) "
                        "VALUES (?,?,?,?)", (src_id, offset, len(raw), err))
            failed += 1
        else:
            cur.execute(
                "INSERT OR IGNORE INTO messages (source_id, offset, length, message_id, "
                "thread_id, date_epoch, date_display, from_name, from_addr, to_addrs, "
                "cc_addrs, subject, labels, attach_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (src_id, offset, len(raw), rec["message_id"], rec["thread_id"],
                 rec["date_epoch"], rec["date_display"], rec["from_name"], rec["from_addr"],
                 rec["to_addrs"], rec["cc_addrs"], rec["subject"], rec["labels"],
                 len(rec["attachments"])))
            if cur.rowcount:
                mid = cur.lastrowid
                if rec["attachments"]:
                    cur.executemany(
                        "INSERT INTO attachments (message_id, part_index, filename, "
                        "content_type, size_encoded) VALUES (?,?,?,?,?)",
                        [(mid, i, fn, ct, sz) for i, fn, ct, sz in rec["attachments"]])
                participants = " ".join(filter(None, [rec["from_name"], rec["from_addr"],
                                                      rec["to_addrs"], rec["cc_addrs"]]))
                cur.execute("INSERT INTO messages_fts (rowid, subject, participants, body, labels) "
                            "VALUES (?,?,?,?,?)",
                            (mid, rec["subject"], participants, rec["body"], rec["labels"]))
            indexed += 1

        if (indexed + failed) % commit_every == 0:
            conn.commit()
            done, total = offset - start, max(1, end - start)
            rate = done / max(1e-6, time.time() - t0)
            print(f"    {indexed:,} indexed, {failed} failed — "
                  f"{100 * done / total:.1f}% ({human(rate)}/s)", flush=True)
    conn.commit()
    return indexed, failed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("source", nargs="?", help="mbox file, Takeout dir, or Thunderbird profile dir")
    ap.add_argument("--db", required=True)
    ap.add_argument("--limit", type=int, default=0, help="stop each source at this byte offset")
    ap.add_argument("--skip-tail", action="store_true",
                    help="drop everything after the last separator. For a download that was "
                         "cut off mid-message. Off by default, which keeps the final message")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--migrate", action="store_true", help="upgrade a single-source database")
    ap.add_argument("--commit-every", type=int, default=2000)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    if args.migrate:
        migrate(conn)
        conn.close()
        return
    if not args.source:
        ap.error("a source is required unless --migrate is given")
    if not os.path.exists(args.source):
        sys.exit(f"No such path: {args.source}")

    conn.executescript(SCHEMA)
    boxes = discover(args.source)
    if not boxes:
        sys.exit(f"No mbox files found under {args.source}")

    print(f"Found {len(boxes)} mbox file(s) under {args.source}")
    grand_i = grand_f = 0
    t0 = time.time()

    for label, path in boxes:
        size, last, _ = find_last_boundary(path)
        if last is None:
            print(f"  SKIP {label}: no separators found")
            continue
        flavour, _ = detect_flavour(path)
        good = last if args.skip_tail else size
        conn.execute("INSERT OR IGNORE INTO sources (label, path, flavour, size, good_bytes) "
                     "VALUES (?,?,?,?,?)", (label, os.path.abspath(path), flavour, size, good))
        conn.commit()
        src_id = conn.execute("SELECT id FROM sources WHERE path=?",
                              (os.path.abspath(path),)).fetchone()[0]

        end = min(args.limit, good) if args.limit else good
        start = 0
        if args.resume:
            row = conn.execute("SELECT MAX(offset + length) FROM messages WHERE source_id=?",
                               (src_id,)).fetchone()
            start = row[0] or 0
            if start >= end:
                print(f"  DONE {label} ({flavour}, {human(size)}) — already indexed")
                continue

        note = f", dropping a {human(size - last)} tail" if args.skip_tail else ""
        print(f"  {label}  [{flavour}, {human(size)}{note}]")
        i, f = index_source(conn, src_id, path, end, start, args.commit_every)
        grand_i += i
        grand_f += f
        print(f"    -> {i:,} indexed, {f} failed")

    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('root', ?)",
                 (os.path.abspath(args.source),))
    conn.commit()

    rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    att = conn.execute("SELECT COUNT(*) FROM messages WHERE attach_count > 0").fetchone()[0]
    span = conn.execute("SELECT MIN(date_epoch), MAX(date_epoch) FROM messages "
                        "WHERE date_epoch IS NOT NULL").fetchone()
    print()
    print(f"Indexed this run: {grand_i:,}  (parse failures: {grand_f})")
    print(f"Rows in database: {rows:,}")
    print(f"With attachments: {att:,}")
    if span and span[0]:
        f = "%Y-%m-%d"
        print(f"Date range:       {time.strftime(f, time.localtime(span[0]))} to "
              f"{time.strftime(f, time.localtime(span[1]))}")
    print(f"Elapsed:          {time.time() - t0:.0f}s")
    conn.close()


if __name__ == "__main__":
    main()
