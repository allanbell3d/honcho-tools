"""Which run a search belongs to, without editing Honcho.

``rerank_run`` holds the run id for the work in progress:

- In the deriver, ``run_dream`` is wrapped. A token is set before the dream starts (its
  real ``run_id`` is generated inside the call) and the mapping token -> ``run_id`` is
  logged and kept after it returns. Dream searches through the agent tools normally carry
  the dream's own ``run_id`` from the tool context; the token is the fallback.
- In the API, ``DialecticAgent.__init__`` is wrapped. After construction the variable is
  set to the agent's ``_run_id``, so prefetch searches carry the same id as the
  ``dialectic.completed`` event. The value stays set for the rest of the task that built
  the agent (one request).
"""

from __future__ import annotations

import logging
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)

rerank_run: ContextVar[str | None] = ContextVar("rerank_run", default=None)

_MAX_DREAM_TOKENS = 256
_dream_runs: OrderedDict[str, str] = OrderedDict()


def current_run_id() -> str | None:
    """The run id set for the current task, or None outside a dream or chat."""
    return rerank_run.get()


def dream_run_id(token: str) -> str | None:
    """The dream ``run_id`` a token stood for, once that dream has returned."""
    return _dream_runs.get(token)


def _remember(token: str, run_id: str) -> None:
    _dream_runs[token] = run_id
    while len(_dream_runs) > _MAX_DREAM_TOKENS:
        _dream_runs.popitem(last=False)


def wrap_run_dream(
    original: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Set a run token around ``run_dream`` and log token -> ``result.run_id``."""
    if getattr(original, "__rerank_wrapped__", None) is not None:
        return original

    @wraps(original)
    async def run_dream(*args: Any, **kwargs: Any) -> Any:
        token = f"rr-{secrets.token_hex(6)}"
        reset = rerank_run.set(token)
        try:
            result = await original(*args, **kwargs)
        finally:
            rerank_run.reset(reset)
        run_id = getattr(result, "run_id", None)
        if isinstance(run_id, str) and run_id:
            _remember(token, run_id)
            logger.info("rerank run token=%s dream run_id=%s", token, run_id)
        return result

    setattr(run_dream, "__rerank_wrapped__", original)  # noqa: B010
    return run_dream


def wrap_dialectic_init(cls: type) -> None:
    """Set ``rerank_run`` to the agent's ``_run_id`` after each construction."""
    original: Callable[..., None] = cls.__init__
    if getattr(original, "__rerank_wrapped__", None) is not None:
        return

    @wraps(original)
    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        run_id = getattr(self, "_run_id", None)
        if isinstance(run_id, str) and run_id:
            rerank_run.set(run_id)

    setattr(__init__, "__rerank_wrapped__", original)  # noqa: B010
    setattr(cls, "__init__", __init__)  # noqa: B010
