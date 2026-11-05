"""Scoring maths: ranking metrics, the cluster bootstrap, majority grades, kappa."""
import math

import pytest

from benchlib import (
    cluster_key, linear_weighted_kappa, majority_grade, pairwise_agreement, paired_cluster_bootstrap, percentile,
    ranking_metrics,
)


def test_binary_metrics_by_hand():
    grades = {"a": 2, "b": 0, "c": 1, "d": 2}
    m = ranking_metrics(["a", "b", "c"], grades, 3, threshold=2)
    assert m["precision"] == pytest.approx(1 / 3)
    assert m["recall"] == pytest.approx(1 / 2)  # one of the two grade-2 documents
    ideal = 1 / math.log2(2) + 1 / math.log2(3)
    assert m["ndcg"] == pytest.approx(1 / ideal)
    assert m["hit"] == 1


def test_graded_metrics_by_hand():
    grades = {"a": 2, "b": 0, "c": 1, "d": 2}
    m = ranking_metrics(["a", "b", "c"], grades, 3, threshold=1)
    dcg = 3 / math.log2(2) + 0 + 1 / math.log2(4)
    idcg = 3 / math.log2(2) + 3 / math.log2(3) + 1 / math.log2(4)
    assert m["ndcg"] == pytest.approx(dcg / idcg)
    assert m["precision"] == pytest.approx(2 / 3)  # a and c are relevant at threshold 1
    assert m["recall"] == pytest.approx(2 / 3)  # a, c, d are relevant


def test_perfect_ranking_scores_one_and_a_short_list_is_penalised():
    grades = {"a": 2, "b": 2, "c": 0}
    assert ranking_metrics(["a", "b"], grades, 2)["ndcg"] == pytest.approx(1.0)
    short = ranking_metrics(["a"], grades, 2)
    assert short["precision"] == pytest.approx(0.5)  # the missing slot counts as a miss


def test_no_relevant_document_scores_zero_not_an_error():
    m = ranking_metrics(["a"], {"a": 0, "b": 1}, 1)
    assert (m["precision"], m["recall"], m["ndcg"], m["hit"]) == (0, 0.0, 0.0, 0)


def test_unjudged_result_raises():
    with pytest.raises(ValueError):
        ranking_metrics(["a", "zzz"], {"a": 2}, 2)


def test_unjudged_result_beyond_k_is_ignored():
    assert ranking_metrics(["a", "zzz"], {"a": 2}, 1)["precision"] == 1.0


def test_percentile_interpolates():
    assert percentile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)
    assert percentile([], 0.5) is None
    assert percentile([7], 0.95) == 7


def test_bootstrap_identical_pairs_have_zero_delta_and_zero_width():
    stat = paired_cluster_bootstrap([0.5, 0.6, 0.7], [0.5, 0.6, 0.7], ["a", "b", "c"], iterations=200)
    assert stat["delta"] == 0 and stat["ci95"] == [0, 0]
    assert stat["interval_includes_zero"]


def test_bootstrap_constant_shift_and_determinism():
    left = [0.1 * i for i in range(12)]
    right = [x + 0.25 for x in left]
    clusters = [f"c{i}" for i in range(12)]
    one = paired_cluster_bootstrap(left, right, clusters, iterations=300, seed=42)
    two = paired_cluster_bootstrap(left, right, clusters, iterations=300, seed=42)
    assert one == two
    assert one["delta"] == pytest.approx(0.25)
    assert one["ci95"][0] == pytest.approx(0.25) and one["ci95"][1] == pytest.approx(0.25)
    assert one["clusters"] == 12 and not one["few_clusters"]
    assert paired_cluster_bootstrap(left, right, clusters[:11] + ["c0"], iterations=10)["clusters"] == 11


def test_bootstrap_flags_few_clusters_and_validates_lengths():
    stat = paired_cluster_bootstrap([0, 0, 0], [1, 1, 1], ["x", "x", "y"], iterations=50)
    assert stat["few_clusters"] and stat["clusters"] == 2
    with pytest.raises(ValueError):
        paired_cluster_bootstrap([1], [1, 2])
    assert paired_cluster_bootstrap([], []) is None


def test_bootstrap_keeps_whole_clusters_together():
    # Two clusters with opposite differences: the mean resamples between -1, 0 and +1 only.
    stat = paired_cluster_bootstrap([0, 0, 1, 1], [1, 1, 0, 0], ["a", "a", "b", "b"], iterations=400, seed=3)
    assert stat["delta"] == 0
    assert stat["ci95"][0] == -1 and stat["ci95"][1] == 1


def test_cluster_key_uses_run_for_dreams_and_pair_session_otherwise():
    dream = dict(caller_type="dream.deduction", run_id="r1", observer="x", observed="y")
    assert cluster_key(dream) == "run:r1"
    ask = dict(caller_type="dialectic", run_id="r9", workspace_name="w", observer="x", observed="y", session_id="s")
    assert cluster_key(ask).startswith("pair/session:") and "r9" not in cluster_key(ask)
    assert cluster_key(dict(dream, run_id=None)).startswith("pair/session:")


def test_majority_grade():
    assert majority_grade([2, 2, 0]) == 2
    assert majority_grade([0, 1, 1]) == 1
    assert majority_grade([0, 1, 2]) == 1  # all different: the middle grade
    with pytest.raises(ValueError):
        majority_grade([1, 2])


def test_kappa_perfect_and_chance():
    assert linear_weighted_kappa([[5, 0, 0], [0, 5, 0], [0, 0, 5]]) == pytest.approx(1.0)
    # Independent raters with identical marginals: kappa is 0.
    assert linear_weighted_kappa([[1, 1, 1], [1, 1, 1], [1, 1, 1]]) == pytest.approx(0.0)
    assert linear_weighted_kappa([[0] * 3] * 3) is None


def test_pairwise_agreement_counts_only_common_pairs():
    left = {("c", "1"): 2, ("c", "2"): 0, ("c", "3"): 1}
    right = {("c", "1"): 2, ("c", "2"): 1, ("c", "9"): 2}
    out = pairwise_agreement(left, right)
    assert out["pairs"] == 2 and out["exact_agreement"] == 0.5
    assert out["confusion"][2][2] == 1 and out["confusion"][0][1] == 1
