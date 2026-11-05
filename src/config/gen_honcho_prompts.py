"""Build config/honcho-prompts.json: Honcho's stock prompts for intake, summaries, questions and dreams.

Nothing is imported from Honcho. The prompt functions are cut out of the source
with ast and run with placeholders, so the text is exactly what Honcho sends,
with {braces} where Honcho puts the messages, peer names and so on.

Each prompt is stored twice: "without" (no custom text) and "with", where
CUSTOM marks the spot Honcho fills with the custom text. The dashboard shows
one or the other, so what it displays is what the model receives.

Dreams are stored in parts, because the dashboard puts them together per peer:
each specialist's system prompt, the optional sections Honcho appends when brief,
budget or notes are on, and the two custom text sections (dream, peer card).

    python src/config/gen_honcho_prompts.py <path to Honcho src folder>

The version string is project.version from the pyproject.toml beside src.
"""
from __future__ import annotations

import __future__
import ast
import inspect
import json
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

CUSTOM = "\u0000CUSTOM\u0000"

# file -> names to cut out (functions and module-level assignments)
SOURCES = {
    "utils/formatting.py": ["custom_instructions_section"],
    "deriver/prompts.py": ["minimal_deriver_prompt"],
    "utils/summarizer.py": ["short_summary_prompt", "long_summary_prompt"],
    "dialectic/prompts.py": [
        "_PAIR_TOOL_GROUPS", "_WORKSPACE_TOOL_GROUPS", "PAIR_PROMPT_TOOLS", "WORKSPACE_PROMPT_TOOLS",
        "_available_tool_names", "_render_tool_groups", "_custom_instructions_suffix",
        "agent_system_prompt", "workspace_agent_system_prompt",
    ],
    "utils/memory_brief.py": ["_LEVELS_TEXT", "_IDS_TEXT", "_TOOL_COVERAGE", "memory_layout_section"],
    "dreamer/working_memory.py": ["PENDING_TTL_DREAMS", "COVERED_TTL_DREAMS", "notes_system_section"],
    "dreamer/specialists.py": [
        "SEARCH_TOOL_NAMES", "budget_system_section", "PEER_CARD_SYSTEM_SECTION", "custom_instructions_prompt",
        "DeductionSpecialist.build_system_prompt", "InductionSpecialist.build_system_prompt",
        "CardRefreshSpecialist.build_system_prompt",
    ],
}
# Dream specialists: (class, label, gets the peer card text, gets brief/budget/notes)
SPECIALISTS = [
    ("DeductionSpecialist", "Deduction: new conclusions and the peer card (system prompt)", True, True),
    ("InductionSpecialist", "Induction: patterns (system prompt)", False, True),
    ("CardRefreshSpecialist", "Card refresh: peer card only (system prompt)", True, False),
]
LIMIT, WINDOW, NOTES_CAP = 90001, 90002, 90003


def _defined_name(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return None


def _definitions(tree: ast.Module) -> dict:
    """Top-level names, plus Class.method for methods (cut out as plain functions)."""
    found = {}
    for node in tree.body:
        found[_defined_name(node)] = node
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    renamed = ast.FunctionDef(**{**item.__dict__, "name": f"{node.name}__{item.name}", "decorator_list": []})
                    found[f"{node.name}.{item.name}"] = renamed
    return found


def load(src: Path) -> dict:
    space: dict = {"c": inspect.cleandoc, "Iterable": Iterable, "__name__": "honcho_prompts"}
    for rel, names in SOURCES.items():
        found = _definitions(ast.parse((src / rel).read_text(encoding="utf-8")))
        missing = [name for name in names if name not in found]
        if missing:
            raise SystemExit(f"{rel}: not found: {', '.join(missing)}")
        module = ast.fix_missing_locations(ast.Module(body=[found[name] for name in names], type_ignores=[]))
        # Annotations stay unevaluated: they name Honcho types that are not loaded here.
        exec(compile(module, str(src / rel), "exec", flags=__future__.annotations.compiler_flag, dont_inherit=True), space)
    return space


def both(build) -> dict:
    return {"without": build(None), "with": build(CUSTOM)}


def build(src: Path) -> dict:
    p = load(src)
    messages = "{messages, one <message> tag each}"
    conversation = "{the conversation}"
    previous = "{the previous summary}"
    tokens = "{target tokens}"
    card = ["{peer card lines}"]
    jobs = {
        "reasoning": [
            {"name": "Intake: facts from new messages",
             **both(lambda ci: p["minimal_deriver_prompt"]("{peer}", messages, ci))},
        ],
        "summary": [
            {"name": "Short summary",
             **both(lambda ci: p["short_summary_prompt"](conversation, tokens, previous, ci))},
            {"name": "Long summary",
             **both(lambda ci: p["long_summary_prompt"](conversation, tokens, previous, ci))},
        ],
        "dialectic": [
            {"name": "Question about one peer (system prompt)",
             **both(lambda ci: p["agent_system_prompt"]("{peer}", "{peer}", card, None, None, ci))},
            {"name": "Question across the workspace (system prompt)",
             **both(lambda ci: p["workspace_agent_system_prompt"](None, ci))},
        ],
    }
    for items in jobs.values():
        for item in items:
            if CUSTOM not in item["with"]:
                raise SystemExit(f"{item['name']}: custom text slot not found")
            item["with"] = item["with"].replace(CUSTOM, "{CUSTOM}")
    return jobs


def build_dream(p: dict) -> dict:
    def numbers(text: str) -> str:
        return (text.replace(str(LIMIT), "{tool calls}").replace(str(WINDOW), "{write rounds}")
                .replace(str(NOTES_CAP), "{notes cap}"))

    def injected(dream, card) -> str:
        config = SimpleNamespace(dream=SimpleNamespace(custom_instructions=dream),
                                 peer_card=SimpleNamespace(custom_instructions=card))
        return p["custom_instructions_prompt"](config, peer_card_enabled=True)

    specialists = []
    for cls, name, card, extras in SPECIALISTS:
        self = SimpleNamespace(rebuild=False)
        specialists.append({"name": name, "peer_card": card, "extras": extras,
                            "base": p[f"{cls}__build_system_prompt"](self, "{peer}", peer_card_enabled=True)})
    custom = {"dream": injected(CUSTOM, None), "peer_card": injected(None, CUSTOM)}
    for key, text in custom.items():
        if text.count(CUSTOM) != 1:
            raise SystemExit(f"dream {key}: custom text slot not found")
        custom[key] = text.replace(CUSTOM, "{CUSTOM}")
    return {
        "specialists": specialists,
        # Appended in this order, each after a blank line, when the setting is on.
        "sections": {
            "brief": p["memory_layout_section"](available_tools=p["SEARCH_TOOL_NAMES"], cite_ids=True),
            "budget": numbers(p["budget_system_section"](LIMIT, WINDOW)),
            "notes": numbers(p["notes_system_section"](NOTES_CAP)),
        },
        # Appended last: the dream text, then the peer card text (peer card specialists only).
        "custom": custom,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    src = Path(argv[1])
    version = tomllib.loads((src.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    out = Path(__file__).resolve().parent / "honcho-prompts.json"
    data = {"honcho_version": version, "slot": "{CUSTOM}", "jobs": build(src), "dream": build_dream(load(src))}
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out} (Honcho {version})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
