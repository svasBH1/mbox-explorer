import json
import sqlite3

from conftest import make_message, run, write_mbox


def rows(db, sql, *params):
    with sqlite3.connect(db) as c:
        return c.execute(sql, params).fetchall()


def test_index_two_sources(indexed):
    assert rows(indexed, "SELECT COUNT(*) FROM messages") == [(5,)]
    labels = sorted(r[0].replace("\\", "/") for r in rows(indexed, "SELECT label FROM sources"))
    assert labels == ["Groups/topics.mbox", "Mail/All mail.mbox"]
    assert rows(indexed, "SELECT COUNT(*) FROM attachments") == [(1,)]


def test_offsets_point_at_separators(indexed):
    for path, offset in rows(indexed, "SELECT s.path, m.offset FROM messages m JOIN sources s ON s.id = m.source_id"):
        with open(path, "rb") as f:
            f.seek(offset)
            assert f.read(5) == b"From "


def test_reindex_is_idempotent(archive, indexed):
    run("index_mbox.py", archive, "--db", indexed)
    assert rows(indexed, "SELECT COUNT(*) FROM messages") == [(5,)]


def test_skip_tail_drops_partial_message_and_default_keeps_last(tmp_path):
    p = tmp_path / "cut.mbox"
    write_mbox(p, [make_message(i) for i in range(3)])
    with open(p, "ab") as f:
        f.write(b"From 1700000000000000999@xxx Mon Mar 03 10:00:00 +0000 2025\nSubject: partial")
    keep, skip = tmp_path / "keep.sqlite", tmp_path / "skip.sqlite"
    run("index_mbox.py", p, "--db", keep)
    run("index_mbox.py", p, "--db", skip, "--skip-tail")
    assert rows(keep, "SELECT COUNT(*) FROM messages") == [(4,)]
    assert rows(skip, "SELECT COUNT(*) FROM messages") == [(3,)]


def test_limit_then_resume(tmp_path):
    p = tmp_path / "box.mbox"
    offsets = write_mbox(p, [make_message(i) for i in range(6)])
    db = tmp_path / "a.sqlite"
    run("index_mbox.py", p, "--db", db, "--limit", offsets[3])
    assert rows(db, "SELECT COUNT(*) FROM messages") == [(3,)]
    run("index_mbox.py", p, "--db", db, "--resume")
    assert rows(db, "SELECT COUNT(*) FROM messages") == [(6,)]
    assert rows(db, "SELECT COUNT(DISTINCT offset) FROM messages") == [(6,)]


def test_migrate_single_source_database(tmp_path):
    p = tmp_path / "old.mbox"
    write_mbox(p, [make_message(1)])
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE messages (id INTEGER PRIMARY KEY, offset INTEGER NOT NULL, length INTEGER NOT NULL,
                message_id TEXT, thread_id TEXT, date_epoch INTEGER, date_display TEXT, from_name TEXT,
                from_addr TEXT, to_addrs TEXT, cc_addrs TEXT, subject TEXT, labels TEXT,
                attach_count INTEGER DEFAULT 0, parse_error TEXT);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        c.execute("INSERT INTO messages (offset, length, subject) VALUES (0, ?, 'old')", (p.stat().st_size,))
        c.execute("INSERT INTO meta VALUES ('mbox_path', ?)", (str(p),))
    out = run("index_mbox.py", "--db", db, "--migrate").stdout
    assert "Migrated 1 messages" in out
    assert rows(db, "SELECT source_id FROM messages") == [(1,)]
    assert rows(db, "SELECT path FROM sources") == [(str(p),)]
    assert "Already multi-source" in run("index_mbox.py", "--db", db, "--migrate").stdout


def search(db, *args):
    p = run("search_mbox.py", "--db", db, "--json", *args)
    return p, json.loads(p.stdout or "[]")


def test_search_query_and_counts_line(indexed):
    p, hits = search(indexed, "--query", "budget")
    assert sorted(h["subject"] for h in hits) == ["message 1", "message 5"]
    assert "5 messages in index, 2 matched" in p.stderr


def test_search_zero_results_still_reports_index_size(indexed):
    p, hits = search(indexed, "--query", "nonexistentword")
    assert hits == [] and "5 messages in index, 0 matched" in p.stderr


def test_search_filters(indexed):
    assert len(search(indexed, "--from", "alerts.example")[1]) == 2
    assert [h["subject"] for h in search(indexed, "--to", "carol@")[1]] == ["message 3"]
    # Messages without a Cc must still match on To.
    assert len(search(indexed, "--to", "bob@example.org")[1]) == 5
    assert [h["subject"] for h in search(indexed, "--until", "2020-12-31")[1]] == ["message 4"]
    assert len(search(indexed, "--since", "2021-01-01")[1]) == 4
    assert len(search(indexed, "--source", "topics")[1]) == 1


def test_search_punctuation_does_not_break_query(indexed):
    p, _ = search(indexed, "--query", "ann@corp.example")
    assert p.returncode == 0


def test_extract_writes_text_files(indexed, tmp_path):
    out = tmp_path / "out"
    run("search_mbox.py", "--db", indexed, "--query", "invoice", "--extract", out)
    files = list(out.iterdir())
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "Subject: message 3" in text and "invoice attached" in text
