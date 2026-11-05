"""Executed-route accounting through fake SDKs and both event streams."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
from typing import ClassVar

import pytest
from pydantic import BaseModel

PATH = Path(__file__).resolve().parents[1] / "src/rerank/honcho_rerank/routing_telemetry.py"
spec = importlib.util.spec_from_file_location("routing_under_test", PATH)
routing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routing)


class Event(BaseModel):
    kind: ClassVar[str] = "llm.call.completed"
    model: str
    provider_input_tokens: int = 0
    provider_output_tokens: int = 0
    cache_read_tokens: int = 0

    @classmethod
    def event_type(cls):
        return cls.kind


class Trace(Event):
    kind: ClassVar[str] = "llm.call.traced"


def harness():
    recorded = []

    class Emitter:
        def emit(self, event):
            recorded.append((event.event_type(), event.model_dump()))

    emitter = Emitter()
    executor = NS()

    def account(**kwargs):
        result = kwargs.get("result")
        for cls in (Event, Trace):
            emitter.emit(cls(model=kwargs["model"], provider_input_tokens=result.input_tokens,
                             provider_output_tokens=result.output_tokens,
                             cache_read_tokens=result.cache_read_input_tokens))

    class Backend:
        def __init__(self, client):
            self._client = client

        async def complete(self):
            raw = await self._client.chat.completions.create(stream=False)
            return NS(raw_response=raw, input_tokens=100, output_tokens=10, cache_read_input_tokens=20)

        async def stream(self):
            raw = await self._client.chat.completions.create(stream=True)
            async for chunk in raw:
                await asyncio.sleep(0)
                yield NS(content="x", output_tokens=getattr(getattr(chunk, "usage", None), "completion_tokens", None))

    async def inner(*, provider, model, client, stream=False):
        backend = executor.backend_for_provider(provider, client)
        if stream:
            async def chunks():
                result = NS(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
                try:
                    async for chunk in backend.stream():
                        if chunk.output_tokens is not None:
                            result.output_tokens = chunk.output_tokens
                        yield chunk
                finally:
                    executor._emit_call_telemetry(provider=provider, model=model, result=result, was_stream=True)
            return chunks()
        result = await backend.complete()
        executor._emit_call_telemetry(provider=provider, model=model, result=result, was_stream=False)
        return "answer"

    executor.honcho_llm_call_inner = inner
    executor.backend_for_provider = lambda provider, client: Backend(client)
    executor._emit_call_telemetry = account
    entry = NS(honcho_llm_call_inner=inner)
    routing.install(executor=executor, emitter_class=Emitter, entry_modules=[entry])
    assert entry.honcho_llm_call_inner is executor.honcho_llm_call_inner
    installed = entry.honcho_llm_call_inner
    routing.install(executor=executor, emitter_class=Emitter, entry_modules=[entry])
    assert entry.honcho_llm_call_inner is installed
    return executor, recorded


def client(model="deepseek-v4.1-flash", backend="deepseek", route="api", usage=True):
    raw = NS(model=model)
    if usage:
        raw.usage = NS(prompt_tokens=100, completion_tokens=10, prompt_tokens_details=NS(cached_tokens=20),
                       billing_route=route, backend=backend, resolved_model=model)

    async def create(**kwargs):
        if not kwargs.get("stream"):
            return raw
        async def chunks():
            yield NS(model=model, usage=None)
            await asyncio.sleep(0)
            if usage:
                yield raw
        return chunks()
    return NS(chat=NS(completions=NS(create=create)))


def test_completed_and_trace_use_real_response_not_stage_alias():
    executor, events = harness()
    original_client = client()
    asyncio.run(executor.honcho_llm_call_inner(provider="openai", model="stage-route", client=original_client))
    assert len(events) == 2
    for kind, event in events:
        assert event["model"] == "deepseek-v4.1-flash"
        assert event["requested_model"] == "stage-route"
        assert event["backend"] == "deepseek" and event["billing_route"] == "api"
        assert event["routing_evidence"] == "bridge_response"
    assert routing._state.get() is None


def test_concurrent_streams_preserve_usage_and_isolate_routes():
    executor, events = harness()

    async def consume(model, backend, route):
        stream = await executor.honcho_llm_call_inner(provider="openai", model="stage-route", client=client(model, backend, route), stream=True)
        async for _ in stream:
            await asyncio.sleep(0)

    async def run():
        await asyncio.gather(consume("flash", "deepseek", "api"), consume("luna", "codex", "cli"))
    asyncio.run(run())
    assert len(events) == 4
    for _, event in events:
        assert (event["backend"], event["billing_route"]) == (("deepseek", "api") if event["model"] == "flash" else ("codex", "cli"))
        assert event["usage_complete"] is True
        assert event["provider_input_tokens"] == 100 and event["provider_output_tokens"] == 10
        assert event["cache_read_tokens"] == 20


def test_missing_stream_usage_is_unknown_instead_of_guessed_from_settings():
    executor, events = harness()
    async def run():
        stream = await executor.honcho_llm_call_inner(provider="openai", model="stage-route", client=client(usage=False), stream=True)
        async for _ in stream:
            pass
    asyncio.run(run())
    for _, event in events:
        assert event["model"] == "deepseek-v4.1-flash"
        assert event["billing_route"] is None
        assert event["usage_complete"] is False


def test_direct_and_unresolved_response_evidence():
    assert routing.response_evidence({}, "claude-haiku", "anthropic")["billing_route"] == "api"
    assert routing.response_evidence({}, "gemini", "gemini")["billing_route"] == "api"
    assert "billing_route" not in routing.response_evidence({}, "stage-route", "openai")
    assert routing.response_evidence({"model": "real"}, "stage-route", "openai")["model"] == "real"
    assert "model" not in routing.response_evidence({"model": "invalid model with spaces"}, "stage-route", "openai")


def test_early_stream_close_from_another_task_keeps_context_local():
    executor, events = harness()
    async def run():
        stream = await executor.honcho_llm_call_inner(provider="openai", model="stage-route", client=client(), stream=True)
        await anext(stream)
        assert routing._state.get() is None
        await asyncio.create_task(stream.aclose())
        assert routing._state.get() is None
    asyncio.run(run())
    assert len(events) == 2
    assert all(event["usage_complete"] is False for _, event in events)


def test_an_enrichment_error_leaves_the_call_and_its_telemetry_alone(monkeypatch):
    # Honcho calls the accounting from the finally of every LLM call: an error there replaced the answer.
    executor, events = harness()

    def broken(*args, **kwargs):
        raise TypeError("unexpected response shape")

    monkeypatch.setattr(routing, "response_evidence", broken)
    answer = asyncio.run(executor.honcho_llm_call_inner(provider="openai", model="stage-route", client=client()))
    assert answer == "answer"
    assert [event["model"] for _kind, event in events] == ["stage-route", "stage-route"]


def test_install_skips_when_honcho_does_not_match():
    # A Honcho upgrade that moves a wrapped name must not stop the API and the deriver from starting.
    class Emitter:
        def emit(self, event):
            return None

    executor = NS(honcho_llm_call_inner=lambda **kwargs: None, backend_for_provider=lambda provider, client: None)
    routing.install(executor=executor, emitter_class=Emitter, entry_modules=[])
    assert not hasattr(executor, "_emit_call_telemetry")
    assert not getattr(executor.honcho_llm_call_inner, "__executed_route__", False)
