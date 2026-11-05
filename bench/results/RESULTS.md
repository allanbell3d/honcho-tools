# Results

Aggregates only: counts, means, intervals, latencies and costs. No query, conclusion, peer or workspace text, no ids, no paths. One deployment, model judges; read the limits in `bench/README.md` before relying on any number. Every number below is printed from a JSON file in this folder by `bench/scripts/render_results.py`; `PROVENANCE.md` says which private run each file came from.

## Study 1: conclusion-search replay, 80 frozen calls, every candidate judged

Source: `study1_conclusion_replay.json`. 80 real search calls (20 per caller type), 9 arms, 31,663 (call, conclusion) cases graded 0, 1 or 2 by one model family and then adjudicated against the source text; there is no human gold set. Strict precision is the share of the first `top_k` results graded 2, averaged over calls. nDCG uses grade 2 as relevant and the judged pool, not the corpus, as the ideal. Latency columns are the median (95th percentile) of total time per call and the median of paired per-call differences against the baseline arm; arms ran one after another, not interleaved. Cost is the reranker's list price for the tokens sent, not an invoice.

| Arm | Strict P@k | nDCG | P@5 | P@10 | Total ms median (p95) | Added ms vs baseline | USD per 1,000 calls | Tokens per call |
|:---|---:|---:|---:|---:|---:|---:|---:|---:|
| vector, strict-order scan, ef_search 100 | 69.49% | 0.7429 | 77.25% | 75.12% | 77 (309) | -29 | 0.000 | 0 |
| vector, strict-order scan, ef_search 250 (baseline) | 69.58% | 0.7431 | 77.00% | 75.38% | 104 (328) | +0 | 0.000 | 0 |
| vector, strict-order scan, ef_search 300 | 69.58% | 0.7431 | 77.00% | 75.38% | 263 (503) | +91 | 0.000 | 0 |
| vector, strict-order scan, ef_search 200 | 69.58% | 0.7431 | 77.00% | 75.38% | 72 (278) | -23 | 0.000 | 0 |
| vector, strict-order scan, ef_search 400 | 69.66% | 0.7433 | 76.75% | 75.38% | 267 (555) | +128 | 0.000 | 0 |
| rerank-3-lite, pool 100 | 78.97% | 0.8465 | 87.25% | 83.38% | 679 (927) | +519 | 0.096 | 4,783 |
| rerank-3-lite, pool 200 | 79.28% | 0.8481 | 87.00% | 83.88% | 773 (1142) | +622 | 0.190 | 9,522 |
| rerank-3-lite, pool 300 | 79.38% | 0.8497 | 87.25% | 84.12% | 849 (1415) | +744 | 0.284 | 14,216 |
| rerank-3-lite, pool 400 | 79.65% | 0.8525 | 87.00% | 84.62% | 1008 (1656) | +860 | 0.375 | 18,746 |


Paired contrasts (80 calls in 43 clusters; paired cluster bootstrap, 2,000 draws, seed 42; percentage points for precision):

| Contrast | Precision change, pp (95% interval) | nDCG change (95% interval) | Calls improved / tied / worsened |
|:---|---:|---:|---:|
| rerank pool 100 against the vector baseline | +9.39 (+5.40 to +11.93) | +0.1034 (+0.0610 to +0.1355) | 43 / 31 / 6 |
| same pool, vector order against reranked order (isolates the reranker) | +9.30 (+5.29 to +11.87) | +0.1029 (+0.0610 to +0.1348) | 43 / 31 / 6 |
| ef_search 100 against 250 | -0.09 (-0.49 to +0.28) | -0.0002 (-0.0032 to +0.0027) | 3 / 73 / 4 |
| ef_search 400 against 250 | +0.08 (+0.00 to +0.32) | +0.0002 (-0.0009 to +0.0017) | 1 / 79 / 0 |
| pool 200 against pool 100 | +0.31 (-0.70 to +1.21) | +0.0016 (-0.0067 to +0.0085) | 19 / 43 / 18 |
| pool 300 against pool 100 | +0.41 (-1.22 to +1.68) | +0.0032 (-0.0102 to +0.0140) | 21 / 45 / 14 |
| pool 400 against pool 100 | +0.69 (-1.20 to +1.98) | +0.0060 (-0.0097 to +0.0173) | 24 / 39 / 17 |


Per caller, pool 100 against the vector baseline. The dialectic and prefetch rows were replayed peer-wide because their session scope was not recorded, and rest on 5 and 7 clusters: descriptive only.

| Caller | Calls / clusters | Vector | Rerank, pool 100 | Gain, pp | nDCG gain |
|:---|---:|---:|---:|---:|---:|
| dream.deduction | 20 / 20 | 80.65% | 86.83% | +6.18 | +0.0836 |
| dream.induction | 20 / 20 | 80.26% | 87.01% | +6.75 | +0.0772 |
| dialectic | 20 / 5 | 62.92% | 75.33% | +12.42 | +0.1227 |
| dialectic_prefetch | 20 / 7 | 54.50% | 66.70% | +12.20 | +0.1300 |


Sensitivity of the pool-100 gain (point estimates):

| Subset | Calls | Precision change, pp | nDCG change |
|:---|---:|---:|---:|
| exclude one nonspecific call | 79 | +9.25 | +0.1029 |
| dream callers only | 40 | +6.47 | +0.0804 |


What reranking does to the result list, by pool size (hand-transcribed from the run summary): `kept` is the share of the vector top-`k` still in the reranked top-`k`; `from deeper` is the mean number of reranked results that came from beyond the vector top-`k`.

| Pool | Kept | From deeper | Pool actually available | Voyage call ms (median) | Tokens per call |
|:---|---:|---:|---:|---:|---:|
| 100 | 0.46 | 11.1 | 100 | 403 | 4,783 |
| 200 | 0.40 | 12.4 | 199 | 442 | 9,522 |
| 300 | 0.38 | 12.9 | 298 | 472 | 14,216 |
| 400 | 0.37 | 13.1 | 396 | 545 | 18,746 |


Reliability warning, message search (a different workload: long message payloads; 40 calls per pool; relevance not judged; hand-transcribed). The cause of the errors was not established.

| Pool | Reranked | HTTP errors | Error rate | Voyage call ms (median) | Tokens per call |
|:---|---:|---:|---:|---:|---:|
| 100 | 40 | 0 | 0% | 566 | 61,883 |
| 200 | 40 | 0 | 0% | 603 | 127,093 |
| 300 | 28 | 12 | 30% | 844 | 158,762 |
| 400 | 30 | 10 | 25% | 951 | 196,464 |


Labelling audit (hand-transcribed counts):

- 719 grading packets, 34,829 packet rows for 31,663 cases (3,166 duplicate instances were injected or repeated).
- Before adjudication, fresh reviewers agreed on 92 of 120 sampled contexts, and on 44 of 60 in an enriched sample (the two samples are not poolable).
- The final adjudication phase changed 548 scores; a source-first refresh of legacy packets changed 366 of 1,029. These phase counts overlap and must not be summed.
- 55 of the 3,208 labels used in an earlier, returned-results-only comparison changed.
- Independent verifier: PASS WITH LIMITS. Human gold: none.

## Study 2: three-judge replay (earlier, larger run)

Source: `study2_three_judge_replay.json`. 270 calls with complete grades, every result graded 0-2 by three model judges; a result counts as relevant when at least two judges gave 2 (majority grade). nDCG at the caller's `top_k`; calls that fell back to vector order are excluded from the reranked columns and counted in the last column. `stock` is the server's default settings, `scan` is iterative scan with `ef_search = max(40, top_k)`, `lite N` and `r3 N` are rerank-3-lite and rerank-3 with pool N (iterative scan, `ef_search` = pool). Latency from this run is not reported: it was measured with several calls in flight and cannot be separated from provider time.

| Caller (calls / clusters) | stock | scan | lite 50 | lite 100 | lite 200 | r3 50 | r3 100 | r3 200 | Fallback, lite 100 |
|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| dream.deduction (105 / 32) | 0.598 | 0.638 | 0.784 | 0.810 | 0.832 | 0.784 | 0.826 | 0.842 | 1.90% |
| dream.induction (105 / 32) | 0.562 | 0.599 | 0.759 | 0.808 | 0.828 | 0.775 | 0.822 | 0.852 | 0.95% |
| dialectic (32 / 3) | 0.548 | 0.562 | 0.670 | 0.704 | 0.732 | 0.681 | 0.730 | 0.741 | 12.50% |
| dialectic_prefetch (28 / 4) | 0.477 | 0.669 | 0.784 | 0.812 | 0.761 | 0.767 | 0.805 | 0.823 | 21.43% |
| overall (270 / 37) | 0.566 | 0.617 | 0.761 | 0.798 | 0.814 | 0.767 | 0.811 | 0.834 | 4.81% |


Primary contrast of the decision rule (written after this run's data were collected, see `METHOD.md`; stock against rerank-3-lite pool 100, dream callers):

| Workload | Paired calls | Clusters | nDCG change | 95% interval |
|:---|---:|---:|---:|---:|
| dream.deduction | 103 | 32 | +0.212 | +0.177 to +0.252 |
| dream.induction | 104 | 32 | +0.248 | +0.202 to +0.299 |


Post-hoc descriptive contrasts, all callers pooled (37 clusters; computed for this write-up, not part of the registered rule; matched non-fallback calls only):

| Contrast | Paired calls | nDCG change | 95% interval |
|:---|---:|---:|---:|
| rerank-3 over rerank-3-lite, pool 100 | 251 | +0.012 | +0.007 to +0.018 |
| rerank-3 over rerank-3-lite, pool 200 | 233 | +0.015 | +0.007 to +0.022 |
| pool 200 over pool 100 (rerank-3-lite) | 238 | +0.023 | +0.014 to +0.033 |
| pool 50 against pool 100 (rerank-3-lite) | 255 | -0.042 | -0.052 to -0.032 |


The practical-gain threshold in the decision rule is +0.03. Adding a reranker clears it by a wide margin; moving between rerank-3 and rerank-3-lite, or between pool sizes, does not, and rerank-3 costs 2.5 times as much per token (see `costs.json`).

Sensitivity to the judge (overall nDCG at `top_k`, calls graded by that judge only; one judge's grades, not the majority):

| Judge | stock | scan | lite 100 | r3 100 | lite 200 |
|:---|---:|---:|---:|---:|---:|
| haiku | 0.486 | 0.533 | 0.691 | 0.708 | 0.698 |
| luna | 0.553 | 0.606 | 0.763 | 0.775 | 0.778 |
| deepseek | 0.589 | 0.644 | 0.814 | 0.828 | 0.830 |
| majority | 0.566 | 0.617 | 0.798 | 0.811 | 0.814 |


Agreement between judges on the same pairs (linear-weighted kappa; confusion tables are in the JSON):

| Pair | Pairs | Exact agreement | Weighted kappa |
|:---|---:|---:|---:|
| haiku / luna | 11,908 | 61.1% | 0.375 |
| haiku / deepseek | 11,908 | 64.9% | 0.422 |
| luna / deepseek | 11,908 | 73.7% | 0.576 |


Registered decision rule, applied to this run: **inconclusive**. A practical gain alone never authorises a production change; missing evidence is inconclusive.

- `dialectic`: inconclusive (interval includes zero; fewer than 10 clusters; fallback rate at least 5%; scope was approximated: descriptive only; guardrail exact_id_cases: no evidence; guardrail correction_cases: no evidence; guardrail scope_leaks: no evidence)
- `dialectic_prefetch`: inconclusive (fewer than 10 clusters; fallback rate at least 5%; scope was approximated: descriptive only; guardrail exact_id_cases: no evidence; guardrail correction_cases: no evidence; guardrail scope_leaks: no evidence)
- `dream.deduction`: inconclusive (guardrail exact_id_cases: no evidence; guardrail correction_cases: no evidence; guardrail scope_leaks: no evidence)
- `dream.induction`: inconclusive (guardrail exact_id_cases: no evidence; guardrail correction_cases: no evidence; guardrail scope_leaks: no evidence)

## Study 3: what the vector index misses (no reranking, no judges)

Source: `study3_ef_sweep.json`. 6,463 real searches replayed at different `hnsw.ef_search` values and compared with an exact nearest-neighbour search on the same data. Recall is the share of the exact top-`k` that the arm returned; searches whose exact result was empty (1,127 of them) are left out of recall and identical-to-exact. Unless noted, the scan mode is strict order. Database time was recorded while arms ran side by side on one machine, so use it for orders of magnitude, not for small differences. Values are rounded to three places from unrounded recomputation, so a few differ in the last digit from earlier write-ups.

| ef_search | Recall | Prefetch | Dialectic | Dream deduction | Dream induction | Identical to exact | Under-filled | DB ms median / p95 |
|:---|---:|---:|---:|---:|---:|---:|---:|---:|
| 40 (default, no iterative scan) | 0.901 | 0.765 | 0.816 | 0.901 | 0.914 | 74.4% | 34.8% | 40 / 129 |
| 100 | 0.990 | 0.993 | 0.982 | 0.988 | 0.992 | 88.7% | 22.8% | 44 / 201 |
| 150 | 0.993 | 0.996 | 0.985 | 0.992 | 0.995 | 91.0% | 22.8% | 43 / 257 |
| 200 | 0.995 | 0.996 | 0.990 | 0.994 | 0.996 | 92.2% | 22.8% | 39 / 275 |
| 250 | 0.996 | 0.998 | 0.992 | 0.995 | 0.997 | 93.0% | 22.8% | 40 / 303 |
| 300 | 0.999 | 1.000 | 0.996 | 0.998 | 0.999 | 97.7% | 22.8% | 41 / 600 |
| 400 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 100.0% | 22.8% | 37 / 648 |
| 200, relaxed order | 0.996 | 0.997 | 0.993 | 0.995 | 0.997 | 92.2% | 22.8% | 41 / 269 |
| 400, relaxed order | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 100.0% | 22.8% | 38 / 655 |


Under-filled means fewer rows than `top_k`. The exact search itself under-filled 22.8% of the time (fewer matching rows exist, or a distance limit applies), so only the gap to that figure is the index's doing: 34.8% at the default against 22.8% with iterative scan at 250.

Recall by size of the peer pair, from an earlier, smaller replay of 1,063 searches (hand-transcribed; recall is against an exact run on a copy of the database without planner statistics; 679 of the 1,063 searches had a non-empty exact result.):

| Conclusions in the pair | Searches | Default settings | Scan, ef_search 200 | Scan, ef_search 400 |
|:---|---:|---:|---:|---:|
| <100 | 141 | 1.000 | 1.000 | n/a |
| 100-1k | 53 | 1.000 | 1.000 | n/a |
| 1k-10k | 254 | 0.839 | 1.000 | n/a |
| >=10k | 231 | 0.763 | 0.984 | 1.000 |


In that earlier run the default settings under-filled 56.6% of searches against 44.1% with scan; the exact-search floor was not recorded there, so that run overstates what the index alone causes. strict_order and relaxed_order gave the same recall to within 0.003 at ef_search 200 and identical recall at 400.

## Study 4: dream-quality A/B pilot (exploratory)

Source: `study4_ab_dream.json`. Status: exploratory pilot, descriptive only; no significance tests. Each arm ran one dream for each of two peer pairs (peer-A/peer-A and peer-B/peer-B), then the same list of recorded question texts (up to 20, 17 distinct questions answered by every arm) on a separate copy of one database. Arms: A = stock search settings, no rerank; B100 = rerank-3-lite pool 100; B150 = rerank-3-lite pool 150; B200 = rerank-3-lite pool 200; B50 = rerank-3-lite pool 50. Rerank arms used ef_search 200, iterative scan relaxed_order. Judges: gpt-6-luna and claude-haiku-5-5. Retrieval is a seeded sample of each arm's own memory searches, every returned conclusion graded 0-2 by both judges (`share 2` = both gave 2). Answers are each rerank arm's answer against the baseline arm's answer to the same question, shown in random X/Y order (wins / ties / losses for the rerank arm, both judges pooled).

| Arm | Retrieval pairs | Mean grade | Share 2 by both | New conclusions, mean grade | Answers W / T / L | Dream search calls | Dream input tokens | Create calls |
|:---|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 641 | 1.30 | 0.262 | 1.57 (n=20) | baseline | 78 | 2,412,048 | 8 |
| B50 | 782 | 1.37 | 0.347 | 1.43 (n=20) | 20 / 0 / 14 | 103 | 3,411,451 | 11 |
| B100 | 849 | 1.50 | 0.465 | 1.32 (n=20) | 20 / 0 / 14 | 77 | 3,182,989 | 12 |
| B150 | 840 | 1.55 | 0.515 | 1.25 (n=16) | 21 / 1 / 12 | 61 | 2,057,467 | 5 |
| B200 | 910 | 1.48 | 0.434 | 1.25 (n=14) | 20 / 0 / 14 | 60 | 3,324,021 | 5 |


Read this table as a smoke test, not a result. One dream per pair per arm, 17 questions, and the language model's own run-to-run variation are all larger than any difference shown. The answer verdicts favour every rerank arm slightly (20 to 21 wins against 12 to 14 losses out of 34 verdicts per arm; win rate with ties as half 0.59 to 0.63), which is not distinguishable from chance: 34 verdicts come from 17 questions and 2 judges, so they are not independent. The new-conclusion grades run the other way: baseline 1.57 against 1.25 to 1.43 for the rerank arms, on 14 to 20 samples per arm. Retrieval grades are higher for the rerank arms, consistent with Studies 1 and 2, but they grade different searches, because each arm's dream chose its own queries.

## Synthetic demo (invented data; not behind any number above)

Pinned output of `bench/scripts/run_synthetic_demo.py` on `bench/data/synthetic`, from `expected-headline.json`. The data and the word-overlap reranker are invented and built so reranking helps; this shows that the pipeline runs and is deterministic, nothing about real reranking.

| Arm | nDCG at top_k (3 fake judges, majority) | Fallback rate |
|:---|---:|---:|
| rerank:lexical:100 | 0.9264 | 5.6% |
| rerank:lexical:100+vector-prefix | 0.7027 | 0.0% |
| rerank:lexical:200 | 0.9264 | 5.6% |
| rerank:lexical:200+vector-prefix | 0.7027 | 0.0% |
| rerank:lexical:50 | 0.9214 | 5.6% |
| rerank:lexical:50+vector-prefix | 0.7027 | 0.0% |
| scan | 0.7027 | 0.0% |
| stock | 0.6869 | 0.0% |


Stock against `rerank:lexical:100`, all callers: +0.2401 nDCG (95% interval +0.1739 to +0.3246, 26 clusters). Index model: recall of the default settings against exact 0.803, with scan 1.000. Decision rule: **inconclusive** (guardrail evidence missing, fallback rate above 5%).
