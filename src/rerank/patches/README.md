# Patches

## summary-output-headroom.patch

The only change to Honcho's source this project carries against upstream Honcho `v3.5.0`. It is optional: only the `[summary] output_headroom` setting needs it.

Models that spend output tokens on reasoning can exhaust a summary's token cap before the summary is finished. The patch adds `OUTPUT_HEADROOM` to `SummarySettings`, a multiplier (default `1.0`, stock behaviour) applied only to the `max_tokens` sent to the model. The prompt's word target is unchanged.

Files touched: `src/utils/summarizer.py`, `src/config.py`, `config.toml.example`, `docs/v3/contributing/configuration.mdx`.

Apply from the root of a Honcho source tree, where `<CODE_ROOT>` is this repository's checkout:

```
git apply --check <CODE_ROOT>/src/rerank/patches/summary-output-headroom.patch
git apply <CODE_ROOT>/src/rerank/patches/summary-output-headroom.patch
```

Or use `apply.sh <honcho-source-dir>`, which checks first and lists the changed files.
