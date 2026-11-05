"""Time conclusion-list pages at several levels of client concurrency.

The bearer token is read at run time from the JSON file named by
VIEWER_SETTINGS_FILE (key ``token``). This file never contains the token and
never prints it. HONCHO_WORKSPACE names the workspace. HONCHO_API_URL, when
set, overrides ``base_url`` in that same JSON file.

Each level requests 32 pages (page size 100, the server maximum) of
``POST /v3/workspaces/{workspace}/conclusions/list`` and prints pages per
second from the wall clock. Levels are 1, 4, 8 and 16 requests in flight.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlencode

PAGE_COUNT = 32
PAGE_SIZE = 100
IN_FLIGHT = (1, 4, 8, 16)
REQUEST_TIMEOUT_S = 60.0


def read_settings(path: Path) -> tuple[str, str]:
    """Return ``(token, base_url)``. ``base_url`` may be empty."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        raise SystemExit("VIEWER_SETTINGS_FILE is missing or unreadable") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit("VIEWER_SETTINGS_FILE is not JSON") from None
    if not isinstance(data, dict):
        raise SystemExit("VIEWER_SETTINGS_FILE must be a JSON object")
    token = data.get("token")
    base_url = data.get("base_url") or ""
    if not isinstance(token, str) or not token.strip():
        raise SystemExit("settings file has no token")
    if not isinstance(base_url, str):
        raise SystemExit("settings file base_url is not a string")
    return token.strip(), base_url.strip()


def fetch_page(base_url: str, workspace: str, token: str, page: int, timeout: float) -> int:
    """POST one page. Return the HTTP status, or 0 when the connection fails.

    Failures stay numeric so a traceback cannot echo the token or the URL.
    """
    query = urlencode({"page": page, "size": PAGE_SIZE})
    path = f"/v3/workspaces/{quote(workspace, safe='')}/conclusions/list?{query}"
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=b"{}",
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            return int(response.status)
    except urllib.error.HTTPError as err:
        status = int(err.code)
        err.close()
        return status
    except Exception:
        return 0


def run_level(
    base_url: str,
    workspace: str,
    token: str,
    in_flight: int,
    pages: int = PAGE_COUNT,
    timeout: float = REQUEST_TIMEOUT_S,
) -> dict[str, float | int]:
    """Fetch ``pages`` pages with at most ``in_flight`` requests at once."""
    started = time.perf_counter()
    ok = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=in_flight) as pool:
        jobs = [
            pool.submit(fetch_page, base_url, workspace, token, page, timeout)
            for page in range(1, pages + 1)
        ]
        for job in jobs:
            if job.result() == 200:
                ok += 1
            else:
                errors += 1
    seconds = time.perf_counter() - started
    return {
        "in_flight": in_flight,
        "pages": pages,
        "ok": ok,
        "errors": errors,
        "seconds": seconds,
        "pages_per_s": pages / seconds if seconds else 0.0,
    }


def format_row(row: dict[str, float | int]) -> str:
    return (
        f"in_flight={row['in_flight']} pages={row['pages']} ok={row['ok']} "
        f"errors={row['errors']} seconds={row['seconds']:.3f} "
        f"pages_per_s={row['pages_per_s']:.2f}"
    )


def measure(
    base_url: str,
    workspace: str,
    token: str,
    levels: tuple[int, ...] = IN_FLIGHT,
    pages: int = PAGE_COUNT,
    timeout: float = REQUEST_TIMEOUT_S,
) -> list[dict[str, float | int]]:
    return [
        run_level(base_url, workspace, token, level, pages, timeout) for level in levels
    ]


def main() -> int:
    settings_path = os.environ.get("VIEWER_SETTINGS_FILE", "").strip()
    workspace = os.environ.get("HONCHO_WORKSPACE", "").strip()
    if not settings_path or not workspace:
        print("Set VIEWER_SETTINGS_FILE and HONCHO_WORKSPACE.", file=sys.stderr)
        return 2
    token, file_url = read_settings(Path(settings_path))
    base_url = os.environ.get("HONCHO_API_URL", "").strip() or file_url
    if not base_url:
        print("Set HONCHO_API_URL or base_url in the settings file.", file=sys.stderr)
        return 2
    rows = measure(base_url, workspace, token)
    for row in rows:
        print(format_row(row))
    if any(row["errors"] for row in rows):
        return 1
    return 0


def test_all_levels_request_every_page(tmp_path: Path, monkeypatch, capsys) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    token = "test-token"
    seen: list[tuple[str, str, str]] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            with lock:
                seen.append((self.command, self.path, self.headers.get("Authorization") or ""))
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"token": token, "base_url": "http://unused"}), encoding="utf-8")
    monkeypatch.setenv("VIEWER_SETTINGS_FILE", str(settings))
    monkeypatch.setenv("HONCHO_WORKSPACE", "ws")
    monkeypatch.setenv("HONCHO_API_URL", f"http://{host}:{port}")
    try:
        assert main() == 0
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert token not in out
    for level in IN_FLIGHT:
        assert f"in_flight={level} pages={PAGE_COUNT} ok={PAGE_COUNT} errors=0 " in out
    assert len(seen) == PAGE_COUNT * len(IN_FLIGHT)
    pages_by_level = [
        seen[i * PAGE_COUNT : (i + 1) * PAGE_COUNT] for i in range(len(IN_FLIGHT))
    ]
    for batch in pages_by_level:
        paths = [item[1] for item in batch]
        assert {item[0] for item in batch} == {"POST"}
        assert {item[2] for item in batch} == {f"Bearer {token}"}
        assert [path.split("?", 1)[0] for path in paths] == ["/v3/workspaces/ws/conclusions/list"] * PAGE_COUNT
        query = {path.split("?", 1)[1] for path in paths}
        assert query == {f"page={page}&size={PAGE_SIZE}" for page in range(1, PAGE_COUNT + 1)}


def test_in_flight_cap() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    lock = threading.Lock()
    state = {"current": 0, "max": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            with lock:
                state["current"] += 1
                state["max"] = max(state["max"], state["current"])
            try:
                time.sleep(0.03)
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with lock:
                    state["current"] -= 1

        def log_message(self, fmt: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        state["max"] = 0
        run_level(base, "ws", "test-token", 1, pages=8, timeout=5)
        assert state["max"] == 1
        state["max"] = 0
        run_level(base, "ws", "test-token", 4, pages=12, timeout=5)
        assert 2 <= state["max"] <= 4
    finally:
        server.shutdown()
        server.server_close()


def test_missing_environment(monkeypatch, capsys) -> None:
    monkeypatch.delenv("VIEWER_SETTINGS_FILE", raising=False)
    monkeypatch.delenv("HONCHO_WORKSPACE", raising=False)
    assert main() == 2
    assert "VIEWER_SETTINGS_FILE" in capsys.readouterr().err


def test_http_error_does_not_print_token(tmp_path: Path, monkeypatch, capsys) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    token = "super-secret-token-value"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = b"nope"
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"token": token, "base_url": ""}), encoding="utf-8")
    monkeypatch.setenv("VIEWER_SETTINGS_FILE", str(settings))
    monkeypatch.setenv("HONCHO_WORKSPACE", "ws")
    monkeypatch.setenv("HONCHO_API_URL", f"http://{host}:{port}")
    try:
        assert main() == 1
    finally:
        server.shutdown()
        server.server_close()
    captured = capsys.readouterr()
    assert token not in captured.out
    assert token not in captured.err
    assert "errors=32" in captured.out


def test_source_has_no_hardcoded_address() -> None:
    import re

    text = Path(__file__).read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z]:\\", text)
    addresses = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text))
    assert addresses <= {"127.0.0.1"}


if __name__ == "__main__":
    sys.exit(main())
