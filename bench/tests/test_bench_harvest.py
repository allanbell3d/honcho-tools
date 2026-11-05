"""Harvest from a tiny fictional telemetry stream, the sample draw and the slice partition."""
import json

import pytest

import harvest
import slices


def ev(type_, data, time="2026-01-01T00:00:00Z", id_=None):
    return dict(type=type_, time=time, id=id_, data=data, _source=dict(file="events-1.jsonl", line=1, event_id=id_))


def dream_stream(run="run-1", query="what does peer-A like", top_k=10):
    return [
        ev("dream.specialist", dict(run_id=run, observer="peer-A", observed="peer-A")),
        ev("llm.call.traced", dict(run_id=run, workspace_name="workspace-1", parent_category="dream", agent_type="deduction",
                                   output_tool_calls=[dict(name="search_memory", id="t1", input=dict(query=query, top_k=top_k))])),
        ev("agent.tool.call.completed", dict(run_id=run, provider_tool_call_id="t1", parent_category="dream",
                                             agent_type="deduction", top_k=top_k, results_count=7)),
    ]


def test_dream_search_becomes_a_replayable_call_without_scope_approximation():
    calls, omissions, raw = harvest.harvest(dream_stream())
    (call,) = calls
    assert call["caller_type"] == "dream.deduction" and call["query"] == "what does peer-A like"
    assert (call["observer"], call["observed"], call["top_k"], call["results_count"]) == ("peer-A", "peer-A", 10, 7)
    assert call["replay_eligible"] and not call["scope_approximated"] and call["run_id"] == "run-1"
    assert omissions["session_context_search_query_unrecorded"] == 0 and len(raw) == 1


def test_identical_searches_merge_and_are_counted():
    calls, _, raw = harvest.harvest(dream_stream("r1") + dream_stream("r2"))
    assert len(raw) == 2 and len(calls) == 1 and calls[0]["occurrences"] == 2


def test_failed_or_unmatched_searches_are_dropped_and_counted():
    stream = dream_stream()
    stream[2]["data"]["is_error"] = True
    calls, omissions, _ = harvest.harvest(stream)
    assert calls == [] and omissions["search_memory_error"] == 1
    calls, omissions, _ = harvest.harvest(dream_stream()[:2])
    assert omissions["search_memory_missing_completion"] == 1


def test_dialectic_calls_are_flagged_as_scope_approximated():
    stream = [
        ev("llm.call.traced", dict(run_id="d1", workspace_name="workspace-1", parent_category="dialectic", observer="peer-A",
                                   observed="peer-A", output_tool_calls=[dict(name="search_memory", id="t9", input=dict(query="q"))])),
        ev("agent.tool.call.completed", dict(run_id="d1", provider_tool_call_id="t9", parent_category="dialectic",
                                             top_k=25, results_count=3)),
    ]
    (call,) = harvest.harvest(stream)[0]
    assert call["caller_type"] == "dialectic" and call["scope_approximated"] and call["session_allowlist"] is None


def test_prefetch_is_rebuilt_as_two_level_searches_from_the_traced_user_message():
    text = "Query: where does peer-A hike" + harvest.PREFETCH_MARKER + "- notes"
    stream = [
        ev("embedding.call.traced", dict(run_id="d1", call_purpose="dialectic_prefetch")),
        ev("trace.content", dict(content_hash="h1", role="user", content=text)),
        ev("llm.call.traced", dict(run_id="d1", workspace_name="workspace-1", parent_category="dialectic", observer="peer-A",
                                   observed="peer-A", input_message_refs=["h1"], output_tool_calls=[])),
        ev("dialectic.completed", dict(run_id="d1", reasoning_level="minimal")),
    ]
    calls, omissions, _ = harvest.harvest(stream)
    assert [c["caller_type"] for c in calls] == ["dialectic_prefetch"] * 2
    assert {tuple(c["filters"]["level"]["in"]) for c in calls} == {("explicit",), ("deductive", "inductive", "contradiction")}
    assert all(c["top_k"] == 10 and c["query"] == "where does peer-A hike" for c in calls)
    stream[3]["data"]["reasoning_level"] = None
    assert harvest.harvest(stream)[1]["dialectic_prefetch_limit_unknown"] == 1


def test_session_context_searches_are_counted_but_never_invented():
    stream = [ev("embedding.call.traced", dict(call_purpose="session_context_search")) for _ in range(3)]
    calls, omissions, _ = harvest.harvest(stream)
    assert calls == [] and omissions["session_context_search_query_unrecorded"] == 3


def many_calls(n_per_pair=20):
    rows = []
    for caller in ("dream.deduction", "dream.induction"):
        for pair in ("a", "b", "c"):
            for i in range(n_per_pair):
                rows.append(dict(call_id=f"{caller}-{pair}-{i}", caller_type=caller, workspace_name="w", observer=pair,
                                 observed=pair, replay_eligible=True))
    rows += [dict(call_id=f"dia-{i}", caller_type="dialectic", workspace_name="w", observer="a", observed="a", replay_eligible=True)
             for i in range(4)]
    rows.append(dict(call_id="bad", caller_type="dream.deduction", workspace_name="w", observer="a", observed="a", replay_eligible=False))
    return rows


def test_sample_keeps_every_dialectic_call_and_deals_dreams_round_robin():
    sample = harvest.sample_calls(many_calls(), size=12, seed=1)
    assert sum(r["caller_type"] == "dialectic" for r in sample) == 4
    dreams = [r for r in sample if r["caller_type"].startswith("dream")]
    assert len(dreams) == 12 and "bad" not in {r["call_id"] for r in sample}
    assert {(r["caller_type"], r["observer"]) for r in dreams} == {(c, p) for c in ("dream.deduction", "dream.induction") for p in "abc"} or len(
        {(r["caller_type"], r["observer"]) for r in dreams}) == 6
    assert sample == harvest.sample_calls(many_calls(), size=12, seed=1)  # fixed seed, fixed sample


def test_read_events_snapshots_files_and_counts_bad_lines(tmp_path):
    (tmp_path / "events-2026-01-01.jsonl").write_text(
        json.dumps(dict(received_at="x", event=dict(type="t", data={}, time="t"))) + "\nnot json\n")
    events, snaps, bad = harvest.read_events(tmp_path)
    assert len(events) == 1 and bad == 1 and snaps[0]["file"] == "events-2026-01-01.jsonl" and len(snaps[0]["sha256"]) == 64


# ------------------------------------------------------------------------------------------ slices
def test_slices_partition_the_sample_with_balanced_caller_mix():
    calls = many_calls()[:-1]
    manifest = slices.build_slices(calls, seed=3, count=5)
    ids = [i for s in manifest["slices"] for i in s["call_ids"]]
    assert sorted(ids) == sorted(c["call_id"] for c in calls)
    sizes = [len(s["call_ids"]) for s in manifest["slices"]]
    assert max(sizes) - min(sizes) <= 2
    assert slices.build_slices(calls, seed=3, count=5) == manifest


def test_filter_calls_selects_slices_and_refuses_a_stale_manifest():
    calls = many_calls()[:-1]
    manifest = slices.build_slices(calls, seed=3, count=5)
    chosen = slices.filter_calls(calls, [1, 2], manifest)
    assert {c["call_id"] for c in chosen} == set(manifest["slices"][0]["call_ids"]) | set(manifest["slices"][1]["call_ids"])
    with pytest.raises(ValueError):
        slices.filter_calls(calls[:-1], [1], manifest)  # the sample changed after the manifest was made
    with pytest.raises(ValueError):
        slices.filter_calls(calls, [99], manifest)
    with pytest.raises(ValueError):
        slices.build_slices(calls + calls[:1])
