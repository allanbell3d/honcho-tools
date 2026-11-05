# cli-secrets/

The stack's private files (`SECRETS_DIR`):

- `tokens.env`: `BRIDGE_TOKEN`, `TELEMETRY_TOKEN` and `TELEMETRY_HEADERS`, written by `setup.sh` (random, or the values
  Honcho's `.env` already has). Every service of this stack and Honcho's `api` and `deriver` load it. Without
  `setup.sh`, create it by hand with those three lines.

The bridge's CLI logins, only needed for the Codex or Claude Code backends:

- `codex/auth.json`: copy it from a machine where `codex login` ran.
- `claude/`: run `docker compose -f deploy/compose.yaml exec bridge claude login` once; it writes here.

Both folders must be writable by the container's user (uid 1000). These are subscription logins:
keep them private. Everything here except this file and the two empty folders is ignored by git.
