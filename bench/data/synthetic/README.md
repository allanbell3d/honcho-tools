# Synthetic demo data

Everything in this folder is invented. The peers (`peer-A`, `peer-B`, `peer-C`), the workspace (`workspace-1`), the
conclusions and the queries are generated from a fixed seed by `bench/scripts/make_synthetic.py`. It is **not** the data
behind any number in `bench/results`, and nothing here comes from a real person or deployment.

It exists so that anyone can run the whole pipeline offline, end to end, and get the same result every time:

```bash
python bench/scripts/run_synthetic_demo.py --out-dir /tmp/bench-demo      # prints the headline numbers
python bench/scripts/make_synthetic.py --out-dir /tmp/synthetic-again     # regenerates these four files byte for byte
```

| File | Content |
|---|---|
| `corpus.jsonl` | 570 conclusions in three peer pairs (360, 150 and 60), each with a hobby `topic`, a `facet`, a `level` and a 16-dimensional embedding. |
| `calls.jsonl` | 90 search calls in four caller types (30 deduction, 30 induction, 15 dialectic, 15 prefetch) with `top_k`, a dream run id or a session id. |
| `embeddings.jsonl` | The cached query vectors, keyed like the real runs (sha256 of model, dimensions and query). |
| `relevance.jsonl` | The ground truth: for each call, the conclusions of its peer pair graded 2 (same topic and facet) or 1 (same topic, other facet). Every pair not listed is grade 0. |
| `expected-headline.json` | The numbers the demo must reproduce; a test compares them. |

How the demo behaves is built in, not discovered:

* Embeddings are topic centroid plus a weak facet offset plus noise, so vector search finds the right topic and mixes up facets,
  while the query text names its facet. The offline reranker (`retrieval.LexicalReranker`, word overlap) can use that, so
  reranking helps. A real reranker on real memory is a different thing; see `bench/results` for those numbers.
* The in-memory index model (`retrieval.MemoryBackend`) scans the whole table in distance order and filters by peer pair afterwards.
  The small peer pair therefore loses rows at the default settings, which iterative scan repairs. That mimics the effect a post-filtered
  HNSW index has; it is not a model of graph search.
* The demo makes about 6% of the rerank calls fail on purpose (`--fail-fraction`), to show the fail-open path, the fallback rate and
  the blended and non-fallback scores.
* Three fake judges return the ground truth, flipped by one grade with probability 5%, 10% and 15%.
