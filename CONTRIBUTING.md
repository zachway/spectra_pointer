# Contributing to The Spectra Pointer

Bug reports and pull requests are welcome via
[GitHub Issues](https://github.com/zachway/spectra_pointer/issues) and
[Pull Requests](https://github.com/zachway/spectra_pointer/pulls).

## Reporting a bug

Open an issue with:

- What you ran (command, environment variables) and what happened
- What you expected to happen
- Python version and, if relevant, PostgreSQL/q3c version

## Adding support for a new archive

This is the most common and most welcome kind of contribution. Each
archive lives in its own file under `sync/archives/` and implements a
`fetch()` generator that the shared runner and matcher in `sync/` drive
to convergence. Steps:

1. Pick an existing archive under `sync/archives/` that talks to a
   similar kind of service (TAP/VO, a REST API, an HTML form) as a
   template.
2. Implement `fetch()` to yield raw records for that archive.
3. Register the archive so `sync/main.py --only <archive_code>` can run
   it in isolation.
4. Add tests under `tests/` covering the parsing/cross-match behavior
   specific to the new archive.
5. Update the archive count and, if relevant, the architecture notes in
   `README.md`.

## Development setup

Follow the "Installation" section of `README.md` to get a local
PostgreSQL database with the q3c extension and the schema loaded.

## Running tests

Tests need a Postgres test database (see "Testing" in `README.md`):

```bash
createdb spectra_test
psql spectra_test -f db/schema.sql
DATABASE_URL=postgresql:///spectra_test pytest tests/
```

Please add or update tests for any behavior change, and make sure the
full suite passes before opening a pull request.

## Pull requests

- Keep PRs focused — one archive, one bug fix, or one feature per PR.
- Include a short description of what changed and why.
- CI (`.github/workflows/tests.yml`) must pass.

## License

By contributing, you agree that your contributions will be licensed
under this project's [MIT License](LICENSE).
