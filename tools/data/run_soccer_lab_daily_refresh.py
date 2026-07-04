#!/usr/bin/env python3
"""Daily Soccer Lab refresh: acquire -> provenance -> pipeline -> FSV gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = ROOT / "scratchpad/wc2026/fsv/daily_refresh"
DEFAULT_RAW_ROOT = ROOT / "scratchpad/wc2026/raw"

INVALID_CONFIG = "CALYX_SOCCER_LAB_DAILY_REFRESH_INVALID_CONFIG"
STEP_FAILED = "CALYX_SOCCER_LAB_DAILY_REFRESH_STEP_FAILED"
REPORT_MISSING = "CALYX_SOCCER_LAB_DAILY_REFRESH_REPORT_MISSING"
READBACK_MISMATCH = "CALYX_SOCCER_LAB_DAILY_REFRESH_READBACK_MISMATCH"


class RefreshError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def default_plan(raw_root: Path, out_dir: Path) -> list[dict[str, Any]]:
    raw = display_path(raw_root)
    provenance = "scratchpad/wc2026/provenance/source_manifest.json"
    return [
        {
            "id": "acquire_sources",
            "command": [sys.executable, "tools/data/acquire_soccer_lab_sources.py", "--out", raw],
            "report": f"{raw}/acquisition_manifest.json",
        },
        {
            "id": "provenance_write",
            "command": [
                sys.executable,
                "tools/data/provenance_manifest.py",
                "write",
                "--raw-root",
                raw,
                "--manifest",
                provenance,
            ],
            "report": provenance,
        },
        {
            "id": "provenance_verify",
            "command": [
                sys.executable,
                "tools/data/provenance_manifest.py",
                "verify",
                "--raw-root",
                raw,
                "--manifest",
                provenance,
            ],
            "report": provenance,
        },
        {
            "id": "pipeline_build",
            "command": [
                sys.executable,
                "tools/data/build_soccer_lab_pipeline.py",
                "--raw-root",
                raw,
                "--out-dir",
                "scratchpad/wc2026/pipeline/daily_refresh",
            ],
            "report": "scratchpad/wc2026/pipeline/daily_refresh/pipeline-report.json",
        },
        {
            "id": "premerge_fsv_gate",
            "command": [
                sys.executable,
                "tools/data/run_soccer_lab_fsv_gate.py",
                "--raw-root",
                raw,
                "--out",
                "scratchpad/wc2026/fsv/daily_refresh/premerge_gate/report.json",
            ],
            "report": "scratchpad/wc2026/fsv/daily_refresh/premerge_gate/report.json",
        },
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=os.environ.get("CALYX_SOCCER_LAB_RAW_ROOT", display_path(DEFAULT_RAW_ROOT)))
    parser.add_argument("--out-dir", default=os.environ.get("CALYX_SOCCER_LAB_DAILY_OUT", display_path(DEFAULT_OUT_DIR)))
    parser.add_argument("--plan", default="", help="optional JSON plan for FSV/smoke verification")
    parser.add_argument("--timeout-secs", type=int, default=7200)
    args = parser.parse_args()

    out_dir = resolve(args.out_dir)
    log_path = out_dir / "daily-refresh.jsonl"
    report_path = out_dir / "daily-refresh-report.json"
    try:
        raw_root = resolve(args.raw_root)
        plan = load_plan(args.plan, raw_root, out_dir)
        report = run_refresh(plan, out_dir, log_path, args.timeout_secs)
        write_json(report_path, report)
        readback = json.loads(report_path.read_text(encoding="utf-8"))
        if readback != report:
            raise RefreshError(
                READBACK_MISMATCH,
                "daily refresh report readback changed after write",
                {"report": display_path(report_path)},
            )
        emit(log_path, "daily_refresh.complete", status=report["status"], report=file_readback(report_path))
        print(json.dumps({"status": "ok", "report": file_readback(report_path)}, sort_keys=True))
        return 0
    except RefreshError as error:
        failure = {
            "status": "error",
            "code": error.code,
            "message": error.message,
            "detail": error.detail,
        }
        emit(log_path, "daily_refresh.failed", **failure)
        write_json(report_path, failure)
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 2


def load_plan(plan_arg: str, raw_root: Path, out_dir: Path) -> list[dict[str, Any]]:
    if not plan_arg:
        plan = default_plan(raw_root, out_dir)
    else:
        path = resolve(plan_arg)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RefreshError(
                INVALID_CONFIG,
                f"cannot read refresh plan {path}: {error}",
                {"plan": str(path)},
            ) from error
        plan = payload.get("steps") if isinstance(payload, dict) else None
    if not isinstance(plan, list) or not plan:
        raise RefreshError(INVALID_CONFIG, "refresh plan must contain non-empty steps[]")
    seen: set[str] = set()
    for index, step in enumerate(plan):
        if not isinstance(step, dict):
            raise RefreshError(INVALID_CONFIG, f"step {index} is not an object")
        step_id = step.get("id")
        command = step.get("command")
        report = step.get("report")
        if not isinstance(step_id, str) or not step_id:
            raise RefreshError(INVALID_CONFIG, f"step {index} id must be non-empty")
        if step_id in seen:
            raise RefreshError(INVALID_CONFIG, f"duplicate refresh step {step_id}")
        seen.add(step_id)
        if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
            raise RefreshError(INVALID_CONFIG, f"step {step_id} command must be a non-empty string array")
        if not isinstance(report, str) or not report:
            raise RefreshError(INVALID_CONFIG, f"step {step_id} report must be non-empty")
    return plan


def run_refresh(plan: list[dict[str, Any]], out_dir: Path, log_path: Path, timeout_secs: int) -> dict[str, Any]:
    if timeout_secs <= 0:
        raise RefreshError(INVALID_CONFIG, "--timeout-secs must be positive")
    out_dir.mkdir(parents=True, exist_ok=True)
    started = int(time.time())
    emit(log_path, "daily_refresh.start", steps=[step["id"] for step in plan])
    results = []
    for step in plan:
        results.append(run_step(step, log_path, timeout_secs))
    return {
        "status": "ok",
        "schema": "soccer-lab-daily-refresh-report-v1",
        "source_of_truth": "physical command reports and daily-refresh.jsonl bytes",
        "started_unix_secs": started,
        "finished_unix_secs": int(time.time()),
        "steps": results,
        "log": file_readback(log_path),
    }


def run_step(step: dict[str, Any], log_path: Path, timeout_secs: int) -> dict[str, Any]:
    step_id = step["id"]
    command = step["command"]
    report = resolve(step["report"])
    started = time.monotonic()
    emit(log_path, "step.start", step_id=step_id, command=command, report=display_path(report))
    try:
        proc = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_secs)
    except subprocess.TimeoutExpired as error:
        detail = {
            "step_id": step_id,
            "command": command,
            "timeout_secs": timeout_secs,
            "stdout_sha256": sha256_bytes(error.stdout or b""),
            "stderr_sha256": sha256_bytes(error.stderr or b""),
        }
        emit(log_path, "step.timeout", **detail)
        raise RefreshError(STEP_FAILED, f"refresh step {step_id} timed out", detail) from error
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if proc.returncode != 0:
        detail = step_detail(step_id, command, proc, elapsed_ms)
        emit(log_path, "step.failed", **detail)
        raise RefreshError(STEP_FAILED, f"refresh step {step_id} failed", detail)
    if not report.is_file():
        detail = step_detail(step_id, command, proc, elapsed_ms) | {"report": display_path(report)}
        emit(log_path, "step.report_missing", **detail)
        raise RefreshError(REPORT_MISSING, f"refresh step {step_id} did not write report", detail)
    report_readback = file_readback(report)
    result = {
        "step_id": step_id,
        "status": "ok",
        "command": command,
        "elapsed_ms": elapsed_ms,
        "report": report_readback,
        "stdout_sha256": sha256_bytes(proc.stdout),
        "stderr_sha256": sha256_bytes(proc.stderr),
    }
    emit(log_path, "step.ok", **result)
    return result


def step_detail(step_id: str, command: list[str], proc: subprocess.CompletedProcess[bytes], elapsed_ms: int) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "command": command,
        "returncode": proc.returncode,
        "elapsed_ms": elapsed_ms,
        "stdout_sha256": sha256_bytes(proc.stdout),
        "stderr_sha256": sha256_bytes(proc.stderr),
        "stdout_tail": proc.stdout.decode("utf-8", "replace")[-2000:],
        "stderr_tail": proc.stderr.decode("utf-8", "replace")[-4000:],
    }


def emit(log_path: Path, event: str, **payload: Any) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "soccer-lab-daily-refresh-log-v1",
        "event": event,
        "ts_ms": int(time.time() * 1000),
        **payload,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.write_text(encoded, encoding="utf-8")
    if path.read_text(encoding="utf-8") != encoded:
        raise RefreshError(READBACK_MISMATCH, f"readback mismatch after writing {path}")


def file_readback(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": display_path(path),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
