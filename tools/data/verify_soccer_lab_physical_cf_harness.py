#!/usr/bin/env python3
"""Assert Soccer Lab pipeline stages against physical CF and file readbacks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import verify_soccer_lab_anchored_outcomes as anchored


ROOT = anchored.ROOT
CALYX = anchored.CALYX
PIPELINE = ROOT / "tools" / "data" / "build_soccer_lab_pipeline.py"
DEFAULT_RAW = anchored.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "physical_cf_harness" / "report.json"

PIPELINE_STEPS = [
    "teams_history_vault",
    "matches_vault",
    "players_vault",
    "bits_assay",
    "weave_loom",
    "kernel_build",
    "guard_calibrate",
    "rebuild_search_index",
]


class PhysicalFsvError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 3600) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str] | None = None, reason: str = "command_failed", timeout: int = 3600) -> subprocess.CompletedProcess[bytes]:
    proc = run(args, env=env, timeout=timeout)
    if proc.returncode != 0:
        raise PhysicalFsvError(
            reason,
            {
                "args": args,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def run_pipeline(out_dir: Path, raw_root: Path) -> dict[str, Any]:
    proc = run_ok(
        [
            sys.executable,
            str(PIPELINE),
            "--raw-root",
            str(raw_root.relative_to(ROOT)),
            "--out-dir",
            str(out_dir.relative_to(ROOT)),
        ],
        reason="pipeline_driver_failed",
        timeout=7200,
    )
    stdout = json.loads(proc.stdout)
    report_path = out_dir / "pipeline-report.json"
    state_path = out_dir / "pipeline-state.json"
    log_path = out_dir / "pipeline.jsonl"
    for path in [report_path, state_path, log_path]:
        if not path.is_file():
            raise PhysicalFsvError("pipeline_artifact_missing", {"path": str(path.relative_to(ROOT))})
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "ok":
        raise PhysicalFsvError("pipeline_report_not_ok", {"report": report})
    completed = sorted(report.get("completed_steps") or [])
    if completed != sorted(PIPELINE_STEPS):
        raise PhysicalFsvError("pipeline_steps_incomplete", {"completed": completed})
    return {
        "stdout": stdout,
        "stdout_sha256": anchored.sha256_bytes(proc.stdout),
        "stderr_sha256": anchored.sha256_bytes(proc.stderr),
        "report": file_readback(report_path),
        "state": file_readback(state_path),
        "log": file_readback(log_path),
        "completed_steps": completed,
    }


def assert_real_pipeline(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    pipeline_dir = work_dir / "pipeline"
    if pipeline_dir.exists():
        shutil.rmtree(pipeline_dir)
    pipeline = run_pipeline(pipeline_dir, raw_root)
    reports = {step: load_report(pipeline_dir, step) for step in PIPELINE_STEPS}
    out = {
        "pipeline": pipeline,
        "vault_stages": {
            "teams_history_vault": assert_vault_stage(reports["teams_history_vault"], 240),
            "matches_vault": assert_vault_stage(reports["matches_vault"], 85),
            "players_vault": assert_vault_stage(reports["players_vault"], 1248),
        },
        "bits_assay": assert_bits_stage(reports["bits_assay"]),
        "weave_loom": assert_weave_stage(reports["weave_loom"]),
        "kernel_build": assert_kernel_stage(reports["kernel_build"]),
        "guard_calibrate": assert_guard_stage(reports["guard_calibrate"]),
        "rebuild_search_index": assert_rebuild_stage(reports["rebuild_search_index"]),
    }
    return out


def load_report(pipeline_dir: Path, step: str) -> dict[str, Any]:
    path = pipeline_dir / "reports" / step / "report.json"
    if not path.is_file():
        raise PhysicalFsvError("stage_report_missing", {"step": step, "path": str(path.relative_to(ROOT))})
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != "ok":
        raise PhysicalFsvError("stage_report_not_ok", {"step": step, "payload": payload})
    return payload | {"_report_file": file_readback(path)}


def assert_vault_stage(report: dict[str, Any], expected_rows: int) -> dict[str, Any]:
    vault = report["vault"]
    vault_path = ROOT / vault["vault_path"]
    physical = vault["physical_vault_readback"]
    files = assert_file_map(physical["required_files"])
    cx = read_cx_list(vault_path, expected_rows)
    if len(cx) != expected_rows or int(vault["cx_list_rows"]) != expected_rows:
        raise PhysicalFsvError("vault_cx_count_mismatch", {"vault": vault["vault_name"], "cx": len(cx), "expected": expected_rows})
    slot_files = {}
    for facet, info in sorted(physical["slot_files"].items()):
        cf_dir = vault_path / "cf" / f"slot_{int(info['slot']):02d}"
        cf_files = sorted(cf_dir.glob("*.sst"))
        if not cf_files:
            raise PhysicalFsvError("vault_slot_cf_missing", {"facet": facet, "slot": info["slot"], "cf_dir": str(cf_dir.relative_to(ROOT))})
        actual_bytes = sum(path.stat().st_size for path in cf_files)
        if actual_bytes != int(info["cf_sst_bytes"]) or len(cf_files) != int(info["cf_sst_count"]):
            raise PhysicalFsvError("vault_slot_cf_stat_mismatch", {"facet": facet, "reported": info, "actual_files": len(cf_files), "actual_bytes": actual_bytes})
        dense = assert_file(info["dense_index"])
        slot_files[facet] = {"slot": info["slot"], "cf_sst_count": len(cf_files), "cf_sst_bytes": actual_bytes, "dense_index": dense}
    return {
        "vault_id": vault["vault_id"],
        "vault_path": vault["vault_path"],
        "cx_list_rows": len(cx),
        "cx_list_sha256": anchored.sha256_bytes(json.dumps(cx, sort_keys=True).encode("utf-8")),
        "required_files": files,
        "slot_files": slot_files,
    }


def assert_bits_stage(report: dict[str, Any]) -> dict[str, Any]:
    vault_path = ROOT / report["vault"]["vault_path"]
    physical = report["bits"]["physical_readback"]
    files = assert_file_map(physical["required_files"])
    cf_stats = assert_cf_sst_stats(vault_path, physical["cf"], required=["base", "anchors", "assay"])
    assay_cf = read_cf(vault_path, "assay")
    if assay_cf["raw_rows"] != int(report["bits"]["assay_cf_raw_rows"]) or assay_cf["unique_rows"] != int(report["bits"]["assay_cf_unique_keys"]):
        raise PhysicalFsvError("assay_cf_count_mismatch", {"readback": assay_cf, "report": report["bits"]})
    axes = {}
    for axis, detail in report["bits"]["decoded_axes"].items():
        if detail["key_hex"] not in assay_cf["latest_keys"]:
            raise PhysicalFsvError("assay_axis_key_missing", {"axis": axis, "key": detail["key_hex"], "available": assay_cf["latest_keys"]})
        axes[axis] = {"n": detail["n"], "slot_count": detail["slot_count"], "key_hex": detail["key_hex"]}
    return {"vault_path": report["vault"]["vault_path"], "required_files": files, "cf": cf_stats, "assay_cf": assay_cf, "axes": axes}


def assert_weave_stage(report: dict[str, Any]) -> dict[str, Any]:
    vault_path = ROOT / report["vault"]["vault_path"]
    physical = report["vault"]["physical_readback"]
    files = assert_file_map(physical["required_files"])
    cf_stats = assert_cf_sst_stats(vault_path, physical["cf"], required=["base", "anchors", "xterm", "graph"])
    xterm = read_cf(vault_path, "xterm")
    graph = read_cf(vault_path, "graph")
    expected_xterm = int(report["vault"]["xterm_readback"]["raw_rows"])
    expected_graph = int(report["vault"]["graph_readback"]["raw_rows"])
    if xterm["raw_rows"] != expected_xterm or graph["raw_rows"] != expected_graph:
        raise PhysicalFsvError("weave_cf_count_mismatch", {"xterm": xterm, "graph": graph, "report": report["vault"]})
    return {
        "vault_path": report["vault"]["vault_path"],
        "required_files": files,
        "cf": cf_stats,
        "xterm_cf": xterm,
        "graph_cf": graph,
        "graph_kind_counts": report["vault"]["graph_readback"]["kind_counts"],
    }


def assert_kernel_stage(report: dict[str, Any]) -> dict[str, Any]:
    real = report["real"]
    vault_path = ROOT / real["vault"]["vault_path"]
    physical = real["vault"]["physical_readback"]
    files = assert_file_map(physical["required_files"])
    cf_stats = assert_cf_sst_stats(vault_path, physical["cf"], required=["base", "anchors", "xterm", "graph"])
    graph = read_cf(vault_path, "graph")
    if graph["raw_rows"] != int(real["graph_readback_after_kernel"]["raw_rows"]):
        raise PhysicalFsvError("kernel_graph_readback_mismatch", {"graph": graph, "report": real["graph_readback_after_kernel"]})
    kernel_json = assert_file(real["artifact_readback"]["kernel_json"])
    index_json = assert_file(real["artifact_readback"]["index_json"])
    kernel_payload = json.loads((ROOT / kernel_json["path"]).read_text(encoding="utf-8"))
    index_payload = json.loads((ROOT / index_json["path"]).read_text(encoding="utf-8"))
    recall = kernel_payload["kernel"]["recall"]["ratio"]
    if float(recall) < 0.95 or len(index_payload.get("rows") or []) != int(real["artifact_readback"]["index_rows"]):
        raise PhysicalFsvError("kernel_artifact_payload_mismatch", {"recall": recall, "index_rows": len(index_payload.get("rows") or [])})
    return {
        "vault_path": real["vault"]["vault_path"],
        "required_files": files,
        "cf": cf_stats,
        "graph_cf": graph,
        "kernel_json": kernel_json,
        "index_json": index_json,
        "recall_ratio": recall,
        "index_rows": len(index_payload["rows"]),
    }


def assert_guard_stage(report: dict[str, Any]) -> dict[str, Any]:
    real = report["real"]
    vault_path = ROOT / real["vault"]["vault_path"]
    physical = real["physical_readback"]
    files = assert_file_map(physical["required_files"])
    cf_stats = assert_cf_sst_stats(vault_path, physical["cf"], required=["base", "anchors", "guard"])
    guard = read_cf(vault_path, "guard")
    expected = real["guard_cf_readback"]
    if guard["raw_rows"] != int(expected["raw_rows"]) or guard["unique_rows"] != int(expected["unique_rows"]):
        raise PhysicalFsvError("guard_cf_count_mismatch", {"guard": guard, "expected": expected})
    for name, profile in expected["decoded_profiles"].items():
        if profile["key_hex"] not in guard["latest_keys"]:
            raise PhysicalFsvError("guard_profile_key_missing", {"name": name, "profile": profile, "keys": guard["latest_keys"]})
    return {"vault_path": real["vault"]["vault_path"], "required_files": files, "cf": cf_stats, "guard_cf": guard, "profiles": expected["decoded_profiles"]}


def assert_rebuild_stage(report: dict[str, Any]) -> dict[str, Any]:
    real = report["real"]
    vault_path = ROOT / real["vault"]["vault_path"]
    manifest = real["manifest_readback"]
    manifest_file = assert_file(manifest["manifest_file"])
    manifest_payload = json.loads((ROOT / manifest_file["path"]).read_text(encoding="utf-8"))
    if manifest_payload.get("format") != "calyx-search-index-manifest-v1" or int(manifest_payload.get("base_seq", -1)) != int(manifest["base_seq"]):
        raise PhysicalFsvError("search_manifest_payload_mismatch", {"manifest": manifest_payload, "report": manifest})
    sidecars = [assert_file(row["file"]) | {"slot": row["slot"], "kind": row["kind"]} for row in manifest["slot_sidecars"]]
    filter_sidecar = assert_file(manifest["filter_sidecar"]["file"])
    tree_files = assert_index_tree(real["after_index_tree"])
    if real["after_index_tree"].get("rebuild_required_marker_exists"):
        raise PhysicalFsvError("rebuild_marker_exists_after_rebuild", {"tree": real["after_index_tree"]})
    if int(real["search_readback"]["matched_rank"]) != 1 or not real["search_readback"]["matched_is_physical_cx_list_member"]:
        raise PhysicalFsvError("rebuild_search_not_grounded", {"search": real["search_readback"]})
    return {
        "vault_path": real["vault"]["vault_path"],
        "manifest": manifest_file,
        "base_seq": manifest["base_seq"],
        "sidecars": sidecars,
        "filter_sidecar": filter_sidecar,
        "tree_files": tree_files,
        "search_rank": real["search_readback"]["matched_rank"],
    }


def assert_file_map(items: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {name: assert_file(stat) for name, stat in sorted(items.items())}


def assert_file(stat: dict[str, Any]) -> dict[str, Any]:
    path = ROOT / stat["path"]
    if not path.is_file():
        raise PhysicalFsvError("physical_file_missing", {"stat": stat})
    actual = file_readback(path)
    if actual["bytes"] != stat["bytes"] or actual["sha256"] != stat["sha256"]:
        raise PhysicalFsvError("physical_file_stat_mismatch", {"expected": stat, "actual": actual})
    return actual


def assert_index_tree(tree: dict[str, Any]) -> list[dict[str, Any]]:
    root = ROOT / tree["root"]
    if not root.is_dir():
        raise PhysicalFsvError("index_tree_missing", {"tree": tree})
    files = [assert_file(item) for item in tree.get("files") or []]
    actual_files = sorted(path for path in root.glob("*") if path.is_file())
    if len(actual_files) != int(tree.get("file_count", -1)):
        raise PhysicalFsvError("index_tree_file_count_mismatch", {"tree": tree, "actual": len(actual_files)})
    return files


def assert_cf_sst_stats(vault_path: Path, reported: dict[str, Any], required: list[str]) -> dict[str, Any]:
    out = {}
    for cf_name in required:
        info = reported.get(cf_name)
        if info is None:
            raise PhysicalFsvError("reported_cf_missing", {"cf": cf_name, "available": sorted(reported)})
        cf_dir = vault_path / "cf" / cf_name
        files = sorted(cf_dir.glob("*.sst"))
        if not files:
            raise PhysicalFsvError("cf_sst_missing", {"cf": cf_name, "dir": str(cf_dir.relative_to(ROOT))})
        bytes_total = sum(path.stat().st_size for path in files)
        first = anchored.sha256_bytes(files[0].read_bytes())
        last = anchored.sha256_bytes(files[-1].read_bytes())
        if len(files) != int(info["sst_count"]) or bytes_total != int(info["bytes"]) or first != info["sha256_first"] or last != info["sha256_last"]:
            raise PhysicalFsvError("cf_sst_stat_mismatch", {"cf": cf_name, "expected": info, "actual": {"sst_count": len(files), "bytes": bytes_total, "sha256_first": first, "sha256_last": last}})
        out[cf_name] = {"sst_count": len(files), "bytes": bytes_total, "sha256_first": first, "sha256_last": last}
    return out


def read_cf(vault_path: Path, cf_name: str) -> dict[str, Any]:
    proc = run_ok([str(CALYX), "readback", "--cf", cf_name, "--vault", str(vault_path)], reason=f"{cf_name}_cf_readback_failed", timeout=300)
    rows = []
    for line in proc.stdout.decode("utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) != 8 or parts[0] != "CF" or parts[1] != cf_name or parts[4] != "KEY" or parts[6] != "VALUE":
            raise PhysicalFsvError("malformed_cf_readback_line", {"cf": cf_name, "line": line})
        value = bytes.fromhex(parts[7])
        rows.append({"file": parts[3], "key_hex": parts[5], "value_sha256": anchored.sha256_bytes(value), "value_bytes": len(value)})
    latest = {row["key_hex"]: row for row in rows}
    return {
        "stdout_sha256": anchored.sha256_bytes(proc.stdout),
        "raw_rows": len(rows),
        "unique_rows": len(latest),
        "latest_keys": sorted(latest),
        "sample": rows[:3],
    }


def read_cx_list(vault_path: Path, limit: int) -> list[dict[str, Any]]:
    proc = run_ok(
        [
            str(CALYX),
            "readback",
            "cx-list",
            "--vault",
            str(vault_path),
            "--include-slots",
            "--limit",
            str(limit),
        ],
        reason="cx_list_readback_failed",
        timeout=600,
    )
    return json.loads(proc.stdout)


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    good = synthetic_happy(work_dir / "happy")
    return {
        "happy": good,
        "missing_required_file": synthetic_missing_required_file(work_dir / "missing_required_file"),
        "hash_mismatch": synthetic_hash_mismatch(work_dir / "hash_mismatch"),
        "missing_cf": synthetic_missing_cf(work_dir / "missing_cf"),
        "malformed_cf_line": synthetic_malformed_cf_line(work_dir / "malformed_cf_line"),
    }


def synthetic_happy(work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True)
    payload = b"known physical bytes"
    path = work_dir / "known.bin"
    path.write_bytes(payload)
    stat = file_readback(path)
    checked = assert_file(stat)
    return {"file": checked}


def synthetic_missing_required_file(work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True)
    missing = work_dir / "missing.bin"
    stat = {"path": str(missing.relative_to(ROOT)), "bytes": 1, "sha256": "0" * 64, "mode": "0o644"}
    return expect_error("missing_required_file", lambda: assert_file(stat))


def synthetic_hash_mismatch(work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True)
    path = work_dir / "known.bin"
    path.write_bytes(b"actual")
    stat = file_readback(path)
    stat["sha256"] = "1" * 64
    return expect_error("hash_mismatch", lambda: assert_file(stat))


def synthetic_missing_cf(work_dir: Path) -> dict[str, Any]:
    vault = work_dir / "vault"
    (vault / "cf").mkdir(parents=True)
    reported = {"base": {"sst_count": 1, "bytes": 1, "sha256_first": "0" * 64, "sha256_last": "0" * 64}}
    return expect_error("missing_cf", lambda: assert_cf_sst_stats(vault, reported, ["base"]))


def synthetic_malformed_cf_line(work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True)
    line = "not\tcf\treadback"
    try:
        parts = line.split("\t")
        if len(parts) != 8:
            raise PhysicalFsvError("malformed_cf_readback_line", {"cf": "synthetic", "line": line})
    except PhysicalFsvError as error:
        return {"reason": error.reason, "detail": error.detail}
    raise PhysicalFsvError("synthetic_malformed_cf_line_passed")


def expect_error(name: str, func: Any) -> dict[str, Any]:
    try:
        func()
    except PhysicalFsvError as error:
        return {"case": name, "reason": error.reason, "detail": error.detail}
    raise PhysicalFsvError("synthetic_edge_passed", {"case": name})


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
    real = assert_real_pipeline(work_dir, raw_root)
    synthetic = synthetic_edges(work_dir / "synthetic_edges")
    report = {"status": "ok", "real": real, "synthetic": synthetic}
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    if report_path.read_text(encoding="utf-8") != encoded + "\n":
        raise PhysicalFsvError("report_readback_mismatch", {"path": str(report_path.relative_to(ROOT))})
    print(
        json.dumps(
            {
                "status": "ok",
                "pipeline_steps": real["pipeline"]["completed_steps"],
                "vault_stages": sorted(real["vault_stages"]),
                "assay_unique_rows": real["bits_assay"]["assay_cf"]["unique_rows"],
                "graph_unique_rows": real["weave_loom"]["graph_cf"]["unique_rows"],
                "guard_unique_rows": real["guard_calibrate"]["guard_cf"]["unique_rows"],
                "rebuild_sidecars": len(real["rebuild_search_index"]["sidecars"]),
                "synthetic_edges": sorted(key for key in synthetic if key != "happy"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
