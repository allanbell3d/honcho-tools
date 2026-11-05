"""setup.sh prepares a checkout: it writes deploy/.env, config/settings.json and config/rates.json once, and never edits Honcho."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def checkout(tmp_path: Path) -> Path:
    tools = tmp_path / "honcho-tools"
    for rel in ("setup.sh", "deploy/.env.example", "deploy/honcho-mounts.yaml", "src/config/settings.example.json",
                "src/config/rates.example.json"):
        (tools / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, tools / rel)
    for folder in ("config", "data", "logs", "cli-secrets"):
        (tools / folder).mkdir()
    return tools


def run(tools: Path, *args: str) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    return subprocess.run([bash, (tools / "setup.sh").as_posix(), *args], capture_output=True, text=True, timeout=30)


def test_setup_writes_the_files_once_and_prints_the_honcho_lines(tmp_path):
    tools = checkout(tmp_path)
    honcho = tmp_path / "honcho"
    honcho.mkdir()
    (honcho / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (honcho / "config.toml").write_text("[app]\n", encoding="utf-8")
    first = run(tools)
    assert first.returncode == 0, first.stderr
    env = (tools / "deploy/.env").read_text(encoding="utf-8")
    assert [line for line in env.splitlines() if line.startswith("HONCHO_ROOT=")][0].endswith("/honcho")
    assert (tools / "config/settings.json").read_bytes() == (ROOT / "src/config/settings.example.json").read_bytes()
    assert (tools / "config/rates.json").read_bytes() == (ROOT / "src/config/rates.example.json").read_bytes()
    assert (tools / "cli-secrets/codex").is_dir() and (tools / "cli-secrets/claude").is_dir()
    assert (tools / "data/instructions").is_dir()
    assert "<CODE_ROOT>" not in first.stdout and "/src/hooks:/app/policies:ro" in first.stdout
    (tools / "deploy/.env").write_text("HONCHO_ROOT=/mine\n", encoding="utf-8")
    (tools / "config/rates.json").write_text("{}\n", encoding="utf-8")
    second = run(tools)
    assert second.returncode == 0, second.stderr
    assert (tools / "deploy/.env").read_text(encoding="utf-8") == "HONCHO_ROOT=/mine\n"
    assert (tools / "config/rates.json").read_text(encoding="utf-8") == "{}\n"
    assert (honcho / ".env").read_text(encoding="utf-8") == "SECRET=1\n"
    assert (honcho / "config.toml").read_text(encoding="utf-8") == "[app]\n"


def test_setup_refuses_without_a_honcho_project(tmp_path):
    tools = checkout(tmp_path)
    result = run(tools, (tmp_path / "nowhere").as_posix())
    assert result.returncode == 1
    assert "No Honcho project" in result.stderr
    assert not (tools / "deploy/.env").exists()


def test_setup_reads_an_example_saved_with_windows_line_endings(tmp_path):
    # A checkout copied from Windows can carry CRLF: no carriage return may reach a path, a folder name or deploy/.env.
    tools = checkout(tmp_path)
    example = tools / "deploy/.env.example"
    example.write_bytes(example.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    honcho = tmp_path / "honcho"
    honcho.mkdir()
    (honcho / ".env").write_text("", encoding="utf-8")
    (honcho / "config.toml").write_text("", encoding="utf-8")
    result = run(tools)
    assert result.returncode == 0, result.stderr
    assert "\r" not in result.stdout
    assert b"\r" not in (tools / "deploy/.env").read_bytes()
    assert sorted(p.name for p in tools.iterdir() if "\r" in p.name) == []


def test_setup_writes_the_tokens_file_once_reusing_honcho_values(tmp_path):
    tools = checkout(tmp_path)
    honcho = tmp_path / "honcho"
    honcho.mkdir()
    (honcho / ".env").write_text("TELEMETRY_TOKEN=kept\n", encoding="utf-8")
    (honcho / "config.toml").write_text("", encoding="utf-8")
    assert run(tools).returncode == 0
    lines = (tools / "cli-secrets/tokens.env").read_text(encoding="utf-8").splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    assert values["TELEMETRY_TOKEN"] == "kept" and values["TELEMETRY_HEADERS"] == '{"Authorization": "Bearer kept"}'
    assert len(values["BRIDGE_TOKEN"]) == 64
    assert (honcho / ".env").read_text(encoding="utf-8") == "TELEMETRY_TOKEN=kept\n"
    before = (tools / "cli-secrets/tokens.env").read_bytes()
    assert run(tools).returncode == 0
    assert (tools / "cli-secrets/tokens.env").read_bytes() == before
