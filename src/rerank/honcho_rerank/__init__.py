"""Reranking for Honcho conclusion searches, installed at run time without source edits.

``honcho_rerank.settings`` is the settings object: the module itself, whose ``RERANK`` is the
current ``RerankSettings``. The scheduler's per-dream proxy may replace this package
attribute; ``honcho_rerank.client`` reads it on every call.
"""

from . import settings as settings

__all__ = ["settings"]
