# Soccer Lab Anneal autotuning

Anneal autotuning is enabled by writing a reversible vault-local policy:

```text
<vault>/.anneal/autotune.toml
<vault>/.anneal/tripwire.toml
```

The Soccer Lab default policy is intentionally conservative:

| Guard | Threshold |
| --- | ---: |
| `recall_at_k` | floor `0.95` |
| `guard_far` | ceiling `0.01` |
| `guard_frr` | ceiling `0.05` |
| `search_p99` | ceiling `200 ms` |
| `ingest_p95` | ceiling `500 ms` |

The policy requires shadow evaluation with at least three replay queries before
promotion. Candidate changes that cross a tripwire, regress a metric, exhaust
shadow budget, lack replay, or emit invalid metrics are reverted. Successful
shadow promotion does not auto-commit the rollback snapshot; explicit rollback
remains available until a later operator-controlled commit.

## Rollback behavior

Anneal prepares a rollback snapshot before changing a live artifact pointer.
For rejected candidates, the live pointer is restored to the prior pointer and
the Anneal ledger records a revert action with the shadow metrics. For promoted
candidates, the live pointer moves to the candidate pointer but remains
rollbackable. Rollback attempts after an explicit commit fail closed with
`CALYX_ANNEAL_CHANGE_COMMITTED`.

## Verification

Manual FSV for issue #68:

```bash
CALYX_ISSUE68_FSV_ROOT=scratchpad/wc2026/fsv/issue68_anneal_autotune \
cargo test -p calyx-anneal --test autotune_fsv -- --ignored --nocapture
```

The FSV artifact reads back the physical policy and tripwire TOML bytes, records
synthetic fail-closed edges, and writes `BLAKE3SUMS.txt`.
