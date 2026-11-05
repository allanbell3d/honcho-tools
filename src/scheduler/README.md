# scheduler

Permanent loop. The container is `restart: unless-stopped`. Each poll re-reads `settings.json` (`CONTROL_FILE`, or `/config/settings.json`). The container mounts the config folder (`CONFIG_DIR`) read-write at `/config`, because peer discovery writes `settings.json`. A file bind would keep the inode from before a rename.

`mode: stock` sleeps for `scheduler.poll_seconds` and does not queue. It logs `sleeping` once per stay in stock. `mode: scheduler` queues dreams.

Eligibility is the keys of `pairs`, plus each listed peer with itself. Both peers must be listed and not `enabled: false`. `scheduler.skip` removes a pair (`observer/observed`). An unlisted peer is not eligible here. The hook treats an unlisted peer as not disabled. Discovery runs before scheduling: it appends unknown peers as `enabled: false` and every observer/observed notebook missing from `pairs` as `{}` in `scheduler.skip` (Off), so nothing new is queued on the poll that finds it.

The `schedule_dream` body is `observer`, `observed`, `dream_type` and `scheduled_by: honcho-scheduler`. The request schema has no `trigger_reason` field. The route writes `trigger_reason: manual` on the queue payload and drops `scheduled_by`. The hook treats `manual` as an API-scheduled dream, and prefers `scheduled_by` when the payload has that attribute. `dream_type` is passed through. The staging API accepts `omni` and `card_refresh`.

Peer names come from `collections` (`observer` union `observed`) for `HONCHO_WORKSPACE`. A name with a slash or whitespace, or a secret-like name, is not added and is not logged. The write is a temp file in the same directory plus `os.replace`. Each previous file is copied to `backups/settings/settings.json.bak-<timestamp>` beside `settings.json` (the folder the dashboard keeps its copies in) and the newest 20 of these copies are kept. The first create has no backup.

`dream-nas.log` is `SCHEDULER_LOG`, by default `/logs/dream-nas.log` (the stack's `LOGS_DIR`, mounted at `/logs`). Its lines are (`=== start`, `config changed`, `scheduled`, `schedule ... ->`, `running`, `=== done`) and appends `mode=` at the end. `=== done` is logged on SIGTERM or SIGINT. The workspace is `HONCHO_WORKSPACE`. The API is `HONCHO_API` if set, otherwise `HONCHO_API_URL` plus `/v3`.

Without `settings.json` the scheduler starts, logs `settings.json unreadable; sleeping` and waits for the file. A file it refuses (bad JSON, a key-shaped string in the values it reads, or `peers`, `pairs` or `scheduler` that is not an object) keeps the previous settings and is logged once per saved version, with the reason. An invalid section it does not read (`providers`, `rerank`, `defaults`) does not stop it applying the sections it reads.

A pair is queued while it has explicit conclusions past Honcho's dream cursor. When the scheduler queues a pair again and its unread count and oldest unread conclusion are what they were the last time, the last dream changed nothing (it failed, or it was a card refresh): the pair then waits 15 minutes, then 30, doubling up to 24 hours, and any change releases it. The hold is in memory and starts empty after a restart. It needs neither the database nor the API to start: both are used only when `mode` is `scheduler` and `HONCHO_WORKSPACE` is set.

Started as a script, the scheduler first replaces itself with a process that holds only what it reads: `CONTROL_FILE`, `SCHEDULER_LOG`, `HONCHO_API`, `HONCHO_API_URL`, `HONCHO_WORKSPACE`, `HONCHO_API_TOKEN`, `POSTGRES_PASSWORD`, every `PG*` (libpq reads its own), and `PATH`, `HOME`, `HOSTNAME`, locale, `TZ`, `TMPDIR`, `PYTHON*`, `SSL_CERT_*` and the proxy variables (`runtime_env()` in `dream_nas.py`). The rest of Honcho's `.env`, which compose passes whole, is not in its environment.

Database reads use `PGHOST` (default `database`), `PGPORT`, `PGUSER`, `PGDATABASE` and `POSTGRES_PASSWORD`. A database error is logged as the driver type and SQLSTATE, not the driver message. `httpx` and `psycopg` are imported only when a connection or a post runs, so the pure functions import without them.

`Dockerfile` is `python:3.12-slim` with the packages of `requirements.txt` installed at build time: `httpx` and `psycopg[binary]`, and what they pull in. To move a package, change its line, run the tests, rebuild. Compose bind-mounts `dream_nas.py`, so a script edit does not need a rebuild.

Tested with Python 3.12.16, httpx 0.28.1 and psycopg[binary] 3.3.4.
