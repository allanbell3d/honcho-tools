"""Install reranking into a running Honcho process without editing its source.

``install()`` checks the Honcho contract it relies on, then:

- replaces ``src.crud.query_documents`` (the package attribute every caller uses as
  ``crud.query_documents``) with a wrapper. ``src.crud.document.query_documents``, the
  module global, is left as it is, so duplicate detection inside ``document.py``, which
  calls the function by that local name, never reaches the wrapper;
- wraps ``src.dreamer.orchestrator.run_dream`` and ``DialecticAgent.__init__`` so
  searches know their run (see ``honcho_rerank.context``).

The wrapper decides the caller from the module that called it, one frame up. Unknown
callers get the original function, untouched.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import importlib.util
import inspect
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import FrameType, ModuleType
from typing import Any, cast

from . import client, context
from .events import RerankCaller

logger = logging.getLogger(__name__)

QUERY_PARAMETERS = (
    "db",
    "workspace_name",
    "query",
    "observer",
    "observed",
    "filters",
    "max_distance",
    "top_k",
    "embedding",
)
AGENT_TOOLS = "src.utils.agent_tools"
DIALECTIC_CORE = "src.dialectic.core"
CONCLUSIONS_ROUTER = "src.routers.conclusions"
TOOL_HANDLER = "_handle_search_memory"
SEARCH_MEMORY = "search_memory"

_installed = False


def detect_caller(frame: FrameType | None) -> tuple[RerankCaller | None, str | None]:
    """Name the search from the frame that called ``crud.query_documents``.

    Returns the caller and its run id: the tool context's ``run_id`` for the agent
    tools, else the ``rerank_run`` context variable; None for the conclusions route.
    """
    if frame is None:
        return None, None
    module = frame.f_globals.get("__name__")
    if module == AGENT_TOOLS:
        function = frame.f_code.co_name
        if function == TOOL_HANDLER:
            ctx = frame.f_locals.get("ctx")
            category = getattr(ctx, "parent_category", None)
            if category == "dream" or category == "dialectic":
                run_id = getattr(ctx, "run_id", None)
                if not isinstance(run_id, str) or not run_id:
                    run_id = context.current_run_id()
                return category, run_id
            return None, None
        if function == SEARCH_MEMORY:
            parent = frame.f_back
            if (
                parent is not None
                and parent.f_globals.get("__name__") == DIALECTIC_CORE
            ):
                return "prefetch", context.current_run_id()
        return None, None
    if module == CONCLUSIONS_ROUTER:
        return "conclusions_api", None
    return None, None


def make_wrapper(
    original: Callable[..., Awaitable[Sequence[Any]]],
) -> Callable[..., Awaitable[Sequence[Any]]]:
    """Wrap ``query_documents``; unknown callers get the original call itself."""
    signature = inspect.signature(original)

    @functools.wraps(original)
    def query_documents(*args: Any, **kwargs: Any) -> Awaitable[Sequence[Any]]:
        # A plain function, so the caller's frame is the one calling this.
        frame = inspect.currentframe()
        try:
            caller, run_id = detect_caller(frame.f_back if frame else None)
        except Exception:
            caller, run_id = None, None
        finally:
            del frame
        if caller is None:
            return original(*args, **kwargs)
        return _search(original, signature, caller, run_id, args, kwargs)

    inspect.markcoroutinefunction(query_documents)
    setattr(query_documents, "__rerank_original__", original)  # noqa: B010
    return query_documents


async def _search(
    original: Callable[..., Awaitable[Sequence[Any]]],
    signature: inspect.Signature,
    caller: RerankCaller,
    run_id: str | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Sequence[Any]:
    try:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = {name: bound.arguments[name] for name in QUERY_PARAMETERS}
    except (TypeError, KeyError):
        # Not a call this wrapper understands: let the original raise or answer.
        return await original(*args, **kwargs)
    return await client.search(
        original,
        values["db"],
        values["workspace_name"],
        values["query"],
        observer=values["observer"],
        observed=values["observed"],
        filters=values["filters"],
        max_distance=values["max_distance"],
        top_k=values["top_k"],
        embedding=values["embedding"],
        caller=caller,
        run_id=run_id,
    )


def _fail(message: str) -> RuntimeError:
    logger.error("rerank hook not installed: %s", message)
    return RuntimeError(f"rerank hook: {message}")


def check_contract() -> tuple[ModuleType, ModuleType, type, ModuleType]:
    """Verify every Honcho name the hook relies on; RuntimeError when one is off."""
    try:
        import src.crud as crud
        import src.crud.document as document
        import src.utils.agent_tools as agent_tools
        from src.dialectic import core
        from src.dreamer import orchestrator
    except Exception as exc:
        raise _fail(f"Honcho import failed ({type(exc).__name__})") from exc

    original = getattr(document, "query_documents", None)
    if not inspect.iscoroutinefunction(original):
        raise _fail("src.crud.document.query_documents is missing or not async")
    original = cast(Callable[..., Any], original)
    names = tuple(inspect.signature(original).parameters)
    if names != QUERY_PARAMETERS:
        raise _fail(
            "query_documents parameters changed: "
            + f"expected {', '.join(QUERY_PARAMETERS)}; found {', '.join(names)}"
        )
    exported = getattr(crud, "query_documents", None)
    if (
        exported is not original
        and getattr(exported, "__rerank_original__", None) is not original
    ):
        raise _fail("src.crud.query_documents is not src.crud.document.query_documents")

    handler = getattr(agent_tools, TOOL_HANDLER, None)
    if handler is None or "ctx" not in handler.__code__.co_varnames:
        raise _fail(f"agent_tools.{TOOL_HANDLER}(ctx, ...) not found")
    if not callable(getattr(agent_tools, SEARCH_MEMORY, None)):
        raise _fail(f"agent_tools.{SEARCH_MEMORY} not found")
    tool_context = getattr(agent_tools, "ToolContext", None)
    fields = (
        {field.name for field in dataclasses.fields(tool_context)}
        if tool_context is not None and dataclasses.is_dataclass(tool_context)
        else set[str]()
    )
    if not {"parent_category", "run_id"} <= fields:
        raise _fail("agent_tools.ToolContext.parent_category / run_id not found")
    if importlib.util.find_spec(CONCLUSIONS_ROUTER) is None:
        raise _fail(f"{CONCLUSIONS_ROUTER} not found")

    agent = getattr(core, "DialecticAgent", None)
    init = getattr(
        getattr(agent, "__init__", None), "__rerank_wrapped__", None
    ) or getattr(agent, "__init__", None)
    code = getattr(init, "__code__", None)
    if agent is None or code is None or "_run_id" not in code.co_names:
        raise _fail("DialecticAgent.__init__ does not set _run_id")

    run_dream = getattr(orchestrator, "run_dream", None)
    if not inspect.iscoroutinefunction(run_dream):
        raise _fail("src.dreamer.orchestrator.run_dream is missing or not async")
    result = getattr(orchestrator, "DreamResult", None)
    if (
        result is None
        or not dataclasses.is_dataclass(result)
        or "run_id" not in {field.name for field in dataclasses.fields(result)}
    ):
        raise _fail("run_dream's DreamResult has no run_id")

    try:
        from src.telemetry.events import emit
    except Exception as exc:
        raise _fail("src.telemetry.events.emit is not importable") from exc
    if not callable(emit):
        raise _fail("src.telemetry.events.emit is not callable")
    try:
        from src.dependencies import tracked_db
    except Exception as exc:
        raise _fail("src.dependencies.tracked_db is not importable") from exc
    if not callable(tracked_db):
        raise _fail("src.dependencies.tracked_db is not callable")
    return crud, document, cast(type, agent), orchestrator


def install() -> None:
    """Install the wrapper and the run-id hooks; raise if Honcho does not match."""
    global _installed
    if _installed:
        return
    crud, document, agent, orchestrator = check_contract()
    original = document.query_documents
    if getattr(crud.query_documents, "__rerank_original__", None) is None:
        setattr(crud, "query_documents", make_wrapper(original))  # noqa: B010
    setattr(  # noqa: B010
        orchestrator, "run_dream", context.wrap_run_dream(orchestrator.run_dream)
    )
    context.wrap_dialectic_init(agent)
    _installed = True
    logger.info("rerank hook installed")


async def check_ef_search() -> None:
    """Read hnsw.ef_search once and warn if it is below CANDIDATES."""
    try:
        from sqlalchemy import text

        from src.dependencies import tracked_db

        async with tracked_db("rerank.ef_search", read_only=True) as db:
            value = (await db.execute(text("SHOW hnsw.ef_search"))).scalar()
        ef_search = int(cast(Any, value))
    except Exception as exc:
        logger.info("rerank: hnsw.ef_search not read (%s)", type(exc).__name__)
        return
    client.note_ef_search(ef_search)
    with contextlib.suppress(Exception):
        client.warn_pool(client.settings.RERANK)


def wrap_lifespan(app: Any) -> None:
    """Add the start-up check and the client close to a FastAPI app's lifespan."""
    original = cast(
        Callable[[Any], AbstractAsyncContextManager[Any]],
        app.router.lifespan_context,
    )
    if getattr(original, "__rerank_wrapped__", None) is not None:
        return

    @asynccontextmanager
    async def lifespan(application: Any) -> AsyncGenerator[Any]:
        async with original(application) as state:
            await check_ef_search()
            try:
                yield state
            finally:
                await client.close_rerank_client()

    setattr(lifespan, "__rerank_wrapped__", original)  # noqa: B010
    app.router.lifespan_context = lifespan


def wrap_deriver_main(queue_manager: ModuleType) -> None:
    """Add the start-up check and the client close around the deriver's main()."""
    original = cast(Callable[..., Awaitable[Any]], queue_manager.main)
    if getattr(original, "__rerank_wrapped__", None) is not None:
        return

    @functools.wraps(original)
    async def main(*args: Any, **kwargs: Any) -> Any:
        await check_ef_search()
        try:
            return await original(*args, **kwargs)
        finally:
            await client.close_rerank_client()

    setattr(main, "__rerank_wrapped__", original)  # noqa: B010
    setattr(queue_manager, "main", main)  # noqa: B010
