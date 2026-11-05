import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORMATTING = '''
def custom_instructions_section(custom_instructions, *, heading="CUSTOM INSTRUCTIONS:", note=None):
    normalized = (custom_instructions or "").strip()
    if not normalized:
        return ""
    return "\\n".join([heading, note, normalized] if note else [heading, normalized])
'''
DERIVER = '''
def minimal_deriver_prompt(peer_id, messages, custom_instructions=None):
    section = custom_instructions_section(custom_instructions, note="For the target peer.")
    return c(f"""
Extract facts.

{section}

Target peer: {peer_id}
{messages}
""")
'''
SUMMARIZER = '''
def short_summary_prompt(formatted_messages, target_tokens, previous_summary_text, custom_instructions=None):
    return c(f"Short.\\n{custom_instructions_section(custom_instructions)}\\n{formatted_messages}")

def long_summary_prompt(formatted_messages, target_tokens, previous_summary_text, custom_instructions=None):
    return c(f"Long.\\n{custom_instructions_section(custom_instructions)}\\n{formatted_messages}")
'''
DIALECTIC = '''
_PAIR_TOOL_GROUPS = [("Memory", [("search_memory", "Search.")])]
_WORKSPACE_TOOL_GROUPS = [("Memory", [("search_memory", "Search.")])]
PAIR_PROMPT_TOOLS = frozenset(["search_memory"])
WORKSPACE_PROMPT_TOOLS = frozenset(["search_memory"])

def _available_tool_names(available_tools, default):
    return default if available_tools is None else frozenset(available_tools)

def _render_tool_groups(available, groups):
    return "\\n".join(f"- {n}" for _, items in groups for n, _d in items if n in available)

def _custom_instructions_suffix(custom_instructions):
    section = custom_instructions_section(custom_instructions)
    return f"\\n{section}\\n" if section else ""

def agent_system_prompt(observer, observed, observer_peer_card, observed_peer_card, available_tools=None, custom_instructions=None):
    tools = _render_tool_groups(_available_tool_names(available_tools, PAIR_PROMPT_TOOLS), _PAIR_TOOL_GROUPS)
    return f"Pair {observed}.\\n{tools}{_custom_instructions_suffix(custom_instructions)}"

def workspace_agent_system_prompt(available_tools=None, custom_instructions=None):
    return f"Workspace.{_custom_instructions_suffix(custom_instructions)}"
'''
MEMORY_BRIEF = '''
_LEVELS_TEXT = "Levels."
_IDS_TEXT = "Ids."
_TOOL_COVERAGE = (("search_memory", "conclusions"),)

def memory_layout_section(*, available_tools: Iterable[str], cite_ids: bool) -> str:
    parts = ["## MEMORY", _LEVELS_TEXT] + ([_IDS_TEXT] if cite_ids else [])
    return "\\n\\n".join(parts + [n for n, _ in _TOOL_COVERAGE if n in set(available_tools)])
'''
WORKING_MEMORY = '''
PENDING_TTL_DREAMS = 3
COVERED_TTL_DREAMS = 5

def notes_system_section(max_tokens: int) -> str:
    return f"## NOTES {PENDING_TTL_DREAMS}/{COVERED_TTL_DREAMS} cap {max_tokens}"
'''
SPECIALISTS = '''
SEARCH_TOOL_NAMES = frozenset({"search_memory"})

def budget_system_section(limit: int, window: int) -> str:
    return f"## BUDGET {limit} calls, last {window} write"

PEER_CARD_SYSTEM_SECTION = "## PEER CARD"

def custom_instructions_prompt(configuration: ResolvedConfiguration | None, *, peer_card_enabled: bool) -> str:
    sections = [custom_instructions_section(configuration.dream.custom_instructions)]
    if peer_card_enabled:
        sections.append(custom_instructions_section(configuration.peer_card.custom_instructions, heading="PEER CARD CUSTOM INSTRUCTIONS:"))
    return "".join(f"\\n\\n{section}" for section in sections if section)

class DeductionSpecialist(BaseSpecialist):
    def build_system_prompt(self, observed: str, *, peer_card_enabled: bool = True) -> str:
        return f"Deduce.{PEER_CARD_SYSTEM_SECTION if peer_card_enabled else ''}"

class InductionSpecialist(BaseSpecialist):
    def build_system_prompt(self, observed: str, *, peer_card_enabled: bool = True) -> str:
        return "Induce."

class CardRefreshSpecialist(BaseSpecialist):
    def build_system_prompt(self, observed: str, *, peer_card_enabled: bool = True) -> str:
        return f"Refresh.{' REBUILD' if self.rebuild else ''}{PEER_CARD_SYSTEM_SECTION}"
'''


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_honcho_prompts", ROOT / "src" / "config" / "gen_honcho_prompts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    for rel, text in {"utils/formatting.py": FORMATTING, "deriver/prompts.py": DERIVER,
                      "utils/summarizer.py": SUMMARIZER, "dialectic/prompts.py": DIALECTIC,
                      "utils/memory_brief.py": MEMORY_BRIEF, "dreamer/working_memory.py": WORKING_MEMORY,
                      "dreamer/specialists.py": SPECIALISTS}.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("import os\n" + text, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n', encoding="utf-8")
    return src


def test_every_prompt_has_the_slot_in_place_of_the_custom_text(tmp_path):
    jobs = _load_generator().build(_fake_source(tmp_path))
    assert sorted(jobs) == ["dialectic", "reasoning", "summary"]
    intake = jobs["reasoning"][0]
    assert "CUSTOM INSTRUCTIONS:\nFor the target peer.\n{CUSTOM}" in intake["with"]
    assert "CUSTOM INSTRUCTIONS" not in intake["without"]
    assert "Target peer: {peer}" in intake["without"]
    for items in jobs.values():
        for item in items:
            assert item["with"].count("{CUSTOM}") == 1


def test_dream_parts_have_slots_and_placeholders(tmp_path):
    gen = _load_generator()
    dream = gen.build_dream(gen.load(_fake_source(tmp_path)))
    names = [s["base"] for s in dream["specialists"]]
    assert names == ["Deduce.## PEER CARD", "Induce.", "Refresh.## PEER CARD"]
    assert [(s["peer_card"], s["extras"]) for s in dream["specialists"]] == [(True, True), (False, True), (True, False)]
    assert dream["sections"]["budget"] == "## BUDGET {tool calls} calls, last {write rounds} write"
    assert dream["sections"]["notes"] == "## NOTES 3/5 cap {notes cap}"
    assert dream["sections"]["brief"].startswith("## MEMORY")
    assert dream["custom"] == {"dream": "\n\nCUSTOM INSTRUCTIONS:\n{CUSTOM}",
                               "peer_card": "\n\nPEER CARD CUSTOM INSTRUCTIONS:\n{CUSTOM}"}


def test_main_writes_the_version_from_pyproject(tmp_path, monkeypatch):
    gen = _load_generator()
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(gen, "__file__", str(out / "gen_honcho_prompts.py"))
    assert gen.main(["gen", str(_fake_source(tmp_path))]) == 0
    data = json.loads((out / "honcho-prompts.json").read_text(encoding="utf-8"))
    assert data["honcho_version"] == "9.9.9" and data["slot"] == "{CUSTOM}"
    assert len(data["dream"]["specialists"]) == 3


def test_missing_function_is_reported(tmp_path):
    src = _fake_source(tmp_path)
    (src / "utils" / "summarizer.py").write_text("x = 1\n", encoding="utf-8")
    try:
        _load_generator().build(src)
    except SystemExit as exc:
        assert "short_summary_prompt" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
