"""Deterministic caller-stratified partitions of a sample, so a run can be paid for and judged in pieces.

    python bench/scripts/slices.py --sample sample.jsonl --output slices.json --count 10

Every call lands in exactly one slice; each caller type is dealt round-robin across the slices, so every slice
has roughly the same caller mix. Run one slice, read the report, then decide whether to spend on the next.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchlib import read_jsonl  # noqa: E402


def build_slices(calls: list[dict], seed: int = 20261031, count: int = 10) -> dict:
    ids = [c["call_id"] for c in calls]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate call ids in the sample")
    groups: dict[str, list[str]] = defaultdict(list)
    for call in calls:
        groups[call["caller_type"]].append(call["call_id"])
    rng = random.Random(seed)
    buckets: list[list[str]] = [[] for _ in range(count)]
    mixes = [Counter() for _ in buckets]
    offset = 0
    for caller, group in sorted(groups.items()):
        group.sort()
        rng.shuffle(group)
        for index, call_id in enumerate(group):
            target = (offset + index) % count
            buckets[target].append(call_id)
            mixes[target][caller] += 1
        offset = (offset + len(group)) % count
    return dict(seed=seed, slices=[dict(slice=i + 1, call_ids=sorted(b), caller_mix=dict(sorted(mixes[i].items())))
                                   for i, b in enumerate(buckets)])


def filter_calls(calls: list[dict], numbers: list[int], manifest: dict) -> list[dict]:
    """The calls of the chosen slices. Refuses a manifest that is not an exact partition of ``calls``."""
    entries = manifest["slices"]
    all_ids = [i for e in entries for i in e["call_ids"]]
    if len(all_ids) != len(set(all_ids)) or set(all_ids) != {c["call_id"] for c in calls}:
        raise ValueError("slice manifest is not a partition of the sample; regenerate it")
    if not set(numbers) <= {e["slice"] for e in entries}:
        raise ValueError("unknown slice number")
    chosen = {i for e in entries if e["slice"] in set(numbers) for i in e["call_ids"]}
    return [c for c in calls if c["call_id"] in chosen]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sample", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--seed", type=int, default=20261031)
    args = p.parse_args(argv)
    manifest = build_slices(read_jsonl(args.sample), args.seed, args.count)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for entry in manifest["slices"]:
        print(f"slice {entry['slice']}: size={len(entry['call_ids'])} caller_mix={json.dumps(entry['caller_mix'], sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
