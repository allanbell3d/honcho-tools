"""Shared helpers for the rerank benchmark: JSONL I/O and the scoring maths.

Everything here is pure Python (standard library only) and deterministic. The metric definitions are the ones
behind every table in bench/results; see bench/METHOD.md for where each headline number is computed.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Sequence

# Grades: 0 = not relevant, 1 = related but not enough, 2 = answers the query or is directly needed.
GRADES = (0, 1, 2)


# --------------------------------------------------------------------------------------------------------------
# JSONL
# --------------------------------------------------------------------------------------------------------------
def read_jsonl(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: str | Path, rows: Iterable[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def append_jsonl(path: str | Path, row: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_seed(*parts: object) -> int:
    """A platform-independent integer seed from arbitrary parts (used for per-item noise in fakes)."""
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


# --------------------------------------------------------------------------------------------------------------
# Descriptive statistics
# --------------------------------------------------------------------------------------------------------------
def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear-interpolation percentile, p in [0, 1]."""
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def median_or_none(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def mean_or_none(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


# --------------------------------------------------------------------------------------------------------------
# Ranking metrics
# --------------------------------------------------------------------------------------------------------------
def ranking_metrics(ids: Sequence[str], grades: dict[str, int], k: int, threshold: int = 2) -> dict:
    """Precision, recall, nDCG and hit at k for one ranked list.

    ``grades`` maps every judged document id of the call (the judged pool) to 0, 1 or 2.
    ``threshold`` 2 counts only grade 2 as relevant (binary gain); threshold 1 counts grades 1 and 2 as relevant and
    uses the graded gain (0, 1, 2) in nDCG.

    * precision = relevant results in the first k / k. A list shorter than k is penalised: the missing slots count
      as misses (this is what makes the index's under-filled searches visible).
    * recall = relevant results in the first k / relevant documents in the judged pool. The pool is what some arm
      returned, not the whole corpus, so recall is relative to the pool.
    * nDCG uses gain 2**g - 1 and the ideal list built from the judged pool.
    * Every returned id in the first k must be judged; an unjudged result raises, so missing labels never count
      silently as zeros.
    """
    ids = [str(i) for i in ids]
    if any(i not in grades for i in ids[:k]):
        raise ValueError("unjudged result in the first k: finish judging before reporting")
    relevant = {key for key, value in grades.items() if value >= threshold}
    hits = sum(i in relevant for i in ids[:k])
    if threshold == 1:
        gains = [grades[i] for i in ids[:k]]
        pool = sorted(grades.values(), reverse=True)[:k]
    else:
        gains = [int(grades[i] == 2) for i in ids[:k]]
        pool = sorted((int(v == 2) for v in grades.values()), reverse=True)[:k]
    dcg = sum((2 ** g - 1) / math.log2(rank + 2) for rank, g in enumerate(gains))
    idcg = sum((2 ** g - 1) / math.log2(rank + 2) for rank, g in enumerate(pool))
    return dict(
        precision=hits / k,
        recall=hits / len(relevant) if relevant else 0.0,
        ndcg=dcg / idcg if idcg else 0.0,
        hit=int(hits > 0),
    )


def cluster_key(call: dict) -> str:
    """Independence unit for the bootstrap: a dream run for dream callers, else workspace/observer/observed/session.

    Searches inside one dream run, or about one peer pair, are not independent observations, so the bootstrap
    resamples these clusters and keeps all of a cluster's paired rows together.
    """
    if str(call.get("caller_type", "")).startswith("dream") and call.get("run_id"):
        return "run:" + str(call["run_id"])
    return "pair/session:" + repr(tuple(call.get(k) for k in ("workspace_name", "observer", "observed", "session_id")))


def paired_cluster_bootstrap(
    left: Sequence[float],
    right: Sequence[float],
    clusters: Sequence[str] | None = None,
    iterations: int = 2000,
    seed: int = 42,
) -> dict | None:
    """Percentile bootstrap of the mean paired difference (right - left), resampling whole clusters.

    Returns delta (the plain mean difference), a 95% interval, the number of clusters and whether there are fewer
    than ten (the decision rule calls that inconclusive). No p-value is computed.
    """
    if len(left) != len(right) or (clusters is not None and len(clusters) != len(left)):
        raise ValueError("paired observations and clusters must have equal lengths")
    if not left:
        return None
    grouped: dict[str, list[float]] = defaultdict(list)
    for cluster, a, b in zip(clusters if clusters is not None else range(len(left)), left, right):
        grouped[str(cluster)].append(b - a)
    keys = sorted(grouped)
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        chosen = rng.choices(keys, k=len(keys))
        means.append(statistics.fmean(v for key in chosen for v in grouped[key]))
    low, high = percentile(means, 0.025), percentile(means, 0.975)
    return dict(
        delta=statistics.fmean(b - a for a, b in zip(left, right)),
        ci95=[low, high],
        interval_includes_zero=low <= 0 <= high,
        clusters=len(keys),
        few_clusters=len(keys) < 10,
        iterations=iterations,
        seed=seed,
        method="paired cluster percentile bootstrap",
    )


# --------------------------------------------------------------------------------------------------------------
# Judges: majority and agreement
# --------------------------------------------------------------------------------------------------------------
def majority_grade(grades: Sequence[int]) -> int:
    """Median of three grades: two of three agree wins; if all three differ the middle grade 1 wins."""
    if len(grades) != 3:
        raise ValueError("majority needs exactly three grades")
    return sorted(grades)[1]


def linear_weighted_kappa(table: list[list[int]]) -> float | None:
    """Cohen's kappa with linear weights |i - j| / 2 for the 3x3 grade confusion table (rows left, columns right)."""
    n = sum(sum(row) for row in table)
    if not n:
        return None
    observed = sum(table[i][j] * abs(i - j) / 2 for i in range(3) for j in range(3)) / n
    expected = sum(
        sum(table[i]) * sum(row[j] for row in table) * abs(i - j) / 2 for i in range(3) for j in range(3)
    ) / n ** 2
    return 1 - observed / expected if expected else None


def pairwise_agreement(left: dict, right: dict) -> dict:
    """Exact agreement, weighted kappa and confusion table of two {(call_id, document_id): grade} maps."""
    common = left.keys() & right.keys()
    table = [[0] * 3 for _ in range(3)]
    for key in common:
        table[left[key]][right[key]] += 1
    n = len(common)
    return dict(
        pairs=n,
        exact_agreement=sum(table[i][i] for i in range(3)) / n if n else None,
        weighted_cohen_kappa=linear_weighted_kappa(table),
        confusion=table,
        weighting="linear: disagreement = abs(left-right)/2",
    )
