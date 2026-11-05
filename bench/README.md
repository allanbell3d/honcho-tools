# Rerank benchmark

I wanted to know what a reranking step in front of Honcho's conclusion search changes in accuracy, latency and cost, and how
much of the problem is the vector index itself. This folder is the harness I used, a synthetic dataset so you can run it
offline, and the aggregate results I got on one real deployment. It is meant to be audited and re-run on your own data.

Nothing here contains real memory data. The numbers are aggregates (counts, means, intervals, latencies, costs); the harness
reads your telemetry and database and writes to your disk only. [`METHOD.md`](METHOD.md) is the full audit trail,
[`results/RESULTS.md`](results/RESULTS.md) the tables.

> **Read the limits first.** One deployment, two main peers, late October to early November 2026. The relevance labels
> come from language models, not from people. Dialectic and prefetch searches were replayed without their session scope. The
> decision rule was written after the first data. The A/B dream test is a small pilot. None of this says whether answers or dreams
> got better, only which conclusions a search returns.

## What was measured

| Study | Question | Data | Judges |
|---|---|---|---|
| 1. Conclusion replay | How much does reranking (rerank-3-lite, pool 100 to 400) change which conclusions a search returns, against the best plain vector setting? What do larger pools buy? | 80 frozen real calls, 20 per caller type; all 31,663 candidates any arm returned were judged | one model family, then adjudicated against the source text |
| 2. Three-judge replay | The same question with rerank-3 as well as rerank-3-lite, pools 50 to 200, on a larger earlier run. | 270 real calls, 11,908 judged pairs | three model judges, majority grade |
| 3. Index recall | How many of the true nearest conclusions does pgvector's HNSW index return at `ef_search` 40 (the default) to 400, with and without iterative scan? | 6,463 real searches against an exact search | none (exact search is the truth) |
| 4. Dream A/B pilot | Does a dream that searches through the reranker write better conclusions or answer better? | one dream per peer pair per arm, five arms, 17 questions | two model judges |

The four caller types are the ones Honcho makes: the dream's deduction and induction specialists (`dream.deduction`,
`dream.induction`), the question model's own `search_memory` (`dialectic`) and Honcho's automatic search on the question text
(`dialectic_prefetch`).

## What I found (aggregates, see `results/RESULTS.md` for every table)

* **Reranking raised the share of relevant results a lot** (Study 1): strict precision at the caller's `top_k` rose from 69.58% to
  78.97% with rerank-3-lite at pool 100, +9.39 points (95% interval +5.40 to +11.93, 43 clusters); 43 calls better, 31 tied, 6
  worse. Keeping the same candidate pool but not reranking it scored 69.66%, so the gain is the reranker's, not the wider pool's.
  Study 2 found the same direction on dream searches, +0.21 and +0.25 nDCG, with a different judge set.
* **Bigger pools and the bigger model buy little** (Studies 1 and 2): pool 400 over pool 100 was +0.69 points (interval -1.20 to
  +1.98); in Study 2 rerank-3 over rerank-3-lite at pool 100 was +0.012 nDCG at 2.5 times the list price per token. Pool 100 costs
  about 0.096 USD per 1,000 calls in reranker tokens (4,783 tokens per call) and added about 520 ms median latency; pool 400 added about
  860 ms and cost 0.375 USD per 1,000 calls.
* **The vector index at its defaults misses results** (Study 3): at `ef_search` 40 with no iterative scan the index returned 90.1% of the
  exact top-`k` (76.5% for prefetch, 81.6% for dialectic); with iterative scan at 100 it returned 99.0%, at 250 99.6%, at 400 all of
  them. Part of the "returns fewer rows than asked" problem is the corpus, not the index: 34.8% of searches under-filled at the defaults
  and 22.8% with iterative scan, which equals what an exact search returned. This is independent of reranking and costs nothing.
* **The wrapper must stay fail-open.** Study 2 saw 4.8% of pool-100 calls fall back to vector order at a 3-second timeout (more on
  dialectic and prefetch), and reranking long message payloads returned HTTP errors on 30% of calls at pool 300 and 25% at pool 400
  (none at 100 and 200; cause not established).
* **No downstream effect established** (Study 4): the pilot is too small to tell, and its new-conclusion grades favour the baseline.
* **The registered decision rule says "inconclusive" for production acceptance**, because the exact-id, correction and scope
  guardrails were never measured and the dialectic workloads rest on 3 to 7 clusters. What follows from it here: rerank-3-lite at
  pool 100, off by default and failing open, is the setting to trial, with a canary and a downstream test.

## Decision rule

Written after the first data, so it is a discipline for reading results, not a preregistration. Primary contrast per workload:
the baseline against rerank-3-lite at pool 100. Everything else is descriptive; the arm with the largest observed gain is never
promoted to the primary contrast.

* Primary metric: nDCG at the caller's `top_k`, relevant = majority grade 2 (or the single label source's grade 2).
* Practical gain: at least +0.03 absolute, with the 95% paired cluster bootstrap interval above zero (seed 42, 2,000 draws; dreams
  cluster by run id, other callers by workspace, peer pair and session).
* Fewer than 10 independent clusters: inconclusive.
* The candidate's fallback rate must be under 5%. Quality excludes fallback rows and compares matched non-fallback pairs; the blended
  number that includes fallbacks is reported next to it.
* Guardrails: no regression on exact-id or correction cases, no scope leaks. **Missing evidence is "inconclusive", never a pass.**
* A practical gain alone cannot authorise a production change. If the evidence is noisy or incomplete, keep the simpler, cheaper setting.

`scripts/report.py` implements it (`decide`); `--guardrails` takes the evidence you have.

## Run the offline demo

Python 3.10 or later (tested on 3.14), standard library only for everything except the optional provider and database packages.

```bash
python bench/scripts/run_synthetic_demo.py --out-dir /tmp/bench-demo
python -m pytest bench/tests -q          # also part of the repository's own `python -m pytest`
```

The demo reads the committed fictional dataset in `bench/data/synthetic` (see its README), replays 90
fictional searches through six arms with an in-memory index model and a word-overlap reranker, judges the pools with three
fake judges, and writes rankings, judgments, a report and a recall table to `--out-dir`. The result is the same every run.
It is a smoke test of the machinery, not evidence about reranking.

## Run it on your own Honcho data

Everything is configured by arguments and environment variables; no host, path, name or key is built in. Keep the calls and rankings
private: they contain your queries and your memory text.

1. **Harvest real searches.** `harvest.py --source <telemetry folder> --out-dir <private folder>` reads Honcho's
   `events-*.jsonl` and writes `calls.jsonl`, a stratified `sample.jsonl` and a summary. Needs Honcho's telemetry on (3.5.0 event
   names). Dialectic and prefetch calls are flagged `scope_approximated`.
2. **Embed the queries once.** `embed_queries.py --calls <sample> --out <embeddings> --model <your embedding model> --dims <its
   dimensions> --usd-per-million <price> --ledger <ledger> --dry-run` first; then without `--dry-run`, with the key in the variable
   named by `--key-env`. The vector must come from the model Honcho uses for conclusions.
3. **Replay through the arms.** Run against a **copy** of your database, read-only:
   `BENCH_DATABASE_URL=... RERANK_API_KEY=... run_arms.py --calls <sample> --embeddings <embeddings> --backend pg --arms stock
   scan:250:strict_order exact rerank:rerank-3-lite:100:250:strict_order --out rankings.jsonl --rates rates.json --ledger
   ledger.jsonl --cap-usd 5`. Start with `--dry-run`. Arms: `stock`, `scan[:EF[:MODE]]`, `exact`, `rerank:MODEL:POOL[:EF[:MODE]]`.
   Needs `psycopg`. Calls run one at a time on purpose (concurrent arms distort latency). The Postgres backend re-implements the
   search for the filters the study's calls used; if you need exact parity with Honcho's own `query_documents`, swap that in
   (`retrieval.PgBackend` is the one place to change).
4. **Judge the pools.** `judge.py --calls <sample> --rankings rankings.jsonl --out-dir judged --rates judge-rates.json --ledger ledger.jsonl
   --dry-run` prints a worst-case cost; then run it without `--dry-run` with the judges' keys in their variables (defaults in
   `judge.DEFAULT_JUDGES`, override with `--judges-file`). Judges see the query, the caller type and the conclusion only, in
   batches of 25 with local labels. Grade a sample by hand too: `write_spotcheck` produces 40 stratified pairs.
5. **Score.** `report.py --calls <sample> --rankings rankings.jsonl --judgments judged/judgments-*.jsonl --baseline stock --candidate
   rerank:rerank-3-lite:100:250:strict_order --vector-prefix-control --rates rates.json --out report.json --markdown REPORT.md`.
6. **Index recall only (no judges, no paid calls but the query embeddings):** run `stock`, `exact` and a few `scan:EF` arms, then
   `ef_sweep.py --calls <sample> --exact exact=rankings-exact.jsonl --arm stock=... --arm ef250=... --out ef-sweep.json`.

`slices.py` cuts a sample into stratified pieces so you can pay for and judge one slice, read the report, and decide whether to
continue. `extract_published.py` shows how the private reports were reduced to the files in `results/`, and its `check` command
scans a file for paths, addresses, keys and hex ids before you publish anything.

## What a run costs

All amounts are the harness's own estimates at the list prices it recorded; they are not invoices and prices change. A
failed request is charged at its full reservation. See `results/costs.json`.

* **Reranker:** about 48 tokens per candidate (the query is counted for every document), so tokens per call = 48 x pool. At the
  recorded 0.02 USD per million tokens for rerank-3-lite: 0.096 USD per 1,000 calls at pool 100, 0.190 at 200, 0.375 at 400. rerank-3
  at 0.05 was 2.5 times that. In production, 1,000 reranked searches at pool 100 cost about a tenth of a US dollar.
* **Query embeddings:** negligible, about 0.002 USD for 6,000 queries.
* **Judges** (Study 2: 11,908 pairs per judge, about 44 pairs per call because the pool is the union of eight arms): Haiku 0.60 USD,
  Luna 0.41 USD, DeepSeek 3.20 USD for all three runs of 541, 550 and 514 requests. The whole of Study 2, with 2,000 reranker requests,
  came to 4.73 USD in the ledger. A replay of 100 calls with the same arms and three judges is therefore about 1.7 USD, dominated by the
  most expensive judge; one cheap judge on 100 calls is about 0.15 to 0.25 USD.
* **Study 1's 31,663 labels** came from model-harness grading sessions that the spend ledger did not meter; budget that part yourself.
* **Study 4:** about 2.1 USD for five arms, two dreams each, with two judges (0.27 USD of it judging).
* Every paid step reserves its worst case in `ledger.py` before sending, stops when the cap would be crossed, and never retries a
  failed paid request on its own. A real run needs `--ledger` and `--cap-usd`.

## Limits and threats to validity

* **One deployment.** Peer count, memory size, query style and language are specific to it. The small caller strata
  (dialectic 20 calls in 5 clusters, prefetch 20 in 7; Study 2: 3 and 4) are descriptive only, and the dream strata carry the
  statistical weight.
* **Model judges, not people.** In Study 1 a single model family graded and adjudicated, and may share its own mistakes;
  before adjudication fresh reviewers agreed on 92 of 120 sampled contexts, mostly differing at the 1/2 boundary. In Study 2 the
  three judges agreed with weighted kappa 0.38 to 0.58, and the headline nDCG moves with the judge (see the sensitivity table).
  Two of the judges' providers also wrote some of the queries (self-preference is possible). The 40-pair human spot-check was
  never graded. There is no human gold set.
* **Relevance is relative to the judged pool**, not the corpus: recall and the ideal ranking only know about candidates some arm
  returned. A conclusion nobody retrieved is invisible.
* **Scope was approximated for 40 of the 80 Study 1 calls** (dialectic and prefetch): their session scope was not recorded, so
  they ran peer-wide. Session-context searches were left out entirely.
* **Frozen replay, not production.** It measures which conclusions come back, not whether a dream or an answer got better; the
  Study 4 pilot is too small to say. Exact-id, correction and scope guardrails were never measured, so production acceptance is
  inconclusive by the rule above.
* **Arms ran on different database copies** in Study 1, without fingerprinting that the corpora were identical; the same-pool
  control (vector order of the rerank arm's own pool against its reranked order) is the cleaner causal check. Latency was
  measured in sequential runs at different times, not interleaved, and the Study 3 times were taken with arms side by side on one
  machine. Treat latency as orders of magnitude. Study 2's rerank timings are not reported because several calls ran at once.
* **The decision rule was written after the data.** Intervals are conditional on the fixed labels; they do not include label error.
* **Fallbacks are real.** At the 3-second timeout a few percent of reranked searches fell back, many more on dialectic and
  prefetch with large pools. Quality is reported without them and blended with them; read both.
* **Prices and models change.** Model names are the ones the study used; rates are as recorded. The Honcho version was 3.4.0 for
  Study 2 and 3.5.0 for the rest.
* **The harness here is a cleaned-up version** of the one that produced the numbers, not the same files. The scoring code was
  checked by rerunning it on the original private inputs: it reproduced Study 2's published tables and Study 3's recall table (see
  `METHOD.md`, "How the published scripts were checked"). The paid-provider request shapes, the PostgreSQL backend and the A/B driver
  are not exercised by the offline tests.

## Layout

```
bench/
  README.md  METHOD.md
  results/     RESULTS.md (generated), study*.json, costs.json, PROVENANCE.md, CHECKSUMS.sha256
  scripts/     benchlib.py ledger.py harvest.py slices.py embed_queries.py retrieval.py run_arms.py judge.py
               report.py ef_sweep.py ab_judge.py ab_report.py extract_published.py render_results.py
               make_synthetic.py run_synthetic_demo.py  *.example.json
  data/synthetic/   the fictional dataset, its README and the pinned demo result
  tests/       offline pytest suite (also collected by the repository's root `python -m pytest`)
```

The reranker itself, the wrapper that was measured, is `src/rerank/`; its settings are documented in `src/rerank/README.md`.
