#!/usr/bin/env python3
"""Fail-closed scheduled Ledger verify-chain job.

The job delegates verification to `calyxd --once`, then treats the emitted
Prometheus text as the source of truth. Any target whose
`calyx_ledger_chain_verify_ok{vault=...}` sample is not exactly `1` fails the
job and records a structured JSONL alert.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

INVALID_CONFIG = "CALYX_LEDGER_VERIFY_JOB_INVALID_CONFIG"
COMMAND_FAILED = "CALYX_LEDGER_VERIFY_JOB_COMMAND_FAILED"
CHAIN_BROKEN = "CALYX_LEDGER_CHAIN_BROKEN"
REPORT_MISMATCH = "CALYX_LEDGER_VERIFY_JOB_REPORT_MISMATCH"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", required=True, help="JSON array of {kind,path} targets")
    parser.add_argument("--out", required=True, help="report JSON path")
    parser.add_argument("--log", required=True, help="structured JSONL log path")
    parser.add_argument(
        "--calyxd-bin",
        default=os.environ.get("CALYXD_BIN", "target/debug/calyxd"),
        help="calyxd binary path",
    )
    args = parser.parse_args()

    out = Path(args.out)
    log = Path(args.log)
    try:
        targets = load_targets(args.targets)
        report = run_job(targets, Path(args.calyxd_bin), log)
        write_json(out, report)
        readback = json.loads(out.read_text(encoding="utf-8"))
        if readback != report:
            append_log(
                log,
                {
                    "event": "ledger_verify_job.error",
                    "code": REPORT_MISMATCH,
                    "message": "report readback differed from written report",
                    "remediation": "write report to a stable regular file and rerun",
                },
            )
            return 5
        return 0 if report["status"] == "ok" else 3
    except JobError as error:
        append_log(log, error.to_json())
        fallback_report = {
            "status": "error",
            "code": error.code,
            "message": error.message,
            "source_of_truth": "calyxd --once Prometheus text per target",
            "targets": [],
        }
        write_json(out, fallback_report)
        return error.exit_code


class JobError(Exception):
    def __init__(self, code: str, message: str, remediation: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation
        self.exit_code = exit_code

    def to_json(self) -> dict[str, Any]:
        return {
            "event": "ledger_verify_job.error",
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }


def load_targets(raw: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise JobError(
            INVALID_CONFIG,
            f"--targets is not valid JSON: {error}",
            "pass a JSON array of {kind,path} objects",
        ) from error
    if not isinstance(parsed, list) or not parsed:
        raise JobError(INVALID_CONFIG, "--targets must be a non-empty array", "configure at least one vault or ledger target")
    targets: list[dict[str, str]] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise JobError(INVALID_CONFIG, f"target {index} is not an object", "use {kind,path} objects")
        kind = item.get("kind")
        path = item.get("path")
        if kind not in {"vault", "ledger"}:
            raise JobError(INVALID_CONFIG, f"target {index} kind must be vault or ledger", "use a supported target kind")
        if not isinstance(path, str) or not path:
            raise JobError(INVALID_CONFIG, f"target {index} path must be non-empty", "use an existing target path")
        targets.append({"kind": kind, "path": path})
    return targets


def run_job(targets: list[dict[str, str]], calyxd_bin: Path, log: Path) -> dict[str, Any]:
    started = int(time.time())
    rows = []
    ok = True
    for target in targets:
        row = run_target(target, calyxd_bin, log)
        rows.append(row)
        ok = ok and row["ok"] == 1
    report = {
        "status": "ok" if ok else "alert",
        "source_of_truth": "calyxd --once Prometheus text per target",
        "started_unix_secs": started,
        "finished_unix_secs": int(time.time()),
        "targets": rows,
    }
    append_log(
        log,
        {
            "event": "ledger_verify_job.complete",
            "status": report["status"],
            "target_count": len(rows),
            "alert_count": sum(1 for row in rows if row["ok"] != 1),
        },
    )
    return report


def run_target(target: dict[str, str], calyxd_bin: Path, log: Path) -> dict[str, Any]:
    kind = target["kind"]
    path = target["path"]
    flag = "--vault" if kind == "vault" else "--ledger"
    command = [str(calyxd_bin), flag, path, "--once"]
    started = time.time()
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    duration_ms = round((time.time() - started) * 1000.0, 3)
    metrics_sha256 = hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest()
    ok_value = parse_ok_metric(proc.stdout, path)
    entries = parse_entries_metric(proc.stdout, path)
    code = None
    if proc.returncode != 0:
        code = COMMAND_FAILED
    elif ok_value != 1:
        code = CHAIN_BROKEN
    row = {
        "kind": kind,
        "path": path,
        "ok": ok_value,
        "entries": entries,
        "returncode": proc.returncode,
        "duration_ms": duration_ms,
        "metrics_sha256": metrics_sha256,
        "stderr": proc.stderr.strip(),
        "code": code,
    }
    append_log(
        log,
        {
            "event": "ledger_verify_job.target",
            "kind": kind,
            "path": path,
            "ok": ok_value,
            "entries": entries,
            "returncode": proc.returncode,
            "duration_ms": duration_ms,
            "code": code,
        },
    )
    return row


def parse_ok_metric(metrics: str, path: str) -> int:
    value = parse_metric(metrics, "calyx_ledger_chain_verify_ok", path)
    if value is None:
        return 0
    return int(value)


def parse_entries_metric(metrics: str, path: str) -> int:
    value = parse_metric(metrics, "calyx_ledger_chain_verify_entries", path)
    if value is None:
        return 0
    return int(value)


def parse_metric(metrics: str, name: str, path: str) -> float | None:
    label = escape_label(path)
    pattern = re.compile(rf'^{re.escape(name)}\{{vault="{re.escape(label)}"\}} ([0-9.eE+-]+)$')
    for line in metrics.splitlines():
        match = pattern.match(line)
        if match:
            return float(match.group(1))
    return None


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def append_log(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
        handle.write("\n")


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
