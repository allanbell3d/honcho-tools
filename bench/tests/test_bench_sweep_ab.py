"""ef_search sweep arithmetic and the A/B dream-quality aggregation and blinding."""
import json

import pytest

import ab_judge
import ab_report
import ef_sweep


# ------------------------------------------------------------------------------------------ ef sweep
def row(cid, ids, top_k=3, ms=10.0):
    return dict(call_id=cid, ids=ids, top_k=top_k, db_ms=ms)


CALLS = [dict(call_id="a", caller_type="dream.deduction"), dict(call_id="b", caller_type="dialectic"),
         dict(call_id="c", caller_type="dialectic")]


def test_recall_identical_underfill_and_latency():
    exact = [row("a", ["1", "2", "3"]), row("b", ["1", "2", "3"]), row("c", [])]
    arms = {"x": [row("a", ["1", "2", "3"], ms=10), row("b", ["1", "9"], ms=30), row("c", [], ms=20)]}
    out = ef_sweep.sweep(CALLS, exact, arms)["arms"]["x"]
    assert out["all"]["searches"] == 3 and out["all"]["searches_with_exact_result"] == 2  # c has no exact rows
    assert out["all"]["recall"] == pytest.approx((1 + 1 / 3) / 2)
    assert out["all"]["identical_to_exact"] == pytest.approx(0.5)
    assert out["all"]["underfilled"] == pytest.approx(2 / 3)  # b (2 of 3) and c (0 of 3)
    assert out["all"]["db_ms_median"] == 20
    assert out["dialectic"]["searches"] == 2 and out["dream.deduction"]["recall"] == 1.0


def test_recall_ignores_rows_beyond_top_k_of_the_exact_list():
    exact = [row("a", ["1", "2", "3", "4", "5"], top_k=2)]
    arms = {"x": [row("a", ["1", "2"], top_k=2)]}
    assert ef_sweep.sweep(CALLS, exact, arms)["arms"]["x"]["all"]["recall"] == 1.0


def test_exact_underfill_separates_corpus_from_index():
    exact = [row("a", ["1"]), row("b", ["1", "2", "3"])]
    assert ef_sweep.sweep(CALLS, exact, {"x": []})["exact_underfilled"] == pytest.approx(0.5)


def test_cli_writes_a_report(tmp_path):
    from benchlib import write_jsonl

    write_jsonl(tmp_path / "calls.jsonl", CALLS)
    write_jsonl(tmp_path / "exact.jsonl", [row("a", ["1", "2", "3"])])
    write_jsonl(tmp_path / "x.jsonl", [row("a", ["1", "2", "3"])])
    ef_sweep.main(["--calls", str(tmp_path / "calls.jsonl"), "--exact", f"exact={tmp_path / 'exact.jsonl'}",
                   "--arm", f"x={tmp_path / 'x.jsonl'}", "--out", str(tmp_path / "o.json")])
    assert json.loads((tmp_path / "o.json").read_text())["arms"]["x"]["all"]["recall"] == 1.0
    with pytest.raises(SystemExit):
        ef_sweep.main(["--calls", "c", "--exact", "nopath", "--arm", "x=y", "--out", "o"])


# ------------------------------------------------------------------------------------------ A/B tables
def test_retrieval_table_requires_every_judge():
    rows = []
    for key, grades in {"A|0|0|dream": (2, 2), "A|0|1|dream": (2, 1), "B|0|0|dream": (1, 1)}.items():
        for judge, g in zip(("j1", "j2"), grades):
            rows.append(dict(key=key, judge=judge, grade=g))
    rows.append(dict(key="B|0|1|dream", judge="j1", grade=2))  # only one judge: left out
    out = ab_report.retrieval_table(rows, ["A", "B"], 2)
    assert out["A"]["pairs"] == 2 and out["A"]["mean_grade"] == pytest.approx((2 + 1.5) / 2)
    assert out["A"]["share_2_by_all"] == 0.5 and out["B"]["pairs"] == 1


def test_answer_table_counts_wins_losses_ties_per_judge():
    rows = [dict(arm="B", index=i, judge=j, winner=w) for i, j, w in
            [(0, "j1", "B"), (0, "j2", "A"), (1, "j1", "tie"), (1, "j2", "B")]]
    out = ab_report.answer_table(rows, ["A", "B"], "A")["B"]
    assert (out["wins"], out["losses"], out["ties"], out["verdicts"], out["distinct_questions"]) == (2, 1, 1, 4, 2)
    assert out["win_rate_ties_half"] == pytest.approx(2.5 / 4)
    assert out["per_judge"]["j1"] == {"win": 1, "tie": 1}


def test_conclusion_table_and_dream_effort(tmp_path):
    rows = [dict(key="A|x", judge="j1", grade=2), dict(key="A|x", judge="j2", grade=1)]
    assert ab_report.conclusion_table(rows, ["A"], 2)["A"] == dict(n=1, mean_grade=1.5)
    folder = tmp_path / "A-part1"
    folder.mkdir()
    events = [dict(type="agent.tool.call.completed", data=dict(tool_name="search_memory")),
              dict(type="agent.tool.call.completed", data=dict(tool_name="create_observations_deductive")),
              dict(type="dream.run", data=dict(total_input_tokens=100)), dict(type="other", data={})]
    (folder / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    assert ab_report.dream_effort(tmp_path, ["A"], ["part1"])["A"] == dict(search_calls=1, input_tokens=100, create_calls=1)


# ------------------------------------------------------------------------------------------ blinding
def test_answer_pairs_are_blind_seeded_and_map_back_to_arms():
    base = {0: dict(query="q0", answer="from-A"), 1: dict(query="q1", answer="from-A1")}
    cand = {"B": [dict(index=0, query="q0", answer="from-B"), dict(index=1, query="changed", answer="x")]}
    pairs = ab_judge.make_answer_pairs(base, cand)
    assert len(pairs) == 1  # a question whose text differs is not paired
    again = ab_judge.make_answer_pairs(base, cand)
    assert pairs == again
    (p,) = pairs
    assert {p["X"], p["Y"]} == {"from-A", "from-B"} and p["candidate_is_x"] == (p["X"] == "from-B")
    x_wins = ab_judge.resolve_winner(p, "X", "A")
    assert x_wins == ("B" if p["candidate_is_x"] else "A")
    assert ab_judge.resolve_winner(p, "tie", "A") == "tie"
    with pytest.raises(ValueError):
        ab_judge.resolve_winner(p, "Z", "A")


def test_judge_pairs_skips_failures_and_never_shows_arm_names():
    base = {i: dict(query=f"q{i}", answer="from-A") for i in range(6)}
    cand = {"B": [dict(index=i, query=f"q{i}", answer="from-B") for i in range(6)]}
    pairs = ab_judge.make_answer_pairs(base, cand)
    seen = []

    def grader(system, user):
        seen.append(user)
        return None if len(seen) == 1 else "X"

    out = ab_judge.judge_pairs(pairs, grader, "j1", "A")
    assert len(out) == 5  # the failed request is skipped, not a tie
    assert all('"B"' not in u and "arm" not in u for u in seen)


def test_samples_are_fixed_by_seed():
    searches = [dict(caller="dream", query=f"q{i}", results=[dict(content="c")]) for i in range(50)]
    a = ab_judge.sample_retrieval_rows("A", searches, n=5)
    assert a == ab_judge.sample_retrieval_rows("A", searches, n=5)
    assert {r["key"].split("|")[0] for r in a} == {"A"} and len(a) == 5
    concl = [dict(id=f"i{i}", level="deductive", content="c") for i in range(30)]
    assert ab_judge.sample_conclusions("A", concl, n=4) == ab_judge.sample_conclusions("A", concl, n=4)
