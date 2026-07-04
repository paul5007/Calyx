#!/usr/bin/env python3
"""FSV for the committed Soccer Lab acquisition script and source inventory."""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ACQUIRE = ROOT / "tools/data/acquire_soccer_lab_sources.py"
SOURCES = ROOT / "tools/data/soccer_lab_sources.json"
RAW_ROOT = ROOT / "scratchpad/wc2026/raw"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fsv-root", required=True)
    args = parser.parse_args()
    root = resolve(args.fsv_root)
    if root.exists():
        import shutil

        shutil.rmtree(root)
    root.mkdir(parents=True)

    inventory = verify_inventory()
    physical_raw = physical_raw_readback()
    happy = run_happy_http_acquisition(root)
    edges = [
        {
            "case": "happy_local_http_file",
            "expected": "status ok and manifest byte readback",
            "observed": happy["status"],
        },
        edge_invalid_json(root),
        edge_unsafe_path(root),
        edge_missing_kaggle_cli(root),
        edge_missing_thestatsapi_key(root),
        edge_missing_hf_repo(root),
    ]
    readback = {
        "status": "ok",
        "surface": "soccer_lab.acquisition_script",
        "source_of_truth": "tools/data/acquire_soccer_lab_sources.py, tools/data/soccer_lab_sources.json, physical raw files, local HTTP bytes",
        "script": file_readback(ACQUIRE),
        "source_inventory": inventory,
        "physical_raw": physical_raw,
        "happy": happy,
        "edges": edges,
    }
    write_json(root / "acquisition-readback.json", readback)
    write_manifest(root, [root / "acquisition-readback.json", root / "happy" / "acquisition_manifest.json", root / "happy" / "acquire.log.jsonl"])
    print(json.dumps(readback, indent=2, sort_keys=True))
    return 0


def verify_inventory() -> dict[str, Any]:
    payload = read_json(SOURCES)
    kaggle = payload.get("kaggle", [])
    http_files = payload.get("http_files", [])
    if len(kaggle) != 5:
        raise AssertionError(f"expected 5 kaggle datasets, got {len(kaggle)}")
    if not any(str(item.get("id", "")).startswith("fjelstul_") for item in http_files):
        raise AssertionError("missing Fjelstul HTTP sources")
    if not payload.get("thestatsapi", {}).get("api_key_env"):
        raise AssertionError("missing TheStatsAPI env contract")
    if not payload.get("huggingface", {}).get("repo_id_env"):
        raise AssertionError("missing Hugging Face mirror env contract")
    return {
        "config": file_readback(SOURCES),
        "kaggle_count": len(kaggle),
        "http_file_count": len(http_files),
        "fjelstul_count": sum(1 for item in http_files if str(item.get("id", "")).startswith("fjelstul_")),
        "thestatsapi": payload["thestatsapi"]["id"],
        "huggingface": payload["huggingface"]["id"],
    }


def physical_raw_readback() -> dict[str, Any]:
    files = sorted(path for path in RAW_ROOT.rglob("*") if path.is_file())
    manifest = RAW_ROOT / "acquisition_manifest.json"
    return {
        "raw_root": str(RAW_ROOT.relative_to(ROOT)),
        "file_count": len(files),
        "manifest": file_readback(manifest) if manifest.exists() else None,
        "sample_files": [file_readback(path) for path in files[:8]],
    }


def run_happy_http_acquisition(root: Path) -> dict[str, Any]:
    server_root = root / "server"
    server_root.mkdir()
    source_bytes = b"team,goals\nCanada,2\nMexico,1\n"
    (server_root / "sample.csv").write_bytes(source_bytes)
    with serve_directory(server_root) as base_url:
        config = {
            "schema_version": 1,
            "project": "Soccer Lab FSV acquisition",
            "root": str((root / "happy").relative_to(ROOT)),
            "http_files": [
                {
                    "id": "local_fsv_sample",
                    "url": f"{base_url}/sample.csv",
                    "path": "local/sample.csv",
                }
            ],
            "kaggle": [],
        }
        config_path = root / "happy-sources.json"
        write_json(config_path, config)
        out = run_acquire(["--sources", str(config_path), "--out", str((root / "happy").relative_to(ROOT)), "--only", "http_files"])
    if out.returncode != 0:
        raise AssertionError(f"happy acquisition failed: stdout={out.stdout} stderr={out.stderr}")
    manifest = read_json(root / "happy" / "acquisition_manifest.json")
    acquired = root / "happy" / "local" / "sample.csv"
    if acquired.read_bytes() != source_bytes:
        raise AssertionError("acquired bytes differ from served source bytes")
    return {
        "status": "ok",
        "stdout": out.stdout.strip(),
        "manifest": file_readback(root / "happy" / "acquisition_manifest.json"),
        "log": file_readback(root / "happy" / "acquire.log.jsonl"),
        "file": file_readback(acquired),
        "manifest_file_count": len(manifest["files"]),
        "served_sha256": sha256_bytes(source_bytes),
    }


class serve_directory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.httpd: socketserver.TCPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> str:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(self.path))
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *_: object) -> None:
        assert self.httpd is not None
        self.httpd.shutdown()
        self.httpd.server_close()
        assert self.thread is not None
        self.thread.join(timeout=5)


def edge_invalid_json(root: Path) -> dict[str, Any]:
    config = root / "bad.json"
    config.write_text("{not-json", encoding="utf-8")
    out = run_acquire(["--sources", str(config), "--out", str((root / "bad-json").relative_to(ROOT))])
    return edge_result("invalid_json_config", "invalid_json", out)


def edge_unsafe_path(root: Path) -> dict[str, Any]:
    server_root = root / "unsafe-server"
    server_root.mkdir()
    (server_root / "sample.csv").write_text("x\n1\n", encoding="utf-8")
    with serve_directory(server_root) as base_url:
        config = {
            "schema_version": 1,
            "project": "unsafe path",
            "root": str((root / "unsafe").relative_to(ROOT)),
            "http_files": [{"id": "unsafe", "url": f"{base_url}/sample.csv", "path": "../escape.csv"}],
        }
        config_path = root / "unsafe-sources.json"
        write_json(config_path, config)
        out = run_acquire(["--sources", str(config_path), "--out", str((root / "unsafe").relative_to(ROOT)), "--only", "http_files"])
    return edge_result("unsafe_output_path", "unsafe_output_path", out)


def edge_missing_kaggle_cli(root: Path) -> dict[str, Any]:
    config = {"schema_version": 1, "project": "kaggle edge", "root": str((root / "kaggle").relative_to(ROOT)), "kaggle": [{"id": "k", "dataset": "owner/name"}]}
    config_path = root / "kaggle-sources.json"
    write_json(config_path, config)
    env = os.environ.copy()
    env["PATH"] = "/nonexistent"
    out = run_acquire(["--sources", str(config_path), "--out", str((root / "kaggle").relative_to(ROOT)), "--only", "kaggle"], env=env)
    return edge_result("missing_kaggle_cli", "missing_kaggle_cli", out)


def edge_missing_thestatsapi_key(root: Path) -> dict[str, Any]:
    config = {
        "schema_version": 1,
        "project": "stats edge",
        "root": str((root / "stats").relative_to(ROOT)),
        "thestatsapi": {"id": "stats", "api_key_env": "ISSUE10_NO_SUCH_KEY", "base_url": "http://127.0.0.1:1", "competition_id": "c", "season_id": "s", "per_page": 1, "path": "stats.json"},
    }
    config_path = root / "stats-sources.json"
    write_json(config_path, config)
    out = run_acquire(["--sources", str(config_path), "--out", str((root / "stats").relative_to(ROOT)), "--only", "thestatsapi"])
    return edge_result("missing_thestatsapi_key", "missing_required_env", out)


def edge_missing_hf_repo(root: Path) -> dict[str, Any]:
    config = {
        "schema_version": 1,
        "project": "hf edge",
        "root": str((root / "hf").relative_to(ROOT)),
        "huggingface": {"id": "hf", "repo_id_env": "ISSUE10_NO_SUCH_HF_REPO", "files": [{"path": "manifest.json", "out": "manifest.json"}]},
    }
    config_path = root / "hf-sources.json"
    write_json(config_path, config)
    out = run_acquire(["--sources", str(config_path), "--out", str((root / "hf").relative_to(ROOT)), "--only", "huggingface"])
    return edge_result("missing_hf_repo", "missing_required_env", out)


def edge_result(case: str, expected_reason: str, out: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        observed = json.loads(out.stderr.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        observed = {"raw_stderr": out.stderr}
    return {
        "case": case,
        "expected": expected_reason,
        "observed": observed.get("reason"),
        "exit_code": out.returncode,
    }


def run_acquire(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ACQUIRE), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> None:
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
