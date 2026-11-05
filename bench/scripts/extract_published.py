"""Turn the private run outputs into the aggregate-only summary files published in bench/results.

This is the audit link between a private run and a published number. It reads your report files, keeps only fields on
an allow-list (counts, means, intervals, latencies, costs, caller types and arm names) and writes small JSON files.
Query text, conclusion text, peer or workspace names, document ids, call ids, paths and hosts are not on any
allow-list, so they cannot get through; ``--check`` re-reads an output and fails on anything that looks like one.

    python bench/scripts/extract_published.py study2 --report report.json --out study2.json
    python bench/scripts/extract_published.py study3 --sweep ef-sweep.json --out study3.json
    python bench/scripts/extract_published.py study4 --report ab.json --out study4.json
    python bench/scripts/extract_published.py check study2.json study3.json study4.json

Study 1's labels come from a separate review process, so its summary is assembled from two report files that process
wrote (``--report`` with metrics/comparisons/operational and ``--supplementary`` with the interval table).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# A subset may be named after the text of a query; only these names pass, anything else is published under a neutral one.
SUBSET_NAMES = {"dream_callers_only"}


def _subset(name: str) -> str:
    return name if name in SUBSET_NAMES else "exclude_one_nonspecific_call"
CALLERS = {"overall", "dream.deduction", "dream.induction", "dialectic", "dialectic_prefetch"}
SUSPICIOUS = [
    re.compile(r"[A-Za-z]:[\\/]"),  # a Windows path
    re.compile(r"\\\\[A-Za-z0-9_.-]+\\"),  # a network share
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),  # an IPv4 address
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # an email address
    re.compile(r"\b(?:sk|pa|key)-[A-Za-z0-9_-]{8,}"),  # an API-key-looking token
    re.compile(r"\b[0-9a-f]{24,}\b"),  # a long hex id (call or document id)
]


def _round(value, places=8):
    return round(value, places) if isinstance(value, float) else value


def _stat(stat: dict | None) -> dict | None:
    if not stat:
        return None
    return dict(delta=_round(stat["delta"]), ci95=[_round(x) for x in stat["ci95"]], clusters=stat["clusters"],
                iterations=stat.get("iterations"), seed=stat.get("seed"))


def study2(report: dict) -> dict:
    """Three-judge replay: majority-grade metrics per caller and arm, primary contrasts, judge agreement."""
    src = report["sources"][report["primary_source"]]
    metrics = [dict(caller=m["caller"], arm=m["arm"], calls=m["calls"], clusters=m["clusters"], scored_calls=m["scored_calls"],
                    fallback_rate=_round(m["fallback_rate"]), ndcg=_round(m["ndcg"]), blended_ndcg=_round(m["blended_ndcg"]),
                    precision=_round(m["precision"]), list_price_usd_per_1000_calls=_round(m["list_price_usd_per_1000_calls"]))
               for m in src["metrics"] if m["threshold"] == 2 and m["k"] == "caller_k" and m["caller"] in CALLERS]
    comparisons = [dict(caller=c["caller"], baseline=c["baseline"], arm=c["arm"], paired_calls=c["paired_calls"],
                        ndcg=_stat(c["ndcg"]), primary=c["primary"], descriptive_only=c["descriptive_only"])
                   for c in src["comparisons"] if c["caller"] in CALLERS]
    agreement = [dict(left=a["left"], right=a["right"], pairs=a["pairs"], exact_agreement=_round(a["exact_agreement"]),
                      weighted_cohen_kappa=_round(a["weighted_cohen_kappa"]), confusion=a["confusion"])
                 for a in report["judge_agreement"]]
    per_judge = {name: {m["arm"]: dict(ndcg=_round(m["ndcg"]), precision=_round(m["precision"]), scored_calls=m["scored_calls"])
                        for m in entry["metrics"]
                        if m["caller"] == "overall" and m["threshold"] == 2 and m["k"] == "caller_k"}
                 for name, entry in report["sources"].items()}
    return dict(primary_source=report["primary_source"], coverage=src["coverage"], metrics=metrics,
                comparisons=comparisons, judge_agreement=agreement, per_judge_overall=per_judge,
                decision=report["decision"], rule=report["rule"])


def study3(sweep: dict) -> dict:
    """ef_search sweep: recall, identical-to-exact share, fill and latency per arm and caller."""
    allowed = ("searches", "searches_with_exact_result", "recall", "identical_to_exact", "underfilled", "fill",
               "db_ms_median", "db_ms_p95")
    arms = {arm: {group: {k: _round(v[k], 4) for k in allowed} for group, v in groups.items()}
            for arm, groups in sweep["arms"].items()}
    return dict(arms=arms, exact_underfilled=_round(sweep["exact_underfilled"], 4))


def study4(report: dict) -> dict:
    """A/B dream quality: judged retrieval, new conclusions, answer verdicts, dream effort per arm."""
    keep = ("arms", "baseline", "retrieval", "new_conclusions", "answers_vs_baseline", "dream_effort")
    return {k: report[k] for k in keep if k in report}


def study1(report: dict, supplementary: dict) -> dict:
    """Conclusion replay (80 frozen calls, every candidate judged). Fields as the review process wrote them."""
    m = [dict(arm=r["arm"], caller=r["caller"], threshold=r["threshold"], k=r["k"], scored_calls=r["scored_calls"],
              clusters=r["clusters"], precision=_round(r["precision"]), recall=_round(r["recall"]), ndcg=_round(r["ndcg"]),
              synthetic_control=r["synthetic_control"])
         for r in report["metrics"] if r["caller"] in CALLERS]
    ops = [{k: (_round(v) if not isinstance(v, dict) else {kk: _round(vv) for kk, vv in v.items()})
            for k, v in o.items() if k not in ("price_note",)} for o in report["operational"]]
    comps = [dict(caller=c["caller"], baseline=c["baseline"], arm=c["arm"], paired_calls=c["paired_calls"],
                  ndcg=_stat(c["ndcg"]), precision_delta=_round(c["precision_delta"]))
             for c in report["comparisons"] if c["caller"] in CALLERS]
    sup = [dict(left=c["left"], right=c["right"], calls=c["calls"], clusters=c["clusters"],
                precision=_stat(c["statistics"]["precision"]), ndcg=_stat(c["statistics"]["ndcg"]),
                precision_call_outcomes=c["precision_call_outcomes"]) for c in supplementary["comparisons"]]
    return dict(calls=report["calls"], labels=report["labels"], grade_distribution=supplementary.get("grade_distribution"),
                metrics=m, operational=ops, comparisons=comps, supplementary_comparisons=sup,
                sensitivity=[dict(x, subset=_subset(x["subset"])) for x in supplementary.get("sensitivity") or []],
                earlier_label_changes=dict(
                    rows=supplementary["old_returned_snapshot"]["rows"],
                    changed_rows=supplementary["old_returned_snapshot"]["changed_rows"],
                    transitions=supplementary["old_returned_snapshot"]["transitions"]))


def check(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [f"{path.name}: {p.pattern!r} matches {m.group(0)!r}" for p in SUSPICIOUS for m in [p.search(text)] if m]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("study1", "study2", "study3", "study4"):
        s = sub.add_parser(name)
        s.add_argument("--out", required=True)
        if name == "study1":
            s.add_argument("--report", required=True)
            s.add_argument("--supplementary", required=True)
        elif name == "study3":
            s.add_argument("--sweep", required=True)
        else:
            s.add_argument("--report", required=True)
    c = sub.add_parser("check")
    c.add_argument("files", nargs="+")
    args = p.parse_args(argv)
    if args.cmd == "check":
        problems = [msg for f in args.files for msg in check(Path(f))]
        print("\n".join(problems) or "clean")
        return 1 if problems else 0
    load = lambda f: json.loads(Path(f).read_text(encoding="utf-8"))  # noqa: E731
    if args.cmd == "study1":
        out = study1(load(args.report), load(args.supplementary))
    elif args.cmd == "study2":
        out = study2(load(args.report))
    elif args.cmd == "study3":
        out = study3(load(args.sweep))
    else:
        out = study4(load(args.report))
    Path(args.out).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    problems = check(Path(args.out))
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
