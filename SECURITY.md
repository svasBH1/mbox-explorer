# Security

mbox-explorer is used on mail archives, which often hold sensitive data. It is designed so that
the archive stays where it is:

- Mail files are opened read-only and never modified
- The database holds headers and the plain-text body of every message, for searching. It is
  as sensitive as the mail: store and share it accordingly
- The viewer listens on `127.0.0.1` only, serves read-only and makes no outbound requests
- The viewer answers only requests addressed to `127.0.0.1` or `localhost`, so a web page
  cannot reach it through DNS rebinding. Any other program on the same computer can reach it
  while it runs; stop it when done
- Message content is always escaped before display; HTML mail is shown as text

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/svasBH1/mbox-explorer/security/advisories/new)
rather than in a public issue. Do not include real mail in a report; a synthetic message that
reproduces the problem is enough.
