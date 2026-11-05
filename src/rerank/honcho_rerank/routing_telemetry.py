"""Carry executed route evidence through Honcho 3.5 telemetry, without source edits.

SDK clients are cached, so they are never mutated. Each executor call gets its own
context and a proxy around its temporary backend's client. Only identifiers and
usage are copied; prompts and response content are not added to telemetry here.
"""
from __future__ import annotations

import functools
import logging
from contextvars import ContextVar
from typing import Any

from pydantic import create_model

logger = logging.getLogger(__name__)
_state: ContextVar[dict | None] = ContextVar("executed_route", default=None)
_event_classes: dict[type, type] = {}
_EVENTS = {"llm.call.completed", "llm.call.traced"}


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _identifier(value):
    if isinstance(value, str) and 0 < len(value) <= 200 and all(c.isalnum() or c in "-_.:/@" for c in value):
        return value
    return None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def response_evidence(raw, requested: str, transport: str) -> dict:
    usage = _get(raw, "usage")
    route = _get(usage, "billing_route")
    backend = _identifier(_get(usage, "backend"))
    resolved = _identifier(_get(usage, "resolved_model"))
    actual = _identifier(_get(raw, "model")) or _identifier(_get(raw, "model_version"))
    metadata = {"requested_model": requested, "routing_evidence": "unresolved"}
    if actual and actual != "stage-route":
        metadata["model"] = actual
    if route in ("api", "cli") and backend and resolved:
        metadata.update(backend=backend, resolved_model=resolved, billing_route=route, routing_evidence="bridge_response")
        metadata.setdefault("model", resolved)
    elif transport in ("anthropic", "gemini"):
        metadata.update(backend=transport, billing_route="api", routing_evidence="direct_transport")
    return metadata


class _ObservedStream:
    def __init__(self, original, state):
        self.original = original
        self.iterator = original.__aiter__()
        self.state = state

    def __aiter__(self):
        return self

    async def __anext__(self):
        chunk = await self.iterator.__anext__()
        model = _identifier(_get(chunk, "model"))
        if model and model != "stage-route":
            self.state["stream_model"] = model
        # Keep the final usage chunk, rather than replacing it with a later
        # finish-only chunk. A single object per call bounds memory use.
        if _get(chunk, "usage") is not None:
            self.state["raw_chunk"] = chunk
        return chunk

    def __getattr__(self, name):
        return getattr(self.original, name)


class _CompletionsProxy:
    def __init__(self, original, state):
        self.original, self.state = original, state

    async def create(self, *args, **kwargs):
        response = await self.original.create(*args, **kwargs)
        return _ObservedStream(response, self.state) if kwargs.get("stream") else response

    def __getattr__(self, name):
        return getattr(self.original, name)


class _ChatProxy:
    def __init__(self, original, state):
        self.original = original
        self.completions = _CompletionsProxy(original.completions, state)

    def __getattr__(self, name):
        return getattr(self.original, name)


class _ClientProxy:
    def __init__(self, original, state):
        self.original = original
        self.chat = _ChatProxy(original.chat, state)

    def __getattr__(self, name):
        return getattr(self.original, name)


def _enrich_event(event):
    state = _state.get()
    metadata = state.get("metadata") if state else None
    if not metadata or event.event_type() not in _EVENTS:
        return event
    cls = type(event)
    enriched = _event_classes.get(cls)
    if enriched is None:
        enriched = create_model(
            cls.__name__ + "WithExecutedRoute", __base__=cls,
            requested_model=(str | None, None), resolved_model=(str | None, None),
            backend=(str | None, None), billing_route=(str | None, None),
            routing_evidence=(str | None, None), usage_complete=(bool | None, None),
        )
        _event_classes[cls] = enriched
    return enriched.model_validate({**event.model_dump(), **metadata})


def install(*, executor=None, emitter_class=None, entry_modules=None) -> None:
    """Idempotently extend executor accounting and both TelemetryEmitter instances."""
    # Optional extension: when a Honcho upgrade moves one of these names, the API and the deriver
    # start without it (one warning) instead of stopping on an AttributeError.
    try:
        if executor is None:
            from src.llm import executor
        if getattr(executor.honcho_llm_call_inner, "__executed_route__", False):
            return
        if emitter_class is None:
            from src.telemetry.emitter import TelemetryEmitter
            emitter_class = TelemetryEmitter
        if entry_modules is None:
            from src.llm import api, tool_loop
            entry_modules = (api, tool_loop)
        original_inner = executor.honcho_llm_call_inner
        original_backend = executor.backend_for_provider
        original_account = executor._emit_call_telemetry
        original_emit = emitter_class.emit
    except (ImportError, AttributeError) as exc:
        logger.warning("Executed-route telemetry not installed: Honcho does not match (%s)", type(exc).__name__)
        return

    async def stream_with_state(iterator, state):
        try:
            while True:
                token = _state.set(state)
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    return
                finally:
                    _state.reset(token)
                yield chunk
        finally:
            token = _state.set(state)
            try:
                if hasattr(iterator, "aclose"):
                    await iterator.aclose()
            finally:
                _state.reset(token)

    @functools.wraps(original_inner)
    async def inner(*args, **kwargs):
        state = {}
        token = _state.set(state)
        try:
            result = await original_inner(*args, **kwargs)
        finally:
            _state.reset(token)
        return stream_with_state(result, state) if hasattr(result, "__aiter__") else result

    def backend(provider, client):
        value = original_backend(provider, client)
        state = _state.get()
        if provider == "openai" and state is not None and hasattr(value, "_client"):
            value._client = _ClientProxy(value._client, state)
        return value

    def account(**kwargs):
        state = _state.get()
        if state is None:
            return original_account(**kwargs)
        original_kwargs = dict(kwargs)
        # Honcho calls this from the finally of every LLM call: an error here would replace the
        # call's own result, so a shape this code does not expect leaves the call untouched.
        try:
            result = kwargs.get("result")
            raw = ({"model": state.get("stream_model"), "usage": _get(state.get("raw_chunk"), "usage")}
                   if kwargs.get("was_stream") else _get(result, "raw_response"))
            metadata = response_evidence(raw, kwargs.get("model", "unknown"), str(kwargs.get("provider")))
            if kwargs.get("was_stream"):
                usage = _get(raw, "usage")
                pin, pout = _count(_get(usage, "prompt_tokens")), _count(_get(usage, "completion_tokens"))
                metadata["usage_complete"] = pin is not None and pout is not None
                if metadata["usage_complete"] and result is not None:
                    result.input_tokens, result.output_tokens = pin, pout
                    details = _get(usage, "prompt_tokens_details")
                    result.cache_read_input_tokens = _count(_get(details, "cached_tokens")) or _count(_get(usage, "cache_read_input_tokens")) or 0
                    result.cache_creation_input_tokens = _count(_get(usage, "cache_creation_input_tokens")) or 0
            state["metadata"] = metadata
            if metadata.get("model"):
                kwargs["model"] = metadata["model"]
        except Exception as exc:
            logger.warning("Executed-route telemetry unavailable (%s)", type(exc).__name__)
            return original_account(**original_kwargs)
        return original_account(**kwargs)

    @functools.wraps(original_emit)
    def emit(self, event):
        try:
            event = _enrich_event(event)
        except Exception as exc:
            logger.warning("Executed-route telemetry unavailable (%s)", type(exc).__name__)
        return original_emit(self, event)

    inner.__executed_route__ = True
    executor.honcho_llm_call_inner = inner
    executor.backend_for_provider = backend
    executor._emit_call_telemetry = account
    for module in entry_modules:
        if getattr(module, "honcho_llm_call_inner", None) is original_inner:
            module.honcho_llm_call_inner = inner
    emitter_class.emit = emit
    logger.info("Executed-route telemetry installed")
