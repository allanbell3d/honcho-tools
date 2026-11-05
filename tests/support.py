"""Shared helpers for the consumer tests.

No network, no database, and no Honcho import. Sample names are placeholders.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_settings(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def sample_settings() -> dict:
    """A small settings object. Backends point at a closed port until a test rewrites them."""
    return {
        "mode": "stock",
        "defaults": {
            "brief": 4,
            "effort": "medium",
            "deduction_rounds": 2,
            "induction_rounds": 3,
            "backend": None,
        },
        "peers": {
            "observer": {"enabled": True, "brief": 8, "backend": None},
            "observed": {"enabled": True, "effort": "low", "backend": "deepseek"},
            "off": {"enabled": False, "brief": 5},
        },
        "pairs": {
            "observer/observed": {"brief": 11},
            "observer/off": {},
        },
        "scheduler": {
            "poll_seconds": 15,
            "max_parallel": 2,
            "dream_type": "omni",
            "skip": ["observer/observed"],
        },
        "providers": {
            "active": "codex",
            "concurrency": 2,
            "timeout_seconds": 30,
            "backends": {
                "codex": {"kind": "cli-codex", "model": "codex-model", "effort": "high"},
                "claude": {"kind": "cli-claude", "model": "claude-model", "effort": "low"},
                "openai": {
                    "kind": "openai-compat",
                    "model": "gpt-test",
                    "base_url": "http://127.0.0.1:9/v1",
                    "key_env": "TEST_OPENAI_KEY",
                },
                "deepseek": {
                    "kind": "openai-compat",
                    "model": "ds-test",
                    "base_url": "http://127.0.0.1:9/v1",
                    "key_env": "TEST_DEEPSEEK_KEY",
                    "thinking_effort": "medium",
                },
                "openrouter": {
                    "kind": "openai-compat",
                    "model": "or-default",
                    "base_url": "http://127.0.0.1:9/v1",
                    "key_env": "TEST_OPENROUTER_KEY",
                    "models": ["or-listed"],
                },
                "anthropic": {
                    "kind": "anthropic-api",
                    "model": "anthropic-test",
                    "key_env": "TEST_ANTHROPIC_KEY",
                    "effort": "high",
                },
            },
        },
    }
