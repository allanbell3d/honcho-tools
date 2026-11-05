"""Search backends, rerankers and the arm runner.

An *arm* is one way of answering a search call:

    stock                          the server's own settings: ef_search left at its default, no iterative scan
    scan[:EF[:MODE]]               iterative scan, ef_search EF (default max(40, top_k)), MODE relaxed_order|strict_order
    exact                          no index: every matching vector is compared and sorted (the ceiling, 200 rows)
    rerank:MODEL:POOL[:EF[:MODE]]  fetch POOL candidates (ef_search default max(40, POOL), relaxed_order), rerank them,
                                   keep the caller's top_k; any reranker failure keeps the vector order (fail open)

Backends
    MemoryBackend  in-memory corpus with a *model* of an index that filters after the scan (offline demo and tests)
    PgBackend      a Honcho-style PostgreSQL ``documents`` table with pgvector (needs ``psycopg``; read-only session)

The PostgreSQL backend is a portable re-implementation of the search, not Honcho's own ``query_documents`` (which the
original study imported and ran). It supports the filters the study's calls used (peer pair, ``level`` in a list,
``max_distance``), nothing else. Replace it with a call into Honcho if you need exact parity.
"""
from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from benchlib import read_jsonl, stable_seed

DEFAULT_EF = 40  # pgvector's default hnsw.ef_search
EXACT_LIMIT = 200
ITERATIVE_MODES = ("off", "relaxed_order", "strict_order")
MAX_SCAN_TUPLES = 20000  # pgvector's default hnsw.max_scan_tuples, used by the in-memory model


# --------------------------------------------------------------------------------------------------------------
# Arms
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Arm:
    name: str
    kind: str  # stock | scan | exact | rerank
    ef_search: int | None = None  # None: choose per call (scan, rerank) or server default (stock)
    iterative: str = "off"
    model: str | None = None
    pool: int | None = None
    min_score: float | None = None


def parse_arm(spec: str, min_score: float | None = None) -> Arm:
    parts = spec.split(":")
    kind = parts[0]
    if kind in ("stock", "exact"):
        if len(parts) != 1:
            raise ValueError(f"arm {spec!r} takes no parameters")
        return Arm(name=spec, kind=kind)
    if kind == "scan":
        if len(parts) > 3:
            raise ValueError(f"bad arm {spec!r}")
        ef = int(parts[1]) if len(parts) > 1 and parts[1] else None
        mode = parts[2] if len(parts) > 2 else "relaxed_order"
        _check_mode(mode)
        return Arm(name=spec, kind="scan", ef_search=ef, iterative=mode)
    if kind == "rerank":
        if not 3 <= len(parts) <= 5:
            raise ValueError(f"bad arm {spec!r}: rerank:MODEL:POOL[:EF[:MODE]]")
        pool = int(parts[2])
        if pool < 1:
            raise ValueError("pool must be positive")
        ef = int(parts[3]) if len(parts) > 3 and parts[3] else None
        mode = parts[4] if len(parts) > 4 else "relaxed_order"
        _check_mode(mode)
        return Arm(name=spec, kind="rerank", ef_search=ef, iterative=mode, model=parts[1], pool=pool, min_score=min_score)
    raise ValueError(f"unknown arm {spec!r}")


def _check_mode(mode: str) -> None:
    if mode not in ITERATIVE_MODES:
        raise ValueError(f"iterative scan mode must be one of {ITERATIVE_MODES}")


def search_settings(arm: Arm, top_k: int) -> tuple[int, int | None, str]:
    """(rows to fetch, ef_search to set or None for the server default, iterative mode) for one call."""
    if arm.kind == "stock":
        return top_k, None, "off"
    if arm.kind == "scan":
        return top_k, arm.ef_search or max(DEFAULT_EF, top_k), arm.iterative
    if arm.kind == "rerank":
        fetch = max(top_k, arm.pool or top_k)
        return fetch, arm.ef_search or max(DEFAULT_EF, fetch), arm.iterative
    raise ValueError("exact has no index settings")


# --------------------------------------------------------------------------------------------------------------
# Vectors and the embedding cache
# --------------------------------------------------------------------------------------------------------------
def cosine_distance(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return math.inf
    return 1.0 - sum(x * y for x, y in zip(a, b)) / (na * nb)


def embedding_key(model: str, dims: int, query: str) -> str:
    import hashlib

    return hashlib.sha256(json.dumps([model, dims, query], ensure_ascii=False).encode()).hexdigest()


def load_embeddings(path: str | Path) -> dict[str, list[float]]:
    return {row["cache_key"]: row["embedding"] for row in read_jsonl(path)}


# --------------------------------------------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Hit:
    id: str
    content: str
    score: float  # cosine similarity (1 - distance)


def _level_filter(call: dict) -> set[str] | None:
    flt = call.get("filters") or {}
    if not isinstance(flt, dict) or set(flt) - {"level"}:
        raise ValueError("unsupported filter: only level equality or {'level': {'in': [...]}} is supported")
    level = flt.get("level")
    if level is None:
        return None
    if isinstance(level, dict) and "in" in level:
        return set(level["in"])
    if isinstance(level, str):
        return {level}
    raise ValueError("unsupported filter: only level equality or {'level': {'in': [...]}} is supported")


class MemoryBackend:
    """In-memory corpus. ``search`` models an index that scans the *whole table* in distance order and filters after.

    * ``iterative == "off"``: take the ``ef_search`` nearest rows of the whole table, then keep those that match the
      call's peer pair and filters. A small peer pair inside a big table can leave fewer than ``limit`` rows.
    * ``iterative`` on: keep scanning past ``ef_search`` until ``limit`` matching rows are found or MAX_SCAN_TUPLES.

    This reproduces the post-filter *effect* that pgvector's HNSW index shows; it is not a model of graph search.
    """

    def __init__(self, documents: list[dict]) -> None:
        self.documents = documents
        self._by_workspace: dict[str, list[dict]] = {}
        for doc in documents:
            self._by_workspace.setdefault(doc["workspace_name"], []).append(doc)

    @classmethod
    def from_file(cls, path: str | Path) -> "MemoryBackend":
        return cls(read_jsonl(path))

    @staticmethod
    def _matches(doc: dict, call: dict, vector: list[float], levels: set[str] | None) -> bool:
        if doc.get("deleted_at") or doc["observer"] != call["observer"] or doc["observed"] != call["observed"]:
            return False
        if levels is not None and doc.get("level") not in levels:
            return False
        if call.get("max_distance") is not None and cosine_distance(doc["embedding"], vector) > call["max_distance"]:
            return False
        return True

    def _ordered(self, call: dict, vector: list[float]) -> list[tuple[float, dict]]:
        scored = [(cosine_distance(d["embedding"], vector), d) for d in self._by_workspace.get(call["workspace_name"], [])]
        scored.sort(key=lambda item: (item[0], item[1]["id"]))
        return scored

    def search(self, call: dict, vector: list[float], limit: int, ef_search: int | None, iterative: str) -> list[Hit]:
        ef = ef_search or DEFAULT_EF
        levels = _level_filter(call)
        out: list[Hit] = []
        for scanned, (dist, doc) in enumerate(self._ordered(call, vector), 1):
            if self._matches(doc, call, vector, levels):
                out.append(Hit(doc["id"], doc["content"], 1.0 - dist))
            if scanned >= ef:
                if iterative == "off" or len(out) >= limit or scanned >= MAX_SCAN_TUPLES:
                    break
        return out[:limit]

    def exact(self, call: dict, vector: list[float], limit: int = EXACT_LIMIT) -> list[Hit]:
        levels = _level_filter(call)
        hits = [Hit(d["id"], d["content"], 1.0 - dist) for dist, d in self._ordered(call, vector)
                if self._matches(d, call, vector, levels)]
        return hits[:limit]


def build_search_sql(*, exact: bool, has_levels: bool, has_max_distance: bool) -> str:
    """SQL for the PostgreSQL backend. Only fixed fragments are concatenated; every value is a bound parameter."""
    where = ["workspace_name = %(workspace)s", "observer = %(observer)s", "observed = %(observed)s",
             "deleted_at IS NULL", "embedding IS NOT NULL"]
    if has_levels:
        where.append("level = ANY(%(levels)s)")
    if has_max_distance:
        where.append("(embedding <=> %(vector)s::vector) <= %(max_distance)s")
    # "+ 0" hides the distance from the planner so no index can order the rows: the exhaustive ceiling.
    order = "(embedding <=> %(vector)s::vector) + 0, id" if exact else "embedding <=> %(vector)s::vector, id"
    return ("SELECT id, content, 1 - (embedding <=> %(vector)s::vector) AS score FROM documents WHERE "
            + " AND ".join(where) + f" ORDER BY {order} LIMIT %(limit)s")


class PgBackend:
    """PostgreSQL + pgvector backend over a Honcho-style ``documents`` table. Sessions are read-only."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _run(self, call: dict, vector: list[float], limit: int, ef: int | None, mode: str, exact: bool) -> list[Hit]:
        _check_mode(mode)  # whitelist first: the mode is the only value formatted into a SET statement
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("PgBackend needs psycopg: pip install 'psycopg[binary]'") from exc
        levels = _level_filter(call)
        sql = build_search_sql(exact=exact, has_levels=levels is not None,
                               has_max_distance=call.get("max_distance") is not None)
        params = dict(workspace=call["workspace_name"], observer=call["observer"], observed=call["observed"],
                      vector="[" + ",".join(repr(float(x)) for x in vector) + "]", limit=int(limit))
        if levels is not None:
            params["levels"] = sorted(levels)
        if call.get("max_distance") is not None:
            params["max_distance"] = float(call["max_distance"])
        with psycopg.connect(self.dsn, options="-c default_transaction_read_only=on") as conn:
            with conn.transaction():
                if not exact and ef is not None:
                    conn.execute(f"SET LOCAL hnsw.ef_search = {int(ef)}")  # int(): never a string from outside
                    if mode != "off":
                        conn.execute(f"SET LOCAL hnsw.iterative_scan = '{mode}'")  # whitelisted above
                rows = conn.execute(sql, params).fetchall()
        return [Hit(str(r[0]), r[1], float(r[2])) for r in rows]

    def search(self, call, vector, limit, ef_search, iterative):  # noqa: ANN001
        return self._run(call, vector, limit, ef_search, iterative, exact=False)

    def exact(self, call, vector, limit=EXACT_LIMIT):  # noqa: ANN001
        return self._run(call, vector, limit, None, "off", exact=True)


# --------------------------------------------------------------------------------------------------------------
# Rerankers
# --------------------------------------------------------------------------------------------------------------
class RerankError(Exception):
    """A reranker failure that must not fail the search. ``reason`` is the recorded fallback reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RerankResult:
    order: list[tuple[int, float]]  # (index into the documents sent, relevance score)
    tokens: int | None


def token_bound(query: str, documents: list[str]) -> int:
    """Upper bound on billed tokens: UTF-8 bytes (a token is at least one byte), the query billed once per document."""
    q = len(query.encode("utf-8"))
    return sum(q + len(d.encode("utf-8")) for d in documents)


class VoyageReranker:
    """Voyage-compatible ``POST {base_url}/rerank``. The key comes from the environment variable you name."""

    def __init__(self, model: str, *, api_key: str, base_url: str = "https://api.voyageai.com/v1",
                 timeout_seconds: float = 3.0) -> None:
        if not api_key:
            raise ValueError("a reranker API key is required")
        self.model, self.base_url, self.timeout, self._key = model, base_url.rstrip("/"), timeout_seconds, api_key

    def rerank(self, query: str, documents: list[str]) -> RerankResult:
        body = json.dumps({"query": query, "documents": documents, "model": self.model}).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/rerank", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._key})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - https base_url
                payload = json.loads(response.read())
        except TimeoutError as exc:
            raise RerankError("timeout") from exc
        except urllib.error.URLError as exc:
            raise RerankError("timeout" if isinstance(exc.reason, TimeoutError) else "http_error") from exc
        except (OSError, ValueError) as exc:
            raise RerankError("http_error") from exc
        return parse_rerank_payload(payload, len(documents))


def parse_rerank_payload(payload: object, n_documents: int) -> RerankResult:
    """Validate a Voyage-style answer; anything unusable is a ``malformed_response`` (and so a fallback)."""
    try:
        rows = payload["data"]  # type: ignore[index]
        order = [(int(r["index"]), float(r["relevance_score"])) for r in rows]
        tokens = (payload.get("usage") or {}).get("total_tokens")  # type: ignore[union-attr]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RerankError("malformed_response") from exc
    indexes = [i for i, _ in order]
    if (len(set(indexes)) != len(indexes) or any(not 0 <= i < n_documents for i in indexes)
            or any(not math.isfinite(s) for _, s in order) or len(order) < min(n_documents, 1)):
        raise RerankError("malformed_response")
    return RerankResult(order=order, tokens=int(tokens) if isinstance(tokens, (int, float)) else None)


_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an and are as at be by for from how in is it of on or that the to was what when where which who "
                  "why with does do did about case".split())


class LexicalReranker:
    """Offline stand-in: IDF-weighted word overlap between query and each candidate. Deterministic, no network.

    It exists so the pipeline runs without a provider. It is not a model of Voyage; the demo uses it only to show
    the reranking step reorders candidates and fails open.
    """

    def rerank(self, query: str, documents: list[str]) -> RerankResult:
        q = {w for w in _WORD.findall(query.lower()) if w not in _STOP}
        docs = [set(_WORD.findall(d.lower())) for d in documents]
        n = max(1, len(docs))
        scores = []
        for i, words in enumerate(docs):
            score = 0.0
            for w in q & words:
                df = sum(w in other for other in docs)
                score += math.log(1 + n / df)
            scores.append((i, score / max(1, len(q))))
        tokens = sum(len(query.split()) + len(d.split()) for d in documents)
        return RerankResult(order=scores, tokens=tokens)


class FlakyReranker:
    """Wraps a reranker and fails a stable, seeded fraction of calls, to exercise the fail-open path offline."""

    def __init__(self, inner, fraction: float, seed: int = 0, reason: str = "timeout") -> None:  # noqa: ANN001
        self.inner, self.fraction, self.seed, self.reason = inner, fraction, seed, reason

    def rerank(self, query: str, documents: list[str]) -> RerankResult:
        if (stable_seed("flaky", self.seed, query) % 10_000) / 10_000 < self.fraction:
            raise RerankError(self.reason)
        return self.inner.rerank(query, documents)


# --------------------------------------------------------------------------------------------------------------
# Running one call through one arm
# --------------------------------------------------------------------------------------------------------------
def run_arm(backend, reranker, call: dict, vector: list[float], arm: Arm) -> dict:  # noqa: ANN001
    """Answer one search call with one arm and return the ranking row (ids in order, timing, fallback reason)."""
    top_k = int(call["top_k"])
    started = time.perf_counter()
    row: dict = dict(call_id=call["call_id"], arm=arm.name, top_k=top_k, fallback_reason=None, outcome="unranked",
                     provider_ms=0.0, rerank_tokens=0, candidate_ids=[])
    if arm.kind == "exact":
        hits = backend.exact(call, vector, EXACT_LIMIT)
        row.update(rows_asked=EXACT_LIMIT, outcome="exact")
        ranked = hits
        db_ms = (time.perf_counter() - started) * 1000
    else:
        fetch, ef, mode = search_settings(arm, top_k)
        hits = backend.search(call, vector, fetch, ef, mode)
        db_ms = (time.perf_counter() - started) * 1000
        row.update(rows_asked=top_k, candidate_ids=[h.id for h in hits] if arm.kind == "rerank" else [])
        ranked = hits[:top_k]
        if arm.kind == "rerank":
            ranked, extra = _rerank(reranker, call["query"], hits, top_k, arm.min_score)
            row.update(extra)
    pool_docs = {h.id: h.content for h in hits[:top_k]}
    pool_docs.update({h.id: h.content for h in ranked})
    row.update(
        ids=[h.id for h in ranked], scores=[round(h.score, 6) for h in ranked], rows_returned=len(ranked),
        db_ms=db_ms, total_ms=(time.perf_counter() - started) * 1000,
        documents=[dict(id=i, content=c) for i, c in pool_docs.items()],
    )
    return row


def _rerank(reranker, query: str, hits: list[Hit], top_k: int, min_score: float | None) -> tuple[list[Hit], dict]:  # noqa: ANN001
    if len(hits) <= 1 and min_score is None:
        return hits[:top_k], dict(outcome="noop_single")
    started = time.perf_counter()
    try:
        result = reranker.rerank(query, [h.content for h in hits])
    except RerankError as exc:
        return hits[:top_k], dict(outcome=exc.reason, fallback_reason=exc.reason,
                                  provider_ms=(time.perf_counter() - started) * 1000)
    provider_ms = (time.perf_counter() - started) * 1000
    ordered = sorted(result.order, key=lambda item: (-item[1], item[0]))
    ranked = [Hit(hits[i].id, hits[i].content, score) for i, score in ordered]
    if min_score is not None:
        ranked = [h for h in ranked if h.score >= min_score]
    return ranked[:top_k], dict(outcome="reranked", provider_ms=provider_ms, rerank_tokens=result.tokens or 0)
