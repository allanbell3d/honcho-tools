"""The published results: RESULTS.md matches its JSON, checksums hold, the JSON is internally consistent and clean."""
import hashlib
import json

import extract_published
import render_results
from conftest import BENCH

RESULTS = BENCH / "results"


def load(name):
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def test_results_md_is_exactly_what_the_renderer_prints():
    committed = (RESULTS / "RESULTS.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == render_results.render()
    assert render_results.main(["--check"]) == 0


def test_checksums_cover_every_published_file_and_match():
    listed = {}
    for line in (RESULTS / "CHECKSUMS.sha256").read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        listed[name] = digest
    published = {p.name for p in RESULTS.iterdir() if p.is_file() and p.name != "CHECKSUMS.sha256"}
    assert set(listed) == published
    for name, digest in listed.items():  # line endings may differ after a checkout, so hash normalised bytes
        data = (RESULTS / name).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == digest, name


def test_published_json_carries_no_paths_addresses_keys_or_ids():
    for path in RESULTS.glob("*.json"):
        assert extract_published.check(path) == [], path.name


def test_study1_internal_consistency():
    d = load("study1_conclusion_replay.json")
    assert d["calls"] == 80 and d["labels"] == 31663
    assert sum(d["grade_distribution"].values()) == d["labels"]
    for c in d["supplementary_comparisons"]:
        assert sum(c["precision_call_outcomes"].values()) == c["calls"] == 80
        assert c["precision"]["clusters"] == 43 and c["precision"]["seed"] == 42 and c["precision"]["iterations"] == 2000
    ops = {o["arm"]: o for o in d["operational"]}
    assert all(o["fallbacks"] == 0 and o["errors"] == 0 and o["calls"] == 80 for o in ops.values())
    assert all(o["returned_rows"] == o["requested_rows"] for o in ops.values())
    # list price follows tokens: price per 1,000 calls = tokens per call x rate / 1e6 x 1,000
    rate = load("costs.json")["rates_used_usd_per_million_tokens"]["rerank-3-lite"]
    for arm in ("rr100", "rr200", "rr300", "rr400"):
        assert abs(ops[arm]["list_price_usd_per_1000_calls"] - ops[arm]["rerank_tokens_per_call"] * rate / 1000) < 1e-6
    metric = {(m["caller"], m["arm"], m["threshold"], m["k"]): m for m in d["metrics"]}
    gain = metric[("overall", "rr100", 2, "caller_k")]["precision"] - metric[("overall", "ef250", 2, "caller_k")]["precision"]
    sup = next(c for c in d["supplementary_comparisons"] if (c["left"], c["right"]) == ("ef250", "rr100"))
    assert abs(gain - sup["precision"]["delta"]) < 1e-9
    assert sup["precision"]["ci95"][0] > 0  # the interval is above zero


def test_study2_internal_consistency():
    d = load("study2_three_judge_replay.json")
    assert d["coverage"]["complete_calls"] == 270 and d["primary_source"] == "majority"
    overall = {m["arm"]: m for m in d["metrics"] if m["caller"] == "overall"}
    assert overall["rerank:rerank-3-lite:100"]["fallback_rate"] < 0.05 and overall["stock"]["fallback_rate"] == 0
    for a in d["judge_agreement"]:
        assert a["pairs"] == sum(sum(r) for r in a["confusion"])
        assert abs(a["exact_agreement"] - sum(a["confusion"][i][i] for i in range(3)) / a["pairs"]) < 1e-6
    assert d["decision"]["verdict"] == "inconclusive"


def test_study3_and_study4_internal_consistency():
    d = load("study3_ef_sweep.json")
    for arm, groups in d["arms"].items():
        parts = sum(v["searches"] for k, v in groups.items() if k != "all")
        assert parts == groups["all"]["searches"] == 6463, arm
    assert d["arms"]["ef400"]["all"]["recall"] == 1.0 and d["arms"]["stock"]["all"]["recall"] < 0.95
    ab = load("study4_ab_dream.json")
    for arm, w in ab["answers_vs_baseline"].items():
        assert w["wins"] + w["losses"] + w["ties"] == w["verdicts"] == sum(sum(j.values()) for j in w["per_judge"].values())
