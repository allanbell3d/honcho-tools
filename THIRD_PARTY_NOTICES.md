# Third-party notices

## Honcho

Honcho is © Plastic Labs, released under the GNU Affero General Public License v3.0
(<https://github.com/plastic-labs/honcho>). These files are derived from Honcho 3.5.0 and keep that licence:

| File | What it holds |
|---|---|
| `src/config/honcho-prompts.json` | Honcho's own prompt texts, extracted by `src/config/gen_honcho_prompts.py`. |
| `src/config/honcho-config.schema.json` | Field names, defaults, bounds and descriptions from Honcho's settings classes, extracted by `src/config/gen_honcho_schema.py`. |
| `src/rerank/patches/summary-output-headroom.patch` | A change to Honcho's source; the context lines are Honcho's code. |

`src/hooks/` and `src/rerank/honcho_rerank/` run inside Honcho's processes and call its modules at run time.

## Reranking ideas

The rerank component's design follows two community forks of Honcho: Grant Jordan's
([grantjayy/honcho](https://github.com/grantjayy/honcho)) shared HTTPS Voyage client with fail-open behaviour, and
myles' ([myleshuzx/honcho](https://github.com/myleshuzx/honcho)) reranking inside `query_documents` with a bounded
candidate pool.
