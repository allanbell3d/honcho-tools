"""The stack layout: one config folder, one data folder, one logs folder, services that start alone.

- Every log, run record and status file defaults to LOGS_DIR (/logs in the containers).
- deploy/compose.yaml is the honcho-tools project. It mounts LOGS_DIR into each service that writes
  a log, CONFIG_DIR at /config, DATA_DIR at /data for the dashboard, only config.toml from the Honcho
  folder, and has no depends_on between its services.
- The dashboard, the bridge and the scheduler start with no settings.json and none of the other
  services present.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for folder in ("hooks", "bridge", "scheduler", "telemetry"):
    sys.path.insert(0, str(SRC / folder))

import bridge_cli  # noqa: E402
import dream_nas  # noqa: E402
import policies_runtime  # noqa: E402
import receiver  # noqa: E402

_spec = importlib.util.spec_from_file_location("dashboard_app_layout", SRC / "dashboard" / "app" / "app.py")
dashboard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dashboard)

LOG_ENV = ("POLICY_LOG", "POLICY_STATUS", "SCHEDULER_LOG", "BRIDGE_LOG", "RESTART_LOG")


def _compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8"))


# ---------- logs ----------

def test_every_log_path_defaults_to_the_logs_folder(monkeypatch):
    for name in LOG_ENV:
        monkeypatch.delenv(name, raising=False)
    logs = Path("/logs")
    paths = {
        "hook policy log": policies_runtime.default_log_path(),
        "hook status": policies_runtime.default_status_path(),
        "bridge call log": bridge_cli.log_path(),
        "scheduler log": dream_nas.log_path(),
        "dashboard policy log": dashboard.policy_log_path(),
        "dashboard scheduler log": dashboard.scheduler_log_path(),
        "dashboard hook status": dashboard.hook_status_path(),
        "dashboard restart log": dashboard.restart_log_path(),
        "telemetry maintenance log": receiver.load_config({"TELEMETRY_TOKEN": "t"}).maintenance_log,
    }
    for label, path in paths.items():
        assert path.parent == logs, f"{label} is {path}"
    assert paths["hook policy log"] == paths["dashboard policy log"]
    assert paths["hook status"] == paths["dashboard hook status"]
    assert paths["scheduler log"] == paths["dashboard scheduler log"]


def test_backup_log_defaults_to_the_logs_folder():
    text = (SRC / "backup" / "backup.sh").read_text(encoding="utf-8")
    assert 'LOG="${BACKUP_LOG:-/logs/backup.log}"' in text
    assert "${BACKUP_ROOT}/backup.log" not in text


def test_telemetry_maintenance_log_goes_where_it_is_told(tmp_path):
    log = tmp_path / "logs" / "maintenance.log"
    receiver.run_maintenance(tmp_path / "events", log_path=log)
    assert log.is_file()
    assert not (tmp_path / "events" / "maintenance.log").exists()


def test_runs_log_sits_beside_the_policy_log(tmp_path, monkeypatch):
    monkeypatch.delenv("POLICY_LOG", raising=False)
    monkeypatch.delenv("POLICY_STATUS", raising=False)
    # No reload: it would write its first log line to the real /logs.
    monkeypatch.setattr(policies_runtime.Policies, "reload", lambda self: None)
    store = policies_runtime.Policies(tmp_path / "settings.json")
    assert store.log_path == Path("/logs/policy-used.log")
    assert store.log_path.with_name("runs.log") == Path("/logs/runs.log")
    assert store.status_path.parent == Path(dashboard.hook_status_path()).parent


def test_no_component_keeps_a_log_beside_its_code():
    for path in (SRC / "hooks" / "policies_runtime.py", SRC / "bridge" / "bridge_cli.py",
                 SRC / "scheduler" / "dream_nas.py", SRC / "dashboard" / "app" / "app.py",
                 SRC / "telemetry" / "receiver.py"):
        text = path.read_text(encoding="utf-8")
        assert 'parent / "logs"' not in text, path
        assert "/control/hook-status.json" not in text, path
        assert "/control/restart.log" not in text, path


def test_compose_mounts_logs_dir_into_every_service_that_logs():
    services = _compose()["services"]
    for name in ("scheduler", "dashboard", "bridge", "telemetry", "backup"):
        volumes = [str(item) for item in services[name].get("volumes", [])]
        assert any(re.match(r"^\$\{LOGS_DIR[:?}-].*:/logs(:rw)?$", item) for item in volumes), (name, volumes)


def test_honcho_mounts_pass_the_hook_log_to_the_deriver():
    text = (ROOT / "deploy" / "honcho-mounts.yaml").read_text(encoding="utf-8")
    assert "POLICY_LOG: /logs/policy-used.log" in text
    assert "POLICY_STATUS: /logs/hook-status.json" in text
    assert ":/logs" in text


# ---------- folders ----------

def test_compose_is_the_honcho_tools_project():
    data = _compose()
    assert data["name"] == "honcho-tools"
    assert {"dashboard", "scheduler", "bridge", "telemetry", "backup"} <= set(data["services"])
    assert "control" not in data["services"]


def test_compose_uses_one_config_folder_and_one_data_folder():
    services = _compose()["services"]
    text = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    assert "CONTROL_DIR" not in text
    assert "restart-allow" not in text.lower()
    for name, mode in (("scheduler", "rw"), ("dashboard", "rw"), ("bridge", "ro")):
        volumes = [str(item) for item in services[name]["volumes"]]
        assert any(item.startswith("${CONFIG_DIR") and item.endswith(":/config:" + mode) for item in volumes), (name, volumes)
        assert services[name]["environment"]["CONTROL_FILE"] == "/config/settings.json", name
    dashboard_volumes = [str(item) for item in services["dashboard"]["volumes"]]
    assert any(item.startswith("${DATA_DIR") and item.endswith(":/data:rw") for item in dashboard_volumes)
    assert any(item.startswith("../src/config:") and item.endswith(":/templates:ro") for item in dashboard_volumes)
    for name, service in services.items():
        if name != "dashboard":
            assert not any("${DATA_DIR" in str(item) for item in service.get("volumes", [])), name


def test_no_container_mounts_the_honcho_folder():
    services = _compose()["services"]
    for name, service in services.items():
        for item in service.get("volumes", []):
            item = str(item)
            if item.startswith("${HONCHO_ROOT"):
                # The only file taken from the Honcho folder: config.toml, into the dashboard.
                assert name == "dashboard", (name, item)
                # Outside /config, so Docker leaves no empty mountpoint file beside settings.json.
                assert re.match(r"^\$\{HONCHO_ROOT[^}]*\}/config\.toml:/honcho/config\.toml:rw$", item), item
        env_files = [str(item) for item in service.get("env_file", [])]
        # Honcho's .env, this stack's .env, and the tokens file setup.sh writes in the secrets folder.
        allowed = (".env", "${SECRETS_DIR:-../cli-secrets}/tokens.env")
        assert all(item in allowed or item.endswith("}/.env") for item in env_files), (name, env_files)


def test_bridge_is_network_only():
    bridge = _compose()["services"]["bridge"]
    assert "ports" not in bridge
    assert "healthcheck" in bridge


def test_env_example_names_the_folders():
    text = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
    for name in ("HONCHO_ROOT", "CONFIG_DIR", "DATA_DIR", "LOGS_DIR", "TELEMETRY_DIR", "BACKUP_DIR", "SECRETS_DIR"):
        assert re.search(rf"^{name}=", text, re.MULTILINE), name
    assert "CONTROL_DIR" not in text
    assert "RESTART_ALLOW_FILE" not in text


def test_dashboard_paths_split_between_config_and_data(tmp_path, monkeypatch):
    monkeypatch.delenv("INSTRUCTIONS_DIR", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONTROL_FILE", str(tmp_path / "config" / "settings.json"))
    monkeypatch.delenv("HONCHO_CONFIG_BAK_DIR", raising=False)
    monkeypatch.delenv("MODELS_FILE", raising=False)
    assert dashboard.instructions_dir() == tmp_path / "data" / "instructions"
    assert dashboard.backups_root() == tmp_path / "config" / "backups"
    assert dashboard.honcho_config.backup_dir(tmp_path / "config" / "config.toml") == tmp_path / "config" / "backups" / "config-toml"
    assert dashboard.models_path() == tmp_path / "config" / "models.json"
    monkeypatch.delenv("DATA_DIR")
    assert dashboard.instructions_dir() == Path("/data/instructions")
    monkeypatch.delenv("INSTRUCTIONS_DIR", raising=False)
    assert policies_runtime.default_instructions_dir() == Path("/data/instructions")


def test_every_settings_path_defaults_to_the_config_folder(monkeypatch):
    monkeypatch.delenv("CONTROL_FILE", raising=False)
    monkeypatch.delenv("HONCHO_CONFIG_FILE", raising=False)
    monkeypatch.delenv("HONCHO_CONFIG_BAK_DIR", raising=False)
    settings = Path("/config/settings.json")
    assert dashboard.settings_path() == settings
    assert dream_nas.settings_path() == settings
    assert bridge_cli.settings_path() == settings
    assert policies_runtime.default_settings_path() == settings
    assert dashboard.honcho_config.config_path() == Path("/honcho/config.toml")
    assert dashboard.honcho_config.backup_dir(dashboard.honcho_config.config_path()) == Path("/config/backups/config-toml")
    assert dashboard.backups_root() == Path("/config/backups")
    rerank_settings = (SRC / "rerank" / "honcho_rerank" / "settings.py").read_text(encoding="utf-8")
    assert 'DEFAULT_SETTINGS_PATH = "/config/settings.json"' in rerank_settings


def test_scheduler_backups_go_to_the_config_backups_folder(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}\n", encoding="utf-8")
    dream_nas.atomic_write(path, {"n": 1})
    assert [item.name.startswith("settings.json.bak-") for item in (tmp_path / "backups" / "settings").iterdir()] == [True]
    assert list(tmp_path.glob("settings.json.bak-*")) == []


def test_no_service_depends_on_another():
    services = _compose()["services"]
    for name, service in services.items():
        # Each one can be stopped alone: no start order, no shared namespace, no borrowed volumes.
        for key in ("depends_on", "links", "volumes_from", "ipc", "pid"):
            assert key not in service, (name, key)
        assert not str(service.get("network_mode", "")).startswith(("service:", "container:")), name


def test_only_the_proxies_mount_the_docker_socket():
    for name, service in _compose()["services"].items():
        socket = [str(item) for item in service.get("volumes", []) if "docker.sock" in str(item)]
        assert bool(socket) == (name in ("docker-proxy", "docker-proxy-action")), name
    read = _compose()["services"]["docker-proxy"]["environment"]
    action = _compose()["services"]["docker-proxy-action"]["environment"]
    assert read["POST"] == "0" and action["CONTAINERS"] == "0"
    for env in (read, action):
        assert env["EVENTS"] == env["PING"] == env["VERSION"] == "0"


def test_bridge_is_a_service_with_cli_logins_from_secrets_dir():
    bridge = _compose()["services"]["bridge"]
    assert bridge["build"]["context"] == "../src/bridge"
    volumes = [str(item) for item in bridge["volumes"]]
    assert any(item.startswith("${SECRETS_DIR") and item.endswith(":/home/node/.codex") for item in volumes)
    assert any(item.startswith("${SECRETS_DIR") and item.endswith(":/home/node/.claude") for item in volumes)
    assert bridge["environment"]["CONTROL_FILE"] == "/config/settings.json"


# ---------- each service starts without the others ----------

def test_dashboard_starts_without_settings_or_other_services(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_FILE", str(tmp_path / "config" / "settings.json"))
    monkeypatch.setenv("TELEMETRY_DIR", str(tmp_path / "telemetry"))
    monkeypatch.setenv("HONCHO_CONFIG_FILE", str(tmp_path / "config" / "config.toml"))
    for name in ("DOCKER_PROXY_URL", "DOCKER_PROXY_ACTION_URL", "HONCHO_WORKSPACE", "HONCHO_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dashboard, "_settings_missing_logged", {"value": False})
    lines = []
    monkeypatch.setattr(dashboard._instruction_logger, "info", lambda message, *args: lines.append(message % args))
    with TestClient(dashboard.app) as app:
        settings = app.get("/api/settings")
        assert settings.status_code == 200
        assert settings.json()["missing"] is True
        status = app.get("/api/status")
        assert status.status_code == 200
        assert status.json()["scheduler_log_missing"] is True
        assert status.json()["policy_log_missing"] is True
        containers = app.get("/api/containers").json()
        assert containers["error"] == "DOCKER_PROXY_URL is unset"
        assert app.get("/api/rerank/stats").status_code == 200
        assert app.get("/api/settings").status_code == 200
    assert sum("settings.json missing at" in line for line in lines) == 1
    assert not (tmp_path / "config" / "settings.json").exists()


def test_bridge_starts_without_settings_or_other_services(tmp_path, capsys):
    app = bridge_cli.Bridge(tmp_path / "missing" / "settings.json", tmp_path / "calls.jsonl")
    health = app.health()
    assert health["settings_ok"] is False
    assert health["settings_error"] == "settings.json unreadable"
    assert health["active"] is None
    assert health["concurrency"] >= 1
    status, body = app.handle("GET", "/v1/models", None)
    assert status == 200 and body["data"] == []
    status, body = app.handle("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "x"}]})
    assert status == 400
    assert "unknown backend" in body["error"]["message"]
    out = capsys.readouterr().out
    assert out.count("settings.json missing at") == 1


def test_bridge_picks_up_settings_written_after_start(tmp_path):
    path = tmp_path / "settings.json"
    app = bridge_cli.Bridge(path, tmp_path / "calls.jsonl")
    assert app.health()["settings_ok"] is False
    path.write_text(json.dumps({"providers": {"active": "x", "backends": {"x": {"kind": "openai-compat", "base_url": "http://127.0.0.1:9", "key_env": "X_KEY", "model": "m"}}}}), encoding="utf-8")
    assert app.health()["settings_ok"] is True
    assert app.health()["active"] == "x"


def test_scheduler_starts_without_settings_database_or_api(tmp_path, monkeypatch):
    log = tmp_path / "dream-nas.log"
    monkeypatch.setenv("CONTROL_FILE", str(tmp_path / "missing" / "settings.json"))
    monkeypatch.setenv("SCHEDULER_LOG", str(log))
    monkeypatch.delenv("HONCHO_WORKSPACE", raising=False)
    monkeypatch.delenv("HONCHO_API_TOKEN", raising=False)
    handlers = {}
    monkeypatch.setattr(dream_nas.signal, "signal", lambda signum, handler: handlers.setdefault("stop", handler))

    def one_poll(_seconds, _stopped):
        handlers["stop"]()

    monkeypatch.setattr(dream_nas, "_sleep", one_poll)

    def no_database(*_args, **_kwargs):
        raise AssertionError("the scheduler opened the database without a workspace")

    monkeypatch.setattr(dream_nas.Database, "_connect", no_database)
    dream_nas.main()
    text = log.read_text(encoding="utf-8")
    assert "settings.json unreadable; sleeping" in text
    assert "=== start" in text
    assert "=== done" in text


def test_backup_reads_the_database_identity_from_the_environment():
    # Host, user and database were hard-coded, so a stock Honcho database (user postgres) was never dumped.
    script = (ROOT / "src" / "backup" / "backup.sh").read_text(encoding="utf-8")
    for line in ('DB_HOST="${PGHOST:-database}"', 'DB_PORT="${PGPORT:-5432}"', 'DB_USER="${PGUSER:-honcho}"', 'DB_NAME="${PGDATABASE:-honcho}"'):
        assert line in script
    assert '-h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -f "$partial" "$DB_NAME"' in script
    assert "-h database -U honcho" not in script
    assert 'PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"' in script


# ---------- minor-tag images, packages without versions ----------

def test_built_images_use_minor_tags_and_unpinned_packages():
    expected = {"scheduler": "python:3.12-slim", "dashboard": "python:3.12-slim", "bridge": "node:22-slim"}
    for name, base in expected.items():
        text = (SRC / name / "Dockerfile").read_text(encoding="utf-8")
        assert re.search(rf"^FROM {re.escape(base)}$", text, re.M), name
        assert "==" not in text, name
        for banned in ("CODEX_VERSION", "CLAUDE_CODE_VERSION", "DISABLE_AUTOUPDATER"):
            assert banned not in text, (name, banned)
    scheduler = (SRC / "scheduler" / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.txt" in scheduler
    dashboard = (SRC / "dashboard" / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.txt" in dashboard
    bridge = (SRC / "bridge" / "Dockerfile").read_text(encoding="utf-8")
    assert "npm install -g @openai/codex" in bridge
    assert "npm install -g @anthropic-ai/claude-code" in bridge
    assert _compose()["services"]["telemetry"]["image"] == "python:3.12-slim"


def test_bridge_cli_build_args_default_to_zero():
    dockerfile = (SRC / "bridge" / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG INSTALL_CODEX=0" in dockerfile
    assert "ARG INSTALL_CLAUDE=0" in dockerfile
    example = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^INSTALL_CODEX=0$", example, re.M)
    assert re.search(r"^INSTALL_CLAUDE=0$", example, re.M)
    args = _compose()["services"]["bridge"]["build"]["args"]
    assert args["INSTALL_CODEX"] == "${INSTALL_CODEX:-0}"
    assert args["INSTALL_CLAUDE"] == "${INSTALL_CLAUDE:-0}"


def test_requirements_files_list_direct_packages_without_versions():
    # The image packages of this stack. src/rerank/requirements.txt is not an image: it states the lowest
    # versions the rerank wrapper needs inside Honcho's own environment, and keeps them.
    paths = [SRC / name / "requirements.txt" for name in ("dashboard", "scheduler")]
    for path in paths:
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert lines, path.parent.name
        for line in lines:
            assert not any(mark in line for mark in ("==", ">=", "<=", "~=", ">", "<")), (path.parent.name, line)
            assert re.fullmatch(r"[A-Za-z0-9_.-]+(\[[A-Za-z0-9_,]+\])?", line), (path.parent.name, line)
