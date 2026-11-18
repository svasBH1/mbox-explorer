import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import mail_explorer
from conftest import run


@pytest.fixture
def server(indexed):
    store = mail_explorer.Store(str(indexed))
    handler = type("H", (mail_explorer.Handler,), {"store": store})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base
    srv.shutdown()
    srv.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def api(base, path):
    status, _, body = get(base + path)
    return status, json.loads(body)


def test_app_page_loads(server):
    status, ctype, body = get(server + "/")
    assert status == 200 and "text/html" in ctype
    assert b"IntersectionObserver" in body and b"themeBtn" in body and b'name="goto"' in body


def test_search_batches_and_count(server):
    _, first = api(server, "/api/search?limit=2&offset=0")
    _, rest = api(server, "/api/search?limit=2&offset=2")
    _, last = api(server, "/api/search?limit=2&offset=4")
    ids = [r["id"] for r in first["rows"] + rest["rows"] + last["rows"]]
    assert len(ids) == len(set(ids)) == 5
    assert first["more"] and rest["more"] and not last["more"]
    assert api(server, "/api/count")[1] == {"total": 5}


def test_exclude_senders_and_sender_strip(server):
    assert api(server, "/api/count?xfrom=alerts.example")[1]["total"] == 3
    _, s = api(server, "/api/senders")
    assert s["by"] == "domain"
    assert {"domain": "alerts.example", "count": 2} in s["senders"]
    _, s = api(server, "/api/senders?from=alerts.example")
    assert s["by"] == "address" and s["senders"] == [{"domain": "noreply@alerts.example", "count": 2}]


def test_sort_orders(server):
    newest = [r["id"] for r in api(server, "/api/search?limit=10")[1]["rows"]]
    oldest = [r["id"] for r in api(server, "/api/search?limit=10&sort=old")[1]["rows"]]
    assert oldest[0] == newest[-1]


def test_bad_query_is_reported_not_crashed(server):
    status, d = api(server, "/api/search?q=AND")
    assert status == 400 and "query not understood" in d["error"]


def test_message_pane_escapes_content(server):
    _, d = api(server, "/api/search?q=ampersand")
    mid = d["rows"][0]["id"]
    _, _, body = get(f"{server}/msg?pane=1&id={mid}")
    assert b"&lt;b&gt;bold&lt;/b&gt;" in body and b"<b>bold" not in body
    assert not body.startswith(b"<!doctype")
    _, _, full = get(f"{server}/msg?id={mid}")
    assert full.startswith(b"<!doctype")


def test_snippet_is_escaped_with_only_highlights_as_markup():
    s = mail_explorer.snippet_html("a <script>x</script> & \x02hit\x03")
    assert s == "a &lt;script&gt;x&lt;/script&gt; &amp; <b>hit</b>"


def test_attachment_and_raw_download(server, indexed):
    _, d = api(server, "/api/search?q=invoice")
    mid = d["rows"][0]["id"]
    with sqlite3.connect(indexed) as c:
        part = c.execute("SELECT part_index FROM attachments WHERE message_id=?", (mid,)).fetchone()[0]
    status, ctype, body = get(f"{server}/attach?id={mid}&part={part}")
    assert status == 200 and ctype.startswith("text/csv") and body == b"col1,col2\n1,2\n"
    status, _, raw = get(f"{server}/raw?id={mid}")
    assert status == 200 and raw.startswith(b"From ")


def test_unknown_message_and_path(server):
    _, _, body = get(server + "/msg?pane=1&id=999")
    assert b"No such message" in body
    assert get(server + "/nope")[0] == 404


def test_startup_refuses_missing_source(indexed, tmp_path):
    with sqlite3.connect(indexed) as c:
        c.execute("UPDATE sources SET path = ? WHERE id = 1", (str(tmp_path / "gone.mbox"),))
    p = run("mail_explorer.py", "--db", indexed, check=False)
    assert p.returncode != 0 and "Mbox not found" in p.stderr


def test_startup_asks_old_database_to_migrate(tmp_path):
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as c:
        c.executescript("CREATE TABLE messages (id INTEGER PRIMARY KEY);"
                        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
    p = run("mail_explorer.py", "--db", db, check=False)
    assert p.returncode != 0 and "--migrate" in p.stderr
