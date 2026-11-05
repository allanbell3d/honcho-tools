"""API entrypoint: install the rerank hook, then serve Honcho as its image does.

Mirrors Honcho's ``docker/entrypoint.sh``: run ``scripts/provision_db.py``, then serve
``src.main:app`` on 0.0.0.0:8000 with ``API_WORKERS`` workers (default 1), with the
options ``fastapi run`` uses. Run from Honcho's root (``/app``)::

    python -m src.honcho_rerank.api_main

Each worker builds the app through ``create_app``, so the hook is installed in every
worker process, and the app's lifespan closes the shared HTTP client at shutdown.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HOST = "0.0.0.0"
PORT = 8000
# Import string for the factory; the package name it was loaded under.
APP_FACTORY = f"{__package__}.api_main:create_app"


def create_app() -> Any:
    """Install the hook, then return Honcho's FastAPI app with its lifespan wrapped."""
    from .hook import install, wrap_lifespan

    from .routing_telemetry import install as install_routing
    install_routing()
    installed = True
    try:
        install()
    except RuntimeError:
        # check_contract logs the miss and raises. Stock Honcho still serves.
        installed = False
    from src.main import app
    if installed:
        wrap_lifespan(app)
    return app


def _log_config() -> dict[str, Any] | None:
    try:
        from fastapi_cli.utils.cli import get_uvicorn_log_config
    except Exception:
        return None
    return get_uvicorn_log_config()


def main() -> None:
    from .hook import install

    from .routing_telemetry import install as install_routing
    install_routing()
    try:
        install()
    except RuntimeError:
        # check_contract logs the miss and raises. Continue into stock Honcho.
        pass
    print("Running database migrations...", flush=True)
    subprocess.run(
        [sys.executable, str(Path.cwd() / "scripts" / "provision_db.py")], check=True
    )
    print("Starting API server...", flush=True)
    import uvicorn

    options: dict[str, Any] = {
        "factory": True,
        "host": HOST,
        "port": PORT,
        "workers": int(os.environ.get("API_WORKERS") or "1"),
        "root_path": "",
        "proxy_headers": True,
        "forwarded_allow_ips": None,
    }
    log_config = _log_config()
    if log_config is not None:
        options["log_config"] = log_config
    uvicorn.run(APP_FACTORY, **options)


if __name__ == "__main__":
    main()
