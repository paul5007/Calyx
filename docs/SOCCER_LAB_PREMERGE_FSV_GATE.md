# Soccer Lab pre-merge FSV gate

`tools/data/run_soccer_lab_fsv_gate.py` is the fail-closed pre-merge gate for
Soccer Lab. It requires the real pulled raw data, release `calyx`, debug
`calyx-web-api`, dashboard dependencies, and a Playwright Chromium browser.

Run from the repository root:

```bash
cargo build --release --bin calyx
cargo build --bin calyx-web-api
cd apps/soccer-lab-dashboard && npm ci && npx playwright install chromium && cd ../..
python3 tools/data/run_soccer_lab_fsv_gate.py
```

Optional environment:

```text
CALYX_SOCCER_LAB_RAW_ROOT=scratchpad/wc2026/raw
CALYX_SOCCER_LAB_GATE_OUT=scratchpad/wc2026/fsv/premerge_gate/report.json
CALYX_SOCCER_LAB_GATE_BEARER=soccer-lab-fsv-premerge-secret
```

The gate emits JSONL events to stdout and
`scratchpad/wc2026/fsv/premerge_gate/gate.jsonl`, writes a readback-verified
report, and exits non-zero on any missing input, verifier failure, service
startup failure, browser E2E failure, or report readback mismatch.
