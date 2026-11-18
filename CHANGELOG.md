# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0]

First public release.

### Indexing
- Indexes Google Takeout folders, Thunderbird profiles, folders of `.mbox` files and single
  files; several sources in one database
- Recognises `gmail`, `thunderbird` and `generic` separators by their full date shape, so body
  lines starting with "From " never split a message
- `--resume`, `--limit`, `--skip-tail` for interrupted runs, trials and cut-off downloads;
  `--limit` stops at the last whole message, so `--resume` continues cleanly
- `--migrate` upgrades a pre-1.0 single-source database without reindexing
- A Date header without a time zone is read as UTC

### Viewer
- Results load as the list scrolls; a reading pane shows the selected message beside the list
- Keyboard: `↑`/`↓` or `j`/`k` move through results, `/` focuses search
- Top senders by domain, or by address once a sender filter is set; one-click exclude
- Go to a message by id, from the header box or `/?id=`
- Sort by newest, oldest or best match; loading bar; light and dark mode
- Search snippets and message bodies are escaped; nothing from a message runs as HTML
- Answers only requests addressed to `127.0.0.1` or `localhost` (DNS-rebinding protection)
- **raw** downloads a plain `.eml`, without the mbox separator line

### Command line
- `mbox-search` with full-text query (including `NEAR(a b)`), sender, recipient, source and date filters; plain, TSV
  or JSON output; `--extract` to text files; `--stats`
- Installable with `pip install .`: `mbox-index`, `mbox-search`, `mbox-explorer`

[1.0.0]: https://github.com/svasBH1/mbox-explorer/releases/tag/v1.0.0
