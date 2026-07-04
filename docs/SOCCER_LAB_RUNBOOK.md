# Soccer Lab production runbook

This runbook is the end-to-end operator path for Soccer Lab: refresh real World
Cup data, rebuild the Calyx prediction pipeline, verify state, serve the API,
deploy the dashboard, and recover from failed checks. All commands run from the
repository root unless noted.

The binding data doctrine is `docs/STRUCTURAL_DATA_DOCTRINE.md`. Soccer Lab
lenses are facet projectors, not text embedders.

## 1. Refresh data

Real source data is pulled into `scratchpad/wc2026/raw`. Do not replace missing
external data with mocks.

```bash
python3 tools/data/acquire_soccer_lab_sources.py
python3 tools/data/provenance_manifest.py
```

Expected physical evidence:

```text
scratchpad/wc2026/raw/
scratchpad/wc2026/provenance/
```

If acquisition credentials are missing, stop and resolve the source credential
issue. Do not synthesize rows for unavailable sources.

The scheduled refresh entrypoint is:

```bash
python3 tools/data/run_soccer_lab_daily_refresh.py \
  --out-dir scratchpad/wc2026/fsv/daily_refresh
```

The CI schedule is `.github/workflows/soccer-lab-daily-refresh.yml`; see
`docs/SOCCER_LAB_DAILY_REFRESH.md`.

## 2. Build pipeline

Generate rows, facet projections, anchors, Oracle exports, and serving artifacts
from the refreshed physical data:

```bash
python3 tools/data/build_soccer_lab_pipeline.py
```

Then run the pre-merge gate:

```bash
python3 tools/data/run_soccer_lab_fsv_gate.py
```

The gate reads real raw files, runs the pipeline driver, validates Oracle export
schema, builds the dashboard, exercises live API paths, writes
`scratchpad/wc2026/fsv/premerge_gate/report.json`, and exits nonzero on any
missing input or readback mismatch. See `docs/SOCCER_LAB_PREMERGE_FSV_GATE.md`.
The CI entrypoint is `.github/workflows/soccer-lab-fsv-gate.yml`.

## 3. Verify operating state

Enable Anneal policy and tripwires for the target vault:

```bash
target/debug/calyx anneal enable-autotune --vault <vault>
target/debug/calyx readback config autotune --vault <vault>
```

Run the scheduled ledger verify job locally before deploying a rebuilt vault:

```bash
python3 tools/ops/run_ledger_verify_job.py \
  --targets "$CALYX_LEDGER_VERIFY_TARGETS" \
  --out scratchpad/wc2026/fsv/ledger_verify_job/report.json \
  --log scratchpad/wc2026/fsv/ledger_verify_job/job.jsonl \
  --calyxd-bin target/debug/calyxd
```

The job accepts only `calyx_ledger_chain_verify_ok{vault="<target>"} 1` as a
healthy target. Alerts on `CALYX_LEDGER_CHAIN_BROKEN` require quarantine and
investigation before serving. See `docs/SOCCER_LAB_LEDGER_VERIFY_JOB.md`.
The scheduled CI entrypoint is
`.github/workflows/soccer-lab-ledger-verify.yml`.

## 4. Serve API

Build and start the loopback origin:

```bash
cargo build --bin calyx-web-api
CALYX_WEB_API_VAULT_DIR=/path/to/calyx_home/vaults/01KWND5F2PJ4ZMB9V8BMZDH44T \
CALYX_WEB_API_VAULT_NAME=soccer-rebuild-search-index \
CALYX_WEB_API_PREDICTION_EXPORT=/path/to/Calyx/docs/data/soccer_lab_prediction_export.json \
CALYX_WEB_API_BEARER_SECRET=<origin-shared-secret> \
CALYX_WEB_API_CACHE_TTL_SECS=0 \
target/debug/calyx-web-api
```

The origin binds loopback (`127.0.0.1:8121`) and every request must include the
bearer secret. Required environment details are in
`docs/SOCCER_LAB_WEB_API_ENV.md`.

Health and monitoring:

```bash
curl -fsS -H "Authorization: Bearer $CALYX_WEB_API_BEARER_SECRET" http://127.0.0.1:8121/v1/health
curl -fsS -H "Authorization: Bearer $CALYX_WEB_API_BEARER_SECRET" http://127.0.0.1:8121/metrics
```

Expected metrics include `calyx_prediction_total`, `calyx_guard_far`,
`calyx_guard_frr`, and the ledger verify families. See
`docs/SOCCER_LAB_MONITORING.md`.

## 5. Deploy UI

Build the static dashboard with same-origin API routing:

```bash
cd apps/soccer-lab-dashboard
VITE_CALYX_WEB_API_BASE_URL=/api npm run build
CALYX_WEB_API_PROXY_TARGET=http://127.0.0.1:8121 \
CALYX_WEB_API_BEARER_SECRET=<origin-shared-secret> \
CALYX_DASHBOARD_PREVIEW_PORT=4173 \
npm run serve:deploy-preview
CALYX_DASHBOARD_PREVIEW_URL=http://127.0.0.1:4173 npm run verify:deploy-preview
```

Production must serve `apps/soccer-lab-dashboard/dist` and proxy `/api/*` to
the private `calyx-web-api` origin while injecting
`Authorization: Bearer <CALYX_WEB_API_BEARER_SECRET>`. See
`docs/SOCCER_LAB_DASHBOARD_DEPLOY.md`.

## 6. Recovery

| Symptom | Action |
| --- | --- |
| Source acquisition fails | Stop. Fix credentials or upstream source availability; rerun `python3 tools/data/acquire_soccer_lab_sources.py`. |
| Pipeline or FSV gate fails | Keep the previous deployed vault/export. Inspect `scratchpad/wc2026/fsv/premerge_gate/report.json` and rerun only after the physical readback mismatch is fixed. |
| `CALYX_LEDGER_CHAIN_BROKEN` | Quarantine the affected vault or ledger target, preserve `job.jsonl` and report artifacts, restore from the last intact snapshot, then rerun `python3 tools/ops/run_ledger_verify_job.py`. |
| Anneal tripwire trips | Revert to the previous live artifact pointer; read `docs/SOCCER_LAB_ANNEAL_AUTOTUNE.md` for rollback behavior. |
| API startup fails | Treat startup as failed closed. Fix the missing vault, prediction export, bearer secret, or guardrail environment value described in the structured error. |
| Dashboard preview fails | Do not deploy `dist`. Rebuild with `VITE_CALYX_WEB_API_BASE_URL=/api` and rerun `npm run verify:deploy-preview`. |

## 7. Release checklist

- `python3 tools/data/run_soccer_lab_fsv_gate.py` passed.
- `python3 tools/data/run_soccer_lab_daily_refresh.py ...` passed for the
  scheduled refresh window.
- `python3 tools/ops/run_ledger_verify_job.py ...` returned status `ok`.
- `target/debug/calyx readback config autotune --vault <vault>` shows
  `revert_on_tripwire_or_regression`.
- `curl` to `/v1/health` and `/metrics` succeeds through the bearer-locked
  loopback origin.
- Dashboard `npm run verify:deploy-preview` passed against the live origin.
- Evidence reports are preserved under `scratchpad/wc2026/fsv/`.
