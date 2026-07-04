#!/usr/bin/env python3
"""Verify the Soccer Lab pipeline driver is logged, resumable, and fail-closed."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import verify_soccer_lab_anchored_outcomes as anchored


ROOT = anchored.ROOT
DRIVER = ROOT / "tools" / "data" / "build_soccer_lab_pipeline.py"
DEFAULT_RAW = anchored.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "pipeline_driver" / "report.json"
DEFAULT_STEPS = [
    "teams_history_vault",
    "matches_vault",
    "players_vault",
    "bits_assay",
    "weave_loom",
    "kernel_build",
    "guard_calibrate",
    "rebuild_search_index",
]


class PipelineDriverFsvError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def run(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_driver(out_dir: Path, raw_root: Path, extra: list[str] | None = None, timeout: int = 3600) -> subprocess.CompletedProcess[bytes]:
    args = [sys.executable, str(DRIVER), "--raw-root", str(raw_root.relative_to(ROOT)), "--out-dir", str(out_dir.relative_to(ROOT))]
    if extra:
        args.extend(extra)
    return run(args, timeout=timeout)


def run_driver_ok(out_dir: Path, raw_root: Path, extra: list[str] | None = None, timeout: int = 3600) -> subprocess.CompletedProcess[bytes]:
    proc = run_driver(out_dir, raw_root, extra, timeout)
    if proc.returncode != 0:
        raise PipelineDriverFsvError(
            "driver_failed",
            {
                "args": [sys.executable, str(DRIVER), "--out-dir", str(out_dir.relative_to(ROOT)), *(extra or [])],
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def verify_real_driver(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    out_dir = work_dir / "real_pipeline"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    first = run_driver_ok(out_dir, raw_root, timeout=7200)
    first_stdout = json.loads(first.stdout)
    first_report = verify_pipeline_report(out_dir, expected_skipped=[])
    first_state_sha = first_report["state"]["sha256"]
    step_reports = verify_default_step_reports(out_dir)
    first_logs = read_log(out_dir / "pipeline.jsonl")

    second = run_driver_ok(out_dir, raw_root, timeout=600)
    second_stdout = json.loads(second.stdout)
    second_report = verify_pipeline_report(out_dir, expected_skipped=DEFAULT_STEPS)
    second_logs = read_log(out_dir / "pipeline.jsonl")
    if first_state_sha != second_report["state"]["sha256"]:
        raise PipelineDriverFsvError("resume_state_changed", {"first": first_state_sha, "second": second_report["state"]["sha256"]})
    skip_events = [record for record in second_logs if record.get("event") == "step.skip"]
    if len(skip_events) < len(DEFAULT_STEPS):
        raise PipelineDriverFsvError("resume_skip_events_missing", {"skip_events": len(skip_events), "logs": second_logs[-20:]})
    return {
        "out_dir": str(out_dir.relative_to(ROOT)),
        "first_stdout": first_stdout,
        "first_stdout_sha256": anchored.sha256_bytes(first.stdout),
        "first_stderr_sha256": anchored.sha256_bytes(first.stderr),
        "second_stdout": second_stdout,
        "second_stdout_sha256": anchored.sha256_bytes(second.stdout),
        "second_stderr_sha256": anchored.sha256_bytes(second.stderr),
        "first_report": first_report,
        "second_report": second_report,
        "step_reports": step_reports,
        "log_event_counts": count_events(second_logs),
        "first_log_records": len(first_logs),
        "second_log_records": len(second_logs),
    }


def verify_pipeline_report(out_dir: Path, expected_skipped: list[str]) -> dict[str, Any]:
    report_path = out_dir / "pipeline-report.json"
    state_path = out_dir / "pipeline-state.json"
    log_path = out_dir / "pipeline.jsonl"
    for path in [report_path, state_path, log_path]:
        if not path.is_file():
            raise PipelineDriverFsvError("driver_artifact_missing", {"path": str(path.relative_to(ROOT))})
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "ok" or report.get("schema") != "soccer-lab-pipeline-report-v1":
        raise PipelineDriverFsvError("pipeline_report_invalid", {"report": report})
    if sorted(report.get("completed_steps") or []) != sorted(DEFAULT_STEPS):
        raise PipelineDriverFsvError("pipeline_completed_steps_mismatch", {"report": report})
    if sorted(report.get("skipped_steps") or []) != sorted(expected_skipped):
        raise PipelineDriverFsvError("pipeline_skipped_steps_mismatch", {"observed": report.get("skipped_steps"), "expected": expected_skipped})
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema") != "soccer-lab-pipeline-state-v1":
        raise PipelineDriverFsvError("pipeline_state_schema_mismatch", {"state": state})
    for step_id in DEFAULT_STEPS:
        item = (state.get("steps") or {}).get(step_id)
        if not item or item.get("status") != "completed":
            raise PipelineDriverFsvError("pipeline_state_step_missing", {"step": step_id, "state": state})
        report_file = ROOT / item["report"]["path"]
        if not report_file.is_file() or file_readback(report_file)["sha256"] != item["report"]["sha256"]:
            raise PipelineDriverFsvError("pipeline_state_report_readback_mismatch", {"step": step_id, "item": item})
    return {
        "file": file_readback(report_path),
        "state": file_readback(state_path),
        "log": file_readback(log_path),
        "completed_steps": report["completed_steps"],
        "skipped_steps": report["skipped_steps"],
        "step_count": len(report["steps"]),
    }


def verify_default_step_reports(out_dir: Path) -> dict[str, Any]:
    reports = {}
    for step_id in DEFAULT_STEPS:
        path = out_dir / "reports" / f"{step_id}.json"
        if not path.is_file():
            raise PipelineDriverFsvError("step_report_missing", {"step": step_id, "path": str(path.relative_to(ROOT))})
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "ok":
            raise PipelineDriverFsvError("step_report_not_ok", {"step": step_id, "payload": payload})
        reports[step_id] = summarize_step_report(step_id, path, payload)
    return reports


def summarize_step_report(step_id: str, path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    out = {"file": file_readback(path)}
    if "generation" in payload:
        generation = payload["generation"]
        out["rows"] = generation.get("rows") or generation.get("selected_rows")
        out["axis_counts"] = generation.get("selected_axis_counts") or generation.get("anchor_counts")
    if "vault" in payload:
        vault = payload["vault"]
        out["vault_id"] = vault.get("vault_id")
        out["cx_list_rows"] = vault.get("cx_list_rows")
    if step_id == "bits_assay":
        out["assay_unique_keys"] = payload["bits"]["assay_cf_unique_keys"]
        out["axes"] = sorted(payload["bits"]["decoded_axes"])
    if step_id == "weave_loom":
        out["graph_counts"] = payload["vault"]["graph_readback"]["kind_counts"]
    if step_id == "kernel_build":
        out["recall_ratio"] = payload["real"]["kernel_report"]["recall"]["ratio"]
        out["kernel_members"] = payload["real"]["kernel_report"]["kernel"]["members"]
    if step_id == "guard_calibrate":
        out["guard_unique_rows"] = payload["real"]["guard_cf_readback"]["unique_rows"]
        out["tau"] = payload["real"]["command_report"]["tau"]
    if step_id == "rebuild_search_index":
        out["sidecar_slots"] = payload["real"]["manifest_readback"]["expected_slots"]
        out["search_rank"] = payload["real"]["search_readback"]["matched_rank"]
    return out


def synthetic_edges(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    helper_ok = write_helper(work_dir / "ok_step.py", "ok")
    helper_fail = write_helper(work_dir / "fail_step.py", "fail")
    helper_bad_report = write_helper(work_dir / "bad_report_step.py", "bad_report")
    return {
        "synthetic_happy_resume": synthetic_happy_resume(work_dir / "happy_resume", raw_root, helper_ok),
        "empty_plan": synthetic_empty_plan(work_dir / "empty_plan", raw_root),
        "missing_script": synthetic_bad_plan(work_dir / "missing_script", raw_root, [[sys.executable, str(work_dir / "missing.py"), "{report}"]]),
        "failing_script": synthetic_bad_plan(work_dir / "failing_script", raw_root, [[sys.executable, str(helper_fail), "{report}"]]),
        "bad_report": synthetic_bad_plan(work_dir / "bad_report", raw_root, [[sys.executable, str(helper_bad_report), "{report}"]]),
    }


def write_helper(path: Path, mode: str) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "report=pathlib.Path(sys.argv[1])\n"
        "report.parent.mkdir(parents=True, exist_ok=True)\n"
        f"mode={mode!r}\n"
        "if mode == 'fail':\n"
        "    print(json.dumps({'status':'error','reason':'synthetic'}), file=sys.stderr)\n"
        "    raise SystemExit(7)\n"
        "if mode == 'bad_report':\n"
        "    report.write_text(json.dumps({'status':'not-ok'})+'\\n', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "report.write_text(json.dumps({'status':'ok','mode':mode}, sort_keys=True)+'\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o100)
    return path


def synthetic_happy_resume(work_dir: Path, raw_root: Path, helper: Path) -> dict[str, Any]:
    plan = write_plan(work_dir / "plan.json", [["synthetic_ok", [sys.executable, str(helper), "{report}"]]])
    first = run_driver_ok(work_dir / "out", raw_root, ["--plan", str(plan.relative_to(ROOT))], timeout=120)
    first_report = json.loads((work_dir / "out" / "pipeline-report.json").read_text(encoding="utf-8"))
    second = run_driver_ok(work_dir / "out", raw_root, ["--plan", str(plan.relative_to(ROOT))], timeout=120)
    second_report = json.loads((work_dir / "out" / "pipeline-report.json").read_text(encoding="utf-8"))
    if second_report.get("skipped_steps") != ["synthetic_ok"]:
        raise PipelineDriverFsvError("synthetic_resume_skip_mismatch", {"report": second_report})
    return {
        "plan": file_readback(plan),
        "first_stdout_sha256": anchored.sha256_bytes(first.stdout),
        "second_stdout_sha256": anchored.sha256_bytes(second.stdout),
        "first_report": {"completed_steps": first_report["completed_steps"], "skipped_steps": first_report["skipped_steps"]},
        "second_report": {"completed_steps": second_report["completed_steps"], "skipped_steps": second_report["skipped_steps"]},
        "state": file_readback(work_dir / "out" / "pipeline-state.json"),
        "log": file_readback(work_dir / "out" / "pipeline.jsonl"),
    }


def synthetic_empty_plan(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    plan = work_dir / "empty.json"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(json.dumps({"steps": []}, sort_keys=True) + "\n", encoding="utf-8")
    before = directory_readback(work_dir)
    proc = run_driver(work_dir / "out", raw_root, ["--plan", str(plan.relative_to(ROOT))], timeout=120)
    after = directory_readback(work_dir)
    return assert_failed("empty_plan", proc) | {"before": before, "after": after, "pipeline_report_exists": (work_dir / "out" / "pipeline-report.json").exists()}


def synthetic_bad_plan(work_dir: Path, raw_root: Path, commands: list[list[str]]) -> dict[str, Any]:
    step_rows = [(f"bad_{index}", command) for index, command in enumerate(commands)]
    plan = write_plan(work_dir / "plan.json", step_rows)
    before = directory_readback(work_dir)
    proc = run_driver(work_dir / "out", raw_root, ["--plan", str(plan.relative_to(ROOT))], timeout=120)
    after = directory_readback(work_dir)
    state_path = work_dir / "out" / "pipeline-state.json"
    log_path = work_dir / "out" / "pipeline.jsonl"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if (work_dir / "out" / "pipeline-report.json").exists():
        raise PipelineDriverFsvError("synthetic_failure_wrote_pipeline_report", {"work_dir": str(work_dir.relative_to(ROOT))})
    return assert_failed("bad_plan", proc) | {
        "plan": file_readback(plan),
        "before": before,
        "after": after,
        "state": file_readback(state_path) if state_path.exists() else None,
        "log": file_readback(log_path) if log_path.exists() else None,
        "state_statuses": {step_id: item.get("status") for step_id, item in (state.get("steps") or {}).items()},
    }


def write_plan(path: Path, step_rows: list[tuple[str, list[str]]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "steps": [
            {"id": step_id, "command": command, "report": "{out_dir}/reports/" + step_id + ".json"}
            for step_id, command in step_rows
        ]
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def assert_failed(name: str, proc: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
    if proc.returncode == 0:
        raise PipelineDriverFsvError("synthetic_edge_passed", {"case": name, "stdout": proc.stdout.decode("utf-8", "replace")})
    return {
        "returncode": proc.returncode,
        "stdout_sha256": anchored.sha256_bytes(proc.stdout),
        "stderr_sha256": anchored.sha256_bytes(proc.stderr),
        "stderr_fragment": proc.stderr.decode("utf-8", "replace")[-800:],
    }


def read_log(path: Path) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(record.get("schema") != "soccer-lab-pipeline-log-v1" for record in records):
        raise PipelineDriverFsvError("pipeline_log_schema_mismatch", {"path": str(path.relative_to(ROOT)), "sample": records[:3]})
    return records


def count_events(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        event = str(record.get("event"))
        counts[event] = counts.get(event, 0) + 1
    return dict(sorted(counts.items()))


def directory_readback(path: Path) -> dict[str, Any]:
    files = sorted(child.relative_to(path).as_posix() for child in path.rglob("*") if child.is_file()) if path.exists() else []
    return {"path": str(path.relative_to(ROOT)), "exists": path.exists(), "files": files}


def file_readback(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": len(data),
        "sha256": anchored.sha256_bytes(data),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW.relative_to(ROOT)))
    parser.add_argument("--out", default=str(DEFAULT_OUT.relative_to(ROOT)))
    return parser.parse_args()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    raw_root = resolve(args.raw_root)
    report_path = resolve(args.out)
    work_dir = report_path.parent
    if work_dir.exists():
        shutil.rmtree(work_dir)
    real = verify_real_driver(work_dir, raw_root)
    synthetic = synthetic_edges(work_dir / "synthetic_edges", raw_root)
    report = {"status": "ok", "real": real, "synthetic": synthetic}
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    if report_path.read_text(encoding="utf-8") != encoded + "\n":
        raise PipelineDriverFsvError("report_readback_mismatch", {"path": str(report_path.relative_to(ROOT))})
    print(
        json.dumps(
            {
                "status": "ok",
                "completed_steps": real["second_report"]["completed_steps"],
                "skipped_steps": real["second_report"]["skipped_steps"],
                "step_reports": sorted(real["step_reports"]),
                "synthetic_edges": sorted(key for key in synthetic if key != "synthetic_happy_resume"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
