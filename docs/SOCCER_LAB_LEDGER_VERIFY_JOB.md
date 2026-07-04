# Soccer Lab ledger verify job

Soccer Lab ledger verification is a scheduled fail-closed job around the daemon
chain verifier. It does not reimplement Ledger math. The job runs:

```bash
calyxd --ledger <dir> --once
calyxd --vault <dir> --once
```

and treats the emitted Prometheus text as the source of truth. A target is
healthy only when this exact sample is present with value `1`:

```text
calyx_ledger_chain_verify_ok{vault="<target>"} 1
```

Any missing sample, nonzero `calyxd` exit, corrupt chain, or broken chain records
a structured JSONL event and makes the job exit nonzero. Broken chains surface
`CALYX_LEDGER_CHAIN_BROKEN`; corrupt rows surface `CALYX_LEDGER_CORRUPT`.

## Configuration

The scheduled GitHub workflow is `.github/workflows/soccer-lab-ledger-verify.yml`.
It requires repository variable `CALYX_LEDGER_VERIFY_TARGETS`:

```json
[
  {"kind":"vault","path":"/srv/calyx/soccer-lab-vault"},
  {"kind":"ledger","path":"/srv/calyx/soccer-lab-ledger"}
]
```

`kind` is closed: `vault` or `ledger`. The workflow fails if the variable is
missing or empty.

## Local command

```bash
python3 tools/ops/run_ledger_verify_job.py \
  --targets "$CALYX_LEDGER_VERIFY_TARGETS" \
  --out scratchpad/wc2026/fsv/ledger_verify_job/report.json \
  --log scratchpad/wc2026/fsv/ledger_verify_job/job.jsonl \
  --calyxd-bin target/debug/calyxd
```

## Verification

Manual issue #70 FSV:

```bash
CALYX_ISSUE70_FSV_ROOT=scratchpad/wc2026/fsv/issue70_ledger_verify_job \
cargo test -p calyxd --test soccer_lab_ledger_verify_job_fsv -- --ignored --nocapture
```

The FSV writes real directory-ledger rows, tampers physical bytes for alert
cases, runs the Python job, reads back the report and JSONL bytes, and writes
`BLAKE3SUMS.txt`.
