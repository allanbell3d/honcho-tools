"""End-to-end offline demo on the synthetic dataset: replay -> judge -> report. No network, no database, no keys.

    python bench/scripts/run_synthetic_demo.py --out-dir /tmp/bench-demo

The numbers it prints are deterministic and come from invented data. They show that the pipeline runs and that its
arithmetic is stable; they say nothing about how reranking behaves on a real deployment (see bench/results for that).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ef_sweep  # noqa: E402
import judge  # noqa: E402
import report  # noqa: E402
import run_arms  # noqa: E402
from benchlib import read_jsonl  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data" / "synthetic"
ARMS = ["stock", "scan", "exact", "rerank:lexical:50", "rerank:lexical:100", "rerank:lexical:200"]
BASELINE, CANDIDATE = "stock", "rerank:lexical:100"


def run_demo(out_dir: Path, data_dir: Path = DATA, fail_fraction: float = 0.06) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    rankings = out_dir / "rankings.jsonl"
    for stale in [rankings, *out_dir.glob("judgments-*.jsonl"), *out_dir.glob("judge-attempts-*.jsonl")]:
        if stale.exists():
            stale.unlink()  # a demo run always starts clean
    run_arms.main(["--calls", str(data_dir / "calls.jsonl"), "--embeddings", str(data_dir / "embeddings.jsonl"),
                   "--backend", "memory", "--corpus", str(data_dir / "corpus.jsonl"), "--reranker", "lexical",
                   "--fail-fraction", str(fail_fraction), "--seed", "1", "--arms", *ARMS, "--out", str(rankings)])
    judge.main(["--calls", str(data_dir / "calls.jsonl"), "--rankings", str(rankings), "--out-dir", str(out_dir),
                "--offline", "--truth", str(data_dir / "relevance.jsonl"), "--corpus", str(data_dir / "corpus.jsonl")])
    judgments = sorted(out_dir.glob("judgments-*.jsonl"))
    rep = report.build_report(
        read_jsonl(data_dir / "calls.jsonl"), read_jsonl(rankings),
        {p.stem.removeprefix("judgments-"): read_jsonl(p) for p in judgments},
        baseline=BASELINE, candidate=CANDIDATE, vector_prefix=True)
    (out_dir / "report.json").write_text(json.dumps(rep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "REPORT.md").write_text(report.render_markdown(rep), encoding="utf-8")
    exact = [r for r in read_jsonl(rankings) if r["arm"] == "exact"]
    sweep = ef_sweep.sweep(read_jsonl(data_dir / "calls.jsonl"), exact,
                           {a: [r for r in read_jsonl(rankings) if r["arm"] == a] for a in ("stock", "scan")})
    (out_dir / "ef-sweep.json").write_text(json.dumps(sweep, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return headline(rep, sweep)


def headline(rep: dict, sweep: dict) -> dict:
    """The few numbers the tests pin: stable across platforms, rounded."""
    src = rep["sources"][rep["primary_source"]]
    ndcg = {m["arm"]: round(m["ndcg"], 4) for m in src["metrics"]
            if m["caller"] == "overall" and m["threshold"] == 2 and m["k"] == "caller_k"}
    fallback = {m["arm"]: round(m["fallback_rate"], 4) for m in src["metrics"]
                if m["caller"] == "overall" and m["threshold"] == 2 and m["k"] == "caller_k"}
    contrast = next(c for c in src["comparisons"] if c["caller"] == "overall" and c["arm"] == CANDIDATE)
    return dict(
        complete_calls=src["coverage"]["complete_calls"],
        overall_ndcg_at_k=ndcg, overall_fallback_rate=fallback,
        primary_contrast_overall=dict(delta=round(contrast["ndcg"]["delta"], 4),
                                      ci95=[round(x, 4) for x in contrast["ndcg"]["ci95"]],
                                      clusters=contrast["ndcg"]["clusters"]),
        decision=rep["decision"]["verdict"],
        stock_recall_vs_exact=round(sweep["arms"]["stock"]["all"]["recall"], 4),
        scan_recall_vs_exact=round(sweep["arms"]["scan"]["all"]["recall"], 4),
        stock_underfilled=round(sweep["arms"]["stock"]["all"]["underfilled"], 4),
        exact_underfilled=round(sweep["exact_underfilled"], 4),
        judge_kappa=[round(a["weighted_cohen_kappa"], 3) for a in rep["judge_agreement"]],
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--data-dir", type=Path, default=DATA)
    args = p.parse_args(argv)
    print(json.dumps(run_demo(args.out_dir, args.data_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
