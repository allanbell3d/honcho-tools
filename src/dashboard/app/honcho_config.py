"""Read and write Honcho's config.toml from the dashboard.

The schema is config/honcho-config.schema.json. Values come from the TOML file.
A missing key uses the schema default. Comments and keys the schema does not
know are left in place. tomlkit writes the file so the layout stays.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import tomlkit
import backups
from fastapi import HTTPException, Request

BODY_LIMIT = 1_000_000
REDACTED = "<redacted>"
SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|AIza|Bearer |eyJ)")


def _no_constant(name: str):
    """json.loads parse_constant: NaN and Infinity are not JSON and would reach the files."""
    raise ValueError(f"{name} is not allowed")
DEFAULT_OVERRIDES = (
    "API_WORKERS,DERIVER_WORKERS,DB_POOL_SIZE,DB_MAX_OVERFLOW,"
    "TELEMETRY_ENDPOINT,TELEMETRY_HEADERS"
)
DREAM_HINT = "Dream routing is driven by settings.json, not here."
DREAM_HINT_FIELDS = {"model", "transport", "base_url", "api_key_env", "thinking_effort"}
_schema_cache: dict = {"mtime": None, "schema": None}


def schema_path() -> Path:
    env = os.environ.get("HONCHO_SCHEMA_FILE", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    if len(here.parents) > 2:
        candidate = here.parents[2] / "config" / "honcho-config.schema.json"
        if candidate.is_file():
            return candidate
    return Path("/templates/honcho-config.schema.json")


def config_path() -> Path:
    """Honcho's config.toml, bound as a single file at /honcho/config.toml, outside /config."""
    return Path(os.environ.get("HONCHO_CONFIG_FILE") or "/honcho/config.toml")


def override_names() -> list[str]:
    raw = os.environ.get("HONCHO_ENV_OVERRIDES")
    if raw is None:
        raw = DEFAULT_OVERRIDES
    return [part.strip() for part in raw.split(",") if part.strip()]


def env_locked(env: str, locked: set[str]) -> bool:
    # Parent JSON settings variables also override their nested schema fields.
    return any(env == name or env.startswith(name + "__") for name in locked | set(os.environ))


def load_schema() -> dict:
    path = schema_path()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError as exc:
        raise HTTPException(status_code=500, detail="honcho schema file is missing") from exc
    if _schema_cache["mtime"] == mtime and _schema_cache["schema"] is not None:
        return _schema_cache["schema"]
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="honcho schema file is invalid") from exc
    if not isinstance(schema, dict) or not isinstance(schema.get("sections"), dict):
        raise HTTPException(status_code=500, detail="honcho schema file is invalid")
    _schema_cache["mtime"] = mtime
    _schema_cache["schema"] = schema
    return schema


def file_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def secret_text(value) -> bool:
    return isinstance(value, str) and SECRET.search(value) is not None


def contains_secret(value) -> bool:
    if secret_text(value):
        return True
    if isinstance(value, dict):
        return any(secret_text(key) or contains_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_secret(item) for item in value)
    return False


def plain(value):
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    unwrap = getattr(value, "unwrap", None)
    if callable(unwrap):
        return plain(unwrap())
    return str(value)


def find_key(table, key: str):
    if not isinstance(table, dict):
        return None
    if key in table:
        return key
    lowered = key.lower()
    for existing in table:
        if str(existing).lower() == lowered:
            return existing
    return None


def child(table, key: str):
    found = find_key(table, key)
    if found is None:
        return None, False
    return plain(table[found]), True


def read_document() -> tuple[dict | None, bool, str | None]:
    """Return the parsed document, whether the file is missing, and a parse error."""
    path = config_path()
    if not path.is_file():
        return tomlkit.document(), True, None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="config.toml cannot be read") from exc
    if not text.strip():
        return tomlkit.document(), False, None
    try:
        return tomlkit.parse(text), False, None
    except tomlkit.exceptions.ParseError:
        return None, False, "config.toml cannot be parsed"


def _bounds(spec: dict, value, path: str, errors: list[str]) -> None:
    limits = spec.get("constraints") or {}
    if "gt" in limits and not value > limits["gt"]:
        errors.append(f"{path}: must be > {limits['gt']}")
    if "ge" in limits and not value >= limits["ge"]:
        errors.append(f"{path}: must be >= {limits['ge']}")
    if "lt" in limits and not value < limits["lt"]:
        errors.append(f"{path}: must be < {limits['lt']}")
    if "le" in limits and not value <= limits["le"]:
        errors.append(f"{path}: must be <= {limits['le']}")


def _scrub(value):
    if contains_secret(value):
        return REDACTED, True
    return value, False


def _hint(section: str, path: str, name: str) -> str | None:
    if section != "dream":
        return None
    if name in {"deduction_model_config", "induction_model_config"}:
        return DREAM_HINT
    if name in DREAM_HINT_FIELDS and (
        "deduction_model_config" in path or "induction_model_config" in path
    ):
        return DREAM_HINT
    return None


def _view_fields(schema, fields, file_table, defaults, prefix, env_prefix, locked, section) -> list[dict]:
    rows = []
    file_table = file_table if isinstance(file_table, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    for key, spec in fields.items():
        env = f"{env_prefix}{spec['env_name']}"
        file_value, present = child(file_table, key)
        overlay = defaults[key] if key in defaults else spec.get("default")
        path = f"{prefix}.{key}" if prefix else key
        read_only = env_locked(env, locked)
        value, redacted = _scrub(file_value) if present else (overlay, False)
        if redacted:
            read_only = True
        row = {
            "name": key,
            "path": path,
            "type": spec.get("type"),
            "nullable": bool(spec.get("nullable")),
            "required": bool(spec.get("required")),
            "enum": spec.get("enum"),
            "constraints": spec.get("constraints") or {},
            "description": spec.get("description") or "",
            "default": overlay,
            "read_only": read_only,
            "note": "set in compose" if env in locked else ("set in environment" if env_locked(env, locked) else None),
            "hint": _hint(section, path, key),
            "items": spec.get("items"),
            "value_type": spec.get("value_type"),
            "free": bool(spec.get("free")),
            "redacted": redacted,
            "env": env,
        }
        if spec.get("type") == "object" and spec.get("model"):
            model = schema["models"][spec["model"]]
            if not row["description"]:
                row["description"] = model.get("description") or ""
            child_file = file_value if isinstance(file_value, dict) else {}
            child_default = overlay if isinstance(overlay, dict) else {}
            row["fields"] = _view_fields(
                schema, model["fields"], child_file, child_default, path, env + "__", locked, section
            )
        elif spec.get("type") == "map" and spec.get("model"):
            model = schema["models"][spec["model"]]
            file_map = file_value if isinstance(file_value, dict) else {}
            default_map = overlay if isinstance(overlay, dict) else {}
            entries = []
            for map_key in spec.get("key_enum") or []:
                entry_file, entry_present = child(file_map, map_key)
                if not entry_present or not isinstance(entry_file, dict):
                    entry_file = {}
                entry_default = default_map.get(map_key)
                if not isinstance(entry_default, dict):
                    entry_default = {}
                entries.append({
                    "key": map_key,
                    "path": f"{path}.{map_key}",
                    "fields": _view_fields(
                        schema,
                        model["fields"],
                        entry_file,
                        entry_default,
                        f"{path}.{map_key}",
                        env + "__" + map_key.upper() + "__",
                        locked,
                        section,
                    ),
                })
            row["entries"] = entries
        else:
            if redacted:
                row["value"] = REDACTED
                row["is_default"] = False
            elif not present:
                row["value"] = overlay
                row["is_default"] = True
            else:
                row["value"] = file_value
                row["is_default"] = file_value == overlay
        rows.append(row)
    return rows


def unknown_paths(schema: dict, document) -> list[str]:
    if not isinstance(document, dict):
        return []
    return _unknown(schema, document, schema["sections"], "", True)


def _unknown(schema, table, fields, prefix: str, sections: bool) -> list[str]:
    if not isinstance(table, dict):
        return []
    known = {key.lower(): (key, spec) for key, spec in fields.items()}
    found = []
    for key, value in table.items():
        spec_pair = known.get(str(key).lower())
        path = f"{prefix}.{key}" if prefix else str(key)
        if spec_pair is None:
            found.append(path)
            continue
        spec = spec_pair[1]
        if sections:
            section_fields = spec["fields"]
            if isinstance(value, dict):
                found.extend(_unknown(schema, value, section_fields, path, False))
            continue
        if spec.get("type") == "object" and spec.get("model") and isinstance(value, dict):
            found.extend(_unknown(schema, value, schema["models"][spec["model"]]["fields"], path, False))
        elif spec.get("type") == "map" and spec.get("model") and isinstance(value, dict):
            allowed = {item.lower() for item in (spec.get("key_enum") or [])}
            model_fields = schema["models"][spec["model"]]["fields"]
            for child_key, child_value in value.items():
                child_path = f"{path}.{child_key}"
                if allowed and str(child_key).lower() not in allowed:
                    found.append(child_path)
                    continue
                if isinstance(child_value, dict):
                    found.extend(_unknown(schema, child_value, model_fields, child_path, False))
    return found


def view() -> dict:
    schema = load_schema()
    document, missing, parse_error = read_document()
    locked = set(override_names())
    sections = []
    source = document if isinstance(document, dict) else {}
    for name, section in schema["sections"].items():
        file_section, _present = child(source, name)
        if not isinstance(file_section, dict):
            file_section = {}
        sections.append({
            "name": name,
            "description": section.get("description") or "",
            "fields": _view_fields(
                schema, section["fields"], file_section, {}, name, section.get("env_prefix") or "", locked, name
            ),
        })
    mtime = file_mtime(config_path())
    return {
        "honcho_version": schema.get("honcho_version"),
        "missing": missing,
        "parse_error": parse_error,
        "mtime_ns": None if mtime is None else str(mtime),
        "before_route_all_at": before_route_all_at(config_path()),
        "unknown": [] if document is None else unknown_paths(schema, document),
        "overrides": override_names(),
        "models": [] if document is None else model_resolution(document, schema),
        "model_gaps": [] if document is None else model_gaps(document, schema),
        "sections": sections,
    }


async def read_body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > BODY_LIMIT:
        raise HTTPException(status_code=413, detail="body is too long")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    try:
        payload = json.loads(text, parse_constant=_no_constant) if text else {}
    except (ValueError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return payload


def _parse_mtime(sent):
    if sent is None:
        return None
    if isinstance(sent, bool):
        raise HTTPException(status_code=400, detail="mtime_ns must be an integer or null")
    if isinstance(sent, int) or (isinstance(sent, str) and sent.isdigit()):
        return int(sent)
    raise HTTPException(status_code=400, detail="mtime_ns must be an integer or null")


def check_mtime(path: Path, payload: dict) -> None:
    if "mtime_ns" not in payload:
        return
    if file_mtime(path) != _parse_mtime(payload.get("mtime_ns")):
        raise HTTPException(status_code=409, detail="the file changed; reload and review the diff again")


def _reject(errors: list[str]) -> None:
    safe = []
    for line in errors:
        safe.append("secret-like string" if SECRET.search(line) else line)
    raise HTTPException(status_code=400, detail=safe)


def _validate_fields(schema, fields, proposed, file_table, defaults, prefix, env_prefix, locked, errors) -> None:
    if not isinstance(proposed, dict):
        errors.append(f"{prefix}: must be an object")
        return
    known = {key.lower(): key for key in fields}
    for key in proposed:
        if str(key).lower() not in known:
            errors.append(f"{prefix}.{key}: not in schema")
    file_table = file_table if isinstance(file_table, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    for key, spec in fields.items():
        sent_key = find_key(proposed, key)
        if sent_key is None:
            continue
        value = proposed[sent_key]
        env = f"{env_prefix}{spec['env_name']}"
        file_value, present = child(file_table, key)
        overlay = defaults[key] if key in defaults else spec.get("default")
        path = f"{prefix}.{key}"
        redacted = present and contains_secret(file_value)
        if env_locked(env, locked) or redacted:
            if value == REDACTED or value == (file_value if present else overlay):
                continue
            if redacted and value == REDACTED:
                continue
            errors.append(f"{path}: set in compose" if env in locked else (f"{path}: set in environment" if env_locked(env, locked) else f"{path}: read-only"))
            continue
        _validate_value(schema, spec, value, file_value if isinstance(file_value, dict) else {}, overlay, path, env, locked, errors)


def _validate_value(schema, spec, value, file_value, overlay, path, env, locked, errors) -> None:
    if value is None:
        if spec.get("nullable") or not spec.get("required"):
            return
        errors.append(f"{path}: required")
        return
    if value == REDACTED:
        errors.append(f"{path}: read-only")
        return
    if contains_secret(value):
        errors.append(f"{path}: secret-like string")
        return
    kind = spec.get("type")
    if kind == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{path}: must be a boolean")
        return
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{path}: must be an integer")
            return
        _bounds(spec, value, path, errors)
        return
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{path}: must be a number")
            return
        _bounds(spec, value, path, errors)
        return
    if kind == "string":
        if not isinstance(value, str):
            errors.append(f"{path}: must be a string")
            return
        choices = spec.get("enum")
        if choices and value not in choices:
            errors.append(f"{path}: must be one of {', '.join(str(item) for item in choices)}")
        return
    if kind == "array":
        if not isinstance(value, list):
            errors.append(f"{path}: must be a list")
            return
        item = spec.get("items")
        choices = spec.get("enum")
        for index, elt in enumerate(value):
            if item == "string" and not isinstance(elt, str):
                errors.append(f"{path}.{index}: must be a string")
            elif item == "string" and choices and elt not in choices:
                # A Literal list in Honcho (rerank.callers): a stray value stops Honcho from starting.
                errors.append(f"{path}.{index}: must be one of {', '.join(str(c) for c in choices)}")
            elif item == "integer" and (isinstance(elt, bool) or not isinstance(elt, int)):
                errors.append(f"{path}.{index}: must be an integer")
            elif item == "number" and (isinstance(elt, bool) or not isinstance(elt, (int, float))):
                errors.append(f"{path}.{index}: must be a number")
        return
    if kind == "map" and spec.get("value_type"):
        if not isinstance(value, dict):
            errors.append(f"{path}: must be an object")
            return
        for item_key, item_value in value.items():
            if not isinstance(item_key, str) or not isinstance(item_value, str):
                errors.append(f"{path}: keys and values must be strings")
                return
        return
    if kind == "object" and spec.get("free"):
        if not isinstance(value, dict):
            errors.append(f"{path}: must be an object")
        return
    if kind == "object" and spec.get("model"):
        if not isinstance(value, dict):
            errors.append(f"{path}: must be an object")
            return
        model = schema["models"][spec["model"]]
        child_default = overlay if isinstance(overlay, dict) else {}
        _validate_fields(
            schema, model["fields"], value, file_value, child_default, path, env + "__", locked, errors
        )
        return
    if kind == "map" and spec.get("model"):
        if not isinstance(value, dict):
            errors.append(f"{path}: must be an object")
            return
        allowed = spec.get("key_enum") or []
        allowed_lower = {item.lower(): item for item in allowed}
        model = schema["models"][spec["model"]]
        default_map = overlay if isinstance(overlay, dict) else {}
        file_map = file_value if isinstance(file_value, dict) else {}
        for item_key, entry in value.items():
            canonical = allowed_lower.get(str(item_key).lower())
            if allowed and canonical is None:
                errors.append(f"{path}.{item_key}: not in schema")
                continue
            if not isinstance(entry, dict):
                errors.append(f"{path}.{item_key}: must be an object")
                continue
            entry_file, _present = child(file_map, canonical or item_key)
            if not isinstance(entry_file, dict):
                entry_file = {}
            entry_default = default_map.get(canonical or item_key)
            if not isinstance(entry_default, dict):
                entry_default = {}
            _validate_fields(
                schema,
                model["fields"],
                entry,
                entry_file,
                entry_default,
                f"{path}.{canonical or item_key}",
                env + "__" + str(canonical or item_key).upper() + "__",
                locked,
                errors,
            )
        return
    if kind == "any":
        return
    errors.append(f"{path}: unsupported value")


def _validate_proposal(schema, proposed, document, locked) -> list[str]:
    errors = []
    if not isinstance(proposed, dict):
        return ["values must be an object"]
    known = {name.lower(): name for name in schema["sections"]}
    for key in proposed:
        if str(key).lower() not in known:
            errors.append(f"{key}: not in schema")
    source = document if isinstance(document, dict) else {}
    for name, section in schema["sections"].items():
        sent = find_key(proposed, name)
        if sent is None:
            continue
        file_section, _present = child(source, name)
        if not isinstance(file_section, dict):
            file_section = {}
        _validate_fields(
            schema,
            section["fields"],
            proposed[sent],
            file_section,
            {},
            name,
            section.get("env_prefix") or "",
            locked,
            errors,
        )
    return errors


def _public(value):
    if contains_secret(value):
        return REDACTED
    return value


def _diff_fields(schema, fields, proposed, file_table, defaults, prefix, env_prefix, locked) -> list[dict]:
    rows = []
    if not isinstance(proposed, dict):
        return rows
    file_table = file_table if isinstance(file_table, dict) else {}
    defaults = defaults if isinstance(defaults, dict) else {}
    for key, spec in fields.items():
        sent_key = find_key(proposed, key)
        if sent_key is None:
            continue
        value = proposed[sent_key]
        env = f"{env_prefix}{spec['env_name']}"
        file_value, present = child(file_table, key)
        overlay = defaults[key] if key in defaults else spec.get("default")
        path = f"{prefix}/{key}"
        if env_locked(env, locked) or (present and contains_secret(file_value)) or value == REDACTED:
            continue
        kind = spec.get("type")
        if kind == "object" and spec.get("model") and isinstance(value, dict):
            model = schema["models"][spec["model"]]
            child_file = file_value if isinstance(file_value, dict) else {}
            child_default = overlay if isinstance(overlay, dict) else {}
            rows.extend(_diff_fields(
                schema, model["fields"], value, child_file, child_default, path, env + "__", locked
            ))
            continue
        if kind == "map" and spec.get("model") and isinstance(value, dict):
            model = schema["models"][spec["model"]]
            file_map = file_value if isinstance(file_value, dict) else {}
            default_map = overlay if isinstance(overlay, dict) else {}
            allowed = {item.lower(): item for item in (spec.get("key_enum") or [])}
            for item_key, entry in value.items():
                canonical = allowed.get(str(item_key).lower(), item_key)
                if not isinstance(entry, dict):
                    continue
                entry_file, _present = child(file_map, canonical)
                if not isinstance(entry_file, dict):
                    entry_file = {}
                entry_default = default_map.get(canonical)
                if not isinstance(entry_default, dict):
                    entry_default = {}
                rows.extend(_diff_fields(
                    schema,
                    model["fields"],
                    entry,
                    entry_file,
                    entry_default,
                    f"{path}/{canonical}",
                    env + "__" + str(canonical).upper() + "__",
                    locked,
                ))
            continue
        current = file_value if present else overlay
        # An absent model or transport is a change even when the value is Honcho's default (see _apply_fields).
        if value != current or (not present and key.lower() in REQUIRED_MODEL_KEYS):
            rows.append({
                "path": path,
                "op": "add" if not present else "replace",
                "before": _public(current),
                "after": _public(value),
            })
    return rows


def _diff(schema, proposed, document, locked) -> list[dict]:
    rows = []
    source = document if isinstance(document, dict) else {}
    for name, section in schema["sections"].items():
        sent = find_key(proposed, name)
        if sent is None:
            continue
        file_section, _present = child(source, name)
        if not isinstance(file_section, dict):
            file_section = {}
        rows.extend(_diff_fields(
            schema,
            section["fields"],
            proposed[sent],
            file_section,
            {},
            name,
            section.get("env_prefix") or "",
            locked,
        ))
    return rows


def _set_value(container, key: str, value) -> None:
    existing = find_key(container, key)
    use = existing if existing is not None else key
    old = None
    if existing is not None:
        try:
            old = container.item(existing)
        except AttributeError:
            old = None
    container[use] = value
    if old is None or not getattr(old, "trivia", None):
        return
    try:
        new = container.item(use)
    except AttributeError:
        return
    if not getattr(new, "trivia", None):
        return
    new.trivia.indent = old.trivia.indent
    new.trivia.comment_ws = old.trivia.comment_ws
    new.trivia.comment = old.trivia.comment
    new.trivia.trail = old.trivia.trail


def _ensure_table(container, key: str):
    existing = find_key(container, key)
    if existing is None:
        container[key] = tomlkit.table()
        return container[key], True
    current = container[existing]
    if isinstance(current, dict):
        return current, False
    container[existing] = tomlkit.table()
    return container[existing], False


REQUIRED_MODEL_KEYS = frozenset({"model", "transport"})


def _apply_fields(schema, container, fields, proposed, defaults, env_prefix, locked) -> bool:
    if not isinstance(proposed, dict):
        return False
    defaults = defaults if isinstance(defaults, dict) else {}
    wrote = False
    for key, spec in fields.items():
        sent_key = find_key(proposed, key)
        if sent_key is None:
            continue
        value = proposed[sent_key]
        env = f"{env_prefix}{spec['env_name']}"
        file_value, present = child(container, key)
        if env_locked(env, locked) or value == REDACTED or (present and contains_secret(file_value)):
            continue
        overlay = defaults[key] if key in defaults else spec.get("default")
        kind = spec.get("type")
        if kind == "object" and spec.get("model") and isinstance(value, dict):
            model = schema["models"][spec["model"]]
            child_default = overlay if isinstance(overlay, dict) else {}
            table, created = _ensure_table(container, key)
            child_wrote = _apply_fields(
                schema, table, model["fields"], value, child_default, env + "__", locked
            )
            if created and not child_wrote:
                existing = find_key(container, key)
                if existing is not None:
                    del container[existing]
            wrote = wrote or child_wrote
            continue
        if kind == "map" and spec.get("model") and isinstance(value, dict):
            model = schema["models"][spec["model"]]
            default_map = overlay if isinstance(overlay, dict) else {}
            allowed = {item.lower(): item for item in (spec.get("key_enum") or [])}
            table, created = _ensure_table(container, key)
            child_wrote = False
            for item_key, entry in value.items():
                canonical = allowed.get(str(item_key).lower(), str(item_key))
                if not isinstance(entry, dict):
                    continue
                entry_default = default_map.get(canonical)
                if not isinstance(entry_default, dict):
                    entry_default = {}
                entry_table, entry_created = _ensure_table(table, canonical)
                entry_wrote = _apply_fields(
                    schema,
                    entry_table,
                    model["fields"],
                    entry,
                    entry_default,
                    env + "__" + canonical.upper() + "__",
                    locked,
                )
                if entry_created and not entry_wrote:
                    existing = find_key(table, canonical)
                    if existing is not None:
                        del table[existing]
                child_wrote = child_wrote or entry_wrote
            if created and not child_wrote:
                existing = find_key(container, key)
                if existing is not None:
                    del container[existing]
            wrote = wrote or child_wrote
            continue
        if isinstance(value, dict) and (spec.get("free") or spec.get("value_type")):
            if not present and value == (overlay or {}):
                continue
            if present and value == file_value:
                continue
            _set_value(container, key, value)
            wrote = True
            continue
        if value is None:
            existing = find_key(container, key)
            if existing is not None:
                del container[existing]
                wrote = True
            continue
        # model and transport are written even when they equal Honcho's default: Apply needs both
        # in the file (model_gaps), so skipping them would leave Apply refused for the default choice.
        if not present and value == overlay and key.lower() not in REQUIRED_MODEL_KEYS:
            continue
        if present and value == file_value:
            continue
        _set_value(container, key, value)
        wrote = True
    return wrote


def _apply(schema, document, proposed, locked) -> None:
    for name, section in schema["sections"].items():
        sent = find_key(proposed, name)
        if sent is None:
            continue
        table, created = _ensure_table(document, name)
        wrote = _apply_fields(
            schema,
            table,
            section["fields"],
            proposed[sent],
            {},
            section.get("env_prefix") or "",
            locked,
        )
        if created and not wrote:
            existing = find_key(document, name)
            if existing is not None:
                del document[existing]


def _blank(value) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return True
    return isinstance(value, dict) and all(_blank(item) for item in value.values())


def _untouched(schema, fields, value) -> bool:
    """Every field empty or at its default, as the page renders a sub-model that the file does not set."""
    if not isinstance(value, dict):
        return _blank(value)
    for key, item in value.items():
        spec = fields.get(key)
        if spec is None:
            return False
        if _blank(item) or item == spec.get("default"):
            continue
        if spec.get("type") == "object" and spec.get("model") and _untouched(schema, schema["models"][spec["model"]]["fields"], item):
            continue
        return False
    return True


def _drop_blank_nullable(schema, fields, proposed, file_table) -> None:
    """Leave out a nullable sub-model that the file does not set and the page sent with every field empty.

    The page renders such a block (for example a model's fallback) with empty controls; sent back, its required
    fields would fail validation although nothing was edited.
    """
    if not isinstance(proposed, dict):
        return
    file_table = file_table if isinstance(file_table, dict) else {}
    for key, spec in fields.items():
        sent_key = find_key(proposed, key)
        if sent_key is None:
            continue
        value = proposed[sent_key]
        file_value, present = child(file_table, key)
        kind = spec.get("type")
        if kind == "object" and spec.get("model") and isinstance(value, dict):
            model_fields = schema["models"][spec["model"]]["fields"]
            if spec.get("nullable") and (not present or file_value is None) and _untouched(schema, model_fields, value):
                del proposed[sent_key]
                continue
            _drop_blank_nullable(schema, model_fields, value, file_value)
        elif kind == "map" and spec.get("model") and isinstance(value, dict):
            file_map = file_value if isinstance(file_value, dict) else {}
            for item_key, entry in value.items():
                entry_file, _present = child(file_map, str(item_key))
                _drop_blank_nullable(schema, schema["models"][spec["model"]]["fields"], entry, entry_file)
        elif kind == "boolean" and value is False and not present and spec.get("default") is None:
            # A checkbox cannot show "unset"; unticked on a field the file does not set means unchanged.
            del proposed[sent_key]


def _prepare(payload: dict):
    schema = load_schema()
    document, missing, parse_error = read_document()
    if parse_error:
        raise HTTPException(status_code=400, detail=parse_error)
    values = payload.get("values")
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="values must be an object")
    locked = set(override_names())
    for name, section in schema["sections"].items():
        sent = find_key(values, name)
        if sent is not None:
            file_section, _present = child(document or {}, name)
            _drop_blank_nullable(schema, section["fields"], values[sent], file_section)
    errors = _validate_proposal(schema, values, document, locked)
    if errors:
        _reject(errors)
    check_mtime(config_path(), payload)
    rows = _diff(schema, values, document, locked)
    return schema, document, values, locked, rows, missing


def model_blocks(schema: dict) -> list[dict]:
    """Every model_config block Honcho resolves: deriver, summary, dialectic levels, dream passes, embedding.

    A fallback block is nullable and is not listed.
    """
    blocks = []
    for section_name, section in schema["sections"].items():
        prefix = section.get("env_prefix") or ""
        for key, spec in section["fields"].items():
            if spec.get("type") == "object" and spec.get("model") and key.endswith("model_config"):
                task = section_name if key == "model_config" else f"{section_name} {key[: -len('_model_config')]}"
                blocks.append({
                    "task": task,
                    "keys": [section_name, key],
                    "env": f"{prefix}{spec['env_name']}__",
                    "default": spec.get("default"),
                })
            elif spec.get("type") == "map" and spec.get("model"):
                inner = schema["models"][spec["model"]]["fields"].get("model_config")
                if not inner or inner.get("type") != "object":
                    continue
                default_map = spec.get("default") if isinstance(spec.get("default"), dict) else {}
                for entry in spec.get("key_enum") or []:
                    entry_default = default_map.get(entry) if isinstance(default_map.get(entry), dict) else {}
                    blocks.append({
                        "task": f"{section_name} {entry}",
                        "keys": [section_name, key, entry, "model_config"],
                        "env": f"{prefix}{spec['env_name']}__{entry.upper()}__{inner['env_name']}__",
                        "default": entry_default.get("model_config") or inner.get("default"),
                    })
    return blocks


def model_resolution(document=None, schema: dict | None = None) -> list[dict]:
    """What each task's model and transport resolve to, and which of the two config.toml leaves unset.

    An unset model or transport makes Honcho fall back to its built-in default (an OpenAI model on the OpenAI key).
    A field set in compose (HONCHO_ENV_OVERRIDES) counts as set.
    """
    schema = schema or load_schema()
    if document is None:
        document, _missing, parse_error = read_document()
        if parse_error:
            document = None
    locked = set(override_names())
    rows = []
    for block in model_blocks(schema):
        table = document if isinstance(document, dict) else {}
        for key in block["keys"]:
            table, _present = child(table, key)
            if not isinstance(table, dict):
                table = {}
                break
        default = block["default"] if isinstance(block["default"], dict) else {}
        row = {"task": block["task"], "path": ".".join(block["keys"]), "missing": []}
        for field in ("model", "transport"):
            value, present = child(table, field)
            env = block["env"] + field.upper()
            if present and isinstance(value, str) and value.strip():
                row[field] = value
                row[field + "_source"] = "file"
            elif env_locked(env, locked):
                row[field] = None
                row[field + "_source"] = "compose"
            else:
                row[field] = default.get(field)
                row[field + "_source"] = "default"
                row["missing"].append(field)
        mode, present = child(table, "structured_output_mode")
        if present and mode is not None:
            row["structured_output_mode"] = mode
        rows.append(row)
    return rows


def model_gaps(document=None, schema: dict | None = None) -> list[str]:
    """One line per model_config block Honcho would not start with as intended.

    A block without a model or a transport falls back to Honcho's default. structured_output_mode on any transport but
    openai stops Honcho 3.5.0 at start-up (a validation error in its settings).
    """
    lines = []
    for row in model_resolution(document, schema):
        if row["missing"]:
            lines.append(
                f"{row['path']}: no {' or '.join(row['missing'])} in config.toml; "
                f"Honcho falls back to {row.get('model') or 'its default'} ({row.get('transport') or 'default transport'})"
            )
        if row.get("structured_output_mode") is not None and row.get("transport") not in (None, "openai"):
            lines.append(
                f"{row['path']}: structured_output_mode is set but the transport is {row['transport']}; "
                "Honcho only accepts it on the openai transport and would not start"
            )
    return lines


def _proposed_document(schema, document, values, locked):
    """The document a write would produce, without touching the file."""
    copy = tomlkit.parse(tomlkit.dumps(document)) if document is not None else tomlkit.document()
    _apply(schema, copy, json.loads(json.dumps(values)), locked)
    return copy


def review(payload: dict) -> dict:
    schema, document, values, locked, rows, missing = _prepare(payload)
    mtime = file_mtime(config_path())
    return {
        "ok": True,
        "changed": bool(rows) or (missing and bool(rows)),
        "creating": missing and bool(rows),
        "diff": rows,
        "unknown": unknown_paths(load_schema(), document),
        "model_gaps": model_gaps(_proposed_document(schema, document, values, locked), schema),
        "mtime_ns": None if mtime is None else str(mtime),
    }


def backup_dir(path: Path) -> Path:
    """Where config.toml.bak_* copies go: HONCHO_CONFIG_BAK_DIR, else backups/config-toml/ in CONFIG_DIR."""
    env = os.environ.get("HONCHO_CONFIG_BAK_DIR", "").strip()
    if env:
        return Path(env)
    return Path(os.environ.get("CONTROL_FILE") or "/config/settings.json").parent / "backups" / "config-toml"


def atomic_write(path: Path, text: str) -> str | None:
    """Back up, then write config.toml in place, then read it back.

    In place, not temp-file-and-rename: Honcho's containers hold config.toml as a
    single-file bind mount, which a rename would leave on the old inode. The backup taken first is the safety net. Copies are
    config.toml.bak_<YYYYMMDD_HHMMSS>: the newest 10 plus any younger than 7 days.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    bak_name = backups.backup_file(path, backup_dir(path))
    if not text.endswith("\n"):
        text += "\n"
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_text(encoding="utf-8") != text:
            raise HTTPException(status_code=500, detail="config.toml does not match after the write; the backup is " + str(bak_name))
    except Exception:
        # The file was opened for writing, so it may be partial: put the backup taken above back, in place, the same way.
        if bak_name is not None:
            try:
                saved = (backup_dir(path) / bak_name).read_text(encoding="utf-8")
                with open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(saved)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass
        raise
    return bak_name


def write_config(payload: dict) -> dict:
    schema, document, values, locked, rows, missing = _prepare(payload)
    path = config_path()
    result = {
        "ok": True,
        "changed": bool(rows),
        "creating": missing and bool(rows),
        "diff": rows,
        "unknown": unknown_paths(schema, document),
        "bak": False,
        "mtime_ns": None if file_mtime(path) is None else str(file_mtime(path)),
    }
    if not rows:
        return result
    if document is None:
        document = tomlkit.document()
    if payload.get("before_route_all") is True and path.is_file():
        # The copy the two undo choices read: the file as it was just before the first Route all was written. A
        # later Route all write keeps that first copy; an undo deletes it.
        before = before_route_all_path(path)
        if not before.exists():
            before.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, before)
    _apply(schema, document, values, locked)
    text = tomlkit.dumps(document)
    bak_name = atomic_write(path, text)
    written = file_mtime(path)
    result["bak"] = bak_name is not None
    result["mtime_ns"] = None if written is None else str(written)
    return result


BEFORE_ROUTE_ALL = "config.toml.before-route-all"
# The config.toml tables of the jobs the Models page lists (embedding is not one of them).
JOB_TABLES = (
    ("deriver", "model_config"),
    ("summary", "model_config"),
    *(("dialectic", "levels", level, "model_config") for level in ("minimal", "low", "medium", "high", "max")),
    ("dream", "deduction_model_config"),
    ("dream", "induction_model_config"),
)


def before_route_all_path(path: Path) -> Path:
    return backup_dir(path) / BEFORE_ROUTE_ALL


def before_route_all_at(path: Path) -> str | None:
    """When the copy from before Route all was taken (UTC), or None without one."""
    try:
        stat = before_route_all_path(path).stat()
    except OSError:
        return None
    return datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()


def _lookup(table, keys):
    """The tomlkit item at keys, comments included, or None."""
    for key in keys:
        found = find_key(table, key)
        if found is None:
            return None
        table = table[found]
    return table


def undo_routing(payload: dict) -> dict:
    """Put config.toml back from the copy taken before Route all.

    scope "jobs": every job table is replaced by the same table from the copy (absent there: removed); the rest of the
    current file stays. scope "file": the whole file comes back from the copy. Both write through atomic_write.
    """
    scope = payload.get("scope")
    if scope not in ("jobs", "file"):
        raise HTTPException(status_code=400, detail="scope must be jobs or file")
    path = config_path()
    before = before_route_all_path(path)
    if not before.is_file():
        raise HTTPException(status_code=409, detail="No copy from before routing")
    check_mtime(path, payload)
    copied = before.read_text(encoding="utf-8-sig")
    if scope == "file":
        text = copied
    else:
        document, _missing, parse_error = read_document()
        if parse_error:
            raise HTTPException(status_code=400, detail=parse_error)
        try:
            source = tomlkit.parse(copied)
        except tomlkit.exceptions.ParseError:
            raise HTTPException(status_code=400, detail="the copy from before routing cannot be parsed") from None
        for keys in JOB_TABLES:
            table = _lookup(source, keys)
            if table is None:
                parent = _lookup(document, keys[:-1])
                existing = find_key(parent, keys[-1]) if isinstance(parent, dict) else None
                if existing is not None:
                    del parent[existing]
                continue
            container = document
            for key in keys[:-1]:
                container, _created = _ensure_table(container, key)
            existing = find_key(container, keys[-1])
            container[existing if existing is not None else keys[-1]] = copy.deepcopy(table)
        text = tomlkit.dumps(document)
    bak_name = atomic_write(path, text)
    before.unlink()
    written = file_mtime(path)
    return {"ok": True, "scope": scope, "bak": bak_name is not None, "mtime_ns": None if written is None else str(written)}
