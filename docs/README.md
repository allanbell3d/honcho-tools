# docs

Start with the repository [README](../README.md) for what the project is and the quick start. Then:

**Getting started**

- [`SETUP.md`](SETUP.md): installing on a fresh host, step by step, with a check after each step.
- [`SETTINGS.md`](SETTINGS.md): every key of `settings.json`, the one behaviour file.

**How it works**

- [`ARCHITECTURE.md`](ARCHITECTURE.md): how the stack fits together, the rules each component follows, what each may
  touch, and the detail of mode, lookup, routing, instruction files, telemetry, backup, the settings file and the
  dashboard.
- [`STAGE-ROUTING.md`](STAGE-ROUTING.md): routing Honcho's jobs (intake, summaries, dialectic, dreams) through the
  bridge, and tool-free CLI calls.
- [`HONCHO-CONFIG.md`](HONCHO-CONFIG.md): the dashboard page that edits Honcho's `config.toml`: routing with undo,
  the per-job Honcho-native fallback, dream knobs, restart.
- [`EXECUTED-ROUTING.md`](EXECUTED-ROUTING.md): which model, backend and billing route answered each call, as recorded
  in telemetry, and how the Cost page prices it.
- [`INTERFACES.md`](INTERFACES.md): the HTTP endpoints, files, logs and records each component exposes.

**Running it**

- [`OPERATIONS.md`](OPERATIONS.md): day-to-day use and the recurring tasks.
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md): symptoms, where to look, what to do.
- [`../deploy/README.md`](../deploy/README.md): the compose stack: folders, what each container sees, the Docker
  socket, stopping one service, the install check.

**Each component**, in depth: [`../src/bridge/README.md`](../src/bridge/README.md),
[`../src/hooks/README.md`](../src/hooks/README.md), [`../src/scheduler/README.md`](../src/scheduler/README.md),
[`../src/telemetry/README.md`](../src/telemetry/README.md), [`../src/backup/README.md`](../src/backup/README.md)
(with [`restore.md`](../src/backup/restore.md)), [`../src/dashboard/README.md`](../src/dashboard/README.md),
[`../src/rerank/README.md`](../src/rerank/README.md), [`../src/config/README.md`](../src/config/README.md) (the
templates and `validate.py`).

**Tests and the benchmark**: [`../tests/README.md`](../tests/README.md) and [`../bench/README.md`](../bench/README.md).
