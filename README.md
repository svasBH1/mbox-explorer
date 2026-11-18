# mbox-explorer

Index an mbox archive into SQLite and search it, from the command line or a local
web page. Built for Google Takeout mail exports, and it also reads a Thunderbird
profile.

Python 3.8 or newer. Standard library only, nothing to install. Source files are
opened read-only and never modified.

## The three parts

| Script | Does |
| --- | --- |
| `index_mbox.py` | Walks a directory, indexes every mbox it finds into one database |
| `search_mbox.py` | Searches that database from the command line |
| `mail_explorer.py` | Serves the same database as a local web page |
| `mboxlib.py` | Shared separator handling and discovery. Not run directly |

The database stores headers, a full-text index and a byte offset per message. It
does not store the mail. Keep it beside the files it was built from or the viewer
cannot read a message back.

## Index

```
python3 index_mbox.py "/path/to/Takeout" --db archive.sqlite
```

Point it at a Takeout directory, a Thunderbird profile directory, a folder of
`.mbox` files, or a single file. Every mbox found becomes a source, and one
database holds them all.

| Flag | Effect |
| --- | --- |
| `--resume` | Continue after a stop, per source |
| `--limit N` | Stop each source at byte N. Use for a dry run |
| `--skip-tail` | Drop everything after the last separator. For a cut-off download |
| `--migrate` | Upgrade a single-source database written by an older version |

Expect roughly 30 MB/s against a local disk, so a 30 GB mailbox takes about
20 minutes. A network share is far slower. Progress lines carry counts only and
print nothing from a message.

`--skip-tail` is for a download that stopped mid-message. It indexes up to the
last separator and reports how much it dropped. Leave it off for a complete file,
or the newest message in every mailbox is discarded.

## Search from the command line

```
python3 search_mbox.py --db archive.sqlite --query 'invoice NEAR overdue'
python3 search_mbox.py --db archive.sqlite --query budget --since 2024-01-01
python3 search_mbox.py --db archive.sqlite --from example.com --tsv
python3 search_mbox.py --db archive.sqlite --query '"project plan"' --extract out/
python3 search_mbox.py --db archive.sqlite --stats
```

`--query` runs against the full-text index over subject, participants, body and
labels. `AND`, `OR`, `NOT`, `NEAR` and `"quoted phrases"` work as in SQLite FTS5.
Bare words are quoted first, so an address or a ticket number with punctuation in
it cannot break the query.

Filters combine: `--subject`, `--from`, `--to`, `--source`, `--since`, `--until`.
Output is a plain list, or `--tsv`, or `--json`. `--extract DIR` writes each match
as a text file with its headers.

Every run prints the number of messages in the index alongside the number matched.
A run reporting zero of zero is a broken run, not a clean sweep.

## Search in a browser

```
python3 mail_explorer.py --db archive.sqlite
```

Then open `http://127.0.0.1:8770`. It binds loopback, serves read-only and fetches
nothing from the internet. Message bodies render as escaped text, never as live
HTML, so a message cannot run anything in the browser. Attachments download on
demand and are read straight out of the mbox.

With more than one source indexed, the search form gains a source selector and the
results gain a source column.

## Separator formats

An mbox marks each message with a `From ` line. Three shapes appear in practice
and all three are handled:

| Flavour | Example |
| --- | --- |
| `gmail` | `From 1700000000000000001@xxx Tue Mar 11 14:18:19 +0000 2025` |
| `thunderbird` | `From - Wed Aug 28 13:00:00 2026` |
| `generic` | `From user@host Mon Jan  1 00:00:00 2024` |

The match covers the whole date shape rather than the first five characters,
because a message body can contain a line beginning with `From `.

## Upgrading an older database

Databases written before multi-source support have no `sources` table.

```
python3 index_mbox.py --db old.sqlite --migrate
```

That backfills a single source row and rebuilds the offset index. It does not
reread the mbox, so it takes seconds rather than a reindex.

## Notes

- A Takeout `Groups` export holds one `topics.mbox` per Google Group. The indexer
  picks these up and files each as its own source.
- A Thunderbird profile nests folders under `.sbd` directories. Discovery walks
  them, so subfolders are indexed rather than only the top-level ones.
- Gmail labels come from the `X-Gmail-Labels` header. Thunderbird mail has none,
  so the label filter does nothing on those sources.
- Calendar and Drive data in a Takeout are not mail and are outside what this
  reads. Unzip them separately.
