# calyxd service and healthcheck

`calyxd` runs as a loopback-only service. It validates `calyx.toml`, performs a
startup CUDA/VRAM/vault readiness check, writes the health JSON source of truth,
and only then binds the HTTP listener.

## Minimal calyx.toml

```toml
bind_addr = "127.0.0.1:7700"
vault_path = "/zfs/hot/calyx/vault"
vram_budget_mib = 8192
log_dir = "/zfs/hot/logs/calyx"
health_log_path = "/zfs/hot/logs/calyx-health/latest.json"
healthcheck_timeout_secs = 30
```

`bind_addr` and optional `mcp_bind_addr` must be loopback addresses. Config
parse/validation failures exit with `CALYX_DAEMON_CONFIG_INVALID`,
`CALYX_DAEMON_BIND_FAILED`, `CALYX_FORGE_VRAM_BUDGET`, or
`CALYX_TLS_CONFIG_INVALID`; no daemon listener is started for invalid config.

## systemd unit shape

```ini
[Unit]
Description=Calyx daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/calyxd --config /etc/calyx/calyx.toml
ExecStartPost=/usr/local/bin/calyx healthcheck --config /etc/calyx/calyx.toml --out /zfs/hot/logs/calyx-health/latest.json
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

The daemon itself also runs the same readiness check during startup. If CUDA,
VRAM budget audit, or vault readback fails, it writes a fail JSON record and
exits before binding.

## Health JSON

The physical source of truth is `health_log_path`. The config-driven daemon also
serves the same startup health record at:

```text
GET http://127.0.0.1:7700/healthz
```

Healthy response:

```json
{
  "ready": true,
  "config_valid": true,
  "status": "pass",
  "timestamp_utc": "2026-07-04T12:00:00Z",
  "cuda_device": "NVIDIA CUDA GPU",
  "vram_budget_mib": 8192,
  "vault_read_ok": true,
  "error_code": null,
  "error_detail": null
}
```

If a health record is present but not ready, `/healthz` returns HTTP `503` with
the fail JSON. Metrics-only test servers do not serve `/healthz`.

## Metrics

Prometheus metrics are served at:

```text
GET http://127.0.0.1:7700/metrics
```

The metrics surface includes chain verification, restore readback counts, VRAM
budget audit, ZFS integrity, and registered hazard gauges.
