# Restore a dump into a scratch database

The Honcho `database` service runs `pgvector/pgvector:pg15` (user `honcho`, database `honcho`, hostname `database` on the compose network). The backup service uses that same image, so `pg_restore` matches the server major. A custom-format dump from a different major will not restore cleanly.

These commands load one dump into a database named `honcho_scratch` on that same server. They leave the live `honcho` database as it is. They do not pass `--create` and they do not connect `pg_restore` to `honcho`.

Run them from `deploy/` (the directory that contains the stack's `compose.yaml`), in a shell on the Docker host or in your Docker UI's project terminal. The project must be on the same Docker network as `database`. `POSTGRES_PASSWORD` comes from the env file already named in `compose.yaml`. The commands export it as `PGPASSWORD` for the client tools and do not print it.

The commands use Honcho's service name `database` and the user `honcho`. If your database service uses other names (Honcho's own compose example uses `postgres`), replace `-h database -U honcho` accordingly.

Replace `YYYY-MM-DD` with the dump's date. The checksum file stores a relative name, so `sha256sum -c` runs from `/backups`.

## 1. Check the file

```bash
docker compose run --rm --no-deps -T --entrypoint bash backup -s <<'EOF'
set -euo pipefail
export PGPASSWORD="$POSTGRES_PASSWORD"
cd /backups
sha256sum -c "honcho-YYYY-MM-DD.dump.sha256"
set +o pipefail
pg_restore -l "honcho-YYYY-MM-DD.dump" | sed -n '1,30p'
EOF
```

`sha256sum -c` prints `OK` when the bytes match. A mismatch means do not restore that file.

## 2. Load it into honcho_scratch

This drops `honcho_scratch` if a previous check left it behind, creates it empty, and restores into it. One scratch database, replaced each time, so restore attempts do not accumulate.

```bash
docker compose run --rm --no-deps -T --entrypoint bash backup -s <<'EOF'
set -euo pipefail
export PGPASSWORD="$POSTGRES_PASSWORD"
stamp=YYYY-MM-DD
cd /backups
sha256sum -c "honcho-${stamp}.dump.sha256"
psql -h database -U honcho -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'honcho_scratch' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS honcho_scratch;
SQL
createdb -h database -U honcho honcho_scratch
pg_restore -h database -U honcho -d honcho_scratch --no-owner --no-acl --exit-on-error \
  "honcho-${stamp}.dump"
echo "restore exit=$?"
EOF
```

If `psql` cannot connect to database `postgres`, use `template1` in that one command. `createdb` and `pg_restore` still use `-h database -U honcho`.

`pg_restore` must exit 0. A non-zero exit means `honcho_scratch` is incomplete. Fix the cause and run this step again; it drops the scratch database first.

The same step as a compose override, if a file is easier to paste than a heredoc. Save it next to `compose.yaml` as `restore-override.yaml`, run it once, then remove it. Do not `up` the project with this override: that would replace the nightly loop until the override is gone.

```yaml
services:
  backup:
    entrypoint: ["bash", "-lc"]
    restart: "no"
    environment:
      STAMP: ${STAMP:?set STAMP=YYYY-MM-DD}
    command:
      - |
        set -euo pipefail
        export PGPASSWORD="$$POSTGRES_PASSWORD"
        stamp="$$STAMP"
        cd /backups
        sha256sum -c "honcho-$${stamp}.dump.sha256"
        psql -h database -U honcho -d postgres -v ON_ERROR_STOP=1 -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'honcho_scratch' AND pid <> pg_backend_pid();"
        psql -h database -U honcho -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS honcho_scratch;"
        createdb -h database -U honcho honcho_scratch
        pg_restore -h database -U honcho -d honcho_scratch --no-owner --no-acl --exit-on-error "honcho-$${stamp}.dump"
```

```bash
STAMP=YYYY-MM-DD docker compose -f compose.yaml -f restore-override.yaml run --rm --no-deps backup
```

`$$` in the override is a single `$` inside the container. `STAMP` is the only value interpolated from the shell.

## 3. Verify counts

Run the same exact-count query on the live database and on the scratch database. Each public table is one row: table name, then `count(*)`.

```bash
docker compose run --rm --no-deps -T --entrypoint bash backup -s <<'EOF'
set -euo pipefail
export PGPASSWORD="$POSTGRES_PASSWORD"
sql="SELECT c.relname,
       (xpath('//c/text()',
          query_to_xml(format('select count(*) as c from %I.%I', n.nspname, c.relname),
                       false, true, '')))[1]::text::bigint AS rows
     FROM pg_class c
     JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind = 'r' AND n.nspname = 'public'
     ORDER BY 1;"
echo "--- honcho_scratch"
psql -h database -U honcho -d honcho_scratch -v ON_ERROR_STOP=1 -c "$sql"
echo "--- honcho (live)"
psql -h database -U honcho -d honcho -v ON_ERROR_STOP=1 -c "$sql"
echo "--- sizes"
psql -h database -U honcho -d postgres -v ON_ERROR_STOP=1 -c \
  "SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database WHERE datname IN ('honcho', 'honcho_scratch') ORDER BY 1;"
EOF
```

What to look at:

- `honcho_scratch` returns one row per public table. Zero rows means the restore did not create tables.
- The scratch counts are the dump's snapshot. They match `honcho` when nothing was committed after `pg_dump` started.
- A live count higher than the scratch count is normal on a database that is in use.
- A scratch count higher than the live count for the same table, or a table present on only one side, is the signal to stop and inspect before dropping anything.

## 4. Drop the scratch database

Run this when the counts are understood. The next restore drops it as well. Leaving it in place is a second full copy of the database.

```bash
docker compose run --rm --no-deps -T --entrypoint bash backup -s <<'EOF'
set -euo pipefail
export PGPASSWORD="$POSTGRES_PASSWORD"
psql -h database -U honcho -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = 'honcho_scratch' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS honcho_scratch;
SQL
EOF
```
