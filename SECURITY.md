# Security

mbox-explorer is used on mail archives, which often hold sensitive data. It is designed so that
the archive stays where it is:

- Mail files are opened read-only and never modified; the database stores offsets, not mail
- The viewer listens on `127.0.0.1` only, serves read-only and makes no outbound requests
- Message content is always escaped before display; HTML mail is shown as text

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/svasBH1/mbox-explorer/security/advisories/new)
rather than in a public issue. Do not include real mail in a report; a synthetic message that
reproduces the problem is enough.
