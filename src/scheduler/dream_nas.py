"""Dream scheduler. Stays running and re-reads settings.json every poll.

In stock mode it sleeps and does not queue. In scheduler mode it queues dreams for
eligible notebooks that have unread conclusions. Eligibility is the configured pairs
plus each enabled peer with itself. Both peers must be listed and not disabled.
A peer seen in the database and missing from the file is appended as enabled false;
a notebook (observer/observed) missing from pairs is appended as {} and put in scheduler.skip,
so it starts Off.
The write re-reads the file and keeps a newer save. Nothing is written when no peer is new.
The database host, user and name, and the API URL, come from the environment.

The schedule_dream body includes scheduled_by. ScheduleDreamRequest has no
trigger_reason field, so the route stores trigger_reason "manual" and drops
scheduled_by. The hook treats "manual" as an API-scheduled dream, and prefers
scheduled_by when that attribute is on the payload.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sys
import time
import urllib.parse
from pathlib import Path

MARK = "honcho-scheduler"
PEER_NAME = re.compile(r"^[^/\s]+$")
SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|AIza|Bearer |eyJ)")
KEEP_BAK = 20

UNREAD_SQL = """SELECT c.observer, c.observed, count(d.id), min(d.created_at) FROM collections c
  JOIN documents d ON d.workspace_name=c.workspace_name AND d.observer=c.observer AND d.observed=c.observed
   AND d.level='explicit' AND d.deleted_at IS NULL
   AND ({cur}->>'through_created_at' IS NULL
        OR d.created_at > ({cur}->>'through_created_at')::timestamptz
        OR (d.created_at = ({cur}->>'through_created_at')::timestamptz AND d.id > coalesce({cur}->>'through_id','')))
  WHERE c.workspace_name=%s GROUP BY 1, 2"""
CURSOR = "c.internal_metadata::jsonb->'dream_notes'->'cursor'"

BUSY_SQL = """SELECT payload::jsonb->>'observer', payload::jsonb->>'observed' FROM queue
  WHERE task_type='dream' AND NOT processed AND workspace_name=%s"""

PAIRS_SQL = """SELECT DISTINCT observer, observed FROM collections WHERE workspace_name=%s"""

ERRORS_SQL = """SELECT count(*) FROM queue WHERE task_type='dream' AND error IS NOT NULL AND workspace_name=%s"""


def settings_path() -> Path:
    return Path(os.environ.get("CONTROL_FILE") or "/config/settings.json")


def log_path() -> Path:
    """SCHEDULER_LOG, else dream-nas.log in the stack's LOGS_DIR, mounted at /logs."""
    return Path(os.environ.get("SCHEDULER_LOG") or "/logs/dream-nas.log")


def log_limits(data) -> tuple[float, int]:
    """Bytes and rotated copies from settings.json logs, or the defaults 10 MB and 1."""
    max_mb = 10
    keep = 1
    logs = data.get("logs") if isinstance(data, dict) else None
    if isinstance(logs, dict):
        raw_mb = logs.get("max_mb", 10)
        raw_keep = logs.get("keep", 1)
        if type(raw_mb) in (int, float) and raw_mb > 0:
            max_mb = raw_mb
        if type(raw_keep) is int and 1 <= raw_keep <= 10:
            keep = raw_keep
    return max_mb * 1024 * 1024, keep


def rotate_log(path: Path, limit: float, keep: int = 1) -> None:
    try:
        if not path.is_file() or path.stat().st_size < limit:
            return
        oldest = path.with_name(f"{path.name}.{keep}")
        if oldest.exists():
            oldest.unlink()
        for index in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{index}")
            if src.exists():
                os.replace(src, path.with_name(f"{path.name}.{index + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        pass


def format_line(message: str, mode: str) -> str:
    return f"{message} mode={mode}"


def safe_text(text: str) -> str:
    flat = (text or "").replace("\n", " ").strip()
    if SECRET.search(flat):
        return "(response hidden)"
    return flat[:150]


def api_base() -> str:
    explicit = os.environ.get("HONCHO_API", "").strip()
    if explicit:
        return explicit.rstrip("/")
    url = os.environ.get("HONCHO_API_URL", "").strip().rstrip("/")
    if not url:
        raise RuntimeError("HONCHO_API_URL is unset")
    if url.endswith("/v3"):
        return url
    return url + "/v3"


def schedule_url(workspace: str) -> str:
    return f"{api_base()}/workspaces/{urllib.parse.quote(workspace, safe='')}/schedule_dream"


def load_settings(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("settings file must be an object")
    if _scheduler_secret(data):
        raise ValueError("secret-like string")
    # The loop reads these as objects; a list here crashed the process at every start.
    for name in ("peers", "pairs", "scheduler"):
        if data.get(name) is not None and not isinstance(data[name], dict):
            raise ValueError(f"{name} must be an object")
    return data


def _secret(value) -> bool:
    if isinstance(value, str):
        return SECRET.search(value) is not None
    if isinstance(value, dict):
        return any(_secret(key) or _secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_secret(item) for item in value)
    return False


def _scheduler_secret(data: dict) -> bool:
    """Secret scan of the values the scheduler reads: mode, peers, pairs, scheduler."""
    if _secret(data.get("mode")):
        return True
    peers = data.get("peers")
    if isinstance(peers, dict):
        for name, block in peers.items():
            if _secret(name):
                return True
            if isinstance(block, dict):
                if _secret(block.get("enabled")):
                    return True
            elif _secret(block):
                return True
    elif _secret(peers):
        return True
    pairs = data.get("pairs")
    if isinstance(pairs, dict):
        for key in pairs:
            if _secret(key):
                return True
    elif _secret(pairs):
        return True
    scheduler = data.get("scheduler")
    if isinstance(scheduler, dict):
        for name in ("poll_seconds", "max_parallel", "dream_type", "skip"):
            if _secret(scheduler.get(name)):
                return True
    elif _secret(scheduler):
        return True
    return False


class SettingsStore:
    """Last good settings, re-read when the mtime changes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict | None = None
        self.mtime: int | None = None
        # Why the file at rejected_mtime was not applied; main() logs it once per file version.
        self.rejected: str | None = None
        self.rejected_mtime: int | None = None

    def reload(self) -> dict | None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return self.data
        if mtime == self.mtime and self.data is not None:
            return self.data
        try:
            data = load_settings(self.path)
        except Exception as exc:
            if mtime != self.rejected_mtime:
                self.rejected_mtime = mtime
                # Our own messages and JSON's position messages carry no values.
                reason = str(exc) if isinstance(exc, ValueError) else ""
                self.rejected = f"{type(exc).__name__}: {reason}"[:160].rstrip(": ")
            return self.data
        self.rejected = None
        self.data = data
        self.mtime = mtime
        return data

    def remember(self, data: dict, mtime: int | None = None) -> None:
        self.data = data
        if mtime is not None:
            self.mtime = mtime
            return
        try:
            self.mtime = self.path.stat().st_mtime_ns
        except OSError:
            self.mtime = None


def bak_dir(path: Path) -> Path:
    """backups/settings in the folder that holds settings.json (CONFIG_DIR), where the dashboard keeps its copies."""
    return Path(path).parent / "backups" / "settings"


def _bak_order(item: Path) -> tuple:
    """Stamp, then the numeric suffix a same-stamp copy gets (-2 before -10)."""
    stamp, _, suffix = item.name.rpartition(".bak-")[2].partition("-")
    return stamp, int(suffix) if suffix.isdigit() else 0


def atomic_write(path: Path, data: dict, *, keep: int = KEEP_BAK, seen_mtime: int | None = None) -> int | None:
    """Write via a temp file in the same directory, and keep at most `keep` bak copies in bak_dir().

    When seen_mtime is set, a file whose mtime changed since that read is left in place.
    Returns the mtime of the file written, or None when the write was skipped.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        folder = bak_dir(path)
        folder.mkdir(parents=True, exist_ok=True)
        # One clock reading: seconds and nanoseconds read apart can sort a later copy first.
        now_ns = time.time_ns()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(now_ns // 1_000_000_000)) + f"{now_ns % 1_000_000_000:09d}"
        bak = folder / f"{path.name}.bak-{stamp}"
        suffix = 0
        while bak.exists():
            suffix += 1
            bak = folder / f"{path.name}.bak-{stamp}-{suffix}"
        shutil.copyfile(path, bak)
        baks = sorted(folder.glob(path.name + ".bak-*"), key=_bak_order)
        while len(baks) > keep:
            baks.pop(0).unlink()
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(payload, encoding="utf-8")
    with open(tmp, "r+b") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    if seen_mtime is not None:
        try:
            current = path.stat().st_mtime_ns
        except OSError:
            current = None
        if current != seen_mtime:
            try:
                tmp.unlink()
            except OSError:
                pass
            return None
    os_replace(tmp, path)
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return seen_mtime


def os_replace(src: Path, dst: Path) -> None:
    os.replace(src, dst)


def _usable(name) -> bool:
    return isinstance(name, str) and bool(PEER_NAME.match(name)) and not SECRET.search(name)


def discover(data: dict, names, pairs=()) -> tuple[list[str], int]:
    """Append unknown peers as enabled false and unknown pairs as {}. Returns what was added and how many were refused.

    added holds peer names and observer/observed keys. A new pair also goes into scheduler.skip, so it starts
    Off like a new peer; the dashboard turns a pair's peers On when the pair is switched On.
    Refused names are not written and not returned. The settings object gains no extra key.
    """
    peers = data.setdefault("peers", {})
    if not isinstance(peers, dict):
        data["peers"] = {}
        peers = data["peers"]
    added = []
    skipped = 0
    for name in sorted({item for item in names if isinstance(item, str)}):
        if name in peers:
            continue
        if not _usable(name):
            skipped += 1
            continue
        peers[name] = {"enabled": False}
        added.append(name)
    known = data.setdefault("pairs", {})
    if not isinstance(known, dict):
        return added, skipped
    for observer, observed in sorted({tuple(item) for item in pairs if len(item) == 2}):
        key = f"{observer}/{observed}"
        if key in known:
            continue
        if not _usable(observer) or not _usable(observed):
            skipped += 1
            continue
        known[key] = {}
        added.append(key)
        scheduler = data.setdefault("scheduler", {})
        skip = scheduler.setdefault("skip", []) if isinstance(scheduler, dict) else None
        if isinstance(skip, list) and key not in skip:
            skip.append(key)
    return added, skipped


def commit_discovery(store: SettingsStore, names, pairs=()) -> tuple[list[str], int] | None:
    """Append unknown peers and pairs and write them.

    Returns (added, skipped). added is empty when nothing new was written.
    Returns None when the file changed again after the re-read; this poll does
    not write, so a dashboard save is left in place.
    """
    data = store.data
    if not isinstance(data, dict):
        return [], 0

    def prepare(source: dict) -> tuple[list[str], int, dict]:
        trial = json.loads(json.dumps(source))
        added, skipped = discover(trial, names, pairs)
        return added, skipped, trial

    added, skipped, trial = prepare(data)
    if not added:
        return [], skipped
    try:
        seen = store.path.stat().st_mtime_ns
    except OSError as exc:
        raise RuntimeError("settings.json unreadable") from exc
    if seen != store.mtime:
        fresh = store.reload()
        if not isinstance(fresh, dict):
            raise RuntimeError("settings.json unreadable")
        added, skipped, trial = prepare(fresh)
        if not added:
            return [], skipped
        try:
            seen = store.path.stat().st_mtime_ns
        except OSError as exc:
            raise RuntimeError("settings.json unreadable") from exc
        if seen != store.mtime:
            return None
    written = atomic_write(store.path, trial, seen_mtime=seen)
    if written is None:
        return None
    store.remember(trial, written)
    return added, skipped


def _listed_on(peers: dict, name: str) -> bool:
    block = peers.get(name)
    return isinstance(block, dict) and block.get("enabled") is not False


def eligible(data: dict) -> list[tuple[str, str]]:
    """Configured pairs, plus each enabled peer with itself. skip removes a pair."""
    peers = data.get("peers") or {}
    if not isinstance(peers, dict):
        peers = {}
    skip = set()
    scheduler = data.get("scheduler") or {}
    if isinstance(scheduler, dict):
        for item in scheduler.get("skip") or []:
            if isinstance(item, str):
                skip.add(item)
    seen: list[tuple[str, str]] = []

    def add(observer: str, observed: str) -> None:
        key = f"{observer}/{observed}"
        if key in skip or not _listed_on(peers, observer) or not _listed_on(peers, observed):
            return
        pair = (observer, observed)
        if pair not in seen:
            seen.append(pair)

    pairs = data.get("pairs") or {}
    if isinstance(pairs, dict):
        for key in pairs:
            if not isinstance(key, str) or "/" not in key:
                continue
            observer, observed = key.split("/", 1)
            add(observer, observed)
    for name in peers:
        if isinstance(name, str):
            add(name, name)
    return seen


def schedule_payload(observer: str, observed: str, dream_type: str) -> dict:
    return {
        "observer": observer,
        "observed": observed,
        "dream_type": dream_type,
        "scheduled_by": MARK,
    }


def scheduling_on(data: dict | None) -> bool:
    return bool(data) and data.get("mode") == "scheduler"


class Holds:
    """Pairs whose last queued dream left their unread conclusions as they were.

    A dream that fails, a card refresh, or a dream with briefs switched off does not move Honcho's
    cursor, and Honcho's schedule route has no time gate of its own, so the pair would be queued
    again at every poll. After the scheduler queues a pair, the pair waits while its signature
    (unread count, oldest unread) stays what it was: 15 minutes after the first queue, doubling
    with every queue that changed nothing, up to 24 hours. Any change to the signature releases it.
    """

    def __init__(self, first: float = 15 * 60, longest: float = 24 * 3600):
        self.first = first
        self.longest = longest
        self.queued: dict = {}

    def blocked(self, pair: tuple, signature: tuple, now: float) -> bool:
        entry = self.queued.get(pair)
        if entry is None:
            return False
        held_signature, wait, queued_at = entry
        if held_signature != signature:
            del self.queued[pair]
            return False
        return now - queued_at < wait

    def record(self, pair: tuple, signature: tuple, now: float) -> bool:
        """Remember a queued pair. True when the previous dream left the same signature."""
        entry = self.queued.get(pair)
        repeat = entry is not None and entry[0] == signature
        wait = min(entry[1] * 2, self.longest) if repeat else self.first
        self.queued[pair] = (signature, wait, now)
        return repeat


def schedule_pass(data: dict, unread, busy: set, post, holds: Holds | None = None,
                  now: float | None = None) -> tuple[int, list[str]]:
    """Queue dreams up to max_parallel. post(payload) -> (status, text)."""
    if not scheduling_on(data):
        return 0, []
    holds = holds if holds is not None else Holds()
    now = time.monotonic() if now is None else now
    scope = set(eligible(data))
    scheduler = data.get("scheduler") or {}
    dream_type = scheduler.get("dream_type") or "omni"
    max_parallel = parallel_limit(scheduler)
    free = max_parallel - len(busy)
    messages = []
    scheduled = 0
    todo = [row for row in unread if (row[0], row[1]) in scope and row[2]]
    todo.sort(key=lambda row: (row[3] is None, row[3]))
    for observer, observed, count, oldest in todo:
        if free <= 0:
            break
        pair = (observer, observed)
        if pair in busy:
            continue
        signature = (count, str(oldest))
        if holds.blocked(pair, signature, now):
            continue
        payload = schedule_payload(observer, observed, dream_type)
        status, text = post(payload)
        if status < 300:
            scheduled += 1
            free -= 1
            busy.add(pair)
            if holds.record(pair, signature, now):
                messages.append(
                    f"scheduled {observer}/{observed} again ({count} unread, unchanged by the last dream; "
                    f"next wait {holds.queued[pair][1] // 60:.0f} min)"
                )
            else:
                messages.append(f"scheduled {observer}/{observed} ({count} unread)")
        else:
            messages.append(f"schedule {observer}/{observed} -> {status} {safe_text(text)}")
    return scheduled, messages


def fingerprint(data: dict) -> tuple:
    scheduler = data.get("scheduler") or {}
    peers = data.get("peers") or {}
    enabled = tuple(sorted(
        (name, bool(isinstance(block, dict) and block.get("enabled") is not False))
        for name, block in peers.items()
    ))
    pairs = tuple(sorted((data.get("pairs") or {}).keys()))
    skip = tuple(scheduler.get("skip") or [])
    return (
        data.get("mode"),
        scheduler.get("poll_seconds"),
        scheduler.get("max_parallel"),
        scheduler.get("dream_type"),
        skip,
        pairs,
        enabled,
    )


def parallel_limit(scheduler) -> int:
    """Dreams that may be queued at once. A missing or unusable value is 1."""
    if not isinstance(scheduler, dict):
        return 1
    try:
        value = int(scheduler.get("max_parallel") or 1)
    except (TypeError, ValueError):
        return 1
    return value if value > 0 else 1


def poll_seconds(data: dict | None) -> int:
    if not data:
        return 15
    try:
        value = int((data.get("scheduler") or {}).get("poll_seconds") or 15)
    except (TypeError, ValueError):
        return 15
    return value if value > 0 else 15


class Database:
    def __init__(self):
        self._conn = None

    def _connect(self):
        import psycopg

        self._conn = psycopg.connect(**database_kwargs())

    def _rows(self, sql: str, params: tuple):
        # The connect is inside the try: psycopg's connect error names the host, port and user.
        try:
            if self._conn is None or getattr(self._conn, "closed", False):
                self._connect()
            return self._conn.execute(sql, params).fetchall()
        except Exception as exc:
            conn = self._conn
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            self._conn = None
            code = getattr(exc, "sqlstate", "") or ""
            raise RuntimeError(f"database unavailable ({type(exc).__name__} {code})".strip()) from None

    def unread(self, workspace: str):
        sql = UNREAD_SQL.format(cur=CURSOR)
        return [(row[0], row[1], int(row[2]), row[3]) for row in self._rows(sql, (workspace,))]

    def busy(self, workspace: str) -> set:
        return {(row[0], row[1]) for row in self._rows(BUSY_SQL, (workspace,))}

    def notebooks(self, workspace: str) -> list[tuple[str, str]]:
        """Every observer/observed pair that has a collection (a notebook) in Honcho."""
        return [(row[0], row[1]) for row in self._rows(PAIRS_SQL, (workspace,))
                if row and isinstance(row[0], str) and isinstance(row[1], str)]

    def error_count(self, workspace: str) -> int:
        rows = self._rows(ERRORS_SQL, (workspace,))
        return int(rows[0][0]) if rows else 0


def post_json(url: str, token: str, payload: dict, timeout: float = 60) -> tuple[int, str]:
    import httpx

    response = httpx.post(url, json=payload, headers={"authorization": f"Bearer {token}"}, timeout=timeout)
    return response.status_code, response.text


def _sleep(seconds: int, stopped) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stopped():
            return
        time.sleep(min(1, end - time.monotonic()))


# Compose loads Honcho's whole .env into this container (env_file): database password, JWT
# secret, provider keys. At start the process replaces itself with one that holds only the names
# below, so code running here cannot read the rest from its own environment. The generic names
# are those Python, the C library and an HTTP client may read.
_ENV_GENERIC = frozenset({"PATH", "HOME", "HOSTNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR"})
_ENV_PROXY = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"})
_ENV_PREFIXES = ("LC_", "PYTHON", "SSL_CERT_")
# PG* keeps the libpq settings (PGSSLMODE and the rest) the driver reads itself.
_ENV_USED = frozenset({
    "CONTROL_FILE", "SCHEDULER_LOG", "HONCHO_API", "HONCHO_API_URL", "HONCHO_WORKSPACE",
    "HONCHO_API_TOKEN", "POSTGRES_PASSWORD",
})


def runtime_env(environ) -> dict:
    """The part of the environment this process reads; everything else is left out."""
    return {
        name: value
        for name, value in environ.items()
        if name in _ENV_USED
        or name in _ENV_GENERIC
        or name.upper() in _ENV_PROXY
        or name.startswith(_ENV_PREFIXES + ("PG",))
    }


def drop_unused_env() -> None:
    """Start again with runtime_env() when the environment holds anything more."""
    keep = runtime_env(os.environ)
    if len(keep) != len(os.environ):
        os.execve(sys.executable, sys.orig_argv, keep)


def database_kwargs() -> dict:
    """Connection arguments from the environment. No host, user or database name is built in."""
    missing = []
    values = {}
    for name in ("PGHOST", "PGUSER", "PGDATABASE", "POSTGRES_PASSWORD"):
        value = os.environ.get(name)
        if value is None or value == "":
            missing.append(name)
        else:
            values[name] = value
    if missing:
        raise RuntimeError("database env unset: " + ", ".join(missing))
    port_text = os.environ.get("PGPORT", "").strip()
    if port_text:
        try:
            port = int(port_text)
        except ValueError as exc:
            raise RuntimeError("PGPORT is not an integer") from exc
    else:
        port = 5432
    return {
        "host": values["PGHOST"],
        "port": port,
        "user": values["PGUSER"],
        "password": values["POSTGRES_PASSWORD"],
        "dbname": values["PGDATABASE"],
        "autocommit": True,
    }


def describe_change(data: dict) -> str:
    scheduler = data.get("scheduler") or {}
    pairs = data.get("pairs") or {}
    skip = [item for item in (scheduler.get("skip") or []) if isinstance(item, str)]
    return (
        f"config changed: max_parallel {parallel_limit(scheduler)}, "
        f"type {scheduler.get('dream_type')}, {len(pairs)} pairs, "
        f"skip {sorted(skip)}"
    )


def main() -> None:
    path = settings_path()
    destination = log_path()
    store = SettingsStore(path)
    workspace = os.environ.get("HONCHO_WORKSPACE", "").strip()
    token = os.environ.get("HONCHO_API_TOKEN", "").strip()
    stopped = {"flag": False}

    def stop(_signum=None, _frame=None):
        stopped["flag"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def write(message: str, mode: str) -> None:
        line = time.strftime("%Y-%m-%d %H:%M:%SZ ", time.gmtime()) + format_line(message, mode)
        print(line, flush=True)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            limit, keep = log_limits(store.data)
            rotate_log(destination, limit, keep)
            with open(destination, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    def halted() -> bool:
        return stopped["flag"]

    data = store.reload()
    mode = data.get("mode") if data and data.get("mode") in ("stock", "scheduler") else "stock"
    if data is None:
        reason = f"not applied ({store.rejected})" if store.rejected else "unreadable"
        write(f"settings.json {reason}; sleeping", mode)
    told_rejected = store.rejected_mtime
    scheduler = (data or {}).get("scheduler") or {}
    pair_count = len((data or {}).get("pairs") or {})
    write(
        f"=== start: {pair_count} pairs, max {parallel_limit(scheduler)} at once, "
        f"type {scheduler.get('dream_type', 'omni')}",
        mode,
    )
    started = time.monotonic()
    scheduled = 0
    last_report = 0.0
    seen = fingerprint(data) if data else None
    told_stock = False
    db = Database()
    holds = Holds()

    while not halted():
        previous = data
        data = store.reload()
        if store.rejected and store.rejected_mtime != told_rejected:
            # Once per file version: a save the scheduler refuses would otherwise go unnoticed.
            told_rejected = store.rejected_mtime
            write(f"settings.json not applied ({store.rejected}); keeping the previous settings", mode)
        if data is None:
            data = previous
        if data is None:
            _sleep(15, halted)
            continue
        mode = data.get("mode") if data.get("mode") in ("stock", "scheduler") else "stock"
        current = fingerprint(data)
        if seen is not None and current != seen:
            write(describe_change(data), mode)
        seen = current
        if workspace and scheduling_on(data):
            try:
                pairs = db.notebooks(workspace)
                names = sorted({name for pair in pairs for name in pair})
            except Exception:
                write("discovery failed (database)", mode)
            else:
                try:
                    result = commit_discovery(store, names, pairs)
                except Exception as exc:
                    store.mtime = None
                    write(f"discovery write failed ({type(exc).__name__})", mode)
                else:
                    if result is None:
                        write("discovery deferred (settings.json changed)", mode)
                    else:
                        added, _skipped = result
                        if added:
                            found = sum("/" in item for item in added)
                            write(f"discovered {len(added) - found} peers, {found} pairs", mode)
                    latest = store.data or data
                    if latest is not data:
                        data = latest
                        current = fingerprint(data)
                        if current != seen:
                            write(describe_change(data), mode)
                        seen = current
        if not scheduling_on(data):
            if not told_stock:
                write("sleeping", mode)
                told_stock = True
            _sleep(poll_seconds(data), halted)
            continue
        told_stock = False
        if not workspace or not token:
            write("HONCHO_WORKSPACE or HONCHO_API_TOKEN is unset", mode)
            _sleep(poll_seconds(data), halted)
            continue
        try:
            target = schedule_url(workspace)
        except RuntimeError as exc:
            write(str(exc), mode)
            _sleep(poll_seconds(data), halted)
            continue
        try:
            unread = db.unread(workspace)
            busy = db.busy(workspace)
        except Exception as exc:
            write(f"database unavailable ({exc})", mode)
            _sleep(poll_seconds(data), halted)
            continue

        def post(payload: dict, _url=target, _token=token):
            return post_json(_url, _token, payload)

        try:
            count, messages = schedule_pass(data, unread, busy, post, holds)
        except Exception as exc:
            write(f"schedule pass failed ({type(exc).__name__})", mode)
            _sleep(poll_seconds(data), halted)
            continue
        scheduled += count
        for message in messages:
            write(message, mode)
        now = time.monotonic()
        if now - last_report > 600:
            left = sum(row[2] for row in unread if (row[0], row[1]) in set(eligible(data)))
            notebooks = sum(1 for row in unread if (row[0], row[1]) in set(eligible(data)) and row[2])
            write(
                f"running {len(busy)}, scheduled so far {scheduled}, unread left {left} in {notebooks} notebooks",
                mode,
            )
            last_report = now
        if not halted():
            _sleep(poll_seconds(data), halted)

    errors = 0
    if workspace:
        try:
            errors = db.error_count(workspace)
        except Exception:
            errors = 0
    minutes = (time.monotonic() - started) / 60
    write(f"=== done in {minutes:.0f} min: {scheduled} dreams, dream errors {errors}", mode)


if __name__ == "__main__":
    drop_unused_env()
    main()
