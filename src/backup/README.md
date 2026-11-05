# backup

Nightly custom-format dump of the Honcho database, with a checksum and a fixed retention window.

The Honcho `database` service is `pgvector/pgvector:pg15` (user `honcho`, database `honcho`). This service uses that same image, so `pg_dump` and `pg_restore` match the server major. When that image tag changes major, change this one with it.

It runs as the `backup` service of `deploy/compose.yaml` (container `honcho-tools-backup-1`).

## Schedule

The image entrypoint runs a command other than `postgres` as given, so `bash /app/backup.sh` is the process. The container timezone is `TZ` from `.env` (default `UTC`). `BACKUP_AT` is a 24-hour `HH:MM` in that zone, default `03:30`. The process sleeps until the next mark, runs once, then sleeps until the next day. On start, if the mark has already passed and today's dump is missing, it dumps at once; otherwise it waits for the next mark.

`pg_dump -Fc --no-password` (host, port, user and database from `PGHOST`, `PGPORT`, `PGUSER` and `PGDATABASE` in `deploy/.env`, defaults `database`, `5432`, `honcho`, `honcho`) writes a partial file under `/backups`. The partial must not be empty and `pg_restore --list` must read its table of contents and find at least one table; only then is its sha256 written and `honcho-YYYY-MM-DD.dump` published, and the published pair is checked again with `sha256sum -c`. `--no-password` keeps a missing password from sitting on an interactive prompt. The sidecar is `honcho-YYYY-MM-DD.dump.sha256` (relative name, so `sha256sum -c` runs from `/backups`). A failed dump removes the partial and leaves the previous dated file in place.

A failure is tried again `BACKUP_ATTEMPTS` times in all (default 8), the first retry after `BACKUP_RETRY_SECONDS` (default 30) and each wait doubling up to 15 minutes, about 45 minutes in all: long enough for a database restart or a host update at the mark. A failure no retry can fix is not retried: wrong password, unknown user or database, `pg_hba.conf` refusal, a client older than the server, or a dump that lists no table (the wrong database). Each attempt and the final result are in the log. `POSTGRES_PASSWORD` from the Honcho env file is exported as `PGPASSWORD` and is not written to the log. The process runs as root so it can write the bind mount, and `umask 077` makes the dump and the log mode 600.

The date in the name is the container's calendar date at the start of the attempt.

## Retention

After every attempt (including a failed dump, once the password is set), the script keeps:

- the newest `KEEP_DAILY` dumps (default 7)
- the newest dump in each of the last `KEEP_WEEKLY` ISO weeks from today (default 4)

A dump that matches both rules is kept once. Every other `honcho-YYYY-MM-DD.dump` is deleted with its `.sha256`. A value that is not a positive integer is replaced with the default and logged. Files that do not match that name are left in place, so a one-off archive dump can sit in the same directory.

Worked example. Today is 2026-10-30 (Friday, ISO week 2026-W44) and a dump exists for every day. The seven newest dates are 2026-10-24 through 2026-10-30. The four week slots are W44 through W41. The newest dump of W44 is 2026-10-30 and of W43 is 2026-10-25, both already in the seven. The newest of W42 is 2026-10-18 and of W41 is 2026-10-11. Nine files stay. 2026-10-23 is in W43 and is deleted: 2026-10-25 is the file kept for that week, and 2026-10-23 is outside the seven.

Check the rules without a database:

```bash
bash backup.sh --retention-plan 2026-10-30 7 4 2026-10-11 2026-10-18 2026-10-23 2026-10-25 2026-10-30
```

## Log

`/logs/backup.log` (`BACKUP_LOG`; compose mounts `LOGS_DIR` at `/logs`), one line per action, also on stderr for the container log. At 5 MB the file is moved to `backup.log.1`, replacing the previous `.1`. Container json logs are capped at 10 MB times 3, the same cap as the database service.

The script writes only under `/backups` and that log. A symlink at `backup.log`, `.backup.lock`, or a dump name is removed or refused, not followed.

## Environment

| Name | Default | Meaning |
|---|---|---|
| `BACKUP_DIR` | `../backups` (beside `src/`) | Host directory mounted at `/backups`. Set in `deploy/.env`. |
| `BACKUP_LOG` | `/logs/backup.log` | The log file inside the container. Compose sets it; `LOGS_DIR` is mounted at `/logs`. |
| `TZ` | `UTC` | Zone in which `BACKUP_AT` is read. From `.env`. |
| `BACKUP_AT` | `03:30` | Hour and minute in that zone. |
| `KEEP_DAILY` | `7` | Newest dumps to keep. `BACKUP_KEEP_DAILY` is read when `KEEP_DAILY` is unset. |
| `KEEP_WEEKLY` | `4` | ISO weeks, counting the current one, for which the newest dump is kept. `BACKUP_KEEP_WEEKLY` is the fallback name. |
| `BACKUP_ATTEMPTS` | `8` | Attempts per run, 1 to 20. |
| `BACKUP_RETRY_SECONDS` | `30` | Wait before the first retry, 0 to 900; it doubles per retry up to 900. |
| `POSTGRES_PASSWORD` | required | Read from the Honcho env file and exported as `PGPASSWORD` (an explicit `PGPASSWORD` wins). Unset: the dump is tried without a password, which only a `trust` server accepts; a refusal is logged and not retried. |

`deploy/.env.example` lists the schedule names. The password stays in the Honcho project's env file.

Compose passes Honcho's whole `.env`. `backup.sh` starts again at once with only `PG*`, `POSTGRES_PASSWORD`, `BACKUP_*`, `KEEP_DAILY`, `KEEP_WEEKLY`, `TZ`, `PATH`, `HOME`, `HOSTNAME`, `TMPDIR` and locale (`BACKUP_ENV_SCRUBBED=1` marks the second start), so `pg_dump` never holds the other keys. A manual restore (`restore.md`) runs in a one-off container with the full environment.

## A dump now

`docker compose exec backup bash /app/backup.sh --now` runs one dump and the retention in the running container and exits 0 when the dump was written, 1 when it failed and 2 when the nightly run held the lock. Take one before a restore or an upgrade.

## Restore

`restore.md` has the manual commands to check a dump and load it into a scratch database; the live database is not touched.

## Order of operations

1. Create the host directories named by `BACKUP_DIR` and `LOGS_DIR` (by default `backups/` and `logs/` beside `src/`).
2. Set `HONCHO_ROOT` in `deploy/.env`. The service reads `${HONCHO_ROOT}/.env` for `POSTGRES_PASSWORD` and mounts `../src/backup/backup.sh`.
3. Confirm the external network is `honcho_default`, or set `HONCHO_NETWORK`. The dump connects to hostname `database` on that network.
4. Start the service (`docker compose up -d backup` in `deploy/`). The loop logs the zone and offset, the next sleep, then the written file.
5. After a run, `sha256sum -c honcho-YYYY-MM-DD.dump.sha256` from `/backups` prints `OK`.
6. To prove a dump restores, follow `restore.md` to load it into `honcho_scratch`, compare counts, then drop `honcho_scratch`.

Changing `BACKUP_AT` or a keep count is an environment change: recreate the container. The running loop does not re-read them.
