"""Shared set-up: every test writes under its own temporary folder.

The components default to the container folders (/logs, /data, /config). On a test machine
those would be real folders at the drive root, so each test points every log, status and data
path at tmp_path first. A test that checks a default removes the variable itself.
"""
import pytest


@pytest.fixture(autouse=True)
def _local_paths(tmp_path, monkeypatch):
    logs = tmp_path / "_logs"
    # DATA_DIR is tmp_path itself, so instructions/ and backups/ sit beside a test's settings.json.
    data = tmp_path
    monkeypatch.setenv("POLICY_LOG", str(logs / "policy-used.log"))
    monkeypatch.setenv("POLICY_STATUS", str(logs / "hook-status.json"))
    monkeypatch.setenv("SCHEDULER_LOG", str(logs / "dream-nas.log"))
    monkeypatch.setenv("BRIDGE_LOG", str(logs / "bridge-calls.jsonl"))
    monkeypatch.setenv("RESTART_LOG", str(logs / "restart.log"))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("INSTRUCTIONS_DIR", str(data / "instructions"))
    # A token set on the test machine would close the bridge to the tests that expect it open.
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
