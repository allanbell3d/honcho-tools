"""backup.sh --now against fake PostgreSQL client tools.

The fakes (pg_dump, pg_restore, psql, createdb) are small bash scripts put first on PATH; they
record their arguments and answer as the test asks. backup.sh starts again with a reduced
environment, so the fakes read their settings from files beside them, not from variables. No
database is involved. The tests need a bash with GNU date (Linux, or Git Bash on Windows) and
skip without one.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "backup"
STAMP = "2026-11-01"
TOC = "215; 1259 16390 TABLE public messages honcho"


def _bash() -> str | None:
    found = shutil.which("bash")
    if not found:
        return None
    # The Windows launcher for WSL runs another system with other paths.
    lowered = found.lower().replace("\\", "/")
    if "/system32/" in lowered or "/windowsapps/" in lowered:
        return None
    try:
        probe = subprocess.run(
            [found, "-c", "date -d 2026-01-05 +%G-W%V"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found if probe.stdout.strip() == "2026-W02" else None


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash with GNU date")

# Settings are files in ../state: dump_fails, dump_error, toc, sessions, session_names.
FAKE = r"""#!/usr/bin/env bash
here=$(cd "$(dirname "$0")/.." && pwd)
knob() { cat "$here/state/$1" 2>/dev/null || printf '%s' "$2"; }
name=$(basename "$0")
{ printf '%s' "$name"; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\n'; } >>"$here/calls.log"
case "$name" in
  pg_dump)
    compgen -e | sort >"$here/state/dump_env"
    n=$(( $(cat "$here/state/dump.count" 2>/dev/null || echo 0) + 1 ))
    echo "$n" >"$here/state/dump.count"
    if (( n <= $(knob dump_fails 0) )); then
      knob dump_error "pg_dump: error: connection to server failed: Connection refused" >&2
      echo >&2
      exit 1
    fi
    while (( $# )); do
      if [[ "$1" == "-f" ]]; then out=$2; shift; fi
      shift
    done
    printf 'PGDMP fake archive\n' >"$out"
    ;;
  pg_restore)
    if [[ "$1" == "--list" ]]; then
      case "$(knob toc ok)" in
        bad) echo "pg_restore: error: input file does not appear to be a valid archive" >&2; exit 1 ;;
        empty) echo ";"; echo "; Archive created at 2026-11-01" ;;
        *) echo ";"; echo "__TOC__" ;;
      esac
    fi
    ;;
  psql)
    for a in "$@"; do
      if [[ "$a" == *"count(*)"* ]]; then
        echo "$(knob sessions 0) $(knob session_names '')"
      fi
    done
    ;;
esac
exit 0
""".replace("__TOC__", TOC)


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("pg_dump", "pg_restore", "psql", "createdb"):
        path = fake_bin / name
        path.write_bytes(FAKE.encode())
        path.chmod(0o755)
    (tmp_path / "state").mkdir()
    values = {
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        "BACKUP_ROOT": root.as_posix(),
        "BACKUP_LOG": (tmp_path / "logs" / "backup.log").as_posix(),
        "BACKUP_RETRY_SECONDS": "0",
        "POSTGRES_PASSWORD": "test-password",
        "TZ": "UTC",
    }
    for name in ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD", "BACKUP_ATTEMPTS", "BACKUP_ENV_SCRUBBED"):
        values[name] = ""
    return tmp_path, values


def _run(env, script: str, *args: str, knobs: dict | None = None, **extra: str) -> subprocess.CompletedProcess:
    tmp_path, values = env
    for name, value in (knobs or {}).items():
        (tmp_path / "state" / name).write_bytes(str(value).encode())
    merged = {**os.environ, **values, **extra}
    for name in [key for key, value in merged.items() if value == ""]:
        del merged[name]
    return subprocess.run(
        [BASH, (SRC / script).as_posix(), *args], env=merged, capture_output=True, text=True, timeout=60
    )


def _calls(env, tool: str | None = None) -> list[str]:
    path = env[0] / "calls.log"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [line for line in lines if tool is None or line.split(" ", 1)[0] == tool]


def _dumps(env) -> list[str]:
    return sorted(p.name for p in (env[0] / "backups").iterdir() if not p.name.startswith("."))


def _log(env) -> str:
    return (env[0] / "logs" / "backup.log").read_text(encoding="utf-8")


def _place_dump(env, stamp: str = STAMP, good: bool = True) -> None:
    root = env[0] / "backups"
    data = b"PGDMP fake archive\n"
    (root / f"honcho-{stamp}.dump").write_bytes(data)
    digest = hashlib.sha256(data if good else b"other").hexdigest()
    # Bytes, so Windows does not turn the line end into CRLF, which sha256sum would read as part of the name.
    (root / f"honcho-{stamp}.dump.sha256").write_bytes(f"{digest}  honcho-{stamp}.dump\n".encode())


def test_now_writes_a_checked_and_listed_dump(env):
    result = _run(env, "backup.sh", "--now")
    assert result.returncode == 0, result.stderr
    names = _dumps(env)
    assert len(names) == 2 and names[0].endswith(".dump") and names[1] == names[0] + ".sha256"
    root = env[0] / "backups"
    digest = hashlib.sha256((root / names[0]).read_bytes()).hexdigest()
    assert (root / names[1]).read_text(encoding="utf-8").split()[0] == digest
    assert len(_calls(env, "pg_dump")) == 1
    listing = _calls(env, "pg_restore")
    assert len(listing) == 1 and "[--list]" in listing[0] and ".partial]" in listing[0]
    assert "wrote honcho-" in _log(env) and "test-password" not in _log(env)
    assert not [p for p in root.iterdir() if p.name.startswith(".") and p.name != ".backup.lock"]


def test_pg_dump_gets_the_database_password_and_no_other_secret(env):
    result = _run(
        env, "backup.sh", "--now", PROVIDER_API_KEY="leak", AUTH_JWT_SECRET="leak", PGSSLMODE="disable"
    )
    assert result.returncode == 0, result.stderr
    names = set((env[0] / "state" / "dump_env").read_text(encoding="utf-8").split())
    assert {"PGPASSWORD", "POSTGRES_PASSWORD", "PGSSLMODE", "BACKUP_LOG", "TZ"} <= names
    assert not names & {"PROVIDER_API_KEY", "AUTH_JWT_SECRET"}


def test_transient_failures_are_retried_until_the_dump_works(env):
    result = _run(env, "backup.sh", "--now", knobs={"dump_fails": 2}, BACKUP_ATTEMPTS="4")
    assert result.returncode == 0, result.stderr
    assert len(_calls(env, "pg_dump")) == 3
    assert "dump attempt 1 of 4 failed; next in 0s" in _log(env)
    assert len(_dumps(env)) == 2


def test_retries_stop_at_the_attempt_limit(env):
    result = _run(env, "backup.sh", "--now", knobs={"dump_fails": 9}, BACKUP_ATTEMPTS="3")
    assert result.returncode == 1
    assert len(_calls(env, "pg_dump")) == 3
    assert _dumps(env) == []


@pytest.mark.parametrize(
    "message",
    [
        'pg_dump: error: FATAL:  password authentication failed for user "honcho"',
        'pg_dump: error: FATAL:  database "honcho" does not exist',
        'pg_dump: error: FATAL:  role "honcho" does not exist',
    ],
)
def test_a_permanent_failure_is_not_retried(env, message):
    result = _run(env, "backup.sh", "--now", knobs={"dump_fails": 9, "dump_error": message})
    assert result.returncode == 1
    assert len(_calls(env, "pg_dump")) == 1
    assert "not retried" in _log(env)


def test_an_unreadable_dump_is_never_published(env):
    previous = env[0] / "backups" / "honcho-2026-10-31.dump"
    previous.write_bytes(b"older")
    result = _run(env, "backup.sh", "--now", knobs={"toc": "bad"}, BACKUP_ATTEMPTS="2")
    assert result.returncode == 1
    assert len(_calls(env, "pg_dump")) == 2
    assert _dumps(env) == ["honcho-2026-10-31.dump"]
    assert not list((env[0] / "backups").glob(".*.partial"))
    assert "could not read the dump" in _log(env)


def test_a_dump_without_tables_is_refused_once(env):
    result = _run(env, "backup.sh", "--now", knobs={"toc": "empty"})
    assert result.returncode == 1
    assert len(_calls(env, "pg_dump")) == 1
    assert _dumps(env) == []
    assert "lists no table" in _log(env)


def test_now_takes_no_extra_argument(env):
    assert _run(env, "backup.sh", "--now", "extra").returncode == 2


def test_retention_plan_still_runs_offline(env):
    result = _run(env, "backup.sh", "--retention-plan", "2026-10-30", "1", "1", "2026-10-29", "2026-10-30")
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["delete 2026-10-29", "keep 2026-10-30"]
