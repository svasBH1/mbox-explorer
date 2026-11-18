"""Shared fixtures: synthetic mail only, generated in code. No real mail is used."""

import os
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SEPARATORS = {
    "gmail": "From 1700000000000000{n:03d}@xxx Mon Mar 03 10:00:00 +0000 2025",
    "thunderbird": "From - Mon Mar  3 10:00:00 2025",
    "generic": "From sender@example.com Mon Mar  3 10:00:00 2025",
}


def make_message(n, sender="alice@example.com", subject=None, body="hello world",
                 date="Mon, 03 Mar 2025 10:00:00 +0000", to="bob@example.org", cc=None,
                 attachment=None):
    m = EmailMessage()
    m["From"] = f"Sender {n} <{sender}>"
    m["To"] = to
    if cc:
        m["Cc"] = cc
    m["Subject"] = subject or f"message {n}"
    m["Date"] = date
    m["Message-ID"] = f"<msg{n}@example.com>"
    m.set_content(body)
    if attachment:
        data, maintype, subtype, name = attachment
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m.as_bytes().replace(b"\r\n", b"\n")


def write_mbox(path, messages, flavour="gmail"):
    """Write (bytes) messages as an mbox; returns the byte offset of each separator."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    offsets = []
    with open(path, "wb") as f:
        for i, raw in enumerate(messages):
            offsets.append(f.tell())
            f.write(SEPARATORS[flavour].format(n=i).encode() + b"\n")
            f.write(raw if raw.endswith(b"\n") else raw + b"\n")
            f.write(b"\n")
    return offsets


def run(script, *args, check=True):
    """Run one of the CLI scripts with the current interpreter."""
    p = subprocess.run([sys.executable, str(ROOT / script), *map(str, args)],
                       capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if check and p.returncode != 0:
        raise AssertionError(f"{script} {args} exited {p.returncode}\n{p.stdout}\n{p.stderr}")
    return p


@pytest.fixture
def archive(tmp_path):
    """Two sources: a Takeout-style tree with a gmail mbox and a plain mbox."""
    a = tmp_path / "Takeout" / "Mail" / "All mail.mbox"
    write_mbox(a, [
        make_message(1, body="quarterly budget review", sender="ann@corp.example"),
        make_message(2, body="From here the line starts with From but is body text\nzebra",
                     sender="noreply@alerts.example"),
        make_message(3, body="invoice attached", sender="billing@vendor.example", cc="carol@corp.example",
                     attachment=(b"col1,col2\n1,2\n", "text", "csv", "report.csv")),
        make_message(4, body="tags <b>bold</b> & ampersand", sender="noreply@alerts.example",
                     date="Wed, 01 Jan 2020 09:00:00 +0000"),
    ])
    b = tmp_path / "Takeout" / "Groups" / "topics.mbox"
    write_mbox(b, [make_message(5, body="group budget thread", sender="dan@corp.example")])
    return tmp_path / "Takeout"


@pytest.fixture
def indexed(archive, tmp_path):
    db = tmp_path / "archive.sqlite"
    run("index_mbox.py", archive, "--db", db)
    return db
