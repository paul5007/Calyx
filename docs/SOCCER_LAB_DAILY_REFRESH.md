# Soccer Lab daily refresh

The daily refresh job pulls current source data, rewrites provenance, rebuilds
the prediction pipeline, and runs the full Soccer Lab FSV gate.

Default command:

```bash
python3 tools/data/run_soccer_lab_daily_refresh.py \
  --out-dir scratchpad/wc2026/fsv/daily_refresh
```

Default step order:

1. `tools/data/acquire_soccer_lab_sources.py`
2. `tools/data/provenance_manifest.py write`
3. `tools/data/provenance_manifest.py verify`
4. `tools/data/build_soccer_lab_pipeline.py`
5. `tools/data/run_soccer_lab_fsv_gate.py`

The job is fail closed. Every step must exit zero and write its expected
physical report. The final report is read back from disk before success is
printed. Structured events are appended to:

```text
scratchpad/wc2026/fsv/daily_refresh/daily-refresh.jsonl
```

The scheduled workflow is `.github/workflows/soccer-lab-daily-refresh.yml`.
Required credentials are the same as acquisition and the FSV gate:

```text
KAGGLE_USERNAME
KAGGLE_KEY
THESTATSAPI_KEY
SOCCER_LAB_HF_DATASET_REPO
HF_TOKEN
CALYX_SOCCER_LAB_GATE_BEARER
```

Missing credentials, unavailable sources, failed re-ingest, failed re-grounding,
failed prediction export validation, failed UI/API verification, missing reports,
or report readback mismatches make the job fail nonzero.

Manual issue #72 FSV:

```bash
python3 tools/data/verify_soccer_lab_daily_refresh.py \
  --fsv-root scratchpad/wc2026/fsv/issue72_daily_refresh
```

The FSV uses a local plan with deterministic physical report files to verify
the scheduler mechanics, idempotent reruns, structured logs, and fail-closed
edges. The production default plan remains the real acquisition-to-FSV path
listed above.
