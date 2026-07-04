#!/usr/bin/env python3
"""Build and verify the Soccer Lab teams-history ex-ante Calyx vault."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CALYX = ROOT / "target" / "release" / "calyx"
ROWGEN = ROOT / "tools" / "data" / "generate_soccer_lab_rows.py"
SOURCES = ROOT / "tools" / "data" / "soccer_lab_sources.json"
DEFAULT_RAW = ROOT / "scratchpad" / "wc2026" / "raw"

SOURCE_ID = "harrachimustapha_fifa_world_cup_team_dataset"
SOURCE_PATH = Path("harrachimustapha/fifa-world-cup-team-dataset.zip")

FACETS = {
    "attack": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "attack", 6),
    "defense": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "defense", 5),
    "tempo": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "tempo", 4),
    "discipline": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "discipline", 4),
    "pedigree": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "pedigree", 6),
    "form": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "form", 5),
    "context": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "context", 8),
}


class VaultBuildError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_stat(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def load_sources() -> dict[str, Any]:
    data = json.loads(SOURCES.read_text(encoding="utf-8"))
    for item in data.get("http_files", []):
        if item.get("id") == SOURCE_ID:
            return item
    raise VaultBuildError("missing_source_config", {"source_id": SOURCE_ID})


def ensure_source_zip(raw_root: Path) -> dict[str, object]:
    item = load_sources()
    path = raw_root / SOURCE_PATH
    if not path.exists():
        request = urllib.request.Request(item["url"], headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                status = int(response.status)
                content_type = response.headers.get("content-type", "")
        except urllib.error.URLError as exc:
            raise VaultBuildError("source_download_failed", {"url": item["url"], "error": str(exc)}) from exc
        if status < 200 or status > 299 or not body:
            raise VaultBuildError("source_download_bad_response", {"url": item["url"], "status": status, "bytes": len(body)})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        if path.read_bytes() != body:
            raise VaultBuildError("source_write_readback_mismatch", {"path": str(path.relative_to(ROOT))})
        downloaded = True
    else:
        downloaded = False
        content_type = "existing"
    stat = file_stat(path)
    with zipfile.ZipFile(path) as archive:
        members = sorted(archive.namelist())
        if members != ["test.csv", "train.csv"]:
            raise VaultBuildError("unexpected_zip_members", {"members": members})
        counts = {}
        for member in members:
            rows = list(csv.DictReader(io.StringIO(archive.read(member).decode("utf-8-sig"))))
            counts[member] = len(rows)
    if counts.get("train.csv") != 192 or counts.get("test.csv") != 48:
        raise VaultBuildError("unexpected_source_row_counts", {"counts": counts})
    return {
        **stat,
        "downloaded": downloaded,
        "content_type": content_type,
        "members": members,
        "member_rows": counts,
    }


def run_cmd(args: list[str], env: dict[str, str] | None = None, timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def generate_rows(raw_root: Path, rows_root: Path) -> dict[str, object]:
    if rows_root.exists():
        shutil.rmtree(rows_root)
    proc = subprocess.run(
        [
            str(ROWGEN),
            "--raw-root",
            str(raw_root.relative_to(ROOT)),
            "--out",
            str(rows_root.relative_to(ROOT)),
            "--only",
            "team-tournaments",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if proc.returncode != 0:
        raise VaultBuildError("row_generation_failed", {"stderr": proc.stderr.decode("utf-8"), "stdout": proc.stdout.decode("utf-8")})
    path = rows_root / "team-tournaments.jsonl"
    rows = []
    split_counts: Counter[str] = Counter()
    anchor_counts: Counter[str] = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        rows.append(row)
        split_counts[row["metadata"]["dataset_split"]] += 1
        for anchor in row.get("anchors", []):
            anchor_counts[f"{anchor['kind']}={anchor['value']}"] += 1
        forbidden = {"winner", "finalist", "semi_finalist", "quarter_finalist"}
        keys = {token.split("=", 1)[0] for token in row["text"].split()}
        leaked = sorted(forbidden & keys)
        if leaked:
            raise VaultBuildError("outcome_key_leaked_to_text", {"leaked": leaked, "text": row["text"]})
    if len(rows) != 240 or split_counts != Counter({"train": 192, "test": 48}):
        raise VaultBuildError("generated_row_count_mismatch", {"rows": len(rows), "splits": dict(split_counts)})
    return {
        "row_file": file_stat(path),
        "rows": len(rows),
        "split_counts": dict(sorted(split_counts.items())),
        "anchor_counts": dict(sorted(anchor_counts.items())),
    }


def create_vault(work_dir: Path, rows_path: Path) -> dict[str, object]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    vault_name = "soccer-teams-history"

    create = run_cmd(["create-vault", vault_name, "--panel-template", "text-default"], env)
    if create.returncode != 0:
        raise VaultBuildError("create_vault_failed", {"stderr": create.stderr.decode("utf-8")})
    created = json.loads(create.stdout)
    vault_path = home / "vaults" / created["vault_id"]

    added_lenses = {}
    slot_map = {}
    for name, (path, dim) in FACETS.items():
        add = run_cmd(
            [
                "add-lens",
                vault_name,
                "--name",
                f"team_{name}",
                "--runtime",
                "external-cmd",
                "--endpoint",
                str(path),
                "--shape",
                f"Dense({dim})",
                "--modality",
                "text",
            ],
            env,
        )
        if add.returncode != 0:
            raise VaultBuildError("add_lens_failed", {"facet": name, "stderr": add.stderr.decode("utf-8")})
        payload = json.loads(add.stdout)
        added_lenses[name] = payload
        slot_map[name] = int(payload["slot_id"])

    panel = run_cmd(["list-panel", vault_name], env)
    if panel.returncode != 0:
        raise VaultBuildError("list_panel_failed", {"stderr": panel.stderr.decode("utf-8")})

    ingest = run_cmd(["ingest", vault_name, "--batch", str(rows_path), "--output", "rows"], env, timeout=180)
    if ingest.returncode != 0:
        raise VaultBuildError("ingest_failed", {"stderr": ingest.stderr.decode("utf-8")[-8000:]})
    ingest_rows = [json.loads(line) for line in ingest.stdout.decode("utf-8").splitlines() if line.strip()]
    if len(ingest_rows) != 240:
        raise VaultBuildError("ingest_row_count_mismatch", {"rows": len(ingest_rows)})

    readback = run_cmd(
        [
            "readback",
            "cx-list",
            "--vault",
            str(vault_path),
            "--include-slots",
            "--limit",
            "240",
            "--rebuild-base-page-index",
        ],
        env,
        timeout=180,
    )
    if readback.returncode != 0:
        raise VaultBuildError("cx_list_failed", {"stderr": readback.stderr.decode("utf-8")})
    cx_rows = json.loads(readback.stdout)
    if len(cx_rows) != 240:
        raise VaultBuildError("cx_list_row_count_mismatch", {"rows": len(cx_rows)})

    slot_counts, dim_counts = inspect_slots(cx_rows, slot_map)
    physical = physical_vault_readback(vault_path, slot_map)
    return {
        "vault_name": vault_name,
        "vault_id": created["vault_id"],
        "vault_path": str(vault_path.relative_to(ROOT)),
        "created": created,
        "slot_map": slot_map,
        "added_lenses": added_lenses,
        "panel_stdout_sha256": sha256_bytes(panel.stdout),
        "ingest_stdout_sha256": sha256_bytes(ingest.stdout),
        "ingest_stderr_sha256": sha256_bytes(ingest.stderr),
        "ingest_rows": len(ingest_rows),
        "cx_list_rows": len(cx_rows),
        "cx_list_sha256": sha256_bytes(readback.stdout),
        "slot_counts": slot_counts,
        "slot_dim_counts": dim_counts,
        "physical_vault_readback": physical,
    }


def physical_vault_readback(vault_path: Path, slot_map: dict[str, int]) -> dict[str, object]:
    required = {
        "MANIFEST": vault_path / "MANIFEST",
        "wal": vault_path / "wal" / "00000000000000000000.wal",
        "base_page_index_manifest": vault_path / "base_page_index_v1" / "manifest.json",
        "search_manifest": vault_path / "idx" / "search" / "manifest.json",
        "ledger_head": vault_path / "ledger_head" / "current.json",
    }
    stats = {}
    for name, path in required.items():
        if not path.exists():
            raise VaultBuildError("missing_physical_vault_file", {"name": name, "path": str(path.relative_to(ROOT))})
        stats[name] = file_stat(path)
    slot_files = {}
    for facet, slot_id in slot_map.items():
        files = sorted((vault_path / "cf" / f"slot_{slot_id:02d}").glob("*.sst"))
        if not files:
            raise VaultBuildError("missing_slot_cf_files", {"facet": facet, "slot": slot_id})
        dense_index = sorted((vault_path / "idx" / "search").glob(f"slot_{slot_id:05d}_seq_*_n_0000000240.flatdense.bin"))
        if not dense_index:
            raise VaultBuildError("missing_slot_dense_index", {"facet": facet, "slot": slot_id})
        slot_files[facet] = {
            "slot": slot_id,
            "cf_sst_count": len(files),
            "cf_sst_bytes": sum(path.stat().st_size for path in files),
            "cf_sst_sha256_first": sha256_bytes(files[0].read_bytes()),
            "dense_index": file_stat(dense_index[-1]),
        }
    return {"required_files": stats, "slot_files": slot_files}


def slot_entries(row: dict[str, Any]) -> list[dict[str, Any]]:
    slots = row.get("slots", [])
    if isinstance(slots, list):
        return [slot for slot in slots if isinstance(slot, dict)]
    if isinstance(slots, dict):
        out = []
        for key, value in slots.items():
            if isinstance(value, dict):
                out.append({"slot": int(key), **value})
        return out
    return []


def inspect_slots(rows: list[dict[str, Any]], slot_map: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    slot_counts = {facet: 0 for facet in slot_map}
    dim_counts: Counter[str] = Counter()
    expected_dims = {facet: dim for facet, (_path, dim) in FACETS.items()}
    for idx, row in enumerate(rows):
        by_slot = {}
        for entry in slot_entries(row):
            slot_id = int(entry.get("slot", entry.get("slot_id", -1)))
            by_slot[slot_id] = entry
        for facet, slot_id in slot_map.items():
            if slot_id not in by_slot:
                raise VaultBuildError("missing_expected_slot", {"row": idx, "facet": facet, "slot": slot_id, "available": sorted(by_slot)})
            entry = by_slot[slot_id]
            dim = int(entry.get("dim", entry.get("len", entry.get("length", 0))))
            if dim == 0 and isinstance(entry.get("data"), list):
                dim = len(entry["data"])
            if dim != expected_dims[facet]:
                raise VaultBuildError("slot_dim_mismatch", {"row": idx, "facet": facet, "slot": slot_id, "expected": expected_dims[facet], "observed": dim, "entry": entry})
            slot_counts[facet] += 1
            dim_counts[f"slot_{slot_id:02d}_dim_{dim}"] += 1
    return slot_counts, dict(sorted(dim_counts.items()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW.relative_to(ROOT)))
    parser.add_argument("--out", default="")
    return parser.parse_args()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    raw_root = resolve(args.raw_root)
    report_path = resolve(args.out) if args.out else ROOT / "scratchpad" / "wc2026" / "fsv" / "teams_history_vault" / "report.json"
    work_dir = report_path.parent
    rows_root = work_dir / "rows"
    source = ensure_source_zip(raw_root)
    generation = generate_rows(raw_root, rows_root)
    vault = create_vault(work_dir, rows_root / "team-tournaments.jsonl")
    report = {
        "status": "ok",
        "source": source,
        "generation": generation,
        "vault": vault,
        "projector_files": {facet: file_stat(path) | {"dim": dim} for facet, (path, dim) in FACETS.items()},
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    if report_path.read_text(encoding="utf-8") != encoded + "\n":
        raise VaultBuildError("report_readback_mismatch", {"path": str(report_path)})
    print(
        json.dumps(
            {
                "status": "ok",
                "rows": generation["rows"],
                "vault_id": vault["vault_id"],
                "cx_list_rows": vault["cx_list_rows"],
                "slots": vault["slot_counts"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
