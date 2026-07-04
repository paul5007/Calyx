#!/usr/bin/env python3
"""Verify Soccer Lab rows ingest Oracle occurrence metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CALYX = ROOT / "target" / "release" / "calyx"
ROWGEN = ROOT / "tools" / "data" / "generate_soccer_lab_rows.py"
DEFAULT_RAW = ROOT / "scratchpad" / "wc2026" / "raw"
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "oracle_metadata" / "report.json"

OUTCOME_FILES = {
    "matches.jsonl": {"rows": 1248, "oracle_rows": 1248, "domain": "soccer_lab.match_result", "action": "predict_match_result"},
    "teams-history.jsonl": {"rows": 2496, "oracle_rows": 2496, "domain": "soccer_lab.team_match_result", "action": "predict_team_match_result"},
    "team-tournaments.jsonl": {"rows": 240, "oracle_rows": 192, "domain": "soccer_lab.tournament_winner", "action": "predict_tournament_winner"},
    "matches-2026.jsonl": {"rows": 85, "oracle_rows": 85, "domain": "soccer_lab.match_result", "action": "predict_match_result"},
}


class OracleMetadataError(RuntimeError):
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


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise OracleMetadataError(reason, {"args": args, "stderr": proc.stderr.decode("utf-8", "replace")[-8000:]})
    return proc


def generate_rows(raw_root: Path, rows_root: Path) -> dict[str, Any]:
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
            "matches",
            "--only",
            "teams-history",
            "--only",
            "team-tournaments",
            "--only",
            "matches-2026",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    if proc.returncode != 0:
        raise OracleMetadataError("row_generation_failed", {"stderr": proc.stderr.decode("utf-8", "replace")})
    summary: dict[str, Any] = {}
    samples: list[dict[str, Any]] = []
    for name, expected in OUTCOME_FILES.items():
        path = rows_root / name
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        oracle_rows = [row for row in rows if "oracle" in row]
        anchor_rows = [row for row in rows if row.get("anchors")]
        if len(rows) != expected["rows"] or len(oracle_rows) != expected["oracle_rows"]:
            raise OracleMetadataError("row_oracle_count_mismatch", {"file": name, "rows": len(rows), "oracle_rows": len(oracle_rows), "expected": expected})
        if len(anchor_rows) != len(oracle_rows):
            raise OracleMetadataError("anchored_row_without_oracle", {"file": name, "anchor_rows": len(anchor_rows), "oracle_rows": len(oracle_rows)})
        domains: dict[str, int] = {}
        actions: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        for row in oracle_rows:
            oracle = row["oracle"]
            if oracle.get("domain") != expected["domain"] or oracle.get("action") != expected["action"]:
                raise OracleMetadataError("unexpected_oracle_domain_action", {"file": name, "oracle": oracle, "expected": expected})
            if oracle.get("t_secs", 0) < 0:
                raise OracleMetadataError("negative_oracle_t_secs", {"file": name, "oracle": oracle})
            first_anchor = row["anchors"][0]
            if oracle.get("outcome") != str(first_anchor["value"]) or oracle.get("outcome_kind") != first_anchor["kind"]:
                raise OracleMetadataError("oracle_outcome_anchor_mismatch", {"file": name, "oracle": oracle, "anchor": first_anchor})
            domains[oracle["domain"]] = domains.get(oracle["domain"], 0) + 1
            actions[oracle["action"]] = actions.get(oracle["action"], 0) + 1
            outcomes[oracle["outcome"]] = outcomes.get(oracle["outcome"], 0) + 1
        samples.extend(oracle_rows[:3])
        summary[name] = {
            "row_file": file_stat(path),
            "rows": len(rows),
            "anchor_rows": len(anchor_rows),
            "oracle_rows": len(oracle_rows),
            "domains": dict(sorted(domains.items())),
            "actions": dict(sorted(actions.items())),
            "outcomes": dict(sorted(outcomes.items())),
        }
    return {"files": summary, "samples": samples}


def build_real_sample_vault(work_dir: Path, samples: list[dict[str, Any]]) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", "soccer-oracle-metadata", "--panel-template", "text-default"], env, "create_vault_failed")
    created = json.loads(create.stdout)
    vault_path = home / "vaults" / created["vault_id"]
    sample_path = work_dir / "oracle-sample.jsonl"
    sample_path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in samples) + "\n", encoding="utf-8")
    ingest = run_ok(["ingest", "soccer-oracle-metadata", "--batch", str(sample_path.relative_to(ROOT)), "--output", "rows"], env, "sample_ingest_failed", timeout=240)
    ingest_rows = [json.loads(line) for line in ingest.stdout.decode("utf-8").splitlines() if line.strip()]
    if len(ingest_rows) != len(samples) or not all(row.get("new") for row in ingest_rows):
        raise OracleMetadataError("sample_ingest_row_count_mismatch", {"observed": ingest_rows})
    decoded = []
    for source_row, ingest_row in zip(samples, ingest_rows):
        cx_id = ingest_row["cx_id"]
        cx = run_ok(
            [
                "readback",
                "cx-list",
                "--vault",
                str(vault_path),
                "--cx-id",
                cx_id,
                "--include-slots",
                "--rebuild-base-page-index",
            ],
            env,
            "cx_list_failed",
            timeout=120,
        )
        rows = json.loads(cx.stdout)
        if len(rows) != 1:
            raise OracleMetadataError("cx_list_singleton_mismatch", {"cx_id": cx_id, "rows": len(rows)})
        base_hex = rows[0]["base_hex"]
        oracle = source_row["oracle"]
        assert_base_hex_contains(base_hex, oracle)
        series = run_ok(["readback", "recurrence-series", "--vault", str(vault_path), "--cx-id", cx_id], env, "recurrence_series_failed")
        series_json = json.loads(series.stdout)
        if series_json["occurrence_count"] != 1 or series_json["frequency"] != 1:
            raise OracleMetadataError("recurrence_count_mismatch", {"cx_id": cx_id, "series": series_json})
        context = decode_context(series_json["occurrences"][0]["context_hex"])
        expected_value = {"enum": oracle["outcome"]}
        if context.get("outcome_anchor", {}).get("value") != expected_value:
            raise OracleMetadataError("outcome_context_mismatch", {"cx_id": cx_id, "context": context, "oracle": oracle})
        consequence = context.get("consequence", {})
        if consequence.get("domain") != oracle["domain"] or consequence.get("outcome", {}).get("value") != expected_value:
            raise OracleMetadataError("consequence_context_mismatch", {"cx_id": cx_id, "context": context, "oracle": oracle})
        decoded.append(
            {
                "cx_id": cx_id,
                "source_entity": source_row["metadata"]["entity"],
                "domain": oracle["domain"],
                "action": oracle["action"],
                "outcome": oracle["outcome"],
                "base_hex_sha256": sha256_bytes(bytes.fromhex(base_hex)),
                "context": context,
                "slot_summary": rows[0]["slot_summary"],
            }
        )
    replay = run_ok(["ingest", "soccer-oracle-metadata", "--batch", str(sample_path.relative_to(ROOT)), "--output", "rows"], env, "sample_reingest_failed", timeout=240)
    replay_rows = [json.loads(line) for line in replay.stdout.decode("utf-8").splitlines() if line.strip()]
    if len(replay_rows) != len(samples) or any(row.get("new") for row in replay_rows):
        raise OracleMetadataError("sample_reingest_not_idempotent", {"rows": replay_rows})
    for ingest_row in ingest_rows:
        series = run_ok(["readback", "recurrence-series", "--vault", str(vault_path), "--cx-id", ingest_row["cx_id"]], env, "recurrence_replay_readback_failed")
        series_json = json.loads(series.stdout)
        if series_json["occurrence_count"] != 1:
            raise OracleMetadataError("recurrence_duplicate_after_reingest", {"cx_id": ingest_row["cx_id"], "series": series_json})
    recurrence_cf = run_ok(["readback", "--cf", "recurrence", "--vault", str(vault_path)], env, "recurrence_cf_readback_failed")
    return {
        "vault_id": created["vault_id"],
        "vault_path": str(vault_path.relative_to(ROOT)),
        "sample_file": file_stat(sample_path),
        "ingest_rows": len(ingest_rows),
        "replay_rows": len(replay_rows),
        "decoded_samples": decoded,
        "cx_list_sha256": sha256_bytes(b"".join(json.dumps(item, sort_keys=True).encode("utf-8") for item in decoded)),
        "recurrence_cf_stdout_sha256": sha256_bytes(recurrence_cf.stdout),
        "physical_readback": physical_readback(vault_path),
    }


def assert_base_hex_contains(base_hex: str, oracle: dict[str, Any]) -> None:
    raw = bytes.fromhex(base_hex)
    required = [
        b"oracle.domain",
        oracle["domain"].encode("utf-8"),
        b"oracle.action",
        oracle["action"].encode("utf-8"),
        b"oracle.effect",
        oracle["outcome"].encode("utf-8"),
        b"oracle.structured",
        b"true",
    ]
    missing = [item.decode("utf-8", "replace") for item in required if item not in raw]
    if missing:
        raise OracleMetadataError("base_metadata_bytes_missing", {"oracle": oracle, "missing": missing})


def decode_context(context_hex: str) -> dict[str, Any]:
    return json.loads(bytes.fromhex(context_hex).decode("utf-8"))


def physical_readback(vault_path: Path) -> dict[str, Any]:
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
            raise OracleMetadataError("missing_physical_file", {"name": name, "path": str(path.relative_to(ROOT))})
        stats[name] = file_stat(path)
    cf_stats = {}
    for cf_name in ["base", "recurrence", "anchors"]:
        files = sorted((vault_path / "cf" / cf_name).glob("*.sst"))
        if not files:
            raise OracleMetadataError("missing_cf_sst", {"cf": cf_name})
        cf_stats[cf_name] = {
            "sst_count": len(files),
            "bytes": sum(path.stat().st_size for path in files),
            "sha256_first": sha256_bytes(files[0].read_bytes()),
            "sha256_last": sha256_bytes(files[-1].read_bytes()),
        }
    return {"required_files": stats, "cf": cf_stats}


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    home = work_dir / "edge_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", "soccer-oracle-edge", "--panel-template", "text-default"], env, "edge_create_vault_failed")
    vault_id = json.loads(create.stdout)["vault_id"]
    vault_path = home / "vaults" / vault_id
    bad_cases = {
        "empty_domain": {"text": "synthetic bad domain", "oracle": {"domain": "", "action": "predict", "outcome": "yes", "outcome_kind": "label:edge"}},
        "negative_t_secs": {"text": "synthetic bad time", "oracle": {"domain": "edge", "action": "predict", "outcome": "yes", "outcome_kind": "label:edge", "t_secs": -1}},
        "unknown_outcome_kind": {"text": "synthetic bad kind", "oracle": {"domain": "edge", "action": "predict", "outcome": "yes", "outcome_kind": "not-a-kind"}},
        "missing_outcome": {"text": "synthetic missing outcome", "oracle": {"domain": "edge", "action": "predict"}},
    }
    observed = {}
    for name, row in bad_cases.items():
        path = work_dir / f"edge-{name}.jsonl"
        path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
        proc = run(["ingest", "soccer-oracle-edge", "--batch", str(path.relative_to(ROOT)), "--output", "rows"], env, timeout=60)
        if proc.returncode == 0:
            raise OracleMetadataError("synthetic_edge_unexpected_success", {"edge": name, "stdout": proc.stdout.decode("utf-8", "replace")})
        cx = run_ok(["readback", "cx-list", "--vault", str(vault_path), "--allow-unbounded", "--rebuild-base-page-index"], env, "edge_cx_list_failed")
        if json.loads(cx.stdout):
            raise OracleMetadataError("synthetic_edge_wrote_rows", {"edge": name, "cx_list": cx.stdout.decode("utf-8", "replace")})
        observed[name] = {
            "returncode": proc.returncode,
            "stderr_sha256": sha256_bytes(proc.stderr),
            "stderr_tail": proc.stderr.decode("utf-8", "replace")[-500:],
            "row_file": file_stat(path),
            "cx_list_after": [],
        }
    happy = {
        "text": "synthetic oracle happy",
        "oracle": {"domain": "edge", "action": "predict", "outcome": "yes", "outcome_kind": "label:edge", "t_secs": 1700000000},
        "anchors": [{"kind": "label:edge", "value": "yes", "source": "synthetic-edge-fsv", "confidence": 1.0}],
    }
    happy_path = work_dir / "edge-happy.jsonl"
    happy_path.write_text(json.dumps(happy, sort_keys=True) + "\n", encoding="utf-8")
    ingest = run_ok(["ingest", "soccer-oracle-edge", "--batch", str(happy_path.relative_to(ROOT)), "--output", "rows"], env, "edge_happy_ingest_failed")
    row = json.loads(ingest.stdout.decode("utf-8").splitlines()[0])
    series = run_ok(["readback", "recurrence-series", "--vault", str(vault_path), "--cx-id", row["cx_id"]], env, "edge_happy_recurrence_failed")
    series_json = json.loads(series.stdout)
    context = decode_context(series_json["occurrences"][0]["context_hex"])
    if context.get("outcome_anchor", {}).get("value") != {"enum": "yes"}:
        raise OracleMetadataError("synthetic_happy_context_mismatch", {"context": context})
    observed["happy_path"] = {
        "cx_id": row["cx_id"],
        "occurrence_count": series_json["occurrence_count"],
        "context": context,
        "row_file": file_stat(happy_path),
    }
    return {"vault_id": vault_id, "vault_path": str(vault_path.relative_to(ROOT)), "edges": observed}


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
    generation = generate_rows(raw_root, rows_root)
    vault = build_real_sample_vault(work_dir, generation["samples"])
    edges = synthetic_edges(work_dir / "synthetic_edges")
    report = {
        "status": "ok",
        "generation": {key: value for key, value in generation.items() if key != "samples"},
        "real_sample_vault": vault,
        "synthetic_edges": edges,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    if report_path.read_text(encoding="utf-8") != encoded + "\n":
        raise OracleMetadataError("report_readback_mismatch", {"path": str(report_path.relative_to(ROOT))})
    print(
        json.dumps(
            {
                "status": "ok",
                "oracle_rows": sum(item["oracle_rows"] for item in report["generation"]["files"].values()),
                "sample_ingest_rows": vault["ingest_rows"],
                "sample_replay_rows": vault["replay_rows"],
                "synthetic_edges": sorted(edges["edges"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
