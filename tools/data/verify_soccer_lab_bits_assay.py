#!/usr/bin/env python3
"""Verify Soccer Lab bits rows persist in the physical Assay CF."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import verify_soccer_lab_anchored_outcomes as anchored


ROOT = anchored.ROOT
CALYX = anchored.CALYX
DEFAULT_RAW = anchored.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "bits_assay" / "report.json"
AXES = ("label:match_result", "label:team_match_result")
LOW_SIGNAL_CODE = "CALYX_ASSAY_LOW_SIGNAL"


class BitsAssayError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise BitsAssayError(
            reason,
            {
                "args": args,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def assay_key(axis: str) -> str:
    return (b"bits\0" + axis.encode("utf-8")).hex()


def decode_assay_cf(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 8 or parts[0] != "CF" or parts[1] != "assay" or parts[4] != "KEY" or parts[6] != "VALUE":
            raise BitsAssayError("malformed_assay_cf_line", {"line": line})
        value = bytes.fromhex(parts[7])
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise BitsAssayError("assay_value_not_json", {"line": line, "error": str(error)}) from error
        rows.append(
            {
                "file": parts[3],
                "key_hex": parts[5],
                "value_sha256": anchored.sha256_bytes(value),
                "payload": payload,
            }
        )
    return rows


def latest_by_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest[row["key_hex"]] = row
    return {key: latest[key] for key in sorted(latest)}


def command_low_signal(proc: subprocess.CompletedProcess[bytes]) -> bool:
    return LOW_SIGNAL_CODE in proc.stderr.decode("utf-8", "replace") or LOW_SIGNAL_CODE in proc.stdout.decode("utf-8", "replace")


def run_bits_axes(work_dir: Path, vault_path: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["CALYX_HOME"] = str(work_dir / "calyx_home")
    command_reports: dict[str, Any] = {}
    for axis in AXES:
        proc = run_ok(["bits", "soccer-anchored-outcomes", axis, "--explain"], env, f"bits_failed_{axis}", timeout=300)
        if command_low_signal(proc):
            raise BitsAssayError("bits_command_low_signal", {"axis": axis, "stderr": proc.stderr.decode("utf-8", "replace")})
        report = json.loads(proc.stdout)
        expected_key = assay_key(axis)
        if report.get("anchor") != axis:
            raise BitsAssayError("bits_anchor_mismatch", {"axis": axis, "report": report})
        if report.get("n") != 150:
            raise BitsAssayError("bits_observed_count_mismatch", {"axis": axis, "n": report.get("n")})
        explain = report.get("explain") or {}
        if explain.get("persisted_cf") != "assay" or explain.get("persisted_key_hex") != expected_key:
            raise BitsAssayError("bits_explain_persistence_mismatch", {"axis": axis, "explain": explain, "expected_key": expected_key})
        per_slot = report.get("per_slot")
        if not isinstance(per_slot, list) or len(per_slot) < 2:
            raise BitsAssayError("bits_per_slot_missing", {"axis": axis, "per_slot": per_slot})
        if not any(float(slot.get("bits", 0.0)) >= 0.05 and not slot.get("low_signal", True) for slot in per_slot):
            raise BitsAssayError("bits_all_slots_low_signal", {"axis": axis, "per_slot": per_slot})
        command_reports[axis] = {
            "stdout_sha256": anchored.sha256_bytes(proc.stdout),
            "stderr_sha256": anchored.sha256_bytes(proc.stderr),
            "low_signal_code_seen": command_low_signal(proc),
            "report": report,
            "per_slot_bits": [
                {
                    "slot": slot["slot"],
                    "name": slot["name"],
                    "bits": slot["bits"],
                    "low_signal": slot["low_signal"],
                }
                for slot in per_slot
            ],
        }

    assay = run_ok(["readback", "--cf", "assay", "--vault", str(vault_path)], env, "assay_cf_readback_failed", timeout=240)
    rows = decode_assay_cf(assay.stdout.decode("utf-8"))
    latest = latest_by_key(rows)
    expected = {axis: assay_key(axis) for axis in AXES}
    decoded_axes: dict[str, Any] = {}
    for axis, key_hex in expected.items():
        row = latest.get(key_hex)
        if row is None:
            raise BitsAssayError("assay_bits_key_missing", {"axis": axis, "key_hex": key_hex, "available": sorted(latest)})
        payload = row["payload"]
        if payload != command_reports[axis]["report"]:
            raise BitsAssayError("assay_payload_mismatch", {"axis": axis, "command": command_reports[axis]["report"], "readback": payload})
        decoded_axes[axis] = {
            "key_hex": key_hex,
            "file": row["file"],
            "value_sha256": row["value_sha256"],
            "per_slot_bits": command_reports[axis]["per_slot_bits"],
            "panel_sufficiency": payload["panel_sufficiency"],
            "n": payload["n"],
            "positive_anchor_count": payload["explain"]["positive_anchor_count"],
            "comparison_count": payload["explain"]["comparison_count"],
            "slot_count": len(payload["per_slot"]),
            "slots_above_floor": [
                slot["name"]
                for slot in payload["per_slot"]
                if float(slot.get("bits", 0.0)) >= 0.05 and not slot.get("low_signal", True)
            ],
        }
    return {
        "commands": command_reports,
        "assay_cf_raw_rows": len(rows),
        "assay_cf_unique_keys": len(latest),
        "assay_cf_stdout_sha256": anchored.sha256_bytes(assay.stdout),
        "decoded_axes": decoded_axes,
        "physical_readback": assay_physical_readback(vault_path),
    }


def assay_physical_readback(vault_path: Path) -> dict[str, Any]:
    stats = anchored.physical_readback(vault_path)
    assay_files = sorted((vault_path / "cf" / "assay").glob("*.sst"))
    if not assay_files:
        raise BitsAssayError("missing_assay_sst", {"vault": str(vault_path.relative_to(ROOT))})
    stats["cf"]["assay"] = {
        "sst_count": len(assay_files),
        "bytes": sum(path.stat().st_size for path in assay_files),
        "sha256_first": anchored.sha256_bytes(assay_files[0].read_bytes()),
        "sha256_last": anchored.sha256_bytes(assay_files[-1].read_bytes()),
    }
    return stats


def assay_cf_count(vault_path: Path, env: dict[str, str]) -> int:
    proc = run(["readback", "--cf", "assay", "--vault", str(vault_path)], env, timeout=60)
    if proc.returncode != 0:
        return 0
    return len(latest_by_key(decode_assay_cf(proc.stdout.decode("utf-8"))))


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", "bits-edge", "--panel-template", "text-default"], env, "edge_create_failed")
    vault_path = home / "vaults" / json.loads(create.stdout)["vault_id"]
    one_row = work_dir / "one-row.jsonl"
    one_row.write_text(
        json.dumps(
            {
                "text": "synthetic one-row bits edge",
                "anchors": [{"kind": "label:tiny", "value": "yes", "source": "bits-edge-fsv", "confidence": 1.0}],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    run_ok(["ingest", "bits-edge", "--batch", str(one_row.relative_to(ROOT)), "--output", "rows"], env, "edge_ingest_failed")

    cases = {
        "insufficient_samples_one_row": ["bits", "bits-edge", "label:tiny", "--explain"],
        "insufficient_samples_absent_axis": ["bits", "bits-edge", "label:absent", "--explain"],
        "unknown_anchor_kind": ["bits", "bits-edge", "not-a-kind", "--explain"],
    }
    observed: dict[str, Any] = {}
    for name, args in cases.items():
        before = assay_cf_count(vault_path, env)
        proc = run(args, env, timeout=60)
        after = assay_cf_count(vault_path, env)
        if proc.returncode == 0:
            raise BitsAssayError("synthetic_bad_case_passed", {"case": name, "stdout": proc.stdout.decode("utf-8", "replace")})
        if before != after:
            raise BitsAssayError("synthetic_bad_case_wrote_assay", {"case": name, "before": before, "after": after})
        stderr = proc.stderr.decode("utf-8", "replace")
        observed[name] = {
            "returncode": proc.returncode,
            "before_assay_cf": before,
            "after_assay_cf": after,
            "stderr_sha256": anchored.sha256_bytes(proc.stderr),
            "expected_no_write": True,
            "observed_error_fragment": stderr[-240:],
        }
    return {"vault_path": str(vault_path.relative_to(ROOT)), "row_file": anchored.file_stat(one_row), "edges": observed}


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
    rows_root = work_dir / "rows"
    generation = anchored.generate_rows(raw_root, rows_root)
    vault = anchored.build_anchor_vault(work_dir, rows_root / "anchored-outcomes-balanced.jsonl", generation["rows"])
    vault_path = ROOT / vault["vault_path"]
    bits = run_bits_axes(work_dir, vault_path)
    edges = synthetic_edges(work_dir / "synthetic_edges")
    report = {
        "status": "ok",
        "generation": generation,
        "vault": vault,
        "bits": bits,
        "synthetic_edges": edges,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    if report_path.read_text(encoding="utf-8") != encoded + "\n":
        raise BitsAssayError("report_readback_mismatch", {"path": str(report_path.relative_to(ROOT))})
    print(
        json.dumps(
            {
                "status": "ok",
                "rows": generation["rows"],
                "assay_cf_raw_rows": bits["assay_cf_raw_rows"],
                "assay_cf_unique_keys": bits["assay_cf_unique_keys"],
                "axes": {
                    axis: {
                        "n": detail["n"],
                        "slot_count": detail["slot_count"],
                        "slots_above_floor": detail["slots_above_floor"],
                        "key_hex": detail["key_hex"],
                    }
                    for axis, detail in bits["decoded_axes"].items()
                },
                "synthetic_edges": sorted(edges["edges"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
