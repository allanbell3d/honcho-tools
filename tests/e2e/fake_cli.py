"""Fake codex / claude CLI. Records argv, environment variable names and the case marker found on stdin,
then prints output in the shape the real CLI prints (codex --json events, claude --output-format json).

Usage (as BRIDGE_EXE / BRIDGE_CLAUDE JSON list): ["<python>", "<this file>", "codex"|"claude"]
The record file is E2E_CLI_LOG. No network, no model.
"""
import json
import os
import re
import sys


def schema_answer(schema):
    props = (schema or {}).get("properties") or {}
    if "tool_calls" in props:
        return {"tool_calls": [], "final": "done"}
    if "final" in props:
        return {"final": "done"}
    return {"explicit": []}


def main():
    kind = sys.argv[1]
    argv = sys.argv[2:]
    stdin = sys.stdin.read()
    case = re.search(r"E2E-CASE\[([^\]]+)\]", stdin)
    schema = None
    if kind == "codex" and "--output-schema" in argv:
        with open(argv[argv.index("--output-schema") + 1], encoding="utf-8") as handle:
            schema = json.load(handle)
    if kind == "claude" and "--json-schema" in argv:
        schema = json.loads(argv[argv.index("--json-schema") + 1])
    shown = []
    for i, arg in enumerate(argv):
        if i > 0 and argv[i - 1] in ("--json-schema", "--system-prompt") or len(arg) > 300:
            shown.append("<%d chars>" % len(arg))
        elif i > 0 and argv[i - 1] == "--output-schema":
            shown.append("<schema file>")
        else:
            shown.append(arg)
    record = {"kind": kind, "case": case.group(1) if case else None, "argv": shown,
              "env_names": sorted(os.environ), "schema_keys": sorted(((schema or {}).get("properties") or {}).keys())}
    with open(os.environ["E2E_CLI_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    answer = schema_answer(schema)
    if kind == "codex":
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(answer)}}))
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}}))
    else:
        print(json.dumps({"type": "result", "is_error": False, "result": json.dumps(answer),
                          "structured_output": answer, "usage": {"input_tokens": 3, "output_tokens": 2}}))


if __name__ == "__main__":
    main()
