"""The settings file: hot reload, last good on a bad file, and the scheduler's proxy."""

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import honcho_rerank
import honcho_rerank.client as client
from honcho_rerank import settings as settings_module
from honcho_rerank.settings import RerankSettings, SettingsLoader


def write(path: Path, data: Any, bump: int = 0) -> None:
    path.write_text(
        data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
    )
    # Same-second writes: move the modification time so the change is seen.
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + bump * 1_000_000_000))


@pytest.fixture(autouse=True)
def no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RERANK_API_KEY", raising=False)


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    loaded = SettingsLoader(tmp_path / "settings.json").get()
    assert loaded == RerankSettings()
    assert not loaded.ENABLED


def test_block_is_read_and_hot_reloaded(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write(path, {"mode": "scheduler", "rerank": {"enabled": True, "candidates": 50}})
    loader = SettingsLoader(path)
    first = loader.get()
    assert (first.ENABLED, first.CANDIDATES) == (True, 50)
    assert loader.get() is first  # unchanged file: same object, no re-read
    write(
        path, {"rerank": {"enabled": True, "candidates": 80, "provider": "tei"}}, bump=1
    )
    second = loader.get()
    assert (second.CANDIDATES, second.PROVIDER) == (80, "tei")


def test_file_without_a_rerank_block_gives_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write(path, {"mode": "stock", "defaults": {"rerank": {"enabled": True}}})
    # Per-dream overrides under defaults/peers/pairs belong to the scheduler's proxy.
    assert not SettingsLoader(path).get().ENABLED


@pytest.mark.parametrize(
    "bad",
    [
        "{not json",
        pytest.param("[" * 100_000 + "0" + "]" * 100_000, id="deeply_nested"),
        "[]",
        {"rerank": []},
        {"rerank": {"candidates": 0}},
        {"rerank": {"provider": "unknown"}},
        {"rerank": {"typo_field": 1}},
    ],
)
def test_bad_file_keeps_the_last_good(
    tmp_path: Path, bad: Any, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "settings.json"
    write(path, {"rerank": {"enabled": True, "candidates": 50}})
    loader = SettingsLoader(path)
    good = loader.get()
    write(path, bad, bump=1)
    with caplog.at_level("WARNING", logger="honcho_rerank.settings"):
        assert loader.get() is good
    assert loader.last_error is not None
    assert any("last good" in record.getMessage() for record in caplog.records)
    # Fixing the file applies the new values.
    write(path, {"rerank": {"enabled": True, "candidates": 60}}, bump=2)
    assert loader.get().CANDIDATES == 60
    assert loader.last_error is None


def test_bad_file_at_start_gives_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write(path, "{not json")
    loader = SettingsLoader(path)
    assert loader.get() == RerankSettings()
    assert loader.last_error == "invalid JSON at line 1"


@pytest.mark.parametrize("failure", [RecursionError, RuntimeError])
def test_non_json_error_is_rejected_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: type[Exception],
) -> None:
    path = tmp_path / "settings.json"
    write(path, {"rerank": {"enabled": True, "candidates": 50}})
    loader = SettingsLoader(path)
    good = loader.get()
    write(path, {"rerank": {"enabled": False}}, bump=1)
    parse = Mock(side_effect=failure("test-value"))
    with monkeypatch.context() as patch:
        patch.setattr(settings_module.json, "loads", parse)
        with caplog.at_level("WARNING", logger="honcho_rerank.settings"):
            assert loader.get() is good
            assert loader.get() is good
            assert loader.get() is good
    parse.assert_called_once()
    assert loader.last_error == failure.__name__
    assert caplog.text.count("keeping the last good ones") == 1
    assert "test-value" not in caplog.text
    write(path, {"rerank": {"candidates": 60}}, bump=2)
    assert loader.get().CANDIDATES == 60
    assert loader.last_error is None


def test_rejected_values_are_not_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "settings.json"
    write(path, {"rerank": {"base_url": "https://user:test-password@reranker.example"}})
    with caplog.at_level("INFO"):
        SettingsLoader(path).get()
    assert "test-password" not in caplog.text
    assert "BASE_URL" in caplog.text


def test_vanished_file_keeps_the_last_good(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    write(path, {"rerank": {"enabled": True}})
    loader = SettingsLoader(path)
    assert loader.get().ENABLED
    path.unlink()
    assert loader.get().ENABLED


def test_key_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    write(path, {"rerank": {"enabled": True}})
    monkeypatch.setenv("RERANK_API_KEY", "test-key\n")
    loaded = SettingsLoader(path).get()
    assert loaded.API_KEY is not None
    assert loaded.API_KEY.get_secret_value() == "test-key"


def test_path_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "control.json"
    write(path, {"rerank": {"enabled": True, "candidates": 7}})
    monkeypatch.setenv("RERANK_SETTINGS", str(path))
    assert SettingsLoader().get().CANDIDATES == 7
    monkeypatch.delenv("RERANK_SETTINGS")
    assert SettingsLoader().path() == Path("/config/settings.json")


def test_module_attribute_reads_the_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    write(path, {"rerank": {"enabled": True, "candidates": 9}})
    monkeypatch.setattr(settings_module, "loader", SettingsLoader(path))
    assert honcho_rerank.settings.RERANK.CANDIDATES == 9
    assert client.settings.RERANK.CANDIDATES == 9
    # Other upper-case sections are Honcho's own settings.
    from src.config import settings as honcho_settings

    assert client.settings.CACHE is honcho_settings.CACHE
    with pytest.raises(AttributeError):
        _ = settings_module.not_a_setting  # pyright: ignore[reportAttributeAccessIssue]


class SchedulerStyleProxy:
    """What the scheduler's RerankSettingsProxy does: replace keys while a dream runs."""

    def __init__(self, base: Any, override: dict[str, Any]) -> None:
        self._base = base
        self._override = override

    def __getattr__(self, name: str) -> Any:
        if name == "RERANK":
            base = self._base.RERANK
            merged = {**base.model_dump(), **self._override}
            return type(base).model_validate(merged)
        return getattr(self._base, name)


def test_scheduler_proxy_on_the_package_attribute_reaches_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    write(path, {"rerank": {"enabled": True, "candidates": 100}})
    monkeypatch.setattr(settings_module, "loader", SettingsLoader(path))
    # The scheduler's contract check: RERANK is a pydantic model with these fields.
    fields = type(honcho_rerank.settings.RERANK).model_fields
    for name in (
        "ENABLED",
        "PROVIDER",
        "MODEL",
        "BASE_URL",
        "CANDIDATES",
        "MAX_DOCUMENTS",
        "TIMEOUT_SECONDS",
        "MIN_SCORE",
        "DAILY_REQUEST_CAP",
    ):
        assert name in fields
    # The scheduler's one-line change makes it swap honcho_rerank.settings.
    monkeypatch.setattr(
        honcho_rerank,
        "settings",
        SchedulerStyleProxy(honcho_rerank.settings, {"CANDIDATES": 20}),
    )
    assert client.settings.RERANK.CANDIDATES == 20
    assert client.candidate_count(5) == 20
    # The proxy reads its base live, so a hot reload still shows through it.
    write(path, {"rerank": {"enabled": False, "candidates": 100}}, bump=1)
    assert client.settings.RERANK.ENABLED is False
    assert client.settings.RERANK.CANDIDATES == 20
