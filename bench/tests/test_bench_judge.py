"""Judging: strict parsing, blinding, batching, retries, ledger charging and the offline fake judges."""
import json

import pytest

import judge
from benchlib import read_jsonl
from ledger import Ledger


def pair(i, call_id="c1", content=None):
    return dict(pair_id=f"p{i}", call_id=call_id, document_id=f"d{i}", query="q one", caller_type="dialectic",
                content=content or f"conclusion {i}")


def labels(n):
    return {f"p{i:02d}" for i in range(1, n + 1)}


def answer(grades):
    return json.dumps({"grades": [{"label": f"p{i:02d}", "grade": g} for i, g in enumerate(grades, 1)]})


# --------------------------------------------------------------------------------------------- parsing
def test_validate_grades_accepts_exact_coverage():
    assert judge.validate_grades(answer([0, 1, 2]), labels(3)) == {"p01": 0, "p02": 1, "p03": 2}


@pytest.mark.parametrize("content", [
    json.dumps({"grades": [{"label": "p01", "grade": 3}]}),
    json.dumps({"grades": [{"label": "p01", "grade": True}]}),
    json.dumps({"grades": [{"label": "p01", "grade": 1.0}]}),
    json.dumps({"grades": [{"label": "p01", "grade": "2"}]}),
    json.dumps({"grades": [{"label": "p01", "grade": 1}, {"label": "p01", "grade": 1}]}),
    json.dumps({"grades": [{"label": "p07", "grade": 1}]}),
    json.dumps({"grades": [{"label": "p01", "grade": 1, "why": "x"}]}),
    json.dumps({"grades": []}),
    json.dumps({"grades": [{"label": "p01", "grade": 1}], "extra": 1}),
    json.dumps([{"label": "p01", "grade": 1}]),
    "not json",
])
def test_validate_grades_rejects_anything_else(content):
    with pytest.raises((ValueError, json.JSONDecodeError)):
        judge.validate_grades(content, labels(1))


# --------------------------------------------------------------------------------------------- blinding
def test_prompt_hides_ids_and_arms_and_uses_batch_local_labels():
    batch = [pair(1), pair(2)]
    rows = json.loads(judge.batch_prompt(batch))
    assert [r["label"] for r in rows] == ["p01", "p02"]
    assert set(rows[0]) == {"caller_type", "query", "content", "label"}
    text = judge.batch_prompt(batch)
    assert "d1" not in text and "c1" not in text and "pair_id" not in text


def test_batches_are_stable_and_independent_of_input_order():
    pairs = [pair(i) for i in range(60)]
    one = judge.prepare_batches(pairs, 25, seed=1)
    two = judge.prepare_batches(list(reversed(pairs)), 25, seed=1)
    assert [[p["pair_id"] for p in b] for b in one] == [[p["pair_id"] for p in b] for b in two]
    assert [len(b) for b in one] == [25, 25, 10]
    other = judge.prepare_batches(pairs, 25, seed=2)
    assert [[p["pair_id"] for p in b] for b in other] != [[p["pair_id"] for p in b] for b in one]


def test_pool_includes_returned_ceiling_top_twenty_and_vector_prefix():
    calls = [dict(call_id="c1", query="q", caller_type="dialectic")]
    docs = {f"d{i}": f"text {i}" for i in range(30)}
    rankings = [
        dict(call_id="c1", arm="exact", top_k=5, ids=list(docs), documents=[dict(id=i, content=c) for i, c in docs.items()]),
        dict(call_id="c1", arm="rerank:m:100", top_k=5, ids=["d29"], candidate_ids=["d0", "d1", "d2", "d3", "d4", "d5", "d6"],
             documents=[dict(id=i, content=c) for i, c in docs.items()]),
    ]
    pooled = {p["document_id"] for p in judge.pool_pairs(calls, rankings)}
    assert pooled == {f"d{i}" for i in range(20)} | {"d29"}  # exact: first 20; rerank: result + 5 vector-prefix (inside the 20)


def test_pool_refuses_results_without_saved_content():
    calls = [dict(call_id="c1", query="q", caller_type="dream.deduction")]
    with pytest.raises(ValueError):
        judge.pool_pairs(calls, [dict(call_id="c1", arm="stock", top_k=1, ids=["d1"], documents=[])])


# --------------------------------------------------------------------------------------------- running
class Scripted:
    """Returns scripted answers in order; an Exception entry is raised as a transport failure."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def grade(self, prompt):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        n = len(json.loads(prompt))
        return (item if isinstance(item, str) else answer([1] * n)), dict(input_tokens=1000, cached_input_tokens=0, output_tokens=100)


SPEC = judge.JudgeSpec("t", "openai", "model-x", "UNUSED_KEY")
RATES = (1.0, 0.0, 2.0)


def test_valid_batch_is_stored_and_resumed_without_new_requests(tmp_path):
    batches = [[pair(1), pair(2)]]
    fake = Scripted([True])
    stats = judge.run_judge("t", fake, batches, tmp_path)
    assert stats["graded"] == 2 and fake.calls == 1
    rows = read_jsonl(tmp_path / "judgments-t.jsonl")
    assert {r["pair_id"] for r in rows} == {"p1", "p2"} and all(r["grade"] == 1 for r in rows)
    again = judge.run_judge("t", Scripted([]), batches, tmp_path)
    assert again["skipped"] == 1 and again["graded"] == 0


def test_invalid_answer_is_retried_once_then_stored_as_missing_never_zero(tmp_path):
    fake = Scripted(["garbage", "still garbage"])
    stats = judge.run_judge("t", fake, [[pair(1)]], tmp_path)
    assert fake.calls == 2 and stats["missing"] == 1
    (row,) = read_jsonl(tmp_path / "judgments-t.jsonl")
    assert row["grade"] is None and row["missing"] is True
    attempts = [a["status"] for a in read_jsonl(tmp_path / "judge-attempts-t.jsonl")]
    assert attempts == ["invalid", "invalid"]


def test_second_attempt_can_succeed(tmp_path):
    fake = Scripted(["garbage", True])
    assert judge.run_judge("t", fake, [[pair(1)]], tmp_path)["graded"] == 1


def test_transport_failure_is_not_retried_and_costs_its_reservation(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", cap_usd=10)
    fake = Scripted([TimeoutError("boom")])
    stats = judge.run_judge("t", fake, [[pair(1)]], tmp_path, spec=SPEC, ledger=ledger, rates=RATES)
    assert fake.calls == 1 and stats["missing"] == 1
    bound = judge.request_cost_bound(judge.batch_prompt([pair(1)]), SPEC, RATES)
    assert float(ledger.committed) == pytest.approx(bound)
    # a rerun skips the failed batch unless asked to retry it
    again = Scripted([True])
    assert judge.run_judge("t", again, [[pair(1)]], tmp_path, spec=SPEC, ledger=ledger, rates=RATES)["skipped"] == 1
    assert again.calls == 0
    retried = judge.run_judge("t", again, [[pair(1)]], tmp_path, spec=SPEC, ledger=ledger, rates=RATES, retry_failed=True)
    assert retried["graded"] == 1


def test_success_settles_from_reported_usage(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", cap_usd=10)
    judge.run_judge("t", Scripted([True]), [[pair(1)]], tmp_path, spec=SPEC, ledger=ledger, rates=RATES)
    assert float(ledger.committed) == pytest.approx((1000 * 1.0 + 100 * 2.0) / 1e6)


def test_ledger_cap_stops_judging_before_a_request(tmp_path):
    from ledger import BudgetExceeded

    ledger = Ledger(tmp_path / "l.jsonl", cap_usd="0.0000001")
    fake = Scripted([True])
    with pytest.raises(BudgetExceeded):
        judge.run_judge("t", fake, [[pair(1)]], tmp_path, spec=SPEC, ledger=ledger, rates=RATES)
    assert fake.calls == 0


def test_cost_bound_uses_bytes_and_the_output_allowance():
    prompt = judge.batch_prompt([pair(1)])
    bound = judge.request_cost_bound(prompt, SPEC, (1.0, 0.0, 2.0))
    assert bound == pytest.approx((judge.input_bound(prompt, SPEC) * 1.0 + judge.OUTPUT_BOUND * 2.0) / 1e6)
    json_spec = judge.JudgeSpec("d", "openai", "m", "K", json_object_mode=True)
    assert judge.input_bound(prompt, json_spec) > judge.input_bound(prompt, SPEC)  # the shape hint is extra input


# --------------------------------------------------------------------------------------------- fakes
def test_fake_judge_is_deterministic_and_noise_flips_by_at_most_one_grade():
    truth = {("q one", f"conclusion {i}"): 2 for i in range(40)}
    batch = [pair(i) for i in range(40)]
    prompt = judge.batch_prompt(batch)
    exact = judge.FakeJudge("x", truth, 0.0).grade(prompt)[0]
    assert set(g["grade"] for g in json.loads(exact)["grades"]) == {2}
    noisy_a = judge.FakeJudge("x", truth, 0.5).grade(prompt)[0]
    noisy_b = judge.FakeJudge("x", truth, 0.5).grade(prompt)[0]
    assert noisy_a == noisy_b
    assert set(g["grade"] for g in json.loads(noisy_a)["grades"]) == {1, 2}  # 2 can only fall to 1 (or stay)
    assert judge.FakeJudge("y", truth, 0.5).grade(prompt)[0] != noisy_a  # a different judge flips different items


def test_build_truth_defaults_to_zero_and_is_keyed_by_text():
    calls = [dict(call_id="c1", query="q")]
    corpus = [dict(id="d1", content="alpha"), dict(id="d2", content="beta")]
    truth = judge.build_truth(calls, corpus, [dict(call_id="c1", document_id="d1", grade=2)])
    assert truth == {("q", "alpha"): 2}


def test_spotcheck_is_stratified_and_leaves_the_human_column_blank(tmp_path):
    pairs = {f"p{i}": pair(i) for i in range(30)}
    judgments = [dict(pair_id=f"p{i}", grade=i % 3) for i in range(30)]
    path = tmp_path / "spot.csv"
    judge.write_spotcheck(pairs, judgments, path, size=9)
    import csv

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 9 and {r["grade"] for r in rows} == {"0", "1", "2"}
    assert all(r["human_grade"] == "" for r in rows)


def test_dry_run_prints_bounds_without_any_judge(tmp_path, capsys):
    from benchlib import write_jsonl

    write_jsonl(tmp_path / "calls.jsonl", [dict(call_id="c1", query="q", caller_type="dialectic")])
    write_jsonl(tmp_path / "r.jsonl", [dict(call_id="c1", arm="stock", top_k=1, ids=["d1"],
                                            documents=[dict(id="d1", content="x")])])
    (tmp_path / "rates.json").write_text(json.dumps({"haiku": [0.1, 0.1, 0.5]}))
    judge.main(["--calls", str(tmp_path / "calls.jsonl"), "--rankings", str(tmp_path / "r.jsonl"),
                "--out-dir", str(tmp_path), "--judges", "haiku", "--rates", str(tmp_path / "rates.json"), "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and out["judges"]["haiku"]["pairs"] == 1 and out["judges"]["haiku"]["worst_case_usd"] > 0


def test_provider_judge_refuses_to_start_without_its_key(monkeypatch):
    monkeypatch.delenv("UNUSED_KEY", raising=False)
    with pytest.raises(SystemExit):
        judge.ProviderJudge(SPEC)
