# Soccer Lab Web API environment

`calyx-web-api` is the loopback-only origin for Soccer Lab reads. It binds
`127.0.0.1:8121`; internet exposure, TLS, client identity, and per-client policy
belong at the reverse proxy or Worker layer.

## Required

| Variable | Purpose |
| --- | --- |
| `CALYX_WEB_API_VAULT_DIR` | Durable vault directory. Startup fails if it is missing or unreadable. |
| `CALYX_WEB_API_VAULT_NAME` | Vault name to open from `CALYX_WEB_API_VAULT_DIR`. |
| `CALYX_WEB_API_PREDICTION_EXPORT` | Soccer Lab Oracle prediction export JSON. Startup fails if it is missing or invalid. |
| `CALYX_WEB_API_BEARER_SECRET` | Shared-secret bearer expected on every request. Empty, blank, or missing values are rejected; there is no anonymous mode. |

Every request must include:

```text
Authorization: Bearer <CALYX_WEB_API_BEARER_SECRET>
```

Missing or wrong bearer credentials return a closed
`CALYX_WEB_API_UNAUTHORIZED` envelope with `WWW-Authenticate:
Bearer realm="calyx-origin"`.

## Response cache

The idempotent read endpoints cache successful serialized responses in process.
Errors are never cached.

| Variable | Default | Notes |
| --- | ---: | --- |
| `CALYX_WEB_API_CACHE_TTL_SECS` | `30` | Short freshness window. Set `0` to disable response caching. |
| `CALYX_WEB_API_CACHE_CAPACITY` | `256` | Maximum in-memory entries; oldest entries are evicted after expired entries. |

Cache hits include `X-Cache: HIT` and `Age`; misses include `X-Cache: MISS`.
Present-but-invalid numeric values fail startup instead of silently falling back
to defaults.

## Guardrails

Each request passes through route-aware resource guardrails before a handler runs:
token-bucket rate limiting, body-size caps, and a hard timeout. GPU-backed routes
use the tighter GPU bucket and 4 KiB body cap; other routes use the default
bucket and 8 KiB body cap.

GPU-backed routes are:

```text
/v1/measure
/v1/search
/search
/v1/guard
/kernel-answer
```

| Variable | Default | Notes |
| --- | ---: | --- |
| `CALYX_WEB_API_RATE_CAPACITY` | `60` | Non-negative finite token bucket capacity for non-GPU routes. |
| `CALYX_WEB_API_RATE_REFILL_PER_SEC` | `30` | Non-negative finite refill rate for non-GPU routes. |
| `CALYX_WEB_API_GPU_RATE_CAPACITY` | `8` | Non-negative finite token bucket capacity for GPU-backed routes. |
| `CALYX_WEB_API_GPU_RATE_REFILL_PER_SEC` | `2` | Non-negative finite refill rate for GPU-backed routes. |
| `CALYX_WEB_API_REQUEST_TIMEOUT_MS` | `5000` | Positive integer request timeout in milliseconds. |

Rate-limited requests return `CALYX_WEB_API_RATE_LIMITED` with
`Retry-After: 1`. Oversized bodies return `CALYX_WEB_API_PAYLOAD_TOO_LARGE`.
Timed-out handlers return `CALYX_WEB_API_TIMEOUT`. Invalid guardrail env values
fail startup with `CALYX_WEB_API_GUARDRAILS_CONFIG_FAILED`.

## Health dependency

| Variable | Default | Notes |
| --- | ---: | --- |
| `CALYX_WEB_API_HHEM_ADDR` | `127.0.0.1:8799` | Loopback HHEM faithfulness backend probed by `/v1/health`. |

The HHEM probe speaks HTTP and has its own short timeout so a socket-activated
but unhealthy backend cannot stall the health endpoint.
