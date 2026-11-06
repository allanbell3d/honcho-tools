"""Check settings.json against src/config/settings.schema.json.

Usage:
  python src/config/validate.py <settings.json>
  python src/config/validate.py <settings.json> <peer-policies.json> <scheduler.json>

The second form prints every mapped value that differs from the two source files.
A string that looks like a key or token is rejected and not printed.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "settings.schema.json"
SECRET = re.compile(r"(?:^|[^A-Za-z0-9])(?:sk-|AIza|Bearer |eyJ)")
POLICY_MAP = {
    "max_conclusions": "brief",
    "reasoning_effort": "deduction_effort",
    "deduction_rounds": "deduction_rounds",
    "induction_rounds": "induction_rounds",
}
# Fields that have no counterpart in the two source files: per-dream limits and the rerank block.
PER_DREAM_FIELDS = {"induction_effort", "brief_tokens", "tool_calls", "notes", "notes_tokens", "budget", "write_window", "rerank"}
ADDED_PEER_FIELDS = {"enabled", "backend"} | PER_DREAM_FIELDS
RERANK_FORBIDDEN = {"api_key", "api-key", "apikey", "key"}


def redact(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if SECRET.search(text):
        return "<redacted>"
    return text


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"missing file: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"invalid json in {path}: {exc}")
    except OSError as exc:
        sys.exit(f"cannot read {path}: {exc.strerror}")


def schema_errors(instance) -> list[str]:
    schema = load_json(SCHEMA_PATH)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [f"schema file is invalid: {exc.message}"]
    validator = Draft202012Validator(schema)
    lines = []
    for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.path)):
        where = "/" + "/".join(str(part) for part in error.path)
        lines.append(f"schema {where or '/'}: {redact(error.message)}")
    return lines


def active_name(active: str) -> str:
    return active.split(":", 1)[0]


def provider_errors(settings) -> list[str]:
    lines = []
    providers = settings.get("providers")
    if not isinstance(providers, dict):
        return lines
    backends = providers.get("backends")
    active = providers.get("active")
    if not isinstance(backends, dict) or not isinstance(active, str):
        return lines
    name = active_name(active)
    backend = backends.get(name)
    if not isinstance(backend, dict):
        lines.append(f"DIFF providers.active: {redact(name)} is not a backend")
        return lines
    if backend.get("enabled") is False:
        lines.append(f"DIFF providers.active: {redact(name)} is disabled")
    for name, block in backends.items():
        models = block.get("models") if isinstance(block, dict) else None
        if models is not None and block.get("model") not in models:
            lines.append(f"DIFF providers.backends.{name}.model: not in models")
    lines.extend(ref_errors("providers.active", active, backends))
    for prefix, block in ref_blocks(settings):
        lines.extend(ref_errors(f"{prefix}.backend", block.get("backend"), backends))
    # A stage route to an unknown or disabled backend passed here and failed every call of that stage.
    stages = providers.get("stages")
    if isinstance(stages, dict):
        for stage, ref in stages.items():
            model_ref = ref.get("model") if isinstance(ref, dict) else ref
            lines.extend(ref_errors(f"providers.stages.{stage}", model_ref, backends))
            if isinstance(model_ref, str):
                target = backends.get(model_ref.partition(":")[0])
                if isinstance(target, dict) and target.get("enabled") is False:
                    lines.append(f"DIFF providers.stages.{stage}: {redact(model_ref.partition(':')[0])} is disabled")
    return lines


def rerank_errors(settings) -> list[str]:
    """A rerank block never holds a key: the schema rejects unknown keys, this names the reason."""
    lines = []
    blocks = list(ref_blocks(settings))
    if isinstance(settings, dict):
        blocks.append(("global", settings))
    for prefix, block in blocks:
        rerank = block.get("rerank")
        if not isinstance(rerank, dict):
            continue
        for key in rerank:
            if str(key).lower() in RERANK_FORBIDDEN:
                lines.append(f"DIFF {prefix}.rerank.{key}: a rerank block never holds a key; set RERANK_API_KEY in the environment of the API and deriver containers")
    return lines


def ref_blocks(settings):
    """Every block that may carry a backend reference: defaults, peers, pairs."""
    defaults = settings.get("defaults")
    if isinstance(defaults, dict):
        yield "defaults", defaults
    for section in ("peers", "pairs"):
        items = settings.get(section)
        if isinstance(items, dict):
            for key, block in items.items():
                if isinstance(block, dict):
                    yield f"{section}.{key}", block


def ref_errors(where: str, ref, backends: dict) -> list[str]:
    """A reference must name a defined backend; an explicit models list limits overrides."""
    if ref is None or not isinstance(ref, str):
        return []
    name, _, model = ref.partition(":")
    backend = backends.get(name)
    if not isinstance(backend, dict):
        return [f"DIFF {where}: {redact(name)} is not a backend"]
    if model:
        models = backend.get("models")
        if isinstance(models, list) and model not in models:
            return [f"DIFF {where}: {redact(model)} is not in backends.{name}.models"]
    return []


def block_diffs(prefix: str, source: dict, settings_block, allowed_extra: set[str]) -> list[str]:
    lines = []
    got = settings_block if isinstance(settings_block, dict) else None
    if got is None:
        lines.append(f"DIFF {prefix}: missing")
        got = {}
    for old, new in POLICY_MAP.items():
        if old not in source:
            continue
        if got.get(new) != source[old]:
            lines.append(
                f"DIFF {prefix}.{new}: settings={redact(got.get(new))} source={redact(source[old])}"
            )
    for old in source:
        if old not in POLICY_MAP:
            lines.append(f"DIFF {prefix}: source field {old} has no mapping")
    for key, value in got.items():
        old = next((name for name, renamed in POLICY_MAP.items() if renamed == key), None)
        if old is None:
            if key not in allowed_extra:
                lines.append(f"DIFF {prefix}.{key}: settings has {redact(value)} and the source does not")
            continue
        if old not in source:
            lines.append(f"DIFF {prefix}.{key}: settings has {redact(value)} and the source does not")
    return lines


def pair_key(item) -> str:
    if isinstance(item, list) and len(item) == 2:
        return f"{item[0]}/{item[1]}"
    return str(item)


def source_diffs(settings, policies, scheduler) -> tuple[list[str], str]:
    lines = []
    defaults = policies.get("default")
    if not isinstance(defaults, dict):
        lines.append("DIFF peer-policies default: missing")
        defaults = {}
    lines.extend(block_diffs("defaults", defaults, settings.get("defaults"), {"backend"} | PER_DREAM_FIELDS))

    policy_peers = policies.get("peers") if isinstance(policies.get("peers"), dict) else {}
    policy_pairs = policies.get("pairs") if isinstance(policies.get("pairs"), dict) else {}
    sched_pairs = scheduler.get("pairs") if isinstance(scheduler.get("pairs"), list) else []
    settings_peers = settings.get("peers") if isinstance(settings.get("peers"), dict) else {}
    settings_pairs = settings.get("pairs") if isinstance(settings.get("pairs"), dict) else {}

    peer_names = []
    for name in policy_peers:
        if name not in peer_names:
            peer_names.append(name)
    for item in sched_pairs:
        if isinstance(item, list) and len(item) == 2:
            for name in item:
                if name not in peer_names:
                    peer_names.append(name)
    for key in policy_pairs:
        if isinstance(key, str) and "/" in key:
            for name in key.split("/", 1):
                if name not in peer_names:
                    peer_names.append(name)

    for name in peer_names:
        block = settings_peers.get(name)
        if not isinstance(block, dict):
            lines.append(f"DIFF peers.{name}: missing")
            continue
        if block.get("enabled") is not True:
            lines.append(f"DIFF peers.{name}.enabled: settings={redact(block.get('enabled'))} source=true")
        if name in policy_peers and isinstance(policy_peers[name], dict):
            lines.extend(block_diffs(f"peers.{name}", policy_peers[name], block, ADDED_PEER_FIELDS))
        else:
            extra = set(block) - {"enabled"}
            if "backend" in extra and block.get("backend") is None:
                extra.remove("backend")
            for key in sorted(extra):
                lines.append(f"DIFF peers.{name}.{key}: settings has {redact(block.get(key))} and the source does not")

    for name in settings_peers:
        if name not in peer_names:
            lines.append(f"DIFF peers.{name}: not in either source file")

    expected_pairs = {}
    for item in sched_pairs:
        key = pair_key(item)
        if key in policy_pairs and isinstance(policy_pairs[key], dict):
            expected_pairs[key] = policy_pairs[key]
        else:
            expected_pairs[key] = {}
    for key, block in policy_pairs.items():
        if key not in expected_pairs and isinstance(block, dict):
            expected_pairs[key] = block

    for key, source_block in expected_pairs.items():
        got = settings_pairs.get(key)
        if source_block == {}:
            if got != {}:
                lines.append(f"DIFF pairs.{key}: settings={redact(got)} source={{}}")
            continue
        lines.extend(block_diffs(f"pairs.{key}", source_block, got, set(PER_DREAM_FIELDS)))
    for key in settings_pairs:
        if key not in expected_pairs:
            lines.append(f"DIFF pairs.{key}: not in either source file")

    sched = settings.get("scheduler") if isinstance(settings.get("scheduler"), dict) else {}
    for key in ("poll_seconds", "max_parallel", "dream_type"):
        if sched.get(key) != scheduler.get(key):
            lines.append(
                f"DIFF scheduler.{key}: settings={redact(sched.get(key))} source={redact(scheduler.get(key))}"
            )
    source_skip = [pair_key(item) for item in scheduler.get("skip", [])]
    if sched.get("skip") != source_skip:
        lines.append(
            f"DIFF scheduler.skip: settings={redact(sched.get('skip'))} source={redact(source_skip)}"
        )

    summary = (
        f"compared: {len(POLICY_MAP)} default fields, {len(peer_names)} peers, "
        f"{len(expected_pairs)} pairs, 4 scheduler fields"
    )
    return lines, summary


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 4):
        sys.exit(
            "usage: python src/config/validate.py <settings.json> "
            "[<peer-policies.json> <scheduler.json>]"
        )
    settings = load_json(Path(argv[1]))
    problems = schema_errors(settings)
    if not problems:
        problems.extend(provider_errors(settings))
    if isinstance(settings, dict):
        problems.extend(rerank_errors(settings))
    compared = None
    if len(argv) == 4 and isinstance(settings, dict):
        diffs, compared = source_diffs(settings, load_json(Path(argv[2])), load_json(Path(argv[3])))
        problems.extend(diffs)
    if any(line.startswith("schema ") for line in problems):
        print(f"schema: {sum(line.startswith('schema ') for line in problems)} errors")
    else:
        print("schema: ok")
    if compared:
        print(compared)
    else:
        print("source comparison skipped")
    diffs = [line for line in problems if line.startswith("DIFF ")]
    schema_lines = [line for line in problems if line.startswith("schema ")]
    for line in schema_lines + diffs:
        print(line)
    print(f"differences: {len(diffs)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
