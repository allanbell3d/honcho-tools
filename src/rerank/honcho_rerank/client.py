"""Provider-neutral, fail-open reranking with UTC-day request caps."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from weakref import WeakKeyDictionary

import httpx
from cashews.backends.redis import Redis

from src.cache.client import cache, cache_key_namespace

from .events import RerankOutcome, RerankProvider
from .settings import RerankSettings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from src import models


def _package_settings() -> Any:
    # The package this module belongs to, under whatever name it was imported.
    return sys.modules[cast(str, __package__)].settings


class _LiveSettings:
    """Reads ``honcho_rerank.settings`` on every access.

    The scheduler's per-dream proxy replaces that package attribute at run time, so
    nothing here may keep a reference to the object it found at import.
    """

    @property
    def RERANK(self) -> RerankSettings:  # noqa: N802 - mirrors the settings attribute
        return _package_settings().RERANK

    @property
    def CACHE(self) -> Any:  # noqa: N802 - Honcho's [cache] section
        return _package_settings().CACHE


settings = _LiveSettings()

logger = logging.getLogger(__name__)
_clients: WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient] = (
    WeakKeyDictionary()
)
_local_counts: dict[str, int] = {}
# hnsw.ef_search as read once at start (None: unknown), and the CANDIDATES values
# already warned about, so a hot-reloaded or per-dream pool size warns once each.
_ef_search: int | None = None
_ef_warned: set[int] = set()
COUNTER_EXPIRY_SECONDS = 2 * 24 * 60 * 60


@dataclass
class _Circuit:
    failures: int = 0
    until: float = 0.0
    probe: bool = False


_circuits: dict[tuple[str, str], _Circuit] = {}


def _circuit(config: RerankSettings) -> _Circuit:
    return _circuits.setdefault((config.PROVIDER, config.BASE_URL or ""), _Circuit())


_cap_hints: set[tuple[str, int]] = set()
_DEFAULT_URLS = {
    "voyage": "https://api.voyageai.com/v1",
    "cohere": "https://api.cohere.com/v2",
    "jina": "https://api.jina.ai/v1",
}
# Default request sizes, including TEI's --max-client-batch-size default.
_DEFAULT_MAX_DOCUMENTS = {"voyage": 1000, "cohere": 1000, "jina": 1000, "tei": 32}
VOYAGE_MAX_DOCUMENTS = 1000
COHERE_MAX_DOCUMENTS = 10000
_HARD_MAX_DOCUMENTS = {"voyage": VOYAGE_MAX_DOCUMENTS, "cohere": COHERE_MAX_DOCUMENTS}
# A ranking is a list of indices and scores: a few hundred bytes per row. A body past
# this size is a wrong base_url or a broken server, and is refused before it is held
# whole in the API or deriver process.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _ResponseTooLarge(ValueError):
    """The provider body passed MAX_RESPONSE_BYTES; it counts as malformed."""


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    """Read a streamed body, refusing it once it passes ``limit`` bytes."""
    declared = response.headers.get("content-length", "")
    if declared.strip().isdigit() and int(declared) > limit:
        raise _ResponseTooLarge("Rerank response too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise _ResponseTooLarge("Rerank response too large")
        chunks.append(chunk)
    return b"".join(chunks)


def note_ef_search(value: int | None) -> None:
    """Record the connection's hnsw.ef_search, read once by the entrypoint."""
    global _ef_search
    _ef_search = value


def warn_pool(config: RerankSettings) -> None:
    """Warn once per pool size, without making retrieval depend on logging."""
    try:
        ef_search = _ef_search
        if (
            not config.ENABLED
            or ef_search is None
            or ef_search >= config.CANDIDATES
            or config.CANDIDATES in _ef_warned
        ):
            return
        _ef_warned.add(config.CANDIDATES)
        logger.warning(
            "Rerank CANDIDATES=%d is above hnsw.ef_search=%d; without an iterative "
            + "scan the HNSW index returns about ef_search rows. Set "
            + "hnsw.ef_search >= CANDIDATES in PGOPTIONS",
            config.CANDIDATES,
            ef_search,
        )
    except Exception:
        pass


def should_rerank(caller: str | None, config: RerankSettings | None = None) -> bool:
    """Keep duplicate detection out of reranking even if explicitly configured."""
    config = config if config is not None else settings.RERANK
    warn_pool(config)
    return (
        config.ENABLED
        and caller is not None
        and caller != "dedupe"
        and caller in config.CALLERS
    )


def max_documents(config: RerankSettings | None = None) -> int:
    config = config if config is not None else settings.RERANK
    default = _DEFAULT_MAX_DOCUMENTS.get(config.PROVIDER)
    if default is None:
        return 0
    limit = config.MAX_DOCUMENTS or default
    return min(limit, _HARD_MAX_DOCUMENTS.get(config.PROVIDER, limit))


def candidate_count(top_k: int, config: RerankSettings | None = None) -> int:
    """Pool to fetch: the caller's top_k at least, never more than one request takes."""
    config = config if config is not None else settings.RERANK
    limit = max_documents(config)
    if not limit:
        return top_k
    return max(top_k, min(config.CANDIDATES, limit))


def skip_reason(config: RerankSettings | None = None) -> RerankOutcome | None:
    """Check cheap local gates before widening the vector search."""
    config = config if config is not None else settings.RERANK
    if config.PROVIDER not in _ADAPTERS:
        return "skipped_unsupported_provider"
    if _base_url(config) is None:
        return "skipped_missing_url"
    today = _utc_day()
    _prune_cap_hints(today)
    if (
        not config.BASE_URL
        and config.PROVIDER in ("voyage", "cohere", "jina")
        and (config.API_KEY is None or not config.API_KEY.get_secret_value().strip())
    ):
        return "skipped_missing_key"
    if config.DAILY_REQUEST_CAP == 0 or (
        config.DAILY_REQUEST_CAP is not None
        and (today, config.DAILY_REQUEST_CAP) in _cap_hints
    ):
        return "skipped_cap"
    state = _circuit(config)
    if state.probe or time.monotonic() < state.until:
        return "skipped_cooldown"
    return None


def _base_url(config: RerankSettings | None = None) -> str | None:
    """The configured API root, else the provider's hosted one (tei has none)."""
    config = config if config is not None else settings.RERANK
    return config.BASE_URL or _DEFAULT_URLS.get(config.PROVIDER)


def _rerank_url(base_url: str) -> httpx.URL:
    """Append /rerank to the root's path, so a trailing slash cannot double it."""
    root = httpx.URL(base_url)
    return root.copy_with(path=root.path.rstrip("/") + "/rerank")


def _provider_label(config: RerankSettings | None = None) -> RerankProvider:
    """Bound telemetry labels: a proxy may supply a provider name we do not know."""
    provider = (config if config is not None else settings.RERANK).PROVIDER
    if provider in _ADAPTERS:
        return cast(RerankProvider, provider)
    return "unsupported"


def _prune_cap_hints(today: str) -> None:
    yesterday = (datetime.fromisoformat(today).date() - timedelta(days=1)).isoformat()
    _cap_hints.intersection_update(
        entry for entry in _cap_hints if entry[0] in (today, yesterday)
    )


def _record_outcome(
    outcome: RerankOutcome,
    day: str | None,
    cap: int | None,
    config: RerankSettings | None = None,
) -> None:
    config = config if config is not None else settings.RERANK
    state = _circuit(config)
    if outcome == "reranked":
        state.failures = 0
        state.until = 0.0
    elif outcome in (
        "timeout",
        "http_error",
        "malformed_response",
        "internal_error",
        "skipped_counter",
    ):
        state.failures += 1
        if state.failures >= config.FAILURES_BEFORE_COOLDOWN:
            state.until = time.monotonic() + config.COOLDOWN_SECONDS
    elif outcome == "skipped_cap" and day is not None and cap is not None:
        _cap_hints.add((day, cap))
        _prune_cap_hints(_utc_day())


def _counter_key(day: str) -> str:
    return f"{cache_key_namespace()}:rerank:requests:{day}"


def _redis_counter_available(key: str) -> bool:
    """A failed Redis init can leave CACHE.ENABLED true with a memory backend."""
    try:
        return isinstance(cache._get_backend(key), Redis)
    except Exception:
        return False


async def _requests_for_day(day: str) -> int:
    if settings.CACHE.ENABLED:
        key = _counter_key(day)
        if not _redis_counter_available(key):
            raise ValueError("Shared rerank request counter unavailable")
        total = await cache.incr(key, value=1, expire=COUNTER_EXPIRY_SECONDS)
        if type(total) is not int or total < 1:
            raise ValueError("Rerank request counter unavailable")
        return total
    total = _local_counts[day] = _local_counts.get(day, 0) + 1
    yesterday = (datetime.fromisoformat(day).date() - timedelta(days=1)).isoformat()
    for old in list(_local_counts):
        if old not in (day, yesterday):
            del _local_counts[old]
    return total


def _utc_day() -> str:
    return datetime.now(UTC).date().isoformat()


def _get_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient()
        _clients[loop] = client
    return client


async def close_rerank_client() -> None:
    """Close the current loop's shared client before its event loop shuts down."""
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()


class _Adapter:
    """Translate provider field names without coupling accounting to wire format."""

    provider: str = ""
    top_field: str = "top_n"
    rows_field: str = "results"
    score_field: str = "relevance_score"
    return_documents: bool = False

    def request(
        self, query: str, texts: list[str], top_k: int, model: str
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "query": query, "documents": texts}
        body[self.top_field] = top_k
        if self.return_documents:
            body["return_documents"] = False
        return body

    def response_rows(self, body: Any) -> Any:
        return (
            cast(dict[str, Any], body).get(self.rows_field)
            if isinstance(body, dict)
            else None
        )

    def row_limit(self, count: int, top_n: int) -> int:
        """Most rows a valid answer holds: the ``top_n`` the request asked for."""
        return min(count, top_n)

    def ranking(
        self, body: Any, count: int, top_n: int | None = None
    ) -> list[tuple[int, float]]:
        """Validate the provider's answer against the request it answers.

        ``count`` documents were sent and ``top_n`` rows were asked for. Every index
        must be an int in range and unique, every score a finite number, and there may
        be no more rows than were requested. Any violation raises ``ValueError`` and
        the caller keeps the vector order.
        """
        rows: Any = self.response_rows(body)
        if not isinstance(rows, list):
            raise ValueError("Invalid rerank data")
        limit = self.row_limit(count, count if top_n is None else top_n)
        if len(cast(list[Any], rows)) > limit:
            raise ValueError("More rerank rows than requested")
        ranked: list[tuple[int, float]] = []
        seen: set[int] = set()
        for row in cast(list[Any], rows):
            if not isinstance(row, dict):
                raise ValueError("Invalid rerank row")
            entry = cast(dict[str, Any], row)
            index = entry.get("index")
            score = entry.get(self.score_field)
            if (
                type(index) is not int
                or not 0 <= index < count
                or index in seen
                or not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not math.isfinite(score)
            ):
                raise ValueError("Invalid rerank index or score")
            seen.add(index)
            ranked.append((index, float(score)))
        return sorted(ranked, key=lambda item: item[1], reverse=True)

    def usage(self, body: Any) -> tuple[int | None, float | None]:
        payload = cast(dict[str, Any], body) if isinstance(body, dict) else {}
        tokens = None
        billed = None
        if self.provider != "tei":
            usage = payload.get("usage")
            if isinstance(usage, dict):
                value = cast(dict[str, Any], usage).get("total_tokens")
                if type(value) is int and value >= 0:
                    tokens = value
        if self.provider == "cohere":
            meta = payload.get("meta")
            if isinstance(meta, dict):
                units = cast(dict[str, Any], meta).get("billed_units")
                if isinstance(units, dict):
                    value = cast(dict[str, Any], units).get("search_units")
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        try:
                            numeric = float(value)
                            if math.isfinite(numeric) and numeric >= 0:
                                billed = numeric
                        except OverflowError:
                            pass
        return tokens, billed


class _VoyageAdapter(_Adapter):
    provider: str = "voyage"
    top_field: str = "top_k"
    rows_field: str = "data"
    return_documents: bool = True


class _CohereAdapter(_Adapter):
    provider: str = "cohere"


class _JinaAdapter(_Adapter):
    provider: str = "jina"
    return_documents: bool = True


class _TEIAdapter(_Adapter):
    provider: str = "tei"
    score_field: str = "score"

    def request(
        self, query: str, texts: list[str], top_k: int, model: str
    ) -> dict[str, Any]:
        return {"query": query, "texts": texts, "truncate": True}

    def row_limit(self, count: int, top_n: int) -> int:
        # TEI takes no top_n and scores every text it was sent.
        return count

    def response_rows(self, body: Any) -> Any:
        return body


_ADAPTERS: dict[str, _Adapter] = {
    "voyage": _VoyageAdapter(),
    "cohere": _CohereAdapter(),
    "jina": _JinaAdapter(),
    "tei": _TEIAdapter(),
}


def _emit_completed(
    caller: str,
    candidates: int,
    results: int,
    duration_ms: float,
    tokens: int | None,
    billed_units: float | None,
    daily_requests: int | None,
    outcome: RerankOutcome,
    daily_request_cap: int | None,
    *,
    workspace_name: str | None = None,
    run_id: str | None = None,
    config: RerankSettings | None = None,
) -> None:
    config = config if config is not None else settings.RERANK
    try:
        # Looked up on each call so Honcho's emit can be replaced (tests, hooks).
        from src.telemetry.events import emit

        from .events import RerankCallCompletedEvent

        emit(
            RerankCallCompletedEvent(
                caller=caller,
                workspace_name=workspace_name,
                run_id=run_id,
                provider=_provider_label(config),
                model=config.MODEL,
                outcome=outcome,
                candidates_sent=candidates,
                results_returned=results,
                duration_ms=duration_ms,
                tokens=tokens,
                billed_units=billed_units,
                daily_requests=daily_requests,
                daily_request_cap=daily_request_cap,
            )
        )
    except Exception:
        pass


async def rerank_documents(
    query: str,
    documents: Sequence[models.Document],
    top_k: int,
    caller: str,
    *,
    workspace_name: str | None = None,
    run_id: str | None = None,
    config: RerankSettings | None = None,
) -> Sequence[models.Document]:
    """Rerank detached candidates; failures preserve the original vector results.

    ``config`` is the settings object the search already read; without it the
    settings are read here, once.
    """
    fallback = documents[: max(0, top_k)]
    started = time.perf_counter()
    outcome: RerankOutcome = "internal_error"
    cap = None
    tokens = None
    billed_units = None
    daily_requests = None
    sent = 0
    day = None
    result = fallback
    # The candidates are fetched before this call, so a settings proxy that
    # stops answering in between must not cost the caller its results. Nothing
    # is recorded until the caller is known to be listed: a disabled or
    # unlisted caller emits nothing, and a failed settings read cannot tell.
    listed = False
    probe: _Circuit | None = None
    try:
        if config is None:
            config = settings.RERANK
        if not should_rerank(caller, config):
            return fallback
        cap = config.DAILY_REQUEST_CAP
        listed = True
        skipped = skip_reason(config)
        limit = max_documents(config)
        if skipped is not None:
            outcome = skipped
        elif not documents or top_k <= 0:
            outcome = "empty_result"
        elif len(documents) == 1 and config.MIN_SCORE is None:
            outcome = "noop_single"
        elif top_k > limit:
            outcome = "too_many_candidates"
        else:
            candidates = documents[:limit]
            adapter = _ADAPTERS.get(config.PROVIDER)
            if adapter is None:
                outcome = "skipped_unsupported_provider"
                return fallback
            base_url = _base_url(config)
            if base_url is None:
                outcome = "skipped_missing_url"
                return fallback
            url = _rerank_url(base_url)
            headers: dict[str, str] = {}
            if config.API_KEY and config.API_KEY.get_secret_value().strip():
                headers["Authorization"] = f"Bearer {config.API_KEY.get_secret_value()}"
            payload = adapter.request(
                query[: config.MAX_QUERY_CHARS],
                [doc.content for doc in candidates],
                min(top_k, len(candidates)),
                config.MODEL,
            )
            state = _circuit(config)
            # No await between the gate and reservation: one task owns the probe.
            if state.until:
                state.probe = True
                probe = state
            async with asyncio.timeout(config.TIMEOUT_SECONDS):
                if cap is not None:
                    day = _utc_day()
                    outcome = "skipped_counter"
                    daily_requests = await _requests_for_day(day)
                    if daily_requests > cap:
                        outcome = "skipped_cap"
                if outcome != "skipped_cap":
                    outcome = "http_error"
                    client = _get_client()
                    sent = len(candidates)
                    top_n = min(top_k, len(candidates))
                    # Streamed, so an oversized body is refused before it is held.
                    async with client.stream(
                        "POST",
                        url,
                        headers=headers,
                        json=payload,
                        timeout=config.TIMEOUT_SECONDS,
                    ) as response:
                        response.raise_for_status()
                        outcome = "malformed_response"
                        raw = await _read_capped(response, MAX_RESPONSE_BYTES)
                    body = json.loads(raw)
                    tokens, billed_units = adapter.usage(body)
                    ranked = adapter.ranking(body, len(candidates), top_n)
                    if len(ranked) >= top_n:
                        result = [
                            candidates[index]
                            for index, score in ranked
                            if config.MIN_SCORE is None or score >= config.MIN_SCORE
                        ][:top_k]
                        outcome = "reranked"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except (TimeoutError, httpx.TimeoutException):
        # The deadline also covers the counter; a stalled counter is a counter
        # failure, not a provider timeout, and no request was sent.
        if outcome != "skipped_counter":
            outcome = "timeout"
    except httpx.HTTPError:
        outcome = "http_error"
    except Exception:
        # Exception messages may contain request bodies or credentials.
        if outcome == "http_error":
            outcome = "internal_error"
    finally:
        if probe is not None:
            probe.probe = False
            if outcome != "reranked" and config is not None:
                # Cancellation and counter/cap exits must also release and defer probing.
                probe.until = time.monotonic() + config.COOLDOWN_SECONDS
        if listed:
            _complete(
                caller,
                outcome,
                day,
                cap,
                sent,
                len(result),
                (time.perf_counter() - started) * 1000,
                tokens,
                billed_units,
                daily_requests,
                workspace_name=workspace_name,
                run_id=run_id,
                config=config,
            )
    return result


def _complete(
    caller: str,
    outcome: RerankOutcome,
    day: str | None,
    cap: int | None,
    sent: int,
    returned: int,
    duration_ms: float,
    tokens: int | None,
    billed_units: float | None,
    daily_requests: int | None,
    *,
    workspace_name: str | None = None,
    run_id: str | None = None,
    config: RerankSettings | None = None,
) -> None:
    """Back-off, log and telemetry for one attempt, each isolated from the others."""
    try:  # noqa: SIM105 - Keep each completion operation isolated.
        _record_outcome(outcome, day, cap, config)
    except Exception:
        pass
    try:
        if outcome != "reranked":
            _log_fallback(caller, outcome, config)
    except Exception:
        pass
    try:  # noqa: SIM105 - Keep each completion operation isolated.
        _emit_completed(
            caller,
            sent,
            returned,
            duration_ms,
            tokens,
            billed_units,
            daily_requests,
            outcome,
            cap,
            workspace_name=workspace_name,
            run_id=run_id,
            config=config,
        )
    except Exception:
        pass


def record_rerank_fallback(
    caller: str,
    documents: Sequence[models.Document],
    top_k: int,
    reason: RerankOutcome,
    *,
    workspace_name: str | None = None,
    run_id: str | None = None,
    config: RerankSettings | None = None,
) -> Sequence[models.Document]:
    """Record an integration fallback without making a provider request."""
    result = documents[: max(0, top_k)]
    try:
        if config is None:
            config = settings.RERANK
        enabled = should_rerank(caller, config)
        cap = config.DAILY_REQUEST_CAP
    except Exception:
        return result
    if enabled:
        try:  # noqa: SIM105 - Keep each completion operation isolated.
            _record_outcome(reason, _utc_day(), cap, config)
        except Exception:
            pass
        try:  # noqa: SIM105 - Keep each completion operation isolated.
            _log_fallback(caller, reason, config)
        except Exception:
            pass
        try:  # noqa: SIM105 - Keep each completion operation isolated.
            _emit_completed(
                caller,
                0,
                len(result),
                0.0,
                None,
                None,
                None,
                reason,
                cap,
                workspace_name=workspace_name,
                run_id=run_id,
                config=config,
            )
        except Exception:
            pass
    return result


def _log_fallback(
    caller: str, outcome: RerankOutcome, config: RerankSettings | None = None
) -> None:
    # Names only: no query, document text, URL or key.
    logger.info(
        "Rerank fallback caller=%s provider=%s reason=%s",
        caller,
        (config if config is not None else settings.RERANK).PROVIDER,
        outcome,
    )


async def search(
    fetch: Callable[..., Awaitable[Sequence[models.Document]]],
    db: AsyncSession | None,
    workspace_name: str,
    query: str,
    *,
    observer: str,
    observed: str,
    filters: dict[str, Any] | None = None,
    max_distance: float | None = None,
    top_k: int = 5,
    embedding: list[float] | None = None,
    caller: str | None = None,
    run_id: str | None = None,
) -> Sequence[models.Document]:
    """Search memory, optionally reranking a larger vector candidate set.

    ``fetch`` is Honcho's own ``query_documents``. A reranked search calls it with
    ``db=None``, so it opens its own short read-only session, detaches the rows and
    closes the session before the provider request. A caller session inside a
    transaction is never released: that search keeps its plain vector order
    (``skipped_transaction``). Disabled and unlisted callers retain the original
    vector search behaviour. ``run_id`` only tags the rerank event.
    """
    if top_k <= 0:
        return []
    try:
        config = settings.RERANK
        rerank = should_rerank(caller, config)
        # A rerank that would certainly fall back (no key or URL, cap reached,
        # provider in cooldown) keeps the caller's own top_k search instead of
        # widening it.
        skipped = skip_reason(config) if rerank else None
        if skipped is not None:
            rerank = False
        pool = candidate_count(top_k, config) if rerank else top_k
    except Exception:
        # Reranking settings can come from a per-call proxy; a broken one must
        # not fail the search, so run the stock search on the caller's session.
        documents = await fetch(
            db,
            workspace_name,
            query,
            observer=observer,
            observed=observed,
            filters=filters,
            max_distance=max_distance,
            top_k=top_k,
            embedding=embedding,
        )
        if caller is None:
            return documents
        return record_rerank_fallback(
            caller,
            documents,
            top_k,
            "internal_error",
            workspace_name=workspace_name,
            run_id=run_id,
        )
    active_transaction = rerank and db is not None and db.in_transaction()
    if active_transaction:
        # A caller-owned transaction cannot be released without losing writes.
        # Fail open rather than hold its connection across an HTTP request.
        rerank = False
    try:
        documents = await fetch(
            None if rerank else db,
            workspace_name,
            query,
            observer=observer,
            observed=observed,
            filters=filters,
            max_distance=max_distance,
            top_k=pool if rerank else top_k,
            embedding=embedding,
        )
    except Exception:
        if not rerank:
            raise
        documents = await fetch(
            db,
            workspace_name,
            query,
            observer=observer,
            observed=observed,
            filters=filters,
            max_distance=max_distance,
            top_k=top_k,
            embedding=embedding,
        )
        if caller is None:
            return documents
        return record_rerank_fallback(
            caller,
            documents,
            top_k,
            "internal_error",
            workspace_name=workspace_name,
            run_id=run_id,
            config=config,
        )
    if skipped is not None and caller is not None:
        return record_rerank_fallback(
            caller,
            documents,
            top_k,
            skipped,
            workspace_name=workspace_name,
            run_id=run_id,
            config=config,
        )
    if active_transaction and caller is not None:
        return record_rerank_fallback(
            caller,
            documents,
            top_k,
            "skipped_transaction",
            workspace_name=workspace_name,
            run_id=run_id,
            config=config,
        )
    if rerank and caller is not None:
        if not documents:
            return record_rerank_fallback(
                caller,
                documents,
                top_k,
                "empty_result",
                workspace_name=workspace_name,
                run_id=run_id,
                config=config,
            )
        return await rerank_documents(
            query,
            documents,
            top_k,
            caller,
            workspace_name=workspace_name,
            run_id=run_id,
            config=config,
        )
    return documents
