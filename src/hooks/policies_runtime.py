"""Per-dream settings from settings.json. Installed into the deriver by deriver_main.py.

The file is re-read when its mtime changes. Brief, the two efforts and rounds resolve field
by field: pair, then peers[observer], then peers[observed], then defaults. A null or absent
field falls through, so an empty pair does not hide the peer blocks. source is the block
that supplied the first effort. Backend uses the same chain, then providers.active. The dream limits
in LIMITS resolve the same way and replace Honcho's [dream] value for that one dream; a
limit set nowhere keeps Honcho's value. The rerank block (RERANK_FIELDS) resolves the same
way; while a dream runs, src.honcho_rerank (when that package is installed) reads its
RerankSettings with those keys replaced, validated by the settings class itself, so a value
it refuses leaves the base settings in force and is logged. Outside a dream the wrapper
sees the base object unchanged. Without the package the dream hook still installs.

Instruction files live in INSTRUCTIONS_DIR (default /data/instructions, the instructions/
folder of the stack's DATA_DIR, mounted read-only into the deriver). For dream and peer-card
they are read on every dream: peers/<observer>/<kind>.md, then peers/<observed>/<kind>.md,
then backends/<backend>/<kind>.md, then default/<kind>.md, then the database value. The old
flat names still resolve for one release, and a deprecation line is logged when one is used.
A same-mtime cache is the only cache. An empty file clears that category. A missing folder
is left alone and is not an error.

Every file the hook writes is in the stack's LOGS_DIR, mounted at /logs: policy-used.log
(POLICY_LOG), runs.log beside it, and hook-status.json (POLICY_STATUS).
"""
from __future__ import annotations

import json
import inspect
import logging
import math
import os
import re
import importlib
import importlib.util
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# One per dream pass. A peer card refresh has no per-dream effort: its job on the Models page sets it.
STAGE_EFFORT = {"dream.deduction": "deduction_effort", "dream.induction": "induction_effort"}
MARK = "honcho-scheduler"
# The schedule route writes this. ScheduleDreamRequest has no trigger_reason field.
API_TRIGGER = "manual"
SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|AIza|Bearer |eyJ)")
BACKEND_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
# File infix, then the attribute on the resolved configuration.
CATEGORIES = (("dream", "dream"), ("peer-card", "peer_card"))
# settings.json field -> Honcho settings.DREAM attribute read during the dream, and its check.
LIMITS = {
    "brief_tokens": ("BRIEF_MAX_NEW_CONCLUSION_TOKENS", lambda v: type(v) is int and 500 <= v <= 100000),
    "tool_calls": ("MAX_TOOL_ITERATIONS", lambda v: type(v) is int and 1 <= v <= 50),
    "notes": ("NOTES_MODE", lambda v: v in ("off", "shadow", "on")),
    "notes_tokens": ("NOTES_MAX_TOKENS", lambda v: type(v) is int and 100 <= v <= 20000),
    "budget": ("BUDGET_ENABLED", lambda v: type(v) is bool),
    "write_window": ("WRITE_WINDOW_ITERATIONS", lambda v: type(v) is int and 1 <= v <= 50),
}
LIMIT_ATTRS = {attr: name for name, (attr, _check) in LIMITS.items()}
RERANK_PROVIDERS = ("voyage", "cohere", "jina", "tei")


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def valid_base_url(value) -> bool:
    """The fork's RerankSettings BASE_URL rule: http or https, a host, no userinfo, no query, no fragment.

    Honcho's HTTP client logs request URLs, so a credential in the URL would be logged;
    the key belongs in RERANK_API_KEY. Control characters and whitespace are refused
    the way the client's own parser refuses them.
    """
    if not isinstance(value, str) or not value:
        return False
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in value):
        return False
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if "@" in parsed.netloc or "?" in value or "#" in value:
        return False
    return True


# settings.json rerank key -> Honcho settings.RERANK attribute, and the check RerankSettings applies.
RERANK_FIELDS = {
    "enabled": ("ENABLED", lambda v: type(v) is bool),
    "provider": ("PROVIDER", lambda v: v in RERANK_PROVIDERS),
    # Printable only: the model is written into policy-used.log, one record per line.
    "model": ("MODEL", lambda v: isinstance(v, str) and bool(v.strip()) and v.isprintable()),
    "base_url": ("BASE_URL", valid_base_url),
    "candidates": ("CANDIDATES", lambda v: type(v) is int and 1 <= v <= 1000),
    "max_documents": ("MAX_DOCUMENTS", lambda v: type(v) is int and 1 <= v <= 10000),
    "timeout_seconds": ("TIMEOUT_SECONDS", lambda v: _finite(v) and v > 0),
    "min_score": ("MIN_SCORE", _finite),
    "daily_request_cap": ("DAILY_REQUEST_CAP", lambda v: type(v) is int and v >= 0),
}

_UNSET = object()
# The stack's LOGS_DIR inside every container. The hook writes nowhere else.
LOGS = Path("/logs")
_decision: ContextVar[Decision | None] = ContextVar("dream_decision", default=None)
_payload_mark: ContextVar[str | None] = ContextVar("dream_scheduled_by", default=None)
_payload_reason: ContextVar[str | None | object] = ContextVar("dream_trigger_reason", default=_UNSET)
# The pair's dream limits for the whole of process_dream, not only inside run_dream: Honcho reads
# working_memory_enabled() (BRIEF_ENABLED, NOTES_MODE) again in its bookkeeping after the dream, and a
# per-dream notes value seen inside the dream but not after it wiped the stored cursor.
_process_policy: ContextVar[dict | None] = ContextVar("dream_process_policy", default=None)
_text_cache: dict[Path, tuple[int, str]] = {}
_cache_lock = threading.Lock()


@dataclass
class Decision:
    policy: dict
    source: str
    mode: str
    backend: str
    backend_name: str | None
    gate: str
    observer: str
    observed: str
    # settings.json rerank keys for this dream, and the block that supplied them (None: no override).
    rerank: dict = field(default_factory=dict)
    rerank_source: str | None = None
    stage_backends: dict = field(default_factory=dict)
    card_refresh: bool = False
    # The validated RerankSettings for this dream, built on first use. _UNSET until then.
    rerank_resolved: object = field(default=_UNSET, repr=False, compare=False)


def default_settings_path() -> Path:
    return Path(os.environ.get("CONTROL_FILE") or "/config/settings.json")


def default_log_path() -> Path:
    return Path(os.environ.get("POLICY_LOG") or LOGS / "policy-used.log")


def default_status_path() -> Path:
    return Path(os.environ.get("POLICY_STATUS") or LOGS / "hook-status.json")


def default_instructions_dir() -> Path:
    return Path(os.environ.get("INSTRUCTIONS_DIR") or "/data/instructions")


def instruction_cap() -> int:
    """Honcho's cap. DeriverSettings.MAX_CUSTOM_INSTRUCTIONS_TOKENS, default 2000."""
    try:
        from src.config import settings

        return int(settings.DERIVER.MAX_CUSTOM_INSTRUCTIONS_TOKENS)
    except Exception:
        return 2000


def token_count(text: str | None) -> int:
    """Honcho's estimate_tokens. A missing import estimates 4 characters per token."""
    if not text:
        return 0
    try:
        from src.utils.tokens import estimate_tokens
    except Exception:
        return len(text) // 4
    return int(estimate_tokens(text))


def _secret(value) -> bool:
    if isinstance(value, str):
        return SECRET.search(value) is not None
    if isinstance(value, dict):
        return any(_secret(key) or _secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_secret(item) for item in value)
    return False


def _block_secret(block) -> bool:
    """Secret-like text in one dream block, ignoring its rerank object."""
    if not isinstance(block, dict):
        return _secret(block)
    for key, value in block.items():
        if key == "rerank":
            continue
        if _secret(key) or _secret(value):
            return True
    return False


def _hook_secret(data: dict) -> bool:
    """Secret scan of the values the hook reads: mode, defaults, peers, pairs, providers.active, providers.stages."""
    if _secret(data.get("mode")):
        return True
    if _block_secret(data.get("defaults")):
        return True
    for section in ("peers", "pairs"):
        blocks = data.get(section)
        if isinstance(blocks, dict):
            for name, block in blocks.items():
                if _secret(name) or _block_secret(block):
                    return True
        elif _secret(blocks):
            return True
    providers = data.get("providers")
    if isinstance(providers, dict):
        if _secret(providers.get("active")) or _secret(providers.get("stages")):
            return True
    return False


def _restore_rerank_block(block: dict, previous) -> None:
    if isinstance(previous, dict) and "rerank" in previous:
        block["rerank"] = json.loads(json.dumps(previous["rerank"]))
    else:
        block.pop("rerank", None)


def _with_previous_rerank(data: dict, previous: dict) -> dict:
    """Apply data, but keep the rerank blocks from the last good settings."""
    restored = json.loads(json.dumps(data))
    if isinstance(previous, dict) and "rerank" in previous:
        restored["rerank"] = json.loads(json.dumps(previous["rerank"]))
    else:
        restored.pop("rerank", None)
    old_defaults = previous.get("defaults") if isinstance(previous, dict) else None
    if isinstance(restored.get("defaults"), dict):
        _restore_rerank_block(restored["defaults"], old_defaults if isinstance(old_defaults, dict) else {})
    for section in ("peers", "pairs"):
        new_section = restored.get(section)
        old_section = previous.get(section) if isinstance(previous, dict) else None
        if not isinstance(new_section, dict):
            continue
        old_map = old_section if isinstance(old_section, dict) else {}
        for name, block in new_section.items():
            if isinstance(block, dict):
                _restore_rerank_block(block, old_map.get(name) if isinstance(old_map.get(name), dict) else {})
    return restored


def split_ref(ref: str | None) -> tuple[str | None, str | None]:
    if not isinstance(ref, str) or not ref:
        return None, None
    if ":" in ref:
        name, model = ref.split(":", 1)
        return name, model or None
    return ref, None


def lookup_block(data: dict, observer: str, observed: str) -> tuple[dict, str]:
    """Each field walks pair, then peers[observer], then peers[observed], then defaults.

    A null or absent field falls through. An empty pair does not hide the peer blocks.
    source is the block that supplied the first effort: the pair key, the observer, the
    observed, or default.
    """
    pairs = data.get("pairs") or {}
    peers = data.get("peers") or {}
    defaults = data.get("defaults") or {}
    key = f"{observer}/{observed}"
    chain: list[tuple[dict, str]] = []
    if isinstance(pairs, dict) and isinstance(pairs.get(key), dict):
        chain.append((pairs[key], key))
    if isinstance(peers, dict) and isinstance(peers.get(observer), dict):
        chain.append((peers[observer], observer))
    if isinstance(peers, dict) and isinstance(peers.get(observed), dict):
        chain.append((peers[observed], observed))
    if isinstance(defaults, dict):
        chain.append((defaults, "default"))
    merged: dict = {}
    source = None
    for key_name in ("brief", *STAGE_EFFORT.values(), "deduction_rounds", "induction_rounds", *LIMITS):
        for block, name in chain:
            if key_name in block and block[key_name] is not None:
                merged[key_name] = block[key_name]
                if key_name in STAGE_EFFORT.values() and source is None:
                    source = name
                break
    return merged, source or "default"


def lookup_rerank(data: dict, observer: str, observed: str) -> tuple[dict, str | None]:
    """Each rerank key walks pair, then peers[observer], then peers[observed], then defaults.

    A null or absent key falls through. source is the first block that supplied a key, or
    None when no block does. Keys outside RERANK_FIELDS are ignored (never an api_key).
    """
    pairs = data.get("pairs") or {}
    peers = data.get("peers") or {}
    defaults = data.get("defaults") or {}
    key = f"{observer}/{observed}"
    chain: list[tuple[dict, str]] = []
    if isinstance(pairs, dict) and isinstance(pairs.get(key), dict):
        chain.append((pairs[key], key))
    if isinstance(peers, dict) and isinstance(peers.get(observer), dict):
        chain.append((peers[observer], observer))
    if isinstance(peers, dict) and isinstance(peers.get(observed), dict):
        chain.append((peers[observed], observed))
    if isinstance(defaults, dict):
        chain.append((defaults, "default"))
    merged: dict = {}
    source = None
    for block, name in chain:
        rerank = block.get("rerank")
        if not isinstance(rerank, dict):
            continue
        for name_key in RERANK_FIELDS:
            if name_key in merged or rerank.get(name_key) is None:
                continue
            merged[name_key] = rerank[name_key]
            if source is None:
                source = name
    return merged, source


def format_rerank_line(observer: str, observed: str, source: str | None, rerank: dict) -> str:
    """One line without braces or an arrow, so the archive's dream regex never matches it.

    A base_url is never printed, valid or not: a URL can carry a credential. The line says base_url=set.
    """
    values = " ".join(
        f"{name}={'set' if name == 'base_url' else rerank[name]}" for name in RERANK_FIELDS if name in rerank
    )
    return f"rerank for {observer}/{observed} from {source or 'none'}: {values}".rstrip(": ")


def resolve_rerank(base, decision: Decision, log):
    """Honcho's RerankSettings with the dream's keys replaced, validated by the settings class.

    The merged dict is checked with model_validate, so a value RerankSettings refuses
    (for instance a base_url httpx cannot parse) leaves the base object in force and is
    logged once per dream. The result is kept on the decision.
    """
    if decision.rerank_resolved is not _UNSET:
        return decision.rerank_resolved
    resolved = base
    clamped = None
    try:
        merged = dict(base.model_dump())
        for name, value in decision.rerank.items():
            attr = RERANK_FIELDS.get(name)
            if attr is not None:
                merged[attr[0]] = value
        base_cap = base.DAILY_REQUEST_CAP
        dream_cap = merged.get("DAILY_REQUEST_CAP")
        if base_cap is not None and type(dream_cap) is int and dream_cap > base_cap:
            merged["DAILY_REQUEST_CAP"] = base_cap
            clamped = f"rerank cap clamped: dream={dream_cap} base={base_cap}"
        # The global switch is the kill switch: a dream may turn rerank off, never on.
        if not base.ENABLED and merged.get("ENABLED"):
            merged["ENABLED"] = False
            clamped = "rerank kept off: the global switch is off"
        # RERANK_API_KEY was issued for the global provider and host. A dream that names another
        # provider or base_url gets no key, so the key never reaches a host it was not meant for.
        if merged.get("PROVIDER") != base.PROVIDER or merged.get("BASE_URL") != base.BASE_URL:
            merged["API_KEY"] = None
        resolved = type(base).model_validate(merged)
    except Exception as exc:
        try:
            log(f"rerank not applied {decision.observer}/{decision.observed} from {decision.rerank_source}: {type(exc).__name__}")
        except Exception:
            pass
        resolved = base
    if clamped is not None and resolved is not base:
        try:
            log(clamped)
        except Exception:
            pass
    decision.rerank_resolved = resolved
    return resolved


class RerankSettingsProxy:
    """Stands in for src.honcho_rerank.settings inside the wrapper.

    RERANK is Honcho's RerankSettings with the dream's rerank keys applied while a dream
    decision is set, and the base object unchanged outside a dream. Every other attribute
    passes through.
    """

    def __init__(self, base, log):
        self._base = base
        self._log = log

    def __getattr__(self, name):
        if name == "RERANK":
            decision = _decision.get()
            base = getattr(self._base, "RERANK")
            if decision is None or not decision.rerank:
                return base
            return resolve_rerank(base, decision, self._log)
        return getattr(self._base, name)


def lookup_backend(data: dict, observer: str, observed: str, *, include_active: bool = True) -> str | None:
    """pair, then peer[observer], then peer[observed], then defaults.backend, then providers.active.

    A null or absent backend falls through. Brief, effort and rounds use the same chain
    in lookup_block and stop at defaults.
    """
    pairs = data.get("pairs") or {}
    peers = data.get("peers") or {}
    defaults = data.get("defaults") or {}
    key = f"{observer}/{observed}"
    blocks = []
    if isinstance(pairs, dict) and isinstance(pairs.get(key), dict):
        blocks.append(pairs[key])
    if isinstance(peers, dict) and isinstance(peers.get(observer), dict):
        blocks.append(peers[observer])
    if isinstance(peers, dict) and isinstance(peers.get(observed), dict):
        blocks.append(peers[observed])
    if isinstance(defaults, dict):
        blocks.append(defaults)
    for block in blocks:
        ref = block.get("backend")
        if isinstance(ref, str) and ref:
            return ref
    active = (data.get("providers") or {}).get("active") if include_active and isinstance(data.get("providers"), dict) else None
    if isinstance(active, str) and active:
        return active
    return None


def peer_allows(data: dict, name: str) -> bool:
    """False only when the peer is listed and enabled is false. An unlisted peer is not disabled."""
    peers = data.get("peers") or {}
    block = peers.get(name) if isinstance(peers, dict) else None
    if not isinstance(block, dict):
        return True
    return block.get("enabled") is not False


def decide_gate(
    mode: str,
    scheduled_by: str | None,
    observer_on: bool,
    observed_on: bool,
    trigger_reason: str | None = None,
) -> str:
    """Scheduler mode runs a marked dream. scheduled_by wins when it is present.

    With no scheduled_by, the dream runs only when trigger_reason is the value
    the schedule route writes (manual). Honcho's own reasons are skipped.
    """
    if not observer_on or not observed_on:
        return "skipped-disabled"
    if mode == "scheduler" and not _scheduler_allows(scheduled_by, trigger_reason):
        return "skipped-mode"
    return "ran"


def _scheduler_allows(scheduled_by: str | None, trigger_reason: str | None) -> bool:
    if scheduled_by is not None:
        return scheduled_by == MARK
    return trigger_reason == API_TRIGGER


def format_used_line(observer: str, observed: str, source: str, policy: dict, mode: str, backend: str, gate: str) -> str:
    """Old columns, then mode backend gate. The JSON keeps the names the archive reads."""
    body = {}
    if "brief" in policy:
        body["max_conclusions"] = policy["brief"]
    for name in STAGE_EFFORT.values():
        if name in policy:
            body[name] = policy[name]
    if "deduction_rounds" in policy:
        body["deduction_rounds"] = policy["deduction_rounds"]
    if "induction_rounds" in policy:
        body["induction_rounds"] = policy["induction_rounds"]
    for name in LIMITS:
        if name in policy:
            body[name] = policy[name]
    return (
        f"{observer}/{observed} <- {source} {json.dumps(body)}"
        f" mode={mode} backend={backend} gate={gate}"
    )


def _read_cached(path: Path) -> str:
    mtime = path.stat().st_mtime_ns
    with _cache_lock:
        hit = _text_cache.get(path)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    text = path.read_text(encoding="utf-8")
    with _cache_lock:
        _text_cache[path] = (mtime, text)
    return text


def _safe_peer(name: str | None) -> bool:
    """One path segment. Rejects traversal, a drive prefix, and a secret-like name."""
    if not isinstance(name, str) or not name or name in {".", ".."} or name.startswith("."):
        return False
    if len(name) > 512:
        return False
    if any(char in name for char in "/\\:\0"):
        return False
    if any(ord(char) < 32 for char in name):
        return False
    if SECRET.search(name):
        return False
    return Path(name).name == name


def _file_under(directory: Path, relative: Path) -> Path | None:
    candidate = directory / relative
    try:
        candidate.resolve().relative_to(directory.resolve())
    except (OSError, ValueError):
        return None
    if candidate.is_file():
        return candidate
    return None


def _locate_instruction(
    directory: Path,
    backend_name: str | None,
    category: str,
    observer: str | None,
    observed: str | None,
) -> tuple[Path, str, str | None] | None:
    """First existing file in the dream chain. The third item is the old flat name when that file won."""
    checks: list[tuple[Path, str, str | None]] = []
    if _safe_peer(observer):
        checks.append((Path("peers") / observer / f"{category}.md", f"settings.peer.{observer}.{category}", None))
    if _safe_peer(observed) and observed != observer:
        checks.append((Path("peers") / observed / f"{category}.md", f"settings.peer.{observed}.{category}", None))
    if backend_name and BACKEND_NAME.match(backend_name):
        flat = f"{backend_name}-{category}-instructions.md"
        checks.append((Path("backends") / backend_name / f"{category}.md", f"settings.backend.{backend_name}.{category}", None))
        checks.append((Path(flat), f"settings.backend.{backend_name}.{category}", flat))
    default_flat = f"default-{category}-instructions.md"
    checks.append((Path("default") / f"{category}.md", f"settings.default.{category}", None))
    checks.append((Path(default_flat), f"settings.default.{category}", default_flat))
    for relative, source, flat in checks:
        path = _file_under(directory, relative)
        if path is not None:
            return path, source, flat
    return None


def _cap_who(source: str, category: str) -> str:
    suffix = f".{category}"
    for prefix in ("settings.backend.", "settings.peer."):
        if source.startswith(prefix) and source.endswith(suffix) and len(source) > len(prefix) + len(suffix):
            return source[len(prefix):-len(suffix)]
    return "default"


def resolve_instruction(
    directory: Path | None,
    backend_name: str | None,
    category: str,
    observer: str | None = None,
    observed: str | None = None,
    log=None,
) -> tuple[str | None, str | None]:
    """Return (text, source). (None, None) leaves the database value.

    An empty file is an explicit clear: text is "" and source names the scope.
    Dream and peer-card only. The chain is observer peer, observed peer, backend, default.
    """
    if category not in ("dream", "peer-card"):
        return None, None
    if directory is None or not directory.is_dir():
        return None, None
    found = _locate_instruction(directory, backend_name, category, observer, observed)
    if found is None:
        return None, None
    path, source, flat = found
    text = _read_cached(path)
    if flat and log is not None:
        try:
            log(f"instructions deprecated file={flat}")
        except Exception:
            pass
    if not text.strip():
        return "", source
    return text, source


def apply_instructions(
    configuration,
    backend_name: str | None,
    directory: Path | None,
    log,
    observer: str | None = None,
    observed: str | None = None,
) -> None:
    """Replace dream and peer-card instructions when a file resolves. Missing folder: no log, no change."""
    if directory is None or not directory.is_dir():
        return
    cap = instruction_cap()
    for category, attr in CATEGORIES:
        try:
            text, source = resolve_instruction(directory, backend_name, category, observer, observed, log)
        except OSError:
            log(f"instructions unreadable category={category}")
            continue
        if source is None:
            continue
        try:
            count = token_count(text)
        except Exception:
            count = len(text or "") // 4
        if text and count > cap:
            log(f"instructions over cap category={category} backend={_cap_who(source, category)} tokens={count} cap={cap}")
        block = getattr(configuration, attr, None)
        if block is None:
            continue
        try:
            block.custom_instructions = text
            block.custom_instructions_source = source
        except Exception:
            log(f"instructions not applied category={category}")


def _stamp_model(value, decision: Decision, policy: dict, stage: str | None = None):
    if value is None:
        return None
    try:
        stamped = value.model_copy(deep=True)
    except Exception:
        return value
    try:
        params = stamped.overrides.provider_params
        if not isinstance(params, dict):
            params = {}
            stamped.overrides.provider_params = params
        extra = params.get("extra_body")
        if not isinstance(extra, dict):
            extra = {}
            params["extra_body"] = extra
    except Exception:
        return stamped
    # Only the openai transport goes to the bridge. A direct provider (anthropic, gemini) gets no bridge fields: the
    # Messages API refuses unknown top-level keys, and Honcho maps thinking_effort for it (minimal to low) itself.
    bridge = getattr(stamped, "transport", "openai") == "openai"
    # none (thinking off) goes to the bridge only. On a direct provider Honcho 3.5.0 cannot use it: the anthropic transport
    # drops it (_anthropic_effort sends no effort, so thinking stays as it was) and the gemini transport would send
    # thinking_level "none", which Google does not list (minimal, low, medium, high). The job keeps its own setting there.
    effort = policy.get(STAGE_EFFORT.get(stage))
    if effort is not None and (bridge or effort != "none"):
        try:
            stamped.thinking_effort = effort
        except Exception:
            pass
        output = extra.get("output_config")
        if bridge:
            if not isinstance(output, dict):
                output = {}
                extra["output_config"] = output
            output["effort"] = effort
        elif isinstance(output, dict) and "effort" in output:
            # A raw effort here would override Honcho's own mapping of thinking_effort.
            del output["effort"]
            if not output:
                del extra["output_config"]
    if bridge:
        extra["backend"] = decision.stage_backends.get(stage, decision.backend)
        extra["observer"] = decision.observer
        extra["observed"] = decision.observed
    return stamped


class DreamSettingsProxy:
    def __init__(self, base):
        self._base = base

    def __getattr__(self, name):
        decision = _decision.get()
        if decision is None:
            policy = _process_policy.get()
            if policy is None:
                return getattr(self._base, name)
            # Outside run_dream only the limits apply; model configs are stamped per dream.
            limit = LIMIT_ATTRS.get(name)
            if limit is not None and limit in policy:
                return policy[limit]
            if name == "BRIEF_MAX_NEW_CONCLUSIONS" and "brief" in policy:
                return policy["brief"]
            return getattr(self._base, name)
        policy = decision.policy
        if name == "BRIEF_MAX_NEW_CONCLUSIONS" and "brief" in policy:
            return policy["brief"]
        if name == "DEDUCTION_MAX_TOOL_ITERATIONS" and "deduction_rounds" in policy:
            return policy["deduction_rounds"]
        if name == "INDUCTION_MAX_TOOL_ITERATIONS" and "induction_rounds" in policy:
            return policy["induction_rounds"]
        limit = LIMIT_ATTRS.get(name)
        if limit is not None and limit in policy:
            return policy[limit]
        value = getattr(self._base, name)
        if name in ("DEDUCTION_MODEL_CONFIG", "INDUCTION_MODEL_CONFIG"):
            stage = "peer_card" if decision.card_refresh else ("dream.deduction" if name == "DEDUCTION_MODEL_CONFIG" else "dream.induction")
            return _stamp_model(value, decision, policy, stage)
        return value


class SettingsProxy:
    def __init__(self, base):
        self._base = base
        self.DREAM = DreamSettingsProxy(base.DREAM)

    def __getattr__(self, name):
        return getattr(self._base, name)


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


class Policies:
    def __init__(self, path: Path | None = None, log_path: Path | None = None, *, check_rerank: bool = True):
        self.path = Path(path) if path is not None else default_settings_path()
        self.log_path = Path(log_path) if log_path is not None else default_log_path()
        self.data: dict = {"mode": "stock", "defaults": {}, "peers": {}, "pairs": {}, "providers": {}}
        self.mtime: int | None = None
        self._lock = threading.Lock()
        self.status_path = default_status_path()
        self.installed_at: str | None = None
        self.last_reload_error: str | None = None
        self.base_dream = None
        self.rerank_settings = None
        self.check_rerank = check_rerank
        # A settings.json that exists but was never applied gates every dream (skipped-settings):
        # falling back to stock would let Honcho's own dreams run in a deployment set to scheduler.
        self.applied = False
        self.settings_present = False
        self.reload()

    def log_run(self, result, decision: Decision, started: str, workspace_name: str | None = None) -> None:
        run_id = getattr(result, "run_id", None)
        if not isinstance(run_id, str) or not run_id:
            return
        dream = DreamSettingsProxy(self.base_dream)
        base_rerank = self.rerank_settings.RERANK if self.rerank_settings is not None else None
        rerank = resolve_rerank(base_rerank, decision, self.log) if base_rerank is not None else None
        row = {
            "run_id": run_id, "workspace_name": workspace_name, "observer": decision.observer, "observed": decision.observed,
            "started": started, "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": decision.mode, "source": decision.source, "backend": decision.backend,
            "stage_backends": decision.stage_backends,
            "brief": getattr(dream, "BRIEF_MAX_NEW_CONCLUSIONS", None),
            "deduction_rounds": getattr(dream, "DEDUCTION_MAX_TOOL_ITERATIONS", None),
            "induction_rounds": getattr(dream, "INDUCTION_MAX_TOOL_ITERATIONS", None),
            "rerank": {"source": decision.rerank_source},
        }
        for key in ("enabled", "provider", "model", "candidates", "min_score", "daily_request_cap"):
            row["rerank"][key] = getattr(rerank, RERANK_FIELDS[key][0], None)
        row["rerank"]["base_url_set"] = bool(getattr(rerank, "BASE_URL", None))
        try:
            path = self.log_path.with_name("runs.log")
            path.parent.mkdir(parents=True, exist_ok=True)
            limit, keep = log_limits(self.data)
            with self._lock:
                rotate_log(path, limit, keep)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, allow_nan=False) + "\n")
        except (OSError, ValueError):
            logging.getLogger(__name__).error("dream run record could not be written")

    def mark_installed(self) -> None:
        self.installed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write_status()

    def write_status(self) -> None:
        status = {
            "installed_at": self.installed_at,
            "checks_passed": self.installed_at is not None,
            "settings_path": str(self.path),
            "settings_mtime_applied": self.mtime,
            "last_reload_error": self.last_reload_error,
            "mode": self.data.get("mode", "stock"),
        }
        sha = os.environ.get("HONCHO_FORK_SHA", "")
        if re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
            status["fork_sha"] = sha
        temporary = self.status_path.with_name(self.status_path.name + f".tmp-{os.getpid()}")
        try:
            with self._lock:
                self.status_path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps(status) + "\n", encoding="utf-8")
                os.replace(temporary, self.status_path)
        except OSError:
            logging.getLogger(__name__).error("hook status could not be written")
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def instructions_dir(self) -> Path:
        return default_instructions_dir()

    def log(self, *parts: str) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            line = time.strftime("%Y-%m-%d %H:%M:%SZ ", time.gmtime()) + " ".join(parts) + "\n"
            limit, keep = log_limits(self.data)
            with self._lock:
                rotate_log(self.log_path, limit, keep)
                with open(self.log_path, "a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError:
            pass

    def reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError as exc:
            self.log("policies reloaded not applied:", type(exc).__name__)
            self.last_reload_error = type(exc).__name__
            self.settings_present = False
            self.write_status()
            return
        self.settings_present = True
        if mtime == self.mtime:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("settings file must be an object")
            if _hook_secret(data):
                raise ValueError("secret-like string")
            self._check(data)
        except Exception as exc:
            self.log("policies reloaded not applied:", type(exc).__name__)
            # Exception messages may include settings values. Store only the class.
            self.last_reload_error = type(exc).__name__
            self.write_status()
            return
        rerank_error = self._rerank_error(data)
        if rerank_error is not None:
            self.log("rerank settings not applied:", type(rerank_error).__name__)
            data = _with_previous_rerank(data, self.data)
        self.data = data
        self.mtime = mtime
        self.applied = True
        self.log("policies reloaded", str(self.path))
        self.last_reload_error = None
        self.write_status()

    def _check(self, data: dict) -> None:
        mode = data.get("mode", "stock")
        if mode not in ("stock", "scheduler"):
            raise ValueError("mode")
        blocks = [data.get("defaults") or {}]
        peers = data.get("peers") or {}
        pairs = data.get("pairs") or {}
        if isinstance(peers, dict):
            blocks.extend(block for block in peers.values() if isinstance(block, dict))
        if isinstance(pairs, dict):
            blocks.extend(block for block in pairs.values() if isinstance(block, dict))
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for name in STAGE_EFFORT.values():
                effort = block.get(name)
                if effort is not None and effort not in EFFORTS:
                    raise ValueError(name)
            for name in ("brief", "deduction_rounds", "induction_rounds"):
                value = block.get(name)
                if value is not None and (type(value) is not int or value <= 0):
                    raise ValueError(name)
                if name != "brief" and value is not None and value > 100:
                    self.log(f"policy rejected: {name}: must be between 1 and 100")
                    raise ValueError(name)
            for name, (_attr, valid) in LIMITS.items():
                value = block.get(name)
                if value is not None and not valid(value):
                    raise ValueError(name)

    def _rerank_error(self, data: dict):
        """Invalid rerank blocks are not a reason to reject the rest of the file."""
        if not self.check_rerank:
            return None
        try:
            if _secret(data.get("rerank")):
                raise ValueError("secret-like string")
            self._check_rerank(data.get("rerank"), global_block=True)
            blocks = [data.get("defaults") or {}]
            peers = data.get("peers") or {}
            pairs = data.get("pairs") or {}
            if isinstance(peers, dict):
                blocks.extend(block for block in peers.values() if isinstance(block, dict))
            if isinstance(pairs, dict):
                blocks.extend(block for block in pairs.values() if isinstance(block, dict))
            for block in blocks:
                if isinstance(block, dict):
                    rerank = block.get("rerank")
                    if _secret(rerank):
                        raise ValueError("secret-like string")
                    self._check_rerank(rerank)
        except Exception as exc:
            return exc
        return None

    @staticmethod
    def _check_rerank(rerank, *, global_block: bool = False) -> None:
        """Validate through the wrapper, or the JSON schema when it is unavailable."""
        if rerank is None:
            return
        if not isinstance(rerank, dict):
            raise ValueError("rerank")
        allowed = set(RERANK_FIELDS)
        if global_block:
            allowed.update(("max_query_chars", "callers", "failures_before_cooldown", "cooldown_seconds"))
        for name, value in rerank.items():
            if not isinstance(name, str) or "key" in name.lower():
                raise ValueError("rerank.api_key")
            if name not in allowed:
                raise ValueError(f"rerank.{name}")
            if value is not None and name in RERANK_FIELDS and not RERANK_FIELDS[name][1](value):
                raise ValueError(f"rerank.{name}")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"rerank.{name}")
        values = {name: value for name, value in rerank.items() if value is not None}
        try:
            settings_class = importlib.import_module("src.honcho_rerank.settings").RerankSettings
        except ImportError:
            from jsonschema import Draft202012Validator

            schema_path = Path(__file__).resolve().parents[1] / "config" / "settings.schema.json"
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            definition = "rerankGlobal" if global_block else "rerankOverride"
            Draft202012Validator({"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]}).validate(values)
        else:
            settings_class.model_validate({name.upper(): value for name, value in values.items()})

    def decide(
        self,
        observer: str,
        observed: str,
        scheduled_by: str | None,
        trigger_reason: str | None = None,
    ) -> Decision:
        self.reload()
        policy, source = lookup_block(self.data, observer, observed)
        ref = lookup_backend(self.data, observer, observed)
        name, _model = split_ref(ref)
        if name is not None and not BACKEND_NAME.match(name):
            name = None
            ref = None
        mode = self.data.get("mode") if self.data.get("mode") in ("stock", "scheduler") else "stock"
        gate = decide_gate(
            mode,
            scheduled_by,
            peer_allows(self.data, observer),
            peer_allows(self.data, observed),
            trigger_reason,
        )
        if gate == "ran" and self.settings_present and not self.applied:
            gate = "skipped-settings"
        if self.check_rerank:
            rerank, rerank_source = lookup_rerank(self.data, observer, observed)
        else:
            rerank, rerank_source = {}, None
        providers = self.data.get("providers")
        stages = (providers.get("stages") if isinstance(providers, dict) else None) or {}
        stage_backends = {}
        if lookup_backend(self.data, observer, observed, include_active=False) is None and isinstance(stages, dict):
            stage_backends = {stage: f"@{stage}" for stage in ("dream.deduction", "dream.induction", "peer_card") if stages.get(stage)}
        return Decision(
            policy=policy,
            source=source,
            mode=mode,
            backend=ref or "none",
            backend_name=name,
            gate=gate,
            observer=observer,
            observed=observed,
            rerank=rerank,
            rerank_source=rerank_source,
            stage_backends=stage_backends,
        )

    def log_decision(self, decision: Decision) -> None:
        self.log(
            format_used_line(
                decision.observer,
                decision.observed,
                decision.source,
                decision.policy,
                decision.mode,
                decision.backend,
                decision.gate,
            )
        )
        if self.check_rerank and decision.rerank and decision.gate == "ran":
            self.log(format_rerank_line(decision.observer, decision.observed, decision.rerank_source, decision.rerank))


def _wrap_run(original, policies: Policies, *, card_refresh: bool = False):
    @wraps(original)
    async def run(workspace_name, observer, observed, *args, **kwargs):
        mark = kwargs.pop("scheduled_by", None)
        if mark is None:
            mark = _payload_mark.get()
        reason = _payload_reason.get()
        if reason is _UNSET:
            reason = kwargs.get("trigger_reason")
        decision = policies.decide(observer, observed, mark, reason)
        decision.card_refresh = card_refresh
        policies.log_decision(decision)
        if decision.gate != "ran":
            return None
        token = _decision.set(decision)
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            result = await original(workspace_name, observer, observed, *args, **kwargs)
            try:
                policies.log_run(result, decision, started, workspace_name)
            except Exception:
                logging.getLogger(__name__).error("dream run record could not be collected")
            return result
        finally:
            _decision.reset(token)

    run.__policies_installed__ = True
    return run


RERANK_PACKAGE = "src.honcho_rerank"
LEGACY_RERANK = "src.rerank"


def import_rerank():
    """Import the honcho_rerank wrapper.

    Returns None when the package is not installed. Raises RuntimeError when only the old
    fork module src.rerank is present, or when the package fails to import for another reason.
    """
    if RERANK_PACKAGE in sys.modules:
        return sys.modules[RERANK_PACKAGE]
    try:
        return importlib.import_module(RERANK_PACKAGE)
    except ModuleNotFoundError as exc:
        if exc.name not in (RERANK_PACKAGE, "src"):
            raise RuntimeError(f"hook requires {RERANK_PACKAGE}, which failed to import: {exc.name} is missing") from exc
        legacy = LEGACY_RERANK in sys.modules
        if not legacy:
            try:
                legacy = importlib.util.find_spec(LEGACY_RERANK) is not None
            except (ImportError, ValueError):
                legacy = False
        if legacy:
            raise RuntimeError(
                f"hook requires the honcho_rerank wrapper ({RERANK_PACKAGE}); found the old fork module {LEGACY_RERANK}, "
                "which is no longer supported. Mount the wrapper and remove the fork's rerank patch"
            ) from exc
        return None
    except ImportError as exc:
        raise RuntimeError(f"hook requires {RERANK_PACKAGE}, which failed to import: {type(exc).__name__}") from exc


def check_fork(orchestrator, specialists, working_memory, rerank_module) -> None:
    """Verify the fork contract before changing any callable or settings object."""
    for module in (orchestrator, specialists, working_memory):
        dream_settings = getattr(module, "settings", None)
        dream = getattr(dream_settings, "DREAM", None)
        for attr in (target for target, _check in LIMITS.values()):
            if not hasattr(dream, attr):
                raise RuntimeError(f"hook requires settings.DREAM.{attr}")
    if rerank_module is not None:
        settings = getattr(rerank_module, "settings", None)
        if settings is None:
            raise RuntimeError("hook requires src.honcho_rerank.settings")
        fields = getattr(type(getattr(settings, "RERANK", None)), "model_fields", {})
        for attr, _check in RERANK_FIELDS.values():
            if attr not in fields:
                raise RuntimeError(f"hook requires RerankSettings.{attr}")
    contracts = (
        ("run_dream", True, ("workspace_name", "observer", "observed"), {}),
        ("run_card_refresh_dream", True, ("workspace_name", "observer", "observed"), {}),
        ("process_dream", True, ("payload", "workspace_name"), {"queue_item_id": None}),
        ("get_configuration", False, ("message_configuration", "session", "workspace"), {}),
    )
    for name, asynchronous, names, keywords in contracts:
        function = getattr(orchestrator, name, None)
        if not callable(function) or inspect.iscoroutinefunction(function) != asynchronous:
            raise RuntimeError(f"hook requires orchestrator.{name} ({'async' if asynchronous else 'sync'})")
        try:
            signature = inspect.signature(function)
            if any(parameter not in signature.parameters for parameter in (*names, *keywords)):
                raise ValueError("missing parameter")
            signature.bind(*(None for _ in names), **keywords)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"hook requires orchestrator.{name} parameters: {', '.join((*names, *keywords))}") from exc


def install() -> None:
    try:
        from src.dreamer import orchestrator, specialists, working_memory
        import src.dreamer as dreamer_pkg
    except ImportError as exc:
        policies = Policies()
        policies.last_reload_error = "fork import failed"
        policies.write_status()
        raise RuntimeError("hook requires src.dreamer") from exc
    try:
        rerank_module = import_rerank()
    except RuntimeError as exc:
        policies = Policies()
        policies.last_reload_error = "honcho_rerank wrapper missing"
        policies.write_status()
        try:
            policies.log(f"FATAL {exc}")
        except Exception:
            pass
        raise

    original = getattr(orchestrator, "run_dream", None)
    if getattr(original, "__policies_installed__", False):
        return
    policies = Policies(check_rerank=rerank_module is not None)
    if rerank_module is None:
        policies.log("rerank package not installed; dream hook runs without it")
    try:
        check_fork(orchestrator, specialists, working_memory, rerank_module)
    except RuntimeError:
        policies.last_reload_error = "fork compatibility checks failed"
        policies.write_status()
        raise
    policies.base_dream = orchestrator.settings.DREAM
    for module in (working_memory, specialists, orchestrator):
        module.settings = SettingsProxy(module.settings)

    original_get = orchestrator.get_configuration

    @wraps(original_get)
    def get_configuration(*args, **kwargs):
        resolved = original_get(*args, **kwargs)
        decision = _decision.get()
        if decision is None:
            return resolved
        try:
            apply_instructions(
                resolved,
                decision.backend_name,
                policies.instructions_dir(),
                policies.log,
                decision.observer,
                decision.observed,
            )
        except Exception:
            policies.log("instructions NOT applied")
        return resolved

    orchestrator.get_configuration = get_configuration

    original_process = orchestrator.process_dream

    @wraps(original_process)
    async def process(payload, *args, **kwargs):
        mark_token = _payload_mark.set(getattr(payload, "scheduled_by", None))
        reason_token = _payload_reason.set(getattr(payload, "trigger_reason", None))
        policy = None
        observer, observed = getattr(payload, "observer", None), getattr(payload, "observed", None)
        if isinstance(observer, str) and isinstance(observed, str):
            try:
                policies.reload()
                policy, _source = lookup_block(policies.data, observer, observed)
            except Exception:
                policy = None
        policy_token = _process_policy.set(policy)
        try:
            return await original_process(payload, *args, **kwargs)
        finally:
            _process_policy.reset(policy_token)
            _payload_reason.reset(reason_token)
            _payload_mark.reset(mark_token)

    orchestrator.process_dream = process
    dreamer_pkg.process_dream = process
    # The chained entrypoint may have imported the consumer before this hook.
    consumer = sys.modules.get("src.deriver.consumer")
    if consumer is not None:
        consumer.process_dream = process
    orchestrator.run_dream = _wrap_run(original, policies)

    original_card = getattr(orchestrator, "run_card_refresh_dream", None)
    if original_card is not None and not getattr(original_card, "__policies_installed__", False):
        orchestrator.run_card_refresh_dream = _wrap_run(original_card, policies, card_refresh=True)
    install_rerank(policies)
    policies.mark_installed()


def install_rerank(policies: Policies) -> None:
    """Replace src.honcho_rerank.settings with RerankSettingsProxy when the package is installed.

    The wrapper reads its settings attribute on every call and never caches it, so the
    proxy changes reranking for the dream in progress and for nothing else. A missing
    package leaves the dream hook in place and installs no proxy.
    """
    rerank_module = import_rerank()
    if rerank_module is None:
        return
    current = getattr(rerank_module, "settings", None)
    if current is None:
        raise RuntimeError("hook requires src.honcho_rerank.settings")
    # Keep the settings source, so run logging sees hot reloads of the base RERANK.
    policies.rerank_settings = current._base if isinstance(current, RerankSettingsProxy) else current
    if isinstance(current, RerankSettingsProxy):
        return
    rerank_module.settings = RerankSettingsProxy(current, policies.log)
    policies.log("rerank hook installed")
