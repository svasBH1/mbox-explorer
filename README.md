# mbox-explorer

Open, search and read mbox mail archives without importing them into a mail client.

mbox-explorer indexes one or many mbox files (Google Takeout exports, Thunderbird profiles,
any standard mbox) into a SQLite full-text index, then lets you search them from the command
line or browse them in a local web viewer. The mail files are never modified and never copied:
the index stores where each message starts, and the viewer reads it straight from the file.

It was built to answer questions like *"what did that former employee's mailbox say about X?"*
and to spot-check mail exports, without setting up a fake mail account to hold someone else's
archive.

- Standard library only. Python 3.9 or newer, nothing else to install
- Works on archives of any size: one message in memory at a time
- Read-only, loopback-only, no network access, no telemetry

## Install

```
pip install .
```

That adds three commands: `mbox-index`, `mbox-search` and `mbox-explorer`. Or skip installing
and run the scripts directly with `python3 index_mbox.py`, `python3 search_mbox.py` and
`python3 mail_explorer.py`.

## Quick start

```
mbox-index "/path/to/Takeout" --db archive.sqlite
mbox-explorer --db archive.sqlite
```

Open `http://127.0.0.1:8770`.

## Index

```
mbox-index SOURCE --db archive.sqlite
```

`SOURCE` is a Google Takeout folder, a Thunderbird profile folder, a folder of `.mbox` files or
a single file. Every mbox found becomes a *source*; one database holds them all. Indexing the
same source again adds nothing twice.

| Flag | Effect |
| --- | --- |
| `--resume` | Continue an interrupted run, per source |
| `--limit N` | Stop each source at byte N, for a quick trial run |
| `--skip-tail` | Drop everything after the last separator, for a download cut off mid-message |
| `--migrate` | Upgrade a database written by a pre-1.0 version (no reindex) |

Measured at 17 to 27 MB/s from a local disk, so a 30 GB mailbox takes 20 to 30 minutes. A
network share is much slower. Progress lines carry counts only and print nothing from a message.

Leave `--skip-tail` off for a complete file, or the last message in every mbox is dropped.

The database stores headers, a full-text index and each message's byte offset, not the mail.
Keep the mbox files where they were when indexed, or the viewer cannot read messages back.

## Browse

```
mbox-explorer --db archive.sqlite [--port 8770]
```

- **Search** subject, body, people and labels with SQLite FTS5 syntax: `AND`, `OR`, `NOT`,
  `NEAR`, `"exact phrases"`. Filter by sender, label, date range and source
- **Results load as you scroll**, newest first, oldest first or best match
- **Reading pane**: click a message and it opens beside the list, which keeps its place.
  `↑`/`↓` or `j`/`k` move through the list, `/` jumps to the search box
- **Top senders** for the current results, by domain, or by address once a sender filter is
  set. One click excludes a sender; exclusions stay in the URL
- **Go to id**: every message shows its id; type one in the header box, or link to `/?id=1234`
- **Attachments** download straight out of the mbox; **raw** downloads the message as `.eml`
- **Light and dark mode**, following the system until you choose

The viewer binds to `127.0.0.1` only, serves read-only and loads nothing from the internet.
Message bodies are shown as escaped text, never as live HTML, so a message cannot run code
in your browser.

## Search from the command line

```
mbox-search --db archive.sqlite --query 'invoice NEAR overdue'
mbox-search --db archive.sqlite --query budget --since 2024-01-01 --until 2024-12-31
mbox-search --db archive.sqlite --from example.com --tsv
mbox-search --db archive.sqlite --query '"project plan"' --extract out/
mbox-search --db archive.sqlite --stats
```

Filters combine: `--query`, `--subject`, `--from`, `--to` (To or Cc), `--source`, `--since`,
`--until`. Output is a plain list, `--tsv` or `--json`; `--extract DIR` writes each match as a
text file with its headers; `--stats` prints counts by year, source and sender.

Every run prints how many messages are in the index next to how many matched. A run reporting
zero of zero is a broken run, not a clean result.

Words are stemmed (`plan` also finds `planning`) because the index uses SQLite's `porter`
tokenizer. Use quotes for an exact phrase.

## Separator formats

Each message in an mbox starts with a `From ` line. All three common shapes are recognised:

| Flavour | Example |
| --- | --- |
| `gmail` | `From 1700000000000000001@xxx Tue Mar 11 14:18:19 +0000 2025` |
| `thunderbird` | `From - Wed Aug 28 13:00:00 2026` |
| `generic` | `From user@host Mon Jan  1 00:00:00 2024` |

The whole date shape is matched, not just the first five characters, so a body line that
starts with "From " never splits a message.

## Notes

- A Takeout `Groups` export holds one `topics.mbox` per Google Group; each becomes a source
- Thunderbird nests subfolders under `.sbd` directories; they are found and indexed
- Gmail labels come from the `X-Gmail-Labels` header. Other mail has none
- A Date header without a time zone (or with `-0000`) is read as UTC
- Calendar, Drive and Chat data in a Takeout are not mail and are not read
- Every loaded result row stays in the page. For hundreds of thousands of matches, narrow
  the search rather than scrolling to the end

## More

- [Database schema](docs/database-schema.md): tables, columns, and how to find a message by
  file and byte offset
- [Changelog](CHANGELOG.md)
- [Security](SECURITY.md)

## Development

```
pip install pytest
python -m pytest
```

The tests build synthetic mailboxes in code; no real mail is used or needed.

## License

MIT. See [LICENSE](LICENSE).
