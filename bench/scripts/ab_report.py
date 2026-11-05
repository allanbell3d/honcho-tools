"""Aggregate the A/B dream-quality judgments per arm (Study 4).

Reads, from ``--judged-dir``: ``retrieval.jsonl`` and ``conclusions.jsonl`` (rows ``{key, judge, grade}``, key =
``ARM|...``) and ``answers.jsonl`` (rows ``{arm, index, judge, winner}`` where winner is the arm, the baseline or
``tie``). Optionally ``--runs-dir`` with one folder per arm and part (``<ARM>-<part>/events.jsonl``) for dream effort.

Definitions
* retrieval: a pair counts only if every judge graded it. Mean grade is the mean of the judges' grades; "share 2 by
  all" is the share where every judge gave 2.
* conclusions: mean over judges of the 0-2 durable-value grade, per arm.
* answers: wins / ties / losses of each arm against the baseline, pooled over judges and questions. The verdict
  count is judge-question pairs, not independent questions; the report also gives the number of distinct questions.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchlib import read_jsonl  # noqa: E402


def retrieval_table(rows: list[dict], arms: list[str], n_judges: int) -> dict:
    grades: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for r in rows:
        grades[r["key"]][r["judge"]] = r["grade"]
    out = {}
    for arm in arms:
        sel = [g for k, g in grades.items() if k.split("|")[0] == arm and len(g) == n_judges]
        if not sel:
            continue
        out[arm] = dict(
            pairs=len(sel),
            mean_grade=sum(sum(g.values()) / n_judges for g in sel) / len(sel),
            share_2_by_all=sum(all(v == 2 for v in g.values()) for g in sel) / len(sel),
            share_at_least_1_by_all=sum(all(v >= 1 for v in g.values()) for g in sel) / len(sel))
    return out


def conclusion_table(rows: list[dict], arms: list[str], n_judges: int) -> dict:
    grades: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for r in rows:
        grades[r["key"]][r["judge"]] = r["grade"]
    out = {}
    for arm in arms:
        sel = [g for k, g in grades.items() if k.split("|")[0] == arm and len(g) == n_judges]
        if sel:
            out[arm] = dict(n=len(sel), mean_grade=sum(sum(g.values()) / n_judges for g in sel) / len(sel))
    return out


def answer_table(rows: list[dict], arms: list[str], baseline: str) -> dict:
    out = {}
    for arm in arms:
        if arm == baseline:
            continue
        sel = [r for r in rows if r["arm"] == arm]
        c = collections.Counter("win" if r["winner"] == arm else "loss" if r["winner"] == baseline else "tie" for r in sel)
        per_judge = {j: dict(collections.Counter(
            "win" if r["winner"] == arm else "loss" if r["winner"] == baseline else "tie" for r in sel if r["judge"] == j))
            for j in sorted({r["judge"] for r in sel})}
        out[arm] = dict(wins=c["win"], losses=c["loss"], ties=c["tie"], verdicts=len(sel),
                        distinct_questions=len({r["index"] for r in sel}), per_judge=per_judge,
                        win_rate_ties_half=(c["win"] + 0.5 * c["tie"]) / len(sel) if sel else None)
    return out


def dream_effort(runs_dir: Path, arms: list[str], parts: list[str]) -> dict:
    out = {}
    for arm in arms:
        searches = tokens = creates = 0
        for part in parts:
            events = read_jsonl(runs_dir / f"{arm}-{part}" / "events.jsonl")
            searches += sum(e["type"] == "agent.tool.call.completed" and e["data"].get("tool_name") == "search_memory" for e in events)
            tokens += sum(e["data"].get("total_input_tokens", 0) for e in events if e["type"] == "dream.run")
            creates += sum(e["type"] == "agent.tool.call.completed" and str(e["data"].get("tool_name", "")).startswith("create_observations")
                           for e in events)
        out[arm] = dict(search_calls=searches, input_tokens=tokens, create_calls=creates)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--judged-dir", required=True)
    p.add_argument("--arms", nargs="+", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--judges", type=int, default=2)
    p.add_argument("--runs-dir")
    p.add_argument("--parts", nargs="+", default=["dream-1", "dream-2"])
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    d = Path(args.judged_dir)
    report = dict(
        arms=args.arms, baseline=args.baseline,
        retrieval=retrieval_table(read_jsonl(d / "retrieval.jsonl"), args.arms, args.judges),
        new_conclusions=conclusion_table(read_jsonl(d / "conclusions.jsonl"), args.arms, args.judges),
        answers_vs_baseline=answer_table(read_jsonl(d / "answers.jsonl"), args.arms, args.baseline))
    if args.runs_dir:
        report["dream_effort"] = dream_effort(Path(args.runs_dir), args.arms, args.parts)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["answers_vs_baseline"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
