#!/usr/bin/env python3
"""Verify Soccer Lab kernel-build artifacts and recall gate."""

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
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "kernel_build" / "report.json"
MIN_RECALL = 0.95


class KernelBuildFsvError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> weave.subprocess.CompletedProcess[bytes]:
    return weave.subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=weave.subprocess.PIPE, stderr=weave.subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> weave.subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise KernelBuildFsvError(
            reason,
            {
                "args": args,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def build_real_kernel(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    rows_root = work_dir / "rows"
    generation = weave.generate_team_rows(raw_root, rows_root)
    vault = weave.build_real_vault(work_dir, rows_root / "team-match-nonzero-balanced.jsonl", generation["selected_rows"])
    env = os.environ.copy()
    env["CALYX_HOME"] = str(work_dir / "calyx_home")
    kernel = run_ok(["kernel-build", weave.WEAVE_VAULT], env, "kernel_build_failed", timeout=300)
    kernel_report = json.loads(kernel.stdout)
    verify_kernel_report(kernel_report, vault["weave_report"])
    artifact_readback = verify_artifacts(kernel_report)
    graph_readback = verify_graph_again(env, ROOT / vault["vault_path"], kernel_report)
    return {
        "generation": generation,
        "vault": vault,
        "kernel_stdout_sha256": anchored.sha256_bytes(kernel.stdout),
        "kernel_stderr_sha256": anchored.sha256_bytes(kernel.stderr),
        "kernel_stderr_tail": kernel.stderr.decode("utf-8", "replace")[-4000:],
        "kernel_report": kernel_report,
        "artifact_readback": artifact_readback,
        "graph_readback_after_kernel": graph_readback,
    }


def verify_kernel_report(report: dict[str, Any], weave_report: dict[str, Any]) -> None:
    if report.get("status") != "ok":
        raise KernelBuildFsvError("kernel_status_not_ok", {"report": report})
    recall = report.get("recall") or {}
    if not recall.get("gate_passed"):
        raise KernelBuildFsvError("kernel_recall_gate_not_passed", {"recall": recall})
    if float(recall.get("min_recall_ratio", 0.0)) < MIN_RECALL - 1e-6:
        raise KernelBuildFsvError("kernel_recall_gate_lowered", {"recall": recall})
    if float(recall.get("ratio", 0.0)) < MIN_RECALL:
        raise KernelBuildFsvError("kernel_recall_below_floor", {"recall": recall})
    if int(recall.get("n_queries_tested", 0)) <= 0:
        raise KernelBuildFsvError("kernel_recall_not_tested", {"recall": recall})
    kernel = report.get("kernel") or {}
    if float(kernel.get("groundedness_fraction", 0.0)) <= 0.0:
        raise KernelBuildFsvError("kernel_not_grounded", {"kernel": kernel})
    if int(kernel.get("members", 0)) <= 0 or int(kernel.get("kernel_graph", 0)) <= 0:
        raise KernelBuildFsvError("kernel_empty_members", {"kernel": kernel})
    graph = report.get("graph") or {}
    assoc = weave_report["assoc_graph"]["report"]
    if graph.get("nodes") != assoc.get("node_count") or graph.get("edges") != assoc.get("edge_count"):
        raise KernelBuildFsvError("kernel_graph_counts_mismatch", {"kernel_graph": graph, "weave_assoc": assoc})
    artifacts = report.get("artifacts") or {}
    readback = artifacts.get("readback") or {}
    if readback.get("health_recall_pass_mode") != "passed":
        raise KernelBuildFsvError("kernel_health_not_passed", {"readback": readback})
    if readback.get("kernel_members") != kernel.get("members") or readback.get("index_rows") != kernel.get("members"):
        raise KernelBuildFsvError("kernel_readback_count_mismatch", {"readback": readback, "kernel": kernel})


def verify_artifacts(report: dict[str, Any]) -> dict[str, Any]:
    kernel_id = report["kernel"]["kernel_id"]
    kernel_path = Path(report["artifacts"]["kernel_json"])
    index_path = Path(report["artifacts"]["index_json"])
    if not kernel_path.exists() or not index_path.exists():
        raise KernelBuildFsvError("kernel_artifact_missing", {"kernel_json": str(kernel_path), "index_json": str(index_path)})
    kernel_bytes = kernel_path.read_bytes()
    index_bytes = index_path.read_bytes()
    if len(kernel_bytes) != report["artifacts"]["kernel_json_bytes"] or len(index_bytes) != report["artifacts"]["index_json_bytes"]:
        raise KernelBuildFsvError("kernel_artifact_byte_count_mismatch", {"report": report["artifacts"]})
    kernel_snapshot = json.loads(kernel_bytes)
    index_snapshot = json.loads(index_bytes)
    if kernel_snapshot.get("format_version") != 1 or index_snapshot.get("format_version") != 1:
        raise KernelBuildFsvError("kernel_artifact_format_mismatch", {"kernel": kernel_snapshot.get("format_version"), "index": index_snapshot.get("format_version")})
    kernel = kernel_snapshot.get("kernel") or {}
    if kernel.get("kernel_id") != kernel_id or index_snapshot.get("kernel_id") != kernel_id:
        raise KernelBuildFsvError("kernel_artifact_id_mismatch", {"kernel_id": kernel_id})
    if kernel.get("recall", {}).get("ratio", 0.0) < MIN_RECALL or kernel.get("recall", {}).get("n_queries_tested", 0) <= 0:
        raise KernelBuildFsvError("kernel_artifact_recall_invalid", {"recall": kernel.get("recall")})
    if len(kernel.get("members") or []) != report["kernel"]["members"]:
        raise KernelBuildFsvError("kernel_artifact_member_count_mismatch", {"kernel": kernel, "report": report["kernel"]})
    rows = index_snapshot.get("rows") or []
    if len(rows) != report["kernel"]["members"]:
        raise KernelBuildFsvError("kernel_index_row_count_mismatch", {"rows": len(rows), "members": report["kernel"]["members"]})
    dim = index_snapshot.get("dim")
    if dim is None or dim <= 0 or any(len(row.get("vector") or []) != dim for row in rows):
        raise KernelBuildFsvError("kernel_index_dim_mismatch", {"dim": dim, "rows": rows[:3]})
    member_ids = set(kernel.get("members") or [])
    row_ids = {row.get("cx_id") for row in rows}
    if member_ids != row_ids:
        raise KernelBuildFsvError("kernel_index_members_mismatch", {"missing": sorted(member_ids - row_ids), "extra": sorted(row_ids - member_ids)})
    return {
        "kernel_json": file_readback(kernel_path),
        "index_json": file_readback(index_path),
        "kernel_format_version": kernel_snapshot["format_version"],
        "index_format_version": index_snapshot["format_version"],
        "kernel_id": kernel_id,
        "kernel_members": len(kernel["members"]),
        "kernel_graph": len(kernel["kernel_graph"]),
        "index_dim": dim,
        "index_rows": len(rows),
        "recall": kernel["recall"],
        "warnings": kernel.get("warnings", []),
        "sample_members": kernel["members"][:6],
        "sample_index_rows": rows[:3],
    }


def verify_graph_again(env: dict[str, str], vault_path: Path, kernel_report: dict[str, Any]) -> dict[str, Any]:
    graph = weave.read_decode_cf(env, vault_path, "graph")
    decoded = [weave.decode_graph_row(row) for row in graph["latest"].values()]
    counts: dict[str, int] = {}
    for row in decoded:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    if counts.get("node") != kernel_report["graph"]["nodes"] or counts.get("edge_out") != kernel_report["graph"]["edges"]:
        raise KernelBuildFsvError("post_kernel_graph_readback_mismatch", {"counts": counts, "report": kernel_report["graph"]})
    return {
        "stdout_sha256": graph["stdout_sha256"],
        "raw_rows": graph["raw_rows"],
        "unique_rows": graph["unique_rows"],
        "kind_counts": counts,
    }


def file_readback(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": len(data),
        "sha256": anchored.sha256_bytes(data),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    projectors = weave.write_synthetic_projectors(work_dir)
    happy = synthetic_happy(work_dir / "happy", projectors)
    edges = {
        "no_woven_graph": synthetic_no_woven_graph(work_dir / "no_woven_graph", projectors),
        "unanchored_graph": synthetic_unanchored_graph(work_dir / "unanchored_graph", projectors),
        "zero_held_out_fraction": synthetic_bad_kernel_args(work_dir / "zero_held_out_fraction", projectors, ["kernel-build", "edge-vault", "--held-out-fraction", "0", "--top-k", "1"]),
        "top_k_zero": synthetic_bad_kernel_args(work_dir / "top_k_zero", projectors, ["kernel-build", "edge-vault", "--top-k", "0"]),
    }
    return {"happy": happy, "edges": edges}


def synthetic_happy(work_dir: Path, projectors: list[Path]) -> dict[str, Any]:
    env, vault_path = build_synthetic_kernel_vault(work_dir, projectors, anchored_rows=True, row_count=30)
    weave.run_ok(["weave-loom", "edge-vault", "--knn", "4"], env, "synthetic_happy_weave_failed")
    kernel = run_ok(["kernel-build", "edge-vault", "--held-out-fraction", "1", "--top-k", "1"], env, "synthetic_happy_kernel_failed")
    report = json.loads(kernel.stdout)
    verify_kernel_report(report, {"assoc_graph": {"report": {"node_count": report["graph"]["nodes"], "edge_count": report["graph"]["edges"]}}})
    artifact = verify_artifacts(report)
    return {
        "vault_path": str(vault_path.relative_to(ROOT)),
        "kernel_report": report,
        "artifact_readback": {
            "kernel_json": artifact["kernel_json"],
            "index_json": artifact["index_json"],
            "index_rows": artifact["index_rows"],
            "recall_ratio": artifact["recall"]["ratio"],
        },
    }


def synthetic_no_woven_graph(work_dir: Path, projectors: list[Path]) -> dict[str, Any]:
    env, vault_path = weave.build_synthetic_vault(work_dir, projectors, zero_row=False)
    before = kernel_artifact_count(vault_path)
    proc = run(["kernel-build", "edge-vault"], env, timeout=60)
    after = kernel_artifact_count(vault_path)
    return assert_bad_case("no_woven_graph", proc, before, after)


def synthetic_unanchored_graph(work_dir: Path, projectors: list[Path]) -> dict[str, Any]:
    env, vault_path = build_synthetic_kernel_vault(work_dir, projectors, anchored_rows=False, row_count=30)
    weave.run_ok(["weave-loom", "edge-vault", "--knn", "4"], env, "synthetic_unanchored_weave_failed")
    before = kernel_artifact_count(vault_path)
    proc = run(["kernel-build", "edge-vault", "--held-out-fraction", "1", "--top-k", "1"], env, timeout=60)
    after = kernel_artifact_count(vault_path)
    return assert_bad_case("unanchored_graph", proc, before, after)


def synthetic_bad_kernel_args(work_dir: Path, projectors: list[Path], args: list[str]) -> dict[str, Any]:
    env, vault_path = weave.build_synthetic_vault(work_dir, projectors, zero_row=False)
    weave.run_ok(["weave-loom", "edge-vault", "--knn", "1"], env, "synthetic_bad_arg_weave_failed")
    before = kernel_artifact_count(vault_path)
    proc = run(args, env, timeout=60)
    after = kernel_artifact_count(vault_path)
    return assert_bad_case(args[-1], proc, before, after)


def build_synthetic_kernel_vault(
    work_dir: Path,
    projectors: list[Path],
    anchored_rows: bool,
    row_count: int,
) -> tuple[dict[str, str], Path]:
    shutil.rmtree(work_dir, ignore_errors=True)
    home = work_dir / "calyx_home"
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", "edge-vault", "--panel-template", "text-default"], env, "synthetic_unanchored_create_failed")
    vault_path = home / "vaults" / json.loads(create.stdout)["vault_id"]
    for index, projector in enumerate(projectors):
        run_ok(
            [
                "add-lens",
                "edge-vault",
                "--name",
                f"facet_{index}",
                "--runtime",
                "external-cmd",
                "--endpoint",
                str(projector),
                "--shape",
                "Dense(2)",
                "--modality",
                "text",
            ],
            env,
            "synthetic_unanchored_add_lens_failed",
        )
    batch = work_dir / "rows.jsonl"
    rows = []
    for index in range(row_count):
        row: dict[str, Any] = {"text": f"ROW {index:02d}"}
        if anchored_rows:
            row["anchors"] = [
                {
                    "kind": "label:edge",
                    "value": "yes" if index % 2 else "no",
                    "source": "synthetic-kernel-fsv",
                    "confidence": 1.0,
                }
            ]
        rows.append(row)
    batch.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    run_ok(["ingest", "edge-vault", "--batch", str(batch.relative_to(ROOT)), "--output", "rows"], env, "synthetic_unanchored_ingest_failed")
    run_ok(
        [
            "readback",
            "cx-list",
            "--vault",
            str(vault_path),
            "--include-slots",
            "--limit",
            str(row_count),
            "--rebuild-base-page-index",
        ],
        env,
        "synthetic_cx_list_failed",
    )
    return env, vault_path


def kernel_artifact_count(vault_path: Path) -> int:
    root = vault_path / "idx" / "kernel"
    if not root.exists():
        return 0
    return len([path for path in root.glob("*/*.json") if path.is_file()])


def assert_bad_case(name: str, proc: weave.subprocess.CompletedProcess[bytes], before: int, after: int) -> dict[str, Any]:
    if proc.returncode == 0:
        raise KernelBuildFsvError("synthetic_bad_case_passed", {"case": name, "stdout": proc.stdout.decode("utf-8", "replace")})
    if before != after:
        raise KernelBuildFsvError("synthetic_bad_case_wrote_artifact", {"case": name, "before": before, "after": after})
    return {
        "returncode": proc.returncode,
        "before_kernel_artifacts": before,
        "after_kernel_artifacts": after,
        "stdout_fragment": proc.stdout.decode("utf-8", "replace")[-240:],
        "stderr_sha256": anchored.sha256_bytes(proc.stderr),
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
    real = build_real_kernel(work_dir, raw_root)
    synthetic = synthetic_edges(work_dir / "synthetic_edges")
    report = {"status": "ok", "real": real, "synthetic": synthetic}
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    if report_path.read_text(encoding="utf-8") != encoded + "\n":
        raise KernelBuildFsvError("report_readback_mismatch", {"path": str(report_path.relative_to(ROOT))})
    print(
        json.dumps(
            {
                "status": "ok",
                "vault_id": real["vault"]["vault_id"],
                "kernel_id": real["kernel_report"]["kernel"]["kernel_id"],
                "recall_ratio": real["kernel_report"]["recall"]["ratio"],
                "min_recall_ratio": real["kernel_report"]["recall"]["min_recall_ratio"],
                "kernel_members": real["kernel_report"]["kernel"]["members"],
                "index_rows": real["artifact_readback"]["index_rows"],
                "synthetic_edges": sorted(synthetic["edges"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
