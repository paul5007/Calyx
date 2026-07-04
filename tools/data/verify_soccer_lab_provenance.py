#!/usr/bin/env python3
"""FSV for Soccer Lab raw-source provenance manifest generation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROVENANCE = ROOT / "tools/data/provenance_manifest.py"
RAW_ROOT = ROOT / "scratchpad/wc2026/raw"
ACQUISITION = RAW_ROOT / "acquisition_manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fsv-root", required=True)
    args = parser.parse_args()
    root = resolve(args.fsv_root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    real = real_manifest_fsv(root)
    happy = synthetic_happy_path(root)
    edges = [
        {
            "case": "real_manifest_verify",
            "expected": "status ok and physical byte readback",
            "observed": real["verify"]["status"],
        },
        {
            "case": "synthetic_happy_path",
            "expected": "2 CSV data rows with matching sha256",
            "observed": happy["status"],
        },
        edge_missing_raw_root(root),
        edge_invalid_acquisition_json(root),
        edge_corrupt_manifest(root),
        edge_path_outside_repo(root),
    ]
    readback = {
        "status": "ok",
        "surface": "soccer_lab.provenance_manifest",
        "source_of_truth": "tools/data/provenance_manifest.py plus physical scratchpad/wc2026/raw bytes",
        "script": file_readback(PROVENANCE),
        "physical_raw_root": str(RAW_ROOT.relative_to(ROOT)),
        "physical_acquisition_manifest": file_readback(ACQUISITION),
        "real_manifest": real,
        "synthetic_happy_path": happy,
        "edges": edges,
    }
    write_json(root / "provenance-readback.json", readback)
    write_manifest(
        root,
        [
            root / "provenance-readback.json",
            root / "real" / "source_manifest.json",
            root / "real" / "provenance.log.jsonl",
            root / "happy" / "source_manifest.json",
            root / "happy" / "provenance.log.jsonl",
        ],
    )
    print(json.dumps(readback, indent=2, sort_keys=True))
    return 0


def real_manifest_fsv(root: Path) -> dict[str, Any]:
    out = root / "real" / "source_manifest.json"
    write_proc = run_provenance("write", RAW_ROOT, ACQUISITION, out)
    assert_success("real write", write_proc)
    verify_proc = run_provenance("verify", RAW_ROOT, ACQUISITION, out)
    assert_success("real verify", verify_proc)

    manifest = read_json(out)
    records = manifest.get("files")
    if not isinstance(records, list):
        raise AssertionError("real manifest missing files array")
    expected_count = len(source_files(RAW_ROOT))
    if manifest.get("file_count") != expected_count or len(records) != expected_count:
        raise AssertionError(f"expected {expected_count} source files, got manifest file_count={manifest.get('file_count')} len={len(records)}")
    if manifest.get("acquisition_manifest_sha256") != file_readback(ACQUISITION)["sha256"]:
        raise AssertionError("acquisition manifest sha256 does not match physical readback")

    validated: list[dict[str, Any]] = []
    for record in records:
        validated.append(validate_record(record))
    required = [
        "scratchpad/wc2026/raw/fjelstul/data-csv/players.csv",
        "scratchpad/wc2026/raw/fjelstul/data-csv/matches.csv",
        "scratchpad/wc2026/raw/fjelstul/data-csv/team_appearances.csv",
        "scratchpad/wc2026/raw/fjelstul/codebook/variables.csv",
    ]
    samples = {path: next((item for item in validated if item["path"] == path), None) for path in required}
    missing_samples = [path for path, item in samples.items() if item is None]
    if missing_samples:
        raise AssertionError(f"missing required provenance samples: {missing_samples}")
    return {
        "status": "ok",
        "write": parse_stdout(write_proc),
        "verify": parse_stdout(verify_proc),
        "manifest": file_readback(out),
        "file_count": manifest["file_count"],
        "validated_record_count": len(validated),
        "sample_records": samples,
    }


def synthetic_happy_path(root: Path) -> dict[str, Any]:
    raw = root / "happy-raw"
    source = raw / "local" / "sample.csv"
    source.parent.mkdir(parents=True)
    source.write_text("id,value\n1,alpha\n2,beta\n", encoding="utf-8")
    acquisition = raw / "acquisition_manifest.json"
    write_json(
        acquisition,
        {
            "schema_version": 1,
            "project": "Soccer Lab provenance FSV",
            "files": [
                {
                    "source_id": "local_sample",
                    "kind": "http",
                    "url": "http://127.0.0.1/synthetic/sample.csv",
                    "content_type": "text/csv",
                    "path": str(source.relative_to(ROOT)),
                }
            ],
        },
    )
    manifest = root / "happy" / "source_manifest.json"
    write_proc = run_provenance("write", raw, acquisition, manifest)
    assert_success("synthetic write", write_proc)
    verify_proc = run_provenance("verify", raw, acquisition, manifest)
    assert_success("synthetic verify", verify_proc)
    payload = read_json(manifest)
    records = payload["files"]
    if len(records) != 1:
        raise AssertionError(f"expected 1 synthetic manifest record, got {len(records)}")
    record = records[0]
    expected_sha = file_readback(source)["sha256"]
    if record["sha256"] != expected_sha or record["row_count"] != 2 or record["source_id"] != "local_sample":
        raise AssertionError(f"unexpected synthetic record: {record}")
    return {
        "status": "ok",
        "source": file_readback(source),
        "manifest": file_readback(manifest),
        "write": parse_stdout(write_proc),
        "verify": parse_stdout(verify_proc),
        "record": record,
    }


def edge_missing_raw_root(root: Path) -> dict[str, Any]:
    proc = run_provenance("write", root / "missing-raw", ACQUISITION, root / "edges" / "missing-raw.json")
    return edge_result("missing_raw_root", "missing_raw_root", proc)


def edge_invalid_acquisition_json(root: Path) -> dict[str, Any]:
    raw = root / "bad-acquisition-raw"
    (raw / "local").mkdir(parents=True)
    (raw / "local" / "sample.csv").write_text("id\n1\n", encoding="utf-8")
    acquisition = raw / "acquisition_manifest.json"
    acquisition.write_text("{bad", encoding="utf-8")
    proc = run_provenance("write", raw, acquisition, root / "edges" / "bad-acquisition.json")
    return edge_result("invalid_acquisition_json", "invalid_json", proc)


def edge_corrupt_manifest(root: Path) -> dict[str, Any]:
    raw = root / "corrupt-raw"
    source = raw / "local" / "sample.csv"
    source.parent.mkdir(parents=True)
    source.write_text("id,value\n1,alpha\n", encoding="utf-8")
    acquisition = raw / "acquisition_manifest.json"
    write_json(
        acquisition,
        {
            "schema_version": 1,
            "files": [
                {
                    "source_id": "corrupt_sample",
                    "kind": "http",
                    "url": "http://127.0.0.1/corrupt/sample.csv",
                    "path": str(source.relative_to(ROOT)),
                }
            ],
        },
    )
    manifest = root / "edges" / "corrupt-source_manifest.json"
    assert_success("corrupt baseline write", run_provenance("write", raw, acquisition, manifest))
    payload = read_json(manifest)
    payload["files"][0]["sha256"] = "0" * 64
    write_json(manifest, payload)
    proc = run_provenance("verify", raw, acquisition, manifest)
    return edge_result("corrupt_manifest_sha", "manifest_mismatch", proc)


def edge_path_outside_repo(root: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            sys.executable,
            str(PROVENANCE),
            "write",
            "--raw-root",
            str(RAW_ROOT),
            "--acquisition-manifest",
            str(ACQUISITION),
            "--manifest",
            "/tmp/calyx-provenance-outside/source_manifest.json",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return edge_result("path_outside_repo", "path_outside_repo", proc)


def validate_record(record: dict[str, Any]) -> dict[str, Any]:
    path = ROOT / record["path"]
    observed = file_readback(path)
    for field in ["bytes", "sha256"]:
        if record[field] != observed[field]:
            raise AssertionError(f"{record['path']} {field} mismatch: {record[field]} != {observed[field]}")
    if record["row_count_kind"] == "csv_data_rows":
        expected_rows = csv_data_rows(path)
        if record["row_count"] != expected_rows:
            raise AssertionError(f"{record['path']} row_count mismatch: {record['row_count']} != {expected_rows}")
    return {
        "path": record["path"],
        "bytes": record["bytes"],
        "sha256": record["sha256"],
        "row_count_kind": record["row_count_kind"],
        "row_count": record["row_count"],
        "source_id": record["source_id"],
    }


def source_files(raw_root: Path) -> list[Path]:
    return sorted(
        path
        for path in raw_root.rglob("*")
        if path.is_file() and path.name not in {"acquire.log.jsonl", "acquisition_manifest.json"}
    )


def csv_data_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        next(reader)
        return sum(1 for _ in reader)


def run_provenance(mode: str, raw_root: Path, acquisition: Path, manifest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PROVENANCE),
            mode,
            "--raw-root",
            str(raw_root),
            "--acquisition-manifest",
            str(acquisition),
            "--manifest",
            str(manifest),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_success(label: str, proc: subprocess.CompletedProcess[str]) -> None:
    if proc.returncode != 0:
        raise AssertionError(f"{label} failed: stdout={proc.stdout} stderr={proc.stderr}")


def edge_result(case: str, expected_reason: str, proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        observed = json.loads(proc.stderr.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        observed = {"raw_stderr": proc.stderr}
    if proc.returncode == 0:
        raise AssertionError(f"{case} unexpectedly succeeded: {proc.stdout}")
    if observed.get("reason") != expected_reason:
        raise AssertionError(f"{case} expected {expected_reason}, got {observed}")
    return {
        "case": case,
        "expected": expected_reason,
        "observed": observed.get("reason"),
        "stage": observed.get("stage"),
        "exit_code": proc.returncode,
    }


def parse_stdout(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(proc.stdout.strip().splitlines()[-1])


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_readback(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def write_manifest(root: Path, files: list[Path]) -> None:
    lines = []
    for path in files:
        data = path.read_bytes()
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {path.relative_to(root)}")
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


if __name__ == "__main__":
    raise SystemExit(main())
