"""Deriver entry point. Installs the settings hook, then runs the stock deriver.

The image and the Honcho source stay untouched. The deriver's entrypoint runs the
rerank component first, which then runs this file in the same process:
`/app/.venv/bin/python -m src.honcho_rerank.deriver_main /app/policies/deriver_main.py`
(see deploy/honcho-mounts.yaml).
"""
import json
import logging
import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/policies")

# Imported inside main(): a hook module that cannot even be imported is a hook failure like any other, and must not
# stop Honcho's intake.
policies_runtime = None


def _entry_disabled(section) -> bool:
    if not isinstance(section, dict):
        return False
    return any(isinstance(entry, dict) and entry.get("enabled") is False for entry in section.values())


def _settings_path() -> Path:
    if policies_runtime is not None:
        return policies_runtime.default_settings_path()
    return Path(os.environ.get("CONTROL_FILE") or "/config/settings.json")


def _status_path() -> Path:
    if policies_runtime is not None:
        return policies_runtime.default_status_path()
    return Path(os.environ.get("POLICY_STATUS") or "/logs/hook-status.json")


def _dreams_blocked() -> bool:
    """False only for a readable stock file (mode absent counts as stock) with nothing disabled."""
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict):
        return True
    if data.get("mode", "stock") != "stock":
        return True
    return _entry_disabled(data.get("peers")) or _entry_disabled(data.get("pairs"))


def _write_failed_status(hook_failed: str, dreams_blocked: bool) -> None:
    """Same path and replace as Policies.write_status. The body is only the failure fields."""
    status_path = _status_path()
    status = {"checks_passed": False, "hook_failed": hook_failed, "dreams_blocked": dreams_blocked}
    temporary = status_path.with_name(status_path.name + f".tmp-{os.getpid()}")
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(status) + "\n", encoding="utf-8")
        os.replace(temporary, status_path)
    except OSError:
        logging.getLogger(__name__).error("hook status could not be written")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> None:
    global policies_runtime
    try:
        import policies_runtime as module

        policies_runtime = module
        policies_runtime.install()
    except Exception as exc:
        logging.getLogger(__name__).error("%s", type(exc).__name__)
        blocked = _dreams_blocked()
        if blocked:
            # Honcho builds its settings once, when src.config is first imported. The environment covers a later
            # import; the rerank entrypoint or install() itself may already have imported it, so the loaded flag is
            # turned off too. Honcho reads settings.DREAM.ENABLED when it schedules and when it runs a dream.
            os.environ["DREAM_ENABLED"] = "false"
            loaded = sys.modules.get("src.config")
            try:
                if loaded is not None:
                    loaded.settings.DREAM.ENABLED = False
            except AttributeError:
                logging.getLogger(__name__).error("Honcho settings have no DREAM.ENABLED; dreams rely on DREAM_ENABLED")
        _write_failed_status(type(exc).__name__, blocked)
    runpy.run_module("src.deriver", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
