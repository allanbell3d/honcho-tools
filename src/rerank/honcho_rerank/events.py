"""Content-free per-call reranking telemetry, emitted through Honcho's ``emit``."""

from typing import ClassVar, Literal

from src.telemetry.events.base import BaseEvent

RerankCaller = Literal["dream", "dialectic", "prefetch", "conclusions_api"]
# "unsupported" stands in for any provider name the adapters do not know (a
# settings proxy can supply one), so labels stay bounded.
RerankProvider = Literal["voyage", "cohere", "jina", "tei", "unsupported"]
RerankOutcome = Literal[
    "reranked",
    "cancelled",
    "skipped_unsupported_provider",
    "skipped_missing_url",
    "skipped_missing_key",
    "skipped_cap",
    "skipped_cooldown",
    "skipped_counter",
    "skipped_transaction",
    "empty_result",
    "noop_single",
    "too_many_candidates",
    "timeout",
    "http_error",
    "malformed_response",
    "internal_error",
]


class RerankCallCompletedEvent(BaseEvent):
    _event_type: ClassVar[str] = "rerank.call.completed"
    _schema_version: ClassVar[int] = 1
    _category: ClassVar[str] = "rerank"
    _volume_class: ClassVar[str] = "high_volume"

    caller: str
    # Correlation with the workspace and the dream or dialectic run that searched;
    # None when the caller has no run (the conclusions route).
    workspace_name: str | None = None
    run_id: str | None = None
    provider: RerankProvider
    model: str
    candidates_sent: int
    results_returned: int
    duration_ms: float
    tokens: int | None
    billed_units: float | None
    daily_requests: int | None
    daily_request_cap: int | None
    outcome: RerankOutcome

    def get_resource_id(self) -> str:
        return f"{self.caller}:{self.model}:{self.timestamp.isoformat()}"
