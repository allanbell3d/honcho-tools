"""Score rankings against judgments and apply the registered decision rule.

Inputs: ``calls.jsonl``, ``rankings.jsonl`` (from run_arms.py) and one or more ``judgments-<judge>.jsonl`` files (from
judge.py, or any file with ``call_id, document_id, grade``). Outputs ``report.json`` and, optionally, a Markdown summary.

How each headline number is computed
* nDCG / precision / recall / hit at the caller's ``top_k`` (and at 5 and 10): ``benchlib.ranking_metrics``. Relevant means
  grade 2 (``threshold 2``) or grade 1 and 2 (``threshold 1``).
* Fallback rows (the reranker failed and the vector order was kept) are excluded from the primary quality number and
  reported as a fallback rate; the blended nDCG that includes them is given next to it.
* Majority grade of three judges: ``benchlib.majority_grade`` (a call is scored only if every judged pair has all three).
* Paired comparison: mean per-call difference of the candidate against the baseline on matched non-fallback calls,
  95% interval from ``benchlib.paired_cluster_bootstrap`` (2000 draws, seed 42), clusters from ``benchlib.cluster_key``.
* Decision rule (bench/METHOD.md, section Decision rule): practical gain >= 0.03 with the interval above zero, at least
  10 independent clusters, candidate fallback rate under 5%, and every guardrail present and passing. Missing
  evidence is "inconclusive", never a pass.
"""
from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchlib import (  # noqa: E402
    cluster_key, majority_grade, mean_or_none, median_or_none, pairwise_agreement, paired_cluster_bootstrap,
    percentile, ranking_metrics, read_jsonl,
)

PRACTICAL_GAIN = 0.03
MAX_FALLBACK_RATE = 0.05
MIN_CLUSTERS = 10
K_LABELS = ("caller_k", "5", "10")
GUARDRAILS = ("exact_id_cases", "correction_cases", "scope_leaks")
CEILING_NAMES = {"exact", "ceiling", "exact_ceiling"}


# --------------------------------------------------------------------------------------------------------------
# Grades
# --------------------------------------------------------------------------------------------------------------
def grade_map(rows: list[dict]) -> dict[tuple[str, str], int]:
    """{(call_id, document_id): grade}. Missing or failed rows are skipped: they never become a zero."""
    out: dict[tuple[str, str], int] = {}
    for row in rows:
        if row.get("missing") or row.get("grade") is None:
            continue
        if type(row["grade"]) is not int or row["grade"] not in (0, 1, 2):
            raise ValueError("grades must be the integers 0, 1 or 2")
        out[(row["call_id"], str(row["document_id"]))] = row["grade"]
    return out


def grade_sources(judgments: dict[str, list[dict]]) -> dict[str, dict]:
    """One grade map per judge, plus ``majority`` when there are exactly three judges."""
    sources = {name: grade_map(rows) for name, rows in judgments.items()}
    if len(sources) == 3:
        common = set.intersection(*(set(m) for m in sources.values()))
        sources["majority"] = {k: majority_grade([m[k] for m in sources.values()]) for k in common}
    return sources


def expected_documents(rankings: list[dict]) -> dict[str, set[str]]:
    """Every document that must be judged for a call: all returned ids and a rerank arm's vector prefix."""
    need: dict[str, set[str]] = defaultdict(set)
    for row in rankings:
        limit = 20 if row["arm"].lower() in CEILING_NAMES else len(row["ids"])
        need[row["call_id"]].update(map(str, row["ids"][:limit]))
        need[row["call_id"]].update(map(str, row.get("candidate_ids", [])[: row["top_k"]]))
    return need


def with_vector_prefix(rankings: list[dict]) -> list[dict]:
    """Add ``<arm>+vector-prefix``: the same fetched pool without reranking (the same-pool control)."""
    extra = []
    for row in rankings:
        if row.get("candidate_ids") and row["arm"].startswith("rerank"):
            extra.append(dict(row, arm=row["arm"] + "+vector-prefix", ids=row["candidate_ids"][: row["top_k"]],
                              fallback_reason=None, provider_ms=0.0, rerank_tokens=0, outcome="vector_prefix"))
    return rankings + extra


# --------------------------------------------------------------------------------------------------------------
# Metrics per caller and arm
# --------------------------------------------------------------------------------------------------------------
def _k(label: str, call: dict) -> int:
    return int(call["top_k"]) if label == "caller_k" else int(label)


def _timing(values: list[float]) -> dict:
    return dict(n=len(values), median=median_or_none(values), p95=percentile(values, 0.95))


def arm_summary(rows: list[dict], calls: dict[str, dict], grades: dict[str, dict], threshold: int, label: str,
                rates: dict[str, float], baseline_rows: dict[str, dict]) -> dict:
    scored, blended = [], []
    for row in rows:
        call = calls[row["call_id"]]
        m = ranking_metrics(row["ids"], grades[row["call_id"]], _k(label, call), threshold)
        blended.append(m)
        if not row.get("fallback_reason"):
            scored.append(m)
    fallbacks = sum(bool(r.get("fallback_reason")) for r in rows)
    added = [r["total_ms"] - baseline_rows[r["call_id"]]["total_ms"] for r in rows
             if r["call_id"] in baseline_rows and r.get("total_ms") is not None
             and baseline_rows[r["call_id"]].get("total_ms") is not None]
    arm_model = rows[0].get("arm", "").split(":")[1] if rows[0].get("arm", "").startswith("rerank:") else None
    tokens = statistics.fmean(r.get("rerank_tokens") or 0 for r in rows)
    result = dict(
        calls=len(rows), scored_calls=len(scored), fallback_rows=fallbacks, fallback_rate=fallbacks / len(rows),
        clusters=len({cluster_key(calls[r["call_id"]]) for r in rows}),
        rows_returned=sum(len(r["ids"]) for r in rows), rows_asked=sum(int(r["top_k"]) for r in rows),
        overlap_with_baseline=mean_or_none([
            len(set(r["ids"]) & set(baseline_rows[r["call_id"]]["ids"])) / max(1, len(baseline_rows[r["call_id"]]["ids"]))
            for r in rows if r["call_id"] in baseline_rows]),
        total_ms=_timing([r["total_ms"] for r in rows if r.get("total_ms") is not None]),
        provider_ms=_timing([r["provider_ms"] for r in rows if r.get("outcome") == "reranked"]),
        added_ms_vs_baseline=_timing(added),
        rerank_tokens_per_call=tokens,
        list_price_usd_per_1000_calls=(tokens * rates[arm_model] / 1e6 * 1000 if arm_model in rates else None),
        blended_ndcg=mean_or_none([m["ndcg"] for m in blended]),
    )
    result.update({k: mean_or_none([m[k] for m in scored]) for k in ("precision", "recall", "ndcg", "hit")})
    return result


def build_report(calls: list[dict], rankings: list[dict], judgments: dict[str, list[dict]], *, baseline: str,
                 candidate: str | None = None, primary_source: str | None = None, vector_prefix: bool = False,
                 rates: dict[str, float] | None = None, guardrails: dict[str, str] | None = None,
                 contrasts: list[tuple[str, str]] | None = None, iterations: int = 2000, seed: int = 42) -> dict:
    rates = rates or {}
    guardrails = guardrails or {}
    call_by_id = {c["call_id"]: c for c in calls}
    rankings = with_vector_prefix(rankings) if vector_prefix else list(rankings)
    # A resumed run can hold a (call, arm) twice; the last row wins.
    rankings = list({(r["call_id"], r["arm"]): r for r in rankings}.values())
    sources = grade_sources(judgments)
    if not sources:
        raise ValueError("no judgments")
    primary_source = primary_source or ("majority" if "majority" in sources else next(iter(sources)))
    need = expected_documents(rankings)
    # A call is scored only if every document any judge was asked about for it (graded or failed) ended up graded.
    for rows in judgments.values():
        for row in rows:
            need[row["call_id"]].add(str(row["document_id"]))

    report: dict = dict(baseline=baseline, candidate=candidate, primary_source=primary_source, sources={})
    for name, graded in sources.items():
        by_call: dict[str, dict[str, int]] = defaultdict(dict)
        for (call_id, doc_id), grade in graded.items():
            by_call[call_id][doc_id] = grade
        # A call no arm returned anything for has nothing to score and is left out.
        eligible = {cid for cid, docs in need.items() if cid in call_by_id and docs and docs <= set(by_call.get(cid, {}))}
        # The exact ceiling only widens the judged pool (its first 20); it is not scored as an arm.
        usable = [r for r in rankings if r["call_id"] in eligible and not r.get("error")
                  and r["arm"].lower() not in CEILING_NAMES]
        base_rows = {r["call_id"]: r for r in usable if r["arm"] == baseline}
        entry: dict = dict(coverage=dict(complete_calls=len(eligible), total_calls=len(call_by_id)), metrics=[],
                           comparisons=[])
        groups = ["overall"] + sorted({c["caller_type"] for c in call_by_id.values()})
        arms = sorted({r["arm"] for r in usable})
        per_call: dict[tuple, dict[str, dict]] = {}
        for caller in groups:
            for arm in arms:
                rows = [r for r in usable if r["arm"] == arm and (caller == "overall" or
                                                                   call_by_id[r["call_id"]]["caller_type"] == caller)]
                if not rows:
                    continue
                for threshold in (1, 2):
                    for label in K_LABELS:
                        summary = arm_summary(rows, call_by_id, by_call, threshold, label, rates, base_rows)
                        summary.update(caller=caller, arm=arm, threshold=threshold, k=label,
                                       scope_approximated=any(call_by_id[r["call_id"]].get("scope_approximated") for r in rows),
                                       few_clusters=summary["clusters"] < MIN_CLUSTERS)
                        entry["metrics"].append(summary)
                        if threshold == 2 and label == "caller_k":
                            per_call[(caller, arm)] = {
                                r["call_id"]: ranking_metrics(r["ids"], by_call[r["call_id"]], _k(label, call_by_id[r["call_id"]]), 2)
                                for r in rows if not r.get("fallback_reason")}
        pairs = [(baseline, arm) for arm in arms if arm != baseline] + [tuple(c) for c in (contrasts or [])]
        for caller in groups:
            for left_arm, arm in pairs:
                if (caller, arm) not in per_call or (caller, left_arm) not in per_call:
                    continue
                a, b = per_call[(caller, left_arm)], per_call[(caller, arm)]
                common = sorted(a.keys() & b.keys())
                if not common:
                    continue
                stat = paired_cluster_bootstrap([a[c]["ndcg"] for c in common], [b[c]["ndcg"] for c in common],
                                                [cluster_key(call_by_id[c]) for c in common], iterations, seed)
                wins = sum(b[c]["precision"] > a[c]["precision"] for c in common)
                losses = sum(b[c]["precision"] < a[c]["precision"] for c in common)
                entry["comparisons"].append(dict(
                    caller=caller, baseline=left_arm, arm=arm, paired_calls=len(common), ndcg=stat,
                    precision_delta=statistics.fmean(b[c]["precision"] - a[c]["precision"] for c in common),
                    precision_calls_improved=wins, precision_calls_worsened=losses,
                    precision_calls_tied=len(common) - wins - losses,
                    primary=(left_arm == baseline and arm == candidate and caller != "overall"),
                    descriptive_only=any(call_by_id[c].get("scope_approximated") for c in common)))
        report["sources"][name] = entry
    if len(sources) >= 2:
        judges = [n for n in sources if n != "majority"]
        report["judge_agreement"] = [dict(left=l, right=r, **pairwise_agreement(sources[l], sources[r]))
                                     for l, r in itertools.combinations(judges, 2)]
    primary = report["sources"][primary_source]
    report["decision"] = decide(primary, candidate, guardrails) if candidate else None
    report["rule"] = dict(practical_gain=PRACTICAL_GAIN, max_fallback_rate=MAX_FALLBACK_RATE, min_clusters=MIN_CLUSTERS,
                          bootstrap=dict(iterations=iterations, seed=seed))
    return report


def decide(source: dict, candidate: str, guardrails: dict[str, str]) -> dict:
    """The registered decision rule, one verdict per primary workload. Missing evidence never passes."""
    fallback = {(m["caller"]): m["fallback_rate"] for m in source["metrics"]
                if m["arm"] == candidate and m["threshold"] == 2 and m["k"] == "caller_k"}
    out = []
    for c in (c for c in source["comparisons"] if c["primary"]):
        stat, reasons = c["ndcg"], []
        if stat["delta"] < PRACTICAL_GAIN:
            reasons.append("gain below +0.03")
        if stat["ci95"][0] <= 0:
            reasons.append("interval includes zero")
        if stat["few_clusters"]:
            reasons.append("fewer than 10 clusters")
        if fallback.get(c["caller"], 1.0) >= MAX_FALLBACK_RATE:
            reasons.append("fallback rate at least 5%")
        if c["descriptive_only"]:
            reasons.append("scope was approximated: descriptive only")
        for g in GUARDRAILS:
            if guardrails.get(g) != "pass":
                reasons.append(f"guardrail {g}: " + ("failed" if guardrails.get(g) == "fail" else "no evidence"))
        out.append(dict(workload=c["caller"], contrast=[c["baseline"], c["arm"]], delta=stat["delta"],
                        ci95=stat["ci95"], clusters=stat["clusters"], verdict="inconclusive" if reasons else "eligible",
                        reasons=reasons))
    verdict = "eligible" if out and all(o["verdict"] == "eligible" for o in out) else "inconclusive"
    return dict(verdict=verdict, workloads=out,
                note="A practical gain alone never authorises a production change; missing evidence is inconclusive.")


# --------------------------------------------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------------------------------------------
def render_markdown(report: dict) -> str:
    source = report["sources"][report["primary_source"]]
    lines = [f"# Report ({report['primary_source']} grades)", "",
             f"Complete calls: {source['coverage']['complete_calls']} of {source['coverage']['total_calls']}.", "",
             "| caller | arm | calls | clusters | scored | nDCG@k | P@k | fallback | blended nDCG |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for m in source["metrics"]:
        if m["threshold"] == 2 and m["k"] == "caller_k":
            lines.append(f"| {m['caller']} | {m['arm']} | {m['calls']} | {m['clusters']} | {m['scored_calls']} | "
                         f"{_f(m['ndcg'])} | {_f(m['precision'])} | {m['fallback_rate']:.1%} | {_f(m['blended_ndcg'])} |")
    lines += ["", "| caller | arm vs baseline | paired calls | clusters | nDCG delta | 95% interval |", "|---|---|---:|---:|---:|---|"]
    for c in source["comparisons"]:
        lines.append(f"| {c['caller']} | {c['arm']} | {c['paired_calls']} | {c['ndcg']['clusters']} | "
                     f"{c['ndcg']['delta']:+.4f} | {c['ndcg']['ci95'][0]:+.4f} to {c['ndcg']['ci95'][1]:+.4f} |")
    if report.get("decision"):
        lines += ["", f"Decision: **{report['decision']['verdict']}**"]
        for w in report["decision"]["workloads"]:
            lines.append(f"- {w['workload']}: {w['verdict']}" + (f" ({'; '.join(w['reasons'])})" if w["reasons"] else ""))
    return "\n".join(lines) + "\n"


def _f(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--calls", required=True)
    p.add_argument("--rankings", required=True)
    p.add_argument("--judgments", nargs="+", required=True, help="judgments-<judge>.jsonl files (the stem names the judge)")
    p.add_argument("--baseline", required=True, help="arm name of the baseline, e.g. stock")
    p.add_argument("--candidate", help="arm name of the candidate for the primary contrast")
    p.add_argument("--primary-source", help="judge name or 'majority'")
    p.add_argument("--vector-prefix-control", action="store_true")
    p.add_argument("--contrast", nargs=2, action="append", metavar=("LEFT", "RIGHT"),
                   help="extra descriptive contrast between two arms (repeatable), e.g. a pool against a larger pool")
    p.add_argument("--rates", help="JSON {model: USD per million rerank tokens}")
    p.add_argument("--guardrails", help='JSON {"exact_id_cases"|"correction_cases"|"scope_leaks": "pass"|"fail"}')
    p.add_argument("--out", required=True)
    p.add_argument("--markdown")
    args = p.parse_args(argv)

    judgments = {Path(f).stem.removeprefix("judgments-"): read_jsonl(f) for f in args.judgments}
    rates = {k: float(v) for k, v in json.loads(Path(args.rates).read_text()).items() if not k.startswith("_")} if args.rates else {}
    guardrails = json.loads(Path(args.guardrails).read_text()) if args.guardrails else {}
    report = build_report(read_jsonl(args.calls), read_jsonl(args.rankings), judgments, baseline=args.baseline,
                          candidate=args.candidate, primary_source=args.primary_source,
                          vector_prefix=args.vector_prefix_control, rates=rates, guardrails=guardrails,
                          contrasts=args.contrast)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown:
        Path(args.markdown).write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(dict(out=args.out, decision=(report["decision"] or {}).get("verdict"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
