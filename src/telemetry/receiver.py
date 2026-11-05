"""CloudEvents receiver.

Accepts Honcho's posts, appends one JSON line per event to a UTC-day file, and
optionally forwards the batch to a viewer. The disk write is the record; the
forward is best-effort and never decides the HTTP result.

Stdlib only. Deployment settings come from the environment (see load_config).
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import shutil
import signal
import sys
import threading
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping

# Default body cap. Honcho sends up to TELEMETRY_BATCH_SIZE events per post (100 by default)
# and, when it is refused, puts the batch back at the head of its buffer and sends it again, so a
# cap below its largest batch stops telemetry rather than dropping one post. MAX_BODY_MB lowers or
# raises it per deployment.
MAX_BODY = 64 * 1024 * 1024
# Honcho's TELEMETRY_BATCH_SIZE is at most 1000.
MAX_EVENTS = 1000
# Bodies read and parsed at the same time. Honcho's api and deriver each send one batch at a time,
# so two cover them; a third waits up to BUSY_WAIT seconds, then gets 503 and is sent again later.
# Memory held by bodies stays near MAX_READERS times the cap whatever the number of connections.
MAX_READERS = 2
BUSY_WAIT = 30.0
REQUIRED = ("id", "type", "source", "specversion")
_DAY = 86400.0
# Compose loads Honcho's whole .env into this container (env_file): database password, JWT
# secret, provider keys. At start the process replaces itself with one that holds only the names
# below, so code running here cannot read the rest from its own environment. The generic names
# are those Python, the C library and an HTTP client may read.
_ENV_GENERIC = frozenset({"PATH", "HOME", "HOSTNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR"})
_ENV_PROXY = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"})
_ENV_PREFIXES = ("LC_", "PYTHON", "SSL_CERT_")
_ENV_USED = frozenset({
    "TELEMETRY_TOKEN", "DATA_DIR", "ALLOWED_SOURCES", "MIN_FREE_GB", "GZIP_AFTER_DAYS",
    "RETENTION_DAYS", "VIEWER_URL", "VIEWER_KEY", "BIND_HOST", "PORT", "MAINTENANCE_LOG",
    "MAX_BODY_MB",
})


def runtime_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The part of the environment this process reads; everything else is left out."""
    return {
        name: value
        for name, value in environ.items()
        if name in _ENV_USED
        or name in _ENV_GENERIC
        or name.upper() in _ENV_PROXY
        or name.startswith(_ENV_PREFIXES)
    }


def drop_unused_env() -> None:
    """Start again with runtime_env() when the environment holds anything more."""
    keep = runtime_env(os.environ)
    if len(keep) != len(os.environ):
        os.execve(sys.executable, sys.orig_argv, keep)
_EVENT_NAME = re.compile(r"^events-(\d{4}-\d{2}-\d{2})\.jsonl(\.gz)?$")


@dataclass(frozen=True)
class Config:
    token: str
    data_dir: Path
    allowed: tuple[str, ...] = ()
    min_free_bytes: int = 2 * 1024**3
    gzip_after_days: float = 1.0
    retention_days: float = 90.0
    viewer_url: str = ""
    viewer_key: str = ""
    bind: str = "0.0.0.0"
    port: int = 8765
    max_body: int = MAX_BODY
    forward_timeout: float = 2.0
    maintenance_interval: float = _DAY
    maintenance_log: Path = Path("/logs/maintenance.log")
    max_events: int = MAX_EVENTS


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Read deployment settings. Raises ValueError when the token is missing."""
    src = os.environ if env is None else env
    token = src.get("TELEMETRY_TOKEN", "")
    if not token:
        raise ValueError("TELEMETRY_TOKEN is required")
    return Config(
        token=token,
        data_dir=Path(src.get("DATA_DIR") or "/data"),
        allowed=_split_csv(src.get("ALLOWED_SOURCES", "")),
        min_free_bytes=int(_number(src, ("MIN_FREE_GB",), 2.0) * 1024**3),
        gzip_after_days=_number(src, ("GZIP_AFTER_DAYS",), 1.0),
        retention_days=_number(src, ("RETENTION_DAYS",), 90.0),
        viewer_url=(src.get("VIEWER_URL") or "").strip(),
        viewer_key=src.get("VIEWER_KEY") or "",
        bind=src.get("BIND_HOST") or "0.0.0.0",
        port=_port(src),
        max_body=_max_body(src),
        forward_timeout=2.0,
        maintenance_log=Path(src.get("MAINTENANCE_LOG") or "/logs/maintenance.log"),
    )


def _split_csv(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(",") if part.strip())


def _number(env: Mapping[str, str], names: tuple[str, ...], default: float) -> float:
    for name in names:
        raw = env.get(name, "")
        if raw == "":
            continue
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"{name} is not a number") from None
    return default


def _max_body(env: Mapping[str, str]) -> int:
    megabytes = _number(env, ("MAX_BODY_MB",), MAX_BODY / 1024**2)
    if not 0 < megabytes <= 1024:
        raise ValueError("MAX_BODY_MB must be above 0 and at most 1024")
    return int(megabytes * 1024**2)


def _port(env: Mapping[str, str]) -> int:
    raw = env.get("PORT", "")
    if raw == "":
        return 8765
    try:
        return int(raw)
    except ValueError:
        raise ValueError("PORT is not a number") from None


def bearer_ok(header: str | None, token: str) -> bool:
    if not header or not token:
        return False
    scheme, _, presented = header.partition(" ")
    if scheme != "Bearer" or presented == "":
        return False
    try:
        return hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))
    except (TypeError, UnicodeError):
        return False


def ip_allowed(client_ip: str, allowed: tuple[str, ...]) -> bool:
    """Empty allow-list means every source. Otherwise the IP must match an entry or a CIDR."""
    if not allowed:
        return True
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    for item in allowed:
        try:
            if "/" in item:
                if addr in ipaddress.ip_network(item, strict=False):
                    return True
            elif addr == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False


def parse_events(payload: object, max_events: int = MAX_EVENTS) -> list[dict] | None:
    """One CloudEvent or a non-empty array of at most max_events.

    None when the batch is empty or too long, or when any item is not an object whose
    required attributes are non-empty strings (CloudEvents 1.0 types them as strings).
    """
    items = payload if isinstance(payload, list) else [payload]
    if not items or len(items) > max_events:
        return None
    events: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        for key in REQUIRED:
            value = item.get(key)
            if not isinstance(value, str) or not value:
                return None
        events.append(item)
    return events


def decode_body(blob: bytes) -> object:
    """Parse a request body. Raises ValueError for anything that is not JSON.

    RecursionError (deep nesting) and the integer-length limit both surface here as
    ValueError, so no input can end a handler thread without an answer.
    """
    try:
        return json.loads(blob)
    except RecursionError:
        raise ValueError("nested too deeply") from None


def day_path(data_dir: Path, moment: datetime | None = None) -> Path:
    when = moment or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    return data_dir / f"events-{when:%Y-%m-%d}.jsonl"


def append_batch(data_dir: Path, events: list[dict], moment: datetime | None = None) -> Path:
    """Write one line per event and fsync before returning. One batch shares one received_at."""
    when = moment or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    received_at = when.isoformat()
    blob = "".join(
        json.dumps({"received_at": received_at, "event": event}, ensure_ascii=False, separators=(",", ":")) + "\n"
        for event in events
    ).encode("utf-8")
    data_dir.mkdir(parents=True, exist_ok=True)
    path = day_path(data_dir, when)
    with path.open("ab") as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _age_days(path: Path, now: float) -> float:
    mtime_age = (now - path.stat().st_mtime) / _DAY
    match = _EVENT_NAME.match(path.name)
    if not match:
        return mtime_age
    try:
        file_day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return mtime_age
    today = datetime.fromtimestamp(now, timezone.utc).date()
    return max(mtime_age, float((today - file_day).days))


def _gzip_file(path: Path) -> None:
    """Replace path with path.gz. The original stays in place until the gzip reads back identical."""
    dest = path.parent / (path.name + ".gz")
    if dest.exists():
        raise FileExistsError(dest.name)
    partial = path.parent / (path.name + ".gz.partial")
    st = path.stat()
    digest = hashlib.sha256()
    try:
        with path.open("rb") as src, partial.open("wb") as raw:
            with gzip.GzipFile(filename=path.name, fileobj=raw, mode="wb", mtime=int(st.st_mtime)) as compressed:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    compressed.write(chunk)
            raw.flush()
            os.fsync(raw.fileno())
        check = hashlib.sha256()
        with gzip.open(partial, "rb") as compressed:
            while True:
                chunk = compressed.read(1024 * 1024)
                if not chunk:
                    break
                check.update(chunk)
        if check.digest() != digest.digest():
            raise RuntimeError("gzip verify failed")
        partial.replace(dest)
        os.utime(dest, (st.st_atime, st.st_mtime))
        path.unlink()
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def run_maintenance(
    data_dir: Path,
    *,
    gzip_after_days: float = 1.0,
    retention_days: float = 90.0,
    now: float | None = None,
    log_path: Path | None = None,
) -> list[str]:
    """Gzip and delete old day files. Today's UTC file is left untouched.

    Each pass appends its actions to log_path (the service passes /logs/maintenance.log,
    in the stack's LOGS_DIR); without one, to maintenance.log in data_dir.

    Age is the greater of mtime age and the UTC date in the file name, in days.
    A file is gzipped when that age is greater than gzip_after_days, and deleted
    when it is greater than retention_days. Deletion wins, so an old plain file
    is removed rather than compressed first.
    """
    moment = datetime.now(timezone.utc).timestamp() if now is None else now
    data_dir.mkdir(parents=True, exist_ok=True)
    today_name = "events-" + datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%d") + ".jsonl"
    actions: list[str] = []
    for partial in sorted(data_dir.glob("events-*.jsonl.gz.partial")):
        if partial.name.endswith(".jsonl.gz.partial") and _EVENT_NAME.match(partial.name[: -len(".partial")]):
            partial.unlink(missing_ok=True)
            actions.append(f"discard {partial.name}")
    files = [
        path
        for path in sorted(data_dir.iterdir())
        if path.is_file() and _EVENT_NAME.match(path.name) and path.name not in (today_name, today_name + ".gz")
    ]
    for path in files:
        try:
            age = _age_days(path, moment)
        except FileNotFoundError:
            continue
        try:
            if age > retention_days:
                path.unlink()
                actions.append(f"delete {path.name}")
                continue
            if path.name.endswith(".gz") or age <= gzip_after_days:
                continue
            if (path.parent / (path.name + ".gz")).exists():
                actions.append(f"gzip-skipped {path.name}")
                continue
            _gzip_file(path)
            actions.append(f"gzip {path.name}")
        except OSError as exc:
            actions.append(f"failed {path.name} {type(exc).__name__}")
        except Exception as exc:
            actions.append(f"failed {path.name} {type(exc).__name__}")
    _append_log(log_path or data_dir / "maintenance.log", moment, actions or ["ok"])
    return actions


def _append_log(path: Path, moment: float, actions: list[str]) -> None:
    stamp = datetime.fromtimestamp(moment, timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for action in actions:
            handle.write(f"{stamp} {action}\n")
        handle.flush()
        os.fsync(handle.fileno())


class Forwarder:
    """One background thread. A full queue drops the batch; nothing is retried."""

    def __init__(self, url: str, key: str, timeout: float = 2.0, max_queued: int = 4) -> None:
        self.url = url
        self.key = key
        self.timeout = timeout
        self.sent = 0
        self.failures = 0
        self._up: bool | None = None
        self._stats = threading.Lock()
        self._queue: queue.Queue[list[dict]] = queue.Queue(maxsize=max_queued)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if url:
            self._thread = threading.Thread(target=self._run, name="telemetry-forward", daemon=True)
            self._thread.start()

    def submit(self, events: list[dict]) -> None:
        if not self.url:
            return
        try:
            self._queue.put_nowait(events)
        except queue.Full:
            self._bump_failure("queue full")

    def stats(self) -> tuple[int, int]:
        with self._stats:
            return self.sent, self.failures

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.timeout + 1)

    def _bump_failure(self, reason: str) -> None:
        with self._stats:
            self.failures += 1
            changed = self._up is not False
            self._up = False
        if changed:
            print(f"viewer unreachable: {reason}", file=sys.stderr, flush=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                events = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._post(events)
            except Exception as exc:
                self._bump_failure(type(exc).__name__)
            else:
                with self._stats:
                    self.sent += 1
                    changed = self._up is not True
                    self._up = True
                if changed:
                    print("viewer reachable", file=sys.stderr, flush=True)
            finally:
                self._queue.task_done()

    def _post(self, events: list[dict]) -> None:
        body = json.dumps(events, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["X-Telemetry-Key"] = self.key
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            response.read()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Receiver:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.forwarder = Forwarder(config.viewer_url, config.viewer_key, config.forward_timeout)
        self._lock = threading.Lock()
        self._readers = threading.BoundedSemaphore(MAX_READERS)
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._accepted = 0
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None
        self._maint: threading.Thread | None = None
        self._maint_stop = threading.Event()
        self.port = config.port

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.config.data_dir).free

    def bind(self) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self._httpd = _Server((self.config.bind, self.config.port), self._handler_class())
        self.port = int(self._httpd.server_address[1])

    def start(self) -> int:
        """Bind and serve on a background thread. Returns the port (useful when it is 0)."""
        self.bind()
        assert self._httpd is not None
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2}, name="telemetry-http", daemon=True
        )
        self._thread.start()
        self._start_maintenance()
        return self.port

    def serve_forever(self) -> None:
        if self._httpd is None:
            self.bind()
        assert self._httpd is not None
        self._start_maintenance()
        self._httpd.serve_forever(poll_interval=0.2)

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self._maint_stop.set()
        httpd = self._httpd
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        if self._maint is not None and self._maint is not threading.current_thread():
            self._maint.join(timeout=5)
        self.forwarder.close()

    def _start_maintenance(self) -> None:
        if self._maint is not None:
            return
        self._maint = threading.Thread(target=self._maintenance_loop, name="telemetry-maintenance", daemon=True)
        self._maint.start()

    def _maintenance_loop(self) -> None:
        while not self._maint_stop.is_set():
            try:
                # Held so a write cannot append to a file this pass is gzipping or deleting.
                with self._lock:
                    run_maintenance(
                        self.config.data_dir,
                        gzip_after_days=self.config.gzip_after_days,
                        retention_days=self.config.retention_days,
                        log_path=self.config.maintenance_log,
                    )
            except Exception as exc:
                print(f"maintenance failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            if self._maint_stop.wait(self.config.maintenance_interval):
                return

    def health(self) -> tuple[int, dict]:
        path = day_path(self.config.data_dir)
        try:
            size = path.stat().st_size if path.exists() else 0
        except OSError:
            size = 0
        try:
            free = self.free_bytes()
        except OSError:
            free = 0
        sent, failures = self.forwarder.stats()
        body = {
            "status": "ok",
            "day": path.name[len("events-") : -len(".jsonl")],
            "today_bytes": size,
            "free_bytes": free,
            "min_free_bytes": self.config.min_free_bytes,
            "accepted": self._accepted,
            "forward_sent": sent,
            "forward_failures": failures,
        }
        # Low space is reported, not failed: a 503 here would make the compose
        # healthcheck restart the container, which frees nothing.
        if free < self.config.min_free_bytes:
            body["status"] = "low_space"
        return 200, body

    def _handler_class(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(120)

            def do_GET(self) -> None:
                owner._get(self)

            def do_POST(self) -> None:
                owner._post(self)

            def log_message(self, fmt: str, *args) -> None:
                sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        return Handler

    def _get(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path.split("?", 1)[0] != "/health":
            _reply(handler, 404, {"error": "not found"})
            return
        status, body = self.health()
        _reply(handler, status, body)

    def _post(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path.split("?", 1)[0] != "/events":
            _reply(handler, 404, {"error": "not found"})
            return
        client = handler.client_address[0]
        if not ip_allowed(client, self.config.allowed):
            _reply(handler, 403, {"error": "forbidden"})
            return
        if not bearer_ok(handler.headers.get("Authorization"), self.config.token):
            _reply(handler, 401, {"error": "unauthorized"})
            return
        if handler.headers.get("Transfer-Encoding") is not None:
            # Chunked bodies are not parsed; reading Content-Length beside one would misframe.
            _reply(handler, 411, {"error": "length required"})
            return
        raw_lengths = handler.headers.get_all("Content-Length") or []
        if not raw_lengths:
            _reply(handler, 411, {"error": "length required"})
            return
        raw_length = raw_lengths[0].strip()
        # Digits only: no sign, no repeated or conflicting header.
        if len(raw_lengths) != 1 or not raw_length.isdigit() or not raw_length.isascii():
            _reply(handler, 400, {"error": "bad request"})
            return
        length = int(raw_length)
        if length > self.config.max_body:
            _reply(handler, 413, {"error": "body too large"})
            return
        if length <= 0:
            _reply(handler, 400, {"error": "bad request"})
            return
        if not self._readers.acquire(timeout=BUSY_WAIT):
            _reply(handler, 503, {"error": "busy"})
            return
        try:
            try:
                blob = handler.rfile.read(length)
            except (TimeoutError, OSError):
                handler.close_connection = True
                return
            if len(blob) != length:
                _reply(handler, 400, {"error": "bad request"})
                return
            try:
                payload = decode_body(blob)
            except ValueError:
                _reply(handler, 400, {"error": "bad request"})
                return
            del blob
            events = parse_events(payload, self.config.max_events)
        finally:
            self._readers.release()
        if events is None:
            _reply(handler, 400, {"error": "bad request"})
            return
        error = None
        free = 0
        with self._lock:
            try:
                free = self.free_bytes()
            except OSError:
                error = "storage"
            else:
                if free < self.config.min_free_bytes:
                    error = "space"
                else:
                    try:
                        append_batch(self.config.data_dir, events)
                        self._accepted += len(events)
                    except OSError:
                        error = "storage"
        if error == "space":
            _reply(
                handler,
                503,
                {"error": "low space", "free_bytes": free, "min_free_bytes": self.config.min_free_bytes},
            )
            return
        if error:
            _reply(handler, 503, {"error": "storage unavailable"})
            return
        # Queued, not awaited: a down viewer must not hold this response.
        self.forwarder.submit(events)
        _reply(handler, 200, {"accepted": len(events)})


def _reply(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except OSError:
        return


def _install_signals(app: Receiver) -> None:
    def _handle(signum, frame) -> None:
        # shutdown() has to run off the serve_forever thread.
        threading.Thread(target=app.stop, name="telemetry-stop", daemon=False).start()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def main() -> int:
    try:
        config = load_config()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    os.umask(0o077)
    app = Receiver(config)
    app.bind()
    _install_signals(app)
    try:
        app.serve_forever()
    finally:
        app.stop()
    return 0


if __name__ == "__main__":
    drop_unused_env()
    sys.exit(main())
