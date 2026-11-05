"""Fakes for the Honcho functions the hook wraps."""

import functools
from typing import Any
from unittest.mock import AsyncMock

from src.crud import document

ORIGINAL_QUERY = document.query_documents


def fake_query(rows: list[Any] | None = None) -> Any:
    """A stand-in for query_documents with its real signature; ``.mock`` records calls."""
    mock = AsyncMock(return_value=list(rows or []))

    @functools.wraps(ORIGINAL_QUERY)
    async def fake(*args: Any, **kwargs: Any) -> Any:
        return await mock(*args, **kwargs)

    setattr(fake, "mock", mock)  # noqa: B010
    return fake
