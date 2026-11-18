#!/usr/bin/env python3
"""Shared mbox handling: separator detection, source discovery, message iteration.

Three separator styles are common in practice:

    gmail        From 1700000000000000001@xxx Tue Mar 11 09:00:00 +0000 2025
    thunderbird  From - Fri Aug 28 13:00:00 2026
    generic      From user@host Mon Jan  1 00:00:00 2024

SEP matches all three. The per-flavour patterns exist so a caller can report
which one a file uses. Message bodies can contain lines starting with "From ",
so the separator is matched on the whole date shape rather than a prefix.

Files are always opened read-only and never modified.
"""

import os
import re

__version__ = "1.0.0"

FLAVOURS = {
    "gmail": re.compile(
        rb"^From \S+ \w{3} \w{3} +\d{1,2} \d{2}:\d{2}:\d{2} [-+]\d{4} \d{4}\r?$", re.M),
    "thunderbird": re.compile(
        rb"^From - \w{3} \w{3} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}\r?$", re.M),
    "generic": re.compile(
        rb"^From \S+ \w{3} \w{3} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}\r?$", re.M),
}

SEP = re.compile(
    rb"^From (?:-|\S+) \w{3} \w{3} +\d{1,2} \d{2}:\d{2}:\d{2} (?:[-+]\d{4} )?\d{4}\r?$", re.M)

CHUNK = 8 * 1024 * 1024
SAMPLE = 8 * 1024 * 1024

# Thunderbird stores folders as extensionless files beside a .msf index.
TBIRD_NAMES = {"INBOX", "Sent", "Sent Mail", "All Mail", "Drafts", "Trash",
               "Archive", "Important", "Starred", "Spam"}


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"


def detect_flavour(path):
    """Return (flavour, separators_seen) from the head of the file."""
    with open(path, "rb") as fh:
        head = fh.read(SAMPLE)
    counts = {name: len(rx.findall(head)) for name, rx in FLAVOURS.items()}
    if not any(counts.values()):
        return None, 0
    # gmail also satisfies generic, so prefer the most specific non-zero match
    for name in ("gmail", "thunderbird", "generic"):
        if counts[name]:
            return name, counts[name]
    return None, 0


def looks_like_mbox(path):
    try:
        if os.path.getsize(path) == 0:
            return False
        with open(path, "rb") as fh:
            return bool(SEP.search(fh.read(1024 * 1024)))
    except OSError:
        return False


def discover(root):
    """Return [(label, path)] for every mbox under root.

    Handles a Google Takeout tree, a Thunderbird profile directory, a plain
    directory of .mbox files, or a single file.
    """
    root = os.path.abspath(root)
    if os.path.isfile(root):
        return [(os.path.basename(root), root)]

    found = []
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for n in names:
            p = os.path.join(dirpath, n)
            ext = os.path.splitext(n)[1].lower()
            if ext in (".msf", ".sqlite", ".zip", ".json", ".html", ".csv", ".ics", ".dat"):
                continue
            if ext == ".mbox" or n in TBIRD_NAMES or ext == "":
                if looks_like_mbox(p):
                    found.append((os.path.relpath(p, root), p))
    return sorted(found)


def find_last_boundary(path, tail_bytes=64 * 1024 * 1024):
    """Byte offset of the final separator, scanning backwards from EOF.

    Everything after it is a partial message, which is how a truncated download
    presents. Returns (size, offset_or_None, separators_in_window).
    """
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        window = min(tail_bytes, size)
        while True:
            start = max(0, size - window)
            fh.seek(start)
            chunk = fh.read(size - start)
            hits = list(SEP.finditer(chunk))
            if hits:
                return size, start + hits[-1].start(), len(hits)
            if start == 0:
                break
            window = min(window * 4, size)
    return size, None, 0


def iter_messages(path, end_offset, start_offset=0):
    """Yield (offset, raw_bytes) per message, reading in CHUNK-sized pieces.

    If end_offset falls inside a message (not at a separator or end of file), that
    partial message is not yielded, so a later resume starts at a whole message.

    raw_bytes includes the separator line.
    """
    with open(path, "rb") as fh:
        fh.seek(start_offset)
        buf = b""
        buf_start = start_offset
        pending_start = None
        pos = start_offset

        while pos < end_offset:
            chunk = fh.read(min(CHUNK, end_offset - pos))
            if not chunk:
                break
            pos += len(chunk)
            buf += chunk
            while True:
                m = SEP.search(buf, 1 if pending_start is not None else 0)
                if not m:
                    break
                abs_at = buf_start + m.start()
                if pending_start is None:
                    pending_start = abs_at
                    buf = buf[m.start():]
                    buf_start = abs_at
                    continue
                yield pending_start, buf[:m.start()]
                buf = buf[m.start():]
                buf_start = abs_at
                pending_start = abs_at

        if pending_start is not None and buf:
            fh.seek(end_offset)
            ahead = fh.read(512)
            if not ahead or SEP.match(ahead):
                yield pending_start, buf
            else:
                # end_offset fell inside a message, possibly inside its separator line: yield
                # only the whole message before that separator, if there is one.
                m = SEP.search(buf + ahead, 1)
                if m and m.start() < len(buf):
                    yield pending_start, buf[:m.start()]
