#!/usr/bin/env python3
"""FSV for the Soccer Lab daily refresh scheduler wrapper."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REFRESH = ROOT / "tools/data/run_soccer_lab_daily_refresh.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fsv-root", required=True)
    args = parser.parse_args()
    root = resolve(args.fsv_root)
    if root.exists():
        import shutil

        shutil.rmtree(root)
    root.mkdir(parents=True)

    happy_plan = write_plan(root, "happy", happy_steps(root))
    first = run_refresh(root, "happy-first", happy_plan)
    second = run_refresh(root, "happy-second", happy_plan)
    assert_success(first, "happy first")
    assert_success(second, "happy second")
    first_report = read_json(root / "happy-first" / "daily-refresh-report.json")
    second_report = read_json(root / "happy-second" / "daily-refresh-report.json")
    assert first_report["status"] == "ok"
    assert second_report["status"] == "ok"
    assert [step["step_id"] for step in first_report["steps"]] == [
        "acquire_sources",
        "provenance_write",
        "pipeline_build",
        "premerge_fsv_gate",
    ]
    assert [step["step_id"] for step in second_report["steps"]] == [
        step["step_id"] for step in first_report["steps"]
    ]

    edges = [
        {
            "case": "happy_idempotent_second_run",
            "expected": "ok",
            "observed": second_report["status"],
        },
        edge(root, "empty_plan", {"steps": []}, "CALYX_SOCCER_LAB_DAILY_REFRESH_INVALID_CONFIG"),
        edge(root, "failing_command", {"steps": [failing_step(root)]}, "CALYX_SOCCER_LAB_DAILY_REFRESH_STEP_FAILED"),
        edge(root, "missing_report", {"steps": [missing_report_step(root)]}, "CALYX_SOCCER_LAB_DAILY_REFRESH_REPORT_MISSING"),
        timeout_edge(root),
    ]
    readback = {
        "status": "ok",
        "surface": "soccer_lab.daily_refresh",
        "source_of_truth": "daily refresh physical reports plus structured JSONL logs",
        "first_report": first_report,
        "second_report": second_report,
        "edges": edges,
    }
    write_json(root / "daily-refresh-readback.json", readback)
    write_manifest(
        root,
        [
            root / "happy-first" / "daily-refresh-report.json",
            root / "happy-first" / "daily-refresh.jsonl",
            root / "happy-second" / "daily-refresh-report.json",
            root / "happy-second" / "daily-refresh.jsonl",
            root / "daily-refresh-readback.json",
        ],
    )
    print(json.dumps(readback, indent=2, sort_keys=True))
    return 0


def happy_steps(root: Path) -> list[dict[str, Any]]:
    return [
        ok_step("acquire_sources", root / "reports/acquire.json", "acquired"),
        ok_step("provenance_write", root / "reports/provenance.json", "grounded"),
        ok_step("pipeline_build", root / "reports/pipeline.json", "predicted"),
        ok_step("premerge_fsv_gate", root / "reports/gate.json", "verified"),
    ]


def ok_step(step_id: str, report: Path, value: str) -> dict[str, Any]:
    code = (
        "import json,pathlib,sys;"
        "p=pathlib.Path(sys.argv[1]);"
        "p.parent.mkdir(parents=True,exist_ok=True);"
        "p.write_text(json.dumps({'status':'ok','value':sys.argv[2]},sort_keys=True)+'\\n',encoding='utf-8')"
    )
    return {
        "id": step_id,
        "command": [sys.executable, "-c", code, str(report), value],
        "report": str(report),
    }


def failing_step(root: Path) -> dict[str, Any]:
    return {
        "id": "failing_command",
        "command": [sys.executable, "-c", "raise SystemExit(9)"],
        "report": str(root / "reports/failing.json"),
    }


def missing_report_step(root: Path) -> dict[str, Any]:
    return {
        "id": "missing_report",
        "command": [sys.executable, "-c", "print('no report written')"],
        "report": str(root / "reports/missing.json"),
    }


def edge(root: Path, name: str, plan: dict[str, Any], expected: str) -> dict[str, Any]:
    plan_path = root / f"{name}-plan.json"
    write_json(plan_path, plan)
    output = run_refresh(root, name, plan_path)
    report = read_json(root / name / "daily-refresh-report.json")
    observed = report.get("code")
    return {
        "case": name,
        "expected": expected,
        "observed": observed,
        "exit_code": output.returncode,
    }


def timeout_edge(root: Path) -> dict[str, Any]:
    plan = root / "timeout-plan.json"
    write_json(plan, {"steps": [ok_step("timeout_config", root / "reports/timeout.json", "x")]})
    out_dir = root / "timeout_config"
    output = subprocess.run(
        [
            sys.executable,
            str(REFRESH),
            "--plan",
            str(plan),
            "--out-dir",
            str(out_dir),
            "--timeout-secs",
            "0",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    report = read_json(out_dir / "daily-refresh-report.json")
    return {
        "case": "invalid_timeout",
        "expected": "CALYX_SOCCER_LAB_DAILY_REFRESH_INVALID_CONFIG",
        "observed": report.get("code"),
        "exit_code": output.returncode,
    }


def write_plan(root: Path, name: str, steps: list[dict[str, Any]]) -> Path:
    path = root / f"{name}-plan.json"
    write_json(path, {"steps": steps})
    return path


def run_refresh(root: Path, name: str, plan: Path) -> subprocess.CompletedProcess[str]:
    out_dir = root / name
    return subprocess.run(
        [
            sys.executable,
            str(REFRESH),
            "--plan",
            str(plan),
            "--out-dir",
            str(out_dir),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_success(output: subprocess.CompletedProcess[str], label: str) -> None:
    if output.returncode != 0:
        raise AssertionError(f"{label} failed: stdout={output.stdout} stderr={output.stderr}")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_manifest(root: Path, files: list[Path]) -> None:
    lines = []
    for path in files:
        data = path.read_bytes()
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {path.relative_to(root)}")
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


if __name__ == "__main__":
    raise SystemExit(main())
