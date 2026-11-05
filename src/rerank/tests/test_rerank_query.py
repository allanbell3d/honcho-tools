"""Fake-only checks for candidate retrieval around Honcho's query_documents."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import honcho_rerank.client as rerank_module
from honcho_rerank.settings import RerankSettings
from src.crud import document

original_skip_reason = rerank_module.skip_reason


@pytest.fixture(autouse=True)
def rerank_attempted(monkeypatch: pytest.MonkeyPatch) -> RerankSettings:
    """These tests exercise the attempted path; skip checks have their own tests."""
    options = RerankSettings(ENABLED=True, CALLERS=["dream", "conclusions_api"])
    monkeypatch.setattr(
        rerank_module,
        "settings",
        SimpleNamespace(RERANK=options, CACHE=SimpleNamespace(ENABLED=False)),
    )
    monkeypatch.setattr(rerank_module, "skip_reason", MagicMock(return_value=None))
    return options


async def search(fetch: Any, db: Any = None, top_k: int = 2, **kwargs: Any) -> Any:
    kwargs.setdefault("caller", "dream")
    return await rerank_module.search(
        fetch,
        db,
        "workspace",
        "query",
        observer="alice",
        observed="bob",
        top_k=top_k,
        **kwargs,
    )


@pytest.mark.parametrize("external", [False, True])
async def test_rerank_fetches_candidates_then_releases_session(
    monkeypatch: pytest.MonkeyPatch, external: bool
):
    """Through Honcho's real query_documents: own read-only session, rows expunged."""
    docs = [MagicMock(), MagicMock(), MagicMock()]
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock()
    active = False
    opened: list[dict[str, Any]] = []

    @asynccontextmanager
    async def tracked(*_args: Any, **kwargs: Any):
        nonlocal active
        opened.append(kwargs)
        active = True
        yield session
        active = False

    vector = AsyncMock(return_value=docs)
    fetch = AsyncMock(return_value=docs)
    ids = AsyncMock(return_value=["a", "b", "c"])

    async def rerank(
        query: str, documents: Any, top_k: int, caller: str, **correlation: Any
    ):
        assert not active
        assert documents == docs
        assert (query, top_k, caller) == ("query", 2, "dream")
        assert correlation["workspace_name"] == "workspace"
        assert correlation["run_id"] == "run-1"
        return docs[::-1][:top_k]

    monkeypatch.setattr(rerank_module, "rerank_documents", rerank)
    monkeypatch.setattr(document, "tracked_db", tracked)
    monkeypatch.setattr(document, "_uses_pgvector", lambda: not external)
    monkeypatch.setattr(document, "_query_documents_pgvector", vector)
    monkeypatch.setattr(document, "query_external_vector_document_ids", ids)
    monkeypatch.setattr(document, "fetch_documents_by_ids", fetch)
    filters = {"level": "explicit"}
    result = await search(
        document.query_documents,
        filters=filters,
        max_distance=0.3,
        embedding=[1.0],
        run_id="run-1",
    )
    assert result == docs[::-1][:2]
    assert session.expunge.call_count == 3
    assert opened == [{"read_only": True}]
    if external:
        assert ids.await_args is not None
        assert ids.await_args.kwargs["top_k"] == 100
        assert ids.await_args.kwargs["filters"] == filters
        assert ids.await_args.kwargs["max_distance"] == 0.3
        assert fetch.await_args is not None
        assert fetch.await_args.kwargs["observer"] == "alice"
        assert fetch.await_args.kwargs["observed"] == "bob"
    else:
        assert vector.await_args is not None
        assert vector.await_args.args == (
            session,
            "workspace",
            "alice",
            "bob",
            [1.0],
            filters,
            0.3,
            100,
        )


@pytest.mark.parametrize("caller", [None, "dedupe", "unlisted"])
async def test_disabled_callers_keep_existing_session_and_limit(caller: str | None):
    session = MagicMock(spec=AsyncSession)
    docs = [MagicMock()]
    vector = AsyncMock(return_value=docs)
    assert await search(vector, session, top_k=3, caller=caller) == docs
    assert vector.await_args is not None
    assert vector.await_args.args[0] is session
    assert vector.await_args.kwargs["top_k"] == 3


async def test_active_caller_transaction_falls_back_without_rollback(
    monkeypatch: pytest.MonkeyPatch,
):
    session = MagicMock(spec=AsyncSession)
    session.in_transaction.return_value = True
    docs = [MagicMock()]
    vector = AsyncMock(return_value=docs)
    rerank = AsyncMock()
    fallback = MagicMock(return_value=docs)
    monkeypatch.setattr(rerank_module, "rerank_documents", rerank)
    monkeypatch.setattr(rerank_module, "record_rerank_fallback", fallback)
    assert await search(vector, session, top_k=3) == docs
    assert vector.await_args is not None
    assert vector.await_args.args[0] is session
    assert vector.await_args.kwargs["top_k"] == 3
    rerank.assert_not_awaited()
    assert fallback.call_args.args[3] == "skipped_transaction"
    session.rollback.assert_not_called()
    session.close.assert_not_called()


async def test_candidate_count_never_reduces_requested_limit(
    rerank_attempted: RerankSettings,
):
    rerank_attempted.CANDIDATES = 10
    vector = AsyncMock(return_value=[])
    await search(vector, top_k=20)
    assert vector.await_args is not None
    assert vector.await_args.kwargs["top_k"] == 20


async def test_zero_limit_skips_all_work(monkeypatch: pytest.MonkeyPatch):
    vector = AsyncMock()
    gate = MagicMock()
    monkeypatch.setattr(rerank_module, "should_rerank", gate)
    assert await search(vector, top_k=0) == []
    vector.assert_not_awaited()
    gate.assert_not_called()


async def test_empty_candidates_record_a_fallback(
    monkeypatch: pytest.MonkeyPatch, rerank_attempted: RerankSettings
):
    fallback = MagicMock(return_value=[])
    rerank = AsyncMock()
    monkeypatch.setattr(rerank_module, "record_rerank_fallback", fallback)
    monkeypatch.setattr(rerank_module, "rerank_documents", rerank)
    assert await search(AsyncMock(return_value=[]), top_k=5) == []
    fallback.assert_called_once_with(
        "dream",
        [],
        5,
        "empty_result",
        workspace_name="workspace",
        run_id=None,
        config=rerank_attempted,
    )
    rerank.assert_not_awaited()


async def test_skipped_rerank_keeps_the_callers_own_search(
    monkeypatch: pytest.MonkeyPatch, rerank_attempted: RerankSettings
):
    session = MagicMock(spec=AsyncSession)
    session.in_transaction = MagicMock(return_value=False)
    docs = [MagicMock(), MagicMock()]
    vector = AsyncMock(return_value=docs)
    fallback = MagicMock(return_value=docs)
    monkeypatch.setattr(
        rerank_module, "skip_reason", MagicMock(return_value="skipped_cooldown")
    )
    monkeypatch.setattr(rerank_module, "record_rerank_fallback", fallback)
    assert await search(vector, session, top_k=5) == docs
    assert vector.await_args is not None
    assert vector.await_args.args[0] is session
    assert vector.await_args.kwargs["top_k"] == 5
    fallback.assert_called_once_with(
        "dream",
        docs,
        5,
        "skipped_cooldown",
        workspace_name="workspace",
        run_id=None,
        config=rerank_attempted,
    )


async def test_query_uses_rerank_candidate_helper(
    monkeypatch: pytest.MonkeyPatch, rerank_attempted: RerankSettings
):
    vector = AsyncMock(return_value=[])
    count = MagicMock(return_value=17)
    monkeypatch.setattr(rerank_module, "candidate_count", count)
    monkeypatch.setattr(
        rerank_module, "record_rerank_fallback", MagicMock(return_value=[])
    )
    await search(vector, top_k=4)
    count.assert_called_once_with(4, rerank_attempted)
    assert vector.await_args is not None
    assert vector.await_args.kwargs["top_k"] == 17


@pytest.mark.parametrize(
    ("provider", "base_url", "reason"),
    [("unknown", None, None), ("tei", None, "skipped_missing_url")],
)
async def test_certain_skips_do_not_widen_the_search(
    monkeypatch: pytest.MonkeyPatch,
    rerank_attempted: RerankSettings,
    provider: str,
    base_url: str | None,
    reason: str | None,
):
    options = SimpleNamespace(**rerank_attempted.model_dump())
    options.PROVIDER = provider
    options.BASE_URL = base_url
    monkeypatch.setattr(rerank_module, "settings", SimpleNamespace(RERANK=options))
    monkeypatch.setattr(rerank_module, "skip_reason", original_skip_reason)
    docs = [MagicMock(), MagicMock()]
    vector = AsyncMock(return_value=docs)
    request = AsyncMock()
    fallback = MagicMock(return_value=docs)
    monkeypatch.setattr(rerank_module, "rerank_documents", request)
    monkeypatch.setattr(rerank_module, "record_rerank_fallback", fallback)
    assert await search(vector) == docs
    assert vector.await_args is not None
    assert vector.await_args.kwargs["top_k"] == 2
    request.assert_not_awaited()
    assert fallback.call_args.args[3] == (reason or "skipped_unsupported_provider")


class _BrokenSettings:
    """A settings proxy whose reranking section cannot be read."""

    @property
    def RERANK(self) -> Any:  # noqa: N802 - mirrors the settings attribute
        raise RuntimeError("policy unavailable")


@pytest.mark.parametrize("failing", ["proxy", "skip_reason", "candidate_count"])
async def test_broken_gate_runs_the_plain_search(
    monkeypatch: pytest.MonkeyPatch, failing: str
):
    session = MagicMock(spec=AsyncSession)
    docs = [MagicMock(), MagicMock(), MagicMock()]
    vector = AsyncMock(return_value=docs)
    fallback = MagicMock(return_value=docs[:2])
    monkeypatch.setattr(rerank_module, "record_rerank_fallback", fallback)
    if failing == "proxy":
        monkeypatch.setattr(rerank_module, "settings", _BrokenSettings())
    else:
        broken = MagicMock(side_effect=RuntimeError("policy unavailable"))
        monkeypatch.setattr(rerank_module, failing, broken)
    assert await search(vector, session) == docs[:2]
    assert vector.await_args is not None
    assert vector.await_args.args[0] is session
    assert vector.await_args.kwargs["top_k"] == 2
    fallback.assert_called_once_with(
        "dream", docs, 2, "internal_error", workspace_name="workspace", run_id=None
    )


async def test_widened_fetch_failure_returns_the_stock_search(
    monkeypatch: pytest.MonkeyPatch, rerank_attempted: RerankSettings
):
    session = MagicMock(spec=AsyncSession)
    session.in_transaction.return_value = False
    docs = [MagicMock(), MagicMock(), MagicMock()]
    seen: list[tuple[Any, int]] = []

    async def fetch(db: Any, *_args: Any, **kwargs: Any) -> Any:
        seen.append((db, kwargs["top_k"]))
        if db is None:
            raise RuntimeError("widened fetch failed")
        return docs

    fallback = MagicMock(return_value=docs[:2])
    monkeypatch.setattr(rerank_module, "record_rerank_fallback", fallback)
    assert await search(fetch, session) == docs[:2]
    assert seen == [(None, rerank_attempted.CANDIDATES), (session, 2)]
    fallback.assert_called_once_with(
        "dream",
        docs,
        2,
        "internal_error",
        workspace_name="workspace",
        run_id=None,
        config=rerank_attempted,
    )


def test_fallback_recording_survives_a_broken_proxy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rerank_module, "settings", _BrokenSettings())
    docs = [MagicMock(), MagicMock(), MagicMock()]
    assert (
        rerank_module.record_rerank_fallback("dream", docs, 2, "internal_error")
        == docs[:2]
    )


async def test_settings_read_before_the_fetch_stay_in_force(
    monkeypatch: pytest.MonkeyPatch, rerank_attempted: RerankSettings
):
    """One search reads its settings once; a proxy that breaks mid-call is not reread."""
    rerank_attempted.API_KEY = None
    rerank_attempted.PROVIDER = "tei"
    rerank_attempted.BASE_URL = "https://reranker.example"
    docs = [SimpleNamespace(content="a"), SimpleNamespace(content="b")]

    async def vector(*_args: Any, **_kwargs: Any) -> Any:
        # The gate passed; the policy disappears while candidates are fetched.
        monkeypatch.setattr(rerank_module, "settings", _BrokenSettings())
        return docs

    stream = MagicMock(side_effect=RuntimeError("no network in tests"))
    monkeypatch.setattr(
        rerank_module, "_get_client", lambda: SimpleNamespace(stream=stream)
    )
    monkeypatch.setattr(rerank_module, "_emit_completed", MagicMock())
    assert await search(vector) == docs
    stream.assert_called_once()


async def test_search_reads_settings_once_across_fetch_and_rerank(
    monkeypatch: pytest.MonkeyPatch, rerank_attempted: RerankSettings
):
    reads = 0

    class Counting:
        CACHE: SimpleNamespace = SimpleNamespace(ENABLED=False)

        @property
        def RERANK(self) -> Any:  # noqa: N802 - mirrors the settings attribute
            nonlocal reads
            reads += 1
            return rerank_attempted

    monkeypatch.setattr(rerank_module, "settings", Counting())
    monkeypatch.setattr(rerank_module, "skip_reason", original_skip_reason)
    rerank_attempted.PROVIDER = "tei"
    rerank_attempted.BASE_URL = "https://reranker.example"
    post = AsyncMock(side_effect=RuntimeError("no network in tests"))
    monkeypatch.setattr(
        rerank_module, "_get_client", lambda: SimpleNamespace(post=post)
    )
    docs = [SimpleNamespace(content="a"), SimpleNamespace(content="b")]
    monkeypatch.setattr(rerank_module, "_emit_completed", MagicMock())
    assert await search(AsyncMock(return_value=docs)) == docs
    assert reads == 1
