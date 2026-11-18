# Database schema

`mbox-index` writes one SQLite database. It holds headers, a full-text index and where each
message sits in its mbox file. It does not hold the mail itself.

## Tables

### `sources`

One row per mbox file indexed.

| Column | Meaning |
| --- | --- |
| `id` | Source id |
| `label` | Path relative to the folder that was indexed, e.g. `Mail/All mail.mbox` |
| `path` | Absolute path of the mbox file when it was indexed |
| `flavour` | Separator style: `gmail`, `thunderbird` or `generic` |
| `size` | File size in bytes when indexed |
| `good_bytes` | Bytes indexed; less than `size` only with `--skip-tail` |

### `messages`

One row per message.

| Column | Meaning |
| --- | --- |
| `id` | Message id, shown in the viewer and used by `/msg?id=` and `/?id=` |
| `source_id` | The `sources` row it came from |
| `offset` | Byte offset of the message's `From ` separator line in the mbox |
| `length` | Bytes from that offset to the next separator (separator line included) |
| `message_id` | `Message-ID` header |
| `thread_id` | `X-GM-THRID` header (Gmail thread), empty otherwise |
| `date_epoch` | `Date` header as Unix time; a date without a time zone is read as UTC |
| `date_display` | `Date` header as written |
| `from_name`, `from_addr` | Sender display name and address |
| `to_addrs`, `cc_addrs` | Comma-separated addresses |
| `subject` | `Subject` header |
| `labels` | `X-Gmail-Labels` header |
| `attach_count` | Number of attachments |
| `parse_error` | Set when the message could not be parsed; the row still records where it is |

### `attachments`

| Column | Meaning |
| --- | --- |
| `message_id` | The `messages.id` it belongs to |
| `part_index` | Position of the part when walking the message; used by `/attach?part=` |
| `filename` | Attachment file name |
| `content_type` | MIME type as declared in the message |
| `size_encoded` | Size of the encoded part in bytes |

### `messages_fts`

SQLite FTS5 table, `porter unicode61` tokenizer, with `rowid` equal to `messages.id`.
Columns: `subject`, `participants` (sender and recipient names and addresses), `body`
(plain text, or HTML with tags removed) and `labels`.

### `meta`

Key/value pairs. `root` is the folder or file most recently indexed.

## Reading a message by hand

A message is `length` bytes starting at `offset` in the file at `sources.path`:

```python
import sqlite3
db = sqlite3.connect("archive.sqlite")
path, offset, length = db.execute(
    "SELECT s.path, m.offset, m.length FROM messages m JOIN sources s ON s.id = m.source_id "
    "WHERE m.id = ?", (1234,)).fetchone()
with open(path, "rb") as f:
    f.seek(offset)
    raw = f.read(length)   # separator line, then the message
```

## Finding a message by file and byte offset

Other tools often record a message by file name and byte offset. To open such a message in the
viewer, look up its id and open `http://127.0.0.1:8770/?id=<id>`:

```
sqlite3 archive.sqlite "SELECT m.id FROM messages m JOIN sources s ON s.id = m.source_id WHERE s.path LIKE '%part01.mbox' AND m.offset = 1048576"
```
