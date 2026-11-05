"""Rerank settings: the ``rerank`` block of a JSON settings file, reloaded on change.

The file is the scheduler's ``settings.json`` by default (path from ``RERANK_SETTINGS``).
It is re-read when its modification time or size changes. A file that cannot be read,
parsed or validated leaves the last good settings in force and logs the reason (field
names only, never values). The API key comes from ``RERANK_API_KEY`` in the environment
and never from the file.

This module also stands in for the old ``src.rerank.settings`` object: ``RERANK`` is the
current ``RerankSettings`` and any other upper-case name (``CACHE``) is read from
Honcho's settings. The package attribute ``honcho_rerank.settings`` is what the scheduler's
per-dream proxy replaces; ``honcho_rerank.client`` reads that attribute on every call.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, cast
from urllib.parse import urlparse

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

logger = logging.getLogger(__name__)

SETTINGS_ENV = "RERANK_SETTINGS"
DEFAULT_SETTINGS_PATH = "/config/settings.json"
API_KEY_ENV = "RERANK_API_KEY"
BLOCK = "rerank"


class RerankSettings(BaseModel):
    """Optional provider-neutral reranking with UTC-day request limits."""

    # Validation errors would otherwise quote the rejected API key verbatim.
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", hide_input_in_errors=True
    )

    ENABLED: bool = False
    PROVIDER: Literal["voyage", "cohere", "jina", "tei"] = "voyage"
    MODEL: str = "rerank-3-lite"
    BASE_URL: str | None = None
    API_KEY: SecretStr | None = None
    CANDIDATES: Annotated[int, Field(ge=1, le=1000)] = 100
    MAX_DOCUMENTS: Annotated[int, Field(ge=1, le=10000)] | None = None
    MAX_QUERY_CHARS: Annotated[int, Field(ge=1)] = 2000
    TIMEOUT_SECONDS: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 5.0
    # Duplicate detection and representation assembly are never reranked.
    CALLERS: list[Literal["dream", "dialectic", "prefetch", "conclusions_api"]] = [
        "dream",
        "dialectic",
        "prefetch",
    ]
    MIN_SCORE: Annotated[float, Field(allow_inf_nan=False)] | None = None
    DAILY_REQUEST_CAP: Annotated[int, Field(ge=0)] | None = None
    FAILURES_BEFORE_COOLDOWN: Annotated[int, Field(ge=1)] = 3
    COOLDOWN_SECONDS: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 60.0

    @field_validator("MODEL")
    @classmethod
    def _validate_model(cls, value: str) -> str:
        # The model is written into log lines; a newline would forge a record.
        if not value.strip() or not value.isprintable():
            raise ValueError("RERANK MODEL must be printable text")
        return value

    @field_validator("BASE_URL")
    @classmethod
    def _validate_base_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("RERANK BASE_URL must be an http(s) URL")
        # The HTTP client logs request URLs, so credentials belong in API_KEY,
        # and a query or fragment would end up after the appended path.
        if "@" in parsed.netloc or "?" in v or "#" in v:
            raise ValueError(
                "RERANK BASE_URL must be a plain API root without credentials, "
                + "query or fragment"
            )
        try:
            httpx.URL(v)  # the client's own parser: ports, control characters
        except httpx.InvalidURL as exc:
            raise ValueError("RERANK BASE_URL is not a valid URL") from exc
        return v

    @field_validator("API_KEY")
    @classmethod
    def _validate_api_key(cls, v: SecretStr | None) -> SecretStr | None:
        # Secrets written with `echo` often end in a newline, which passes a
        # presence check but makes every request fail at the HTTP layer.
        if v is None:
            return None
        key = v.get_secret_value().strip()
        if not key:
            return None
        # A header value must be printable ASCII; the message never echoes it.
        if not key.isascii() or any(ord(c) < 0x20 or ord(c) == 0x7F for c in key):
            raise ValueError(
                "RERANK API_KEY must not contain non-ASCII or control characters"
            )
        return SecretStr(key)


def build_settings(block: dict[str, Any] | None) -> RerankSettings:
    """Validate one ``rerank`` block; keys are matched case-insensitively.

    A null value means "use the default", as in the scheduler's blocks. A key field in
    the file is ignored with a warning: the key only comes from the environment.
    """
    values: dict[str, Any] = {}
    for name, value in (block or {}).items():
        if "key" in name.lower():
            logger.warning(
                "rerank settings: a key field in the settings file is ignored; "
                + "set %s in the environment",
                API_KEY_ENV,
            )
            continue
        if value is not None:
            values[name.upper()] = value
    key = os.environ.get(API_KEY_ENV)
    if key is not None:
        values["API_KEY"] = key
    return RerankSettings.model_validate(values)


def describe_error(exc: BaseException) -> str:
    """A reason safe to log: exception class and field names, never values."""
    if isinstance(exc, ValidationError):
        fields = sorted(
            {".".join(str(part) for part in error["loc"]) for error in exc.errors()}
        )
        return f"invalid {', '.join(fields) or 'settings'}"
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON at line {exc.lineno}"
    return type(exc).__name__


class SettingsLoader:
    """Hot-reloading reader for the ``rerank`` block, keeping the last good result."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._fixed: Path | None = Path(path) if path is not None else None
        self._lock: threading.Lock = threading.Lock()
        self._stamp: tuple[str, int, int] | None = None
        self._current: RerankSettings | None = None
        self.last_error: str | None = None

    def path(self) -> Path:
        if self._fixed is not None:
            return self._fixed
        return Path(os.environ.get(SETTINGS_ENV) or DEFAULT_SETTINGS_PATH)

    def get(self) -> RerankSettings:
        path = self.path()
        try:
            stat = path.stat()
            stamp = (str(path), stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stamp = (str(path), -1, -1)
        except OSError:
            stamp = None
        current = self._current
        if current is not None and (stamp is None or stamp == self._stamp):
            return current
        with self._lock:
            if self._current is None or (stamp is not None and stamp != self._stamp):
                self._reload(path, stamp)
            return cast(RerankSettings, self._current)

    def _reload(self, path: Path, stamp: tuple[str, int, int] | None) -> None:
        missing = stamp is None or stamp[1] == -1
        if missing:
            if self._current is None:
                self._current = self._defaults()
                logger.info("rerank settings: no readable file at %s; defaults", path)
            else:
                logger.warning(
                    "rerank settings: file at %s is gone; keeping the last good ones",
                    path,
                )
            self._stamp = stamp
            return
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings file is not a JSON object")
            block: Any = cast(dict[str, Any], data).get(BLOCK)
            if block is not None and not isinstance(block, dict):
                raise ValueError("rerank block is not an object")
            loaded = build_settings(cast("dict[str, Any] | None", block))
        except Exception as exc:
            # Parsing or validation can also fail with non-JSON errors.
            self.last_error = describe_error(exc)
            logger.warning(
                "rerank settings not applied, keeping the last good ones: %s",
                self.last_error,
            )
            if self._current is None:
                self._current = self._defaults()
        else:
            self._current = loaded
            self.last_error = None
            logger.info(
                "rerank settings loaded: enabled=%s provider=%s",
                loaded.ENABLED,
                loaded.PROVIDER,
            )
        # A bad file is not retried until it changes.
        self._stamp = stamp

    @staticmethod
    def _defaults() -> RerankSettings:
        try:
            return build_settings(None)
        except ValidationError as exc:
            logger.warning(
                "rerank settings: %s; reranking stays off", describe_error(exc)
            )
            return RerankSettings()


loader = SettingsLoader()


def current() -> RerankSettings:
    """The current settings from the file, reloaded if it changed."""
    return loader.get()


def __getattr__(name: str) -> Any:
    if name == "RERANK":
        return loader.get()
    if name.isupper():
        from src.config import settings as honcho_settings

        return getattr(honcho_settings, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
