"""Replay a set of search calls through one or more arms and write ``rankings.jsonl`` (resumable).

Offline demo (no network, no database)::

    python bench/scripts/run_arms.py --calls bench/data/synthetic/calls.jsonl \
        --embeddings bench/data/synthetic/embeddings.jsonl --backend memory --corpus bench/data/synthetic/corpus.jsonl \
        --reranker lexical --arms stock scan exact rerank:lexical:50 rerank:lexical:100 --out /tmp/rankings.jsonl

Against a Honcho database (read-only session)::

    export BENCH_DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DBNAME'   # the variable name is configurable
    export RERANK_API_KEY='...'                                                  # the variable name is configurable
    python bench/scripts/run_arms.py --calls calls.jsonl --embeddings embeddings.jsonl --backend pg \
        --arms stock scan:250:strict_order rerank:rerank-3-lite:100:250:strict_order --out rankings.jsonl \
        --rates rates.json --ledger ledger.jsonl --cap-usd 5

Calls run one at a time, arm after arm, on purpose: concurrent arms contend for the same database and inflate the
latency numbers. Add ``--dry-run`` to print the plan and a cost estimate without any request.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchlib import append_jsonl, read_jsonl  # noqa: E402
from ledger import BudgetExceeded, Ledger  # noqa: E402
from retrieval import (  # noqa: E402
    Arm, FlakyReranker, LexicalReranker, MemoryBackend, PgBackend, RerankError, RerankResult, VoyageReranker,
    embedding_key, load_embeddings, parse_arm, parse_rerank_payload, run_arm, token_bound,
)

TOKENS_PER_CANDIDATE = 48  # measured: Voyage billed ~47-48 tokens per candidate (query counted per document)


class PaidReranker:
    """Reserves the worst case in the ledger before each request and settles it from the reported usage."""

    def __init__(self, inner, ledger: Ledger, provider: str, model: str, usd_per_million: float) -> None:  # noqa: ANN001
        self.inner, self.ledger, self.provider, self.model, self.rate = inner, ledger, provider, model, usd_per_million

    def rerank(self, query: str, documents: list[str]) -> RerankResult:
        bound = token_bound(query, documents)
        reservation = self.ledger.reserve(self.provider, self.model, bound * self.rate / 1e6, tokens=bound)
        try:
            result = self.inner.rerank(query, documents)
        except RerankError:
            reservation.complete(outcome="request_failed")  # full reservation kept; never retried
            raise
        except BaseException:
            reservation.complete(outcome="request_failed")
            raise
        used = result.tokens if result.tokens is not None else bound
        reservation.complete(tokens=used, cost_estimate=min(used, bound) * self.rate / 1e6)
        return result


def ranking_identity(row: dict) -> tuple[str, str]:
    return row["call_id"], row["arm"]


def load_rates(path: str | None) -> dict[str, float]:
    """{model: USD per million tokens}. You supply it; prices change, so none is built in."""
    if not path:
        return {}
    return {k: float(v) for k, v in json.loads(Path(path).read_text(encoding="utf-8")).items() if not k.startswith("_")}


def plan(calls: list[dict], arms: list[Arm], rates: dict[str, float], tokens_per_candidate: int) -> dict:
    """Offline cost estimate: candidates x tokens per candidate x list price, per reranker model."""
    out: dict[str, dict] = {}
    for arm in arms:
        if arm.kind != "rerank":
            continue
        entry = out.setdefault(arm.name, dict(model=arm.model, requests=0, estimated_tokens=0, estimated_usd=None))
        entry["requests"] += len(calls)
        entry["estimated_tokens"] += sum(max(c["top_k"], arm.pool or 0) for c in calls) * tokens_per_candidate
        if arm.model in rates:
            entry["estimated_usd"] = round(entry["estimated_tokens"] * rates[arm.model] / 1e6, 6)
    return dict(dry_run=True, calls=len(calls), arms=[a.name for a in arms], rerank=out,
                note=f"estimate assumes {tokens_per_candidate} tokens per candidate; live reservations use real byte bounds")


def build_backend(args: argparse.Namespace):
    if args.backend == "memory":
        return MemoryBackend.from_file(args.corpus)
    dsn = os.environ.get(args.db_url_env)
    if not dsn:
        raise SystemExit(f"environment variable {args.db_url_env} is not set")
    return PgBackend(dsn)


def build_reranker(args: argparse.Namespace, arm: Arm, ledger: Ledger | None, rates: dict[str, float]):
    if args.reranker == "lexical":
        inner = LexicalReranker()
        if args.fail_fraction:
            inner = FlakyReranker(inner, args.fail_fraction, seed=args.seed)
        return inner
    key = os.environ.get(args.rerank_key_env, "")
    if not key:
        raise SystemExit(f"environment variable {args.rerank_key_env} is not set")
    if arm.model not in rates or ledger is None:
        raise SystemExit("a paid reranker needs --rates (with the model's price) and --ledger")
    inner = VoyageReranker(arm.model, api_key=key, base_url=args.rerank_url, timeout_seconds=args.timeout)
    return PaidReranker(inner, ledger, "rerank", arm.model, rates[arm.model])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--calls", required=True, help="calls.jsonl (call_id, caller_type, query, top_k, workspace_name, observer, observed, ...)")
    p.add_argument("--embeddings", required=True, help="embeddings.jsonl: {cache_key, embedding}; see embed_queries.py")
    p.add_argument("--embedding-model", default="synthetic-16")
    p.add_argument("--embedding-dims", type=int, default=16)
    p.add_argument("--out", required=True, help="rankings.jsonl (appended; finished (call, arm) pairs are skipped)")
    p.add_argument("--arms", nargs="+", default=["stock", "scan", "exact"])
    p.add_argument("--min-score", type=float, help="drop reranked results below this score (rerank arms only)")
    p.add_argument("--backend", choices=["memory", "pg"], default="memory")
    p.add_argument("--corpus", help="corpus.jsonl for the memory backend")
    p.add_argument("--db-url-env", default="BENCH_DATABASE_URL", help="name of the variable holding the PostgreSQL URL")
    p.add_argument("--reranker", choices=["lexical", "voyage"], default="lexical")
    p.add_argument("--rerank-key-env", default="RERANK_API_KEY", help="name of the variable holding the reranker key")
    p.add_argument("--rerank-url", default="https://api.voyageai.com/v1")
    p.add_argument("--timeout", type=float, default=3.0, help="reranker timeout in seconds (the shipping default)")
    p.add_argument("--fail-fraction", type=float, default=0.0, help="offline only: fail this share of rerank calls")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rates", help="JSON {model: USD per million rerank tokens}")
    p.add_argument("--ledger", help="spend ledger JSONL (paid rerankers)")
    p.add_argument("--cap-usd", type=float, default=5.0)
    p.add_argument("--max-usd", type=float, help="stop this run after this much (reservations included)")
    p.add_argument("--tokens-per-candidate", type=int, default=TOKENS_PER_CANDIDATE)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    calls = [c for c in read_jsonl(args.calls) if c.get("replay_eligible", True)]
    arms = [parse_arm(a, args.min_score) for a in args.arms]
    rates = load_rates(args.rates)
    if args.dry_run:
        print(json.dumps(plan(calls, arms, rates, args.tokens_per_candidate), indent=2))
        return 0

    vectors = load_embeddings(args.embeddings)
    backend = build_backend(args)
    ledger = Ledger(args.ledger, args.cap_usd, args.max_usd) if args.ledger else None
    out = Path(args.out)
    done = {ranking_identity(r) for r in read_jsonl(out)}
    rerankers = {}
    written = 0
    try:
        for call in calls:
            key = embedding_key(args.embedding_model, args.embedding_dims, call["query"])
            if key not in vectors:
                raise SystemExit(f"no cached embedding for call {call['call_id']}; run embed_queries.py first")
            for arm in arms:
                if (call["call_id"], arm.name) in done:
                    continue
                if arm.kind == "rerank" and arm.name not in rerankers:
                    rerankers[arm.name] = build_reranker(args, arm, ledger, rates)
                row = run_arm(backend, rerankers.get(arm.name), call, vectors[key], arm)
                row["scope_approximated"] = bool(call.get("scope_approximated", False))
                append_jsonl(out, row)
                written += 1
    except BudgetExceeded as exc:
        print(f"stopped: {exc}; {written} rankings written, rerun to resume", file=sys.stderr)
        return 2
    print(json.dumps(dict(rankings_written=written, out=str(out),
                          committed_usd=float(ledger.committed) if ledger else 0.0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
