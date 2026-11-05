"""With reranking off, Honcho's search behaves as stock.

Two cases: the settings file says ``"enabled": false``, and there is no settings file at all.
In both, a search from a caller the wrapper knows (dream, dialectic, prefetch, conclusions
route) reaches Honcho's own ``query_documents`` exactly once, with the caller's session and
``top_k`` unchanged, and gets its result back as is: no wider pool, no provider request, no
event, no log line.
"""

# pytest puts tests/ on sys.path (no __init__.py, so no clash with a Honcho tests package).
# pyright: reportImplicitRelativeImport=false

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fake_honcho import fake_query

import honcho_rerank.client as client
from honcho_rerank import hook
from honcho_rerank import settings as settings_module
from honcho_rerank.settings import SettingsLoader

ROWS = [object(), object(), object()]
CALLERS = ["dream", "dialectic", "prefetch", "conclusions_api"]


@pytest.fixture(autouse=True)
def no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RERANK_API_KEY", raising=False)


@pytest.fixture
def forbidden(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Fail on any reranking step, and record emitted events."""
    import src.telemetry.events as telemetry_events

    emitted: list[Any] = []
    monkeypatch.setattr(telemetry_events, "emit", emitted.append)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a reranking step ran while reranking is off")

    monkeypatch.setattr(client, "rerank_documents", refuse)
    monkeypatch.setattr(client, "record_rerank_fallback", refuse)
    monkeypatch.setattr(client, "_get_client", refuse)
    return emitted


def disabled_file(tmp_path: Path) -> Path:
    path = tmp_path / "settings.json"
    # Every other key set as a live file would have it: only enabled matters.
    block = {
        "enabled": False,
        "provider": "voyage",
        "model": "rerank-model",
        "candidates": 100,
        "callers": CALLERS,
        "daily_request_cap": 10,
    }
    path.write_text(json.dumps({"mode": "stock", "rerank": block}), encoding="utf-8")
    return path


@pytest.fixture(params=["disabled", "absent"])
def off(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    path = disabled_file(tmp_path) if request.param == "disabled" else tmp_path / "none.json"
    monkeypatch.setattr(settings_module, "loader", SettingsLoader(path))
    assert settings_module.RERANK.ENABLED is False
    return request.param


@pytest.mark.parametrize("caller", CALLERS)
async def test_search_is_the_stock_call_when_off(
    off: str, forbidden: list[Any], caller: str, caplog: pytest.LogCaptureFixture
) -> None:
    fetch = fake_query(ROWS)
    db = MagicMock()
    with caplog.at_level("INFO", logger="honcho_rerank.client"):
        result = await client.search(
            fetch,
            db,
            "workspace",
            "query",
            observer="a",
            observed="b",
            filters={"level": "explicit"},
            max_distance=0.5,
            top_k=3,
            embedding=[0.1, 0.2],
            caller=caller,
            run_id="run-1",
        )
    assert result == ROWS
    fetch.mock.assert_awaited_once_with(
        db,
        "workspace",
        "query",
        observer="a",
        observed="b",
        filters={"level": "explicit"},
        max_distance=0.5,
        top_k=3,
        embedding=[0.1, 0.2],
    )
    db.in_transaction.assert_not_called()
    assert forbidden == []
    assert caplog.records == []


async def test_installed_wrapper_passes_through_when_off(
    off: str, forbidden: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A caller the wrapper recognises, so the call goes through client.search.
    monkeypatch.setattr(hook, "detect_caller", lambda _frame: ("dream", "run-1"))
    fetch = fake_query(ROWS)
    wrapped = hook.make_wrapper(fetch)
    db = MagicMock()
    result = await wrapped(
        db, "workspace", "query", observer="a", observed="b", top_k=3
    )
    assert result == ROWS
    fetch.mock.assert_awaited_once_with(
        db,
        "workspace",
        "query",
        observer="a",
        observed="b",
        filters=None,
        max_distance=None,
        top_k=3,
        embedding=None,
    )
    assert forbidden == []


def test_should_rerank_is_false_for_every_caller_when_off(off: str) -> None:
    for caller in [*CALLERS, "dedupe", None]:
        assert client.should_rerank(caller) is False
