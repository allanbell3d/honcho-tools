"""Arms, the in-memory index model, the rerank wrapper (fail open, min_score) and the PostgreSQL backend's SQL."""
import json
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import run_arms
from ledger import BudgetExceeded, Ledger
from retrieval import (
    FlakyReranker, LexicalReranker, MemoryBackend, PgBackend, RerankError, RerankResult, VoyageReranker, build_search_sql,
    parse_arm, parse_rerank_payload, run_arm, search_settings, token_bound,
)


def doc(i, observer="p", observed="p", level="explicit", vec=None, content=None):
    return dict(id=f"d{i}", workspace_name="w", observer=observer, observed=observed, level=level,
                content=content or f"text {i}", embedding=vec or [1.0, 0.01 * i])


def call(**kw):
    base = dict(call_id="c1", caller_type="dream.deduction", query="alpha beta", top_k=3, workspace_name="w",
                observer="p", observed="p", filters=None, max_distance=None)
    return {**base, **kw}


# ----------------------------------------------------------------------------------------------- arms
def test_parse_arm_grammar():
    assert parse_arm("stock").kind == "stock"
    scan = parse_arm("scan:250:strict_order")
    assert (scan.ef_search, scan.iterative) == (250, "strict_order")
    assert parse_arm("scan").iterative == "relaxed_order"
    rr = parse_arm("rerank:rerank-3-lite:100:250:strict_order", min_score=0.3)
    assert (rr.model, rr.pool, rr.ef_search, rr.iterative, rr.min_score) == ("rerank-3-lite", 100, 250, "strict_order", 0.3)
    for bad in ("scan:100:evil'; --", "rerank:m", "rerank:m:0", "exact:1", "wat"):
        with pytest.raises(ValueError):
            parse_arm(bad)


def test_search_settings_follow_the_study_defaults():
    assert search_settings(parse_arm("stock"), 10) == (10, None, "off")
    assert search_settings(parse_arm("scan"), 10) == (10, 40, "relaxed_order")
    assert search_settings(parse_arm("scan"), 60) == (60, 60, "relaxed_order")
    assert search_settings(parse_arm("rerank:m:100"), 10) == (100, 100, "relaxed_order")
    assert search_settings(parse_arm("rerank:m:5"), 10) == (10, 40, "relaxed_order")


# ------------------------------------------------------------------------------------ index model
def crowded_backend():
    """50 near documents of another peer pair crowd the query's 3 own documents out of the 40 nearest rows."""
    others = [doc(i, observer="q", observed="q", vec=[1.0, 0.001 * i]) for i in range(50)]
    mine = [doc(100 + i, vec=[1.0, 0.5 + 0.1 * i]) for i in range(3)]
    return MemoryBackend(others + mine)


def test_post_filter_underfills_and_iterative_scan_repairs_it():
    backend, c, v = crowded_backend(), call(top_k=3), [1.0, 0.0]
    stock = backend.search(c, v, 3, ef_search=5, iterative="off")
    scan = backend.search(c, v, 3, ef_search=5, iterative="relaxed_order")
    exact = backend.exact(c, v)
    assert len(stock) < 3  # filtered after the scan: the nearest rows belong to the other pair
    assert [h.id for h in scan] == [h.id for h in exact][:3] == ["d100", "d101", "d102"]


def test_filters_deleted_rows_and_max_distance():
    docs = [doc(1), doc(2, level="deductive"), dict(doc(3), deleted_at="2026-01-01"), doc(4, vec=[0.0, 1.0])]
    backend = MemoryBackend(docs)
    only_explicit = call(filters={"level": {"in": ["explicit"]}})
    assert {h.id for h in backend.exact(only_explicit, [1.0, 0.0])} == {"d1", "d4"}
    near = call(max_distance=0.1)
    assert {h.id for h in backend.exact(near, [1.0, 0.0])} == {"d1", "d2"}  # d3 deleted, d4 orthogonal
    for unsupported in ({"kind": "x"}, {"level": {"gt": 1}}):
        with pytest.raises(ValueError):
            backend.exact(call(filters=unsupported), [1.0, 0.0])


# ---------------------------------------------------------------------------------------- rerank wrapper
class Reverse:
    """A reranker that prefers later documents: easy to see that the order really came from it."""

    def rerank(self, query, documents):
        return RerankResult(order=[(i, float(i)) for i in range(len(documents))], tokens=7)


class Boom:
    def __init__(self, reason):
        self.reason = reason

    def rerank(self, query, documents):
        raise RerankError(self.reason)


def ten_docs():
    return MemoryBackend([doc(i, vec=[1.0, 0.01 * i]) for i in range(10)])


def test_rerank_widens_the_pool_reorders_and_cuts_to_top_k():
    row = run_arm(ten_docs(), Reverse(), call(top_k=3), [1.0, 0.0], parse_arm("rerank:m:10"))
    assert row["candidate_ids"] == [f"d{i}" for i in range(10)]  # the pool was 10, not 3
    assert row["ids"] == ["d9", "d8", "d7"] and row["outcome"] == "reranked" and row["fallback_reason"] is None
    assert row["rerank_tokens"] == 7 and row["rows_returned"] == 3 and row["rows_asked"] == 3
    assert {d["id"] for d in row["documents"]} >= {"d9", "d0", "d1", "d2"}  # judged pool: results + vector prefix


@pytest.mark.parametrize("reason", ["timeout", "http_error", "malformed_response"])
def test_any_reranker_failure_keeps_the_vector_order(reason):
    row = run_arm(ten_docs(), Boom(reason), call(top_k=3), [1.0, 0.0], parse_arm("rerank:m:10"))
    assert row["ids"] == ["d0", "d1", "d2"]
    assert row["fallback_reason"] == reason and row["outcome"] == reason


def test_a_budget_error_is_not_swallowed_as_a_fallback():
    class Broke:
        def rerank(self, q, d):
            raise BudgetExceeded("cap")

    with pytest.raises(BudgetExceeded):
        run_arm(ten_docs(), Broke(), call(), [1.0, 0.0], parse_arm("rerank:m:10"))


def test_min_score_drops_low_results_and_single_candidate_sends_no_request():
    arm = parse_arm("rerank:m:10", min_score=7.5)
    row = run_arm(ten_docs(), Reverse(), call(top_k=5), [1.0, 0.0], arm)
    assert row["ids"] == ["d9", "d8"]  # scores 9 and 8 pass; fewer than top_k is allowed
    one = MemoryBackend([doc(1)])
    row = run_arm(one, Boom("timeout"), call(top_k=3), [1.0, 0.0], parse_arm("rerank:m:10"))
    assert row["outcome"] == "noop_single" and row["fallback_reason"] is None and row["ids"] == ["d1"]


def test_stock_scan_and_exact_arms_do_not_touch_the_reranker():
    backend, c, v = crowded_backend(), call(top_k=3), [1.0, 0.0]
    stock = run_arm(backend, None, c, v, parse_arm("stock"))
    scan = run_arm(backend, None, c, v, parse_arm("scan:5"))
    exact = run_arm(backend, None, c, v, parse_arm("exact"))
    assert stock["rows_returned"] < 3 <= scan["rows_returned"] and exact["rows_asked"] == 200
    assert stock["provider_ms"] == 0 and stock["candidate_ids"] == []


def test_lexical_reranker_prefers_word_overlap_and_flaky_is_seeded():
    r = LexicalReranker().rerank("alpha beta", ["gamma", "alpha beta", "alpha"])
    best = max(r.order, key=lambda t: t[1])[0]
    assert best == 1
    flaky = FlakyReranker(LexicalReranker(), 1.0)
    with pytest.raises(RerankError):
        flaky.rerank("q", ["a"])
    never = FlakyReranker(LexicalReranker(), 0.0)
    assert never.rerank("q", ["a"]).order


def test_token_bound_counts_the_query_once_per_document():
    assert token_bound("ab", ["xyz", "é"]) == (2 + 3) + (2 + 2)


# ------------------------------------------------------------------------------- provider payloads and HTTP
def test_parse_rerank_payload_validates():
    ok = parse_rerank_payload({"data": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}],
                               "usage": {"total_tokens": 12}}, 2)
    assert ok.order == [(1, 0.9), (0, 0.1)] and ok.tokens == 12
    for bad in ({}, {"data": [{"index": 5, "relevance_score": 1}]},
                {"data": [{"index": 0, "relevance_score": 1}, {"index": 0, "relevance_score": 1}]},
                {"data": [{"index": 0, "relevance_score": float("nan")}]}, {"data": [{"index": 0}]}, [], None):
        with pytest.raises(RerankError) as info:
            parse_rerank_payload(bad, 2)
        assert info.value.reason == "malformed_response"


class Handler(BaseHTTPRequestHandler):
    mode = "ok"
    seen: dict = {}

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen = dict(path=self.path, auth=self.headers.get("Authorization"), body=body)
        if Handler.mode == "slow":
            time.sleep(1.0)
        if Handler.mode == "error":
            self.send_response(500)
            self.end_headers()
            return
        data = [{"index": i, "relevance_score": 1.0 - i / 10} for i in range(len(body["documents"]))]
        payload = json.dumps({"data": data, "usage": {"total_tokens": 5}} if Handler.mode == "ok" else {"oops": 1}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep the test output quiet
        pass


@pytest.fixture()
def server():
    httpd = HTTPServer(("localhost", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://localhost:{httpd.server_address[1]}/v1"
    httpd.shutdown()
    httpd.server_close()


def test_voyage_client_request_shape_and_success(server):
    Handler.mode = "ok"
    r = VoyageReranker("m", api_key="test-key", base_url=server, timeout_seconds=5).rerank("q", ["a", "b"])
    assert r.tokens == 5 and len(r.order) == 2
    assert Handler.seen["path"] == "/v1/rerank" and Handler.seen["auth"] == "Bearer test-key"
    assert Handler.seen["body"] == {"query": "q", "documents": ["a", "b"], "model": "m"}


@pytest.mark.parametrize("mode,reason", [("error", "http_error"), ("bad", "malformed_response"), ("slow", "timeout")])
def test_voyage_client_failures_map_to_fallback_reasons(server, mode, reason):
    Handler.mode = mode
    client = VoyageReranker("m", api_key="k", base_url=server, timeout_seconds=0.3)
    with pytest.raises(RerankError) as info:
        client.rerank("q", ["a"])
    assert info.value.reason == reason


def test_voyage_client_needs_a_key():
    with pytest.raises(ValueError):
        VoyageReranker("m", api_key="")


def test_paid_reranker_reserves_settles_and_charges_failures(tmp_path):
    ledger = Ledger(tmp_path / "l.jsonl", cap_usd=1)
    paid = run_arms.PaidReranker(Reverse(), ledger, "rerank", "m", 1.0)
    paid.rerank("q", ["abc", "de"])  # tokens reported by the reranker: 7
    assert float(ledger.committed) == pytest.approx(7 / 1e6)
    broke = run_arms.PaidReranker(Boom("http_error"), ledger, "rerank", "m", 1.0)
    before = ledger.committed
    with pytest.raises(RerankError):
        broke.rerank("q", ["abc"])
    assert float(ledger.committed - before) == pytest.approx(token_bound("q", ["abc"]) / 1_000_000)  # full reservation kept
    tiny = Ledger(tmp_path / "t.jsonl", cap_usd="0.000001")
    with pytest.raises(BudgetExceeded):
        run_arms.PaidReranker(Reverse(), tiny, "rerank", "m", 1.0).rerank("q", ["abc"])


# ----------------------------------------------------------------------------------------- PostgreSQL SQL
def test_search_sql_binds_values_and_hides_distance_for_the_exact_ceiling():
    sql = build_search_sql(exact=False, has_levels=True, has_max_distance=True)
    assert "deleted_at IS NULL" in sql and "level = ANY(%(levels)s)" in sql and "<= %(max_distance)s" in sql
    assert "LIMIT %(limit)s" in sql and "+ 0" not in sql
    exact = build_search_sql(exact=True, has_levels=False, has_max_distance=False)
    assert "ORDER BY (embedding <=> %(vector)s::vector) + 0, id" in exact and "level" not in exact


def test_pg_backend_sets_index_options_inside_a_read_only_transaction(monkeypatch):
    log: list = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def transaction(self):
            return self

        def execute(self, sql, params=None):
            log.append((sql, params))
            return types.SimpleNamespace(fetchall=lambda: [("d1", "text", 0.9)])

    fake = types.SimpleNamespace(connect=lambda dsn, options: (log.append(("connect", options)), Conn())[1])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    hits = PgBackend("postgresql://x").search(call(), [0.1, 0.2], 5, 250, "strict_order")
    assert hits[0].id == "d1"
    assert log[0] == ("connect", "-c default_transaction_read_only=on")
    assert log[1][0] == "SET LOCAL hnsw.ef_search = 250"
    assert log[2][0] == "SET LOCAL hnsw.iterative_scan = 'strict_order'"
    stock_log = log.copy()
    log.clear()
    PgBackend("postgresql://x").search(call(), [0.1, 0.2], 5, None, "off")
    assert not any("SET LOCAL" in str(s) for s, _ in log[1:])  # the stock arm changes nothing
    assert stock_log
    with pytest.raises(ValueError):
        PgBackend("postgresql://x").search(call(), [0.1], 5, 40, "x'; drop table documents; --")


def test_dry_run_plans_a_cost_without_any_request(tmp_path, capsys):
    from benchlib import write_jsonl

    write_jsonl(tmp_path / "c.jsonl", [call(call_id="a", top_k=10), call(call_id="b", top_k=10)])
    (tmp_path / "rates.json").write_text(json.dumps({"rerank-3-lite": 0.02, "_note": "x"}))
    code = run_arms.main(["--calls", str(tmp_path / "c.jsonl"), "--embeddings", "unused", "--out", "unused",
                          "--arms", "stock", "rerank:rerank-3-lite:100", "--rates", str(tmp_path / "rates.json"),
                          "--dry-run"])
    plan = json.loads(capsys.readouterr().out)
    assert code == 0 and plan["dry_run"]
    entry = plan["rerank"]["rerank:rerank-3-lite:100"]
    assert entry["requests"] == 2 and entry["estimated_tokens"] == 2 * 100 * 48
    assert entry["estimated_usd"] == pytest.approx(2 * 100 * 48 * 0.02 / 1e6, abs=1e-6)


def test_run_arms_is_resumable(tmp_path):
    from benchlib import append_jsonl, read_jsonl, write_jsonl

    data = tmp_path
    write_jsonl(data / "corpus.jsonl", [doc(i) for i in range(5)])
    c = call(call_id="only", query="text 1")
    write_jsonl(data / "calls.jsonl", [c])
    from retrieval import embedding_key

    append_jsonl(data / "emb.jsonl", dict(cache_key=embedding_key("synthetic-16", 16, "text 1"), embedding=[1.0, 0.0]))
    argv = ["--calls", str(data / "calls.jsonl"), "--embeddings", str(data / "emb.jsonl"), "--corpus", str(data / "corpus.jsonl"),
            "--arms", "stock", "scan", "--out", str(data / "r.jsonl")]
    run_arms.main(argv)
    run_arms.main(argv)  # nothing new to write
    assert [r["arm"] for r in read_jsonl(data / "r.jsonl")] == ["stock", "scan"]
