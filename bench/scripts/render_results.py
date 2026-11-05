"""Render bench/results/RESULTS.md from the summary JSON files in the same folder.

Every number in RESULTS.md is either printed from one of those JSON files by this script or is in a block the JSON
marks as hand-transcribed. A test re-renders the file and fails if the committed RESULTS.md differs, so the tables
cannot drift from the data.

    python bench/scripts/render_results.py            # rewrite bench/results/RESULTS.md
    python bench/scripts/render_results.py --check    # exit 1 if it is out of date
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "results"
CALLERS = ["dream.deduction", "dream.induction", "dialectic", "dialectic_prefetch"]

S1_ARMS = [  # (arm key, label)
    ("ef100", "vector, strict-order scan, ef_search 100"),
    ("ef250", "vector, strict-order scan, ef_search 250 (baseline)"),
    ("ef300", "vector, strict-order scan, ef_search 300"),
    ("strict200", "vector, strict-order scan, ef_search 200"),
    ("strict400", "vector, strict-order scan, ef_search 400"),
    ("rr100", "rerank-3-lite, pool 100"),
    ("rr200", "rerank-3-lite, pool 200"),
    ("rr300", "rerank-3-lite, pool 300"),
    ("rr400", "rerank-3-lite, pool 400"),
]


def load(name: str) -> dict:
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def pct(x: float, places: int = 2) -> str:
    return f"{100 * x:.{places}f}%"


def pp(x: float) -> str:
    return f"{100 * x:+.2f}"


def ci_pp(stat: dict) -> str:
    return f"{100 * stat['ci95'][0]:+.2f} to {100 * stat['ci95'][1]:+.2f}"


def ci(stat: dict, places: int = 4) -> str:
    return f"{stat['ci95'][0]:+.{places}f} to {stat['ci95'][1]:+.{places}f}"


def table(header: list[str], rows: list[list[str]], align: str | None = None) -> str:
    align = align or "l" + "r" * (len(header) - 1)
    rule = "|" + "|".join(":---" if a == "l" else "---:" for a in align) + "|"
    return "\n".join(["| " + " | ".join(header) + " |", rule] + ["| " + " | ".join(r) + " |" for r in rows]) + "\n"


# ---------------------------------------------------------------------------------------------------------- study 1
def study1(d: dict) -> str:
    metric = {(m["caller"], m["arm"], m["threshold"], m["k"]): m for m in d["metrics"]}
    ops = {o["arm"]: o for o in d["operational"]}
    rows = []
    for arm, label in S1_ARMS:
        m, o = metric[("overall", arm, 2, "caller_k")], ops[arm]
        rows.append([label, pct(m["precision"]), f"{m['ndcg']:.4f}", pct(metric[("overall", arm, 2, "5")]["precision"]),
                     pct(metric[("overall", arm, 2, "10")]["precision"]),
                     f"{o['total_ms']['median']:.0f} ({o['total_ms']['p95']:.0f})",
                     f"{o['paired_total_delta_ms']['median']:+.0f}", f"{o['list_price_usd_per_1000_calls']:.3f}",
                     f"{o['rerank_tokens_per_call']:,.0f}"])
    out = ["## Study 1: conclusion-search replay, 80 frozen calls, every candidate judged", "",
           f"Source: `study1_conclusion_replay.json`. {d['calls']} real search calls (20 per caller type), {len(S1_ARMS)} arms, "
           f"{d['labels']:,} (call, conclusion) cases graded 0, 1 or 2 by one model family and then adjudicated against the source "
           "text; there is no human gold set. Strict precision is the share of the first `top_k` results graded 2, averaged over "
           "calls. nDCG uses grade 2 as relevant and the judged pool, not the corpus, as the ideal. Latency columns are the median "
           "(95th percentile) of total time per call and the median of paired per-call differences against the baseline arm; "
           "arms ran one after another, not interleaved. Cost is the reranker's list price for the tokens sent, not an invoice.", "",
           table(["Arm", "Strict P@k", "nDCG", "P@5", "P@10", "Total ms median (p95)", "Added ms vs baseline", "USD per 1,000 calls",
                  "Tokens per call"], rows), ""]

    comps = {(c["left"], c["right"]): c for c in d["supplementary_comparisons"]}
    contrast_rows = []
    for left, right, what in [("ef250", "rr100", "rerank pool 100 against the vector baseline"),
                              ("vector-prefix-rr100", "rr100", "same pool, vector order against reranked order (isolates the reranker)"),
                              ("ef250", "ef100", "ef_search 100 against 250"), ("ef250", "strict400", "ef_search 400 against 250"),
                              ("rr100", "rr200", "pool 200 against pool 100"), ("rr100", "rr300", "pool 300 against pool 100"),
                              ("rr100", "rr400", "pool 400 against pool 100")]:
        c = comps[(left, right)]
        o = c["precision_call_outcomes"]
        contrast_rows.append([what, f"{pp(c['precision']['delta'])} ({ci_pp(c['precision'])})",
                              f"{c['ndcg']['delta']:+.4f} ({ci(c['ndcg'])})",
                              f"{o.get('improved', 0)} / {o.get('tied', 0)} / {o.get('worsened', 0)}"])
    out += ["Paired contrasts (80 calls in 43 clusters; paired cluster bootstrap, 2,000 draws, seed 42; percentage points for precision):", "",
            table(["Contrast", "Precision change, pp (95% interval)", "nDCG change (95% interval)", "Calls improved / tied / worsened"],
                  contrast_rows, "lrrr"), ""]

    pc_rows = []
    for caller in CALLERS:
        base, cand = metric[(caller, "ef250", 2, "caller_k")], metric[(caller, "rr100", 2, "caller_k")]
        cmp_ = next(c for c in d["comparisons"] if c["caller"] == caller and c["baseline"] == "ef250" and c["arm"] == "rr100")
        pc_rows.append([caller, f"{base['scored_calls']} / {base['clusters']}", pct(base["precision"]), pct(cand["precision"]),
                        pp(cand["precision"] - base["precision"]), f"{cmp_['ndcg']['delta']:+.4f}"])
    out += ["Per caller, pool 100 against the vector baseline. The dialectic and prefetch rows were replayed peer-wide because their "
            "session scope was not recorded, and rest on 5 and 7 clusters: descriptive only.", "",
            table(["Caller", "Calls / clusters", "Vector", "Rerank, pool 100", "Gain, pp", "nDCG gain"], pc_rows), ""]

    sens = d["sensitivity"]
    out += ["Sensitivity of the pool-100 gain (point estimates):", "",
            table(["Subset", "Calls", "Precision change, pp", "nDCG change"],
                  [[s["subset"].replace("_", " "), str(s["calls"]), pp(s["precision_delta"]), f"{s['ndcg_delta']:+.4f}"]
                   for s in sens if s["arm"] == "rr100"]), ""]

    h = d["hand_transcribed_from_run_summary"]
    behave = [[pool, f"{v['kept']:.2f}", f"{v['from_deeper']:.1f}", str(v["pool_used"]), str(v["voyage_ms"]), f"{v['tokens_per_call']:,}"]
              for pool, v in h["conclusion_search_80_calls"].items()]
    out += ["What reranking does to the result list, by pool size (hand-transcribed from the run summary): `kept` is the share of "
            "the vector top-`k` still in the reranked top-`k`; `from deeper` is the mean number of reranked results that came from "
            "beyond the vector top-`k`.", "",
            table(["Pool", "Kept", "From deeper", "Pool actually available", "Voyage call ms (median)", "Tokens per call"], behave), ""]

    msg = [[pool, str(v["reranked"]), str(v["http_errors"]), f"{100 * v['http_errors'] / 40:.0f}%", str(v["voyage_ms"]),
            f"{v['tokens_per_call']:,}"] for pool, v in h["message_search_40_calls_per_pool_relevance_not_judged"].items()]
    out += ["Reliability warning, message search (a different workload: long message payloads; 40 calls per pool; relevance not "
            "judged; hand-transcribed). The cause of the errors was not established.", "",
            table(["Pool", "Reranked", "HTTP errors", "Error rate", "Voyage call ms (median)", "Tokens per call"], msg), ""]

    lab = d["labelling_process"]
    chg = d["earlier_label_changes"]
    out += ["Labelling audit (hand-transcribed counts):", "",
            f"- {lab['packets']} grading packets, {lab['packet_rows']:,} packet rows for {lab['original_call_document_cases']:,} "
            f"cases ({lab['duplicate_instances_beyond_cases']:,} duplicate instances were injected or repeated).",
            f"- Before adjudication, fresh reviewers agreed on {lab['fresh_reviewer_agreement_before_adjudication']['global_sample'][0]} "
            f"of {lab['fresh_reviewer_agreement_before_adjudication']['global_sample'][1]} sampled contexts, and on "
            f"{lab['fresh_reviewer_agreement_before_adjudication']['enriched_new_range_sample'][0]} of "
            f"{lab['fresh_reviewer_agreement_before_adjudication']['enriched_new_range_sample'][1]} in an enriched sample "
            "(the two samples are not poolable).",
            f"- The final adjudication phase changed {lab['final_adjudication_scores_changed']} scores; a source-first refresh of "
            f"legacy packets changed {lab['legacy_packet_refresh_scores_changed'][0]} of {lab['legacy_packet_refresh_scores_changed'][1]:,}. "
            "These phase counts overlap and must not be summed.",
            f"- {chg['changed_rows']} of the {chg['rows']:,} labels used in an earlier, returned-results-only comparison changed.",
            f"- Independent verifier: {lab['independent_verifier']}. Human gold: {lab['human_gold']}.", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------------- study 2
def study2(d: dict) -> str:
    m = {(x["caller"], x["arm"]): x for x in d["metrics"]}
    cols = [("stock", "stock"), ("scan", "scan"), ("rerank:rerank-3-lite:50", "lite 50"), ("rerank:rerank-3-lite:100", "lite 100"),
            ("rerank:rerank-3-lite:200", "lite 200"), ("rerank:rerank-3:50", "r3 50"), ("rerank:rerank-3:100", "r3 100"),
            ("rerank:rerank-3:200", "r3 200")]
    rows = []
    for caller in CALLERS + ["overall"]:
        first = m[(caller, "stock")]
        rows.append([f"{caller} ({first['calls']} / {first['clusters']})"] + [f"{m[(caller, a)]['ndcg']:.3f}" for a, _ in cols]
                    + [pct(m[(caller, 'rerank:rerank-3-lite:100')]["fallback_rate"])])
    cov = d["coverage"]
    out = ["## Study 2: three-judge replay (earlier, larger run)", "",
           f"Source: `study2_three_judge_replay.json`. {cov['complete_calls']} calls with complete grades, every result graded 0-2 by "
           "three model judges; a result counts as relevant when at least two judges gave 2 (majority grade). nDCG at the caller's "
           "`top_k`; calls that fell back to vector order are excluded from the reranked columns and counted in the last column. "
           "`stock` is the server's default settings, `scan` is iterative scan with `ef_search = max(40, top_k)`, `lite N` and `r3 N` "
           "are rerank-3-lite and rerank-3 with pool N (iterative scan, `ef_search` = pool). Latency from this run is not reported: "
           "it was measured with several calls in flight and cannot be separated from provider time.", "",
           table(["Caller (calls / clusters)"] + [c for _, c in cols] + ["Fallback, lite 100"], rows), ""]

    prim = [c for c in d["comparisons"] if c["primary"] and c["caller"].startswith("dream")]
    rates = json.loads((RESULTS / "costs.json").read_text(encoding="utf-8"))["rates_used_usd_per_million_tokens"]
    ratio = rates["rerank-3"] / rates["rerank-3-lite"]
    out += ["Primary contrast of the decision rule (written after this run's data were collected, see `METHOD.md`; stock against rerank-3-lite pool 100, dream callers):", "",
            table(["Workload", "Paired calls", "Clusters", "nDCG change", "95% interval"],
                  [[c["caller"], str(c["paired_calls"]), str(c["ndcg"]["clusters"]), f"{c['ndcg']['delta']:+.3f}", ci(c["ndcg"], 3)]
                   for c in prim]), ""]

    post = []
    for left, right, what in [("rerank:rerank-3-lite:100", "rerank:rerank-3:100", "rerank-3 over rerank-3-lite, pool 100"),
                              ("rerank:rerank-3-lite:200", "rerank:rerank-3:200", "rerank-3 over rerank-3-lite, pool 200"),
                              ("rerank:rerank-3-lite:100", "rerank:rerank-3-lite:200", "pool 200 over pool 100 (rerank-3-lite)"),
                              ("rerank:rerank-3-lite:100", "rerank:rerank-3-lite:50", "pool 50 against pool 100 (rerank-3-lite)")]:
        c = next(c for c in d["comparisons"] if c["caller"] == "overall" and c["baseline"] == left and c["arm"] == right)
        post.append([what, str(c["paired_calls"]), f"{c['ndcg']['delta']:+.3f}", ci(c["ndcg"], 3)])
    out += ["Post-hoc descriptive contrasts, all callers pooled (37 clusters; computed for this write-up, not part of the registered rule; "
            "matched non-fallback calls only):", "",
            table(["Contrast", "Paired calls", "nDCG change", "95% interval"], post), "",
            "The practical-gain threshold in the decision rule is +0.03. Adding a reranker clears it by a wide margin; moving between "
            f"rerank-3 and rerank-3-lite, or between pool sizes, does not, and rerank-3 costs {ratio:.1f} times as much per token "
            "(see `costs.json`).", ""]

    j = d["per_judge_overall"]
    out += ["Sensitivity to the judge (overall nDCG at `top_k`, calls graded by that judge only; one judge's grades, not the majority):", "",
            table(["Judge", "stock", "scan", "lite 100", "r3 100", "lite 200"],
                  [[name] + [f"{j[name][a]['ndcg']:.3f}" for a in ("stock", "scan", "rerank:rerank-3-lite:100",
                                                                   "rerank:rerank-3:100", "rerank:rerank-3-lite:200")]
                   for name in ("haiku", "luna", "deepseek", "majority")]), ""]
    ag = d["judge_agreement"]
    out += ["Agreement between judges on the same pairs (linear-weighted kappa; confusion tables are in the JSON):", "",
            table(["Pair", "Pairs", "Exact agreement", "Weighted kappa"],
                  [[f"{a['left']} / {a['right']}", f"{a['pairs']:,}", pct(a["exact_agreement"], 1), f"{a['weighted_cohen_kappa']:.3f}"]
                   for a in ag]), ""]
    dec = d["decision"]
    out += [f"Registered decision rule, applied to this run: **{dec['verdict']}**. {dec['note']}", ""]
    for w in dec["workloads"]:
        out.append(f"- `{w['workload']}`: {w['verdict']}" + (f" ({'; '.join(w['reasons'])})" if w["reasons"] else ""))
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------------- study 3
def study3(d: dict) -> str:
    order = [("stock", "40 (default, no iterative scan)"), ("ef100", "100"), ("ef150", "150"), ("ef200", "200"), ("ef250", "250"),
             ("ef300", "300"), ("ef400", "400"), ("relaxed200", "200, relaxed order"), ("relaxed400", "400, relaxed order")]
    arms = d["arms"]
    rows = []
    for key, label in order:
        a = arms[key]
        rows.append([label, f"{a['all']['recall']:.3f}", f"{a['dialectic_prefetch']['recall']:.3f}", f"{a['dialectic']['recall']:.3f}",
                     f"{a['dream.deduction']['recall']:.3f}", f"{a['dream.induction']['recall']:.3f}",
                     pct(a["all"]["identical_to_exact"], 1), pct(a["all"]["underfilled"], 1),
                     f"{a['all']['db_ms_median']:.0f} / {a['all']['db_ms_p95']:.0f}"])
    n = arms["stock"]["all"]
    old = d["earlier_smaller_run"]
    size_rows = [[band, str(v["searches"]), f"{v['stock']:.3f}", f"{v['strict_order_ef200']:.3f}",
                  f"{v['strict_order_ef400']:.3f}" if "strict_order_ef400" in v else "n/a"]
                 for band, v in ((b, old["recall_by_pair_size"][b]) for b in ("<100", "100-1k", "1k-10k", ">=10k"))]
    out = ["## Study 3: what the vector index misses (no reranking, no judges)", "",
           f"Source: `study3_ef_sweep.json`. {n['searches']:,} real searches replayed at different `hnsw.ef_search` values and compared with "
           f"an exact nearest-neighbour search on the same data. Recall is the share of the exact top-`k` that the arm returned; searches "
           f"whose exact result was empty ({n['searches'] - n['searches_with_exact_result']:,} of them) are left out of recall and "
           "identical-to-exact. Unless noted, the scan mode is strict order. Database time was recorded while arms ran side by side on "
           "one machine, so use it for orders of magnitude, not for small differences. Values are rounded to three places from unrounded "
           "recomputation, so a few differ in the last digit from earlier write-ups.", "",
           table(["ef_search", "Recall", "Prefetch", "Dialectic", "Dream deduction", "Dream induction", "Identical to exact",
                  "Under-filled", "DB ms median / p95"], rows), "",
           f"Under-filled means fewer rows than `top_k`. The exact search itself under-filled {pct(d['exact_underfilled'], 1)} of the time "
           "(fewer matching rows exist, or a distance limit applies), so only the gap to that figure is the index's doing: "
           f"{pct(n['underfilled'], 1)} at the default against {pct(arms['ef250']['all']['underfilled'], 1)} with iterative scan at 250.", "",
           "Recall by size of the peer pair, from an earlier, smaller replay of "
           f"{old['searches']:,} searches (hand-transcribed; {old['note'].split('. ')[1].lower() if '. ' in old['note'] else ''}):", "",
           table(["Conclusions in the pair", "Searches", "Default settings", "Scan, ef_search 200", "Scan, ef_search 400"], size_rows), "",
           "In that earlier run the default settings under-filled "
           f"{pct(old['fill_and_underfill']['stock']['underfilled'], 1)} of searches against "
           f"{pct(old['fill_and_underfill']['strict_order_ef200']['underfilled'], 1)} with scan; the exact-search floor was not recorded there, "
           "so that run overstates what the index alone causes. " + d["scan_modes"], ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------------- study 4
def study4(d: dict) -> str:
    dz = dict(d["design"], distinct_questions=next(iter(d["answers_vs_baseline"].values()))["distinct_questions"])
    arms = d["arms"]
    rows = []
    for a in arms:
        r, c, w, e = d["retrieval"][a], d["new_conclusions"][a], d["answers_vs_baseline"].get(a), d["dream_effort"][a]
        rows.append([a, str(r["pairs"]), f"{r['mean_grade']:.2f}", f"{r['share_2_by_all']:.3f}", f"{c['mean_grade']:.2f} (n={c['n']})",
                     "baseline" if w is None else f"{w['wins']} / {w['ties']} / {w['losses']}",
                     str(e["search_calls"]), f"{e['input_tokens']:,}", str(e["create_calls"])])
    cand = [d["answers_vs_baseline"][a] for a in arms if a in d["answers_vs_baseline"]]
    wins, losses = [c["wins"] for c in cand], [c["losses"] for c in cand]
    rate = [c["win_rate_ties_half"] for c in cand]
    verdicts = cand[0]["verdicts"]
    answers = (f"The answer verdicts favour every rerank arm slightly ({min(wins)} to {max(wins)} wins against {min(losses)} to "
               f"{max(losses)} losses out of {verdicts} verdicts per arm; win rate with ties as half {min(rate):.2f} to {max(rate):.2f}), which "
               f"is not distinguishable from chance: {verdicts} verdicts come from {cand[0]['distinct_questions']} questions and 2 judges, so they "
               "are not independent.")
    base_c, others = d["new_conclusions"][arms[0]], [d["new_conclusions"][a] for a in arms[1:]]
    conclusions = (f"The new-conclusion grades run the other way: baseline {base_c['mean_grade']:.2f} against "
                   f"{min(o['mean_grade'] for o in others):.2f} to {max(o['mean_grade'] for o in others):.2f} for the rerank arms, on "
                   f"{min(o['n'] for o in others + [base_c])} to {max(o['n'] for o in others + [base_c])} samples per arm.")
    out = ["## Study 4: dream-quality A/B pilot (exploratory)", "",
           f"Source: `study4_ab_dream.json`. Status: {dz['status']}. Each arm ran {dz['per_arm']}. Arms: " +
           "; ".join(f"{k} = {v}" for k, v in dz["arms"].items()) + f". Rerank arms used {dz['rerank_arms_index_settings']}. "
           f"Judges: {' and '.join(dz['judges'])}. Retrieval is a seeded sample of each arm's own memory searches, every returned "
           "conclusion graded 0-2 by both judges (`share 2` = both gave 2). Answers are each rerank arm's answer against the baseline arm's "
           "answer to the same question, shown in random X/Y order (wins / ties / losses for the rerank arm, both judges pooled).", "",
           table(["Arm", "Retrieval pairs", "Mean grade", "Share 2 by both", "New conclusions, mean grade", "Answers W / T / L",
                  "Dream search calls", "Dream input tokens", "Create calls"], rows), "",
           f"Read this table as a smoke test, not a result. One dream per pair per arm, {dz['distinct_questions']} questions, and the language "
           "model's own run-to-run variation are all larger than any difference shown. " + answers + " " + conclusions +
           " Retrieval grades are higher for the rerank arms, consistent with Studies 1 and 2, but they grade different searches, "
           "because each arm's dream chose its own queries.", ""]
    return "\n".join(out)


# ---------------------------------------------------------------------------------------------------------- demo
def demo() -> str:
    path = RESULTS.parent / "data" / "synthetic" / "expected-headline.json"
    h = json.loads(path.read_text(encoding="utf-8"))
    rows = [[a, f"{v:.4f}", pct(h["overall_fallback_rate"][a], 1)] for a, v in h["overall_ndcg_at_k"].items()]
    c = h["primary_contrast_overall"]
    return "\n".join([
        "## Synthetic demo (invented data; not behind any number above)", "",
        "Pinned output of `bench/scripts/run_synthetic_demo.py` on `bench/data/synthetic`, from `expected-headline.json`. The data and "
        "the word-overlap reranker are invented and built so reranking helps; this shows that the pipeline runs and is deterministic, "
        "nothing about real reranking.", "",
        table(["Arm", "nDCG at top_k (3 fake judges, majority)", "Fallback rate"], rows), "",
        f"Stock against `rerank:lexical:100`, all callers: {c['delta']:+.4f} nDCG (95% interval {c['ci95'][0]:+.4f} to {c['ci95'][1]:+.4f}, "
        f"{c['clusters']} clusters). Index model: recall of the default settings against exact {h['stock_recall_vs_exact']:.3f}, with scan "
        f"{h['scan_recall_vs_exact']:.3f}. Decision rule: **{h['decision']}** (guardrail evidence missing, fallback rate above 5%).", ""])


def render() -> str:
    parts = [
        "# Results", "",
        "Aggregates only: counts, means, intervals, latencies and costs. No query, conclusion, peer or workspace text, no ids, no paths. "
        "One deployment, model judges; read the limits in `bench/README.md` before relying on any number. "
        "Every number below is printed from a JSON file in this folder by `bench/scripts/render_results.py`; `PROVENANCE.md` says which "
        "private run each file came from.", "",
        study1(load("study1_conclusion_replay.json")), study2(load("study2_three_judge_replay.json")),
        study3(load("study3_ef_sweep.json")), study4(load("study4_ab_dream.json")), demo()]
    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true")
    args = p.parse_args(argv)
    text = render()
    target = RESULTS / "RESULTS.md"
    if args.check:
        current = target.read_text(encoding="utf-8").replace("\r\n", "\n") if target.exists() else ""
        if current != text:
            print("RESULTS.md is out of date", file=sys.stderr)
            return 1
        return 0
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
