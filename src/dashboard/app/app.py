"""Dashboard for settings.json and the instruction files.

With CONTROL_PASSWORD set, every route needs that basic login (guard()). A save is checked against the schema, returned as a
diff, and written only when the caller sets apply. The write is a temp file in
the same directory plus rename. The previous file is copied to
backups/<kind>/.../name.bak_<YYYYMMDD_HHMMSS> in CONFIG_DIR (/config/backups in the container).
The newest 10 copies per file are kept, plus any younger than 7 days. The write is read back
and compared. On start, older bak files beside the instruction files are moved there.

settings.json and models.json are in CONFIG_DIR, mounted at /config. Honcho's config.toml is
bound as a single file at /honcho/config.toml. The instruction files are in
DATA_DIR, mounted at /data. Every log this process writes or reads is in LOGS_DIR, mounted at
/logs. The shipped templates (schemas, example, models seed, prompts) are mounted at /templates.

Instruction files use the same write. Dream and peer-card resolve per dream:
peers/<observer>, peers/<observed>, backends/<backend>, default, then the
database value. reasoning, summary and dialectic are written to the workspace
configuration. On start, a flat instruction name is moved into that tree when
the new file is absent.

Container listing goes through DOCKER_PROXY_URL. Restart, start and stop go
through DOCKER_PROXY_ACTION_URL, or DOCKER_PROXY_URL when that is unset. The
container names are the constant RESTART_ALLOWED. The log line records the socket peer.
"""
from __future__ import annotations

import base64
import gzip
import hmac
import json
import logging
import math
import os
import random
import zlib
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

_APP_DIR = str(Path(__file__).resolve().parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
import backups
import honcho_config

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

BODY_LIMIT = 1_000_000
FILE_CATEGORIES = ("dream", "peer-card")
WORKSPACE_KINDS = ("reasoning", "summary", "dialectic")
ALL_KINDS = FILE_CATEGORIES + WORKSPACE_KINDS
KIND_MODULE = {
    "dream": "dream",
    "peer-card": "peer_card",
    "reasoning": "reasoning",
    "summary": "summary",
    "dialectic": "dialectic",
}
SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|AIza|Bearer |eyJ)")


def _no_constant(name: str):
    """json.loads parse_constant: NaN and Infinity are not JSON and would reach the files."""
    raise ValueError(f"{name} is not allowed")
SECRET_NAME = re.compile(r"KEY|SECRET|PASSWORD|TOKEN|_URI$", re.IGNORECASE)
BACKEND_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
OLD_FLAT = re.compile(r"^(?P<who>default|[a-z][a-z0-9_-]*)-(?P<kind>dream|peer-card)-instructions\.md$")
BAK_NAME = re.compile(r"^settings\.json\.bak(?:-\d{8}T\d{15}(?:-\d+)?|_\d{8}_\d{6}(?:_\d+)?)$")
WORKSPACE_BAK = re.compile(r"^_workspace-config\.bak-\d{8}T\d{15}(?:-\d+)?\.json$")
TOKEN_CAP_LIMIT = 2000
PROXY_ACTION_TIMEOUT = 60
PROXY_READ_TIMEOUT = 10
CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# The containers the dashboard may restart, stop or start, as Docker names them.
RESTART_ALLOWED = (
    "honcho-api-1",
    "honcho-deriver-1",
    "honcho-tools-bridge-1",
    "honcho-tools-scheduler-1",
    "honcho-tools-telemetry-1",
    "honcho-tools-backup-1",
    "honcho-tools-dashboard-1",
)
# The stack's LOGS_DIR inside the container.
LOGS = Path("/logs")

CONTROL_MODULES = ("dream", "reasoning", "summary")
BAK_ANY = re.compile(r"^(?P<file>.+?)\.bak(?:-\d{8}T\d{15}(?:-\d+)?(?:\.json)?|_\d{8}_\d{6}(?:_\d+)?)$")
WORKSPACE_FILE = "workspace-config.json"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
        migrate_instructions(instructions_dir())
    except Exception:
        _instruction_logger.info("instruction migration failed")
    try:
        move_backups()
    except Exception:
        _instruction_logger.info("backup move failed")
    yield


app = FastAPI(title="dashboard", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
_schema_cache: dict = {"mtime": None, "validator": None}

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SECURITY_HEADERS = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}


def login_required() -> bool:
    """A login is required when CONTROL_PASSWORD is set. Blank or unset leaves the page open."""
    return bool((os.environ.get("CONTROL_PASSWORD") or "").strip())


def authorized(header: str | None) -> bool:
    """HTTP basic login against CONTROL_USER (default admin) and CONTROL_PASSWORD, in constant time."""
    user = (os.environ.get("CONTROL_USER") or "").strip() or "admin"
    password = (os.environ.get("CONTROL_PASSWORD") or "").strip()
    if not password or not header:
        return False
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token:
        return False
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except Exception:
        return False
    got_user, sep, got_password = decoded.partition(":")
    if not sep:
        return False
    return hmac.compare_digest(got_user.encode(), user.encode()) & hmac.compare_digest(
        got_password.encode(), password.encode()
    )


def _plain(status: int, text: str, headers: dict | None = None) -> Response:
    return Response(status_code=status, content=text, media_type="text/plain", headers={**SECURITY_HEADERS, **(headers or {})})


@app.middleware("http")
async def guard(request: Request, call_next):
    """Login when a password is set, JSON-only writes, no framing.

    A write must carry Content-Type application/json: a form or a no-cors fetch from another site
    cannot send it without a preflight, which this app never grants. That holds with or without
    the login.
    """
    if login_required() and not authorized(request.headers.get("authorization")):
        return _plain(401, "unauthorized", {"WWW-Authenticate": 'Basic realm="dashboard"'})
    if request.method in WRITE_METHODS:
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return _plain(415, "writes must be application/json")
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)
_restart_log_lock = threading.Lock()
_restart_logger = logging.getLogger("dashboard.restart")
if not _restart_logger.handlers:
    _restart_handler = logging.StreamHandler(sys.stdout)
    _restart_handler.setFormatter(logging.Formatter("%(message)s"))
    _restart_logger.addHandler(_restart_handler)
    _restart_logger.setLevel(logging.INFO)
    _restart_logger.propagate = False
_instruction_logger = logging.getLogger("dashboard.instructions")
if not _instruction_logger.handlers:
    _instruction_handler = logging.StreamHandler(sys.stdout)
    _instruction_handler.setFormatter(logging.Formatter("%(message)s"))
    _instruction_logger.addHandler(_instruction_handler)
    _instruction_logger.setLevel(logging.INFO)
    _instruction_logger.propagate = False


def settings_path() -> Path:
    return Path(os.environ.get("CONTROL_FILE") or "/config/settings.json")


def config_dir() -> Path:
    """The stack's CONFIG_DIR in the container: the folder that holds settings.json."""
    return settings_path().parent


def data_dir() -> Path:
    """The stack's DATA_DIR in the container: instructions/, nothing else."""
    return Path(os.environ.get("DATA_DIR") or "/data")


def instructions_dir() -> Path:
    env = os.environ.get("INSTRUCTIONS_DIR")
    if env:
        return Path(env)
    return data_dir() / "instructions"


def backups_root() -> Path:
    """backups/ in CONFIG_DIR: settings/, instructions/<relative dir>/, workspace/, models/, config-toml/."""
    return config_dir() / "backups"


def settings_bak_dir() -> Path:
    return backups_root() / "settings"


def workspace_bak_dir() -> Path:
    return backups_root() / "workspace"


def instruction_bak_dir(path: Path) -> Path:
    """backups/instructions/<directory of the file relative to the instructions folder>."""
    directory = instructions_dir()
    try:
        relative = Path(path).resolve().parent.relative_to(directory.resolve())
    except (OSError, ValueError):
        relative = Path()
    return backups_root() / "instructions" / relative


def _move_one(item: Path, dest_dir: Path, lines: list[str], shown: str) -> None:
    dest = dest_dir / item.name
    if dest.exists():
        line = f"backup move skipped {shown} because the destination exists"
    else:
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(dest))
            line = f"moved backup {shown}"
        except OSError:
            line = f"backup move failed {shown}"
    _instruction_logger.info("%s", line)
    lines.append(line)


def move_backups() -> list[str]:
    """Move bak copies that sit beside the instruction files into backups/. Nothing is deleted.

    The scheduler writes its settings.json.bak-* straight into backups/settings, so nothing beside
    settings.json is moved.
    """
    lines: list[str] = []
    directory = instructions_dir()
    if not directory.is_dir():
        return lines
    root = backups_root().resolve()
    for item in sorted(directory.rglob("*.bak-*")):
        if not item.is_file() or not BAK_ANY.match(item.name):
            continue
        try:
            item.resolve().relative_to(root)
            continue
        except ValueError:
            pass
        shown = relative_posix(directory, item)
        if item.parent.resolve() == directory.resolve() and WORKSPACE_BAK.match(item.name):
            _move_one(item, workspace_bak_dir(), lines, shown)
        else:
            _move_one(item, instruction_bak_dir(item), lines, shown)
    return lines


def migrate_instructions(directory: Path) -> list[str]:
    """Move flat instruction names into the tree when the new file is absent.

    default-dream-instructions.md becomes default/dream.md. <backend>-<kind>-instructions.md
    becomes backends/<backend>/<kind>.md. A file that is already in the tree is left in place.
    """
    lines: list[str] = []
    if not directory.is_dir():
        return lines
    for item in sorted(directory.iterdir(), key=lambda path: path.name):
        if not item.is_file():
            continue
        match = OLD_FLAT.match(item.name)
        if not match:
            continue
        who = match.group("who")
        kind = match.group("kind")
        if who == "default":
            dest = directory / "default" / f"{kind}.md"
            shown = f"default/{kind}.md"
        else:
            dest = directory / "backends" / who / f"{kind}.md"
            shown = f"backends/{who}/{kind}.md"
        if dest.exists():
            line = f"instruction migration skipped {item.name} because {shown} exists"
            _instruction_logger.info("%s", line)
            lines.append(line)
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(item, dest)
        except OSError:
            line = f"instruction migration failed file={item.name}"
            _instruction_logger.info("%s", line)
            lines.append(line)
            continue
        line = f"migrated {item.name} -> {shown}"
        _instruction_logger.info("%s", line)
        lines.append(line)
    return lines


def _beside_or_repo(name: str) -> Path:
    beside = Path(__file__).resolve().with_name(name)
    if beside.is_file():
        return beside
    return Path(__file__).resolve().parents[2] / "config" / name


def schema_path() -> Path:
    env = os.environ.get("SCHEMA_FILE")
    if env:
        return Path(env)
    return _beside_or_repo("settings.schema.json")


def example_path() -> Path:
    env = os.environ.get("EXAMPLE_FILE")
    if env:
        return Path(env)
    return _beside_or_repo("settings.example.json")


def policy_log_path() -> Path:
    return Path(os.environ.get("POLICY_LOG") or LOGS / "policy-used.log")


def scheduler_log_path() -> Path:
    return Path(os.environ.get("SCHEDULER_LOG") or LOGS / "dream-nas.log")


def hook_status_path() -> Path:
    return Path(os.environ.get("POLICY_STATUS") or LOGS / "hook-status.json")


def telemetry_dir() -> Path:
    return Path(os.environ.get("TELEMETRY_DIR") or "/telemetry")


def instruction_cap() -> int:
    """DeriverSettings.MAX_CUSTOM_INSTRUCTIONS_TOKENS. The field cannot exceed 2000."""
    raw = os.environ.get("INSTRUCTION_TOKEN_CAP", "").strip()
    if not raw:
        return TOKEN_CAP_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return TOKEN_CAP_LIMIT
    if value < 0 or value > TOKEN_CAP_LIMIT:
        return TOKEN_CAP_LIMIT
    return value


def token_count(text: str | None) -> int:
    """Same fallback the deriver uses when its tokenizer cannot be imported: 4 characters per token."""
    if not text:
        return 0
    return len(text) // 4


def _secret(value) -> bool:
    if isinstance(value, str):
        return SECRET.search(value) is not None
    if isinstance(value, dict):
        return any(_secret(key) or _secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_secret(item) for item in value)
    return False


def public(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if SECRET.search(text):
        return "<redacted>"
    return value


def validator() -> Draft202012Validator:
    path = schema_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError as exc:
        raise HTTPException(status_code=500, detail="schema file is missing") from exc
    if _schema_cache["mtime"] == mtime and _schema_cache["validator"] is not None:
        return _schema_cache["validator"]
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        raise HTTPException(status_code=500, detail="schema file is invalid") from exc
    compiled = Draft202012Validator(schema)
    _schema_cache["mtime"] = mtime
    _schema_cache["validator"] = compiled
    return compiled


def schema_errors(data) -> list[str]:
    lines = []
    for error in sorted(validator().iter_errors(data), key=lambda item: list(item.path)):
        where = "/" + "/".join(str(part) for part in error.path)
        message = error.message
        if SECRET.search(message):
            message = "rejected"
        lines.append(f"schema {where or '/'}: {message}")
    return lines


def _split_ref(ref: str) -> tuple[str, str]:
    name, sep, model = ref.partition(":")
    return name, model if sep else ""


def reference_errors(data: dict) -> list[str]:
    """Backend names must exist; model overrides obey an explicit models list, when present.

    A name that is referenced and not defined is one line listing every reference.
    """
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return []
    backends = providers.get("backends")
    if not isinstance(backends, dict):
        return []
    lines = []
    for name, block in backends.items():
        models = block.get("models") if isinstance(block, dict) else None
        if isinstance(models, list) and isinstance(block, dict) and block.get("model") not in models:
            lines.append(f"providers.backends.{name}.model is not in models")
    missing: dict[str, list[str]] = {}

    def note(where: str, ref) -> None:
        if ref is None or not isinstance(ref, str) or not ref:
            return
        name, model = _split_ref(ref)
        backend = backends.get(name)
        if not isinstance(backend, dict):
            bucket = missing.setdefault(name, [])
            if where not in bucket:
                bucket.append(where)
            return
        if model:
            models = backend.get("models")
            if isinstance(models, list) and model not in models:
                lines.append(f"{where}: {public(model)} is not in models")

    active = providers.get("active")
    if isinstance(active, str):
        note("providers.active", active)
        chosen = backends.get(_split_ref(active)[0]) if active else None
        if isinstance(chosen, dict) and chosen.get("enabled") is False:
            lines.append("providers.active is disabled")
    defaults = data.get("defaults")
    if isinstance(defaults, dict):
        note("defaults.backend", defaults.get("backend"))
    stages = providers.get("stages")
    if isinstance(stages, dict):
        for stage, ref in stages.items():
            note(f"providers.stages.{stage}", ref.get("model") if isinstance(ref, dict) else ref)
    for section in ("peers", "pairs"):
        items = data.get(section)
        if not isinstance(items, dict):
            continue
        for key, block in items.items():
            if isinstance(block, dict):
                note(f"{section}.{key}.backend", block.get("backend"))
    for name, wheres in missing.items():
        lines.append(f"backend {public(name)} is not defined; referenced by {', '.join(wheres)}")
    return lines


def check_settings(data) -> list[str]:
    if not isinstance(data, dict):
        return ["settings must be an object"]
    if _secret(data):
        return ["secret-like string"]
    errors = schema_errors(data)
    if errors:
        return errors
    return reference_errors(data) + rerank_cap_errors(data)


def rerank_cap_errors(data: dict) -> list[str]:
    """A dream may lower the global request cap (the top-level rerank block), but cannot raise it."""
    cap = (data.get("rerank") or {}).get("daily_request_cap") if isinstance(data.get("rerank"), dict) else None
    if not isinstance(cap, int) or isinstance(cap, bool):
        return []
    blocks = [("defaults", data.get("defaults") or {})]
    for section in ("peers", "pairs"):
        blocks.extend((f"{section}.{name}", block) for name, block in (data.get(section) or {}).items())
    errors = []
    for where, block in blocks:
        value = (block.get("rerank") or {}).get("daily_request_cap")
        if value is not None and value > cap:
            errors.append(f"{where}.rerank.daily_request_cap: cannot exceed the global cap ({cap})")
    return errors


def diff_json(before, after, path: str = "") -> list[dict]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        rows = []
        for key in dict.fromkeys([*before, *after]):
            child = f"{path}/{key}"
            if key not in before:
                rows.append({"path": child, "op": "add", "before": None, "after": public(after[key])})
            elif key not in after:
                rows.append({"path": child, "op": "remove", "before": public(before[key]), "after": None})
            else:
                rows.extend(diff_json(before[key], after[key], child))
        return rows
    return [{"path": path or "/", "op": "replace", "before": public(before), "after": public(after)}]


_mtime_not_checked = object()


def atomic_write_text(path: Path, text: str, *, bak_dir: Path | None = None, seen_mtime: object = _mtime_not_checked) -> str | None:
    """Copy the previous file into bak_dir (rotated), then temp file and rename, then read back and compare."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bak_name = backups.backup_file(path, Path(bak_dir) if bak_dir is not None else path.parent)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            # On disk before the rename: after a power cut the file is the old or the new one, not empty.
            os.fsync(handle.fileno())
        if seen_mtime is not _mtime_not_checked and file_mtime(path) != seen_mtime:
            raise HTTPException(status_code=409, detail="the file changed; reload and review the diff again")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    try:
        written = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="the written file cannot be read back") from exc
    if written != text:
        raise HTTPException(status_code=500, detail="the written file does not match; the backup is " + str(bak_name))
    return bak_name


def dump_settings(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def file_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def load_example() -> dict:
    try:
        data = json.loads(example_path().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


_settings_missing_logged = {"value": False}


def read_settings() -> tuple[dict | None, dict]:
    path = settings_path()
    if not path.is_file():
        # Tolerated: the page shows the example's defaults until a first save. Logged once.
        if not _settings_missing_logged["value"]:
            _instruction_logger.info("settings.json missing at %s; showing the defaults", path)
            _settings_missing_logged["value"] = True
        return load_example(), {"missing": True, "mtime_ns": None, "errors": []}
    _settings_missing_logged["value"] = False
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="settings file cannot be read") from exc
    if SECRET.search(text):
        return None, {"missing": False, "mtime_ns": file_mtime(path), "errors": ["secret-like string"]}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, {"missing": False, "mtime_ns": file_mtime(path), "errors": ["invalid json"]}
    if not isinstance(data, dict):
        return None, {"missing": False, "mtime_ns": file_mtime(path), "errors": ["settings must be an object"]}
    return data, {"missing": False, "mtime_ns": file_mtime(path), "errors": check_settings(data)}


def relaxed_settings() -> dict:
    data, meta = read_settings()
    if isinstance(data, dict) and not meta.get("missing"):
        return data
    if meta.get("missing") and isinstance(data, dict):
        return data
    path = settings_path()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_mtime(sent) -> int | None:
    if sent is None:
        return None
    if isinstance(sent, bool):
        raise HTTPException(status_code=400, detail="mtime_ns must be an integer or null")
    if isinstance(sent, int) or (isinstance(sent, str) and sent.isdigit()):
        return int(sent)
    raise HTTPException(status_code=400, detail="mtime_ns must be an integer or null")


def check_mtime(path: Path, payload: dict) -> None:
    """Skip the check when the caller omits mtime_ns. A sent value must match the file."""
    if "mtime_ns" not in payload:
        return
    if file_mtime(path) != parse_mtime(payload.get("mtime_ns")):
        raise HTTPException(status_code=409, detail="the file changed; reload and review the diff again")


def assert_clean(path: Path) -> None:
    """Refuse to copy a file that already contains a secret-like string."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="file cannot be read") from exc
    if SECRET.search(text):
        raise HTTPException(status_code=400, detail="secret-like string")


def backend_blocks(data) -> dict:
    """providers.backends when the file has that shape, else {}. A hand-edited file may not."""
    providers = data.get("providers") if isinstance(data, dict) else None
    backends = providers.get("backends") if isinstance(providers, dict) else None
    return backends if isinstance(backends, dict) else {}


def backend_names(data: dict) -> list[str]:
    providers = data.get("providers") if isinstance(data, dict) else None
    backends = providers.get("backends") if isinstance(providers, dict) else None
    if not isinstance(backends, dict):
        return []
    return [name for name in backends if isinstance(name, str) and BACKEND_NAME.match(name)]


def safe_peer_name(name) -> bool:
    """One path segment. Rejects traversal, a drive prefix, and a secret-like name."""
    if not isinstance(name, str) or not name or name in {".", ".."} or name.startswith("."):
        return False
    # A path segment is at most 255 bytes; a longer name made the file calls fail with 500.
    if len(name.encode("utf-8")) > 200:
        return False
    if any(char in name for char in "/\\:\0"):
        return False
    if any(ord(char) < 32 for char in name):
        return False
    if SECRET.search(name):
        return False
    return Path(name).name == name


def contained(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _file_under(directory: Path, relative: Path) -> Path | None:
    if not directory.is_dir():
        return None
    candidate = directory / relative
    if not contained(directory, candidate) or not candidate.is_file():
        return None
    return candidate


def target_relative(scope: str, name: str | None, category: str) -> str:
    if scope == "default":
        return f"default/{category}.md"
    if scope == "backend":
        return f"backends/{name}/{category}.md"
    return f"peers/{name}/{category}.md"


def relative_posix(directory: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(directory.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _backend_key(ref) -> str | None:
    if not isinstance(ref, str) or not ref:
        return None
    name, _model = _split_ref(ref)
    if not BACKEND_NAME.match(name):
        return None
    return name


def active_backend_name(data: dict) -> str | None:
    providers = data.get("providers") if isinstance(data, dict) else None
    active = providers.get("active") if isinstance(providers, dict) else None
    return _backend_key(active)


def peer_backend_name(data: dict, peer: str) -> str | None:
    """Peer backend, else defaults.backend, else the active backend. The part before :model."""
    peers = data.get("peers") if isinstance(data, dict) else None
    block = peers.get(peer) if isinstance(peers, dict) else None
    ref = block.get("backend") if isinstance(block, dict) else None
    name = _backend_key(ref)
    if name:
        return name
    defaults = data.get("defaults") if isinstance(data, dict) else None
    name = _backend_key(defaults.get("backend") if isinstance(defaults, dict) else None)
    if name:
        return name
    return active_backend_name(data)


def locate_instruction(directory: Path, backend_name: str | None, category: str, observer: str | None, observed: str | None):
    """Same chain as the hook. Returns path, source, resolution, and the flat name when that file won."""
    if not directory.is_dir():
        return None
    checks = []
    if safe_peer_name(observer):
        checks.append((Path("peers") / observer / f"{category}.md", f"settings.peer.{observer}.{category}", "peer", None))
    if safe_peer_name(observed) and observed != observer:
        checks.append((Path("peers") / observed / f"{category}.md", f"settings.peer.{observed}.{category}", "peer", None))
    if backend_name and BACKEND_NAME.match(backend_name):
        flat = f"{backend_name}-{category}-instructions.md"
        checks.append((Path("backends") / backend_name / f"{category}.md", f"settings.backend.{backend_name}.{category}", "backend", None))
        checks.append((Path(flat), f"settings.backend.{backend_name}.{category}", "backend", flat))
    flat_default = f"default-{category}-instructions.md"
    checks.append((Path("default") / f"{category}.md", f"settings.default.{category}", "default", None))
    checks.append((Path(flat_default), f"settings.default.{category}", "default", flat_default))
    for relative, source, resolution, flat in checks:
        path = _file_under(directory, relative)
        if path is not None:
            return path, source, resolution, flat
    return None


def default_winner(directory: Path, category: str):
    """The shared fallback only. A backend file does not show up on the default row."""
    if not directory.is_dir():
        return None
    path = _file_under(directory, Path("default") / f"{category}.md")
    if path is not None:
        return path, f"settings.default.{category}", "default", None
    flat = f"default-{category}-instructions.md"
    path = _file_under(directory, Path(flat))
    if path is not None:
        return path, f"settings.default.{category}", "default", flat
    return None


def list_baks(path: Path) -> list[str]:
    directory = instruction_bak_dir(path)
    if not directory.is_dir():
        return []
    prefix = path.name + ".bak"
    names = [item.name for item in directory.glob(prefix + "*") if item.is_file() and BAK_ANY.match(item.name)
             and BAK_ANY.match(item.name).group("file") == path.name]
    names.sort(reverse=True)
    return names


def describe_instruction(directory: Path, scope: str, name: str | None, category: str, backend: str | None) -> dict:
    """One editable file, plus the file the hook would actually apply when this one is absent."""
    target = target_relative(scope, name, category)
    target_path = directory / target
    mtime = file_mtime(target_path) if target_path.is_file() else None
    row = {
        "scope": scope,
        "name": name,
        "backend": "default" if scope == "default" else (name if scope == "backend" else None),
        "peer": name if scope == "peer" else None,
        "category": category,
        "resolution": "database",
        "file": None,
        "target": target,
        "source": "database",
        "text": "",
        "empty": False,
        "tokens": None,
        "over": False,
        "hidden": False,
        "mtime_ns": None if mtime is None else str(mtime),
        "backups": list_baks(target_path),
        "informational": False,
    }
    if scope == "default":
        winner = default_winner(directory, category)
    else:
        observer = name if scope == "peer" else None
        winner = locate_instruction(directory, backend, category, observer, None)
    if winner is None:
        return row
    path, source, resolution, _flat = winner
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return row
    row["file"] = relative_posix(directory, path)
    row["resolution"] = resolution
    row["source"] = source
    if SECRET.search(text):
        row["hidden"] = True
        return row
    cleared = not text.strip()
    count = 0 if cleared else token_count(text)
    row["text"] = text
    row["empty"] = cleared
    row["tokens"] = count
    row["over"] = (not cleared) and count > instruction_cap()
    return row


def configured_peers(data: dict) -> list[str]:
    peers = data.get("peers") if isinstance(data, dict) else None
    if not isinstance(peers, dict):
        return []
    return [name for name in peers if safe_peer_name(name)]


def module_text(config: dict, kind: str) -> str:
    block = config.get(KIND_MODULE[kind])
    if not isinstance(block, dict):
        return ""
    value = block.get("custom_instructions")
    return value if isinstance(value, str) else ""


def module_state(config: dict, kind: str) -> str:
    """missing (Honcho falls back to the top-level text), empty (explicit none) or set."""
    block = config.get(KIND_MODULE[kind])
    value = block.get("custom_instructions") if isinstance(block, dict) else None
    if not isinstance(value, str):
        return "missing"
    return "set" if value else "empty"


def with_kind_text(config: dict, kind: str, text: str | None) -> dict:
    """None removes custom_instructions from the module block, so Honcho falls back to the top-level text.

    Honcho merges top-level configuration keys on update and cannot drop one, so the block stays, possibly empty.
    """
    updated = json.loads(json.dumps(config))
    module = KIND_MODULE[kind]
    block = updated.get(module)
    block = dict(block) if isinstance(block, dict) else {}
    if text is None:
        if module in updated:
            block.pop("custom_instructions", None)
            updated[module] = block
        return updated
    block["custom_instructions"] = text
    updated[module] = block
    return updated


def workspace_configuration() -> tuple[dict | None, str | None]:
    """POST /workspaces (get-or-create) and return the configuration object."""
    if not workspace_name():
        return None, "HONCHO_WORKSPACE is unset"
    if not os.environ.get("HONCHO_API_TOKEN", "").strip():
        return None, "HONCHO_API_TOKEN is unset"
    parsed, error = api_json("POST", "/workspaces", {"id": workspace_name()})
    if error:
        return None, error
    if not isinstance(parsed, dict):
        return None, "Honcho API unavailable"
    config = parsed.get("configuration")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        return None, "Honcho API unavailable"
    return config, None


def informational_files(directory: Path, names: list[str]) -> list[dict]:
    """reasoning, summary and dialectic files. Honcho does not read them."""
    found = []
    slots = [("default", None)] + [("backend", name) for name in names]
    for kind in WORKSPACE_KINDS:
        for scope, name in slots:
            relative = target_relative(scope, name, kind)
            path = _file_under(directory, Path(relative))
            if path is None:
                continue
            item = {"kind": kind, "path": relative, "hidden": False, "tokens": None, "empty": False, "over": False}
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if SECRET.search(text):
                item["hidden"] = True
                found.append(item)
                continue
            cleared = not text.strip()
            count = 0 if cleared else token_count(text)
            item["empty"] = cleared
            item["tokens"] = count
            item["over"] = (not cleared) and count > instruction_cap()
            found.append(item)
    return found


def instruction_rows(extra_peer: str | None = None) -> dict:
    directory = instructions_dir()
    data = relaxed_settings()
    names = backend_names(data)
    peers = configured_peers(data)
    if extra_peer:
        if not safe_peer_name(extra_peer):
            raise HTTPException(status_code=400, detail="peer name is not allowed")
        if extra_peer not in peers:
            peers.append(extra_peer)
    rows = []
    for category in FILE_CATEGORIES:
        rows.append(describe_instruction(directory, "default", None, category, None))
        for name in names:
            rows.append(describe_instruction(directory, "backend", name, category, name))
    for peer in peers:
        backend = peer_backend_name(data, peer)
        for category in FILE_CATEGORIES:
            rows.append(describe_instruction(directory, "peer", peer, category, backend))
    defaults = {}
    for category in FILE_CATEGORIES:
        defaults[category] = default_winner(directory, category) is not None
    config, error = workspace_configuration()
    database = {kind: None for kind in ALL_KINDS}
    database["shared"] = None
    database_state = {}
    if config is not None:
        for kind in ALL_KINDS:
            database[kind] = module_text(config, kind)
            database_state[kind] = module_state(config, kind)
        shared = config.get("custom_instructions")
        database["shared"] = shared if isinstance(shared, str) else ""
    active = active_backend_name(data)
    override = {}
    for category in FILE_CATEGORIES:
        winner = locate_instruction(directory, active, category, None, None)
        if winner is None:
            override[category] = {"file": None, "resolution": "database", "source": "database"}
        else:
            path, source, resolution, _flat = winner
            override[category] = {"file": relative_posix(directory, path), "resolution": resolution, "source": source}
    return {
        "cap": instruction_cap(),
        "folder": directory.is_dir(),
        "defaults": defaults,
        "rows": rows,
        "database": database,
        "database_state": database_state,
        "switched_off": sorted(c for c in FILE_CATEGORIES if (directory / "default" / f"{c}.md{OFF_SUFFIX}").is_file()),
        "workspace_error": error,
        "override": override,
        "informational": informational_files(directory, names),
    }


def instruction_target(scope: str, name: str | None, category: str) -> Path:
    if category not in FILE_CATEGORIES:
        raise HTTPException(status_code=400, detail="category is not allowed")
    directory = instructions_dir()
    if scope == "default":
        relative = Path("default") / f"{category}.md"
    elif scope == "backend":
        if not isinstance(name, str) or not BACKEND_NAME.match(name):
            raise HTTPException(status_code=400, detail="backend name is not allowed")
        relative = Path("backends") / name / f"{category}.md"
    elif scope == "peer":
        if not safe_peer_name(name):
            raise HTTPException(status_code=400, detail="peer name is not allowed")
        relative = Path("peers") / name / f"{category}.md"
    else:
        raise HTTPException(status_code=400, detail="scope is not allowed")
    candidate = directory / relative
    if not contained(directory, candidate):
        raise HTTPException(status_code=400, detail="instruction file name is not allowed")
    return candidate


def scope_parts(payload: dict) -> tuple[str, str | None, str]:
    scope = payload.get("scope")
    category = payload.get("category")
    name = payload.get("name")
    if not isinstance(scope, str):
        raise HTTPException(status_code=400, detail="scope is not allowed")
    if not isinstance(category, str):
        category = ""
    if scope == "default":
        name = None
    elif not isinstance(name, str):
        name = None
    return scope, name, category


def read_text_file(path: Path, missing: str) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=missing) from exc


def instruction_result(label: str, text: str, before: str) -> dict:
    count = token_count(text)
    cap = instruction_cap()
    return {
        "ok": True,
        "name": label,
        "target": label,
        "text": text,
        "changed": text != before,
        "diff": [] if text == before else [{"path": label, "op": "replace", "before": before, "after": text}],
        "tokens": count,
        "cap": cap,
        "over": bool(text) and count > cap,
        "empty": not text.strip(),
        "bak": False,
    }


def apply_instruction_text(path: Path, text: str, before: str, apply: bool, result: dict) -> dict:
    if apply and text != before:
        bak_name = atomic_write_text(path, text, bak_dir=instruction_bak_dir(path))
        result["bak"] = bak_name is not None
        written = file_mtime(path)
        result["mtime_ns"] = None if written is None else str(written)
    return result


def named_instruction_file(relative: Path, flat_name: str | None) -> Path | None:
    directory = instructions_dir()
    path = _file_under(directory, relative)
    if path is not None:
        return path
    if flat_name:
        return _file_under(directory, Path(flat_name))
    return None


def write_workspace_bak(config: dict) -> str:
    """Previous workspace configuration as backups/workspace/workspace-config.json.bak_<stamp>, rotated."""
    text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    return backups.backup_text(text, workspace_bak_dir(), WORKSPACE_FILE)


def tail_lines(path: Path, count: int = 50, max_bytes: int = 256_000) -> list[str] | None:
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            raw = handle.read()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if SECRET.search(text):
        return ["(log hidden)"]
    lines = text.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    return lines[-count:]


def telemetry_status() -> dict:
    directory = telemetry_dir()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = directory / f"events-{day}.jsonl"
    if not path.is_file():
        gzipped = directory / f"events-{day}.jsonl.gz"
        if gzipped.is_file():
            return {"file": gzipped.name, "bytes": gzipped.stat().st_size, "last_event": None, "note": "gzip"}
        return {"file": path.name, "bytes": None, "last_event": None, "note": "missing"}
    try:
        size = path.stat().st_size
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return {"file": path.name, "bytes": None, "last_event": None, "note": "unreadable"}
    last = None
    lines = tail_lines(path, 1)
    if lines:
        try:
            item = json.loads(lines[-1])
            if isinstance(item, dict) and isinstance(item.get("received_at"), str):
                last = item["received_at"]
        except json.JSONDecodeError:
            last = None
    if last is None:
        last = mtime
    return {"file": path.name, "bytes": size, "last_event": last, "note": None}


def api_base() -> str:
    explicit = os.environ.get("HONCHO_API", "").strip()
    if explicit:
        return explicit.rstrip("/")
    url = os.environ.get("HONCHO_API_URL", "http://api:8000").strip().rstrip("/")
    if url.endswith("/v3"):
        return url
    return url + "/v3"


def workspace_name() -> str:
    return os.environ.get("HONCHO_WORKSPACE", "").strip()


def api_json(method: str, path: str, body: dict | None = None):
    token = os.environ.get("HONCHO_API_TOKEN", "").strip()
    if not workspace_name():
        return None, "HONCHO_WORKSPACE is unset"
    if not token:
        return None, "HONCHO_API_TOKEN is unset"
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(api_base() + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with _opener.open(request, timeout=4) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return None, "Honcho API unavailable"
        raw = exc.read()
        status = exc.code
    except Exception:
        return None, "Honcho API unavailable"
    try:
        text = raw.decode("utf-8") if raw else ""
    except Exception:
        return None, "Honcho API unavailable"
    if text and SECRET.search(text):
        return None, "Honcho API response hidden"
    if status >= 400:
        return None, "Honcho API returned an error"
    if not text:
        return None, None
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, "Honcho API unavailable"


def list_peer_names():
    workspace = workspace_name()
    if not workspace:
        return None, "HONCHO_WORKSPACE is unset"
    quoted = urllib.parse.quote(workspace, safe="")
    found: list[str] = []
    seen: set[str] = set()
    for page in range(1, 51):
        parsed, error = api_json(
            "POST",
            f"/workspaces/{quoted}/peers/list?page={page}&size=100",
            {},
        )
        if error:
            return None, error
        if not isinstance(parsed, dict):
            return None, "Honcho API unavailable"
        items = parsed.get("items") or []
        if not isinstance(items, list):
            return None, "Honcho API unavailable"
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("id") if isinstance(item.get("id"), str) else item.get("name")
            if isinstance(name, str) and name not in seen:
                seen.add(name)
                found.append(name)
        pages = parsed.get("pages")
        if not isinstance(pages, int) or page >= pages:
            break
    return found, None


async def read_payload(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > BODY_LIMIT:
        raise HTTPException(status_code=413, detail="body is too long")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if SECRET.search(text):
        raise HTTPException(status_code=400, detail="secret-like string")
    try:
        payload = json.loads(text, parse_constant=_no_constant) if text else {}
    except (ValueError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return payload


def reject(errors: list[str]) -> None:
    safe = ["secret-like string" if SECRET.search(line) else line for line in errors]
    raise HTTPException(status_code=400, detail=safe)


@app.get("/", response_class=HTMLResponse)
def page():
    index = Path(__file__).resolve().with_name("index.html")
    try:
        body = index.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="page is missing") from exc
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


@app.get("/api/settings")
def get_settings():
    data, meta = read_settings()
    mtime = meta["mtime_ns"]
    return {
        "settings": data,
        "missing": meta["missing"],
        "mtime_ns": None if mtime is None else str(mtime),
        "errors": meta["errors"],
    }


@app.post("/api/settings")
async def post_settings(request: Request):
    payload = await read_payload(request)
    proposed = payload.get("settings")
    errors = check_settings(proposed)
    if errors:
        reject(errors)
    path = settings_path()
    assert_clean(path)
    check_mtime(path, payload)
    current, meta = read_settings()
    creating = bool(meta.get("missing"))
    before = current if isinstance(current, dict) else {}
    rows = diff_json(before, proposed)
    changed = creating or proposed != before
    mtime = file_mtime(path)
    result = {
        "ok": True,
        "changed": changed,
        "creating": creating,
        "diff": rows,
        "mtime_ns": None if mtime is None else str(mtime),
        "bak": False,
    }
    if payload.get("apply") and changed:
        bak_name = atomic_write_text(path, dump_settings(proposed), bak_dir=settings_bak_dir(), seen_mtime=mtime)
        written = file_mtime(path)
        result["mtime_ns"] = None if written is None else str(written)
        result["bak"] = bak_name is not None
    return result


def _bak_entry(candidate: Path, **extra) -> dict | None:
    try:
        stat = candidate.stat()
    except OSError:
        return None
    entry = {
        "name": candidate.name,
        "bytes": stat.st_size,
        "at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }
    entry.update(extra)
    return entry


def history_items() -> list[dict]:
    directory = settings_bak_dir()
    if not directory.is_dir():
        return []
    items = []
    for candidate in directory.glob(settings_path().name + ".bak*"):
        if not BAK_NAME.match(candidate.name) or not candidate.is_file():
            continue
        entry = _bak_entry(candidate)
        if entry is not None:
            items.append(entry)
    items.sort(key=lambda item: item["name"], reverse=True)
    return items


def instruction_history() -> list[dict]:
    """Instruction file copies under backups/instructions, newest first, with the file they belong to."""
    root = backups_root() / "instructions"
    if not root.is_dir():
        return []
    items = []
    for candidate in root.rglob("*.bak*"):
        match = BAK_ANY.match(candidate.name)
        if not match or not candidate.is_file():
            continue
        folder = relative_posix(root, candidate.parent)
        file = match.group("file") if folder in ("", ".") else f"{folder}/{match.group('file')}"
        extra = {"file": file, "scope": None, "owner": None, "category": None}
        parts = file.split("/")
        stem = parts[-1][:-3] if parts[-1].endswith(".md") else ""
        if stem in FILE_CATEGORIES:
            if len(parts) == 2 and parts[0] == "default":
                extra.update(scope="default", category=stem)
            elif len(parts) == 3 and parts[0] == "backends":
                extra.update(scope="backend", owner=parts[1], category=stem)
            elif len(parts) == 3 and parts[0] == "peers":
                extra.update(scope="peer", owner=parts[1], category=stem)
        entry = _bak_entry(candidate, **extra)
        if entry is not None:
            items.append(entry)
    items.sort(key=lambda item: (item["file"], item["name"]), reverse=True)
    return items


def workspace_history() -> list[dict]:
    directory = workspace_bak_dir()
    if not directory.is_dir():
        return []
    items = []
    for candidate in directory.iterdir():
        name = candidate.name
        if not (WORKSPACE_BAK.match(name) or backups.bak_pattern(WORKSPACE_FILE).match(name)) or not candidate.is_file():
            continue
        entry = _bak_entry(candidate)
        if entry is not None:
            items.append(entry)
    items.sort(key=lambda item: item["name"], reverse=True)
    return items


def honcho_config_history() -> list[dict]:
    path = honcho_config.config_path()
    directory = honcho_config.backup_dir(path)
    if not directory.is_dir():
        return []
    items = []
    for candidate in directory.glob(path.name + ".bak*"):
        if not candidate.is_file():
            continue
        entry = _bak_entry(candidate)
        if entry is not None:
            items.append(entry)
    items.sort(key=lambda item: item["name"], reverse=True)
    return items


def bak_file(name: str) -> Path:
    if not isinstance(name, str) or not BAK_NAME.match(name) or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="backup name is not allowed")
    directory = settings_bak_dir()
    candidate = (directory / name)
    if candidate.name != name or candidate.parent.resolve() != directory.resolve():
        raise HTTPException(status_code=400, detail="backup name is not allowed")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="backup is missing")
    return candidate


@app.get("/api/history")
def get_history():
    try:
        move_backups()
    except OSError:
        _instruction_logger.info("backup move failed")
    return {
        "backups": history_items(),
        "kinds": {
            "settings": history_items(),
            "instructions": instruction_history(),
            "workspace": workspace_history(),
            "honcho-config": honcho_config_history(),
        },
    }


@app.post("/api/history/restore")
async def post_restore(request: Request):
    payload = await read_payload(request)
    candidate = bak_file(payload.get("name"))
    try:
        text = candidate.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="backup cannot be read") from exc
    if SECRET.search(text):
        raise HTTPException(status_code=400, detail="secret-like string")
    try:
        proposed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    errors = check_settings(proposed)
    if errors:
        reject(errors)
    path = settings_path()
    assert_clean(path)
    check_mtime(path, payload)
    current, meta = read_settings()
    before = current if isinstance(current, dict) and not meta.get("missing") else {}
    rows = diff_json(before, proposed)
    mtime = file_mtime(path)
    result = {"ok": True, "changed": proposed != before, "diff": rows, "bak": False, "settings": proposed}
    if payload.get("apply") and proposed != before:
        bak_name = atomic_write_text(path, dump_settings(proposed), bak_dir=settings_bak_dir(), seen_mtime=mtime)
        result["bak"] = bak_name is not None
        written = file_mtime(path)
        result["mtime_ns"] = None if written is None else str(written)
    return result


@app.get("/api/peers")
def get_peers():
    data, _meta = read_settings()
    configured = []
    peers = data.get("peers") if isinstance(data, dict) else None
    if isinstance(peers, dict):
        configured = [name for name in peers if isinstance(name, str)]
    names, error = list_peer_names()
    discovered = []
    if names is not None:
        have = set(configured)
        discovered = [name for name in names if name not in have]
    return {"configured": configured, "discovered": discovered, "error": error}


@app.get("/api/card")
def get_card(name: str = ""):
    if not name or SECRET.search(name) or any(ord(char) < 32 for char in name):
        raise HTTPException(status_code=400, detail="peer name is not allowed")
    workspace = workspace_name()
    if not workspace:
        raise HTTPException(status_code=400, detail="HONCHO_WORKSPACE is unset")
    quoted_ws = urllib.parse.quote(workspace, safe="")
    quoted_name = urllib.parse.quote(name, safe="")
    parsed, error = api_json("GET", f"/workspaces/{quoted_ws}/peers/{quoted_name}/card")
    if error:
        raise HTTPException(status_code=502, detail=error)
    card = parsed.get("peer_card") if isinstance(parsed, dict) else None
    if card is not None and not isinstance(card, list):
        card = None
    if isinstance(card, list):
        card = [item for item in card if isinstance(item, str)]
    return {"peer_card": card}


@app.get("/api/status")
def get_status():
    workspace = workspace_name()
    queue = None
    queue_error = None
    if not workspace:
        queue_error = "HONCHO_WORKSPACE is unset"
    else:
        quoted = urllib.parse.quote(workspace, safe="")
        parsed, error = api_json("GET", f"/workspaces/{quoted}/queue/status")
        if error:
            queue_error = error
        elif isinstance(parsed, dict):
            queue = {
                "total_work_units": parsed.get("total_work_units"),
                "completed_work_units": parsed.get("completed_work_units"),
                "in_progress_work_units": parsed.get("in_progress_work_units"),
                "pending_work_units": parsed.get("pending_work_units"),
            }
        else:
            queue_error = "Honcho API unavailable"
    policy = tail_lines(policy_log_path())
    scheduler = tail_lines(scheduler_log_path())
    return {
        "queue": queue,
        "queue_error": queue_error,
        "policy_log": policy,
        "policy_log_missing": policy is None,
        "scheduler_log": scheduler,
        "scheduler_log_missing": scheduler is None,
        "telemetry": telemetry_status(),
        "hook": hook_status(),
    }


def hook_status() -> dict:
    path = hook_status_path()
    try:
        if path.stat().st_size > 16384:
            raise ValueError("oversized status")
        status = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            raise ValueError("status object required")
    except (OSError, ValueError):
        return {"active": False, "message": "hook status missing or unreadable"}
    failed = status.get("hook_failed")
    if status.get("checks_passed") is False and isinstance(failed, str) and failed:
        if status.get("dreams_blocked") is True:
            return {"active": False, "message": "hook failed — dreams blocked"}
        if status.get("dreams_blocked") is False:
            return {"active": False, "message": "hook failed — running stock Honcho"}
    applied = status.get("settings_mtime_applied")
    installed = status.get("installed_at")
    if status.get("checks_passed") is not True or not installed or type(applied) is not int:
        return {"active": False, "message": "hook installation not verified"}
    if status.get("last_reload_error"):
        return {"active": False, "message": "hook settings reload failed"}
    current = file_mtime(settings_path())
    if current is None:
        return {"active": False, "message": "hook settings file missing"}
    # The hook re-reads settings.json when a dream starts, not on a timer, so a newer file is pending, not stale.
    if current != applied:
        return {"active": True, "pending": True, "settings_mtime_applied": str(applied),
                "message": f"hook active; settings saved {_local_time(current)} apply at the next dream (last applied {_local_time(applied)})"}
    return {"active": True, "message": f"hook active, settings applied {_local_time(applied)}", "settings_mtime_applied": str(applied)}


def _local_time(ns: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ns / 1_000_000_000))
    except (OverflowError, OSError, ValueError):
        return "unknown time"


@app.get("/api/instructions")
def get_instructions(peer: str = ""):
    return instruction_rows(peer or None)


@app.post("/api/instructions")
async def post_instruction(request: Request):
    payload = await read_payload(request)
    scope, name, category = scope_parts(payload)
    text = payload.get("text")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text must be a string")
    if len(text) > BODY_LIMIT:
        raise HTTPException(status_code=413, detail="text is too long")
    path = instruction_target(scope, name, category)
    label = target_relative(scope, name, category)
    assert_clean(path)
    check_mtime(path, payload)
    before = read_text_file(path, "instruction file cannot be read")
    result = instruction_result(label, text, before)
    return apply_instruction_text(path, text, before, bool(payload.get("apply")), result)


@app.post("/api/instructions/from-default")
async def post_from_default(request: Request):
    payload = await read_payload(request)
    scope, name, category = scope_parts(payload)
    if scope not in ("backend", "peer"):
        raise HTTPException(status_code=400, detail="scope is not allowed")
    path = instruction_target(scope, name, category)
    label = target_relative(scope, name, category)
    source = named_instruction_file(Path("default") / f"{category}.md", f"default-{category}-instructions.md")
    if source is None:
        raise HTTPException(status_code=404, detail="no default file for this category")
    text = read_text_file(source, "default file cannot be read")
    if SECRET.search(text):
        raise HTTPException(status_code=400, detail="secret-like string")
    assert_clean(path)
    before = read_text_file(path, "instruction file cannot be read")
    result = instruction_result(label, text, before)
    return apply_instruction_text(path, text, before, bool(payload.get("apply")), result)


@app.post("/api/instructions/from-backend")
async def post_from_backend(request: Request):
    payload = await read_payload(request)
    peer = payload.get("peer")
    category = payload.get("category")
    if not safe_peer_name(peer):
        raise HTTPException(status_code=400, detail="peer name is not allowed")
    if category not in FILE_CATEGORIES:
        raise HTTPException(status_code=400, detail="category is not allowed")
    backend = payload.get("backend")
    if not isinstance(backend, str) or not backend:
        backend = peer_backend_name(relaxed_settings(), peer)
    if not isinstance(backend, str) or not BACKEND_NAME.match(backend):
        raise HTTPException(status_code=400, detail="backend name is not allowed")
    source = named_instruction_file(
        Path("backends") / backend / f"{category}.md",
        f"{backend}-{category}-instructions.md",
    )
    if source is None:
        raise HTTPException(status_code=404, detail="no backend file for this category")
    text = read_text_file(source, "backend file cannot be read")
    if SECRET.search(text):
        raise HTTPException(status_code=400, detail="secret-like string")
    path = instruction_target("peer", peer, category)
    label = target_relative("peer", peer, category)
    assert_clean(path)
    before = read_text_file(path, "instruction file cannot be read")
    result = instruction_result(label, text, before)
    return apply_instruction_text(path, text, before, bool(payload.get("apply")), result)


@app.post("/api/instructions/workspace")
async def post_workspace_instructions(request: Request):
    payload = await read_payload(request)
    kind = payload.get("kind")
    unset = payload.get("unset") is True
    text = "" if unset else payload.get("text")
    if kind not in WORKSPACE_KINDS:
        raise HTTPException(status_code=400, detail="kind is not allowed")
    if not isinstance(text, str):
        raise HTTPException(status_code=400, detail="text must be a string")
    if len(text) > BODY_LIMIT:
        raise HTTPException(status_code=413, detail="text is too long")
    cap = instruction_cap()
    count = token_count(text)
    over = bool(text) and count > cap
    if payload.get("apply") and over:
        raise HTTPException(status_code=400, detail="custom instructions are over the cap")
    config, error = workspace_configuration()
    if error:
        status = 400 if error in ("HONCHO_WORKSPACE is unset", "HONCHO_API_TOKEN is unset") else 502
        raise HTTPException(status_code=status, detail=error)
    updated = with_kind_text(config, kind, None if unset else text)
    result = {
        "ok": True,
        "changed": updated != config,
        "diff": diff_json(config, updated),
        "tokens": count,
        "cap": cap,
        "over": over,
        "empty": not unset and not text.strip(),
        "unset": unset,
        "workspace": True,
        "bak": False,
        "kind": kind,
    }
    if payload.get("apply") and updated != config:
        put_workspace(config, updated)
        result["bak"] = True
    return result


def put_workspace(config: dict, updated: dict) -> None:
    """Back up the current configuration, then PUT the whole updated one.

    Honcho merges top-level keys on update, so each module block is sent complete: a block sent with only
    `enabled` would replace the stored block and drop its custom_instructions.
    """
    write_workspace_bak(config)
    quoted = urllib.parse.quote(workspace_name(), safe="")
    parsed, put_error = api_json("PUT", f"/workspaces/{quoted}", {"configuration": updated})
    if put_error or not isinstance(parsed, dict):
        raise HTTPException(status_code=502, detail=put_error or "Honcho API unavailable")


def module_enabled(config: dict, module: str) -> bool | None:
    """True, False, or None when the block or its enabled key is absent (Honcho's own setting applies)."""
    block = config.get(module)
    value = block.get("enabled") if isinstance(block, dict) else None
    return value if isinstance(value, bool) else None


def with_enabled(config: dict, module: str, value: bool | None) -> dict:
    """Set or drop <module>.enabled and keep every other key of that block."""
    updated = json.loads(json.dumps(config))
    block = updated.get(module)
    block = dict(block) if isinstance(block, dict) else {}
    if value is None:
        if module in updated:
            block.pop("enabled", None)
            updated[module] = block
        return updated
    block["enabled"] = value
    updated[module] = block
    return updated


def config_toml_dream_enabled() -> dict:
    """config.toml [dream] ENABLED, read-only. Honcho reads it at process start."""
    try:
        schema = honcho_config.load_schema()
        document, missing, parse_error = honcho_config.read_document()
    except HTTPException as exc:
        return {"value": None, "in_file": False, "default": None, "locked": False, "error": str(exc.detail)}
    section = schema.get("sections", {}).get("dream", {})
    spec = section.get("fields", {}).get("enabled", {})
    default = spec.get("default")
    env = (section.get("env_prefix") or "") + str(spec.get("env_name") or "ENABLED")
    if parse_error:
        return {"value": None, "in_file": False, "default": default, "locked": False, "error": parse_error}
    table, _present = honcho_config.child(document if isinstance(document, dict) else {}, "dream")
    value, present = honcho_config.child(table if isinstance(table, dict) else {}, "enabled")
    return {
        "value": value if present else default,
        "in_file": present,
        "default": default,
        "missing": missing,
        "locked": env in set(honcho_config.override_names()),
        "env": env,
        "error": None,
    }


@app.get("/api/control")
def get_control():
    config, error = workspace_configuration()
    enabled = None
    if config is not None:
        enabled = {module: module_enabled(config, module) for module in CONTROL_MODULES}
    models, models_error = [], None
    try:
        document, _missing, parse_error = honcho_config.read_document()
        if parse_error:
            models_error = parse_error
        else:
            models = honcho_config.model_resolution(document)
    except HTTPException as exc:
        models_error = str(exc.detail)
    return {
        "enabled": enabled,
        "workspace_error": error,
        "dialectic": "cannot be paused from configuration",
        "config_toml_dream": config_toml_dream_enabled(),
        "models": models,
        "models_error": models_error,
    }


@app.post("/api/workspace")
async def post_workspace(request: Request):
    """One combined workspace change: enabled flags and custom instructions, one diff, one backup, one PUT.

    Body: {"enabled": {"dream": true|false|null, ...}, "instructions": {"reasoning": {"text": "..."} or
    {"unset": true}, ...}, "apply": bool}.
    """
    payload = await read_payload(request)
    enabled = payload.get("enabled") or {}
    texts = payload.get("instructions") or {}
    if not isinstance(enabled, dict) or not isinstance(texts, dict):
        raise HTTPException(status_code=400, detail="enabled and instructions must be objects")
    for module, value in enabled.items():
        if module not in CONTROL_MODULES:
            raise HTTPException(status_code=400, detail="module is not allowed")
        if value is not None and not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="enabled must be true, false or null")
    cap = instruction_cap()
    over = []
    for kind, item in texts.items():
        if kind not in WORKSPACE_KINDS:
            raise HTTPException(status_code=400, detail="kind is not allowed")
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="instruction change must be an object")
        if item.get("unset") is True:
            continue
        text = item.get("text")
        if not isinstance(text, str):
            raise HTTPException(status_code=400, detail="text must be a string")
        if len(text) > BODY_LIMIT:
            raise HTTPException(status_code=413, detail="text is too long")
        if text and token_count(text) > cap:
            over.append(kind)
    if payload.get("apply") and over:
        raise HTTPException(status_code=400, detail="custom instructions are over the cap")
    config, error = workspace_configuration()
    if error:
        status = 400 if error in ("HONCHO_WORKSPACE is unset", "HONCHO_API_TOKEN is unset") else 502
        raise HTTPException(status_code=status, detail=error)
    updated = config
    for module, value in enabled.items():
        updated = with_enabled(updated, module, value)
    for kind, item in texts.items():
        updated = with_kind_text(updated, kind, None if item.get("unset") is True else item.get("text"))
    result = {
        "ok": True,
        "changed": updated != config,
        "diff": diff_json(config, updated),
        "over": bool(over),
        "over_kinds": over,
        "cap": cap,
        "workspace": True,
        "bak": False,
    }
    if payload.get("apply") and updated != config:
        put_workspace(config, updated)
        result["bak"] = True
    return result


OFF_SUFFIX = ".off"


@app.post("/api/instructions/remove")
async def post_instruction_remove(request: Request):
    """Remove a peer or backend file: it is kept as a backup in backups/instructions and leaves the tree."""
    payload = await read_payload(request)
    scope, name, category = scope_parts(payload)
    if scope == "default":
        raise HTTPException(status_code=400, detail="the All peers file is switched off, not removed")
    path = instruction_target(scope, name, category)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="instruction file is missing")
    assert_clean(path)
    if not payload.get("apply"):
        return {"ok": True, "file": target_relative(scope, name, category)}
    kept = backups.backup_file(path, instruction_bak_dir(path))
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return {"ok": True, "file": target_relative(scope, name, category), "backup": kept}


@app.post("/api/instructions/switch")
async def post_instruction_switch(request: Request):
    """Turn the All peers file off (renamed <kind>.md.off, so dreams fall back to the workspace text) or on again."""
    payload = await read_payload(request)
    category = payload.get("category")
    path = instruction_target("default", None, category if isinstance(category, str) else "")
    off = path.with_name(path.name + OFF_SUFFIX)
    on = payload.get("on") is True
    source, target = (off, path) if on else (path, off)
    if not source.is_file():
        return {"ok": True, "on": path.is_file()}
    if target.exists():
        raise HTTPException(status_code=409, detail="both the file and its switched-off copy exist")
    source.rename(target)
    return {"ok": True, "on": on}


@app.post("/api/instructions/restore")
async def post_instruction_restore(request: Request):
    payload = await read_payload(request)
    scope, name, category = scope_parts(payload)
    path = instruction_target(scope, name, category)
    label = target_relative(scope, name, category)
    bak = payload.get("bak")
    if not isinstance(bak, str) or Path(bak).name != bak or not (BAK_ANY.match(bak) and BAK_ANY.match(bak).group("file") == path.name):
        raise HTTPException(status_code=400, detail="backup name is not allowed")
    directory = instruction_bak_dir(path)
    candidate = directory / bak
    if not candidate.is_file() or candidate.parent.resolve() != directory.resolve():
        raise HTTPException(status_code=404, detail="backup is missing")
    text = read_text_file(candidate, "backup cannot be read")
    if SECRET.search(text):
        raise HTTPException(status_code=400, detail="secret-like string")
    assert_clean(path)
    before = read_text_file(path, "instruction file cannot be read")
    result = instruction_result(label, text, before)
    return apply_instruction_text(path, text, before, bool(payload.get("apply")), result)


class ProxyError(Exception):
    """The socket proxy was not called, or the call did not succeed."""


def restart_log_path() -> Path:
    """RESTART_LOG, else restart.log in LOGS_DIR."""
    return Path(os.environ.get("RESTART_LOG", "").strip() or LOGS / "restart.log")


def client_address(request: Request) -> str:
    """Socket peer. Forwarded headers are not consulted."""
    client = request.client
    if client is None or not client.host:
        return ""
    return client.host


def allow_names() -> list[str]:
    """The container names the dashboard may act on: RESTART_ALLOWED."""
    return list(RESTART_ALLOWED)


def restart_configured() -> bool:
    """Apply can restart Honcho: a proxy is set and the list has one api and one deriver container."""
    proxy = os.environ.get("DOCKER_PROXY_ACTION_URL", "").strip() or proxy_base()
    names = allow_names()
    return bool(proxy) and all(service_container(names, service) for service in ("api", "deriver"))


def _one_line(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ")


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


def write_restart_log(ip: str, name: str, action: str, result: str) -> None:
    line = " ".join([
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ip=" + _one_line(ip),
        "name=" + _one_line(name),
        "action=" + _one_line(action),
        "result=" + _one_line(result),
    ])
    _restart_logger.info("%s", line)
    path = restart_log_path()
    try:
        with _restart_log_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                limit, keep = log_limits(relaxed_settings())
            except Exception:
                limit, keep = 10 * 1024 * 1024, 1
            rotate_log(path, limit, keep)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
    except OSError:
        _restart_logger.info("restart log could not be written")


def proxy_base() -> str:
    return os.environ.get("DOCKER_PROXY_URL", "").strip().rstrip("/")


def proxy_open(method: str, path: str, timeout: float) -> tuple[int, bytes]:
    base = proxy_base()
    if method == "POST":
        # Actions go through the action-only proxy when one is configured.
        base = os.environ.get("DOCKER_PROXY_ACTION_URL", "").strip().rstrip("/") or base
    if not base:
        raise ProxyError("DOCKER_PROXY_URL is unset")
    request = urllib.request.Request(
        base + path,
        data=b"" if method == "POST" else None,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return getattr(response, "status", 200), response.read()
    except urllib.error.HTTPError as exc:
        raise ProxyError("docker proxy returned " + str(exc.code)) from exc
    except ProxyError:
        raise
    except Exception as exc:
        raise ProxyError("docker proxy is unreachable") from exc


def docker_container_rows(raw: bytes) -> list[dict]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProxyError("docker proxy returned an unreadable list") from exc
    if not isinstance(data, list):
        raise ProxyError("docker proxy returned an unreadable list")
    rows = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        names = item.get("Names")
        if not isinstance(names, list):
            continue
        chosen = ""
        for candidate in names:
            if isinstance(candidate, str) and candidate.strip():
                chosen = candidate.strip()
                break
        if chosen.startswith("/"):
            chosen = chosen[1:]
        if not chosen or chosen in seen:
            continue
        seen.add(chosen)
        state = item.get("State")
        status = item.get("Status")
        rows.append({
            "name": chosen,
            "state": state if isinstance(state, str) and state else "unknown",
            "uptime": status if isinstance(status, str) and status else None,
        })
    return rows


def container_started_at(name: str) -> str | None:
    quoted = urllib.parse.quote(name, safe="")
    try:
        _status, raw = proxy_open("GET", "/containers/" + quoted + "/json", PROXY_READ_TIMEOUT)
        data = json.loads(raw.decode("utf-8"))
    except (ProxyError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    state = data.get("State") if isinstance(data, dict) else None
    if not isinstance(state, dict):
        return None
    started = state.get("StartedAt")
    if not isinstance(started, str) or not started or started.startswith("0001-"):
        return None
    return started


def container_view(row: dict) -> dict:
    return {
        "name": row["name"],
        "state": row["state"],
        "uptime": row.get("uptime"),
        "started_at": container_started_at(row["name"]),
    }


def containers_payload() -> dict:
    names = allow_names()
    body: dict = {"allowed": names}
    if not proxy_base():
        body["error"] = "DOCKER_PROXY_URL is unset"
        return body
    try:
        _status, raw = proxy_open("GET", "/containers/json?all=1", PROXY_READ_TIMEOUT)
        found = docker_container_rows(raw)
    except ProxyError as exc:
        body["error"] = str(exc)
        return body
    by_name = {row["name"]: row for row in found}
    selected = []
    for name in names:
        if name in by_name:
            selected.append(container_view(by_name[name]))
        else:
            selected.append({
                "name": name,
                "state": "unknown",
                "uptime": None,
                "started_at": None,
            })
    body["read_only"] = False
    body["containers"] = selected
    return body


def refuse_action(ip: str, name: str, action: str, reason: str) -> None:
    write_restart_log(ip, name, action, "refused: " + reason)
    raise HTTPException(status_code=403, detail=reason)


def run_container_action(request: Request, action: str, name: str) -> dict:
    ip = client_address(request)
    names = allow_names()
    if name not in names:
        refuse_action(ip, name, action, "container is not in the allow list")
    if not CONTAINER_NAME.match(name):
        refuse_action(ip, name, action, "container name is not allowed")
    quoted = urllib.parse.quote(name, safe="")
    try:
        proxy_open("POST", "/containers/" + quoted + "/" + action + "?t=10", PROXY_ACTION_TIMEOUT)
    except ProxyError as exc:
        write_restart_log(ip, name, action, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    write_restart_log(ip, name, action, "ok")
    return {"ok": True, "name": name, "action": action}


@app.get("/api/containers")
def get_containers():
    return containers_payload()


def models_path() -> Path:
    """models.json beside settings.json (editable, offline or from the page); seeded from config/models.json."""
    env = os.environ.get("MODELS_FILE", "").strip()
    if env:
        return Path(env)
    return settings_path().parent / "models.json"


def models_catalogue() -> dict:
    path = models_path()
    if not path.is_file():
        seed = Path("/templates/models.json")
        if not seed.is_file():
            seed = Path(__file__).resolve().parents[2] / "config" / "models.json"
        try:
            path.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            path = seed
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


@app.get("/api/prompts")
def get_prompts():
    """Honcho's stock prompts for intake, summaries and questions (config/honcho-prompts.json, built by gen_honcho_prompts.py)."""
    path = Path("/templates/honcho-prompts.json")
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "config" / "honcho-prompts.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"jobs": {}}


@app.get("/api/models")
def get_models():
    """The model catalogue: dream entries whose backend exists in settings.json, and Honcho task models by transport.

    Provider APIs list hundreds of models; only the chosen catalogue is offered.
    """
    data, _meta = read_settings()
    catalogue = models_catalogue()
    backends = backend_blocks(data)
    dream = []
    entries = catalogue.get("dream")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("backend") not in backends:
            continue
        dream.append({key: str(entry.get(key, "")) for key in ("vendor", "label", "backend", "model")})
    tasks = {}
    groups = catalogue.get("tasks")
    for transport, entries in (groups.items() if isinstance(groups, dict) else []):
        rows = entries if isinstance(entries, list) else []
        tasks[transport] = [{"label": str(e.get("label", "")), "model": str(e.get("model", ""))} for e in rows if isinstance(e, dict)]
    return {"dream": dream, "tasks": tasks, "backends": {name: block.get("kind", "") if isinstance(block, dict) else "" for name, block in backends.items()}}


@app.post("/api/models/add")
async def post_models_add(request: Request):
    """Add one dream model to models.json (vendor, label, the bridge backend that reaches it, its id)."""
    payload = await read_payload(request)
    entry = {key: payload.get(key) for key in ("vendor", "label", "backend", "model")}
    if not all(isinstance(value, str) and value.strip() and len(value) <= 200 for value in entry.values()):
        raise HTTPException(status_code=400, detail="vendor, label, backend and model are required")
    entry = {key: value.strip() for key, value in entry.items()}
    if SECRET.search(json.dumps(entry)):
        raise HTTPException(status_code=400, detail="secret-like string")
    data, _meta = read_settings()
    if entry["backend"] not in backend_blocks(data):
        raise HTTPException(status_code=400, detail="backend is not in settings.json")
    catalogue = models_catalogue()
    dream = catalogue.setdefault("dream", [])
    if not isinstance(dream, list):
        raise HTTPException(status_code=409, detail="models.json: dream must be a list")
    if any(e.get("backend") == entry["backend"] and e.get("model") == entry["model"] for e in dream if isinstance(e, dict)):
        raise HTTPException(status_code=409, detail="that model is already in the list")
    dream.append(entry)
    path = models_path()
    atomic_write_text(path, json.dumps(catalogue, indent=2, ensure_ascii=False) + "\n", bak_dir=backups_root() / "models")
    return {"ok": True, "entry": entry}


def runtime_env_names() -> list[str]:
    """Environment names shown as running values. Only these names are read back; never a secret."""
    raw = os.environ.get("RUNTIME_ENV_NAMES", "DERIVER_WORKERS,API_WORKERS")
    return [name.strip() for name in raw.split(",") if name.strip() and not SECRET_NAME.search(name.strip())]


@app.get("/api/runtime")
def get_runtime():
    """Listed environment values of the allowed containers, read through the read-only proxy (container inspect)."""
    names = runtime_env_names()
    values: dict = {}
    for container in allow_names():
        try:
            _status, raw = proxy_open("GET", "/containers/" + urllib.parse.quote(container, safe="") + "/json", PROXY_READ_TIMEOUT)
            env = (json.loads(raw.decode("utf-8")).get("Config") or {}).get("Env") or []
        except (ProxyError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            continue
        for item in env:
            key, sep, value = str(item).partition("=")
            if sep and key in names:
                values.setdefault(key, {})[container] = value
    return {"values": values, "error": None}


@app.post("/api/restart/{name}")
def post_restart(name: str, request: Request):
    return run_container_action(request, "restart", name)


@app.post("/api/stop/{name}")
def post_stop(name: str, request: Request):
    return run_container_action(request, "stop", name)


@app.post("/api/start/{name}")
def post_start(name: str, request: Request):
    return run_container_action(request, "start", name)


@app.get("/api/honcho-config")
def get_honcho_config():
    result = honcho_config.view()
    result["restart_configured"] = restart_configured()
    schema = honcho_config.load_schema()
    result["schema_honcho_version"] = schema.get("honcho_version")
    result["schema_upstream_sha"] = schema.get("upstream_sha")
    result["fork_sha"] = schema.get("fork_sha")
    result["fork_version_heading"] = schema.get("fork_version_heading")
    runtime = running_honcho_version()
    result["running_honcho_version"] = runtime
    result["version_warning"] = (
        f"Running Honcho {runtime} differs from schema {result['honcho_version']}"
        if runtime and result.get("honcho_version") and runtime != result["honcho_version"] else None
    )
    return result


def running_honcho_version() -> str | None:
    """Last reported version in a bounded tail of today's telemetry; absent is unknown."""
    path = telemetry_dir() / f"events-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
    for line in reversed(tail_lines(path, 100) or []):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        event = item.get("event") if isinstance(item, dict) else None
        data = event.get("data") if isinstance(event, dict) else None
        version = data.get("honcho_version") if isinstance(data, dict) else None
        if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9.+_-]{1,80}", version):
            return version
    return None


@app.post("/api/honcho-config/review")
async def post_honcho_review(request: Request):
    return honcho_config.review(await honcho_config.read_body(request))


@app.post("/api/honcho-config/write")
async def post_honcho_write(request: Request):
    return honcho_config.write_config(await honcho_config.read_body(request))


@app.post("/api/honcho-config/undo-routing")
async def post_honcho_undo_routing(request: Request):
    return honcho_config.undo_routing(await honcho_config.read_body(request))


@app.get("/api/bridge-status")
def get_bridge_status():
    """Read configured dream bridge health without forwarding credentials."""
    document, missing, error = honcho_config.read_document()
    result = {"configured": False, "config_error": bool(error), "bridges": []}
    if missing or error or not document:
        return result
    targets = {}
    dream = document.get("dream") or {}
    if not isinstance(dream, dict):
        result["config_error"] = True
        return result
    for stage in ("deduction", "induction"):
        config = dream.get(stage + "_model_config") or {}
        if not isinstance(config, dict):
            result["config_error"] = True
            continue
        overrides = config.get("overrides", config)
        if not isinstance(overrides, dict):
            result["config_error"] = True
            continue
        params = overrides.get("provider_params") or {}
        if not isinstance(params, dict):
            result["config_error"] = True
            continue
        extra = params.get("extra_body") or {}
        if not isinstance(extra, dict):
            result["config_error"] = True
            continue
        if not isinstance(extra.get("backend"), str) or not extra["backend"].startswith("@"):
            continue
        result["configured"] = True
        base = overrides.get("base_url") or params.get("base_url")
        try:
            parsed = urllib.parse.urlsplit(base) if isinstance(base, str) else None
        except ValueError:
            parsed = None
        if not parsed or parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            result["bridges"].append({"stages": [stage], "status": "invalid_config"})
            continue
        base = base.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        targets.setdefault(base + "/health", []).append(stage)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    for url, stages in targets.items():
        entry = {"stages": stages, "status": "unreachable"}
        try:
            opener = urllib.request.build_opener(NoRedirect)
            with opener.open(urllib.request.Request(url, method="GET"), timeout=2) as response:
                raw = response.read(65537)
            health = json.loads(raw) if len(raw) <= 65536 else None
            if isinstance(health, dict):
                allowed = ("ok", "settings_ok", "active", "concurrency", "timeout_seconds", "log_ok", "log_write_errors", "calls", "errors", "tool_turns", "final_turns", "in", "out")
                safe = {key: public(health[key]) for key in allowed if key in health and isinstance(health[key], (str, int, float, bool, type(None))) and (not isinstance(health[key], (int, float)) or isinstance(health[key], bool) or _finite_number(health[key]) is not None)}
                safe["settings_error"] = bool(health.get("settings_error"))
                safe["routing"] = {}
                routing = health.get("routing") or {}
                if isinstance(routing, dict):
                    for name, row in list(routing.items())[:64]:
                        if not isinstance(row, dict):
                            continue
                        last = row.get("last_call") if isinstance(row.get("last_call"), dict) else {}
                        safe["routing"][public(name)] = {"calls": _finite_int(row.get("calls")), "errors": _finite_int(row.get("errors")),
                            "last_call": {key: public(last[key]) for key in ("ts", "requested_model", "resolved_model", "upstream_model", "billing_route", "ok")
                                          if key in last and isinstance(last[key], (str, bool, type(None)))}}
                entry.update(status="ok", health=safe)
        except (OSError, ValueError, TypeError):
            pass
        result["bridges"].append(entry)
    return result


@app.post("/api/honcho-config/apply")
def post_honcho_apply(request: Request):
    """Restart api and deriver so Honcho reads config.toml. Refused while a model_config block lacks model or transport.

    Honcho would otherwise start on its built-in default model on the OpenAI key without saying so.
    """
    document, _missing, parse_error = honcho_config.read_document()
    if parse_error:
        raise HTTPException(status_code=400, detail=parse_error)
    gaps = honcho_config.model_gaps(document)
    if gaps:
        raise HTTPException(status_code=409, detail=["Apply refused: a model_config block is incomplete.", *gaps])
    names = allow_names()
    targets = {service: service_container(names, service) for service in ("api", "deriver")}
    unknown = [service for service, name in targets.items() if name is None]
    if unknown:
        raise HTTPException(status_code=409, detail="Restart refused: no single container for Honcho's "
                            + " and ".join(unknown) + " in the restart allow list.")
    done = []
    for name in targets.values():
        run_container_action(request, "restart", name)
        done.append(name)
    return {"ok": True, "restarted": done}


def service_container(names: list[str], service: str) -> str | None:
    """The allowed container of a compose service: the bare service name or <project>-<service>-<n>."""
    pattern = re.compile(r"^(?:.+[-_])?" + re.escape(service) + r"(?:[-_]\d+)?$")
    matches = [name for name in names if pattern.match(name)]
    return matches[0] if len(matches) == 1 else None


# ---------- rerank telemetry summary ----------
RERANK_EVENT = "rerank.call.completed"
RERANK_MARK = f'"{RERANK_EVENT}"'.encode()
RERANK_DAYS = 7
# Bounds per request. A line above RERANK_LINE_LIMIT is drained in chunks and skipped; the scan stops,
# with truncated set, after RERANK_SCAN_BYTES read or RERANK_SCAN_EVENTS kept; each period keeps at most
# RERANK_DURATION_SAMPLE durations (reservoir sample) for the percentiles.
RERANK_LINE_LIMIT = 1_000_000
RERANK_CHUNK = 64 * 1024
RERANK_SCAN_BYTES = 256 * 1024 * 1024
RERANK_SCAN_EVENTS = 500_000
RERANK_DURATION_SAMPLE = 5_000
# Values above this cannot be represented exactly in the page's JSON numbers.
RERANK_MAX_INT = 2**53
UNREADABLE = (OSError, EOFError, zlib.error, UnicodeDecodeError)
_day_cache: dict[Path, tuple[int, int, "_RerankPeriod"]] = {}
_day_cache_lock = threading.Lock()


def _utc_days(count: int, today: datetime | None = None) -> list[str]:
    now = today or datetime.now(timezone.utc)
    return [(now - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(count)]


class _Budget:
    """What one request may read. Shared by every file of the request."""

    def __init__(self, max_bytes: int = RERANK_SCAN_BYTES, max_events: int = RERANK_SCAN_EVENTS) -> None:
        self.bytes_left = max_bytes
        self.events_left = max_events
        self.truncated: list[str] = []

    def spent(self) -> bool:
        return self.bytes_left <= 0 or self.events_left <= 0


def _event_lines(path: Path, budget: _Budget, consumed: list[int] | None = None):
    """Complete lines of one events file, plain or gzipped, read in bounded chunks.

    A line longer than RERANK_LINE_LIMIT is drained chunk by chunk and skipped without
    ever being held whole. Reading stops when the budget's bytes are spent. With consumed,
    a one-item list, reading starts at that offset, the item follows the end of the last
    complete line, and a last line with no newline yet is left for the next read.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    pending = bytearray()
    oversize = False
    position = consumed[0] if consumed else 0
    with opener(path, "rb") as handle:
        handle.seek(position)
        while True:
            if budget.bytes_left <= 0:
                budget.truncated.append(f"{path.name}: byte budget")
                return
            chunk = handle.read(min(RERANK_CHUNK, budget.bytes_left))
            if not chunk:
                break
            budget.bytes_left -= len(chunk)
            start = 0
            while True:
                newline = chunk.find(b"\n", start)
                if newline < 0:
                    rest = chunk[start:]
                    if not oversize:
                        if len(pending) + len(rest) > RERANK_LINE_LIMIT:
                            oversize = True
                            pending.clear()
                        else:
                            pending += rest
                    break
                if consumed is not None:
                    consumed[0] = position + newline + 1
                if not oversize:
                    pending += chunk[start:newline]
                    if len(pending) <= RERANK_LINE_LIMIT:
                        yield bytes(pending)
                pending.clear()
                oversize = False
                start = newline + 1
            position += len(chunk)
    if pending and not oversize and consumed is None:
        yield bytes(pending)


def _parse_line(raw: bytes) -> dict | None:
    try:
        item = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, RecursionError):
        return None
    return item if isinstance(item, dict) else None


def _finite_number(value) -> float | None:
    """A finite number within JSON range, or None. Bools, NaN, infinities and huge integers are None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        if abs(value) >= RERANK_MAX_INT:
            return None
        return float(value)
    if not math.isfinite(value):
        return None
    return value


def _finite_int(value) -> int | None:
    number = _finite_number(value)
    if number is None or not isinstance(value, int):
        return None
    return value


def _percentile(values: list[float], share: float) -> float | None:
    """Nearest-rank percentile of a sorted list. None when empty."""
    if not values:
        return None
    rank = math.ceil(share * len(values))
    return values[max(0, min(len(values) - 1, rank - 1))]


def _finite_out(value):
    """A number leaves the response only when it is finite; anything else becomes None."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if _finite_number(value) is not None else None
    return value


class _RerankPeriod:
    def __init__(self, seed: int = 0) -> None:
        self.total = 0
        self.outcomes: dict[str, int] = {}
        self.callers: dict[str, int] = {}
        self.providers: dict[str, int] = {}
        self.tokens = 0
        self.billed_units = 0.0
        self.sent = 0
        self.durations: list[float] = []
        self.duration_seen = 0
        self.skipped_metrics = 0
        self._random = random.Random(seed)
        self.latest: dict | None = None
        self.latest_time = ""
        self.counter: dict | None = None
        self.counter_time = ""

    def _sample(self, value: float) -> None:
        """Reservoir sample of fixed size: every duration seen has the same chance to stay."""
        self.duration_seen += 1
        if len(self.durations) < RERANK_DURATION_SAMPLE:
            self.durations.append(value)
            return
        slot = self._random.randrange(self.duration_seen)
        if slot < RERANK_DURATION_SAMPLE:
            self.durations[slot] = value

    def add(self, data: dict, when: str) -> None:
        self.total += 1
        outcome = data.get("outcome") if isinstance(data.get("outcome"), str) else "unknown"
        caller = data.get("caller") if isinstance(data.get("caller"), str) else "unknown"
        provider = data.get("provider") if isinstance(data.get("provider"), str) else "unknown"
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        self.callers[caller] = self.callers.get(caller, 0) + 1
        self.providers[provider] = self.providers.get(provider, 0) + 1
        tokens = _finite_int(data.get("tokens"))
        if tokens is not None and tokens >= 0:
            self.tokens += tokens
        elif data.get("tokens") is not None:
            self.skipped_metrics += 1
        billed = _finite_number(data.get("billed_units"))
        if billed is not None and billed >= 0:
            self.billed_units += billed
        elif data.get("billed_units") is not None:
            self.skipped_metrics += 1
        # A call sent a request when candidates were sent, whatever its outcome (cancelled included).
        candidates = _finite_int(data.get("candidates_sent"))
        if candidates is not None and candidates > 0:
            self.sent += 1
            duration = _finite_number(data.get("duration_ms"))
            if duration is not None and duration >= 0:
                self._sample(duration)
            elif data.get("duration_ms") is not None:
                self.skipped_metrics += 1
        elif candidates is None and data.get("candidates_sent") is not None:
            self.skipped_metrics += 1
        requests = _finite_int(data.get("daily_requests"))
        cap = _finite_int(data.get("daily_request_cap"))
        if when >= self.latest_time:
            self.latest_time = when
            self.latest = {"time": when, "outcome": outcome, "daily_requests": requests, "daily_request_cap": cap}
        # The counter is reported only by calls that reached it; a later skip says nothing about it.
        if requests is not None and when >= self.counter_time:
            self.counter_time = when
            self.counter = {"time": when, "daily_requests": requests, "daily_request_cap": cap}

    def merge(self, other: "_RerankPeriod") -> None:
        self.total += other.total
        for mine, theirs in ((self.outcomes, other.outcomes), (self.callers, other.callers), (self.providers, other.providers)):
            for key, count in theirs.items():
                mine[key] = mine.get(key, 0) + count
        self.tokens += other.tokens
        self.billed_units += other.billed_units
        self.sent += other.sent
        self.skipped_metrics += other.skipped_metrics
        for value in other.durations:
            self._sample(value)
        self.duration_seen += other.duration_seen - len(other.durations)
        if other.latest is not None and other.latest_time >= self.latest_time:
            self.latest, self.latest_time = other.latest, other.latest_time
        if other.counter is not None and other.counter_time >= self.counter_time:
            self.counter, self.counter_time = other.counter, other.counter_time

    def view(self) -> dict:
        durations = sorted(self.durations)
        fallbacks = self.total - self.outcomes.get("reranked", 0)
        return {
            "total": self.total,
            "outcomes": dict(sorted(self.outcomes.items())),
            "callers": dict(sorted(self.callers.items())),
            "providers": dict(sorted(self.providers.items())),
            "fallbacks": fallbacks,
            "fallback_rate": _finite_out(fallbacks / self.total) if self.total else None,
            "tokens": _finite_out(self.tokens),
            "billed_units": _finite_out(self.billed_units),
            "sent": self.sent,
            "duration_ms": {
                "p50": _finite_out(_percentile(durations, 0.5)),
                "p95": _finite_out(_percentile(durations, 0.95)),
                "sampled": len(durations),
                "seen": self.duration_seen,
            },
            "skipped_metrics": self.skipped_metrics,
            "latest": self.latest,
            "last_counter": self.counter,
        }


def _scan_file(path: Path, days: list[str], budget: _Budget, period: _RerankPeriod,
               today: _RerankPeriod | None = None) -> None:
    for raw in _event_lines(path, budget):
        if budget.events_left <= 0:
            budget.truncated.append(f"{path.name}: event budget")
            return
        if RERANK_MARK not in raw:
            continue
        item = _parse_line(raw)
        if item is None:
            continue
        event = item.get("event")
        if not isinstance(event, dict) or event.get("type") != RERANK_EVENT:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        when = event.get("time") if isinstance(event.get("time"), str) else item.get("received_at")
        when = when if isinstance(when, str) else ""
        if when[:10] not in days:
            continue
        budget.events_left -= 1
        period.add(data, when)
        # Today's file also holds late events dated earlier: they count for the week only.
        if today is not None and when[:10] == days[0]:
            today.add(data, when)


def _day_period(path: Path, days: list[str], budget: _Budget, cache: bool,
                today: _RerankPeriod | None = None) -> _RerankPeriod:
    """The aggregate of one file. A past day's file is cached by (path, size, mtime); today's is re-read."""
    stat = path.stat()
    key = (stat.st_size, stat.st_mtime_ns)
    if cache:
        with _day_cache_lock:
            hit = _day_cache.get(path)
        if hit is not None and hit[0] == key[0] and hit[1] == key[1]:
            return hit[2]
    period = _RerankPeriod(seed=hash(path.name) & 0xFFFF)
    _scan_file(path, days, budget, period, today)
    if cache and not budget.truncated:
        with _day_cache_lock:
            _day_cache[path] = (key[0], key[1], period)
    return period


def _forget_old_days(directory: Path, days: list[str]) -> None:
    """Drop cached aggregates of files outside the window, so the cache does not grow for the life of the process."""
    keep = {directory / f"events-{day}{suffix}" for day in days for suffix in (".jsonl", ".jsonl.gz")}
    with _day_cache_lock:
        for path in [path for path in _day_cache if path not in keep]:
            del _day_cache[path]


def rerank_stats(directory: Path | None = None, now: datetime | None = None, budget: _Budget | None = None) -> dict:
    """Counts from the rerank.call.completed events of today and the last 7 UTC days. Read-only.

    The receiver writes {"received_at", "event"} per line, one file per UTC day
    (events-YYYY-MM-DD.jsonl, gzipped after a few days). The event's own time
    decides the day of a line; a missing time falls back to received_at. Past
    days come from the cache when their file has not changed.
    """
    directory = directory or telemetry_dir()
    days = _utc_days(RERANK_DAYS, now)
    budget = budget or _Budget()
    today = _RerankPeriod()
    week = _RerankPeriod(seed=1)
    files_read = 0
    errors: list[str] = []
    for day in days:
        for name in (f"events-{day}.jsonl", f"events-{day}.jsonl.gz"):
            path = directory / name
            if not path.is_file():
                continue
            if budget.spent():
                budget.truncated.append(f"{name}: not read")
                continue
            try:
                period = _day_period(path, days, budget, cache=day != days[0], today=today if day == days[0] else None)
            except UNREADABLE:
                errors.append(name)
                continue
            files_read += 1
            week.merge(period)
    _forget_old_days(directory, days)
    return {
        "event": RERANK_EVENT,
        "today": today.view(),
        "week": week.view(),
        "days": days,
        "files_read": files_read,
        "errors": errors,
        "truncated": bool(budget.truncated),
        "truncated_reasons": budget.truncated,
        "limits": {"line_bytes": RERANK_LINE_LIMIT, "scan_bytes": RERANK_SCAN_BYTES, "scan_events": RERANK_SCAN_EVENTS, "duration_sample": RERANK_DURATION_SAMPLE},
        "note": "High-volume events are sampled by Honcho's HIGH_VOLUME_SAMPLE_RATE. The request counter is per UTC day across the API and the deriver, and only as current as the last event that reported it.",
    }


@app.get("/api/rerank/stats")
def get_rerank_stats():
    # allow_nan=False: a non-finite value must fail here, in a test, not silently reach the page as NaN.
    return Response(json.dumps(rerank_stats(), allow_nan=False), media_type="application/json")


RERANK_DEFAULT_CALLERS = ["dream", "dialectic", "prefetch"]
_EF_SEARCH = re.compile(r"hnsw\.ef_search\s*=\s*(\d+)")
_ITERATIVE_SCAN = re.compile(r"hnsw\.iterative_scan\s*=\s*(\w+)")


def parse_pgoptions(value) -> dict:
    """ef_search and the iterative scan mode from a PGOPTIONS string; None for what it does not set."""
    text = value if isinstance(value, str) else ""
    ef = _EF_SEARCH.search(text)
    scan = _ITERATIVE_SCAN.search(text)
    return {"ef_search": int(ef.group(1)) if ef else None, "iterative_scan": scan.group(1) if scan else None}


def deployed_pgoptions() -> dict:
    """The PGOPTIONS the Honcho containers run with: ef_search, iterative_scan and where they were read.

    Read from the environment of the containers in RESTART_ALLOWED, through the
    read-only docker proxy (Config.Env, only the PGOPTIONS entry is looked at). When no
    container can be read, this process's own PGOPTIONS is used (set the same value on the
    dashboard service). Otherwise ef_search is None and the page shows "unknown".
    """
    found: list[dict] = []
    if proxy_base():
        for name in allow_names():
            try:
                _status, raw = proxy_open("GET", "/containers/" + urllib.parse.quote(name, safe="") + "/json", PROXY_READ_TIMEOUT)
                info = json.loads(raw.decode("utf-8"))
            except (ProxyError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            env = (info.get("Config") or {}).get("Env") if isinstance(info, dict) else None
            for entry in env if isinstance(env, list) else []:
                if isinstance(entry, str) and entry.startswith("PGOPTIONS="):
                    parsed = parse_pgoptions(entry.partition("=")[2])
                    if parsed["ef_search"] is not None:
                        found.append(parsed)
    source = "containers"
    if not found:
        own = parse_pgoptions(os.environ.get("PGOPTIONS", ""))
        if own["ef_search"] is not None:
            found.append(own)
            source = "dashboard environment"
    if not found:
        return {"ef_search": None, "iterative_scan": None, "source": None}
    efs = [item["ef_search"] for item in found]
    return {"ef_search": min(efs), "iterative_scan": found[0]["iterative_scan"], "source": source, "differs": len(set(efs)) > 1}


@app.get("/api/rerank/warnings")
def rerank_warnings():
    settings, _meta = read_settings()
    settings = settings if isinstance(settings, dict) else relaxed_settings()
    global_block = settings.get("rerank") if isinstance(settings.get("rerank"), dict) else {}
    callers = global_block.get("callers")
    callers = callers if isinstance(callers, list) else RERANK_DEFAULT_CALLERS
    pg = deployed_pgoptions()
    ef = pg["ef_search"]
    blocks = [("global", global_block), ("defaults", settings.get("defaults"))]
    for section in ("peers", "pairs"):
        group = settings.get(section)
        blocks.extend((f"{section}.{name}", block) for name, block in (group.items() if isinstance(group, dict) else []))
    warnings = []
    for where, block in blocks:
        if not isinstance(block, dict):
            continue
        rerank = block if where == "global" else block.get("rerank")
        if not isinstance(rerank, dict):
            continue
        if where != "global" and rerank.get("enabled") is True and "dream" not in callers:
            warnings.append(f"{where}: this block has no effect because the global rerank callers lack dream")
        candidates = rerank.get("candidates")
        if type(candidates) is int and ef is not None and candidates > ef:
            warnings.append(f"{where}: candidates ({candidates}) exceeds ef_search ({ef}); the search returns at most ef_search rows unless the iterative scan fills the pool")
    return {"warnings": warnings, "ef_search": ef if ef is not None else "unknown", "iterative_scan": pg["iterative_scan"] or "unknown", "pgoptions_source": pg["source"]}


LLM_EVENT = "llm.call.completed"
EMBEDDING_EVENT = "embedding.call.completed"
INDEXED_EVENTS = (LLM_EVENT, RERANK_EVENT, EMBEDDING_EVENT, "dream.run", "dream.specialist", "dialectic.completed")
INDEXED_MARKS = tuple(f'"{kind}"'.encode() for kind in INDEXED_EVENTS)
INDEXED_FIELDS = frozenset((
    "run_id", "workspace_name", "call_purpose", "model", "resolved_model", "backend", "billing_route", "routing_evidence",
    "transport", "provider_input_tokens", "provider_output_tokens", "cache_read_tokens", "cache_creation_tokens",
    "usage_complete", "outcome", "was_stream", "was_fallback", "attempt", "error_class", "duration_ms", "provider", "tokens",
    "billed_units", "caller", "candidates_sent", "results_returned", "daily_requests", "daily_request_cap",
    "input_tokens_estimate", "total_duration_ms", "specialist_type", "created_observation_count", "reasoning_level",
))
# Bounds per refresh of the index: what one request may read of what is new.
EVENT_SCAN_BYTES = 8 * 1024 * 1024 * 1024
RUN_LOG_BYTES = 8_000_000
RUN_LOG_LINES = 20_000
_POLICY_LINE = re.compile(r"(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)\S*\s+(\S+?)/(\S+)\s+<-\s+\S+\s+(\{.*?\})\s+mode=")


class _EventFile:
    """What the Runs and Cost pages need from one events file, and how far it has been read."""

    def __init__(self) -> None:
        self.mtime = 0
        self.offset = 0
        self.read = False
        self.events: list[tuple] = []


_event_files: dict[Path, _EventFile] = {}
_event_files_lock = threading.Lock()


def _load_events(path: Path, budget: _Budget) -> _EventFile:
    """The file's entry, brought up to date by reading only what is new. A gzipped file is read once."""
    stat = path.stat()
    packed = path.suffix == ".gz"
    entry = _event_files.get(path)
    if entry is None or stat.st_size < entry.offset or (packed and stat.st_mtime_ns != entry.mtime):
        entry = _event_files[path] = _EventFile()
    if (packed and entry.read) or (not packed and entry.offset == stat.st_size):
        return entry
    consumed = None if packed else [entry.offset]
    if packed:
        entry.events = []
    try:
        for raw in _event_lines(path, budget, consumed):
            if not any(mark in raw for mark in INDEXED_MARKS):
                continue
            item = _parse_line(raw)
            event = item.get("event") if item else None
            data = event.get("data") if isinstance(event, dict) else None
            if not isinstance(data, dict) or event.get("type") not in INDEXED_EVENTS:
                continue
            if budget.events_left <= 0:
                budget.truncated.append("event budget")
                break
            budget.events_left -= 1
            when = event.get("time") or item.get("received_at") or ""
            entry.events.append((event["type"], when if isinstance(when, str) else "", event.get("id"),
                                 {key: value for key, value in data.items() if key in INDEXED_FIELDS}))
    finally:
        if packed:
            entry.mtime = stat.st_mtime_ns
            entry.read = not budget.truncated
        else:
            entry.offset = consumed[0]
    return entry


def telemetry_events(days: list[str]) -> tuple[list[tuple], list[str], bool]:
    """The indexed events dated in days, each once: (type, time, id, fields), the file names that failed, and truncated."""
    budget = _Budget(max_bytes=EVENT_SCAN_BYTES)
    directory = telemetry_dir()
    errors = []
    events = []
    seen = set()
    with _event_files_lock:
        for day in days:
            for suffix in (".jsonl", ".jsonl.gz"):
                path = directory / f"events-{day}{suffix}"
                if not path.is_file():
                    continue
                try:
                    entry = _load_events(path, budget)
                except (OSError, EOFError, zlib.error):
                    errors.append(path.name)
                    continue
                for event in entry.events:
                    if event[1][:10] not in days:
                        continue
                    if isinstance(event[2], str):
                        if event[2] in seen:
                            continue
                        seen.add(event[2])
                    events.append(event)
        keep = {directory / f"events-{day}{suffix}" for day in {*days, *_utc_days(RERANK_DAYS)} for suffix in (".jsonl", ".jsonl.gz")}
        for path in [path for path in _event_files if path not in keep]:
            del _event_files[path]
    return events, errors, bool(budget.truncated)


def run_records(days: list[str]) -> dict:
    """The runs.log records that started in days, by run id."""
    rows = {}
    for line in tail_lines(policy_log_path().with_name("runs.log"), RUN_LOG_LINES, RUN_LOG_BYTES) or []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or _secret(row):
            continue
        run_id = row.get("run_id")
        if isinstance(run_id, str) and isinstance(row.get("started"), str) and row["started"][:10] in days:
            rows[run_id] = row
    return rows


def policy_efforts() -> dict:
    """Per pair, in log order, each dream's two efforts: (time, deduction, induction)."""
    pairs: dict = {}
    for line in tail_lines(policy_log_path(), RUN_LOG_LINES, RUN_LOG_BYTES) or []:
        match = _POLICY_LINE.match(line)
        if not match:
            continue
        try:
            body = json.loads(match[5])
        except ValueError:
            continue
        pairs.setdefault(f"{match[3]}/{match[4]}", []).append(
            (f"{match[1]}T{match[2]}", body.get("deduction_effort"), body.get("induction_effort")))
    return pairs


def _efforts_at(entries: list, started: str) -> tuple:
    found = (None, None, None)
    for entry in entries:
        if entry[0] > started[:19]:
            break
        found = entry
    return found[1], found[2]


@app.get("/api/runs")
def get_runs(pair: str = "", date: str = ""):
    if date:
        try:
            if datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
                raise ValueError("date format")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    days = [date] if date else _utc_days(7)
    log = policy_log_path().with_name("runs.log")
    rows = {}
    allowed = ("run_id", "workspace_name", "observer", "observed", "started", "finished", "mode", "source", "backend", "stage_backends", "brief", "deduction_rounds", "induction_rounds", "rerank")
    for run_id, row in run_records(days).items():
        if not pair or f"{row.get('observer')}/{row.get('observed')}" == pair:
            rows[run_id] = {key: row.get(key) for key in allowed}
    observed = {run_id: {} for run_id in rows}
    periods = {run_id: _RerankPeriod() for run_id in rows}
    dreams = {run_id: {"duration_ms": None, "conclusions_created": None, "specialists": []} for run_id in rows}
    events, errors, truncated = telemetry_events(days) if rows else ([], [], False)
    for kind, when, _event_id, data in events:
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or run_id not in rows:
            continue
        workspace = rows[run_id].get("workspace_name")
        if workspace and data.get("workspace_name") != workspace:
            continue
        if kind == LLM_EVENT:
            fields = ("stage", "model", "backend", "billing_route", "evidence")
            values = (data.get("call_purpose"), data.get("model"), data.get("backend"),
                      data.get("billing_route"), data.get("routing_evidence"))
            values = tuple(public(value) if isinstance(value, str) else "unknown" for value in values)
            entry = observed[run_id].setdefault(values, {**dict(zip(fields, values)), "calls": 0})
            entry["calls"] += 1
        elif kind == RERANK_EVENT:
            periods[run_id].add(data, when)
        elif kind == "dream.run":
            dreams[run_id]["duration_ms"] = _finite_number(data.get("total_duration_ms"))
        elif kind == "dream.specialist":
            dreams[run_id]["specialists"].append({"type": public(data.get("specialist_type")), "duration_ms": _finite_number(data.get("duration_ms")), "conclusions_created": _finite_int(data.get("created_observation_count"))})
    costs = cost_stats(date)
    efforts = policy_efforts()
    for run_id, row in rows.items():
        row["observed_models"] = list(observed[run_id].values())
        row["rerank_stats"] = periods[run_id].view()
        row["dream"] = dreams[run_id]
        # Honcho counts per dream pass; dream.run carries no total.
        created = [item["conclusions_created"] for item in dreams[run_id]["specialists"] if item["conclusions_created"] is not None]
        row["dream"]["conclusions_created"] = sum(created) if created else None
        row["cost"] = costs["by_run"].get(json.dumps([row.get("workspace_name"), run_id]))
        row["deduction_effort"], row["induction_effort"] = _efforts_at(efforts.get(f"{row.get('observer')}/{row.get('observed')}", []), row["started"])
    return {"runs": sorted(rows.values(), key=lambda row: row["started"], reverse=True), "errors": errors,
            "truncated": truncated or (log.is_file() and log.stat().st_size > RUN_LOG_BYTES), "days": days}


def load_rates() -> tuple[dict, str | None]:
    path = Path(os.environ.get("RATES_FILE") or settings_path().with_name("rates.json"))
    try:
        if path.stat().st_size > 256000:
            return {}, "rate file is too large"
        rates = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rates, dict):
            return {}, "rate file must be an object"
    except (OSError, ValueError):
        return {}, "rate file missing or unreadable"
    # Accept the older benchmark price map without changing the user's file.
    legacy = rates.get("voyage")
    if isinstance(legacy, dict):
        canonical = rates.setdefault("rerank", {})
        if isinstance(canonical, dict):
            for model, price in legacy.items():
                canonical.setdefault(f"voyage/{model}", {"per_million_tokens": price})
    return rates, None


def _off_peak_factor(spec: dict, when: str) -> float:
    """1 inside the entry's peak windows (peak_utc "HH:MM-HH:MM" on peak_days "Mon-Fri"), off_peak_factor outside.

    An entry without the three fields, or a call without a UTC time, keeps the factor 1.
    """
    off_peak = _finite_number(spec.get("off_peak_factor"))
    days = re.fullmatch(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)-(Mon|Tue|Wed|Thu|Fri|Sat|Sun)", str(spec.get("peak_days")))
    windows = [re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)", str(w)) for w in spec.get("peak_utc") or []]
    try:
        stamp = datetime.fromisoformat(str(when).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return 1.0
    if off_peak is None or not days or not windows or not all(windows):
        return 1.0
    names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    if not names.index(days[1]) <= stamp.weekday() <= names.index(days[2]):
        return off_peak
    minute = stamp.hour * 60 + stamp.minute
    for w in windows:
        if int(w[1]) * 60 + int(w[2]) <= minute < int(w[3]) * 60 + int(w[4]):
            return 1.0
    return off_peak


def estimate_call(kind: str, data: dict, rates: dict, when: str = "") -> dict:
    model = data.get("model") if isinstance(data.get("model"), str) else "unknown"
    cli = rates.get("cli") if isinstance(rates.get("cli"), dict) else {}
    explicit = data.get("billing_route")
    route = "api" if kind == RERANK_EVENT else (explicit if explicit in ("api", "cli") else ("api" if data.get("transport") in ("anthropic", "gemini") else "cli" if cli.get(model) == "subscription" else "unknown"))
    result = {"billing_route": route, "estimated_usd": None, "tokens": None, "note": None,
              "route_conflict": explicit == "api" and cli.get(model) == "subscription"}
    if data.get("usage_complete") is False:
        result["note"] = "token usage incomplete"
        return result
    if kind == RERANK_EVENT:
        amounts = [("per_million_tokens", data.get("tokens"))]
        key = f"{data.get('provider')}/{model}"
        table = rates.get("rerank")
    else:
        pin = _finite_int(data.get("provider_input_tokens"))
        pout = _finite_int(data.get("provider_output_tokens"))
        read = _finite_int(data.get("cache_read_tokens", 0))
        write = _finite_int(data.get("cache_creation_tokens", 0))
        if any(value is None or value < 0 for value in (pin, pout, read, write)):
            result["note"] = "token usage unknown"
            return result
        if not any((pin, pout, read, write)) and (data.get("outcome") in ("error", "cancelled") or data.get("was_stream") is True):
            result["note"] = "token usage unavailable for failed/cancelled or streaming call"
            return result
        inclusive = data.get("transport") in ("openai", "gemini")
        if (read or write) and data.get("transport") not in ("openai", "gemini", "anthropic"):
            result["note"] = "cache token accounting unknown"
            return result
        fresh = pin - read - write if inclusive else pin
        if fresh < 0:
            result["note"] = "inconsistent cache token usage"
            return result
        amounts = [("input_per_million", fresh), ("output_per_million", pout),
                   ("cache_read_per_million", read), ("cache_creation_per_million", write)]
        key, table = model, rates.get("llm")
    counts = [_finite_int(value) for _name, value in amounts]
    if any(value is None or value < 0 for value in counts):
        result["note"] = "token usage unknown"
        return result
    result["tokens"] = _finite_out(sum(counts))
    if route == "cli":
        result["note"] = "subscription; tokens only"
        return result
    spec = table.get(key) if isinstance(table, dict) else None
    resolved = data.get("resolved_model")
    if (kind != RERANK_EVENT and not isinstance(spec, dict) and isinstance(table, dict)
            and data.get("routing_evidence") == "bridge_response" and isinstance(resolved, str)
            and isinstance(table.get(resolved), dict)):
        spec = table[resolved]
        result["note"] = "rate from observed resolved model: " + resolved
    if not isinstance(spec, dict):
        result["note"] = "no rate"
        return result
    if kind != RERANK_EVENT and isinstance(spec.get("long_context"), dict):
        tier = spec["long_context"]
        threshold = _finite_int(tier.get("above_input_tokens"))
        if threshold is None or threshold < 0 or not isinstance(tier.get("rates"), dict):
            result["note"] = "no rate: invalid long-context tier"
            return result
        if fresh + read + write > threshold:
            spec = {**spec, **tier["rates"]}
    cost = 0.0
    for name, count in amounts:
        if not count:
            continue
        price = _finite_number(spec.get(name))
        if price is None or price < 0:
            result["note"] = f"no rate: {name}"
            return result
        cost += count * price / 1_000_000
    cost *= _off_peak_factor(spec, when)
    result["estimated_usd"] = _finite_out(cost)
    if result["estimated_usd"] is None:
        result["note"] = "estimate outside numeric range"
    elif route == "unknown":
        result["note"] = "potential API estimate; billing route unknown"
    if result["estimated_usd"] is not None and isinstance(spec.get("estimate_note"), str):
        result["note"] = "; ".join(filter(None, (result["note"], spec["estimate_note"])))
    return result


def _call_status(kind: str, data: dict) -> str:
    outcome = data.get("outcome")
    if kind == RERANK_EVENT:
        if outcome is None:
            return "ok" if data.get("tokens") else "skipped"
        if outcome in ("reranked", "success"):
            return "ok"
        if isinstance(outcome, str) and outcome.startswith(("skipped", "noop")):
            return "skipped"
    if outcome in (None, "success"):
        return "ok"
    return "cancelled" if outcome == "cancelled" else "failed"


def _call_stage(kind: str, data: dict, levels: dict) -> str:
    """The Models page setting this call belongs to; rerank and embeddings have their own."""
    if kind == RERANK_EVENT:
        return "rerank"
    if kind == EMBEDDING_EVENT:
        return "embedding"
    purpose = data.get("call_purpose") if isinstance(data.get("call_purpose"), str) else "unknown"
    if purpose == "dialectic.answer":
        return "dialectic." + str(levels.get(data.get("run_id")) or "unknown")
    return purpose.split(".")[0] if purpose.startswith(("summary.", "deriver.")) else purpose


def _call_provider(kind: str, data: dict, configured: dict, models: dict, status: str) -> tuple[str, str]:
    """Who the call went to, as (provider, model), from the call, else what the run or the settings say."""
    model = data.get("resolved_model") or data.get("model")
    model = model if isinstance(model, str) else "unknown"
    if kind != LLM_EVENT:
        provider = data.get("provider")
        return provider if isinstance(provider, str) else "not recorded", model
    if isinstance(data.get("backend"), str):
        return data["backend"], model
    override = configured.get(data.get("run_id")) or ""
    if status != "ok" and ":" in override and data.get("was_fallback") is not True:
        return tuple(override.split(":", 1))
    if data.get("transport") in ("anthropic", "gemini"):
        return data["transport"], model
    return models.get(model, "not recorded"), model


def _embedding_estimate(data: dict, rates: dict) -> dict:
    tokens = _finite_int(data.get("input_tokens_estimate"))
    table = rates.get("embedding")
    spec = table.get(f"{data.get('provider')}/{data.get('model')}") if isinstance(table, dict) else None
    price = _finite_number(spec.get("per_million_tokens")) if isinstance(spec, dict) else None
    amount = tokens * price / 1_000_000 if tokens is not None and price is not None else None
    return {"billing_route": "api", "estimated_usd": _finite_out(amount), "tokens": tokens, "note": None if amount is not None else "no rate", "route_conflict": False}


def _detail_call(kind: str, when: str, data: dict, stage: str, provider: str, model: str, status: str, estimate: dict) -> dict:
    """One line of a run's call list."""
    fields = {"time": when, "kind": "rerank" if kind == RERANK_EVENT else "llm", "stage": stage, "provider": provider, "model": model,
              "route": estimate["billing_route"], "status": status, "usd": estimate["estimated_usd"], "tokens": estimate["tokens"],
              "note": estimate["note"], "duration_ms": _finite_number(data.get("duration_ms"))}
    if kind == RERANK_EVENT:
        fields.update(outcome=data.get("outcome"), caller=data.get("caller"), candidates=_finite_int(data.get("candidates_sent")), results=_finite_int(data.get("results_returned")))
    else:
        fields.update(attempt=_finite_int(data.get("attempt")), fallback=data.get("was_fallback") is True, error=data.get("error_class"),
                      input=_finite_int(data.get("provider_input_tokens")), output=_finite_int(data.get("provider_output_tokens")),
                      cache_read=_finite_int(data.get("cache_read_tokens")), cache_write=_finite_int(data.get("cache_creation_tokens")))
    return {key: public(value) if isinstance(value, str) else value for key, value in fields.items()}


def cost_stats(date: str = "", run_id: str = "") -> dict:
    days = [date] if date else _utc_days(7)
    rates, rate_error = load_rates()
    events, errors, truncated = telemetry_events(days)
    levels = {data.get("run_id"): data.get("reasoning_level") for kind, _when, _event_id, data in events if kind == "dialectic.completed"}
    configured = {key: row.get("backend") for key, row in run_records(days).items()}
    settings, _meta = read_settings()
    backends = ((settings or {}).get("providers") or {}).get("backends") if isinstance(settings, dict) else None
    models = {backend.get("model"): name for name, backend in (backends or {}).items() if isinstance(backend, dict)}
    entries = {}
    by_run = {}
    calls_list = []
    calls = priced = no_rate = subscription = unknown = 0
    billed = potential = 0.0
    potential_calls = 0
    for kind, when, _event_id, data in events:
        if kind not in (LLM_EVENT, RERANK_EVENT, EMBEDDING_EVENT) or (run_id and data.get("run_id") != run_id):
            continue
        status = _call_status(kind, data)
        stage = _call_stage(kind, data, levels)
        provider, model = _call_provider(kind, data, configured, models, status)
        estimate = _embedding_estimate(data, rates) if kind == EMBEDDING_EVENT else estimate_call(kind, data, rates, when)
        route, amount = estimate["billing_route"], estimate["estimated_usd"]
        fallback = data.get("was_fallback") is True
        rid = data.get("run_id") if isinstance(data.get("run_id"), str) else ""
        entry = entries.setdefault((stage, provider, model, route, when[:10], rid, status, fallback), [0, 0, 0.0])
        entry[0] += 1
        entry[1] += estimate["tokens"] or 0
        entry[2] += amount or 0
        run = None
        if rid:
            run_key = json.dumps([data.get("workspace_name"), rid])
            run = by_run.setdefault(run_key, {"estimated_usd": 0.0, "priced_calls": 0, "calls": 0, "subscription_calls": 0, "unknown_route_calls": 0,
                                              "llm_usd": 0.0, "rerank_usd": 0.0, "rerank_calls": 0, "tokens": 0, "failed": 0, "fallback": 0,
                                              "embedding_calls": 0, "embedding_tokens": 0})
        if kind == EMBEDDING_EVENT:
            if run:
                run["embedding_calls"] += 1
                run["embedding_tokens"] += estimate["tokens"] or 0
            continue
        calls += 1
        subscription += route == "cli"
        unknown += route == "unknown"
        no_rate += str(estimate["note"]).startswith("no rate")
        if route == "api" and amount is not None:
            billed += amount
            priced += 1
        if route == "unknown" and amount is not None:
            potential += amount
            potential_calls += 1
        if run_id:
            calls_list.append(_detail_call(kind, when, data, stage, provider, model, status, estimate))
        if run:
            run["calls"] += 1
            run["subscription_calls"] += route == "cli"
            run["unknown_route_calls"] += route == "unknown"
            run["tokens"] += estimate["tokens"] or 0
            run["failed"] += status in ("failed", "cancelled")
            run["fallback"] += fallback
            run["rerank_calls"] += kind == RERANK_EVENT
            run["rerank_usd" if kind == RERANK_EVENT else "llm_usd"] += amount or 0
            if route == "api" and amount is not None:
                run["estimated_usd"] += amount
                run["priced_calls"] += 1
    for run in by_run.values():
        run["estimated_usd"] = _finite_out(run["estimated_usd"]) if run["priced_calls"] else None
        run["llm_usd"] = _finite_out(run["llm_usd"])
        run["rerank_usd"] = _finite_out(run["rerank_usd"])
    calls_list.sort(key=lambda item: item["time"])
    return {"days": days, "estimated_usd": _finite_out(billed) if priced else None,
            "potential_api_usd": _finite_out(potential) if potential_calls else None,
            "calls": calls, "priced_calls": priced, "no_rate_calls": no_rate, "subscription_calls": subscription,
            "unknown_route_calls": unknown, "by_run": by_run, "call_list": calls_list,
            "entries": [[*(public(value) if isinstance(value, str) else value for value in key), *values[:2], _finite_out(values[2])] for key, values in entries.items()],
            "rate_error": rate_error, "errors": errors, "truncated": truncated}


@app.get("/api/cost")
def get_cost(date: str = "", run_id: str = ""):
    if date:
        try:
            if datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%d") != date:
                raise ValueError("date format")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    return cost_stats(date, run_id)
