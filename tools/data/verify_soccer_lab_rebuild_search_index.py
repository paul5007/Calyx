#!/usr/bin/env python3
"""Verify Soccer Lab rebuild-search-index sidecars and search readback."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

import verify_soccer_lab_anchored_outcomes as anchored
import verify_soccer_lab_weave_loom as weave


ROOT = anchored.ROOT
CALYX = anchored.CALYX
DEFAULT_RAW = anchored.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "rebuild_search_index" / "report.json"
VAULT_NAME = "soccer-rebuild-search-index"
EXPECTED_SPARSE_SLOT = 1


class RebuildSearchFsvError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> weave.subprocess.CompletedProcess[bytes]:
    return weave.subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=weave.subprocess.PIPE, stderr=weave.subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> weave.subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise RebuildSearchFsvError(
            reason,
            {
                "args": args,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def build_real_rebuild(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    rows_root = work_dir / "rows"
    generation = weave.generate_team_rows(raw_root, rows_root)
    rows_path = rows_root / "team-match-nonzero-balanced.jsonl"
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    vault = build_real_vault(work_dir, rows_path, generation["selected_rows"])
    env = os.environ.copy()
    env["CALYX_HOME"] = str(work_dir / "calyx_home")
    before = index_tree_readback(ROOT / vault["vault_path"])
    rebuild = run_ok(["rebuild-search-index", VAULT_NAME], env, "rebuild_search_index_failed", timeout=300)
    rebuild_report = json.loads(rebuild.stdout)
    verify_rebuild_report(rebuild_report, ROOT / vault["vault_path"])
    manifest = verify_manifest_sidecars(
        ROOT / vault["vault_path"],
        expected_rows=generation["selected_rows"],
        expected_dense_slots=sorted(vault["slot_map"].values()),
    )
    progress = verify_progress_artifact(Path(rebuild_report["progress_artifact"]))
    search = verify_basic_search(env, rows[0]["text"], vault["all_cx_ids"], manifest["base_seq"])
    after = index_tree_readback(ROOT / vault["vault_path"])
    return {
        "generation": generation,
        "vault": vault,
        "rebuild_stdout_sha256": anchored.sha256_bytes(rebuild.stdout),
        "rebuild_stderr_sha256": anchored.sha256_bytes(rebuild.stderr),
        "rebuild_report": rebuild_report,
        "before_index_tree": before,
        "after_index_tree": after,
        "manifest_readback": manifest,
        "progress_readback": progress,
        "search_readback": search,
        "cx_list_readback": verify_cx_list(env, ROOT / vault["vault_path"], generation["selected_rows"], vault["slot_map"]),
    }


def build_real_vault(work_dir: Path, rows_path: Path, row_count: int) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", VAULT_NAME, "--panel-template", "text-default"], env, "create_vault_failed")
    created = json.loads(create.stdout)
    vault_path = home / "vaults" / created["vault_id"]
    slot_map = weave.add_soccer_lenses(env, VAULT_NAME)
    ingest = run_ok(["ingest", VAULT_NAME, "--batch", str(rows_path.relative_to(ROOT)), "--output", "rows"], env, "ingest_failed", timeout=600)
    ingest_rows = [json.loads(line) for line in ingest.stdout.decode("utf-8").splitlines() if line.strip()]
    if len(ingest_rows) != row_count or not all(row.get("new") for row in ingest_rows):
        raise RebuildSearchFsvError("ingest_row_count_mismatch", {"observed": len(ingest_rows), "expected": row_count})
    cx_rows = cx_list(env, vault_path, row_count)
    slot_counts = anchored.inspect_slots(cx_rows, slot_map)
    return {
        "vault_id": created["vault_id"],
        "vault_path": str(vault_path.relative_to(ROOT)),
        "slot_map": slot_map,
        "ingest_rows": len(ingest_rows),
        "ingest_stdout_sha256": anchored.sha256_bytes(ingest.stdout),
        "cx_list_rows": len(cx_rows),
        "slot_counts": slot_counts,
        "sample_cx_ids": [row["cx_id"] for row in cx_rows[:3]],
        "all_cx_ids": [row["cx_id"] for row in cx_rows],
    }


def verify_rebuild_report(report: dict[str, Any], vault_path: Path) -> None:
    if report.get("status") != "ok":
        raise RebuildSearchFsvError("rebuild_status_not_ok", {"report": report})
    if Path(report.get("vault_dir", "")).resolve() != vault_path.resolve():
        raise RebuildSearchFsvError("rebuild_vault_dir_mismatch", {"report": report, "vault_path": str(vault_path)})
    if report.get("rebuild_required_marker") is not None:
        raise RebuildSearchFsvError("rebuild_marker_not_cleared", {"report": report})
    marker = vault_path / "idx" / "search" / "rebuild-required.json"
    if marker.exists():
        raise RebuildSearchFsvError("rebuild_required_marker_exists", {"marker": str(marker.relative_to(ROOT))})


def verify_manifest_sidecars(vault_path: Path, expected_rows: int, expected_dense_slots: list[int]) -> dict[str, Any]:
    manifest_path = vault_path / "idx" / "search" / "manifest.json"
    if not manifest_path.is_file():
        raise RebuildSearchFsvError("search_manifest_missing", {"path": str(manifest_path.relative_to(ROOT))})
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("format") != "calyx-search-index-manifest-v1":
        raise RebuildSearchFsvError("search_manifest_format_mismatch", {"manifest": manifest})
    base_seq = int(manifest.get("base_seq", 0))
    slots = manifest.get("slots") or []
    by_slot = {int(entry["slot"]): entry for entry in slots}
    expected_slots = [EXPECTED_SPARSE_SLOT, *expected_dense_slots]
    missing = [slot for slot in expected_slots if slot not in by_slot]
    if missing:
        raise RebuildSearchFsvError("search_manifest_missing_slots", {"missing": missing, "available": sorted(by_slot)})
    sidecars = []
    for slot in expected_slots:
        entry = by_slot[slot]
        if int(entry.get("len", -1)) != expected_rows or int(entry.get("built_at_seq", -1)) != base_seq:
            raise RebuildSearchFsvError("search_manifest_entry_mismatch", {"slot": slot, "entry": entry, "base_seq": base_seq, "rows": expected_rows})
        sidecars.append(verify_slot_sidecar(vault_path, entry, base_seq, expected_rows))
    filter_entry = manifest.get("filter") or {}
    filter_readback = verify_filter_sidecar(vault_path, filter_entry, base_seq, expected_rows)
    return {
        "manifest_file": file_readback(manifest_path),
        "format": manifest["format"],
        "base_seq": base_seq,
        "slot_count": len(slots),
        "expected_slots": expected_slots,
        "slot_sidecars": sidecars,
        "filter_sidecar": filter_readback,
    }


def verify_slot_sidecar(vault_path: Path, entry: dict[str, Any], base_seq: int, expected_rows: int) -> dict[str, Any]:
    rel = entry.get("index_rel")
    path = vault_path / rel
    if not path.is_file():
        raise RebuildSearchFsvError("slot_sidecar_missing", {"entry": entry})
    stat = file_readback(path)
    if stat["sha256"] != entry.get("sha256"):
        raise RebuildSearchFsvError("slot_sidecar_sha_mismatch", {"entry": entry, "file": stat})
    kind = entry.get("kind")
    if kind == "flat_dense":
        data = path.read_bytes()
        if data[:16] != b"CALYXFLATDENSE01":
            raise RebuildSearchFsvError("flatdense_magic_mismatch", {"path": stat["path"]})
        header_len = int.from_bytes(data[16:20], "little")
        dim = int(entry.get("dim", 0))
        expected_min = 20 + header_len + expected_rows * (16 + dim * 4)
        if len(data) != expected_min:
            raise RebuildSearchFsvError("flatdense_size_mismatch", {"path": stat["path"], "bytes": len(data), "expected": expected_min, "entry": entry})
        decoded = {"format": "calyx-search-flat-dense-v1", "header_len": header_len, "dim": dim, "rows": expected_rows}
    elif kind == "sparse_inverted":
        decoded_json = json.loads(path.read_text(encoding="utf-8"))
        if decoded_json.get("format") != "calyx-search-sparse-index-v1" or int(decoded_json.get("base_seq", -1)) != base_seq:
            raise RebuildSearchFsvError("sparse_sidecar_schema_mismatch", {"path": stat["path"], "json": decoded_json})
        if len(decoded_json.get("rows") or []) != expected_rows:
            raise RebuildSearchFsvError("sparse_sidecar_rows_mismatch", {"path": stat["path"], "rows": len(decoded_json.get("rows") or []), "expected": expected_rows})
        decoded = {
            "format": decoded_json["format"],
            "dim": decoded_json.get("dim"),
            "rows": len(decoded_json.get("rows") or []),
            "postings_terms": len(decoded_json.get("postings") or {}),
        }
    else:
        raise RebuildSearchFsvError("unsupported_slot_sidecar_kind", {"entry": entry})
    return {
        "slot": int(entry["slot"]),
        "kind": kind,
        "index_rel": rel,
        "manifest_sha256": entry["sha256"],
        "file": stat,
        "decoded": decoded,
    }


def verify_filter_sidecar(vault_path: Path, entry: dict[str, Any], base_seq: int, expected_rows: int) -> dict[str, Any]:
    rel = entry.get("index_rel")
    path = vault_path / rel
    if not path.is_file():
        raise RebuildSearchFsvError("filter_sidecar_missing", {"entry": entry})
    stat = file_readback(path)
    if stat["sha256"] != entry.get("sha256"):
        raise RebuildSearchFsvError("filter_sidecar_sha_mismatch", {"entry": entry, "file": stat})
    decoded = json.loads(path.read_text(encoding="utf-8"))
    if decoded.get("format") != "calyx-search-filter-index-v1" or int(decoded.get("base_seq", -1)) != base_seq:
        raise RebuildSearchFsvError("filter_sidecar_schema_mismatch", {"decoded": decoded})
    rows = decoded.get("rows") or []
    if len(rows) != expected_rows or int(entry.get("len", -1)) != expected_rows:
        raise RebuildSearchFsvError("filter_sidecar_rows_mismatch", {"rows": len(rows), "entry": entry, "expected": expected_rows})
    return {
        "index_rel": rel,
        "manifest_sha256": entry["sha256"],
        "file": stat,
        "decoded": {
            "format": decoded["format"],
            "base_seq": decoded["base_seq"],
            "rows": len(rows),
            "sample_cx_ids": [row.get("cx_id") for row in rows[:3]],
        },
    }


def verify_progress_artifact(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RebuildSearchFsvError("progress_artifact_missing", {"path": str(path)})
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    phases = [record.get("phase") for record in records]
    required = {"run_start", "slot_plan_ok", "slot_build_start", "manifest_write_ok", "complete"}
    if not required.issubset(set(phases)):
        raise RebuildSearchFsvError("progress_phases_missing", {"required": sorted(required), "phases": phases})
    if any(record.get("schema") != "calyx-search-rebuild-progress-v1" for record in records):
        raise RebuildSearchFsvError("progress_schema_mismatch", {"records": records[:3]})
    return {
        "file": file_readback(path),
        "records": len(records),
        "phases": sorted(set(phases)),
        "slot_build_records": sum(1 for phase in phases if phase == "slot_build_start"),
    }


def verify_basic_search(env: dict[str, str], query: str, ingested_cx_ids: list[str], base_seq: int) -> dict[str, Any]:
    proc = run_ok(["search", VAULT_NAME, query, "--k", "10", "--fusion", "rrf", "--guard", "off", "--no-provenance"], env, "basic_search_failed", timeout=180)
    hits = json.loads(proc.stdout)
    hit_ids = [hit["cx_id"] for hit in hits]
    ingested = set(ingested_cx_ids)
    matched = [hit for hit in hits if hit["cx_id"] in ingested]
    if not matched:
        raise RebuildSearchFsvError("basic_search_missing_ingested_cx", {"ingested_sample": ingested_cx_ids[:10], "hits": hit_ids})
    top_hit = matched[0]
    freshness = top_hit.get("freshness") or {}
    if int(freshness.get("base_seq", -1)) != base_seq or int(freshness.get("stale_by", -1)) != 0:
        raise RebuildSearchFsvError("basic_search_freshness_mismatch", {"hit": top_hit, "base_seq": base_seq})
    return {
        "query_sha256": anchored.sha256_bytes(query.encode("utf-8")),
        "matched_cx_id": top_hit["cx_id"],
        "matched_is_physical_cx_list_member": True,
        "hit_count": len(hits),
        "matched_rank": top_hit["rank"],
        "matched_score": top_hit["score"],
        "freshness": freshness,
        "stdout_sha256": anchored.sha256_bytes(proc.stdout),
        "stderr_sha256": anchored.sha256_bytes(proc.stderr),
        "hit_sample": hits[:3],
    }


def verify_cx_list(env: dict[str, str], vault_path: Path, expected_rows: int, slot_map: dict[str, int]) -> dict[str, Any]:
    rows = cx_list(env, vault_path, expected_rows)
    if len(rows) != expected_rows:
        raise RebuildSearchFsvError("cx_list_row_count_mismatch", {"observed": len(rows), "expected": expected_rows})
    return {
        "rows": len(rows),
        "slot_counts": anchored.inspect_slots(rows, slot_map),
        "sample_cx_ids": [row["cx_id"] for row in rows[:3]],
    }


def cx_list(env: dict[str, str], vault_path: Path, limit: int) -> list[dict[str, Any]]:
    proc = run_ok(
        [
            "readback",
            "cx-list",
            "--vault",
            str(vault_path),
            "--include-slots",
            "--limit",
            str(limit),
            "--rebuild-base-page-index",
        ],
        env,
        "cx_list_failed",
        timeout=600,
    )
    return json.loads(proc.stdout)


def index_tree_readback(vault_path: Path) -> dict[str, Any]:
    root = vault_path / "idx" / "search"
    files = sorted(path for path in root.glob("*") if path.is_file()) if root.exists() else []
    return {
        "root": str(root.relative_to(ROOT)),
        "exists": root.exists(),
        "file_count": len(files),
        "files": [file_readback(path) for path in files],
        "manifest_exists": (root / "manifest.json").is_file(),
        "rebuild_required_marker_exists": (root / "rebuild-required.json").is_file(),
    }


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    projectors = weave.write_synthetic_projectors(work_dir)
    happy = synthetic_happy(work_dir / "happy", projectors)
    edges = {
        "missing_manifest_search": synthetic_missing_manifest_search(work_dir / "missing_manifest_search", projectors),
        "corrupt_manifest_search": synthetic_corrupt_manifest_search(work_dir / "corrupt_manifest_search", projectors),
        "unknown_vault_rebuild": synthetic_unknown_vault_rebuild(work_dir / "unknown_vault_rebuild"),
    }
    return {"happy": happy, "edges": edges}


def synthetic_happy(work_dir: Path, projectors: list[Path]) -> dict[str, Any]:
    env, vault_path = weave.build_synthetic_vault(work_dir, projectors, zero_row=False)
    before = index_tree_readback(vault_path)
    rebuild = run_ok(["rebuild-search-index", "edge-vault"], env, "synthetic_rebuild_failed")
    report = json.loads(rebuild.stdout)
    manifest = verify_manifest_sidecars(vault_path, expected_rows=2, expected_dense_slots=[8, 9])
    search = synthetic_search(env, "ROW A", manifest["base_seq"])
    return {
        "vault_path": str(vault_path.relative_to(ROOT)),
        "before_index_tree": before,
        "rebuild_report": report,
        "manifest_readback": {
            "base_seq": manifest["base_seq"],
            "slot_count": manifest["slot_count"],
            "slot_sidecars": [
                {"slot": row["slot"], "kind": row["kind"], "sha256": row["file"]["sha256"], "decoded": row["decoded"]}
                for row in manifest["slot_sidecars"]
            ],
            "filter_rows": manifest["filter_sidecar"]["decoded"]["rows"],
        },
        "search_readback": search,
    }


def synthetic_search(env: dict[str, str], query: str, base_seq: int) -> dict[str, Any]:
    proc = run_ok(["search", "edge-vault", query, "--k", "2", "--fusion", "rrf", "--guard", "off", "--no-provenance"], env, "synthetic_search_failed")
    hits = json.loads(proc.stdout)
    if not hits or int(hits[0]["freshness"]["base_seq"]) != base_seq:
        raise RebuildSearchFsvError("synthetic_search_freshness_mismatch", {"hits": hits, "base_seq": base_seq})
    return {
        "hit_count": len(hits),
        "top_hit": hits[0],
        "stdout_sha256": anchored.sha256_bytes(proc.stdout),
    }


def synthetic_missing_manifest_search(work_dir: Path, projectors: list[Path]) -> dict[str, Any]:
    env, vault_path = weave.build_synthetic_vault(work_dir, projectors, zero_row=False)
    manifest = vault_path / "idx" / "search" / "manifest.json"
    if manifest.exists():
        manifest.unlink()
    before = index_tree_readback(vault_path)
    proc = run(["search", "edge-vault", "ROW A", "--k", "2"], env, timeout=60)
    after = index_tree_readback(vault_path)
    return assert_no_index_write("missing_manifest_search", proc, before, after)


def synthetic_corrupt_manifest_search(work_dir: Path, projectors: list[Path]) -> dict[str, Any]:
    env, vault_path = weave.build_synthetic_vault(work_dir, projectors, zero_row=False)
    manifest = vault_path / "idx" / "search" / "manifest.json"
    manifest.write_text("{not-json", encoding="utf-8")
    before = index_tree_readback(vault_path)
    proc = run(["search", "edge-vault", "ROW A", "--k", "2"], env, timeout=60)
    after = index_tree_readback(vault_path)
    return assert_no_index_write("corrupt_manifest_search", proc, before, after)


def synthetic_unknown_vault_rebuild(work_dir: Path) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    before = directory_readback(home)
    proc = run(["rebuild-search-index", "missing-vault"], env, timeout=60)
    after = directory_readback(home)
    if before != after:
        raise RebuildSearchFsvError("unknown_vault_rebuild_wrote_files", {"before": before, "after": after})
    return assert_failure("unknown_vault_rebuild", proc) | {"before": before, "after": after}


def assert_no_index_write(name: str, proc: weave.subprocess.CompletedProcess[bytes], before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    if before != after:
        raise RebuildSearchFsvError("synthetic_edge_wrote_index", {"case": name, "before": before, "after": after})
    return assert_failure(name, proc) | {"before_index_tree": before, "after_index_tree": after}


def assert_failure(name: str, proc: weave.subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
    if proc.returncode == 0:
        raise RebuildSearchFsvError("synthetic_edge_passed", {"case": name, "stdout": proc.stdout.decode("utf-8", "replace")})
    return {
        "returncode": proc.returncode,
        "stdout_sha256": anchored.sha256_bytes(proc.stdout),
        "stderr_sha256": anchored.sha256_bytes(proc.stderr),
        "stdout_fragment": proc.stdout.decode("utf-8", "replace")[-400:],
        "stderr_fragment": proc.stderr.decode("utf-8", "replace")[-400:],
    }


def directory_readback(path: Path) -> dict[str, Any]:
    files = sorted(child.relative_to(path).as_posix() for child in path.rglob("*") if child.is_file()) if path.exists() else []
    return {"path": str(path.relative_to(ROOT)), "exists": path.exists(), "files": files}


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
    real = build_real_rebuild(work_dir, raw_root)
    synthetic = synthetic_edges(work_dir / "synthetic_edges")
    report = {"status": "ok", "real": real, "synthetic": synthetic}
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    if report_path.read_text(encoding="utf-8") != encoded + "\n":
        raise RebuildSearchFsvError("report_readback_mismatch", {"path": str(report_path.relative_to(ROOT))})
    print(
        json.dumps(
            {
                "status": "ok",
                "vault_id": real["vault"]["vault_id"],
                "base_seq": real["manifest_readback"]["base_seq"],
                "sidecar_slots": real["manifest_readback"]["expected_slots"],
                "search_matched_rank": real["search_readback"]["matched_rank"],
                "synthetic_edges": sorted(synthetic["edges"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
