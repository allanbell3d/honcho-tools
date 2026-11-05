"""Receiver tests. No network beyond localhost.

The captured-batch test reads TELEMETRY_SAMPLE at run time (a jsonl of CloudEvents,
one object per line, raw or wrapped as {"received_at", "event"}). It posts the first
200 events and does not copy them into the tree.
"""

from __future__ import annotations

import gzip
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "telemetry"))

import receiver  # noqa: E402

TOKEN = "test-token"
_DAY = 86400.0


def _event(i: int = 1, **extra) -> dict:
    body = {"id": f"evt-{i}", "type": "test.event", "source": "/test", "specversion": "1.0"}
    body.update(extra)
    return body


@contextmanager
def serving(tmp_path: Path, **kwargs):
    config = receiver.Config(
        token=kwargs.pop("token", TOKEN),
        data_dir=tmp_path,
        port=0,
        bind="127.0.0.1",
        min_free_bytes=kwargs.pop("min_free_bytes", 0),
        **kwargs,
    )
    app = receiver.Receiver(config)
    app.start()
    try:
        yield app
    finally:
        app.stop()


def _call(app, method: str, path: str, body=None, token=TOKEN, headers=None, timeout=10):
    data = None if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
    hdrs = {}
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    if data is not None and "Content-Type" not in (headers or {}):
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    request = urllib.request.Request(
        f"http://127.0.0.1:{app.port}{path}", data=data, headers=hdrs, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else {}


def _lines(tmp_path: Path) -> list[dict]:
    files = sorted(tmp_path.glob("events-*.jsonl"))
    assert len(files) == 1
    text = files[0].read_bytes()
    assert text.endswith(b"\n")
    assert b"\r" not in text
    parsed = []
    for index, line in enumerate(text.split(b"\n")[:-1], 1):
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            raise AssertionError(f"line {index} is not json") from None
    return parsed


def _sample_events(limit: int = 200) -> list[dict]:
    raw = os.environ.get("TELEMETRY_SAMPLE", "")
    if not raw:
        pytest.skip("TELEMETRY_SAMPLE not set: no captured CloudEvents on this machine")
    path = Path(raw)
    if not path.is_file():
        raise AssertionError("TELEMETRY_SAMPLE is not a file")
    events = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if len(events) >= limit:
                break
            if not line.strip():
                continue
            obj = json.loads(line)
            if isinstance(obj, dict) and isinstance(obj.get("event"), dict) and "type" not in obj:
                obj = obj["event"]
            events.append(obj)
    if len(events) != limit:
        raise AssertionError(f"need {limit} events in TELEMETRY_SAMPLE, found {len(events)}")
    return events


def test_rejects_missing_or_wrong_token(tmp_path):
    with serving(tmp_path) as app:
        status, _ = _call(app, "POST", "/events", [_event()], token=None)
        assert status == 401
        status, body = _call(app, "POST", "/events", [_event()], token="nope")
        assert status == 401
        assert body["error"] == "unauthorized"
        status, _ = _call(app, "POST", "/events", [_event()], headers={"Authorization": "Token " + TOKEN})
        assert status == 401
    assert list(tmp_path.glob("events-*.jsonl")) == []


def test_single_object_array_and_bad_body(tmp_path):
    with serving(tmp_path) as app:
        status, body = _call(app, "POST", "/events", _event(1))
        assert status == 200
        assert body == {"accepted": 1}
        status, body = _call(app, "POST", "/events", [_event(2), _event(3)])
        assert status == 200
        assert body == {"accepted": 2}
        missing = _event(4)
        del missing["specversion"]
        status, _ = _call(app, "POST", "/events", [missing])
        assert status == 400
        status, _ = _call(app, "POST", "/events", [])
        assert status == 400
        status, _ = _call(app, "POST", "/events", b"not-json")
        assert status == 400
        status, _ = _call(app, "POST", "/nope", [_event()])
        assert status == 404
        status, _ = _call(app, "GET", "/events")
        assert status == 404
    rows = _lines(tmp_path)
    assert [row["event"]["id"] for row in rows] == ["evt-1", "evt-2", "evt-3"]
    stamp = rows[1]["received_at"]
    assert rows[2]["received_at"] == stamp
    assert stamp.endswith("+00:00")
    assert rows[0]["event"] == _event(1)


def test_health_reports_size_and_free_space(tmp_path):
    with serving(tmp_path) as app:
        status, health = _call(app, "GET", "/health")
        assert status == 200
        assert health["status"] == "ok"
        assert health["today_bytes"] == 0
        assert health["free_bytes"] > 0
        assert health["forward_failures"] == 0
        assert health["day"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _call(app, "POST", "/events", [_event()])
        _, health = _call(app, "GET", "/health")
        assert health["today_bytes"] > 0
        assert health["accepted"] == 1


def test_low_space_refuses_write_and_health(tmp_path):
    with serving(tmp_path, min_free_bytes=1) as app:
        app.free_bytes = lambda: 0
        status, body = _call(app, "POST", "/events", [_event()])
        assert status == 503
        assert body["error"] == "low space"
        status, health = _call(app, "GET", "/health")
        assert status == 200
        assert health["status"] == "low_space"
        assert health["free_bytes"] == 0
        assert health["today_bytes"] == 0
    assert list(tmp_path.glob("events-*.jsonl")) == []


def test_body_over_limit_is_413(tmp_path):
    assert receiver.MAX_BODY == 64 * 1024 * 1024
    with serving(tmp_path, max_body=64) as app:
        status, body = _call(app, "POST", "/events", b"x" * 80)
        assert status == 413
        assert body["error"] == "body too large"
    assert list(tmp_path.glob("events-*.jsonl")) == []


def test_allowed_sources_ip_and_cidr(tmp_path):
    with serving(tmp_path, allowed=("10.0.0.0/8",)) as app:
        status, _ = _call(app, "POST", "/events", [_event()])
        assert status == 403
    with serving(tmp_path, allowed=("127.0.0.0/8",)) as app:
        status, _ = _call(app, "POST", "/events", [_event()])
        assert status == 200
    assert receiver.ip_allowed("::ffff:10.1.2.3", ("10.1.2.3",))
    assert not receiver.ip_allowed("203.0.113.5", ("10.0.0.0/8",))


def test_forward_sends_array_with_key_and_failure_still_stores(tmp_path):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen["body"] = self.rfile.read(length)
            seen["key"] = self.headers.get("X-Telemetry-Key")
            seen["path"] = self.path
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/events"
        with serving(tmp_path, viewer_url=url, viewer_key="viewer-key") as app:
            batch = [_event(7), _event(8)]
            status, _ = _call(app, "POST", "/events", batch)
            assert status == 200
            deadline = time.time() + 3
            while time.time() < deadline and app.forwarder.stats()[0] < 1:
                time.sleep(0.02)
            sent, failures = app.forwarder.stats()
            assert sent == 1
            assert failures == 0
            assert seen["key"] == "viewer-key"
            assert seen["path"] == "/v1/events"
            assert json.loads(seen["body"]) == batch
    finally:
        server.shutdown()
        server.server_close()

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    refused = closed.getsockname()[1]
    closed.close()
    with serving(tmp_path, viewer_url=f"http://127.0.0.1:{refused}/events", viewer_key="viewer-key") as app:
        status, _ = _call(app, "POST", "/events", [_event(9)])
        assert status == 200
        deadline = time.time() + 3
        while time.time() < deadline and app.forwarder.stats()[1] < 1:
            time.sleep(0.02)
        assert app.forwarder.stats()[1] >= 1
        _, health = _call(app, "GET", "/health")
        assert health["forward_failures"] >= 1
    rows = _lines(tmp_path)
    assert [row["event"]["id"] for row in rows] == ["evt-7", "evt-8", "evt-9"]


def test_forward_does_not_block_the_post(tmp_path):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    held.settimeout(5)
    port = held.getsockname()[1]

    def _accept():
        try:
            conn, _ = held.accept()
            time.sleep(2)
            conn.close()
        except OSError:
            return

    threading.Thread(target=_accept, daemon=True).start()
    try:
        with serving(
            tmp_path, viewer_url=f"http://127.0.0.1:{port}/v1/events", forward_timeout=1.0
        ) as app:
            started = time.perf_counter()
            status, _ = _call(app, "POST", "/events", [_event()])
            elapsed = time.perf_counter() - started
            assert status == 200
            # A blocking forward would sit until forward_timeout (1s).
            assert elapsed < 0.4
    finally:
        held.close()


def test_stop_releases_the_port(tmp_path):
    app = receiver.Receiver(
        receiver.Config(token=TOKEN, data_dir=tmp_path, port=0, bind="127.0.0.1", min_free_bytes=0)
    )
    app.start()
    status, _ = _call(app, "GET", "/health")
    assert status == 200
    app.stop()
    app.stop()
    try:
        _call(app, "GET", "/health", timeout=1)
    except (urllib.error.URLError, TimeoutError, OSError):
        return
    raise AssertionError("health still answered after stop")


def test_rotation_mtime(tmp_path):
    now = time.time()
    today = datetime.fromtimestamp(now, timezone.utc).date()
    # Threshold 2: a 1-day-old name with a fresh mtime stays; a 2-day-old name
    # stays on the date alone (2 is not greater than 2) unless its mtime is older.
    gzip_after = 2
    fresh = tmp_path / f"events-{(today - timedelta(days=1)).isoformat()}.jsonl"
    fresh.write_bytes(b"fresh\n")
    os.utime(fresh, (now - 3600, now - 3600))

    by_mtime = tmp_path / f"events-{(today - timedelta(days=2)).isoformat()}.jsonl"
    by_mtime.write_bytes(b"by-mtime\n")
    os.utime(by_mtime, (now - 5 * _DAY, now - 5 * _DAY))

    by_name = tmp_path / f"events-{(today - timedelta(days=4)).isoformat()}.jsonl"
    by_name.write_bytes(b"by-name\n")
    os.utime(by_name, (now - 60, now - 60))  # fresh mtime, old name

    ancient = tmp_path / f"events-{(today - timedelta(days=200)).isoformat()}.jsonl"
    ancient.write_bytes(b"ancient\n")
    os.utime(ancient, (now - 200 * _DAY, now - 200 * _DAY))

    old_gz = tmp_path / f"events-{(today - timedelta(days=120)).isoformat()}.jsonl.gz"
    old_gz.write_bytes(gzip.compress(b"old-gz\n"))
    os.utime(old_gz, (now - 120 * _DAY, now - 120 * _DAY))

    live = tmp_path / f"events-{today.isoformat()}.jsonl"
    live.write_bytes(b"live\n")
    os.utime(live, (now - 200 * _DAY, now - 200 * _DAY))

    actions = receiver.run_maintenance(
        tmp_path, gzip_after_days=gzip_after, retention_days=90, now=now
    )
    assert fresh.exists()
    assert fresh.read_bytes() == b"fresh\n"
    assert not by_mtime.exists()
    assert not by_name.exists()
    assert (tmp_path / (by_mtime.name + ".gz")).exists()
    assert (tmp_path / (by_name.name + ".gz")).exists()
    assert gzip.decompress((tmp_path / (by_mtime.name + ".gz")).read_bytes()) == b"by-mtime\n"
    assert gzip.decompress((tmp_path / (by_name.name + ".gz")).read_bytes()) == b"by-name\n"
    assert not ancient.exists()
    assert not old_gz.exists()
    assert live.exists() and live.read_bytes() == b"live\n"
    assert f"gzip {by_mtime.name}" in actions
    assert f"gzip {by_name.name}" in actions
    assert f"delete {ancient.name}" in actions
    assert f"delete {old_gz.name}" in actions
    log = (tmp_path / "maintenance.log").read_text(encoding="utf-8")
    assert f"gzip {by_mtime.name}" in log
    assert f"delete {ancient.name}" in log

    again = receiver.run_maintenance(
        tmp_path, gzip_after_days=gzip_after, retention_days=90, now=now
    )
    assert not any(item.startswith("failed") for item in again)
    assert (tmp_path / (by_mtime.name + ".gz")).exists()
    preserved = (tmp_path / (by_mtime.name + ".gz")).stat().st_mtime
    assert abs(preserved - (now - 5 * 86400)) < 2


def test_real_events_round_trip(tmp_path):
    posted = _sample_events(200)
    with serving(tmp_path) as app:
        status, body = _call(app, "POST", "/events", posted, timeout=30)
        assert status == 200
        assert body == {"accepted": 200}
    rows = _lines(tmp_path)
    assert len(rows) == 200
    assert all(set(row) == {"received_at", "event"} for row in rows)
    assert len({row["received_at"] for row in rows}) == 1
    received = rows[0]["received_at"]
    assert received.endswith("+00:00")
    day = received[:10]
    assert (tmp_path / f"events-{day}.jsonl").exists()
    assert [row["event"] for row in rows] == posted


def test_load_config_names(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEMETRY_TOKEN", raising=False)
    try:
        receiver.load_config()
    except ValueError as exc:
        assert "TELEMETRY_TOKEN" in str(exc)
    else:
        raise AssertionError("missing token was accepted")
    monkeypatch.setenv("TELEMETRY_TOKEN", "abc")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOWED_SOURCES", "10.0.0.0/8, 127.0.0.1")
    monkeypatch.setenv("MIN_FREE_GB", "3")
    monkeypatch.setenv("GZIP_AFTER_DAYS", "4")
    monkeypatch.setenv("RETENTION_DAYS", "15")
    monkeypatch.setenv("VIEWER_URL", "http://viewer.example/v1/events")
    monkeypatch.setenv("VIEWER_KEY", "k")
    monkeypatch.setenv("PORT", "0")
    config = receiver.load_config()
    assert config.token == "abc"
    assert config.data_dir == tmp_path
    assert config.allowed == ("10.0.0.0/8", "127.0.0.1")
    assert config.min_free_bytes == 3 * 1024**3
    assert config.gzip_after_days == 4
    assert config.retention_days == 15
    assert config.port == 0
    assert config.forward_timeout == 2.0
    monkeypatch.setenv("RETENTION_DAYS", "11")
    assert receiver.load_config().retention_days == 11


def _raw_post(app, head: bytes, body: bytes = b"", timeout: float = 5) -> int:
    """Send a hand-built request; return the status the receiver answers with."""
    with socket.create_connection(("127.0.0.1", app.port), timeout=timeout) as conn:
        conn.sendall(head + b"\r\n" + body)
        reply = b""
        while b"\r\n" not in reply:
            chunk = conn.recv(4096)
            if not chunk:
                break
            reply += chunk
    return int(reply.split(b" ", 2)[1])


def _head(*lines: str) -> bytes:
    base = ["POST /events HTTP/1.1", "Host: 127.0.0.1", f"Authorization: Bearer {TOKEN}"]
    return "".join(line + "\r\n" for line in base + list(lines)).encode()


def test_max_body_mb_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEMETRY_TOKEN", "abc")
    monkeypatch.delenv("MAX_BODY_MB", raising=False)
    assert receiver.load_config().max_body == receiver.MAX_BODY
    monkeypatch.setenv("MAX_BODY_MB", "0.5")
    assert receiver.load_config().max_body == 512 * 1024
    for bad in ("0", "-1", "2048", "lots"):
        monkeypatch.setenv("MAX_BODY_MB", bad)
        with pytest.raises(ValueError):
            receiver.load_config()


def test_framing_that_cannot_be_trusted_is_refused(tmp_path):
    body = json.dumps([_event()]).encode()
    with serving(tmp_path) as app:
        assert _raw_post(app, _head("Transfer-Encoding: chunked"), b"0\r\n\r\n") == 411
        assert _raw_post(app, _head(f"Content-Length: {len(body)}", "Transfer-Encoding: chunked"), body) == 411
        assert _raw_post(app, _head(f"Content-Length: +{len(body)}"), body) == 400
        assert _raw_post(app, _head("Content-Length: -1")) == 400
        assert _raw_post(app, _head("Content-Length: 1e3")) == 400
        two = _head(f"Content-Length: {len(body)}", f"Content-Length: {len(body)}")
        assert _raw_post(app, two, body) == 400
        assert _raw_post(app, _head(f"Content-Length: {len(body)}"), body) == 200
    assert len(_lines(tmp_path)) == 1


def test_malformed_payloads_answer_400_and_the_server_stays_up(tmp_path):
    deep = b"[" * 100_000 + b"]" * 100_000
    huge_int = b'{"id": ' + b"9" * 50_000 + b"}"
    with serving(tmp_path, max_events=3) as app:
        for body in (
            deep,
            huge_int,
            b"\xff\xfe not utf",
            json.dumps([_event(1, id=7)]).encode(),
            json.dumps([_event(1, type="")]).encode(),
            json.dumps([_event(1, source=None)]).encode(),
            json.dumps([_event(i) for i in range(4)]).encode(),
            json.dumps([[_event()]]).encode(),
            b'"just a string"',
        ):
            status, _ = _call(app, "POST", "/events", body)
            assert status == 400, body[:40]
        status, body = _call(app, "POST", "/events", [_event(i) for i in range(3)])
        assert status == 200 and body == {"accepted": 3}
        assert _call(app, "GET", "/health")[0] == 200
    assert len(_lines(tmp_path)) == 3


def test_concurrent_bodies_are_bounded(tmp_path, monkeypatch):
    """A reader slot held by a slow body makes the next post wait, then answer 503."""
    monkeypatch.setattr(receiver, "MAX_READERS", 1)
    monkeypatch.setattr(receiver, "BUSY_WAIT", 0.3)
    body = json.dumps([_event()]).encode()
    with serving(tmp_path) as app:
        slow = socket.create_connection(("127.0.0.1", app.port), timeout=5)
        try:
            # Headers only: the handler takes the slot and waits for a body that is not sent.
            slow.sendall(_head(f"Content-Length: {len(body)}") + b"\r\n")
            deadline = time.monotonic() + 5
            while app._readers._value and time.monotonic() < deadline:
                time.sleep(0.01)
            status, answer = _call(app, "POST", "/events", [_event(2)])
            assert status == 503 and answer == {"error": "busy"}
            slow.sendall(body)
            assert int(slow.recv(4096).split(b" ", 2)[1]) == 200
        finally:
            slow.close()
        assert _call(app, "POST", "/events", [_event(3)])[0] == 200
    assert [row["event"]["id"] for row in _lines(tmp_path)] == ["evt-1", "evt-3"]


def test_runtime_env_keeps_only_what_the_receiver_reads(monkeypatch):
    honcho = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "LC_ALL": "C",
        "PYTHONUNBUFFERED": "1",
        "https_proxy": "http://proxy.example:3128",
        "AUTH_JWT_SECRET": "x",
        "DB_CONNECTION_URI": "x",
        "CACHE_URL": "x",
        "DEEPSEEK_API_KEY": "x",
        "RERANK_API_KEY": "x",
        "TELEMETRY_HEADERS": "x",
    }
    mine = {"TELEMETRY_TOKEN": "t", "VIEWER_KEY": "k", "PORT": "8765", "MAX_BODY_MB": "64", "DATA_DIR": "/data"}
    kept = receiver.runtime_env({**honcho, **mine})
    assert kept == {
        "PATH": "/usr/bin", "HOME": "/root", "LC_ALL": "C", "PYTHONUNBUFFERED": "1",
        "https_proxy": "http://proxy.example:3128", **mine,
    }
    calls = []
    monkeypatch.setattr(receiver.os, "execve", lambda *args: calls.append(args))
    monkeypatch.setattr(receiver.os, "environ", {**honcho, **mine})
    receiver.drop_unused_env()
    assert len(calls) == 1 and calls[0][0] == receiver.sys.executable and calls[0][2] == kept
    calls.clear()
    monkeypatch.setattr(receiver.os, "environ", dict(kept))
    receiver.drop_unused_env()
    assert calls == []  # the second start has nothing to drop, so it does not loop
