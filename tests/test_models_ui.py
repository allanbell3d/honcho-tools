"""Behavioral checks of stage routing edits without a browser or provider."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_peer_model_display_matches_stage_and_override_precedence():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    functions = source[source.index("function dreamModelDisplay("):source.index("function modelButton(")]
    checks = r"""
const assert = require('node:assert/strict');
const S = {settings: {providers: {active: 'anthropic', backends: {anthropic: {model: 'haiku'}, deepseek: {model: 'flash'}}, stages: {'dream.deduction': 'deepseek:flash', 'dream.induction': 'deepseek:flash', peer_card: 'deepseek:flash'}}, defaults: {backend: null}, peers: {A: {}, B: {}}, pairs: {'A/B': {}}}};
const shortLabel = (ref) => ref;
const activeRef = (settings) => settings.providers.active + ':' + settings.providers.backends[settings.providers.active].model;
assert.equal(dreamModelDisplay('peers', 'A').label, 'deepseek:flash · stages');
assert.equal(dreamModelDisplay('defaults', '').label, 'deepseek:flash · stages');
S.settings.defaults.backend = 'anthropic';
assert.equal(dreamModelDisplay('peers', 'A').label, 'anthropic:haiku · default');
S.settings.peers.B.backend = 'deepseek:other';
assert.equal(dreamModelDisplay('pairs', 'A/B').label, 'deepseek:other · B');
S.settings.peers.A.backend = 'deepseek:first';
assert.equal(dreamModelDisplay('pairs', 'A/B').label, 'deepseek:first · A');
S.settings.pairs['A/B'].backend = 'deepseek:pair';
assert.equal(dreamModelDisplay('pairs', 'A/B').label, 'deepseek:pair · A/B');
S.settings.defaults.backend = null;
S.settings.peers.A.backend = null;
S.settings.providers.stages['dream.induction'] = 'deepseek:different';
assert.equal(dreamModelDisplay('peers', 'A').label, 'Per-stage models');
assert.match(dreamModelDisplay('peers', 'A').title, /dream.induction: deepseek:different/);
S.settings.providers.stages['dream.deduction'] = { model: 'deepseek:flash', effort: 'high' };
S.settings.providers.stages['dream.induction'] = { model: 'deepseek:flash', effort: 'low' };
S.settings.providers.stages.peer_card = { model: 'deepseek:flash', effort: 'medium' };
assert.equal(dreamModelDisplay('peers', 'A').label, 'deepseek:flash · stages');
S.settings.providers.stages = {};
assert.equal(dreamModelDisplay('peers', 'A').label, 'anthropic:haiku · fallback');
"""
    result = subprocess.run([node, "-e", functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_utc_times_ignore_the_browser_zone():
    # The Runs page said UTC but printed browser-local time, four hours off in a UTC+4 browser.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    start = source.index("function whenUtc(")
    functions = source[start:source.index("\n}\n", start) + 3]
    checks = r"""
const assert = require('node:assert/strict');
assert.equal(whenUtc('2026-11-04T15:04:23Z'), '2026-11-04 15:04:23');
assert.equal(whenUtc(''), '—');
assert.equal(whenUtc('not a time'), 'not a time');
"""
    for zone in ("Asia/Tokyo", "America/New_York", "UTC"):
        result = subprocess.run([node, "-e", functions + checks], capture_output=True, text=True, timeout=10,
                                env={**os.environ, "TZ": zone})
        assert result.returncode == 0, zone + ": " + result.stderr
    assert "whenUtc(row.started)" in source


def test_stage_setup_merges_params_and_later_provider_edits_are_hot():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    ensure = source[source.index("function ensureListed("):source.index("function activeRef(")]
    setup = r"""
const assert = require('node:assert/strict');
const S = {settings: {providers: {backends: {codex: {kind: 'cli-codex'}}, stages: {}}}, hEdits: {}};
let base = {'deriver.model_config.transport': 'anthropic', 'deriver.model_config.overrides.provider_params': {custom: true, extra_body: {keep: 'yes'}}};
const clone = (value) => JSON.parse(JSON.stringify(value));
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { if (JSON.stringify(base[path]) !== JSON.stringify(value)) S.hEdits[path] = value; };
const bridgeRoute = () => ({base: 'http://bridge.example/v1', key: 'BRIDGE_KEY'});
"""
    checks = r"""
writeJob('deriver.model_config', 'bridge:codex:one', 'deriver');
assert.deepEqual(S.hEdits, {});
assert.equal(S.settings.providers.stages.deriver, 'codex:one');
assert.equal(S.settings.providers.backends.codex.models, undefined);
assert.equal(base['deriver.model_config.overrides.provider_params'].extra_body.keep, 'yes');
writeJob('deriver.model_config', 'bridge:codex:two', 'deriver');
assert.deepEqual(S.hEdits, {});
assert.equal(S.settings.providers.stages.deriver, 'codex:two');
"""
    result = subprocess.run([node, "-e", setup + ensure + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_direct_and_blank_effort_edits_leave_a_config_honcho_starts_with():
    # Findings from the end-to-end routing run: a direct pick on a dream row kept structured_output_mode (Honcho refused
    # to start), dropped thinking_effort, left the bridge stage behind; blank effort left the other effort shape set.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    ensure = source[source.index("function ensureListed("):source.index("function activeRef(")]
    setup = r"""
const assert = require('node:assert/strict');
const P = 'dream.deduction_model_config';
const S = {settings: {providers: {backends: {deepseek: {kind: 'openai-compat'}}, stages: {'dream.deduction': 'deepseek:flash', peer_card: 'deepseek:flash', summary: 'x:y'}}}, hEdits: {}};
let base = {};
base[P + '.transport'] = 'openai'; base[P + '.model'] = 'stage-route'; base[P + '.thinking_effort'] = 'medium';
base[P + '.structured_output_mode'] = 'json_object';
base[P + '.overrides.base_url'] = 'http://bridge.example/v1'; base[P + '.overrides.api_key_env'] = 'K';
base[P + '.overrides.provider_params'] = {extra_body: {backend: '@dream.deduction', output_config: {effort: 'low'}}};
const clone = (value) => JSON.parse(JSON.stringify(value));
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { if (JSON.stringify(base[path]) !== JSON.stringify(value)) S.hEdits[path] = value; else delete S.hEdits[path]; };
const bridgeRoute = () => ({base: 'http://bridge.example/v1', key: 'K'});
"""
    checks = r"""
writeJob(P, 'direct:claude-haiku-5-5', 'dream.deduction');
assert.deepEqual(S.hEdits, {});
assert.equal(hValue(P + '.thinking_effort'), 'medium');
assert.equal(hValue(P + '.structured_output_mode'), 'json_object');
assert.equal(S.settings.providers.stages['dream.deduction'], 'deepseek:flash');
assert.equal(S.settings.providers.stages.peer_card, 'deepseek:flash');
assert.equal(S.settings.providers.stages.summary, 'x:y');
writeStageEffort('dream.deduction', 'high');
assert.deepEqual(S.settings.providers.stages['dream.deduction'], {model: 'deepseek:flash', effort: 'high'});
assert.equal(hValue(P + '.thinking_effort'), 'medium');
writeStageEffort('dream.deduction', '');
assert.equal(S.settings.providers.stages['dream.deduction'], 'deepseek:flash');
assert.deepEqual(S.hEdits, {});
"""
    result = subprocess.run([node, "-e", setup + ensure + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_routed_model_and_effort_picks_write_only_settings():
    # A routed row's model and effort stay in settings.json. Neither stages a config.toml edit.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    assert "not routed — use Route all jobs through the bridge" in source
    assert "Anthropic (direct)" not in source
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    ensure = source[source.index("function ensureListed("):source.index("function activeRef(")]
    setup = r"""
const assert = require('node:assert/strict');
const S = {settings: {providers: {backends: {codex: {kind: 'cli-codex'}}, stages: {summary: 'codex:one'}}}, hEdits: {}};
let base = {'summary.model_config.transport': 'openai', 'summary.model_config.model': 'stage-route', 'summary.model_config.thinking_effort': 'low', 'summary.model_config.overrides.provider_params': {extra_body: {backend: '@summary', keep: 'yes'}}};
const clone = (value) => JSON.parse(JSON.stringify(value));
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { throw new Error('config.toml write ' + path); };
const api = (path) => { throw new Error('unexpected ' + path); };
"""
    checks = r"""
assert.equal(stageHot('summary.model_config', 'summary'), true);
writeJob('summary.model_config', 'bridge:codex:two', 'summary');
assert.equal(S.settings.providers.stages.summary, 'codex:two');
assert.deepEqual(S.hEdits, {});
assert.equal(base['summary.model_config.thinking_effort'], 'low');
writeStageEffort('summary', 'high');
assert.deepEqual(S.settings.providers.stages.summary, {model: 'codex:two', effort: 'high'});
assert.deepEqual(S.hEdits, {});
writeJob('summary.model_config', 'bridge:codex:three', 'summary');
assert.deepEqual(S.settings.providers.stages.summary, {model: 'codex:three', effort: 'high'});
writeStageEffort('summary', '');
assert.equal(S.settings.providers.stages.summary, 'codex:three');
assert.deepEqual(S.hEdits, {});
assert.equal(Object.keys(base).length, 4);
"""
    result = subprocess.run([node, "-e", setup + ensure + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_fallback_edit_writes_only_that_blocks_fallback():
    # A Honcho config row edits only that block's fallback. The plain-words column names the model and effort.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    assert '["honcho", "Honcho config"]' in source
    assert "Peer card refresh shares this fallback." in source
    assert "No fallback: if the bridge is down this job fails" in source
    tasks = source[source.index("const TASKS = "):source.index("function jobOptions(")]
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    setup = r"""
const assert = require('node:assert/strict');
const LEVELS = ["minimal", "low", "medium", "high", "max"];
const S = {settings: {providers: {stages: {}}}, catalogue: {tasks: {anthropic: [{model: "claude-haiku-5-5", label: "Claude Haiku 5.5"}], openai: [{model: "text-embedding-3-small", label: "OpenAI text-embedding-3-small"}]}}, hEdits: {}};
let base = {"deriver.model_config.model": "stage-route", "deriver.model_config.transport": "openai", "deriver.model_config.overrides.provider_params": {extra_body: {backend: "@deriver"}},
  "summary.model_config.model": "stage-route", "summary.model_config.transport": "openai", "summary.model_config.overrides.provider_params": {extra_body: {backend: "@summary"}}};
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { if (JSON.stringify(base[path]) === JSON.stringify(value)) delete S.hEdits[path]; else S.hEdits[path] = value; };
"""
    checks = r"""
const intake = jobBlocks().find((task) => task.stage === "deriver");
const summary = jobBlocks().find((task) => task.stage === "summary");
assert.equal(fallbackWords(summary), "No fallback: if the bridge is down this job fails");
writeFallbackModel(intake, "anthropic:claude-haiku-5-5");
writeFallbackEffort(intake, "medium");
assert.deepEqual(Object.keys(S.hEdits).sort(), [
  "deriver.model_config.fallback.model",
  "deriver.model_config.fallback.thinking_effort",
  "deriver.model_config.fallback.transport",
].sort());
assert.equal(S.hEdits["deriver.model_config.fallback.model"], "claude-haiku-5-5");
assert.equal(S.hEdits["deriver.model_config.fallback.transport"], "anthropic");
assert.equal(S.hEdits["deriver.model_config.fallback.thinking_effort"], "medium");
assert.equal(hValue("deriver.model_config.model"), "stage-route");
assert.equal(hValue("deriver.model_config.transport"), "openai");
assert.equal(hValue("summary.model_config.model"), "stage-route");
assert.equal(fallbackWords(intake), "Bridge down → Intake uses Claude Haiku 5.5 (Honcho native), effort medium");
assert.equal(jobBlocks().some((task) => task.stage === "peer_card"), false);
"""
    result = subprocess.run([node, "-e", setup + tasks + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_route_all_copies_direct_deriver_into_fallback_through_the_summarys_bridge():
    # A direct deriver and an already routed summary: install copies the deriver into its fallback and routes it
    # through the summary's bridge; summary stays put.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    assert "Route all jobs through the bridge" in source
    assert "Undo routing" in source
    tasks = source[source.index("const TASKS = "):source.index("function jobOptions(")]
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    setup = r"""
const assert = require('node:assert/strict');
const LEVELS = ["minimal", "low", "medium", "high", "max"];
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const D = "deriver.model_config";
const SM = "summary.model_config";
const S = {settings: {providers: {active: "active-fallback", backends: {anthropic: {kind: "anthropic-api", model: "claude-haiku-5-5"}}, stages: {summary: "keep:summary"}}}, catalogue: {dream: [{backend: "anthropic", model: "claude-haiku-5-5", vendor: "Anthropic", label: "Claude Haiku 5.5"}]}, hEdits: {}};
let base = {};
base[D + ".transport"] = "anthropic";
base[D + ".model"] = "claude-haiku-5-5";
base[D + ".thinking_effort"] = "medium";
base[D + ".thinking_budget_tokens"] = null;
base[D + ".max_output_tokens"] = null;
base[D + ".structured_output_mode"] = null;
base[D + ".overrides.base_url"] = null;
base[D + ".overrides.api_key_env"] = null;
base[D + ".overrides.provider_params"] = {extra_body: {cache_control: {type: "ephemeral"}}};
base[SM + ".transport"] = "openai";
base[SM + ".model"] = "stage-route";
base[SM + ".overrides.base_url"] = "http://bridge.example/v1";
base[SM + ".overrides.api_key_env"] = "LIVE_BRIDGE_KEY";
base[SM + ".overrides.provider_params"] = {extra_body: {backend: "@summary"}};
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { if (JSON.stringify(base[path]) === JSON.stringify(value)) delete S.hEdits[path]; else S.hEdits[path] = value; };
function blockShape(path) {
  return {
    transport: hValue(path + ".transport"), model: hValue(path + ".model"),
    thinking_effort: hValue(path + ".thinking_effort"),
    thinking_budget_tokens: hValue(path + ".thinking_budget_tokens"),
    max_output_tokens: hValue(path + ".max_output_tokens"),
    structured_output_mode: hValue(path + ".structured_output_mode"),
    base_url: hValue(path + ".overrides.base_url"), api_key_env: hValue(path + ".overrides.api_key_env"),
    provider_params: hValue(path + ".overrides.provider_params"),
  };
}
function assertSummary() {
  assert.equal(hValue(SM + ".transport"), "openai");
  assert.equal(hValue(SM + ".model"), "stage-route");
  assert.equal(hValue(SM + ".overrides.base_url"), "http://bridge.example/v1");
  assert.equal(hValue(SM + ".overrides.api_key_env"), "LIVE_BRIDGE_KEY");
  assert.deepEqual(hValue(SM + ".overrides.provider_params"), {extra_body: {backend: "@summary"}});
  assert.equal(S.settings.providers.stages.summary, "keep:summary");
  assert.equal(readFallback(SM), null);
  assert.equal(Object.keys(S.hEdits).some((key) => key.startsWith(SM)), false);
}
"""
    checks = r"""
const deriver0 = clone(blockShape(D));
routeAll();
const fb = S.hEdits[D + ".fallback"];
assert.equal(fb.model, "claude-haiku-5-5");
assert.equal(fb.transport, "anthropic");
assert.equal(fb.thinking_effort, "medium");
assert.equal(fb.structured_output_mode, undefined);
assert.deepEqual(fb.overrides.provider_params, {extra_body: {cache_control: {type: "ephemeral"}}});
assert.equal(hValue(D + ".transport"), "openai");
assert.equal(hValue(D + ".model"), "stage-route");
assert.equal(hValue(D + ".overrides.base_url"), "http://bridge.example/v1");
assert.equal(hValue(D + ".overrides.api_key_env"), "LIVE_BRIDGE_KEY");
assert.equal(hValue(D + ".thinking_effort"), null);
const pp = hValue(D + ".overrides.provider_params");
assert.equal(pp.extra_body.backend, "@deriver");
assert.deepEqual(pp.extra_body.cache_control, {type: "ephemeral"});
assert.equal(Object.keys(S.hEdits).some((key) => key.startsWith(SM)), false);
assert.equal(Object.keys(S.hEdits).some((key) => key.startsWith("embedding.")), false);
assert.deepEqual(S.settings.providers.stages.deriver, {model: "anthropic:claude-haiku-5-5", effort: "medium"});
assert.equal(S.settings.providers.stages.peer_card, S.settings.providers.stages["dream.deduction"]);
assertSummary();
"""
    result = subprocess.run([node, "-e", setup + tasks + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_no_page_writes_the_embedding_model():
    # The embedding model is set once in config.toml at install. No page reads or writes embedding.model_config.
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    assert "embedding.model_config" not in source
    assert "Embedding" not in source
    assert "The embedding model is set once in config.toml at install" in (ROOT / "src/dashboard/README.md").read_text(encoding="utf-8")
    assert "The embedding model is set once in config.toml at install" in (ROOT / "docs/HONCHO-CONFIG.md").read_text(encoding="utf-8")


def test_summary_headroom_is_edited_on_the_honcho_config_page_only():
    # The Honcho config page is the one place that writes config.toml. The Rerank page keeps settings.json only.
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    rerank_page = source[source.index("function pageRerank("):source.index("async function refreshRuns(")]
    honcho_page = source[source.index("function pageHoncho("):source.index("function addModelBox(")]
    assert "output_headroom" not in rerank_page and "honchoLabel(" not in rerank_page and "hSet(" not in rerank_page
    assert "Honcho summary setting (config.toml)" in honcho_page
    assert "HONCHO_SEARCH.map(([p, label]) => honchoLabel(p, label))" in honcho_page
    assert 'const HONCHO_SEARCH = [["summary.output_headroom", "Summary output headroom"]];' in source


def test_fallback_provider_change_drops_fields_the_new_transport_refuses():
    # An openai fallback carries structured_output_mode and its own overrides. Switched to an Anthropic model,
    # neither may stay: Honcho 3.5.0 refuses structured_output_mode on any transport but openai, and base_url,
    # api_key_env and provider_params belong to the old provider; the overrides table is removed whole. The same
    # provider keeps them.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    tasks = source[source.index("const TASKS = "):source.index("function jobOptions(")]
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    setup = r"""
const assert = require('node:assert/strict');
const LEVELS = ["minimal", "low", "medium", "high", "max"];
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const F = "summary.model_config.fallback";
const S = {settings: {providers: {stages: {}}}, catalogue: {tasks: {}}, hEdits: {}};
let base = {};
base[F + ".model"] = "gpt-5-mini"; base[F + ".transport"] = "openai"; base[F + ".thinking_effort"] = "low";
base[F + ".structured_output_mode"] = "json_object";
base[F + ".overrides.base_url"] = "http://provider.example/v1"; base[F + ".overrides.api_key_env"] = "OLD_KEY";
base[F + ".overrides.provider_params"] = {extra_body: {keep: "old"}};
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { if (JSON.stringify(base[path]) === JSON.stringify(value)) delete S.hEdits[path]; else S.hEdits[path] = value; };
"""
    checks = r"""
const summary = jobBlocks().find((task) => task.stage === "summary");
writeFallbackModel(summary, "openai:gpt-5");
assert.deepEqual(S.hEdits, {[F + ".model"]: "gpt-5"});
assert.equal(readFallback(summary.path).structured_output_mode, "json_object");
assert.equal(readFallback(summary.path).overrides.base_url, "http://provider.example/v1");
writeFallbackModel(summary, "anthropic:claude-haiku-5-5");
assert.deepEqual(S.hEdits, {
  [F + ".model"]: "claude-haiku-5-5", [F + ".transport"]: "anthropic",
  [F + ".structured_output_mode"]: null,
  [F + ".overrides.base_url"]: null, [F + ".overrides.api_key_env"]: null, [F + ".overrides.provider_params"]: null,
  [F + ".overrides"]: null,
});
// The overrides table goes last, so the write removes it whole instead of leaving it empty.
assert.equal(Object.keys(S.hEdits).indexOf(F + ".overrides") > Object.keys(S.hEdits).indexOf(F + ".overrides.provider_params"), true);
const fb = readFallback(summary.path);
assert.equal(fb.model, "claude-haiku-5-5");
assert.equal(fb.transport, "anthropic");
assert.equal(fb.thinking_effort, "low");
assert.equal("structured_output_mode" in fb, false);
assert.equal("overrides" in fb, false);
// A fallback staged by Route all in this session and not yet written is one object; a provider change edits it.
S.hEdits = {};
S.hEdits["deriver.model_config.fallback"] = {model: "gpt-5-mini", transport: "openai", structured_output_mode: "json_object", overrides: {base_url: "http://provider.example/v1"}};
const intake = jobBlocks().find((task) => task.stage === "deriver");
writeFallbackModel(intake, "anthropic:claude-haiku-5-5");
assert.deepEqual(S.hEdits, {"deriver.model_config.fallback": {model: "claude-haiku-5-5", transport: "anthropic"}});
"""
    result = subprocess.run([node, "-e", setup + tasks + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_route_all_keeps_an_existing_stage_entry():
    # A stage entry that already exists, as a string or an object, is kept by Route all; a missing one is set.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    tasks = source[source.index("const TASKS = "):source.index("function jobOptions(")]
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    setup = r"""
const assert = require('node:assert/strict');
const LEVELS = ["minimal", "low", "medium", "high", "max"];
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const S = {settings: {providers: {active: "active-fallback", backends: {anthropic: {kind: "anthropic-api", model: "claude-haiku-5-5"}}, stages: {deriver: "keep:string", summary: {model: "keep:object", effort: "high"}}}}, catalogue: {dream: [{backend: "anthropic", model: "claude-haiku-5-5", vendor: "Anthropic", label: "Claude Haiku 5.5"}]}, hEdits: {}};
let base = {};
for (const path of ["deriver.model_config", "summary.model_config", "dream.deduction_model_config"]) {
  base[path + ".transport"] = "anthropic"; base[path + ".model"] = "claude-haiku-5-5";
}
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { if (JSON.stringify(base[path]) === JSON.stringify(value)) delete S.hEdits[path]; else S.hEdits[path] = value; };
"""
    checks = r"""
routeAll();
assert.equal(hValue("deriver.model_config.model"), "stage-route");
assert.equal(hValue("summary.model_config.model"), "stage-route");
assert.equal(S.settings.providers.stages.deriver, "keep:string");
assert.deepEqual(S.settings.providers.stages.summary, {model: "keep:object", effort: "high"});
assert.equal(S.settings.providers.stages["dream.deduction"], "anthropic:claude-haiku-5-5");
"""
    result = subprocess.run([node, "-e", setup + tasks + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_plain_words_name_a_job_that_is_not_routed():
    # The "If the bridge is down" column: a block that is not routed says Honcho calls the model itself; a routed
    # block without a fallback says the job fails; a routed block with one names the model.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    tasks = source[source.index("const TASKS = "):source.index("function jobOptions(")]
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    setup = r"""
const assert = require('node:assert/strict');
const LEVELS = ["minimal", "low", "medium", "high", "max"];
const S = {settings: {providers: {stages: {}}}, catalogue: {tasks: {anthropic: [{model: "claude-haiku-5-5", label: "Claude Haiku 5.5"}]}}, hEdits: {}};
let base = {
  "deriver.model_config.model": "claude-haiku-5-5", "deriver.model_config.transport": "anthropic",
  "summary.model_config.model": "stage-route", "summary.model_config.transport": "openai", "summary.model_config.overrides.provider_params": {extra_body: {backend: "@summary"}},
  "dream.deduction_model_config.model": "stage-route", "dream.deduction_model_config.transport": "openai", "dream.deduction_model_config.overrides.provider_params": {extra_body: {backend: "@dream.deduction"}},
  "dream.deduction_model_config.fallback.model": "claude-haiku-5-5", "dream.deduction_model_config.fallback.transport": "anthropic",
};
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
"""
    checks = r"""
const block = (stage) => jobBlocks().find((task) => task.stage === stage);
assert.equal(fallbackWords(block("deriver")), "Not routed: Honcho calls this model itself (Honcho native)");
assert.equal(fallbackWords(block("summary")), "No fallback: if the bridge is down this job fails");
assert.equal(fallbackWords(block("dream.deduction")), "Bridge down → Dream deduction uses Claude Haiku 5.5 (Honcho native)");
"""
    result = subprocess.run([node, "-e", setup + tasks + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_read_job_has_no_honcho_native_branch():
    # After C2 a job row has a picker only when routed, so readJob never sees a Honcho-native block: the "direct:"
    # branch, the "<transport> <model>" string and the config.toml effort readers are gone. A routed block with no
    # stage entry reads as nothing picked.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    assert '"direct:"' not in source and "readEffort" not in source and "legacyEffort" not in source
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    setup = r"""
const assert = require('node:assert/strict');
const S = {settings: {providers: {backends: {}, stages: {summary: 'codex:one'}}}, hEdits: {}};
let base = {'summary.model_config.transport': 'openai', 'summary.model_config.model': 'stage-route', 'summary.model_config.overrides.provider_params': {extra_body: {backend: '@summary'}},
  'deriver.model_config.transport': 'openai', 'deriver.model_config.model': 'stage-route', 'deriver.model_config.overrides.provider_params': {extra_body: {backend: '@deriver'}},
  'dream.induction_model_config.transport': 'openai', 'dream.induction_model_config.model': 'old-alias', 'dream.induction_model_config.overrides.provider_params': {extra_body: {backend: 'codex:legacy'}}};
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
"""
    checks = r"""
assert.equal(readJob('summary.model_config', 'summary'), 'bridge:codex:one');
assert.equal(readJob('deriver.model_config', 'deriver'), '');
assert.equal(readJob('dream.induction_model_config', 'dream.induction'), 'bridge:codex:legacy');
"""
    result = subprocess.run([node, "-e", setup + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_route_all_keeps_a_fallback_the_file_already_has():
    # A job with its own fallback in config.toml: Route all routes the primary and leaves that fallback alone, and the
    # routing box names the job. A job without one gets its old primary as the fallback, as before.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    assert '"Kept your own fallback: " + S.keptFallback.join(", ")' in source
    tasks = source[source.index("const TASKS = "):source.index("function jobOptions(")]
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    setup = r"""
const assert = require('node:assert/strict');
const LEVELS = ["minimal", "low", "medium", "high", "max"];
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const D = "deriver.model_config", SM = "summary.model_config";
const S = {settings: {providers: {active: "active-fallback", backends: {anthropic: {kind: "anthropic-api", model: "claude-haiku-5-5"}}, stages: {}}}, catalogue: {dream: [{backend: "anthropic", model: "claude-haiku-5-5", vendor: "Anthropic", label: "Claude Haiku 5.5"}]}, hEdits: {}};
let base = {};
for (const path of [D, SM]) { base[path + ".transport"] = "anthropic"; base[path + ".model"] = "claude-haiku-5-5"; base[path + ".thinking_effort"] = "medium"; }
base[D + ".fallback.model"] = "gpt-6-luna"; base[D + ".fallback.transport"] = "openai"; base[D + ".fallback.thinking_effort"] = "high";
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { if (JSON.stringify(base[path]) === JSON.stringify(value)) delete S.hEdits[path]; else S.hEdits[path] = value; };
"""
    checks = r"""
routeAll();
assert.deepEqual(S.keptFallback, ["Intake"]);
assert.equal(Object.keys(S.hEdits).some((key) => key.startsWith(D + ".fallback")), false);
assert.deepEqual(readFallback(D), {model: "gpt-6-luna", transport: "openai", thinking_effort: "high"});
assert.equal(hValue(D + ".model"), "stage-route");
assert.equal(hValue(D + ".transport"), "openai");
assert.deepEqual(S.settings.providers.stages.deriver, {model: "anthropic:claude-haiku-5-5", effort: "medium"});
assert.deepEqual(S.hEdits[SM + ".fallback"], {model: "claude-haiku-5-5", transport: "anthropic", thinking_effort: "medium"});
assert.equal(hValue(SM + ".model"), "stage-route");
"""
    result = subprocess.run([node, "-e", setup + tasks + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_route_all_clears_old_effort_on_a_routed_job():
    # A routed primary that still carries thinking_effort, or an older extra_body.output_config.effort, sends that
    # effort with every request, and the bridge lets the request's effort beat the per-job pick. Route all clears
    # both and leaves the job's fallback alone.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    tasks = source[source.index("const TASKS = "):source.index("function jobOptions(")]
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    setup = r"""
const assert = require('node:assert/strict');
const LEVELS = ["minimal", "low", "medium", "high", "max"];
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const SM = "summary.model_config";
const S = {settings: {providers: {active: "a", backends: {}, stages: {summary: "x:y"}}}, catalogue: {dream: []}, hEdits: {}};
let base = {};
base[SM + ".transport"] = "openai"; base[SM + ".model"] = "stage-route"; base[SM + ".thinking_effort"] = "low";
base[SM + ".overrides.base_url"] = "http://bridge.example/v1"; base[SM + ".overrides.api_key_env"] = "BRIDGE_KEY";
base[SM + ".overrides.provider_params"] = {extra_body: {backend: "@summary", output_config: {effort: "high"}}, keep: true};
base[SM + ".fallback.model"] = "claude-haiku-5-5"; base[SM + ".fallback.transport"] = "anthropic"; base[SM + ".fallback.thinking_effort"] = "medium";
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { if (JSON.stringify(base[path]) === JSON.stringify(value)) delete S.hEdits[path]; else S.hEdits[path] = value; };
"""
    checks = r"""
// The other blocks are empty here and get routed; only the summary's edits are under test.
const mine = () => Object.fromEntries(Object.entries(S.hEdits).filter(([key]) => key.startsWith(SM + ".")));
routeAll();
assert.deepEqual(mine(), {
  [SM + ".thinking_effort"]: null,
  [SM + ".overrides.provider_params"]: {extra_body: {backend: "@summary"}, keep: true},
});
assert.deepEqual(readFallback(SM), {model: "claude-haiku-5-5", transport: "anthropic", thinking_effort: "medium"});
assert.equal(S.settings.providers.stages.summary, "x:y");
// Run again on a file that is already clean: nothing to edit on the summary.
base[SM + ".thinking_effort"] = null; base[SM + ".overrides.provider_params"] = {extra_body: {backend: "@summary"}}; S.hEdits = {};
routeAll();
assert.deepEqual(mine(), {});
"""
    result = subprocess.run([node, "-e", setup + tasks + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_route_all_moves_an_old_output_config_effort_into_the_fallback_only():
    # A job routed for the first time may still carry the extra_body.output_config.effort older pages wrote. The
    # routed primary must not send it (the bridge would let it beat the per-job pick); the fallback keeps it. An effort
    # picked for that staged fallback is shown and written as one value.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    tasks = source[source.index("const TASKS = "):source.index("function jobOptions(")]
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    setup = r"""
const assert = require('node:assert/strict');
const LEVELS = ["minimal", "low", "medium", "high", "max"];
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const DM = "deriver.model_config";
const S = {settings: {providers: {active: "a", backends: {}, stages: {}}}, catalogue: {dream: []}, hEdits: {}};
let base = {};
base[DM + ".transport"] = "openai"; base[DM + ".model"] = "gpt-5"; base[DM + ".thinking_effort"] = "low";
base[DM + ".overrides.provider_params"] = {extra_body: {output_config: {effort: "high"}}};
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { if (JSON.stringify(base[path]) === JSON.stringify(value)) delete S.hEdits[path]; else S.hEdits[path] = value; };
"""
    checks = r"""
routeAll();
assert.deepEqual(S.hEdits[DM + ".overrides.provider_params"], {extra_body: {backend: "@deriver"}});
assert.equal(S.hEdits[DM + ".thinking_effort"], null);
const fb = S.hEdits[DM + ".fallback"];
assert.equal(fb.thinking_effort, "low");
assert.deepEqual(fb.overrides.provider_params.extra_body.output_config, {effort: "high"});
const task = TASKS.find((t) => t.path === DM);
writeFallbackEffort(task, "max");
assert.equal(readFallback(DM).thinking_effort, "max");
assert.ok(!(DM + ".fallback.thinking_effort" in S.hEdits));
writeFallbackEffort(task, "");
assert.ok(!("thinking_effort" in readFallback(DM)));
"""
    result = subprocess.run([node, "-e", setup + tasks + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_a_write_that_stops_midway_can_be_finished():
    # Each file written leaves the waiting edits at once: after settings.json is written and config.toml fails, a
    # second Write skips settings.json (no stale mtime, no 409) and still carries the config.toml edits.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    walk = source[source.index("function settingsChanges() {"):source.index("\n}\n", source.index("function settingsChanges() {")) + 3]
    write_all = source[source.index("async function writeAll() {"):source.index("\n}\n", source.index("async function writeAll() {")) + 3]
    setup = r"""
const assert = require('node:assert/strict');
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
global.document = { getElementById: () => null };
const closeModal = () => {}, loadAll = async () => {}, setPath = (o, p, v) => { o[p] = v; };
let noted = "";
const note = (text) => { noted = text; };
const S = {settings0: {mode: "stock"}, settings: {mode: "scheduler"}, mtime: "1", instr: {}, ws: {instructions: {}},
  hEdits: {"summary.model_config.model": "stage-route"}, honcho: {mtime_ns: "9"}, routeAllStaged: true};
const calls = [];
let failConfig = true;
const api = async (path, body) => {
  calls.push([path, body.mtime_ns]);
  if (path === "/api/settings") { if (body.mtime_ns !== "1") throw new Error("409"); return {mtime_ns: "2"}; }
  if (path === "/api/honcho-config/write" && failConfig) throw new Error("write failed");
  return {};
};
"""
    checks = r"""
(async () => {
  await writeAll();
  assert.match(noted, /Stopped after dream settings: write failed/);
  assert.equal(S.mtime, "2");
  assert.deepEqual(settingsChanges(), []);
  assert.ok("summary.model_config.model" in S.hEdits);
  failConfig = false; calls.length = 0;
  await writeAll();
  assert.deepEqual(calls.map((c) => c[0]), ["/api/honcho-config/write"]);
  assert.match(noted, /Written: Honcho config/);
})().catch((e) => { console.error(e); process.exit(1); });
"""
    result = subprocess.run([node, "-e", setup + walk + write_all + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_route_all_sets_the_peer_card_stage_from_dream_deduction():
    # The peer card refresh shares dream deduction's block. Route all copies the dream.deduction entry to peer_card
    # when peer_card is unset; an existing peer_card entry is never changed.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    tasks = source[source.index("const TASKS = "):source.index("function jobOptions(")]
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    setup = r"""
const assert = require('node:assert/strict');
const LEVELS = ["minimal", "low", "medium", "high", "max"];
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const DD = "dream.deduction_model_config";
const S = {settings: {providers: {active: "active-fallback", backends: {anthropic: {kind: "anthropic-api", model: "claude-haiku-5-5"}}, stages: {}}}, catalogue: {dream: [{backend: "anthropic", model: "claude-haiku-5-5", vendor: "Anthropic", label: "Claude Haiku 5.5"}]}, hEdits: {}};
let base = {};
base[DD + ".transport"] = "anthropic"; base[DD + ".model"] = "claude-haiku-5-5";
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { if (JSON.stringify(base[path]) === JSON.stringify(value)) delete S.hEdits[path]; else S.hEdits[path] = value; };
"""
    checks = r"""
routeAll();
assert.equal(S.settings.providers.stages["dream.deduction"], "anthropic:claude-haiku-5-5");
assert.equal(S.settings.providers.stages.peer_card, "anthropic:claude-haiku-5-5");
S.settings.providers.stages = {"dream.deduction": {model: "codex:one", effort: "high"}, peer_card: "keep:card"};
S.hEdits = {};
routeAll();
assert.equal(S.settings.providers.stages.peer_card, "keep:card");
S.settings.providers.stages = {"dream.deduction": {model: "codex:one", effort: "high"}};
routeAll();
assert.deepEqual(S.settings.providers.stages.peer_card, {model: "codex:one", effort: "high"});
assert.notEqual(S.settings.providers.stages.peer_card, S.settings.providers.stages["dream.deduction"]);
"""
    result = subprocess.run([node, "-e", setup + tasks + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_undo_routing_is_the_copy_from_before_route_all():
    # The field-by-field undo is gone. Route all marks the Write that carries its edits, so the server keeps a copy of
    # config.toml from before it; two buttons put that copy back and are disabled without one.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    assert "function restorePrimary(" not in source and "function clearFallback(" not in source and "routeAll(false)" not in source
    assert '"Undo routing (jobs only)"' in source and '"Restore config.toml from before routing"' in source
    assert 'undoRouting("jobs")' in source and 'undoRouting("file")' in source
    assert 'api("/api/honcho-config/undo-routing", { scope, mtime_ns: S.honcho.mtime_ns })' in source
    assert 'await api("/api/honcho-config/write", { values, mtime_ns: S.honcho.mtime_ns, before_route_all: !!S.routeAllStaged })' in source
    tasks = source[source.index("const TASKS = "):source.index("function jobOptions(")]
    functions = source[source.index("function stageHot("):source.index("function pairSelects(")]
    setup = r"""
const assert = require('node:assert/strict');
const LEVELS = ["minimal", "low", "medium", "high", "max"];
const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
const S = {settings: {providers: {active: "a", backends: {}, stages: {}}}, catalogue: {dream: []}, hEdits: {}, routeAllStaged: false};
let base = {"deriver.model_config.transport": "anthropic", "deriver.model_config.model": "m"};
const hValue0 = (path) => base[path];
const hValue = (path) => path in S.hEdits ? S.hEdits[path] : base[path];
const hSet = (path, value) => { if (JSON.stringify(base[path]) === JSON.stringify(value)) delete S.hEdits[path]; else S.hEdits[path] = value; };
"""
    checks = r"""
routeAll();
assert.equal(S.routeAllStaged, true);
assert.equal(hValue("deriver.model_config.model"), "stage-route");
"""
    result = subprocess.run([node, "-e", setup + tasks + functions + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_routing_box_arms_undo_and_needs_settings_for_route_all():
    # Both undo choices arm first like every other destructive button; Route all is disabled while settings.json is
    # unusable (it writes providers.stages); the "every other change" warning names only the whole-file restore.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    helper = source[source.index("function h(tag, attrs, ...kids)"):source.index("\n}\n", source.index("function h(tag, attrs, ...kids)")) + 3]
    two_step = source[source.index("function twoStep("):source.index("\n}\n", source.index("function twoStep(")) + 3]
    box = source[source.index("function routingBox("):source.index("\n}\n", source.index("function routingBox(")) + 3]
    setup = r"""
const assert = require('node:assert/strict');
class Node { constructor(tag) { this.tag = tag; this.kids = []; this.dataset = {}; } append(...k) { this.kids.push(...k); } setAttribute(k, v) { this[k] = v === "" ? true : v; } addEventListener(e, f) { this["on" + e] = f; } }
global.Node = Node;
global.document = { createElement: (tag) => new Node(tag), createTextNode: (s) => Object.assign(new Node("#text"), {text: String(s)}) };
global.setTimeout = () => 0;
const when = (s) => "T(" + s + ")";
const calls = [];
const undoRouting = (scope) => calls.push(scope);
const routeAll = () => calls.push("route");
const render = () => {};
const S = {};
const text = (n) => n.textContent !== undefined ? n.textContent : n.text !== undefined ? n.text : (n.kids || []).map(text).join("");
const buttons = (b) => b.kids[1].kids[1].kids;
const button = (b, label) => buttons(b).find((x) => text(x) === label);
"""
    checks = r"""
S.settings = null; S.honcho = {};
let b = routingBox();
assert.ok(button(b, "Route all jobs through the bridge").disabled);
assert.ok(button(b, "Undo routing (jobs only)").disabled);
assert.ok(button(b, "Restore config.toml from before routing").disabled);
assert.match(text(b), /No copy from before routing/);
S.settings = {providers: {}}; S.honcho = {before_route_all_at: "2026-11-05T01:00:00+00:00"};
b = routingBox();
assert.ok(!button(b, "Route all jobs through the bridge").disabled);
const undo = button(b, "Undo routing (jobs only)");
undo.onclick(); assert.deepEqual(calls, []); assert.equal(text(undo), "Click again to undo");
undo.onclick(); assert.deepEqual(calls, ["jobs"]);
const restore = button(b, "Restore config.toml from before routing");
restore.onclick(); restore.onclick(); assert.deepEqual(calls, ["jobs", "file"]);
assert.match(text(b), /Undo routing puts back only the job tables.*Restore also undoes every other config\.toml change/);
"""
    result = subprocess.run([node, "-e", setup + helper + two_step + box + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_a_broken_settings_file_blanks_only_the_pages_that_need_it():
    # While the first load is in flight every page says Loading. Once loaded with settings.json unreadable, a page
    # that reads it shows one error box with the server's reason; Honcho config, Runs, Cost and Logs draw as usual.
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    assert 'S.settingsError = d.settings ? null : ((d.errors || []).join("; ") || "settings.json cannot be read")' in source
    assert "content = pageBody(S.page, page);" in source
    helper = source[source.index("function h(tag, attrs, ...kids)"):source.index("\n}\n", source.index("function h(tag, attrs, ...kids)")) + 3]
    body = source[source.index("const NEEDS_SETTINGS = "):source.index("function render() {")]
    setup = r"""
const assert = require('node:assert/strict');
class Node { constructor(tag) { this.tag = tag; this.kids = []; } append(...k) { this.kids.push(...k); } setAttribute() {} addEventListener() {} }
global.Node = Node;
global.document = { createElement: (tag) => new Node(tag), createTextNode: (s) => Object.assign(new Node("#text"), {text: String(s)}) };
const S = {loaded: false, settings: null, settingsError: null};
const drawn = () => ["drawn"];
"""
    checks = r"""
assert.equal(pageBody("models", drawn)[0].kids[0].text, "Loading…");
assert.equal(pageBody("logs", drawn)[0].kids[0].text, "Loading…");
S.loaded = true; S.settingsError = "invalid json";
for (const id of ["overview", "models", "peers", "pairs", "rerank", "instructions", "advanced"]) {
  const box = pageBody(id, drawn)[0];
  assert.equal(box.className, "err", id);
  assert.match(box.kids[0].text, /settings\.json.*invalid json/, id);
}
for (const id of ["honcho", "runs", "cost", "logs"]) assert.deepEqual(pageBody(id, drawn), ["drawn"], id);
S.settings = {providers: {}};
assert.deepEqual(pageBody("models", drawn), ["drawn"]);
"""
    result = subprocess.run([node, "-e", setup + helper + body + checks], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_pair_and_peer_switches_stay_in_step():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    functions = source[source.index("function allPairKeys("):source.index("function bulkBar(")]
    checks = r"""
const assert = require('node:assert/strict');
const pairKey = (a, b) => a + '/' + b;
const s = {peers: {A: {enabled: false}, E: {enabled: false}}, pairs: {'A/E': {}}, scheduler: {skip: []}};
setPairOn(s, 'A/E', true);
assert.deepEqual([s.peers.A.enabled, s.peers.E.enabled], [true, true]);
assert.ok(pairRuns(s, 'A/E'));
assert.ok(!pairRuns(s, 'A/A') && !pairRuns(s, 'E/E'));
setPairOn(s, 'A/A', true);
setPairOn(s, 'A/E', false);
assert.deepEqual([s.peers.A.enabled, s.peers.E.enabled], [true, false]);
setPairOn(s, 'A/A', false);
assert.equal(s.peers.A.enabled, false);
setPeerOn(s, 'E', true);
assert.ok(pairRuns(s, 'E/E') && !pairRuns(s, 'A/E'));
setPairOn(s, 'A/E', true);
setPeerOn(s, 'A', false);
assert.ok(!pairRuns(s, 'A/E') && !pairRuns(s, 'A/A') && pairRuns(s, 'E/E'));
assert.equal(s.peers.A.enabled, false);
const t = {peers: {A: {enabled: false}, E: {enabled: false}}, pairs: {'E/A': {}}, scheduler: {skip: []}};
setPairOn(t, 'E/A', true);
setPeerOn(t, 'A', false);
assert.deepEqual([t.peers.A.enabled, t.peers.E.enabled], [false, false]);
"""
    result = subprocess.run([node, "-e", functions + checks], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_route_all_picks_the_backend_on_the_jobs_own_host():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is unavailable")
    source = (ROOT / "src/dashboard/app/index.html").read_text(encoding="utf-8")
    functions = source[source.index("function stageRefFor("):source.index("function effectivelyRouted(")]
    checks = r"""
const assert = require('node:assert/strict');
const S = {catalogue: {dream: [{backend: 'vertex', model: 'g'}, {backend: 'gemini', model: 'g'}]}, settings: {providers: {active: 'openai:m', backends: {vertex: {kind: 'openai-compat', base_url: 'http://vertex-proxy:4000/v1'}, gemini: {kind: 'openai-compat', base_url: 'https://generativelanguage.googleapis.com/v1beta/openai'}}}}};
assert.equal(stageRefFor('g', 'openai', 'http://vertex-proxy:4000/v1'), 'vertex:g');
assert.equal(stageRefFor('g', 'openai', 'https://generativelanguage.googleapis.com/v1beta/openai'), 'gemini:g');
assert.equal(stageRefFor('g', 'openai', undefined), 'openai:m');
"""
    result = subprocess.run([node, "-e", functions + checks], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
