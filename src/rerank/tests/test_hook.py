"""The runtime hook against Honcho's real modules: install, callers, run ids, asserts."""

# pytest puts tests/ on sys.path (no __init__.py, so no clash with a Honcho tests package).
# pyright: reportImplicitRelativeImport=false

import asyncio
import inspect
from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fake_honcho import fake_query
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

import honcho_rerank.client as client
from honcho_rerank import context, hook
from honcho_rerank.settings import RerankSettings
from src import crud, schemas
from src.crud import document
from src.dialectic import core
from src.dreamer import orchestrator
from src.routers import conclusions
from src.utils import agent_tools

ORIGINAL_QUERY = document.query_documents


@pytest.fixture
def restore(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Undo every attribute install() replaces, and its once-only flag."""
    monkeypatch.setattr(crud, "query_documents", crud.query_documents)
    monkeypatch.setattr(document, "query_documents", document.query_documents)
    monkeypatch.setattr(orchestrator, "run_dream", orchestrator.run_dream)
    monkeypatch.setattr(core.DialecticAgent, "__init__", core.DialecticAgent.__init__)
    monkeypatch.setattr(hook, "_installed", False)
    yield


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import src.telemetry.events as telemetry_events

    emitted: list[Any] = []
    monkeypatch.setattr(telemetry_events, "emit", emitted.append)
    return emitted


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> RerankSettings:
    options = RerankSettings(
        ENABLED=True,
        API_KEY=SecretStr("test-key"),
        CALLERS=["dream", "dialectic", "prefetch", "conclusions_api"],
    )
    monkeypatch.setattr(
        client,
        "settings",
        SimpleNamespace(RERANK=options, CACHE=SimpleNamespace(ENABLED=False)),
    )
    monkeypatch.setattr(client, "_circuits", {})
    return options


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Install a wrapper over a fake original; record what reaches client.search."""
    calls: list[dict[str, Any]] = []

    async def search(fetch: Any, db: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append({"db": db, "args": args, **kwargs})
        return await fetch(
            db,
            *args,
            **{k: v for k, v in kwargs.items() if k not in ("caller", "run_id")},
        )

    monkeypatch.setattr(client, "search", search)
    monkeypatch.setattr(crud, "query_documents", hook.make_wrapper(fake_query()))
    return calls


def tool_context(category: str, run_id: str | None = "run-1") -> Any:
    return agent_tools.ToolContext(
        workspace_name="w",
        observer="a",
        observed="b",
        session_name=None,
        current_messages=None,
        include_observation_ids=False,
        db_lock=asyncio.Lock(),
        parent_category=category,
        agent_type="deduction" if category == "dream" else "dialectic",
        run_id=run_id,
    )


class Retrieved(Exception):
    pass


async def run_search_tool(monkeypatch: pytest.MonkeyPatch, ctx: Any) -> None:
    monkeypatch.setattr(
        agent_tools.embedding_client, "embed", AsyncMock(return_value=[1.0])
    )

    # Stop right after retrieval: these checks concern attribution only.
    def stop(_ctx: Any, _documents: Any) -> None:
        raise Retrieved

    monkeypatch.setattr(agent_tools, "_record_conclusion_evidence", stop)
    with pytest.raises(Retrieved):
        await agent_tools._handle_search_memory(ctx, {"query": "q"})


def prefetch_agent(monkeypatch: pytest.MonkeyPatch, run_id: str = "chat-1") -> Any:
    def unchanged(text: str) -> str:
        return text

    monkeypatch.setattr(
        core,
        "embedding_client",
        SimpleNamespace(
            embed=AsyncMock(return_value=[1.0]), truncate_to_token_limit=unchanged
        ),
    )
    agent = object.__new__(core.DialecticAgent)
    for name, value in {
        "workspace_name": "w",
        "observer": "a",
        "observed": "b",
        "session_id": None,
        "session_allowlist": None,
        "reasoning_level": "low",
        "evidence": None,
        "_run_id": run_id,
    }.items():
        setattr(agent, name, value)
    return agent


# --- install and the duplicate check ---------------------------------------------


def test_install_wraps_the_package_attribute_only(restore: None) -> None:
    hook.install()
    assert crud.query_documents is not ORIGINAL_QUERY
    assert getattr(crud.query_documents, "__rerank_original__", None) is ORIGINAL_QUERY
    # The module global that document.py's own code resolves stays the original.
    assert document.query_documents is ORIGINAL_QUERY
    assert inspect.iscoroutinefunction(crud.query_documents)
    assert getattr(orchestrator.run_dream, "__rerank_wrapped__", None) is not None
    assert getattr(core.DialecticAgent.__init__, "__rerank_wrapped__", None) is not None
    wrapped = crud.query_documents
    hook.install()
    hook._installed = False
    hook.install()  # idempotent even when the flag is lost
    assert crud.query_documents is wrapped


async def test_duplicate_check_is_not_wrapped(
    restore: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook.install()
    search = AsyncMock(side_effect=AssertionError("dedupe reached the wrapper"))
    monkeypatch.setattr(client, "search", search)
    nearest = MagicMock()
    vector = AsyncMock(return_value=[nearest])
    monkeypatch.setattr(document, "_uses_pgvector", lambda: True)
    monkeypatch.setattr(document, "_query_documents_pgvector", vector)
    monkeypatch.setattr(document, "_score_semantic_dup", lambda _doc, _existing: "dup")
    db = MagicMock(spec=AsyncSession)
    # The fields _semantic_dup_decision reads from a DocumentCreate.
    doc = SimpleNamespace(
        content="c", session_name="s", level="explicit", embedding=[1.0]
    )
    decision = await document._semantic_dup_decision(
        db,
        doc,  # pyright: ignore[reportArgumentType]
        "w",
        observer="a",
        observed="b",
    )
    assert decision == ("dup", nearest)
    search.assert_not_awaited()
    assert vector.await_args is not None
    assert vector.await_args.args[0] is db
    assert vector.await_args.args[-1] == document._SEMANTIC_DUP_TOP_K


# --- caller detection ---------------------------------------------------------------


@pytest.mark.parametrize("category", ["dream", "dialectic"])
async def test_search_tool_caller_and_run(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]], category: str
) -> None:
    await run_search_tool(monkeypatch, tool_context(category))
    assert len(captured) == 1
    assert (captured[0]["caller"], captured[0]["run_id"]) == (category, "run-1")
    assert captured[0]["db"] is None


async def test_search_tool_other_category_passes_through(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:
    await run_search_tool(monkeypatch, tool_context("api"))
    assert captured == []


async def test_dialectic_prefetch_is_named_prefetch(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:
    agent = prefetch_agent(monkeypatch)
    token = context.rerank_run.set("chat-1")
    try:
        await agent._prefetch_relevant_observations("q")
    finally:
        context.rerank_run.reset(token)
    assert [call["caller"] for call in captured] == ["prefetch", "prefetch"]
    assert [call["run_id"] for call in captured] == ["chat-1", "chat-1"]


async def test_search_memory_from_elsewhere_passes_through(
    captured: list[dict[str, Any]],
) -> None:
    # Representation assembly and any other user of search_memory stay stock.
    await agent_tools.search_memory("w", "a", "b", "q", 5, embedding=[1.0])
    assert captured == []


async def test_conclusions_route_is_named_and_keeps_its_session(
    monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]
) -> None:
    session = MagicMock(spec=AsyncSession)
    body = schemas.ConclusionQuery(
        query="q", filters={"observer": "a", "observed": "b"}, top_k=3
    )
    assert await conclusions.query_conclusions("w", body, session) == []
    assert len(captured) == 1
    assert (captured[0]["caller"], captured[0]["run_id"]) == ("conclusions_api", None)
    # The wrapper gets the route's session; client.search decides whether to use it.
    assert captured[0]["db"] is session


async def test_unknown_caller_gets_the_original_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search = AsyncMock()
    monkeypatch.setattr(client, "search", search)
    fake = fake_query(["row"])
    wrapper = hook.make_wrapper(fake)
    assert await wrapper(None, "w", "q", observer="a", observed="b") == ["row"]
    search.assert_not_awaited()
    fake.mock.assert_awaited_once_with(None, "w", "q", observer="a", observed="b")


def test_detect_caller_without_a_frame() -> None:
    assert hook.detect_caller(None) == (None, None)


# --- run ids reach the event ----------------------------------------------------------


async def test_dream_run_reaches_the_event(
    monkeypatch: pytest.MonkeyPatch, enabled: RerankSettings, events: list[Any]
) -> None:
    monkeypatch.setattr(crud, "query_documents", hook.make_wrapper(fake_query()))
    seen: dict[str, Any] = {}

    async def fake_run_dream(**_kwargs: Any) -> Any:
        seen["token"] = context.current_run_id()
        # A tool context without a run id falls back to the dream's token ...
        await run_search_tool(monkeypatch, tool_context("dream", run_id=None))
        # ... and one with a run id (the normal case) carries it.
        await run_search_tool(monkeypatch, tool_context("dream", run_id="dream-7"))
        return SimpleNamespace(run_id="dream-7")

    wrapped = context.wrap_run_dream(fake_run_dream)
    result = await wrapped(workspace_name="w", observer="a", observed="b")
    assert result.run_id == "dream-7"
    token = seen["token"]
    assert isinstance(token, str) and token.startswith("rr-")
    assert [(e.caller, e.run_id, e.outcome) for e in events] == [
        ("dream", token, "empty_result"),
        ("dream", "dream-7", "empty_result"),
    ]
    assert context.dream_run_id(token) == "dream-7"
    assert context.current_run_id() is None


async def test_chat_run_reaches_the_event(
    monkeypatch: pytest.MonkeyPatch, enabled: RerankSettings, events: list[Any]
) -> None:
    monkeypatch.setattr(crud, "query_documents", hook.make_wrapper(fake_query()))

    class FakeAgent:
        def __init__(self) -> None:
            self._run_id = "chat-9"

    context.wrap_dialectic_init(FakeAgent)

    async def chat() -> None:
        FakeAgent()  # the API builds the agent, then prefetches in the same task
        await prefetch_agent(monkeypatch, "chat-9")._prefetch_relevant_observations("q")

    await asyncio.create_task(chat())
    assert [(e.caller, e.run_id) for e in events] == [
        ("prefetch", "chat-9"),
        ("prefetch", "chat-9"),
    ]
    assert context.current_run_id() is None  # the task's context, not ours


def test_wrappers_are_idempotent() -> None:
    async def run(**_kwargs: Any) -> Any:
        return None

    once = context.wrap_run_dream(run)
    assert context.wrap_run_dream(once) is once

    class Agent:
        def __init__(self) -> None:
            self._run_id = "x"

    context.wrap_dialectic_init(Agent)
    first = Agent.__init__
    context.wrap_dialectic_init(Agent)
    assert Agent.__init__ is first


async def test_run_dream_wrapper_keeps_the_signature() -> None:
    # The scheduler's hook inspects run_dream's signature after this wraps it.
    wrapped = context.wrap_run_dream(orchestrator.run_dream)
    assert inspect.iscoroutinefunction(wrapped)
    assert inspect.signature(wrapped) == inspect.signature(orchestrator.run_dream)


# --- start-up asserts ------------------------------------------------------------------


def test_contract_holds_for_this_honcho() -> None:
    hook.check_contract()


async def _renamed(  # parameter "text" instead of "query"
    db: Any,
    workspace_name: str,
    text: str,
    *,
    observer: str,
    observed: str,
    filters: Any = None,
    max_distance: Any = None,
    top_k: int = 5,
    embedding: Any = None,
) -> list[Any]:
    return []


@pytest.mark.parametrize(
    "breakage",
    [
        "query_parameters",
        "run_dream_missing",
        "dream_result",
        "dialectic_run_id",
        "tool_context",
        "exported_differs",
        "emit_missing",
        "tracked_db_missing",
    ],
)
def test_install_refuses_a_changed_honcho(
    restore: None, monkeypatch: pytest.MonkeyPatch, breakage: str
) -> None:
    if breakage == "query_parameters":
        monkeypatch.setattr(document, "query_documents", _renamed)
        monkeypatch.setattr(crud, "query_documents", _renamed)
    elif breakage == "run_dream_missing":
        monkeypatch.delattr(orchestrator, "run_dream")
    elif breakage == "dream_result":
        monkeypatch.setattr(orchestrator, "DreamResult", SimpleNamespace)
    elif breakage == "dialectic_run_id":

        def __init__(self: Any) -> None:
            self.other = 1

        monkeypatch.setattr(core.DialecticAgent, "__init__", __init__)
    elif breakage == "tool_context":
        monkeypatch.setattr(agent_tools, "ToolContext", SimpleNamespace)
    elif breakage == "exported_differs":
        monkeypatch.setattr(crud, "query_documents", AsyncMock())
    elif breakage == "tracked_db_missing":
        import src.dependencies as dependencies

        monkeypatch.delattr(dependencies, "tracked_db")
    else:
        import src.telemetry.events as telemetry_events

        monkeypatch.delattr(telemetry_events, "emit")
    before = crud.query_documents
    with pytest.raises(RuntimeError, match="rerank hook"):
        hook.install()
    # Nothing was half-installed.
    assert crud.query_documents is before
    assert not hook._installed


# --- entrypoint wrappers -----------------------------------------------------------------


async def test_ef_search_warning_once_per_pool(
    monkeypatch: pytest.MonkeyPatch,
    enabled: RerankSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar=lambda: "40"))

    @asynccontextmanager
    async def tracked(*_args: Any, **_kwargs: Any):
        yield session

    import src.dependencies

    monkeypatch.setattr(src.dependencies, "tracked_db", tracked)
    monkeypatch.setattr(client, "_ef_search", None)
    monkeypatch.setattr(client, "_ef_warned", set())
    enabled.CANDIDATES = 100
    with caplog.at_level("WARNING", logger="honcho_rerank.client"):
        await hook.check_ef_search()
        client.warn_pool(enabled)
    warnings = [r for r in caplog.records if "ef_search=40" in r.getMessage()]
    assert len(warnings) == 1
    enabled.CANDIDATES = 40
    client.warn_pool(enabled)
    assert len([r for r in caplog.records if "ef_search" in r.getMessage()]) == 1


async def test_ef_search_read_failure_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def tracked(*_args: Any, **_kwargs: Any):
        raise RuntimeError("no database")
        yield  # pragma: no cover

    import src.dependencies

    monkeypatch.setattr(src.dependencies, "tracked_db", tracked)
    monkeypatch.setattr(client, "_ef_search", None)
    await hook.check_ef_search()
    assert client._ef_search is None


async def test_deriver_main_wrapper_closes_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        hook, "check_ef_search", AsyncMock(side_effect=lambda: order.append("check"))
    )
    monkeypatch.setattr(
        client,
        "close_rerank_client",
        AsyncMock(side_effect=lambda: order.append("close")),
    )

    async def main() -> None:
        order.append("main")
        raise RuntimeError("deriver stopped")

    queue_manager = SimpleNamespace(main=main)
    hook.wrap_deriver_main(queue_manager)  # pyright: ignore[reportArgumentType]
    hook.wrap_deriver_main(queue_manager)  # pyright: ignore[reportArgumentType]
    with pytest.raises(RuntimeError):
        await queue_manager.main()
    assert order == ["check", "main", "close"]


async def test_lifespan_wrapper_closes_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(
        hook, "check_ef_search", AsyncMock(side_effect=lambda: order.append("check"))
    )
    monkeypatch.setattr(
        client,
        "close_rerank_client",
        AsyncMock(side_effect=lambda: order.append("close")),
    )

    @asynccontextmanager
    async def lifespan(_app: Any):
        order.append("start")
        yield
        order.append("stop")

    app = SimpleNamespace(router=SimpleNamespace(lifespan_context=lifespan))
    hook.wrap_lifespan(app)
    hook.wrap_lifespan(app)
    async with app.router.lifespan_context(app):
        order.append("serve")
    assert order == ["start", "check", "serve", "close", "stop"]


def test_api_factory_installs_and_wraps(
    restore: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from honcho_rerank import api_main, routing_telemetry
    installed = []
    monkeypatch.setattr(routing_telemetry, "install", lambda: installed.append(True))
    from src.main import app

    monkeypatch.setattr(app.router, "lifespan_context", app.router.lifespan_context)
    assert api_main.create_app() is app
    assert installed == [True]
    assert hook._installed
    assert getattr(app.router.lifespan_context, "__rerank_wrapped__", None) is not None


def test_deriver_entrypoint_chains_the_next_script(
    restore: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import src.deriver.queue_manager as queue_manager
    from honcho_rerank import deriver_main, routing_telemetry
    installed = []
    monkeypatch.setattr(routing_telemetry, "install", lambda: installed.append(True))

    monkeypatch.setattr(queue_manager, "main", queue_manager.main)
    marker = tmp_path / "ran.txt"
    script = tmp_path / "next_entry.py"
    script.write_text(
        "import sys, pathlib\n" + "pathlib.Path(sys.argv[1]).write_text(__name__)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["deriver_main"])
    deriver_main.main([str(script), str(marker)])
    assert installed == [True]
    assert marker.read_text(encoding="utf-8") == "__main__"
    assert hook._installed
    assert getattr(queue_manager.main, "__rerank_wrapped__", None) is not None
