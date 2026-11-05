# telemetry

Receives Honcho's CloudEvents, writes them to a UTC-day JSONL file, and optionally forwards each batch to a viewer hub. The viewer is not modified. The disk file is the record; the forward is best-effort.

It runs as the `telemetry` service of `deploy/compose.yaml` (container `honcho-tools-telemetry-1`).

Tested with Python 3.12.16.

## Environment

The service reads these names from `deploy/.env` (`deploy/.env.example` lists them) and the token from Honcho's `.env`. `env.example` here lists the same names. Empty values take the defaults below.

| Name | Default | Meaning |
|---|---|---|
| `TELEMETRY_TOKEN` | required | Bearer token. The process exits if it is empty. |
| `ALLOWED_SOURCES` | empty (any) | Comma-separated IPs and CIDRs. Anyone else gets 403. |
| `MIN_FREE_GB` | 2 | Refuse with 503 when free space on the data directory is below this many gibibytes (`1024³`). |
| `GZIP_AFTER_DAYS` | 1 | Gzip a day file once it is older than this. |
| `RETENTION_DAYS` | 90 | Delete a day file (plain or gzip) once it is older than this. |
| `VIEWER_URL` | empty (off) | Where to POST each accepted batch. Any path the hub listens on. |
| `VIEWER_KEY` | empty | Sent as `X-Telemetry-Key` when set. |
| `PORT` | 8765 | Listen port inside the container. The healthcheck uses the same variable. |
| `DATA_DIR` | `/data` | Directory inside the container. Compose sets it to `/data` and mounts `TELEMETRY_DIR` there. |
| `MAINTENANCE_LOG` | `/logs/maintenance.log` | The maintenance log. Compose sets it and mounts `LOGS_DIR` at `/logs`. |
| `BIND_HOST` | `0.0.0.0` | Listen address inside the container. |
| `MAX_BODY_MB` | 64 | Largest request body, in mebibytes (above 0, at most 1024). See "Request size" below before lowering it. |

`TELEMETRY_DIR` is the host directory (see `deploy/.env`), by default `telemetry/` beside `src/`.

Compose passes Honcho's whole `.env`. Started as a script, the receiver first replaces itself with a process that holds
only the names in the table, plus `PATH`, `HOME`, `HOSTNAME`, locale, `TZ`, `TMPDIR`, `PYTHON*`, `SSL_CERT_*` and the
proxy variables (`runtime_env()` in `receiver.py`); the database password and provider keys are not in its
environment. A name added to the table must be added there too.

## HTTP

`POST /events` accepts one CloudEvent or a JSON array of at most 1000. Every item must be an object whose `id`, `type`, `source` and `specversion` are non-empty strings. `Authorization: Bearer <TELEMETRY_TOKEN>` is required and is checked before anything else of the request is read.

| Status | When |
|---|---|
| 200 `{"accepted": N}` | The batch is on disk. |
| 401 | Missing or wrong bearer token. |
| 403 | `ALLOWED_SOURCES` is set and the client IP is not in it. |
| 400 | Not JSON (including bad UTF-8, nesting too deep for the parser, an integer past Python's digit limit), empty, more than 1000 items, or any item without the four string attributes. Also a `Content-Length` that is not plain digits, or given twice. |
| 411 | No `Content-Length`, or a `Transfer-Encoding` header (chunked bodies are not parsed). |
| 413 | Body larger than `MAX_BODY_MB`. The body is not read. |
| 503 | Free space is under `MIN_FREE_GB`, the write failed, or `{"error": "busy"}`: two bodies were already being read and no slot freed within 30 seconds. |
| `GET /health` | Today's file size, free space, and forward counters. `"status": "low_space"` (still 200) when free space is under the minimum: only `POST /events` refuses, so a full disk does not restart the container. |

### Request size

At most two request bodies are read and parsed at the same time (Honcho's api and deriver each send one batch at a
time); a third post waits up to 30 seconds for a slot and then gets 503, which Honcho sends again later. Memory held by
bodies therefore stays near two times `MAX_BODY_MB` however many connections are open. Every refusal answers with a
status and closes the connection; no input ends a handler without an answer.

Honcho puts a refused batch back at the head of its buffer and sends it again at the next flush. A 413 for a batch
larger than the cap is therefore not one lost post: that batch is refused again on every flush and nothing behind it
is delivered. Keep `MAX_BODY_MB` above Honcho's largest batch: up to `TELEMETRY_BATCH_SIZE` events (100 by default),
which can be large with `TELEMETRY_TRACE_PAYLOADS_ENABLED=true` (prompts and answers inside the events). To lower the
cap, lower `TELEMETRY_BATCH_SIZE` on Honcho's side first. A 413 is written to the container log by the HTTP server.

Each accepted event becomes one line:

```json
{"received_at": "2026-10-30T00:00:00+00:00", "event": {}}
```

`received_at` is UTC. The file is `events-YYYY-MM-DD.jsonl` for that UTC day. The batch is fsynced before the response. A batch shares one `received_at`.

## Rotation

A daily thread appends its actions to `maintenance.log` in the logs folder (`MAINTENANCE_LOG`). Today's UTC file is never gzipped or deleted. For every other `events-YYYY-MM-DD.jsonl` (and `.gz`), age is the greater of:

- days since the file's mtime
- UTC midnights between the date in the name and today

Age greater than `RETENTION_DAYS` deletes the file. Otherwise age greater than `GZIP_AFTER_DAYS` gzips a plain file and removes it only after the gzip reads back identical. With the defaults, yesterday stays plain unless its mtime is already more than a day old; the day before that is gzipped. The gzip keeps the original mtime, so the retention clock does not restart. Compression holds the write lock, so a post waits until an old file has been compressed.

## Forward

After the fsync, the same events are queued as a JSON array and POSTed to `VIEWER_URL` with `Content-Type: application/json` and, when `VIEWER_KEY` is set, `X-Telemetry-Key`. The timeout is 2 seconds. The HTTP response does not wait on it. A failure or a full queue increments `forward_failures` on `/health`. Nothing is retried. The hub must accept one CloudEvent or a JSON array of them.

## Honcho compose

Set these two variables on Honcho's `api` and `deriver` services (in Honcho's compose file both usually inherit them from one shared block):

```yaml
    TELEMETRY_ENDPOINT: "http://telemetry:8765/events"
    TELEMETRY_HEADERS: '{"Authorization": "Bearer ${TELEMETRY_TOKEN}"}'
```

Add `TELEMETRY_TOKEN` to that project's `.env`. Compose interpolates it from the project `.env` before the container starts. Do not commit the value, and do not write it inline in the compose file. Keep the token free of `"`, `$` and `}`.

Leave `TELEMETRY_ENABLED` as it is. The telemetry service joins the external network `honcho_default` (override with `HONCHO_NETWORK` in the stack compose), which is the Honcho project's default network, so the name `telemetry` resolves from `api` and `deriver`. The Honcho compose has no `networks:` key and does not need one.

## Order of operations

1. Create the directories named by `TELEMETRY_DIR` and `LOGS_DIR` (by default `telemetry/` and `logs/` beside `src/`).
2. Set `TELEMETRY_TOKEN` in Honcho's `.env`. Set `VIEWER_URL` and `VIEWER_KEY` in `deploy/.env` when a live viewer should receive a copy.
3. Start the service (`docker compose up -d telemetry` in `deploy/`). Confirm `/health` (port 8765 inside the container):

   ```
   docker compose exec telemetry python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/health').read().decode())"
   ```

4. Apply the Honcho compose diff above and set the same `TELEMETRY_TOKEN` in that project's `.env`.
5. When the dream queue is empty, restart `api` and `deriver`.
6. Confirm new lines in `events-YYYY-MM-DD.jsonl` (UTC day) under `TELEMETRY_DIR`.
7. If another receiver took Honcho's events before, keep it running until a day of events has arrived here, then stop it.

## Tests

`python -m pytest tests/test_receiver.py`

Set `TELEMETRY_SAMPLE` to a jsonl of captured CloudEvents (one object per line, raw or wrapped). The test posts the first 200 lines and does not copy them into the tree.
