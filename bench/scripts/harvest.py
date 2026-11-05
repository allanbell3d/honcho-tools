"""Harvest real conclusion searches from Honcho's telemetry files, offline, and draw a stratified sample.

Reads ``events-*.jsonl`` files (one line per event: ``{"received_at": ..., "event": {"type", "time", "data"}}``) from a
folder you name. Nothing is sent anywhere. Event names are those of Honcho 3.5.0's telemetry.

What becomes a call
* ``llm.call.traced`` events carry the model's tool calls; every ``search_memory`` input (query, top_k) is joined by
  ``run_id`` and tool-call id to its ``agent.tool.call.completed`` event, which gives the caller type (dream
  deduction or induction, dialectic) and the original result count. Failed tool calls are dropped.
* Dialectic prefetch (the automatic search on the question text) is rebuilt from the user message of the traced model
  call: two searches, one on explicit conclusions and one on deductive, inductive and contradiction levels, with
  ``top_k`` 10 at reasoning level minimal and 25 otherwise.
* The session scope of a dialectic or prefetch search is not recorded, so those calls are replayed peer-wide and flagged
  ``scope_approximated``. They are descriptive only in every analysis. Dream searches carry no session filter by
  construction.
* Session-context searches record no query text and are left out; the omission is counted.
* Identical (workspace, observer, observed, query, top_k, filters, max_distance) calls are merged and counted.

Outputs in ``--out-dir``: ``calls.jsonl`` (every unique call), ``sample.jsonl`` (all dialectic and prefetch calls plus
``--sample-size`` dream calls spread round-robin over caller type and peer pair; fixed seed) and
``harvest_summary.json`` (counts, per-day volume, omissions, the sha256 of every source file read).
Calls contain query text and peer names: keep them private, never commit them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchlib import write_jsonl  # noqa: E402

PREFETCH_MARKER = "\n\n## Relevant Observations (prefetched)\n"


def read_events(source: Path) -> tuple[list[dict], list[dict], int]:
    """Parse every event file from a byte snapshot, so a file still being appended to cannot change mid-run."""
    events, snapshots, malformed = [], [], 0
    for path in sorted(source.glob("events-*.jsonl")):
        raw = path.read_bytes()
        snapshots.append(dict(file=path.name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()))
        for number, line in enumerate(raw.splitlines(), 1):
            try:
                event = json.loads(line)["event"]
                event["_source"] = dict(file=path.name, line=number, event_id=event.get("id"))
                events.append(event)
            except (ValueError, KeyError, TypeError):
                malformed += 1
    return events, snapshots, malformed


def identity(row: dict) -> str:
    """Filters belong to the identity: two prefetch searches share pair, query and top_k but cover disjoint levels."""
    keys = ("workspace_name", "observer", "observed", "query", "top_k", "filters", "max_distance")
    return json.dumps([row.get(k) for k in keys], sort_keys=True)


def _base(event: dict, data: dict, caller: str, query, top_k, observer, observed) -> dict:  # noqa: ANN001
    return dict(caller_type=caller, workspace_name=data.get("workspace_name"), observer=observer, observed=observed,
                query=query, top_k=top_k, filters=None, max_distance=None, session_allowlist=None,
                results_count=None, run_id=data.get("run_id"), timestamp=event.get("time"),
                provenance=[event["_source"]], replay_eligible=True, replay_limitations=[],
                scope_approximated=False, occurrences=1)


def apply_scope_approximation(row: dict) -> dict:
    row["scope_approximated"] = row["caller_type"].startswith("dialectic")
    if row["scope_approximated"]:
        row["session_allowlist"] = None
        row["replay_eligible"] = all(row.get(k) for k in ("workspace_name", "observer", "observed", "query"))
    return row


def harvest(events: list[dict]) -> tuple[list[dict], dict, list[dict]]:
    runs: dict[str, list[dict]] = defaultdict(list)
    completions: dict[tuple, list[dict]] = defaultdict(list)
    contents: dict[str, dict] = {}
    omissions: Counter = Counter()
    for event in events:
        data = event["data"]
        if data.get("run_id"):
            runs[data["run_id"]].append(event)
        if event["type"] == "trace.content":
            contents[data.get("content_hash")] = data
        if event["type"] == "agent.tool.call.completed":
            completions[(data.get("run_id"), data.get("provider_tool_call_id"))].append(event)

    rows: list[dict] = []
    for event in events:
        if event["type"] != "llm.call.traced":
            continue
        data = event["data"]
        for tool in data.get("output_tool_calls", []):
            if tool.get("name") != "search_memory":
                continue
            args = tool.get("input", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    omissions["invalid_tool_input"] += 1
                    continue
            completed = completions.get((data.get("run_id"), tool.get("id")), [])
            if not completed:
                omissions["search_memory_missing_completion"] += 1
                continue
            completion = completed[-1]
            meta = completion["data"]
            if meta.get("is_error"):
                omissions["search_memory_error"] += 1
                continue
            parent = meta.get("parent_category") or data.get("parent_category")
            agent = meta.get("agent_type") or data.get("agent_type")
            if parent not in ("dream", "dialectic"):
                omissions["unsupported_caller"] += 1
                continue
            caller = f"dream.{agent}" if parent == "dream" else "dialectic"
            observers = data.get("observers") or []
            observer = data.get("observer") or (observers[0] if len(observers) == 1 else None)
            observed = data.get("observed")
            for context in runs[data.get("run_id")]:
                if context["type"] in ("dream.specialist", "dream.run"):
                    observer = observer or context["data"].get("observer")
                    observed = observed or context["data"].get("observed")
            row = _base(event, data, caller, args.get("query"), meta.get("top_k", args.get("top_k", 20)), observer, observed)
            row["results_count"] = meta.get("results_count")
            row["provenance"].append(completion["_source"])
            if parent == "dialectic":
                apply_scope_approximation(row)
            if not all((row["workspace_name"], observer, observed, row["query"])):
                row["replay_limitations"].append("missing exact pair or query")
            if row["results_count"] is None:
                row["replay_limitations"].append("original results_count absent")
            row["replay_eligible"] = not row["replay_limitations"]
            rows.append(row)

    # Prefetch: the query is the user message of the traced dialectic call; no text is invented from answers.
    for run_id, related in sorted(runs.items()):
        prefetch = [e for e in related if e["type"] == "embedding.call.traced"
                    and e["data"].get("call_purpose") == "dialectic_prefetch"]
        if not prefetch:
            continue
        traces = sorted((e for e in related if e["type"] == "llm.call.traced"
                         and e["data"].get("parent_category") == "dialectic"), key=lambda e: e.get("time", ""))
        found = None
        for trace in traces:
            for ref in trace["data"].get("input_message_refs", []):
                content = contents.get(ref, {})
                text = content.get("content")
                if content.get("role") == "user" and isinstance(text, str) and text.startswith("Query: ") and PREFETCH_MARKER in text:
                    found = (text[len("Query: "):].split(PREFETCH_MARKER, 1)[0], trace)
                    break
            if found:
                break
        if not found:
            omissions["dialectic_prefetch_query_unrecoverable"] += len(prefetch)
            continue
        query, trace = found
        completed = next((e for e in related if e["type"] == "dialectic.completed"), None)
        if not completed or not completed["data"].get("reasoning_level"):
            omissions["dialectic_prefetch_limit_unknown"] += len(prefetch)
            continue
        data = trace["data"]
        observers = data.get("observers") or []
        observer = data.get("observer") or (observers[0] if len(observers) == 1 else None)
        for levels in (["explicit"], ["deductive", "inductive", "contradiction"]):
            row = _base(prefetch[0], data, "dialectic_prefetch", query,
                        10 if completed["data"]["reasoning_level"] == "minimal" else 25, observer, data.get("observed"))
            row["filters"] = {"level": {"in": levels}}
            row["provenance"] += [trace["_source"], completed["_source"]]
            row["replay_limitations"] = ["only the aggregate prefetched count is recorded, not the per-search result count"]
            apply_scope_approximation(row)
            rows.append(row)

    omissions["session_context_search_query_unrecorded"] = sum(
        e["type"] == "embedding.call.traced" and e["data"].get("call_purpose") == "session_context_search" for e in events)

    unique: dict[str, dict] = {}
    for row in rows:
        key = identity(row)
        if key in unique:
            first = unique[key]
            first["occurrences"] += 1
            if row["replay_eligible"] and not first["replay_eligible"]:
                row["call_id"], row["occurrences"] = first["call_id"], first["occurrences"]
                unique[key] = row
        else:
            row["call_id"] = hashlib.sha256(key.encode()).hexdigest()[:24]
            unique[key] = row
    return list(unique.values()), dict(omissions), rows


def sample_calls(calls: list[dict], size: int = 300, seed: int = 20261031) -> list[dict]:
    """Every eligible dialectic/prefetch call, plus ``size`` dream calls dealt round-robin over (caller, workspace, pair)."""
    eligible = [r for r in calls if r.get("replay_eligible")]
    chosen = sorted((r for r in eligible if r["caller_type"].startswith("dialectic")), key=lambda r: r["call_id"])
    strata: dict[tuple, list[dict]] = defaultdict(list)
    for r in eligible:
        if not r["caller_type"].startswith("dialectic"):
            strata[(r["caller_type"], r["workspace_name"], r["observer"], r["observed"])].append(r)
    rng = random.Random(seed)
    buckets = [sorted(v, key=lambda r: r["call_id"]) for _, v in sorted(strata.items())]
    for bucket in buckets:
        rng.shuffle(bucket)
    target = len(chosen) + size
    while buckets and len(chosen) < target:
        for bucket in buckets:
            if len(chosen) >= target:
                break
            chosen.append(bucket.pop())
        buckets = [b for b in buckets if b]
    return chosen


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", type=Path, required=True, help="folder holding events-*.jsonl")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--sample-size", type=int, default=300, help="dream calls to sample, on top of every dialectic call")
    p.add_argument("--seed", type=int, default=20261031)
    args = p.parse_args(argv)
    events, snapshots, malformed = read_events(args.source)
    calls, omissions, raw = harvest(events)
    sample = sample_calls(calls, args.sample_size, args.seed)
    write_jsonl(args.out_dir / "calls.jsonl", calls)
    write_jsonl(args.out_dir / "sample.jsonl", sample)
    summary = dict(
        source_files=snapshots, events=len(events), malformed_lines=malformed, raw_calls=len(raw), unique_calls=len(calls),
        counts_per_caller=dict(Counter(r["caller_type"] for r in calls)),
        raw_calls_per_day=dict(Counter((r["timestamp"] or "")[:10] for r in raw)), omissions=omissions,
        seed=args.seed, sample_calls=len(sample), sample_counts_per_caller=dict(Counter(r["caller_type"] for r in sample)),
        limitations=["Dialectic and prefetch are replayed peer-wide (scope_approximated): descriptive only.",
                     "Prefetch results_count is null: only a combined count is recorded.",
                     "Session-context searches have no recorded query and are omitted.",
                     "Identical searches are merged; filters are part of the identity."])
    (args.out_dir / "harvest_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(dict(unique_calls=len(calls), sample_calls=len(sample), omissions=omissions)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
