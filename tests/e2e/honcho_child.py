"""Runs inside Honcho 3.5.0's Python environment (stock tree on sys.path).

For each case: load that case's config.toml with Honcho's own settings classes (a rejected file is recorded),
take each requested task's model config exactly as Honcho's call sites do, apply this repository's dream hook
(_stamp_model with a real Policies decision) for dream stages, and make one honcho_llm_call in the call shape
of that job. The calls go to the harness bridge (openai transport) or to the fake Anthropic endpoint
(anthropic transport, via LLM_ANTHROPIC_BASE_URL). The prompt carries E2E-CASE[<case>/<task>] so every capture
can be matched to its case.

argv: jobs.json results.json honcho_tree hooks_dir control_url
"""
import asyncio
import json
import os
import sys
import traceback
import urllib.request

jobs_path, results_path, honcho_tree, hooks_dir, control_url = sys.argv[1:6]
sys.path.insert(0, honcho_tree)
sys.path.insert(0, hooks_dir)
if os.name == "nt" and "fcntl" not in sys.modules:
    # Honcho's reasoning-trace log locks its file with fcntl (POSIX only). The harness runs on Windows: a no-op
    # stand-in lets the module import; trace logging is not part of what is measured.
    import types
    stub = types.ModuleType("fcntl")
    stub.LOCK_EX, stub.LOCK_UN, stub.LOCK_SH, stub.LOCK_NB = 2, 8, 1, 4
    stub.flock = lambda *a, **k: None
    stub.lockf = lambda *a, **k: None
    sys.modules["fcntl"] = stub

import src.config as hc  # noqa: E402
from src.llm.api import honcho_llm_call  # noqa: E402
from src.utils.representation import PromptRepresentation  # noqa: E402
import policies_runtime as pr  # noqa: E402

TOOL = {"name": "search_memory", "description": "Search memory.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}


def task_config(settings, task):
    if task == "deriver":
        return settings.DERIVER.MODEL_CONFIG
    if task == "summary":
        return settings.SUMMARY.MODEL_CONFIG
    if task.startswith("dialectic."):
        return settings.DIALECTIC.LEVELS[task.split(".", 1)[1]].MODEL_CONFIG
    if task in ("dream.deduction", "peer_card"):
        return settings.DREAM.DEDUCTION_MODEL_CONFIG
    if task == "dream.induction":
        return settings.DREAM.INDUCTION_MODEL_CONFIG
    raise KeyError(task)


def dump(cfg):
    return {
        "transport": cfg.transport, "model": cfg.model, "thinking_effort": cfg.thinking_effort,
        "structured_output_mode": cfg.structured_output_mode,
        "base_url": cfg.overrides.base_url, "api_key_env": cfg.overrides.api_key_env,
        "provider_params": cfg.overrides.provider_params,
    }


def control(payload):
    request = urllib.request.Request(control_url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=10).read()


async def call(cfg, task, marker):
    async def executor(name, args):
        return "nothing found"

    text = f"{marker} Say ok."
    if task == "deriver":
        return await honcho_llm_call(model_config=cfg, prompt=text, max_tokens=400, response_model=PromptRepresentation,
                                     json_mode=True, enable_retry=False)
    if task == "summary":
        return await honcho_llm_call(model_config=cfg, prompt=text, max_tokens=400, enable_retry=False)
    messages = [{"role": "system", "content": "You are a test."}, {"role": "user", "content": text}]
    return await honcho_llm_call(model_config=cfg, prompt="", max_tokens=400, tools=[TOOL], tool_choice=None,
                                 tool_executor=executor, max_tool_iterations=2, messages=messages, enable_retry=False)


async def main():
    jobs = json.load(open(jobs_path, encoding="utf-8"))
    results = []
    for job in jobs:
        case = job["case"]
        out = {"case": case, "tasks": {}}
        results.append(out)
        try:
            hc.TOML_CONFIG = hc.load_toml_config(job["config_toml"])
            settings = hc.AppSettings()
        except Exception as exc:  # Honcho would not start with this file
            out["load_error"] = f"{type(exc).__name__}: {str(exc)[:1500]}"
            continue
        out["loaded"] = True
        policies = None
        for task in job["tasks"]:
            entry = out["tasks"].setdefault(task, {})
            try:
                cfg = task_config(settings, task)
                entry["config"] = dump(cfg)
                if job.get("hook") and (task.startswith("dream.") or task == "peer_card"):
                    if policies is None:
                        policies = pr.Policies(path=job["settings_json"])
                    decision = policies.decide("observer", "observed", "honcho-scheduler", "manual")
                    decision.card_refresh = task == "peer_card"
                    cfg = pr._stamp_model(cfg, decision, decision.policy, task)
                    entry["hooked"] = dump(cfg)
                    entry["hook_policy_effort"] = decision.policy.get("effort")
                marker = f"E2E-CASE[{case}/{task}]"
                control({"case": f"{case}/{task}", "settings": job["settings_json"]})
                response = await call(cfg, task, marker)
                entry["ok"] = True
                entry["content"] = str(getattr(response, "content", ""))[:200]
            except Exception as exc:
                entry["ok"] = False
                entry["error"] = f"{type(exc).__name__}: {str(exc)[:800]}"
                entry["trace"] = traceback.format_exc()[-1500:]
    json.dump(results, open(results_path, "w", encoding="utf-8"), indent=1, default=str)


asyncio.run(main())
