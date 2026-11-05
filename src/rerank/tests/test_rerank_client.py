"""Offline provider, accounting and fail-open reranking checks."""

# pyright: reportPrivateUsage=false, reportPrivateLocalImportUsage=false
import asyncio
import json
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cashews import Cache
from pydantic import SecretStr, ValidationError

import honcho_rerank.client as rerank
import honcho_rerank.settings as config
import src.telemetry.events as telemetry_events


@pytest.fixture
def setup_rerank(monkeypatch: pytest.MonkeyPatch) -> Any:
    options = cast(Any, config.RerankSettings)(
        ENABLED=True, API_KEY=SecretStr("test-key")
    )
    monkeypatch.setattr(
        rerank,
        "settings",
        SimpleNamespace(RERANK=options, CACHE=SimpleNamespace(ENABLED=False)),
    )
    state: tuple[tuple[str, Any], ...] = (
        ("_local_counts", {}),
        ("_circuits", {}),
        ("_cap_hints", set[tuple[str, int]]()),
    )
    for name, value in state:
        monkeypatch.setattr(rerank, name, value)
    monkeypatch.setattr(rerank, "_utc_day", lambda: "2026-10-31")
    events: list[Any] = []
    monkeypatch.setattr(telemetry_events, "emit", events.append)
    return options, events


@pytest.fixture
def documents() -> Any:
    return [
        SimpleNamespace(content=text)
        for text in ("first-private-text", "second-private-text", "third-private-text")
    ]


def rows(score: str = "relevance_score", top_n: int | None = 2) -> list[dict[str, Any]]:
    """Unsorted on purpose; a provider that takes top_n returns that many rows."""
    if top_n is None:
        return [
            {"index": 1, score: 0.4},
            {"index": 2, score: 0.9},
            {"index": 0, score: 0.7},
        ]
    return [{"index": 0, score: 0.7}, {"index": 2, score: 0.9}][:top_n]


def failing_client(failure: BaseException) -> Any:
    """A client whose request raises before any response exists."""
    return SimpleNamespace(stream=MagicMock(side_effect=failure))


def body(provider: str = "voyage") -> Any:
    if provider == "tei":
        return rows("score", None)
    result: dict[str, Any] = {"data" if provider == "voyage" else "results": rows()}
    if provider == "cohere":
        result["meta"] = {"billed_units": {"search_units": 1.5}}
    else:
        result["usage"] = {"total_tokens": 7}
    return result


async def invoke(
    monkeypatch: pytest.MonkeyPatch,
    documents: Any,
    payload: Any,
    top_k: int = 2,
    status: int = 200,
) -> Any:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status,
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(rerank, "_get_client", lambda: client)
        result = await rerank.rerank_documents(
            "private-query", documents, top_k, "dream"
        )
    return result, requests


@pytest.mark.parametrize(
    "provider,url",
    [
        ("voyage", "https://api.voyageai.com/v1/rerank"),
        ("cohere", "https://api.cohere.com/v2/rerank"),
        ("jina", "https://api.jina.ai/v1/rerank"),
        ("tei", "https://reranker.example/rerank"),
    ],
)
async def test_adapters(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,
    url: str,
) -> None:
    options, events = setup_rerank
    options.PROVIDER = provider
    if provider == "tei":
        options.BASE_URL = "https://reranker.example"
    result, requests = await invoke(monkeypatch, documents, body(provider))
    assert result == [documents[2], documents[0]]
    assert len(requests) == 1 and str(requests[0].url) == url
    assert requests[0].headers["authorization"] == "Bearer test-key"
    expected: dict[str, Any] = {"query": "private-query"}
    if provider == "tei":
        expected.update(texts=[d.content for d in documents], truncate=True)
    else:
        expected.update(model=options.MODEL, documents=[d.content for d in documents])
        expected["top_k" if provider == "voyage" else "top_n"] = 2
        if provider != "cohere":
            expected["return_documents"] = False
    assert json.loads(requests[0].content) == expected
    assert len(events) == 1
    event = events[0]
    assert (event.provider, event.model, event.outcome) == (
        provider,
        options.MODEL,
        "reranked",
    )
    assert (event.candidates_sent, event.results_returned) == (3, 2)
    assert event.tokens == (7 if provider in ("voyage", "jina") else None)
    assert event.billed_units == (1.5 if provider == "cohere" else None)
    assert event.daily_requests is None and event.daily_request_cap is None
    assert event.duration_ms >= 0


@pytest.mark.parametrize("provider", ["voyage", "cohere", "jina", "tei"])
@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("key", [None, " ", "test-key"])
async def test_keys_and_override(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,
    override: bool,
    key: str | None,
) -> None:
    options, events = setup_rerank
    options.PROVIDER = provider
    options.API_KEY = SecretStr(key) if key is not None else None
    options.BASE_URL = "https://reranker.example/custom/" if override else None
    required = provider != "tei" and not override and not (key or "").strip()
    if provider == "tei" and not override:
        # tei has no hosted URL, so it skips before any key check.
        assert rerank.skip_reason() == "skipped_missing_url"
        return
    assert rerank.skip_reason() == ("skipped_missing_key" if required else None)
    result, requests = await invoke(monkeypatch, documents, body(provider))
    assert events[0].outcome == ("skipped_missing_key" if required else "reranked")
    if required:
        assert requests == [] and result == documents[:2]
        assert events[0].candidates_sent == 0 and events[0].tokens is None
    else:
        assert result == [documents[2], documents[0]]
        if override:
            assert str(requests[0].url) == "https://reranker.example/custom/rerank"
        assert ("authorization" in requests[0].headers) == bool((key or "").strip())


@pytest.mark.parametrize("provider", ["voyage", "jina", "cohere"])
@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"total_tokens": -1},
        {"total_tokens": True},
        {"total_tokens": "7"},
        {"total_tokens": float("inf")},
    ],
)
async def test_optional_usage(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,
    usage: Any,
) -> None:
    options, events = setup_rerank
    options.PROVIDER = provider
    payload = body(provider)
    payload.pop("meta", None)
    payload["usage"] = usage
    result, _ = await invoke(monkeypatch, documents, payload)
    assert result == [documents[2], documents[0]]
    assert events[0].tokens is None and events[0].billed_units is None


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        [],
        [{"index": 0, "relevance_score": 1}],
        [{"index": 0, "relevance_score": 1}, {"index": 0, "relevance_score": 0.2}],
        [{"index": 99, "relevance_score": 1}],
        [{"index": True, "relevance_score": 1}],
        [{"index": 0, "relevance_score": float("nan")}],
        [{"index": 0, "relevance_score": True}],
    ],
)
async def test_malformed_ranking(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    _, events = setup_rerank
    # json.dumps writes NaN and Infinity, and the client's parser reads them back.
    result, _ = await invoke(monkeypatch, documents, {"data": bad})
    assert result == documents[:2]
    assert events[0].outcome == "malformed_response"


async def test_min_score(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, _ = setup_rerank
    options.MIN_SCORE = 0.8
    result, _ = await invoke(monkeypatch, documents, body())
    assert result == [documents[2]]


async def test_max_documents(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, events = setup_rerank
    options.MAX_DOCUMENTS = 2
    result, requests = await invoke(
        monkeypatch, documents, {"data": rows()[:1]}, top_k=3
    )
    assert result == documents and not requests
    assert (
        events[-1].outcome == "too_many_candidates" and events[-1].candidates_sent == 0
    )
    payload = {
        "data": [
            {"index": 1, "relevance_score": 1},
            {"index": 0, "relevance_score": 0.5},
        ]
    }
    result, requests = await invoke(monkeypatch, documents, payload)
    assert result == [documents[1], documents[0]]
    assert len(json.loads(requests[0].content)["documents"]) == 2


@pytest.mark.parametrize(
    "provider,_limit", [("voyage", 1000), ("cohere", 1000), ("jina", 1000), ("tei", 32)]
)
def test_candidate_defaults(setup_rerank: Any, provider: Any, _limit: int) -> None:
    options, _ = setup_rerank
    options.PROVIDER = provider
    options.CANDIDATES = 1000
    # The pool never exceeds what one request takes, but never drops below top_k.
    assert rerank.max_documents() == _limit
    assert rerank.candidate_count(2) == _limit
    assert rerank.candidate_count(1001) == 1001
    options.MAX_DOCUMENTS = 50
    assert rerank.candidate_count(2) == 50


async def test_cap_counts_once_and_memo_resets(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, events = setup_rerank
    options.DAILY_REQUEST_CAP = 1
    for expected in ("reranked", "skipped_cap", "skipped_cap"):
        await invoke(monkeypatch, documents, body())
        assert events[-1].outcome == expected
    assert rerank._local_counts == {"2026-10-31": 2}
    assert events[-1].daily_requests is None
    options.DAILY_REQUEST_CAP = 3
    assert rerank.skip_reason() is None
    await invoke(monkeypatch, documents, body())
    assert events[-1].daily_requests == 3
    monkeypatch.setattr(rerank, "_utc_day", lambda: "2026-11-01")
    assert rerank.skip_reason() is None
    await invoke(monkeypatch, documents, body())
    assert events[-1].daily_requests == 1


async def test_zero_and_no_cap_never_count(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, events = setup_rerank
    counter = AsyncMock(side_effect=AssertionError("must not count"))
    monkeypatch.setattr(rerank, "_requests_for_day", counter)
    for cap, outcome in ((0, "skipped_cap"), (None, "reranked")):
        options.DAILY_REQUEST_CAP = cap
        await invoke(monkeypatch, documents, body())
        assert events[-1].outcome == outcome and events[-1].daily_requests is None
    counter.assert_not_awaited()


@pytest.mark.parametrize(
    "failure,outcome",
    [
        (httpx.ReadTimeout("private-query test-key"), "timeout"),
        (httpx.ConnectError("private-query test-key"), "http_error"),
        (RuntimeError("private-query test-key"), "internal_error"),
    ],
)
async def test_failures_stay_counted_and_private(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    outcome: str,
) -> None:
    options, events = setup_rerank
    options.DAILY_REQUEST_CAP = 10
    monkeypatch.setattr(
        rerank,
        "_get_client",
        lambda: failing_client(failure),
    )
    with caplog.at_level("INFO"):
        assert (
            await rerank.rerank_documents("private-query", documents, 2, "dream")
            == documents[:2]
        )
    assert rerank._local_counts == {"2026-10-31": 1}
    assert events[-1].outcome == outcome and events[-1].daily_requests == 1
    serialized = events[-1].model_dump_json() + caplog.text
    for secret in ("private-query", "test-key", "first-private-text", "https://"):
        assert secret not in serialized


async def test_http_status_failure(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, events = setup_rerank
    result, requests = await invoke(
        monkeypatch, documents, {"error": "private-query"}, status=503
    )
    assert result == documents[:2] and len(requests) == 1
    assert events[-1].outcome == "http_error"


@pytest.mark.parametrize(
    "available,result",
    [
        (False, 1),
        (True, None),
        (True, True),
        (True, -1),
    ],
)
async def test_unavailable_shared_counter(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
    result: Any,
) -> None:
    options, events = setup_rerank
    options.DAILY_REQUEST_CAP = 5
    assert setup_rerank is not None
    rerank.settings.CACHE.ENABLED = True
    fake = SimpleNamespace(incr=AsyncMock(return_value=result), expire=AsyncMock())
    monkeypatch.setattr(rerank, "cache", fake)
    monkeypatch.setattr(
        rerank, "_redis_counter_available", MagicMock(return_value=available)
    )
    result, requests = await invoke(monkeypatch, documents, body())
    assert result == documents[:2] and not requests
    assert events[-1].outcome == "skipped_counter" and events[-1].daily_requests is None


async def test_redis_increment_and_expiry(
    setup_rerank: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert setup_rerank is not None
    rerank.settings.CACHE.ENABLED = True
    fake = SimpleNamespace(
        incr=AsyncMock(return_value=4), expire=AsyncMock(return_value=True)
    )
    monkeypatch.setattr(rerank, "cache", fake)
    monkeypatch.setattr(
        rerank, "_redis_counter_available", MagicMock(return_value=True)
    )
    assert await rerank._requests_for_day("2026-10-31") == 4
    key = rerank._counter_key("2026-10-31")
    assert key.endswith(":rerank:requests:2026-10-31")
    fake.incr.assert_awaited_once_with(
        key, value=1, expire=rerank.COUNTER_EXPIRY_SECONDS
    )
    fake.expire.assert_not_awaited()


async def test_in_process_retains_two_days(setup_rerank: Any) -> None:
    assert setup_rerank is not None
    for day in ("2026-10-29", "2026-10-30", "2026-10-31"):
        assert await rerank._requests_for_day(day) == 1
    assert rerank._local_counts == {"2026-10-30": 1, "2026-10-31": 1}


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "http_error",
        "malformed_response",
        "internal_error",
        "skipped_counter",
    ],
)
def test_configured_cooldown(
    setup_rerank: Any, monkeypatch: pytest.MonkeyPatch, failure: Any
) -> None:
    options, _ = setup_rerank
    options.FAILURES_BEFORE_COOLDOWN = 2
    options.COOLDOWN_SECONDS = 7
    monkeypatch.setattr(rerank.time, "monotonic", lambda: 100.0)
    rerank._record_outcome(failure, "2026-10-31", None)
    assert rerank.skip_reason() is None
    rerank._record_outcome("reranked", "2026-10-31", None)
    rerank._record_outcome(failure, "2026-10-31", None)
    assert rerank.skip_reason() is None
    rerank._record_outcome(failure, "2026-10-31", None)
    assert rerank.skip_reason() == "skipped_cooldown"
    assert rerank._circuit(options).failures == 2
    rerank._record_outcome("skipped_cooldown", "2026-10-31", None)
    assert rerank._circuit(options).failures == 2
    monkeypatch.setattr(rerank.time, "monotonic", lambda: 108.0)
    assert rerank.skip_reason() is None
    assert rerank._circuit(options).failures == 2
    rerank._record_outcome(failure, "2026-10-31", None)
    assert rerank.skip_reason() == "skipped_cooldown"
    assert rerank._circuit(options).failures == 3
    rerank._record_outcome("reranked", "2026-10-31", None)
    assert rerank._circuit(options).failures == 0


async def test_settings_proxy_is_authoritative(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, events = setup_rerank
    alternate = cast(Any, config.RerankSettings)(
        ENABLED=True,
        PROVIDER="tei",
        MODEL="proxy-model",
        BASE_URL="https://reranker.example",
        CANDIDATES=8,
        DAILY_REQUEST_CAP=1,
        CALLERS=["conclusions_api"],
    )
    monkeypatch.setattr(
        rerank,
        "settings",
        SimpleNamespace(RERANK=alternate, CACHE=SimpleNamespace(ENABLED=False)),
    )
    assert not rerank.should_rerank("dream") and rerank.should_rerank("conclusions_api")
    assert rerank.candidate_count(2) == 8
    # The low-level entry point still gates on the selected caller.
    alternate.CALLERS = ["dream"]
    await invoke(monkeypatch, documents, body("tei"))
    assert (
        events[-1].provider,
        events[-1].model,
        events[-1].daily_request_cap,
        events[-1].daily_requests,
    ) == ("tei", "proxy-model", 1, 1)
    await invoke(monkeypatch, documents, body("tei"))
    assert events[-1].outcome == "skipped_cap"


async def test_disabled_and_unlisted_emit_nothing(
    setup_rerank: Any, documents: Any
) -> None:
    options, events = setup_rerank
    for caller in ("dedupe", "unlisted", None):
        assert not rerank.should_rerank(caller)
        if caller:
            assert (
                await rerank.rerank_documents("q", documents, 2, caller)
                == documents[:2]
            )
    options.ENABLED = False
    assert await rerank.rerank_documents("q", documents, 2, "dream") == documents[:2]
    assert not events


@pytest.mark.parametrize(
    "reason", ["skipped_transaction", "empty_result", "skipped_cooldown"]
)
def test_integration_fallback_event(
    setup_rerank: Any, documents: Any, reason: Any
) -> None:
    _, events = setup_rerank
    assert rerank.record_rerank_fallback("dream", documents, 2, reason) == documents[:2]
    event = events[0]
    assert event.outcome == reason and event.candidates_sent == 0
    assert (
        event.tokens is None
        and event.billed_units is None
        and event.daily_requests is None
    )
    assert event.workspace_name is None and event.run_id is None


def test_fallback_event_carries_workspace_and_run(
    setup_rerank: Any, documents: Any
) -> None:
    _, events = setup_rerank
    rerank.record_rerank_fallback(
        "dream", documents, 2, "empty_result", workspace_name="ws", run_id="run-1"
    )
    assert (events[0].workspace_name, events[0].run_id) == ("ws", "run-1")


async def test_reranked_event_carries_workspace_and_run(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, events = setup_rerank

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(rerank, "_get_client", lambda: client)
        await rerank.rerank_documents(
            "q", documents, 2, "dialectic", workspace_name="ws", run_id="run-2"
        )
    assert len(events) == 1
    event = events[0]
    assert (event.outcome, event.workspace_name, event.run_id) == (
        "reranked",
        "ws",
        "run-2",
    )


@pytest.mark.parametrize(
    "values",
    [
        {"PROVIDER": "unknown"},
        {"MAX_DOCUMENTS": 0},
        {"MAX_DOCUMENTS": 10001},
        {"DAILY_REQUEST_CAP": -1},
        {"FAILURES_BEFORE_COOLDOWN": 0},
        {"COOLDOWN_SECONDS": -1},
        {"TIMEOUT_SECONDS": 0},
        {"MIN_SCORE": float("nan")},
        {"BASE_URL": "ftp://reranker.example"},
        {"BASE_URL": "https://["},
        {"BASE_URL": "https://reranker.example:bad"},
    ],
)
def test_invalid_settings(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        cast(Any, config.RerankSettings)(**values)


def test_memory_cache_is_not_redis(
    setup_rerank: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert setup_rerank is not None
    memory = Cache()
    memory.setup("mem://")
    monkeypatch.setattr(rerank, "cache", memory)
    assert not rerank._redis_counter_available("test")


async def test_client_is_reused_and_closed(setup_rerank: Any) -> None:
    assert setup_rerank is not None
    first = rerank._get_client()
    assert rerank._get_client() is first
    await rerank.close_rerank_client()
    assert first.is_closed
    assert asyncio.get_running_loop() not in rerank._clients


@pytest.mark.parametrize("provider", ["voyage", "cohere", "jina", "tei"])
async def test_adapter_rejects_duplicate_indices(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch, provider: Any
) -> None:
    options, events = setup_rerank
    options.PROVIDER = provider
    options.BASE_URL = "https://reranker.example"
    ranking = [
        {"index": 0, "score" if provider == "tei" else "relevance_score": 0.9}
    ] * 2
    payload = (
        ranking
        if provider == "tei"
        else {"data" if provider == "voyage" else "results": ranking}
    )
    result, _ = await invoke(monkeypatch, documents, payload)
    assert result == documents[:2] and events[-1].outcome == "malformed_response"


async def test_counter_exception_and_deadline(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, events = setup_rerank
    options.DAILY_REQUEST_CAP = 2
    counter = AsyncMock(side_effect=RuntimeError("private-query test-key"))
    monkeypatch.setattr(rerank, "_requests_for_day", counter)
    result, requests = await invoke(monkeypatch, documents, body())
    assert result == documents[:2] and not requests
    assert events[-1].outcome == "skipped_counter"
    options.TIMEOUT_SECONDS = 0.01

    async def slow(_day: str) -> int:
        await asyncio.sleep(1)
        return 1

    monkeypatch.setattr(rerank, "_requests_for_day", slow)
    result, requests = await invoke(monkeypatch, documents, body())
    assert result == documents[:2] and not requests
    # The provider was never called, so the stall is the counter's failure.
    assert events[-1].outcome == "skipped_counter"
    assert events[-1].candidates_sent == 0
    assert rerank._circuit(options).failures == 2


async def test_empty_input_has_null_usage(
    setup_rerank: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, events = setup_rerank
    result, requests = await invoke(monkeypatch, [], body())
    assert result == [] and not requests
    assert events[-1].outcome == "empty_result"
    assert events[-1].tokens is None and events[-1].daily_requests is None


@pytest.mark.parametrize(
    "provider,limit", [("voyage", 1000), ("cohere", 1000), ("jina", 1000), ("tei", 32)]
)
async def test_provider_default_document_limit(
    setup_rerank: Any, monkeypatch: pytest.MonkeyPatch, provider: Any, limit: int
) -> None:
    options, events = setup_rerank
    options.PROVIDER = provider
    options.BASE_URL = "https://reranker.example"
    documents = [SimpleNamespace(content="test-text") for _ in range(limit + 1)]
    ranking = [{"index": 0, "score" if provider == "tei" else "relevance_score": 0.9}]
    payload = (
        ranking
        if provider == "tei"
        else {"data" if provider == "voyage" else "results": ranking}
    )
    result, requests = await invoke(monkeypatch, documents, payload, top_k=1)
    assert result == documents[:1]
    sent = json.loads(requests[0].content)
    assert len(sent["texts" if provider == "tei" else "documents"]) == limit
    assert events[-1].candidates_sent == limit


async def test_overflowing_billed_units_are_optional(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, events = setup_rerank
    options.PROVIDER = "cohere"
    payload = body("cohere")
    payload["meta"]["billed_units"]["search_units"] = 10**400
    result, _ = await invoke(monkeypatch, documents, payload)
    assert result == [documents[2], documents[0]]
    assert events[-1].outcome == "reranked"
    assert events[-1].billed_units is None


def test_cap_memo_retains_interleaved_caps(setup_rerank: Any) -> None:
    options, _ = setup_rerank
    options.DAILY_REQUEST_CAP = 1
    rerank._record_outcome("skipped_cap", "2026-10-31", 1)
    assert rerank.skip_reason() == "skipped_cap"
    options.DAILY_REQUEST_CAP = 2
    assert rerank.skip_reason() is None
    options.DAILY_REQUEST_CAP = 1
    assert rerank.skip_reason() == "skipped_cap"


@pytest.mark.parametrize("count,outcome", [(1, "reranked"), (2, "skipped_cap")])
async def test_cap_is_captured_before_counter_await(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    outcome: str,
) -> None:
    options, events = setup_rerank
    options.DAILY_REQUEST_CAP = 1

    async def counter(_day: str) -> int:
        options.DAILY_REQUEST_CAP = 10
        return count

    monkeypatch.setattr(rerank, "_requests_for_day", counter)
    _, requests = await invoke(monkeypatch, documents, body())
    assert events[-1].outcome == outcome
    assert events[-1].daily_request_cap == 1
    assert events[-1].daily_requests == count
    assert len(requests) == (1 if outcome == "reranked" else 0)
    if outcome == "skipped_cap":
        assert ("2026-10-31", 1) in rerank._cap_hints
        assert ("2026-10-31", 10) not in rerank._cap_hints


def test_cap_memo_retains_only_today_and_yesterday(
    setup_rerank: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options, _ = setup_rerank
    options.DAILY_REQUEST_CAP = 2
    for day in ("2026-10-29", "2026-10-30", "2026-10-31"):
        monkeypatch.setattr(rerank, "_utc_day", lambda day=day: day)
        rerank._record_outcome("skipped_cap", day, 2)
    assert rerank._cap_hints == {("2026-10-30", 2), ("2026-10-31", 2)}
    monkeypatch.setattr(rerank, "_utc_day", lambda: "2026-11-02")
    assert rerank.skip_reason() is None
    assert rerank._cap_hints == set()


@pytest.mark.parametrize(
    "provider,limit",
    [("voyage", 1000), ("cohere", 10000), ("jina", 10000), ("tei", 10000)],
)
def test_provider_hard_ceiling(setup_rerank: Any, provider: Any, limit: int) -> None:
    options, _ = setup_rerank
    options.PROVIDER = provider
    options.MAX_DOCUMENTS = 10000
    assert rerank.max_documents() == limit


async def test_unknown_provider_is_fail_open(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options, _ = setup_rerank
    proxy = SimpleNamespace(**options.model_dump())
    proxy.PROVIDER = "unknown"
    monkeypatch.setattr(rerank.settings, "RERANK", proxy)
    assert rerank.max_documents() == 0
    assert rerank.candidate_count(2) == 2
    assert rerank.skip_reason() == "skipped_unsupported_provider"
    result, requests = await invoke(monkeypatch, documents, body())
    assert result == documents[:2] and not requests


@pytest.mark.parametrize(
    "operation", ["_record_outcome", "_log_fallback", "_emit_completed"]
)
@pytest.mark.parametrize("path", ["success", "skipped", "failed", "integration"])
async def test_postprocessing_cannot_replace_results(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    path: str,
) -> None:
    options, events = setup_rerank
    if path == "skipped":
        options.DAILY_REQUEST_CAP = 0
    failure = MagicMock(side_effect=RuntimeError("bookkeeping unavailable"))
    monkeypatch.setattr(rerank, operation, failure)
    if path == "integration":
        result = rerank.record_rerank_fallback("dream", documents, 2, "empty_result")
    else:
        result, _ = await invoke(
            monkeypatch, documents, body(), status=503 if path == "failed" else 200
        )
    assert result == (
        [documents[2], documents[0]] if path == "success" else documents[:2]
    )
    if operation != "_log_fallback" or path != "success":
        failure.assert_called_once()
    if operation != "_emit_completed":
        assert len(events) == 1


@pytest.mark.parametrize("provider", ["cohere", "jina"])
@pytest.mark.parametrize("server", ["vllm", "infinity"])
async def test_compatible_server_usage(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,
    server: str,
) -> None:
    options, events = setup_rerank
    options.PROVIDER = provider
    options.BASE_URL = "https://reranker.example"
    payload: dict[str, Any] = {
        "results": rows(),
        "usage": {"total_tokens": 17},
    }
    if server == "infinity":
        payload.update(id="test-response", model="test-model")
        payload["usage"]["prompt_tokens"] = 17
        for row in payload["results"]:
            row["document"] = "echoed-text"
    else:
        payload["model"] = "test-model"
        for row in payload["results"]:
            row["document"] = {"text": "echoed-text"}
    result, _ = await invoke(monkeypatch, documents, payload)
    assert result == [documents[2], documents[0]]
    assert events[-1].tokens == 17
    assert events[-1].billed_units is None


@pytest.mark.parametrize(
    "neutral",
    [
        "skipped_cap",
        "skipped_missing_key",
        "skipped_cooldown",
        "skipped_transaction",
        "skipped_unsupported_provider",
        "empty_result",
        "too_many_candidates",
        "cancelled",
    ],
)
def test_neutral_outcomes_preserve_failure_streak(
    setup_rerank: Any, neutral: Any
) -> None:
    options, _ = setup_rerank
    options.FAILURES_BEFORE_COOLDOWN = 3
    rerank._record_outcome("timeout", "2026-10-31", None)
    rerank._record_outcome(neutral, "2026-10-31", None)
    assert rerank._circuit(options).failures == 1
    rerank._record_outcome("malformed_response", "2026-10-31", None)
    assert rerank._circuit(options).failures == 2
    rerank._record_outcome("reranked", "2026-10-31", None)
    assert rerank._circuit(options).failures == 0


@pytest.mark.parametrize("stage", ["counter", "http"])
@pytest.mark.parametrize("broken_postprocessing", [False, True])
async def test_cancellation_propagates_with_neutral_accounting(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    broken_postprocessing: bool,
) -> None:
    options, events = setup_rerank
    options.DAILY_REQUEST_CAP = 5
    cancellation = asyncio.CancelledError("cancelled request")
    record = MagicMock(wraps=rerank._record_outcome)
    monkeypatch.setattr(rerank, "_record_outcome", record)
    if stage == "counter":
        monkeypatch.setattr(
            rerank, "_requests_for_day", AsyncMock(side_effect=cancellation)
        )
    else:
        monkeypatch.setattr(
            rerank,
            "_get_client",
            lambda: failing_client(cancellation),
        )
    if broken_postprocessing:
        monkeypatch.setattr(
            rerank, "_log_fallback", MagicMock(side_effect=RuntimeError)
        )
        monkeypatch.setattr(
            rerank, "_emit_completed", MagicMock(side_effect=RuntimeError)
        )
    with pytest.raises(asyncio.CancelledError) as raised:
        await rerank.rerank_documents("private-query", documents, 2, "dream")
    assert raised.value is cancellation
    record.assert_called_once_with("cancelled", "2026-10-31", 5, options)
    assert rerank._circuit(options).failures == 0
    if not broken_postprocessing:
        assert events[-1].outcome == "cancelled"
        assert events[-1].candidates_sent == (3 if stage == "http" else 0)
        assert events[-1].daily_requests == (1 if stage == "http" else None)


@pytest.mark.parametrize("path", ["success", "skipped", "integration"])
async def test_completion_settings_failure_is_fail_soft(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    options, _ = setup_rerank
    if path == "skipped":
        options.DAILY_REQUEST_CAP = 0
    original_emit = rerank._emit_completed

    class BrokenSettings:
        @property
        def RERANK(self) -> Any:
            raise RuntimeError("settings unavailable")

    def emit(*args: Any) -> None:
        monkeypatch.setattr(rerank, "settings", BrokenSettings())
        original_emit(*args)

    monkeypatch.setattr(rerank, "_emit_completed", emit)
    if path == "integration":
        result = rerank.record_rerank_fallback("dream", documents, 2, "empty_result")
    else:
        result, _ = await invoke(monkeypatch, documents, body())
    assert result == (
        [documents[2], documents[0]] if path == "success" else documents[:2]
    )


async def test_cohere_reports_tokens_and_billing_together(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options, events = setup_rerank
    options.PROVIDER = "cohere"
    payload = body("cohere")
    payload["usage"] = {"total_tokens": 23}
    await invoke(monkeypatch, documents, payload)
    assert (events[-1].tokens, events[-1].billed_units) == (23, 1.5)


async def test_concurrent_proxy_caps_keep_independent_memos(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options, events = setup_rerank
    current: ContextVar[Any] = ContextVar("rerank_options")
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]

    class SettingsProxy:
        CACHE: Any = SimpleNamespace(ENABLED=False)

        @property
        def RERANK(self) -> Any:
            return current.get()

    async def counter(_day: str) -> int:
        cap = cast(int, current.get().DAILY_REQUEST_CAP)
        entered[cap - 1].set()
        await release[cap - 1].wait()
        return cap + 1

    async def request(cap: int) -> None:
        token = current.set(options.model_copy(update={"DAILY_REQUEST_CAP": cap}))
        try:
            assert (
                await rerank.rerank_documents("q", documents, 2, "dream")
                == documents[:2]
            )
        finally:
            current.reset(token)

    monkeypatch.setattr(rerank, "settings", SettingsProxy())
    monkeypatch.setattr(rerank, "_requests_for_day", counter)
    first = asyncio.create_task(request(1))
    second = asyncio.create_task(request(2))
    await asyncio.gather(*(event.wait() for event in entered))
    release[0].set()
    await first
    release[1].set()
    await second
    assert {(event.daily_request_cap, event.daily_requests) for event in events} == {
        (1, 2),
        (2, 3),
    }
    assert rerank._cap_hints == {("2026-10-31", 1), ("2026-10-31", 2)}
    for cap in (1, 2, 1):
        token = current.set(options.model_copy(update={"DAILY_REQUEST_CAP": cap}))
        try:
            assert rerank.skip_reason() == "skipped_cap"
        finally:
            current.reset(token)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("test-key\n", "test-key"),
        ("  test-key\t", "test-key"),
        ("test-key", "test-key"),
        ("", None),
        (" \n", None),
    ],
)
def test_api_key_is_stripped_at_load(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str | None
) -> None:
    loaded = cast(Any, config.RerankSettings)(API_KEY=raw)
    if expected is None:
        assert loaded.API_KEY is None
    else:
        assert loaded.API_KEY.get_secret_value() == expected


@pytest.mark.parametrize("raw", ["test-kéy", "test\x00key", "test\nkey", "test\x7fkey"])
def test_api_key_with_unsendable_characters_is_a_settings_error(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    with pytest.raises(ValidationError) as raised:
        cast(Any, config.RerankSettings)(API_KEY=raw)
    assert raw not in str(raised.value)


@pytest.mark.parametrize("block", [None, {}, {"enabled": True}])
def test_rejected_environment_key_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch, block: dict[str, Any] | None
) -> None:
    monkeypatch.setenv("RERANK_API_KEY", "test-kéy")
    with pytest.raises(ValidationError) as raised:
        config.build_settings(block)
    assert "test-k" not in str(raised.value)
    assert "test-k" not in config.describe_error(raised.value)


def test_api_key_from_environment_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RERANK_API_KEY", "test-key\n")
    loaded = config.build_settings(None)
    assert loaded.API_KEY is not None
    assert loaded.API_KEY.get_secret_value() == "test-key"


async def test_loaded_key_reaches_a_real_server(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MockTransport skips header validation; a real connection does not."""
    _, events = setup_rerank
    seen: list[str] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        headers = dict(
            line.split(": ", 1) for line in head.split("\r\n")[1:] if ": " in line
        )
        lowered = {name.lower(): value for name, value in headers.items()}
        seen.append(lowered.get("authorization", ""))
        await reader.readexactly(int(lowered.get("content-length", "0")))
        payload = json.dumps(body()).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
            + f"content-length: {len(payload)}\r\nconnection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "localhost", 0)
    port = server.sockets[0].getsockname()[1]
    loaded = cast(Any, config.RerankSettings)(
        ENABLED=True,
        API_KEY="test-key\n",
        BASE_URL=f"http://localhost:{port}",
    )
    monkeypatch.setattr(rerank.settings, "RERANK", loaded)
    try:
        # trust_env off: a proxy from the environment must not intercept localhost.
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.LocalProtocolError):
                await client.post(
                    f"http://localhost:{port}/rerank",
                    headers={"Authorization": "Bearer test-key\n"},
                    json={},
                )
            monkeypatch.setattr(rerank, "_get_client", lambda: client)
            result = await rerank.rerank_documents("q", documents, 2, "dream")
    finally:
        server.close()
        await server.wait_closed()
    assert result == [documents[2], documents[0]]
    assert events[-1].outcome == "reranked"
    assert seen == ["Bearer test-key"]


async def test_unsupported_provider_event_uses_bounded_label(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, events = setup_rerank
    proxy = SimpleNamespace(**options.model_dump())
    proxy.PROVIDER = "unknown"
    monkeypatch.setattr(rerank.settings, "RERANK", proxy)
    result, requests = await invoke(monkeypatch, documents, body())
    assert result == documents[:2] and not requests
    assert events[-1].outcome == "skipped_unsupported_provider"
    assert events[-1].provider == "unsupported"


async def test_missing_url_skips_without_cooldown(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, events = setup_rerank
    options.PROVIDER = "tei"
    options.BASE_URL = None
    options.FAILURES_BEFORE_COOLDOWN = 1
    assert rerank.skip_reason() == "skipped_missing_url"
    for _ in range(3):
        result, requests = await invoke(monkeypatch, documents, body("tei"))
        assert result == documents[:2] and not requests
    assert [event.outcome for event in events] == ["skipped_missing_url"] * 3
    assert rerank._circuit(options).failures == 0
    assert rerank.skip_reason() == "skipped_missing_url"


@pytest.mark.parametrize("cluster", [False, True])
def test_real_redis_backend_is_detected(
    monkeypatch: pytest.MonkeyPatch, cluster: bool
) -> None:
    # setup() builds the backend without connecting; detection needs no server.
    redis_cache = Cache()
    redis_cache.setup("redis://reranker.example:6379/0", cluster=cluster)
    monkeypatch.setattr(rerank, "cache", redis_cache)
    assert rerank._redis_counter_available(rerank._counter_key("2026-10-31"))


@pytest.mark.parametrize("name", ["api_key", "API_KEY", "key"])
def test_key_in_the_settings_file_is_ignored(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # The key only comes from the environment; a key written into the file is dropped.
    monkeypatch.delenv("RERANK_API_KEY", raising=False)
    loaded = config.build_settings({name: "test-file-key", "enabled": True})
    assert loaded.ENABLED and loaded.API_KEY is None
    monkeypatch.setenv("RERANK_API_KEY", "test-env-key")
    loaded = config.build_settings({name: "test-file-key"})
    assert loaded.API_KEY is not None
    assert loaded.API_KEY.get_secret_value() == "test-env-key"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:test-password@reranker.example",
        "https://user@reranker.example/v1",
        "https://@reranker.example",
        "https://reranker.example/v1?token=test-password",
        "https://reranker.example/v1?",
        "https://reranker.example/v1#fragment",
    ],
)
def test_base_url_rejects_credentials_query_and_fragment(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    with pytest.raises(ValidationError) as raised:
        cast(Any, config.RerankSettings)(BASE_URL=url)
    assert "test-password" not in str(raised.value)


@pytest.mark.parametrize(
    "root", ["https://reranker.example/v1", "https://reranker.example/v1/"]
)
async def test_base_url_path_is_kept_under_rerank(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch, root: str
) -> None:
    options, events = setup_rerank
    options.PROVIDER = "tei"
    options.BASE_URL = root
    result, requests = await invoke(monkeypatch, documents, body("tei"))
    assert result == [documents[2], documents[0]]
    assert str(requests[0].url) == "https://reranker.example/v1/rerank"
    assert events[0].outcome == "reranked"


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_broken_entry_settings_keep_the_vector_order(
    documents: Any, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    # The candidates are already fetched when the entry settings are read, so
    # a proxy that stopped answering must not cost the caller its results.
    class Broken:
        @property
        def RERANK(self) -> Any:  # noqa: N802 - mirrors the settings attribute
            raise failure("policy unavailable")

    monkeypatch.setattr(rerank, "settings", Broken())
    events: list[Any] = []
    monkeypatch.setattr(telemetry_events, "emit", events.append)
    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await rerank.rerank_documents("private-query", documents, 2, "dream")
        return
    result = await rerank.rerank_documents("private-query", documents, 2, "dream")
    assert result == documents[:2]
    assert not events


@pytest.mark.parametrize("provider", ["voyage", "cohere", "jina", "tei"])
async def test_malformed_ranking_preserves_reported_usage(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,
) -> None:
    options, events = setup_rerank
    options.PROVIDER = provider
    if provider == "tei":
        options.BASE_URL = "https://reranker.example"
        payload = [rows("score")[0]] * 2
    else:
        payload = body(provider)
        payload["data" if provider == "voyage" else "results"] = [rows()[0]] * 2
        payload["usage"] = {"total_tokens": 123}
    result, requests = await invoke(monkeypatch, documents, payload)
    assert result == documents[:2] and len(requests) == 1
    assert events[0].outcome == "malformed_response"
    assert events[0].tokens == (None if provider == "tei" else 123)
    assert events[0].billed_units == (1.5 if provider == "cohere" else None)


@pytest.mark.parametrize("min_score", [None, 0.5])
async def test_single_candidate_only_requests_when_filtering(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    min_score: float | None,
) -> None:
    options, events = setup_rerank
    options.MIN_SCORE = min_score
    options.DAILY_REQUEST_CAP = 10
    counter = AsyncMock(return_value=1)
    monkeypatch.setattr(rerank, "_requests_for_day", counter)
    # A deterministic clock proves no timer or request wait is needed for the noop.
    monkeypatch.setattr(rerank.time, "perf_counter", lambda: 1.0)
    result, requests = await invoke(
        monkeypatch,
        documents[:1],
        {"data": [{"index": 0, "relevance_score": 0.1}]},
        top_k=1,
    )
    event = events[0]
    if min_score is None:
        assert requests == []
        counter.assert_not_awaited()
        assert result == documents[:1]
        assert event.outcome == "noop_single"
        assert event.candidates_sent == 0 and event.duration_ms == 0
        assert event.daily_requests is None
    else:
        assert len(requests) == 1
        counter.assert_awaited_once()
        assert result == [] and event.outcome == "reranked"


@pytest.mark.parametrize("provider", ["voyage", "cohere", "jina", "tei"])
@pytest.mark.parametrize("query_limit", [None, 7])
async def test_query_payload_is_bounded_by_characters(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    provider: Any,
    query_limit: int | None,
) -> None:
    options, events = setup_rerank
    options.PROVIDER = provider
    if query_limit is not None:
        options.MAX_QUERY_CHARS = query_limit
    if provider == "tei":
        options.BASE_URL = "https://reranker.example"
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(json.loads(request.content)["query"])
        return httpx.Response(200, json=body(provider))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(rerank, "_get_client", lambda: client)
        result = await rerank.rerank_documents("😀" * 10_000, documents, 2, "dream")
    assert queries == ["😀" * (query_limit or 2000)]
    assert result == [documents[2], documents[0]]
    assert events[0].outcome == "reranked"


@pytest.mark.parametrize("limit", [0, -1])
def test_query_limit_rejects_nonpositive_values(limit: int) -> None:
    with pytest.raises(ValidationError):
        cast(Any, config.RerankSettings)(MAX_QUERY_CHARS=limit)


@pytest.mark.parametrize(
    "path", ["success", "failure", "noop", "disabled", "integration"]
)
async def test_search_reads_rerank_settings_once(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    options, events = setup_rerank
    options.ENABLED = path != "disabled"

    class CountingSettings:
        reads: int = 0
        CACHE: SimpleNamespace = SimpleNamespace(ENABLED=False)

        @property
        def RERANK(self) -> Any:
            self.reads += 1
            return options

    proxy = CountingSettings()
    monkeypatch.setattr(rerank, "settings", proxy)
    if path == "integration":
        assert (
            rerank.record_rerank_fallback("dream", documents, 2, "empty_result")
            == documents[:2]
        )
    else:
        docs = documents[:1] if path == "noop" else documents
        await invoke(
            monkeypatch, docs, body(), status=500 if path == "failure" else 200
        )
    assert proxy.reads == 1
    if path != "disabled":
        assert len(events) == 1


@pytest.mark.parametrize("change", ["provider", "base_url"])
async def test_cooldown_isolated_by_provider_and_url(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    options, events = setup_rerank
    options.FAILURES_BEFORE_COOLDOWN = 1
    options.BASE_URL = "https://reranker.example/a"
    await invoke(monkeypatch, documents, {}, status=500)
    assert rerank.skip_reason() == "skipped_cooldown"
    healthy = options.model_copy(
        update={
            "PROVIDER" if change == "provider" else "BASE_URL": "jina"
            if change == "provider"
            else "https://reranker.example/b"
        }
    )
    monkeypatch.setattr(rerank.settings, "RERANK", healthy)
    result, requests = await invoke(monkeypatch, documents, body(healthy.PROVIDER))
    assert len(requests) == 1 and result == [documents[2], documents[0]]
    assert events[-1].outcome == "reranked"
    monkeypatch.setattr(rerank.settings, "RERANK", options)
    assert rerank.skip_reason() == "skipped_cooldown"


@pytest.mark.parametrize("probe_outcome", ["failure", "success", "cancelled"])
async def test_expired_cooldown_allows_one_probe(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    probe_outcome: str,
) -> None:
    options, events = setup_rerank
    options.FAILURES_BEFORE_COOLDOWN = 1
    clock = [100.0]
    monkeypatch.setattr(rerank.time, "monotonic", lambda: clock[0])
    rerank._record_outcome("timeout", None, None)
    clock[0] += options.COOLDOWN_SECONDS + 1
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        entered.set()
        await release.wait()
        return httpx.Response(500 if probe_outcome == "failure" else 200, json=body())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(rerank, "_get_client", lambda: client)
        probe = asyncio.create_task(rerank.rerank_documents("q", documents, 2, "dream"))
        await entered.wait()
        await asyncio.gather(
            *(rerank.rerank_documents("q", documents, 2, "dream") for _ in range(9))
        )
        assert attempts == 1
        assert len([e for e in events if e.outcome == "skipped_cooldown"]) == 9
        if probe_outcome == "cancelled":
            probe.cancel()
            with pytest.raises(asyncio.CancelledError):
                await probe
        else:
            release.set()
            await probe
    state = rerank._circuit(options)
    assert not state.probe
    assert rerank.skip_reason() == (
        None if probe_outcome == "success" else "skipped_cooldown"
    )
    if probe_outcome == "success":
        assert state.failures == 0 and state.until == 0


@pytest.mark.parametrize("provider", ["voyage", "cohere", "jina", "tei"])
async def test_more_rows_than_requested_keep_vector_order(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch, provider: Any
) -> None:
    """A provider asked for top_n rows (TEI: one per text) may not return more."""
    options, events = setup_rerank
    options.PROVIDER = provider
    options.BASE_URL = "https://reranker.example"
    score = "score" if provider == "tei" else "relevance_score"
    extra = rows(score, None)
    if provider == "tei":
        # Four rows for three texts: an index repeats or leaves the range.
        extra = [*extra, {"index": 3, score: 0.1}]
    payload = (
        extra
        if provider == "tei"
        else {"data" if provider == "voyage" else "results": extra}
    )
    result, requests = await invoke(monkeypatch, documents, payload)
    assert len(requests) == 1
    assert result == documents[:2] and events[-1].outcome == "malformed_response"


def test_ranking_contract_rejects_each_violation() -> None:
    adapter = rerank._ADAPTERS["cohere"]
    good = {"results": [{"index": 1, "relevance_score": 0.5}]}
    assert adapter.ranking(good, 3, 1) == [(1, 0.5)]
    for bad in (
        {"results": [{"index": 3, "relevance_score": 0.5}]},
        {"results": [{"index": -1, "relevance_score": 0.5}]},
        {"results": [{"index": 1.0, "relevance_score": 0.5}]},
        {"results": [{"index": 1, "relevance_score": float("inf")}]},
        {"results": [{"index": 1, "relevance_score": "0.5"}]},
        {"results": [{"index": 1, "relevance_score": 0.5}] * 2},
        {"results": [{"index": 0, "relevance_score": 0.5}, {"index": 1, "relevance_score": 0.4}]},
    ):
        with pytest.raises(ValueError):
            adapter.ranking(bad, 3, 1)


@pytest.mark.parametrize("declared", [True, False])
async def test_oversized_response_is_refused(
    setup_rerank: Any,
    documents: Any,
    monkeypatch: pytest.MonkeyPatch,
    declared: bool,
) -> None:
    """A body past the cap is not read to the end, with or without Content-Length."""
    _, events = setup_rerank
    monkeypatch.setattr(rerank, "MAX_RESPONSE_BYTES", 64)
    payload = json.dumps(body()).encode() + b" " * 128
    pulled: list[int] = []

    async def chunks() -> Any:
        for start in range(0, len(payload), 16):
            pulled.append(start)
            yield payload[start : start + 16]

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": "application/json"}
        if declared:
            headers["content-length"] = str(len(payload))
        return httpx.Response(200, headers=headers, content=chunks())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(rerank, "_get_client", lambda: client)
        result = await rerank.rerank_documents("q", documents, 2, "dream")
    assert result == documents[:2]
    assert events[-1].outcome == "malformed_response"
    assert len(pulled) * 16 < len(payload)


async def test_response_under_the_cap_is_ranked(
    setup_rerank: Any, documents: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, events = setup_rerank
    monkeypatch.setattr(rerank, "MAX_RESPONSE_BYTES", len(json.dumps(body())))
    result, _ = await invoke(monkeypatch, documents, body())
    assert result == [documents[2], documents[0]]
    assert events[-1].outcome == "reranked"
