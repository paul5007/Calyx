# Soccer Lab monitoring

Soccer Lab production monitoring uses the existing daemon Prometheus surface and
a bounded JSONL operational log.

## Metrics

The source of truth for metrics is a real HTTP scrape:

```text
GET /metrics
```

Required Soccer Lab families:

| Signal | Metric |
| --- | --- |
| Ingest throughput | `calyx_ingest_total{vault,status}` |
| Ingest latency | `calyx_ingest_duration_seconds_bucket{vault,le}` |
| Search throughput | `calyx_search_total{vault,strategy,status}` |
| Search latency | `calyx_search_duration_seconds_bucket{vault,strategy,le}` |
| Prediction throughput | `calyx_prediction_total{vault,endpoint,status}` |
| Prediction latency | `calyx_prediction_duration_seconds_bucket{vault,endpoint,le}` |
| Guard false accepts | `calyx_guard_far{vault,slot}` |
| Guard false rejects | `calyx_guard_frr{vault,slot}` |

Prediction endpoint labels are closed: `match`, `progression`, and `player`.
Status labels are closed: `ok` and `err`.

## Structured Logs

Structured operational events are JSONL rows written through
`StructuredMetricLog`. Each row carries:

```text
ts_unix_secs, surface, vault, status, duration_ms,
search_strategy, prediction_endpoint, guard_slot, guard_far, guard_frr, message
```

Validation is fail closed before append. Invalid surfaces, unbounded prediction
endpoints, missing labels, negative or non-finite durations, and guard rates
outside `[0, 1]` return `CALYX_METRICS_INVALID_OBSERVATION`. An unwritable log
path returns `CALYX_METRICS_LOG_WRITE_FAILED`.

## Verification

Manual issue #69 FSV:

```bash
CALYX_ISSUE69_FSV_ROOT=scratchpad/wc2026/fsv/issue69_monitoring \
cargo test -p calyxd --test soccer_lab_monitoring_fsv -- --ignored --nocapture
```

The FSV binds the real metrics server, scrapes `/metrics` over TCP, writes the
Prometheus body and JSONL log to disk, reads those physical bytes back, and
writes `BLAKE3SUMS.txt`.
