#!/usr/bin/env python3
"""Write and verify Soccer Lab raw-source provenance manifests."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_ROOT = ROOT / "scratchpad" / "wc2026" / "raw"
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "provenance" / "source_manifest.json"
DEFAULT_ACQUISITION = DEFAULT_RAW_ROOT / "acquisition_manifest.json"


class ManifestError(RuntimeError):
    def __init__(self, stage: str, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(f"{stage}: {reason}")
        self.stage = stage
        self.reason = reason
        self.detail = detail or {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repo_path(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def log_event(log_path: Path, event: dict[str, Any]) -> None:
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as fh:
        fh.write(json.dumps(event, sort_keys=True).encode("utf-8") + b"\n")


def load_json_required(path: Path, stage: str) -> Any:
    if not path.exists():
        raise ManifestError(stage, "missing_required_input", {"path": str(path)})
    data = path.read_bytes()
    if not data:
        raise ManifestError(stage, "empty_required_input", {"path": str(path)})
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise ManifestError(stage, "invalid_json", {"path": str(path), "sha256": sha256_bytes(data)}) from exc


def csv_row_count(path: Path) -> int:
    data = path.read_bytes()
    if not data:
        raise ManifestError("count_rows", "empty_required_input", {"path": str(path)})
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ManifestError("count_rows", "invalid_utf8_csv", {"path": str(path), "sha256": sha256_bytes(data)}) from exc
    reader = csv.reader(text.splitlines())
    try:
        next(reader)
    except StopIteration as exc:
        raise ManifestError("count_rows", "missing_csv_header", {"path": str(path)}) from exc
    return sum(1 for _ in reader)


def json_row_count(path: Path) -> int | None:
    payload = load_json_required(path, "count_rows")
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return len(data)
        files = payload.get("files")
        if isinstance(files, list):
            return len(files)
    return None


def row_count_for(path: Path) -> tuple[str, int | None]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv_data_rows", csv_row_count(path)
    if suffix in {".json", ".jsonl"}:
        if suffix == ".jsonl":
            return "jsonl_rows", sum(1 for line in path.read_bytes().splitlines() if line.strip())
        return "json_rows", json_row_count(path)
    return "not_counted", None


def acquisition_records(path: Path) -> dict[str, dict[str, Any]]:
    payload = load_json_required(path, "load_acquisition")
    records = payload.get("files")
    if not isinstance(records, list):
        raise ManifestError("load_acquisition", "missing_files_array", {"path": str(path)})
    out: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or "path" not in record:
            raise ManifestError("load_acquisition", "malformed_file_record", {"path": str(path)})
        out[str(record["path"])] = record
    return out


def source_files(raw_root: Path) -> list[Path]:
    if not raw_root.exists():
        raise ManifestError("scan_sources", "missing_raw_root", {"path": str(raw_root)})
    files = sorted(
        path
        for path in raw_root.rglob("*")
        if path.is_file() and path.name not in {"acquire.log.jsonl", "acquisition_manifest.json"}
    )
    if not files:
        raise ManifestError("scan_sources", "no_source_files", {"path": str(raw_root)})
    return files


def build_manifest(raw_root: Path, acquisition_path: Path) -> dict[str, Any]:
    acquired = acquisition_records(acquisition_path)
    files = []
    for path in source_files(raw_root):
        rel = repo_path(path)
        row_count_kind, row_count = row_count_for(path)
        acquisition = acquired.get(rel, {})
        record = {
            "path": rel,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "row_count_kind": row_count_kind,
            "row_count": row_count,
            "source_id": acquisition.get("source_id", ""),
            "source_kind": acquisition.get("kind", ""),
            "url": acquisition.get("url", ""),
            "content_type": acquisition.get("content_type", ""),
        }
        files.append(record)
    return {
        "schema_version": 1,
        "project": "Soccer Lab",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "raw_root": repo_path(raw_root),
        "acquisition_manifest": repo_path(acquisition_path),
        "acquisition_manifest_sha256": sha256_file(acquisition_path),
        "file_count": len(files),
        "files": files,
    }


def write_json_checked(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    staging = path.parent / f".{path.name}.staging-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    staged = staging / path.name
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    staged.write_bytes(encoded)
    observed = staged.read_bytes()
    if observed != encoded:
        raise ManifestError(
            "write_manifest",
            "readback_mismatch",
            {"path": str(staged), "expected_sha256": sha256_bytes(encoded), "observed_sha256": sha256_bytes(observed)},
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    staged.replace(path)
    shutil.rmtree(staging)
    final = path.read_bytes()
    if final != encoded:
        raise ManifestError(
            "write_manifest",
            "published_readback_mismatch",
            {"path": str(path), "expected_sha256": sha256_bytes(encoded), "observed_sha256": sha256_bytes(final)},
        )
    return {"path": repo_path(path), "bytes": len(final), "sha256": sha256_bytes(final)}


def compare_manifest(path: Path, observed: dict[str, Any]) -> dict[str, Any]:
    expected = load_json_required(path, "verify_manifest")
    expected_files = {record["path"]: record for record in expected.get("files", [])}
    observed_files = {record["path"]: record for record in observed.get("files", [])}
    if set(expected_files) != set(observed_files):
        missing = sorted(set(expected_files) - set(observed_files))
        extra = sorted(set(observed_files) - set(expected_files))
        raise ManifestError("verify_manifest", "file_set_mismatch", {"missing": missing, "extra": extra})
    mismatches = []
    fields = ["bytes", "sha256", "row_count_kind", "row_count", "source_id", "source_kind", "url"]
    for rel, expected_record in expected_files.items():
        observed_record = observed_files[rel]
        for field in fields:
            if expected_record.get(field) != observed_record.get(field):
                mismatches.append(
                    {
                        "path": rel,
                        "field": field,
                        "expected": expected_record.get(field),
                        "observed": observed_record.get(field),
                    }
                )
    if mismatches:
        raise ManifestError("verify_manifest", "manifest_mismatch", {"mismatches": mismatches[:20], "mismatch_count": len(mismatches)})
    return {"path": repo_path(path), "file_count": len(expected_files), "status": "ok"}


def resolve_repo_path(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def run(args: argparse.Namespace) -> int:
    raw_root = resolve_repo_path(args.raw_root)
    acquisition = resolve_repo_path(args.acquisition_manifest)
    manifest_path = resolve_repo_path(args.manifest)
    log_path = manifest_path.parent / "provenance.log.jsonl"
    if not str(raw_root).startswith(str(ROOT.resolve())) or not str(manifest_path).startswith(str(ROOT.resolve())):
        raise ManifestError("config", "path_outside_repo", {"raw_root": str(raw_root), "manifest": str(manifest_path)})
    log_event(log_path, {"event": "start", "mode": args.mode, "raw_root": repo_path(raw_root), "manifest": repo_path(manifest_path)})
    try:
        observed = build_manifest(raw_root, acquisition)
        if args.mode == "write":
            record = write_json_checked(manifest_path, observed)
            log_event(log_path, {"event": "complete", "mode": "write", "manifest": record, "file_count": observed["file_count"]})
            print(json.dumps({"status": "ok", "mode": "write", **record, "file_count": observed["file_count"]}, sort_keys=True))
        else:
            record = compare_manifest(manifest_path, observed)
            log_event(log_path, {"event": "complete", "mode": "verify", **record})
            print(json.dumps({"status": "ok", "mode": "verify", **record}, sort_keys=True))
    except ManifestError as exc:
        log_event(log_path, {"event": "error", "stage": exc.stage, "reason": exc.reason, **exc.detail})
        raise
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["write", "verify"])
    parser.add_argument("--raw-root", default=repo_path(DEFAULT_RAW_ROOT), help="raw source root")
    parser.add_argument("--acquisition-manifest", default=repo_path(DEFAULT_ACQUISITION), help="acquisition manifest JSON")
    parser.add_argument("--manifest", default=repo_path(DEFAULT_OUT), help="provenance manifest JSON")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except ManifestError as exc:
        print(json.dumps({"status": "error", "stage": exc.stage, "reason": exc.reason, **exc.detail}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
