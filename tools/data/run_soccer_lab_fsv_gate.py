#!/usr/bin/env python3
"""Pre-merge FSV gate for Soccer Lab real-data verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_ROOT = ROOT / "scratchpad" / "wc2026" / "raw"
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "premerge_gate" / "report.json"
DEFAULT_PREVIEW_URL = "http://127.0.0.1:4173"
WEB_API_URL = "http://127.0.0.1:8121"


class GateError(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_readback(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": display_path(path),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def write_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8")
    if path.read_text(encoding="utf-8") != encoded:
        raise GateError("GATE_REPORT_READBACK_MISMATCH", "report bytes changed after write", {"path": str(path)})
    return file_readback(path)


def emit(log_path: Path, event: str, **payload: Any) -> None:
    record = {
        "schema": "soccer-lab-fsv-gate-log-v1",
        "event": event,
        "ts_ms": int(time.time() * 1000),
        **payload,
    }
    encoded = json.dumps(record, sort_keys=True)
    print(encoded, flush=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(encoded + "\n")


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def require_path(path: Path, kind: str) -> dict[str, Any]:
    if kind == "file" and not path.is_file():
        raise GateError("GATE_REQUIRED_FILE_MISSING", "required file is missing", {"path": str(path)})
    if kind == "dir" and not path.is_dir():
        raise GateError("GATE_REQUIRED_DIR_MISSING", "required directory is missing", {"path": str(path)})
    return file_readback(path) if path.is_file() else {"path": display_path(path), "exists": True}


def run_command(
    name: str,
    command: list[str],
    log_path: Path,
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
    timeout: int = 3600,
) -> dict[str, Any]:
    started = time.monotonic()
    emit(log_path, "step.start", name=name, command=command, cwd=display_path(cwd))
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        detail = {
            "name": name,
            "command": command,
            "elapsed_ms": elapsed_ms,
            "timeout_secs": timeout,
            "stdout_tail": (error.stdout or b"").decode("utf-8", "replace")[-4000:],
            "stderr_tail": (error.stderr or b"").decode("utf-8", "replace")[-8000:],
        }
        emit(log_path, "step.timeout", **detail)
        raise GateError("GATE_COMMAND_TIMEOUT", f"{name} timed out", detail) from error
    elapsed_ms = int((time.monotonic() - started) * 1000)
    summary = {
        "name": name,
        "command": command,
        "returncode": proc.returncode,
        "elapsed_ms": elapsed_ms,
        "stdout_sha256": sha256_bytes(proc.stdout),
        "stderr_sha256": sha256_bytes(proc.stderr),
        "stdout_tail": proc.stdout.decode("utf-8", "replace")[-4000:],
        "stderr_tail": proc.stderr.decode("utf-8", "replace")[-8000:],
    }
    if proc.returncode != 0:
        emit(log_path, "step.failed", **summary)
        raise GateError("GATE_COMMAND_FAILED", f"{name} failed", summary)
    emit(log_path, "step.ok", **{key: value for key, value in summary.items() if not key.endswith("_tail")})
    return summary


def wait_tcp(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as error:
            last_error = str(error)
            time.sleep(0.2)
    raise GateError("GATE_SERVICE_UNAVAILABLE", "service did not accept TCP connections", {"host": host, "port": port, "last_error": last_error})


def wait_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if 200 <= response.status < 500:
                    return
        except OSError as error:
            last_error = str(error)
            time.sleep(0.2)
    raise GateError("GATE_SERVICE_UNAVAILABLE", "service did not return HTTP", {"url": url, "last_error": last_error})


def start_process(name: str, command: list[str], log_path: Path, *, env: dict[str, str], cwd: Path = ROOT) -> subprocess.Popen[bytes]:
    emit(log_path, "process.start", name=name, command=command)
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def stop_process(name: str, proc: subprocess.Popen[bytes], log_path: Path) -> dict[str, Any]:
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate(timeout=10)
    else:
        stdout, stderr = proc.communicate(timeout=1)
    summary = {
        "name": name,
        "returncode": proc.returncode,
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
        "stdout_tail": stdout.decode("utf-8", "replace")[-2000:],
        "stderr_tail": stderr.decode("utf-8", "replace")[-4000:],
    }
    emit(log_path, "process.stop", **{key: value for key, value in summary.items() if not key.endswith("_tail")})
    return summary


def synthetic_edges(work_dir: Path, log_path: Path) -> dict[str, Any]:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    happy = run_command(
        "synthetic_happy_command",
        [sys.executable, "-c", "print('synthetic ok')"],
        log_path,
        timeout=30,
    )
    edges: dict[str, Any] = {"happy_command": happy}
    for name, func in {
        "missing_required_file": lambda: require_path(work_dir / "missing.json", "file"),
        "failing_command": lambda: run_command(
            "synthetic_failing_command",
            [sys.executable, "-c", "raise SystemExit(7)"],
            log_path,
            timeout=30,
        ),
        "unavailable_tcp_service": lambda: wait_tcp("127.0.0.1", 9, 0.1),
    }.items():
        try:
            func()
        except GateError as error:
            edges[name] = {"code": error.code, "message": error.message, "detail": error.detail}
        else:
            raise GateError("GATE_SYNTHETIC_EDGE_PASSED", "synthetic edge unexpectedly passed", {"case": name})
    return edges


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def infer_api_vault(physical_report: Path) -> dict[str, str]:
    rebuild_report = physical_report.parent / "pipeline" / "reports" / "rebuild_search_index" / "report.json"
    require_path(rebuild_report, "file")
    payload = load_json(rebuild_report)
    vault_path = payload["real"]["vault"]["vault_path"]
    vault_name = payload["real"]["rebuild_report"]["vault"]
    vault_dir = resolve(vault_path)
    require_path(vault_dir, "dir")
    return {
        "vault_dir": str(vault_dir),
        "vault_name": str(vault_name),
        "source_report": str(rebuild_report.relative_to(ROOT)),
        "source_report_sha256": file_readback(rebuild_report)["sha256"],
    }


def run_live_ui_gate(out_dir: Path, log_path: Path, vault: dict[str, str]) -> dict[str, Any]:
    dashboard = ROOT / "apps" / "soccer-lab-dashboard"
    prediction_export = ROOT / "docs" / "data" / "soccer_lab_prediction_export.json"
    require_path(prediction_export, "file")
    bearer = os.environ.get("CALYX_SOCCER_LAB_GATE_BEARER", "soccer-lab-fsv-premerge-secret")
    env = os.environ.copy()
    env.update(
        {
            "CALYX_WEB_API_VAULT_DIR": vault["vault_dir"],
            "CALYX_WEB_API_VAULT_NAME": vault["vault_name"],
            "CALYX_WEB_API_PREDICTION_EXPORT": str(prediction_export),
            "CALYX_WEB_API_BEARER_SECRET": bearer,
            "CALYX_WEB_API_CACHE_TTL_SECS": "0",
        }
    )
    api = start_process("calyx-web-api", [str(ROOT / "target" / "debug" / "calyx-web-api")], log_path, env=env)
    preview: subprocess.Popen[bytes] | None = None
    try:
        wait_tcp("127.0.0.1", 8121, 20)
        preview_env = os.environ.copy()
        preview_env.update(
            {
                "CALYX_WEB_API_PROXY_TARGET": WEB_API_URL,
                "CALYX_WEB_API_BEARER_SECRET": bearer,
                "CALYX_DASHBOARD_PREVIEW_PORT": "4173",
            }
        )
        preview = start_process(
            "soccer-lab-dashboard-preview",
            ["npm", "run", "serve:deploy-preview"],
            log_path,
            cwd=dashboard,
            env=preview_env,
        )
        wait_http(f"{DEFAULT_PREVIEW_URL}/", 20)
        deploy = run_command(
            "dashboard_verify_deploy_preview",
            ["npm", "run", "verify:deploy-preview"],
            log_path,
            cwd=dashboard,
            env=os.environ.copy() | {"CALYX_DASHBOARD_PREVIEW_URL": DEFAULT_PREVIEW_URL},
            timeout=120,
        )
        e2e = run_command(
            "dashboard_verify_e2e_live",
            ["npm", "run", "verify:e2e-live"],
            log_path,
            cwd=dashboard,
            env=os.environ.copy() | {"CALYX_DASHBOARD_PREVIEW_URL": DEFAULT_PREVIEW_URL},
            timeout=180,
        )
        return {"deploy_preview": deploy, "e2e_live": e2e}
    finally:
        stopped = {}
        if preview is not None:
            stopped["preview"] = stop_process("soccer-lab-dashboard-preview", preview, log_path)
        stopped["api"] = stop_process("calyx-web-api", api, log_path)
        write_json(out_dir / "live-ui-processes.json", stopped)


def run_gate(raw_root: Path, report_path: Path) -> dict[str, Any]:
    out_dir = report_path.parent
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    log_path = out_dir / "gate.jsonl"
    emit(log_path, "gate.start", raw_root=display_path(raw_root), report=display_path(report_path))
    inputs = {
        "raw_root": require_path(raw_root, "dir"),
        "release_calyx": require_path(ROOT / "target" / "release" / "calyx", "file"),
        "debug_web_api": require_path(ROOT / "target" / "debug" / "calyx-web-api", "file"),
        "prediction_export": require_path(ROOT / "docs" / "data" / "soccer_lab_prediction_export.json", "file"),
    }
    synthetic = synthetic_edges(out_dir / "synthetic_edges", log_path)
    steps = {
        "physical_cf_harness": run_command(
            "physical_cf_harness",
            [
                sys.executable,
                "tools/data/verify_soccer_lab_physical_cf_harness.py",
                "--raw-root",
                display_path(raw_root),
                "--out",
                display_path(out_dir / "physical_cf_harness" / "report.json"),
            ],
            log_path,
            timeout=7200,
        ),
        "pipeline_driver": run_command(
            "pipeline_driver",
            [
                sys.executable,
                "tools/data/verify_soccer_lab_pipeline_driver.py",
                "--raw-root",
                display_path(raw_root),
                "--out",
                display_path(out_dir / "pipeline_driver" / "report.json"),
            ],
            log_path,
            timeout=7200,
        ),
        "prediction_export_schema": run_command(
            "prediction_export_schema",
            [
                sys.executable,
                "tools/data/verify_soccer_lab_prediction_export_schema.py",
                "--out",
                display_path(out_dir / "prediction_export_schema" / "report.json"),
            ],
            log_path,
            timeout=600,
        ),
        "dashboard_lint": run_command("dashboard_lint", ["npm", "run", "lint"], log_path, cwd=ROOT / "apps" / "soccer-lab-dashboard", timeout=300),
        "dashboard_build": run_command(
            "dashboard_build",
            ["npm", "run", "build"],
            log_path,
            cwd=ROOT / "apps" / "soccer-lab-dashboard",
            env=os.environ.copy() | {"VITE_CALYX_WEB_API_BASE_URL": "/api"},
            timeout=600,
        ),
        "dashboard_verify_match": run_command("dashboard_verify_match", ["npm", "run", "verify:match-view"], log_path, cwd=ROOT / "apps" / "soccer-lab-dashboard", timeout=120),
        "dashboard_verify_bracket": run_command("dashboard_verify_bracket", ["npm", "run", "verify:bracket-view"], log_path, cwd=ROOT / "apps" / "soccer-lab-dashboard", timeout=120),
        "dashboard_verify_player": run_command("dashboard_verify_player", ["npm", "run", "verify:player-view"], log_path, cwd=ROOT / "apps" / "soccer-lab-dashboard", timeout=120),
        "dashboard_verify_explainability": run_command("dashboard_verify_explainability", ["npm", "run", "verify:explainability-view"], log_path, cwd=ROOT / "apps" / "soccer-lab-dashboard", timeout=120),
    }
    vault = infer_api_vault(out_dir / "physical_cf_harness" / "report.json")
    steps["live_ui"] = run_live_ui_gate(out_dir, log_path, vault)
    report = {
        "status": "ok",
        "inputs": inputs,
        "synthetic_edges": synthetic,
        "steps": steps,
        "api_vault": vault,
        "log": file_readback(log_path),
    }
    report["report"] = write_json(report_path, report)
    emit(log_path, "gate.ok", report=report["report"])
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=os.environ.get("CALYX_SOCCER_LAB_RAW_ROOT", str(DEFAULT_RAW_ROOT.relative_to(ROOT))))
    parser.add_argument("--out", default=os.environ.get("CALYX_SOCCER_LAB_GATE_OUT", str(DEFAULT_OUT.relative_to(ROOT))))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_path = resolve(args.out)
    log_path = report_path.parent / "gate.jsonl"
    try:
        report = run_gate(resolve(args.raw_root), report_path)
    except GateError as error:
        emit(log_path, "gate.failed", code=error.code, message=error.message, detail=error.detail)
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": error.code,
                    "message": error.message,
                    "detail": error.detail,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception as error:  # noqa: BLE001 - last-resort structured gate failure.
        detail = {"error_type": type(error).__name__, "message": str(error)}
        emit(log_path, "gate.failed", code="GATE_UNHANDLED_EXCEPTION", message="unhandled gate exception", detail=detail)
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": "GATE_UNHANDLED_EXCEPTION",
                    "message": "unhandled gate exception",
                    "detail": detail,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"status": "ok", "report": report["report"], "steps": sorted(report["steps"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
