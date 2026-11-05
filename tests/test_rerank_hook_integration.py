"""The hook proxy against the rerank component's real settings and client (rerank/honcho_rerank).

Skipped unless a Honcho v3.5.0 tree is on PYTHONPATH, because the rerank component imports
Honcho. The check runs in a child interpreter (HONCHO_RERANK_PYTHON, else the one running
pytest) that needs Honcho's dependencies. The child adds this repository's rerank/ folder to
the `src` package path, so the component imports as src.honcho_rerank, as it is mounted in the
container. Nothing here hard-codes a path outside the repository.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import sample_settings, write_settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "src" / "hooks"
RERANK = ROOT / "src" / "rerank"

CHILD = r'''
import json, os, sys
wrapper_src, hooks, control, log = sys.argv[1:5]
sys.path.insert(0, hooks)
os.environ["CONTROL_FILE"] = control
os.environ["POLICY_LOG"] = log
os.environ["RERANK_SETTINGS"] = control
os.environ["RERANK_API_KEY"] = "test-key"
import src
src.__path__.append(wrapper_src)
import policies_runtime as pr
import src.honcho_rerank as rerank
from src.honcho_rerank import client
from src.honcho_rerank.settings import RerankSettings
from src.config import settings as honcho_settings

base = rerank.settings.RERANK
out = {"base_type": type(base).__name__, "base_candidates": base.CANDIDATES, "base_enabled": base.ENABLED, "base_cap": base.DAILY_REQUEST_CAP}
policies = pr.Policies()
pr.install_rerank(policies)
out["proxy"] = type(rerank.settings).__name__
out["outside_is_base"] = rerank.settings.RERANK is base
out["outside_should"] = client.should_rerank("dream")
decision = policies.decide("observer", "observed", None)
out["source"] = decision.rerank_source
out["rerank"] = decision.rerank
token = pr._decision.set(decision)
try:
    r = rerank.settings.RERANK
    out["inside_is_rerank_settings"] = isinstance(r, RerankSettings)
    out["inside_is_base"] = r is base
    out["inside"] = {k: getattr(r, k) for k in ("ENABLED", "PROVIDER", "MODEL", "BASE_URL", "CANDIDATES", "MAX_DOCUMENTS", "DAILY_REQUEST_CAP", "TIMEOUT_SECONDS", "CALLERS", "MAX_QUERY_CHARS")}
    out["inside_api_key_kept"] = r.API_KEY == base.API_KEY
    out["inside_should"] = client.should_rerank("dream")
    out["inside_dedupe"] = client.should_rerank("dedupe")
    out["candidate_count_5"] = client.candidate_count(5)
    out["max_documents"] = client.max_documents()
    out["same_object_twice"] = rerank.settings.RERANK is r
    out["cache_passthrough"] = rerank.settings.CACHE.ENABLED == honcho_settings.CACHE.ENABLED
finally:
    pr._decision.reset(token)
out["after_is_base"] = rerank.settings.RERANK is base
bad = pr.Decision(policy={}, source="default", mode="stock", backend="none", backend_name=None, gate="ran",
                  observer="a", observed="b", rerank={"base_url": "http://[bad", "candidates": 9}, rerank_source="default")
token = pr._decision.set(bad)
try:
    out["bad_is_base"] = rerank.settings.RERANK is base
    out["bad_candidates"] = rerank.settings.RERANK.CANDIDATES
finally:
    pr._decision.reset(token)
out["base_after"] = {"CANDIDATES": base.CANDIDATES, "ENABLED": base.ENABLED}
print(json.dumps(out))
'''


def _wrapper() -> tuple[Path, str]:
    source = RERANK
    assert (source / "honcho_rerank" / "settings.py").is_file(), "rerank/honcho_rerank is missing"
    on_path = [Path(item) for item in os.environ.get("PYTHONPATH", "").split(os.pathsep) if item]
    if not any((item / "src" / "config.py").is_file() for item in on_path):
        pytest.skip("no Honcho v3.5.0 tree on PYTHONPATH: the wrapper imports Honcho")
    return source, os.environ.get("HONCHO_RERANK_PYTHON", "").strip() or sys.executable


def test_hook_proxy_against_the_wrapper(tmp_path):
    wrapper_src, python = _wrapper()
    data = sample_settings()
    data["rerank"] = {"enabled": False, "provider": "voyage", "candidates": 100, "daily_request_cap": 50}
    data["defaults"]["rerank"] = {"candidates": 40, "daily_request_cap": 10, "enabled": False}
    data["peers"]["observer"]["rerank"] = {"enabled": True, "provider": "tei", "base_url": "http://reranker.example:8080", "model": "bge-reranker"}
    data["pairs"]["observer/observed"]["rerank"] = {"candidates": 7}
    control = write_settings(tmp_path / "settings.json", data)
    log = tmp_path / "policy-used.log"
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    env = dict(os.environ)
    run = subprocess.run(
        [python, str(script), str(wrapper_src), str(HOOKS), str(control), str(log)],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(tmp_path),
    )
    assert run.returncode == 0, run.stderr[-3000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["base_type"] == "RerankSettings"
    assert out["base_candidates"] == 100 and out["base_enabled"] is False and out["base_cap"] == 50  # the global block
    assert out["proxy"] == "RerankSettingsProxy"
    assert out["outside_is_base"] is True
    assert out["outside_should"] is False
    assert out["source"] == "observer/observed"
    assert out["rerank"] == {"enabled": True, "provider": "tei", "model": "bge-reranker", "base_url": "http://reranker.example:8080", "candidates": 7, "daily_request_cap": 10}
    assert out["inside_is_rerank_settings"] is True and out["inside_is_base"] is False
    # The global switch is off, so the dream stays off; the other keys still apply.
    assert out["inside"]["ENABLED"] is False
    assert out["inside"]["PROVIDER"] == "tei"
    assert out["inside"]["MODEL"] == "bge-reranker"
    assert out["inside"]["BASE_URL"] == "http://reranker.example:8080"
    assert out["inside"]["CANDIDATES"] == 7
    assert out["inside"]["DAILY_REQUEST_CAP"] == 10
    assert out["inside"]["MAX_DOCUMENTS"] is None
    assert out["inside"]["TIMEOUT_SECONDS"] == 5.0
    assert out["inside"]["MAX_QUERY_CHARS"] == 2000
    assert out["inside"]["CALLERS"] == ["dream", "dialectic", "prefetch"]
    # The dream names another provider and host, so the global key is not sent there.
    assert out["inside_api_key_kept"] is False
    assert out["inside_should"] is False
    assert out["inside_dedupe"] is False
    assert out["max_documents"] == 32  # the wrapper's TEI default
    assert out["candidate_count_5"] == 7
    assert out["same_object_twice"] is True
    assert out["cache_passthrough"] is True
    assert out["after_is_base"] is True
    assert out["bad_is_base"] is True and out["bad_candidates"] == out["base_candidates"]
    assert out["base_after"] == {"CANDIDATES": out["base_candidates"], "ENABLED": out["base_enabled"]}
    text = log.read_text(encoding="utf-8")
    assert "rerank hook installed" in text
    assert "rerank not applied a/b from default: ValidationError" in text
