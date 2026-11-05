"""Fake provider endpoints and a fake CLI for the end-to-end model routing harness.

CaptureServer answers OpenAI-style chat completions and Anthropic Messages calls on 127.0.0.1 and records
method, path, the original upstream URL (header X-E2E-Original-URL, set by the bridge patch), header names with
auth values removed, and the JSON body. Nothing here talks to a real provider.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUTH_HEADERS = {"authorization", "x-api-key", "api-key", "x-goog-api-key"}
# Valid for every Honcho call shape used by the harness: JSON for the deriver's PromptRepresentation,
# plain text everywhere else.
ANSWER = '{"explicit": []}'


def scrub_headers(headers) -> dict:
    out = {}
    for name, value in headers.items():
        low = name.lower()
        if low in AUTH_HEADERS:
            out[low] = "<redacted:%d chars>" % len(value)
        elif low.startswith("x-e2e-"):
            continue
        else:
            out[low] = value
    return out


class CaptureServer:
    def __init__(self):
        self.records: list[dict] = []
        self.lock = threading.Lock()
        self.case = None
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = {"_raw": raw[:200].decode("utf-8", "replace")}
                record = {
                    "case": server.case,
                    "method": "POST",
                    "path": self.path,
                    "original_url": self.headers.get("X-E2E-Original-URL"),
                    "headers": scrub_headers(self.headers),
                    "body": body,
                    "t": time.time(),
                }
                with server.lock:
                    server.records.append(record)
                if self.path.endswith("/messages"):
                    reply = {
                        "id": "msg_" + uuid.uuid4().hex[:16],
                        "type": "message",
                        "role": "assistant",
                        "model": (body or {}).get("model", "fake"),
                        "content": [{"type": "text", "text": ANSWER}],
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                        "usage": {"input_tokens": 3, "output_tokens": 2},
                    }
                else:
                    reply = {
                        "id": "chatcmpl-" + uuid.uuid4().hex[:16],
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": (body or {}).get("model", "fake"),
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": ANSWER}}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                    }
                blob = json.dumps(reply).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def take(self, case) -> list[dict]:
        with self.lock:
            return [r for r in self.records if r["case"] == case]
