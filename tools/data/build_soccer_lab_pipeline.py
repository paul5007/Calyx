#!/usr/bin/env python3
"""Run the Soccer Lab Epic C pipeline with structured logs and resume state."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW = ROOT / "scratchpad" / "wc2026" / "raw"
DEFAULT_OUT_DIR = ROOT / "scratchpad" / "wc2026" / "pipeline" / "epic_c"


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def default_plan() -> list[dict[str, Any]]:
    return [
        step("teams_history_vault", "verify_soccer_lab_teams_history_vault.py"),
        step("matches_vault", "verify_soccer_lab_matches_vault.py"),
        step("players_vault", "verify_soccer_lab_players_vault.py"),
        step("bits_assay", "verify_soccer_lab_bits_assay.py"),
        step("weave_loom", "verify_soccer_lab_weave_loom.py"),
        step("kernel_build", "verify_soccer_lab_kernel_build.py"),
        step("guard_calibrate", "verify_soccer_lab_guard_calibrate.py"),
        step("rebuild_search_index", "verify_soccer_lab_rebuild_search_index.py"),
    ]


def step(step_id: str, script: str) -> dict[str, Any]:
    report = f"{{out_dir}}/reports/{step_id}.json"
    return {
        "id": step_id,
        "command": [
            sys.executable,
            str(ROOT / "tools" / "data" / script),
            "--raw-root",
            "{raw_root}",
            "--out",
            report,
        ],
        "report": report,
    }


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def file_readback(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "soccer-lab-pipeline-log-v1",
        "ts_ms": int(time.time() * 1000),
        **payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": "soccer-lab-pipeline-state-v1", "steps": {}}
    state = load_json(path)
    if state.get("schema") != "soccer-lab-pipeline-state-v1" or not isinstance(state.get("steps"), dict):
        raise PipelineError("PIPELINE_STATE_INVALID", f"invalid pipeline state at {path}", {"path": str(path)})
    return state


def load_plan(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        plan = default_plan()
    else:
        payload = load_json(path)
        plan = payload.get("steps") if isinstance(payload, dict) else None
    if not isinstance(plan, list) or not plan:
        raise PipelineError("PIPELINE_EMPTY_PLAN", "pipeline plan must contain at least one step")
    seen: set[str] = set()
    for item in plan:
        if not isinstance(item, dict) or not item.get("id") or not isinstance(item.get("command"), list) or not item.get("report"):
            raise PipelineError("PIPELINE_PLAN_INVALID", "each pipeline step requires id, command, and report", {"step": item})
        if item["id"] in seen:
            raise PipelineError("PIPELINE_PLAN_DUPLICATE_STEP", f"duplicate pipeline step {item['id']}")
        seen.add(item["id"])
    return plan


def expand(value: str, raw_root: Path, out_dir: Path, report: Path | None = None) -> str:
    return value.format(
        root=str(ROOT),
        raw_root=str(raw_root),
        out_dir=str(out_dir),
        report=str(report) if report is not None else "",
    )


def expand_step(item: dict[str, Any], raw_root: Path, out_dir: Path) -> dict[str, Any]:
    report = resolve(expand(str(item["report"]), raw_root, out_dir))
    command = [expand(str(part), raw_root, out_dir, report) for part in item["command"]]
    return {"id": str(item["id"]), "command": command, "report": report}


def report_ok(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("status") != "ok":
        return None
    return payload


def completed_record(step_id: str, command: list[str], report: Path, started_ms: int, completed_ms: int, stdout: bytes, stderr: bytes) -> dict[str, Any]:
    return {
        "status": "completed",
        "step_id": step_id,
        "command": command,
        "started_ms": started_ms,
        "completed_ms": completed_ms,
        "elapsed_ms": completed_ms - started_ms,
        "report": file_readback(report),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
    }


def valid_completed(state: dict[str, Any], step_id: str, report: Path, command: list[str]) -> dict[str, Any] | None:
    payload = report_ok(report)
    if payload is None:
        return None
    stat = file_readback(report)
    stored = state.get("steps", {}).get(step_id)
    if stored and stored.get("status") == "completed" and stored.get("report", {}).get("sha256") == stat["sha256"]:
        return stored
    return {
        "status": "completed",
        "step_id": step_id,
        "command": command,
        "started_ms": None,
        "completed_ms": None,
        "elapsed_ms": None,
        "report": stat,
        "stdout_sha256": None,
        "stderr_sha256": None,
        "recovered_from_report": True,
    }


def run_pipeline(raw_root: Path, out_dir: Path, plan_path: Path | None, force: bool) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "pipeline.jsonl"
    state_path = out_dir / "pipeline-state.json"
    report_path = out_dir / "pipeline-report.json"
    plan = load_plan(plan_path)
    state = load_state(state_path)
    append_log(log_path, {"event": "pipeline.start", "out_dir": str(out_dir.relative_to(ROOT)), "steps": [item["id"] for item in plan]})
    results = []
    for raw_step in plan:
        expanded = expand_step(raw_step, raw_root, out_dir)
        step_id = expanded["id"]
        command = expanded["command"]
        report = expanded["report"]
        if not force:
            existing = valid_completed(state, step_id, report, command)
            if existing is not None:
                state["steps"][step_id] = existing
                atomic_write_json(state_path, state)
                append_log(log_path, {"event": "step.skip", "step_id": step_id, "reason": "completed_report_readback", "report": existing["report"]})
                results.append({"step_id": step_id, "status": "skipped", "report": existing["report"]})
                continue
        started_ms = int(time.time() * 1000)
        append_log(log_path, {"event": "step.start", "step_id": step_id, "command": command, "report": str(report.relative_to(ROOT))})
        proc = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3600)
        completed_ms = int(time.time() * 1000)
        if proc.returncode != 0:
            failure = {
                "status": "failed",
                "step_id": step_id,
                "command": command,
                "returncode": proc.returncode,
                "started_ms": started_ms,
                "completed_ms": completed_ms,
                "elapsed_ms": completed_ms - started_ms,
                "stdout_sha256": sha256_bytes(proc.stdout),
                "stderr_sha256": sha256_bytes(proc.stderr),
                "stdout_tail": proc.stdout.decode("utf-8", "replace")[-2000:],
                "stderr_tail": proc.stderr.decode("utf-8", "replace")[-4000:],
            }
            state["steps"][step_id] = failure
            atomic_write_json(state_path, state)
            append_log(log_path, {"event": "step.failed", **failure})
            raise PipelineError("PIPELINE_STEP_FAILED", f"pipeline step {step_id} failed", failure)
        if report_ok(report) is None:
            failure = {
                "status": "failed",
                "step_id": step_id,
                "command": command,
                "reason": "missing_or_non_ok_report",
                "report": str(report.relative_to(ROOT)),
                "stdout_sha256": sha256_bytes(proc.stdout),
                "stderr_sha256": sha256_bytes(proc.stderr),
            }
            state["steps"][step_id] = failure
            atomic_write_json(state_path, state)
            append_log(log_path, {"event": "step.failed", **failure})
            raise PipelineError("PIPELINE_STEP_REPORT_INVALID", f"pipeline step {step_id} did not write an ok report", failure)
        record = completed_record(step_id, command, report, started_ms, completed_ms, proc.stdout, proc.stderr)
        state["steps"][step_id] = record
        atomic_write_json(state_path, state)
        append_log(log_path, {"event": "step.complete", "step_id": step_id, "report": record["report"], "elapsed_ms": record["elapsed_ms"]})
        results.append({"step_id": step_id, "status": "completed", "report": record["report"], "elapsed_ms": record["elapsed_ms"]})
    final_state = load_state(state_path)
    summary = {
        "status": "ok",
        "schema": "soccer-lab-pipeline-report-v1",
        "out_dir": str(out_dir.relative_to(ROOT)),
        "state": file_readback(state_path),
        "log": file_readback(log_path),
        "steps": results,
        "completed_steps": sorted(step_id for step_id, item in final_state["steps"].items() if item.get("status") == "completed"),
        "skipped_steps": [item["step_id"] for item in results if item["status"] == "skipped"],
    }
    atomic_write_json(report_path, summary)
    append_log(log_path, {"event": "pipeline.complete", "report": file_readback(report_path)})
    return summary | {"pipeline_report": file_readback(report_path)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW.relative_to(ROOT)))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR.relative_to(ROOT)))
    parser.add_argument("--plan", default="", help="optional JSON plan with a steps array")
    parser.add_argument("--force", action="store_true", help="rerun completed steps instead of resuming")
    return parser.parse_args()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    try:
        raw_root = resolve(args.raw_root)
        out_dir = resolve(args.out_dir)
        plan_path = resolve(args.plan) if args.plan else None
        result = run_pipeline(raw_root, out_dir, plan_path, args.force)
    except PipelineError as error:
        print(
            json.dumps(
                {"status": "error", "code": error.code, "message": error.message, "detail": error.detail},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "out_dir": result["out_dir"],
                "completed_steps": result["completed_steps"],
                "skipped_steps": result["skipped_steps"],
                "pipeline_report": result["pipeline_report"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
