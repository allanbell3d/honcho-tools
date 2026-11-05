"""Recall of the vector index against the exact search, per ``ef_search`` setting, caller and run.

Inputs are ``rankings.jsonl`` files from run_arms.py, one per arm, one of them the exact ceiling::

    python bench/scripts/ef_sweep.py --calls calls.jsonl --exact exact=rankings-exact.jsonl \
        --arm stock=rankings-stock.jsonl --arm ef100=rankings-ef100.jsonl --arm ef400=rankings-ef400.jsonl --out ef-sweep.json

Definitions (per search, then averaged; searches whose exact result is empty are left out of recall):
* recall = |arm ids intersect exact ids[:top_k]| / |exact ids[:top_k]|
* identical = the arm's ordered ids equal the exact ids[:top_k]
* underfilled = the arm returned fewer rows than ``top_k``. Part of that is the index (rows lost to post-filtering),
  part is the corpus (fewer matching rows exist, or ``max_distance`` cuts them), so compare against the exact arm's
  own fill before blaming the index.
* latency is the median and 95th percentile of ``db_ms`` as recorded. Run the arms one after another if you want it
  comparable; arms run side by side contend for the same database.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchlib import mean_or_none, median_or_none, percentile, read_jsonl  # noqa: E402


def sweep(calls: list[dict], exact_rows: list[dict], arms: dict[str, list[dict]]) -> dict:
    caller_of = {c["call_id"]: c["caller_type"] for c in calls}
    exact = {r["call_id"]: r for r in exact_rows if not r.get("error")}
    out: dict[str, dict] = {}
    for name, rows in arms.items():
        buckets: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for row in rows:
            if row.get("error"):
                continue
            call_id = row["call_id"]
            caller = caller_of.get(call_id, "unknown")
            top_k = int(row["top_k"])
            truth = [str(i) for i in exact[call_id]["ids"][:top_k]] if call_id in exact else []
            ids = [str(i) for i in row["ids"]]
            for group in ("all", caller):
                b = buckets[group]
                b["searches"].append(1)
                b["underfilled"].append(len(ids) < top_k)
                b["fill"].append(min(1.0, len(ids) / max(1, top_k)))
                if row.get("db_ms") is not None:
                    b["db_ms"].append(row["db_ms"])
                if truth:
                    b["recall"].append(len(set(ids) & set(truth)) / len(truth))
                    b["identical"].append(ids == truth)
        out[name] = {
            group: dict(
                searches=len(b["searches"]), searches_with_exact_result=len(b["recall"]),
                recall=mean_or_none(b["recall"]), identical_to_exact=mean_or_none([float(x) for x in b["identical"]]),
                underfilled=mean_or_none([float(x) for x in b["underfilled"]]), fill=mean_or_none(b["fill"]),
                db_ms_median=median_or_none(b["db_ms"]), db_ms_p95=percentile(b["db_ms"], 0.95))
            for group, b in sorted(buckets.items())
        }
    return dict(arms=out, exact_underfilled=_exact_fill(exact_rows))


def _exact_fill(rows: list[dict]) -> float | None:
    return mean_or_none([float(len(r["ids"]) < int(r["top_k"])) for r in rows if not r.get("error")])


def _pair(text: str) -> tuple[str, str]:
    name, sep, path = text.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError("expected NAME=PATH")
    return name, path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--calls", required=True)
    p.add_argument("--exact", type=_pair, required=True, metavar="NAME=PATH")
    p.add_argument("--arm", type=_pair, action="append", required=True, metavar="NAME=PATH")
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    result = sweep(read_jsonl(args.calls), read_jsonl(args.exact[1]),
                   {name: read_jsonl(path) for name, path in args.arm})
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({n: round(v["all"]["recall"], 4) for n, v in result["arms"].items() if v.get("all", {}).get("recall") is not None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
