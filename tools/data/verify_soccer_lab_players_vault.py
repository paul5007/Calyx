#!/usr/bin/env python3
"""Build and verify the Soccer Lab 2026 players ex-ante Calyx vault."""

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
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CALYX = ROOT / "target" / "release" / "calyx"
ROWGEN = ROOT / "tools" / "data" / "generate_soccer_lab_rows.py"
SOURCES = ROOT / "tools" / "data" / "soccer_lab_sources.json"
DEFAULT_RAW = ROOT / "scratchpad" / "wc2026" / "raw"

SOURCE_ID = "mominullptr_fifa_world_cup_2026_dataset"
SOURCE_PATH = Path("mominullptr/fifa-world-cup-2026-dataset.zip")
SOURCE_MEMBERS = {
    "squads_and_players.csv": 1248,
    "teams.csv": 48,
}

FACETS = {
    "output": (ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "output", 5),
    "profile": (ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "profile", 7),
    "efficiency": (ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "efficiency", 5),
}


class PlayersVaultError(RuntimeError):
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


def source_config() -> dict[str, Any]:
    data = json.loads(SOURCES.read_text(encoding="utf-8"))
    for item in data.get("http_files", []):
        if item.get("id") == SOURCE_ID:
            return item
    raise PlayersVaultError("missing_source_config", {"source_id": SOURCE_ID})


def ensure_source_zip(raw_root: Path) -> dict[str, object]:
    item = source_config()
    path = raw_root / SOURCE_PATH
    if not path.exists():
        request = urllib.request.Request(item["url"], headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                status = int(response.status)
                content_type = response.headers.get("content-type", "")
        except urllib.error.URLError as exc:
            raise PlayersVaultError("source_download_failed", {"url": item["url"], "error": str(exc)}) from exc
        if status < 200 or status > 299 or not body:
            raise PlayersVaultError("source_download_bad_response", {"url": item["url"], "status": status, "bytes": len(body)})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        if path.read_bytes() != body:
            raise PlayersVaultError("source_write_readback_mismatch", {"path": str(path.relative_to(ROOT))})
        downloaded = True
    else:
        downloaded = False
        content_type = "existing"
    with zipfile.ZipFile(path) as archive:
        members = sorted(archive.namelist())
        missing = sorted(set(SOURCE_MEMBERS) - set(members))
        if missing:
            raise PlayersVaultError("missing_zip_members", {"missing": missing, "observed": members})
        counts = {}
        for member, expected_rows in SOURCE_MEMBERS.items():
            rows = list(csv.DictReader(io.StringIO(archive.read(member).decode("utf-8-sig"))))
            counts[member] = len(rows)
            if len(rows) != expected_rows:
                raise PlayersVaultError("unexpected_source_row_count", {"member": member, "expected": expected_rows, "observed": len(rows)})
    return {**file_stat(path), "source_id": SOURCE_ID, "downloaded": downloaded, "content_type": content_type, "member_rows": counts}


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
            "players-2026",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if proc.returncode != 0:
        raise PlayersVaultError("row_generation_failed", {"stderr": proc.stderr.decode("utf-8"), "stdout": proc.stdout.decode("utf-8")})
    path = rows_root / "players-2026.jsonl"
    rows = []
    teams = set()
    positions = {}
    role_flags = {"goal_keeper": 0, "defender": 0, "midfielder": 0, "forward": 0}
    forbidden = {
        "matches_played",
        "matches_started",
        "minutes_played",
        "assists",
        "shots",
        "shots_on_target",
        "yellow_cards",
        "red_cards",
        "average_rating",
        "clean_sheets",
        "saves",
        "goals_conceded",
    }
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        rows.append(row)
        if row["metadata"].get("entity") != "player_2026":
            raise PlayersVaultError("wrong_row_entity", {"metadata": row["metadata"]})
        keys = {token.split("=", 1)[0] for token in row["text"].split()}
        leaked = sorted(forbidden & keys)
        if leaked:
            raise PlayersVaultError("post_match_key_leaked_to_text", {"leaked": leaked, "text": row["text"]})
        if row.get("anchors"):
            raise PlayersVaultError("unexpected_player_anchor_in_predictive_rows", {"row": row})
        teams.add(row["metadata"]["team_id"])
        fields = dict(token.split("=", 1) for token in row["text"].split())
        positions[fields["position_code"]] = positions.get(fields["position_code"], 0) + 1
        active_roles = sum(int(fields[name]) for name in role_flags)
        if active_roles != 1:
            raise PlayersVaultError("position_flag_mismatch", {"position_code": fields["position_code"], "active_roles": active_roles, "text": row["text"]})
        for name in role_flags:
            role_flags[name] += int(fields[name])
    if len(rows) != 1248 or len(teams) != 48:
        raise PlayersVaultError("generated_row_count_mismatch", {"rows": len(rows), "teams": len(teams)})
    if role_flags != {"goal_keeper": 145, "defender": 421, "midfielder": 369, "forward": 313}:
        raise PlayersVaultError("role_flag_count_mismatch", {"role_flags": role_flags})
    return {"row_file": file_stat(path), "rows": len(rows), "teams": len(teams), "position_counts": dict(sorted(positions.items())), "role_flag_counts": role_flags}


def run_calyx(args: list[str], env: dict[str, str], timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def build_vault(work_dir: Path, rows_path: Path) -> dict[str, object]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    vault_name = "soccer-players-2026"
    create = run_calyx(["create-vault", vault_name, "--panel-template", "text-default"], env)
    if create.returncode != 0:
        raise PlayersVaultError("create_vault_failed", {"stderr": create.stderr.decode("utf-8")})
    created = json.loads(create.stdout)
    vault_path = home / "vaults" / created["vault_id"]
    slot_map = {}
    added = {}
    for name, (path, dim) in FACETS.items():
        add = run_calyx(
            [
                "add-lens",
                vault_name,
                "--name",
                f"player_{name}",
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
            raise PlayersVaultError("add_lens_failed", {"facet": name, "stderr": add.stderr.decode("utf-8")})
        payload = json.loads(add.stdout)
        added[name] = payload
        slot_map[name] = int(payload["slot_id"])
    panel = run_calyx(["list-panel", vault_name], env)
    if panel.returncode != 0:
        raise PlayersVaultError("list_panel_failed", {"stderr": panel.stderr.decode("utf-8")})
    ingest = run_calyx(["ingest", vault_name, "--batch", str(rows_path), "--output", "rows"], env, timeout=240)
    if ingest.returncode != 0:
        raise PlayersVaultError("ingest_failed", {"stderr": ingest.stderr.decode("utf-8")[-8000:]})
    ingest_rows = [json.loads(line) for line in ingest.stdout.decode("utf-8").splitlines() if line.strip()]
    if len(ingest_rows) != 1248:
        raise PlayersVaultError("ingest_row_count_mismatch", {"rows": len(ingest_rows)})
    readback = run_calyx(
        [
            "readback",
            "cx-list",
            "--vault",
            str(vault_path),
            "--include-slots",
            "--limit",
            "1248",
            "--rebuild-base-page-index",
        ],
        env,
        timeout=240,
    )
    if readback.returncode != 0:
        raise PlayersVaultError("cx_list_failed", {"stderr": readback.stderr.decode("utf-8")})
    cx_rows = json.loads(readback.stdout)
    if len(cx_rows) != 1248:
        raise PlayersVaultError("cx_list_row_count_mismatch", {"rows": len(cx_rows)})
    slot_counts, dim_counts = inspect_slots(cx_rows, slot_map)
    return {
        "vault_name": vault_name,
        "vault_id": created["vault_id"],
        "vault_path": str(vault_path.relative_to(ROOT)),
        "slot_map": slot_map,
        "added_lenses": added,
        "panel_stdout_sha256": sha256_bytes(panel.stdout),
        "ingest_stdout_sha256": sha256_bytes(ingest.stdout),
        "ingest_stderr_sha256": sha256_bytes(ingest.stderr),
        "ingest_rows": len(ingest_rows),
        "cx_list_rows": len(cx_rows),
        "cx_list_sha256": sha256_bytes(readback.stdout),
        "slot_counts": slot_counts,
        "slot_dim_counts": dim_counts,
        "physical_vault_readback": physical_vault_readback(vault_path, slot_map),
    }


def slot_entries(row: dict[str, Any]) -> list[dict[str, Any]]:
    slots = row.get("slots", [])
    if isinstance(slots, list):
        return [slot for slot in slots if isinstance(slot, dict)]
    if isinstance(slots, dict):
        return [{"slot": int(key), **value} for key, value in slots.items() if isinstance(value, dict)]
    return []


def inspect_slots(rows: list[dict[str, Any]], slot_map: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    expected_dims = {facet: dim for facet, (_path, dim) in FACETS.items()}
    slot_counts = {facet: 0 for facet in slot_map}
    dim_counts: dict[str, int] = {}
    for idx, row in enumerate(rows):
        by_slot = {}
        for entry in slot_entries(row):
            slot_id = int(entry.get("slot", entry.get("slot_id", -1)))
            by_slot[slot_id] = entry
        for facet, slot_id in slot_map.items():
            if slot_id not in by_slot:
                raise PlayersVaultError("missing_expected_slot", {"row": idx, "facet": facet, "slot": slot_id, "available": sorted(by_slot)})
            entry = by_slot[slot_id]
            dim = int(entry.get("dim", entry.get("len", entry.get("length", 0))))
            if dim == 0 and isinstance(entry.get("data"), list):
                dim = len(entry["data"])
            if dim != expected_dims[facet]:
                raise PlayersVaultError("slot_dim_mismatch", {"row": idx, "facet": facet, "slot": slot_id, "expected": expected_dims[facet], "observed": dim, "entry": entry})
            slot_counts[facet] += 1
            key = f"slot_{slot_id:02d}_dim_{dim}"
            dim_counts[key] = dim_counts.get(key, 0) + 1
    return slot_counts, dict(sorted(dim_counts.items()))


def physical_vault_readback(vault_path: Path, slot_map: dict[str, int]) -> dict[str, object]:
    required = {
        "MANIFEST": vault_path / "MANIFEST",
        "wal": vault_path / "wal" / "00000000000000000000.wal",
        "base_page_index_manifest": vault_path / "base_page_index_v1" / "manifest.json",
        "search_manifest": vault_path / "idx" / "search" / "manifest.json",
        "ledger_head": vault_path / "ledger_head" / "current.json",
    }
    required_stats = {}
    for name, path in required.items():
        if not path.exists():
            raise PlayersVaultError("missing_physical_vault_file", {"name": name, "path": str(path.relative_to(ROOT))})
        required_stats[name] = file_stat(path)
    slot_files = {}
    for facet, slot_id in slot_map.items():
        cf_files = sorted((vault_path / "cf" / f"slot_{slot_id:02d}").glob("*.sst"))
        dense_index = sorted((vault_path / "idx" / "search").glob(f"slot_{slot_id:05d}_seq_*_n_0000001248.flatdense.bin"))
        if not cf_files or not dense_index:
            raise PlayersVaultError("missing_slot_physical_files", {"facet": facet, "slot": slot_id, "cf_files": len(cf_files), "dense_index": len(dense_index)})
        slot_files[facet] = {
            "slot": slot_id,
            "cf_sst_count": len(cf_files),
            "cf_sst_bytes": sum(path.stat().st_size for path in cf_files),
            "cf_sst_sha256_first": sha256_bytes(cf_files[0].read_bytes()),
            "dense_index": file_stat(dense_index[-1]),
        }
    return {"required_files": required_stats, "slot_files": slot_files}


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
    report_path = resolve(args.out) if args.out else ROOT / "scratchpad" / "wc2026" / "fsv" / "players_vault" / "report.json"
    work_dir = report_path.parent
    rows_root = work_dir / "rows"
    source = ensure_source_zip(raw_root)
    generation = generate_rows(raw_root, rows_root)
    vault = build_vault(work_dir, rows_root / "players-2026.jsonl")
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
        raise PlayersVaultError("report_readback_mismatch", {"path": str(report_path)})
    print(json.dumps({"status": "ok", "rows": generation["rows"], "vault_id": vault["vault_id"], "cx_list_rows": vault["cx_list_rows"], "slots": vault["slot_counts"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
