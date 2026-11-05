"""The synthetic dataset is reproducible, and the whole pipeline runs offline end to end with a pinned result."""
import hashlib
import json

import pytest

import extract_published
import make_synthetic
import run_synthetic_demo
from benchlib import read_jsonl
from conftest import SYNTHETIC

FILES = ("corpus.jsonl", "calls.jsonl", "embeddings.jsonl", "relevance.jsonl")


def test_generator_is_deterministic_and_matches_the_committed_files(tmp_path):
    digests = make_synthetic.write_all(tmp_path)
    assert digests == make_synthetic.write_all(tmp_path / "again")
    for name in FILES:
        committed = (SYNTHETIC / name).read_bytes().replace(b"\r\n", b"\n")  # a checkout may convert line endings
        assert committed == (tmp_path / name).read_bytes(), f"{name} differs from the generator output"
        assert hashlib.sha256(committed).hexdigest() == digests[name]


def test_a_different_seed_gives_a_different_dataset(tmp_path):
    other = make_synthetic.write_all(tmp_path, seed=make_synthetic.SEED + 1)
    assert other["corpus.jsonl"] != hashlib.sha256((SYNTHETIC / "corpus.jsonl").read_bytes()).hexdigest()


def test_dataset_invariants():
    corpus, calls = read_jsonl(SYNTHETIC / "corpus.jsonl"), read_jsonl(SYNTHETIC / "calls.jsonl")
    relevance, vectors = read_jsonl(SYNTHETIC / "relevance.jsonl"), read_jsonl(SYNTHETIC / "embeddings.jsonl")
    assert len(calls) == 90 and len(corpus) == 570
    assert len({d["content"] for d in corpus}) == len(corpus)  # unique text: the fake judges key on it
    assert len({c["query"] for c in calls}) == len(calls)
    assert len({v["cache_key"] for v in vectors}) == len(vectors) == len(calls)
    assert {c["caller_type"] for c in calls} == {"dream.deduction", "dream.induction", "dialectic", "dialectic_prefetch"}
    assert all(len(d["embedding"]) == make_synthetic.DIM for d in corpus)
    by_id = {d["id"]: d for d in corpus}
    for row in relevance:
        call = next(c for c in calls if c["call_id"] == row["call_id"])
        doc = by_id[row["document_id"]]
        assert (doc["observer"], doc["observed"]) == (call["observer"], call["observed"])
        assert row["grade"] in (1, 2)
    peers = {d["observer"] for d in corpus} | {d["observed"] for d in corpus}
    assert peers == {"peer-A", "peer-B", "peer-C"}


def test_pipeline_end_to_end_reproduces_the_pinned_headline(tmp_path):
    expected = json.loads((SYNTHETIC / "expected-headline.json").read_text(encoding="utf-8"))
    first = run_synthetic_demo.run_demo(tmp_path / "one")
    second = run_synthetic_demo.run_demo(tmp_path / "two")
    assert first == second == expected
    for name in ("rankings.jsonl", "report.json", "REPORT.md", "ef-sweep.json"):
        assert (tmp_path / "one" / name).exists()


def test_demo_shows_the_effects_it_was_built_to_show(tmp_path):
    h = run_synthetic_demo.run_demo(tmp_path)
    ndcg = h["overall_ndcg_at_k"]
    assert ndcg["rerank:lexical:100"] > ndcg["stock"] + 0.03  # reranking helps on this constructed data
    assert ndcg["rerank:lexical:100+vector-prefix"] == pytest.approx(ndcg["scan"], abs=0.02)  # same pool, no rerank
    assert h["scan_recall_vs_exact"] > h["stock_recall_vs_exact"]  # the index model post-filters at the default
    assert h["stock_underfilled"] > h["exact_underfilled"] == 0
    assert h["overall_fallback_rate"]["rerank:lexical:100"] > 0.05  # the injected failures exercise fail-open
    assert h["decision"] == "inconclusive"  # missing guardrail evidence can never pass


def test_demo_outputs_contain_no_secrets_or_paths(tmp_path):
    run_synthetic_demo.run_demo(tmp_path)
    for name in ("report.json", "ef-sweep.json"):
        assert extract_published.check(tmp_path / name) == []


def test_extract_check_flags_leaks(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"p": "C:\\Users\\someone\\x", "ip": "10.1.2.3", "m": "a@b.example", "k": "sk-abcdefgh12345678"}))
    assert len(extract_published.check(bad)) >= 4
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"recall": 0.9, "arm": "rerank:rerank-3-lite:100"}))
    assert extract_published.check(good) == []


def test_extractors_keep_only_allow_listed_fields():
    sweep = dict(arms={"x": {"all": dict(searches=1, searches_with_exact_result=1, recall=1.0, identical_to_exact=1.0,
                                         underfilled=0.0, fill=1.0, db_ms_median=1.0, db_ms_p95=2.0, secret="leak")}},
                 exact_underfilled=0.0)
    out = extract_published.study3(sweep)
    assert "secret" not in json.dumps(out)
    ab = dict(arms=["A"], baseline="A", retrieval={}, new_conclusions={}, answers_vs_baseline={}, query_text="leak")
    assert "query_text" not in extract_published.study4(ab)


def test_subset_names_are_neutral_unless_allow_listed():
    assert extract_published._subset("dream_callers_only") == "dream_callers_only"
    assert extract_published._subset("exclude_whatever_a_query_said") == "exclude_one_nonspecific_call"
