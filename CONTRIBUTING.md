# Contributing

Issues and pull requests are welcome.

- Python 3.11 or newer: `pip install -r requirements-dev.txt`, then `python -m pytest` (offline, about 80 seconds).
  The rerank component's tests need a Honcho 3.5.0 tree and its dependencies; `src/rerank/README.md` has the steps.
- **Honcho's source stays stock.** The hook and the rerank component change Honcho only in memory, from the
  entrypoints. A change that needs an edit to Honcho's files belongs upstream or in `src/rerank/patches/`, and the
  start-up checks must name every Honcho symbol the code relies on.
- **`settings.json` is the only source of behaviour.** A component that needs a setting reads it from there and picks
  up changes without a restart. Never add a second file or an environment variable for the same setting.
- **No secret in any file.** `settings.json` names environment variables; keys live in Honcho's `.env`. Logs, events
  and run records never hold a key, a URL with credentials, or prompt and memory text.
- **Fail safe.** Reranking falls back to the vector order; a setting the hook cannot apply keeps the last good one;
  a write keeps a backup and reads the file back. Add a test when you touch one of these paths.
- **No real data.** Keep real hosts, paths, peer names, tokens and memory content out of code, tests, fixtures and
  screenshots. Use made-up data.
- The version lives only in `VERSION`. Every functional change bumps the patch and adds a line to `CHANGELOG.md`.
- **Docs follow the code.** A change that alters what a page, a file or a flag does edits the sentences it makes wrong
  (`README.md`, `docs/`, the component README) in the same change.

## Branches and releases

- `main` is the latest release; every release is tagged (`v0.1.0`, ...).
- Work happens on `dev` or a branch off it. At each release `dev` is merged into `main`, `VERSION` and `CHANGELOG.md`
  are updated, and the merge is tagged.
- Open pull requests against `dev`.
