#!/usr/bin/env python3
"""Acquire Soccer Lab source datasets with fail-closed byte verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCES = ROOT / "tools" / "data" / "soccer_lab_sources.json"


class AcquisitionError(RuntimeError):
    def __init__(self, source_id: str, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(f"{source_id}: {reason}")
        self.source_id = source_id
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


def load_json(path: Path) -> Any:
    with path.open("rb") as fh:
        data = fh.read()
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise AcquisitionError(
            "config",
            "invalid_json",
            {"path": str(path), "input_hash": sha256_bytes(data), "error": str(exc)},
        ) from exc


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.write_bytes(encoded)
    observed = path.read_bytes()
    if observed != encoded:
        raise AcquisitionError(
            "file_write",
            "readback_mismatch",
            {"path": str(path), "expected_sha256": sha256_bytes(encoded), "observed_sha256": sha256_bytes(observed)},
        )


def log_event(log_path: Path, event: dict[str, Any]) -> None:
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as fh:
        fh.write(json.dumps(event, sort_keys=True).encode("utf-8") + b"\n")


def require_nonblank_env(name: str, source_id: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise AcquisitionError(source_id, "missing_required_env", {"env": name})
    return value


def http_get(source_id: str, url: str, headers: dict[str, str] | None = None) -> tuple[bytes, dict[str, Any]]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = int(response.status)
            body = response.read()
            final_url = response.geturl()
            content_type = response.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        body = exc.read(4096)
        raise AcquisitionError(
            source_id,
            "http_error",
            {"url": url, "status": exc.code, "body_prefix_sha256": sha256_bytes(body), "body_prefix_len": len(body)},
        ) from exc
    except urllib.error.URLError as exc:
        raise AcquisitionError(source_id, "http_unreachable", {"url": url, "error": str(exc)}) from exc
    if status < 200 or status > 299:
        raise AcquisitionError(source_id, "http_non_2xx", {"url": url, "status": status})
    if not body:
        raise AcquisitionError(source_id, "empty_http_body", {"url": url, "status": status})
    return body, {"url": url, "final_url": final_url, "status": status, "content_type": content_type}


def verified_write(source_id: str, out_root: Path, rel_path: str, data: bytes, meta: dict[str, Any]) -> dict[str, Any]:
    if rel_path.startswith("/") or ".." in Path(rel_path).parts:
        raise AcquisitionError(source_id, "unsafe_output_path", {"path": rel_path})
    path = out_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    observed = path.read_bytes()
    expected_sha = sha256_bytes(data)
    observed_sha = sha256_bytes(observed)
    if observed != data:
        raise AcquisitionError(
            source_id,
            "file_readback_mismatch",
            {"path": str(path), "expected_sha256": expected_sha, "observed_sha256": observed_sha},
        )
    return {
        "source_id": source_id,
        "path": str(path.relative_to(ROOT)),
        "bytes": len(observed),
        "sha256": observed_sha,
        **meta,
    }


def acquire_http_files(config: dict[str, Any], out_root: Path) -> list[dict[str, Any]]:
    records = []
    for item in config.get("http_files", []):
        source_id = item["id"]
        body, meta = http_get(source_id, item["url"])
        records.append(verified_write(source_id, out_root, item["path"], body, {"kind": "http_file", **meta}))
    return records


def acquire_kaggle(config: dict[str, Any], out_root: Path) -> list[dict[str, Any]]:
    if shutil.which("kaggle") is None:
        raise AcquisitionError("kaggle", "missing_kaggle_cli", {"expected": "kaggle on PATH"})
    if not (os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY")) and not Path.home().joinpath(".kaggle", "kaggle.json").exists():
        raise AcquisitionError("kaggle", "missing_kaggle_auth", {"expected": "KAGGLE_USERNAME/KAGGLE_KEY or ~/.kaggle/kaggle.json"})
    records = []
    for item in config.get("kaggle", []):
        source_id = item["id"]
        dataset = item["dataset"]
        target = out_root / "kaggle" / source_id
        target.mkdir(parents=True, exist_ok=True)
        cmd = ["kaggle", "datasets", "download", dataset, "--path", str(target), "--unzip", "--force", "--quiet"]
        proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        if proc.returncode != 0:
            raise AcquisitionError(
                source_id,
                "kaggle_download_failed",
                {
                    "dataset": dataset,
                    "returncode": proc.returncode,
                    "stdout_sha256": sha256_bytes(proc.stdout.encode()),
                    "stderr_sha256": sha256_bytes(proc.stderr.encode()),
                },
            )
        files = sorted(path for path in target.rglob("*") if path.is_file())
        if not files:
            raise AcquisitionError(source_id, "kaggle_download_empty", {"dataset": dataset, "dir": str(target)})
        for path in files:
            data = path.read_bytes()
            if not data:
                raise AcquisitionError(source_id, "empty_downloaded_file", {"path": str(path)})
            records.append(
                {
                    "source_id": source_id,
                    "kind": "kaggle",
                    "dataset": dataset,
                    "path": str(path.relative_to(ROOT)),
                    "bytes": len(data),
                    "sha256": sha256_bytes(data),
                }
            )
    return records


def acquire_thestatsapi(config: dict[str, Any], out_root: Path) -> list[dict[str, Any]]:
    item = config["thestatsapi"]
    source_id = item["id"]
    token = require_nonblank_env(item["api_key_env"], source_id)
    base_url = item["base_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    page = 1
    rows: list[Any] = []
    pages: list[dict[str, Any]] = []
    while True:
        query = urllib.parse.urlencode(
            {
                "competition_id": item["competition_id"],
                "season_id": item["season_id"],
                "per_page": int(item["per_page"]),
                "page": page,
            }
        )
        url = f"{base_url}/football/matches?{query}"
        body, meta = http_get(source_id, url, headers)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise AcquisitionError(source_id, "invalid_json_response", {"url": url, "body_sha256": sha256_bytes(body)}) from exc
        data = payload.get("data")
        if not isinstance(data, list):
            raise AcquisitionError(source_id, "missing_data_array", {"url": url, "body_sha256": sha256_bytes(body)})
        rows.extend(data)
        pages.append({"page": page, "items": len(data), **meta})
        pagination = payload.get("pagination") or payload.get("meta") or {}
        last_page = pagination.get("last_page") or pagination.get("total_pages")
        if last_page is None:
            if len(data) < int(item["per_page"]):
                break
        elif page >= int(last_page):
            break
        page += 1
        if page > 20:
            raise AcquisitionError(source_id, "pagination_limit_exceeded", {"last_seen_page": page, "rows": len(rows)})
    if not rows:
        raise AcquisitionError(source_id, "empty_match_payload", {"pages": pages})
    encoded = json.dumps({"source": source_id, "pages": pages, "data": rows}, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    return [verified_write(source_id, out_root, item["path"], encoded, {"kind": "thestatsapi", "rows": len(rows), "pages": pages})]


def acquire_huggingface(config: dict[str, Any], out_root: Path) -> list[dict[str, Any]]:
    item = config["huggingface"]
    source_id = item["id"]
    repo_id = require_nonblank_env(item["repo_id_env"], source_id)
    token = os.environ.get(item.get("token_env", ""), "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    revision = item.get("revision", "main")
    records = []
    for file_item in item.get("files", []):
        file_path = file_item["path"].lstrip("/")
        quoted_repo = "/".join(urllib.parse.quote(part, safe="") for part in repo_id.split("/"))
        quoted_file = "/".join(urllib.parse.quote(part, safe="") for part in file_path.split("/"))
        url = f"https://huggingface.co/datasets/{quoted_repo}/resolve/{urllib.parse.quote(revision, safe='')}/{quoted_file}"
        body, meta = http_get(source_id, url, headers)
        records.append(verified_write(source_id, out_root, file_item["out"], body, {"kind": "huggingface", "repo_id": repo_id, **meta}))
    if not records:
        raise AcquisitionError(source_id, "no_huggingface_files_configured")
    return records


def selected(args: argparse.Namespace, name: str) -> bool:
    return not args.only or name in args.only


def run(args: argparse.Namespace) -> int:
    config_path = Path(args.sources).resolve()
    config = load_json(config_path)
    out_root = (ROOT / args.out).resolve() if args.out else (ROOT / config["root"]).resolve()
    if not str(out_root).startswith(str(ROOT.resolve())):
        raise AcquisitionError("config", "output_outside_repo", {"out": str(out_root)})
    log_path = out_root / "acquire.log.jsonl"
    manifest_path = out_root / "acquisition_manifest.json"
    log_event(log_path, {"event": "start", "project": config.get("project"), "config": str(config_path), "config_sha256": sha256_file(config_path)})
    records: list[dict[str, Any]] = []
    try:
        if selected(args, "http_files"):
            records.extend(acquire_http_files(config, out_root))
        if selected(args, "kaggle"):
            records.extend(acquire_kaggle(config, out_root))
        if selected(args, "thestatsapi"):
            records.extend(acquire_thestatsapi(config, out_root))
        if selected(args, "huggingface"):
            records.extend(acquire_huggingface(config, out_root))
        if not records:
            raise AcquisitionError("run", "no_sources_selected")
        manifest = {
            "schema_version": 1,
            "project": config.get("project"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source_config": str(config_path.relative_to(ROOT)),
            "source_config_sha256": sha256_file(config_path),
            "files": records,
        }
        write_json(manifest_path, manifest)
        log_event(log_path, {"event": "complete", "files": len(records), "manifest": str(manifest_path.relative_to(ROOT)), "manifest_sha256": sha256_file(manifest_path)})
    except AcquisitionError as exc:
        log_event(log_path, {"event": "error", "source": exc.source_id, "reason": exc.reason, **exc.detail})
        raise
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default=str(DEFAULT_SOURCES), help="source config JSON")
    parser.add_argument("--out", default="", help="output directory relative to repo root; defaults to config root")
    parser.add_argument(
        "--only",
        action="append",
        choices=["http_files", "kaggle", "thestatsapi", "huggingface"],
        help="limit acquisition to one or more source groups",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except AcquisitionError as exc:
        print(json.dumps({"status": "error", "source": exc.source_id, "reason": exc.reason, **exc.detail}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
