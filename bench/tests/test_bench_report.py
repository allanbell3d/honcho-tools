"""Report: nDCG/precision per caller and arm, fallback handling, paired contrasts, the decision rule, agreement."""
import pytest

import report
from benchlib import ranking_metrics


def make(n_clusters=12, gain=True, fallback_every=0):
    """n dream calls (one per run), each with 4 candidates d0..d3; d0 is the only relevant one.
    Arm 'stock' ranks it last (nDCG low), arm 'cand' first when ``gain``."""
    calls, rankings, judgments = [], [], {"j1": [], "j2": [], "j3": []}
    for i in range(n_clusters):
        cid = f"c{i}"
        calls.append(dict(call_id=cid, caller_type="dream.deduction", run_id=f"r{i}", top_k=2, query="q",
                          workspace_name="w", observer="o", observed="o", scope_approximated=False))
        docs = [dict(id=f"{cid}-d{k}", content=f"t{k}") for k in range(4)]
        stock_ids = [f"{cid}-d3", f"{cid}-d2"]
        cand_ids = [f"{cid}-d0", f"{cid}-d1"] if gain else stock_ids
        fell = bool(fallback_every) and i % fallback_every == 0
        rankings.append(dict(call_id=cid, arm="stock", top_k=2, ids=stock_ids, documents=docs, total_ms=10.0,
                             provider_ms=0.0, fallback_reason=None))
        rankings.append(dict(call_id=cid, arm="rerank:m:100", top_k=2, ids=stock_ids if fell else cand_ids, documents=docs,
                             candidate_ids=[d["id"] for d in docs], total_ms=110.0, provider_ms=90.0, outcome="reranked",
                             rerank_tokens=1000, fallback_reason="timeout" if fell else None))
        for name, rows in judgments.items():
            for k in range(4):
                rows.append(dict(call_id=cid, document_id=f"{cid}-d{k}", grade=2 if k == 0 else 0))
    return calls, rankings, judgments


def overall(rep, arm, source=None, k="caller_k", threshold=2):
    src = rep["sources"][source or rep["primary_source"]]
    return next(m for m in src["metrics"] if m["caller"] == "overall" and m["arm"] == arm and m["k"] == k and m["threshold"] == threshold)


def test_metrics_and_contrast_on_a_toy_set():
    calls, rankings, judgments = make()
    rep = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100")
    assert rep["primary_source"] == "majority"
    assert overall(rep, "stock")["ndcg"] == 0 and overall(rep, "stock")["precision"] == 0
    assert overall(rep, "rerank:m:100")["ndcg"] == pytest.approx(1.0)
    assert overall(rep, "rerank:m:100")["precision"] == pytest.approx(0.5)
    comp = next(c for c in rep["sources"]["majority"]["comparisons"] if c["caller"] == "dream.deduction")
    assert comp["ndcg"]["delta"] == pytest.approx(1.0) and comp["ndcg"]["clusters"] == 12
    assert comp["precision_calls_improved"] == 12 and comp["primary"]


def test_fallback_rows_leave_the_primary_number_but_stay_in_the_blended_one():
    calls, rankings, judgments = make(fallback_every=4)  # calls 0, 4, 8 fall back
    rep = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100")
    m = overall(rep, "rerank:m:100")
    assert m["fallback_rows"] == 3 and m["fallback_rate"] == pytest.approx(0.25) and m["scored_calls"] == 9
    assert m["ndcg"] == pytest.approx(1.0)  # successful rows only
    assert m["blended_ndcg"] == pytest.approx(9 / 12)  # fallback rows score like the baseline (0)
    comp = next(c for c in rep["sources"]["majority"]["comparisons"] if c["caller"] == "dream.deduction")
    assert comp["paired_calls"] == 9  # matched non-fallback pairs only


def test_vector_prefix_control_is_the_same_pool_without_reranking():
    calls, rankings, judgments = make()
    rep = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100", vector_prefix=True)
    control = overall(rep, "rerank:m:100+vector-prefix")
    assert control["ndcg"] == pytest.approx(1.0)  # candidates are d0..d3 in order, so the vector prefix has d0 first
    assert control["fallback_rows"] == 0


def test_a_call_with_an_unjudged_document_is_excluded_not_scored_as_zero():
    calls, rankings, judgments = make(n_clusters=3)
    judgments["j2"] = [r for r in judgments["j2"] if r["call_id"] != "c1"]  # one judge missed the whole call
    rep = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100")
    assert rep["sources"]["majority"]["coverage"]["complete_calls"] == 2
    assert rep["sources"]["j1"]["coverage"]["complete_calls"] == 3


def test_missing_rows_never_become_zero_grades():
    calls, rankings, judgments = make(n_clusters=2)
    judgments["j3"][0] = dict(call_id="c0", document_id="c0-d0", grade=None, missing=True)
    rep = report.build_report(calls, rankings, judgments, baseline="stock")
    assert rep["sources"]["majority"]["coverage"]["complete_calls"] == 1


def test_calls_where_no_arm_returned_anything_are_left_out():
    calls, rankings, judgments = make(n_clusters=2)
    calls.append(dict(call_id="empty", caller_type="dream.induction", run_id="rx", top_k=2, query="q",
                      workspace_name="w", observer="o", observed="o"))
    rankings += [dict(call_id="empty", arm="stock", top_k=2, ids=[], documents=[], total_ms=1.0, fallback_reason=None)]
    rep = report.build_report(calls, rankings, judgments, baseline="stock")
    assert rep["sources"]["majority"]["coverage"]["complete_calls"] == 2


def test_exact_ceiling_widens_the_pool_but_is_not_scored():
    calls, rankings, judgments = make(n_clusters=1)
    docs = [dict(id=f"c0-d{k}", content="t") for k in range(4)]
    rankings.append(dict(call_id="c0", arm="exact", top_k=2, ids=[d["id"] for d in docs], documents=docs, total_ms=5.0))
    rep = report.build_report(calls, rankings, judgments, baseline="stock")
    assert all(m["arm"] != "exact" for m in rep["sources"]["majority"]["metrics"])


def test_decision_rule_is_eligible_only_with_every_condition_and_all_guardrails():
    calls, rankings, judgments = make()
    good = dict(exact_id_cases="pass", correction_cases="pass", scope_leaks="pass")
    rep = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100", guardrails=good)
    assert rep["decision"]["verdict"] == "eligible"
    missing = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100")
    assert missing["decision"]["verdict"] == "inconclusive"
    assert any("no evidence" in r for r in missing["decision"]["workloads"][0]["reasons"])
    failed = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100",
                                 guardrails=dict(good, scope_leaks="fail"))
    assert any("scope_leaks: failed" in r for r in failed["decision"]["workloads"][0]["reasons"])


def test_decision_rule_blocks_few_clusters_small_gain_and_high_fallback():
    good = dict(exact_id_cases="pass", correction_cases="pass", scope_leaks="pass")
    calls, rankings, judgments = make(n_clusters=9)
    few = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100", guardrails=good)
    assert "fewer than 10 clusters" in few["decision"]["workloads"][0]["reasons"]
    calls, rankings, judgments = make(gain=False)
    flat = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100", guardrails=good)
    reasons = flat["decision"]["workloads"][0]["reasons"]
    assert "gain below +0.03" in reasons and "interval includes zero" in reasons
    calls, rankings, judgments = make(fallback_every=2)
    flaky = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100", guardrails=good)
    assert "fallback rate at least 5%" in flaky["decision"]["workloads"][0]["reasons"]


def test_scope_approximated_workloads_are_descriptive_only():
    calls, rankings, judgments = make()
    for c in calls:
        c["scope_approximated"] = True
        c["caller_type"] = "dialectic"
    rep = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100",
                              guardrails=dict(exact_id_cases="pass", correction_cases="pass", scope_leaks="pass"))
    assert rep["decision"]["verdict"] == "inconclusive"
    assert any("approximated" in r for r in rep["decision"]["workloads"][0]["reasons"])


def test_agreement_between_judges_and_majority_only_for_three():
    calls, rankings, judgments = make(n_clusters=2)
    judgments["j3"] = [dict(r, grade=1 if r["grade"] == 2 else 0) for r in judgments["j3"]]
    rep = report.build_report(calls, rankings, judgments, baseline="stock")
    assert len(rep["judge_agreement"]) == 3
    j12 = next(a for a in rep["judge_agreement"] if (a["left"], a["right"]) == ("j1", "j2"))
    assert j12["exact_agreement"] == 1.0
    two = report.build_report(calls, rankings, {k: judgments[k] for k in ("j1", "j2")}, baseline="stock")
    assert "majority" not in two["sources"] and two["primary_source"] == "j1"


def test_price_and_latency_summaries():
    calls, rankings, judgments = make(n_clusters=2)
    rep = report.build_report(calls, rankings, judgments, baseline="stock", rates={"m": 0.02})
    m = overall(rep, "rerank:m:100")
    assert m["list_price_usd_per_1000_calls"] == pytest.approx(1000 * 0.02 / 1e6 * 1000)
    assert m["added_ms_vs_baseline"]["median"] == pytest.approx(100.0)
    assert m["provider_ms"]["median"] == pytest.approx(90.0)


def test_grade_map_rejects_bad_grades_and_skips_missing():
    with pytest.raises(ValueError):
        report.grade_map([dict(call_id="c", document_id="d", grade=5)])
    assert report.grade_map([dict(call_id="c", document_id="d", grade=None, missing=True)]) == {}


def test_report_agrees_with_ranking_metrics_directly():
    calls, rankings, judgments = make(n_clusters=1)
    grades = {f"c0-d{k}": 2 if k == 0 else 0 for k in range(4)}
    direct = ranking_metrics(["c0-d3", "c0-d2"], grades, 2)
    rep = report.build_report(calls, rankings, judgments, baseline="stock")
    assert overall(rep, "stock")["precision"] == direct["precision"]


def test_markdown_renders_the_decision():
    calls, rankings, judgments = make()
    rep = report.build_report(calls, rankings, judgments, baseline="stock", candidate="rerank:m:100")
    text = report.render_markdown(rep)
    assert "Decision: **inconclusive**" in text and "rerank:m:100" in text
