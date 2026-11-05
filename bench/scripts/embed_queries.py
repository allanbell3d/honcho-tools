"""Embed each distinct query once and cache the vector, so every arm of a run searches with the same vector.

The vector must come from the model your Honcho deployment uses to embed conclusions, with the same dimensions,
otherwise distances are meaningless. Any OpenAI-compatible ``/embeddings`` endpoint works.

    export EMBED_API_KEY='...'       # the variable name is configurable (--key-env)
    python bench/scripts/embed_queries.py --calls calls.jsonl --out embeddings.jsonl \
        --model MODEL --dims 1536 --base-url https://api.openai.com/v1 --usd-per-million 0.02 \
        --ledger ledger.jsonl --cap-usd 5

Cached rows are keyed by sha256 of [model, dims, query]; a rerun embeds only what is missing. A failed request is not
retried automatically. ``--dry-run`` prints the number of queries to embed and an upper-bound token estimate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchlib import append_jsonl, read_jsonl  # noqa: E402
from ledger import Ledger  # noqa: E402
from retrieval import embedding_key  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--calls", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--dims", type=int, required=True)
    p.add_argument("--base-url", default="https://api.openai.com/v1")
    p.add_argument("--key-env", default="EMBED_API_KEY")
    p.add_argument("--usd-per-million", type=float, required=True, help="list price per million input tokens")
    p.add_argument("--ledger")
    p.add_argument("--cap-usd", type=float, default=5.0)
    p.add_argument("--batch", type=int, default=100)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    cached = {r["cache_key"] for r in read_jsonl(args.out)}
    queries = sorted({c["query"] for c in read_jsonl(args.calls)
                      if embedding_key(args.model, args.dims, c["query"]) not in cached})
    bound = sum(len(q.encode("utf-8")) for q in queries)  # bytes >= tokens
    if args.dry_run:
        print(json.dumps(dict(queries_to_embed=len(queries), tokens_upper_bound=bound,
                              usd_upper_bound=round(bound * args.usd_per_million / 1e6, 6))))
        return 0
    key = os.environ.get(args.key_env)
    if not key:
        raise SystemExit(f"environment variable {args.key_env} is not set")
    if not args.ledger:
        raise SystemExit("--ledger is required for a paid run")
    ledger = Ledger(args.ledger, args.cap_usd)
    for i in range(0, len(queries), args.batch):
        batch = queries[i:i + args.batch]
        worst = sum(len(q.encode("utf-8")) for q in batch)
        reservation = ledger.reserve("embedding", args.model, worst * args.usd_per_million / 1e6, tokens=worst)
        request = urllib.request.Request(
            args.base_url.rstrip("/") + "/embeddings", method="POST",
            data=json.dumps({"model": args.model, "input": batch, "dimensions": args.dims}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                payload = json.loads(response.read())
            data = sorted(payload["data"], key=lambda d: d["index"])
            if len(data) != len(batch) or any(len(d["embedding"]) != args.dims for d in data):
                raise ValueError("unexpected embedding shape")
            used = int(payload["usage"]["total_tokens"])
        except BaseException:
            reservation.complete(outcome="request_failed")
            raise SystemExit("embedding request failed; nothing was retried")
        reservation.complete(tokens=used, cost_estimate=min(used, worst) * args.usd_per_million / 1e6)
        for query, row in zip(batch, data):
            append_jsonl(args.out, dict(cache_key=embedding_key(args.model, args.dims, query),
                                        embedding=row["embedding"], model=args.model))
    print(json.dumps(dict(embedded=len(queries), committed_usd=float(ledger.committed))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
