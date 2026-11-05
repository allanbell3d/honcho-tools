# Provenance

Each file in this folder was derived from one private run. The runs are named here by a label and a month, not by path or
timestamp; their inputs (queries, conclusions, rankings, labels, telemetry) are private and are not published. `CHECKSUMS.sha256`
lists the SHA-256 of every file in this folder as published (`sha256sum -c CHECKSUMS.sha256` from this folder).

| Published file | Private run | When | Honcho | How it was derived |
|---|---|---|---|---|
| `study1_conclusion_replay.json` | **replay-2**: 80 frozen calls, nine saved arms, final adjudicated label set | early November 2026 | 3.5.0, the shipping rerank wrapper | The review's final report (`metrics`, `comparisons`, `operational`) and its supplementary interval table, reduced to an allow-list by `extract_published.py study1`. The blocks `hand_transcribed_from_run_summary` and `labelling_process` are copied by hand from the run's own summary table and completion record. Rescored with `report.py` on the private rankings and labels: precision, nDCG and the pool-100 contrast reproduce exactly. |
| `study2_three_judge_replay.json` | **replay-1**: 369 sampled calls, 270 fully judged by three judges, arms stock / scan / exact / rerank-3-lite and rerank-3 at pools 50, 100, 200 | late October 2026 | 3.4.0, an earlier copy of the wrapper | `report.py` on the private cleaned rankings and the three judges' files (`--baseline stock --candidate rerank:rerank-3-lite:100 --contrast ...`), then `extract_published.py study2`. Reproduces the repaired report of that run exactly; the four post-hoc contrasts were added for this write-up. |
| `study3_ef_sweep.json` | **scan-1**: 6,463 searches, ten saved arms including the exact ceiling | early November 2026 | 3.5.0 | `ef_sweep.py` on the private per-arm rankings, then `extract_published.py study3`. The block `earlier_smaller_run` (1,063 searches, **scan-0**) is hand-transcribed from that run's own tables and was not recomputed. |
| `study4_ab_dream.json` | **ab-1**: five arms, one dream per pair per arm, two judges | early November 2026 | 3.5.0, the shipping rerank wrapper | `ab_report.py` on the private judged rows and dream event files, then `extract_published.py study4`. The `design` block is written by hand from the driver's configuration. |
| `costs.json` | the spend ledgers of the runs above | | | Latest row per request summed by provider and model; written by hand from the ledger totals. Study 1's labelling cost was not metered. |
| `RESULTS.md` | | | | `render_results.py` from the five JSON files and `data/synthetic/expected-headline.json`. |

Dates are given to the month on purpose. Harvest counts (events, raw and unique calls) are in `METHOD.md`.

## What was not published, and why

Queries, conclusions, answers, per-call and per-pair labels, rankings, document and call ids, run ids, telemetry file names and
hashes, peer and workspace names, peer-pair sizes beyond coarse bands, example conclusions or "corrected examples" from the label
review, local paths and hosts, the judge-reasoning settings of the label review, and the original private scripts. The Study 4 and
Study 1 raw files, the database dumps the arms were cloned from and the vendored Honcho source stay private.
