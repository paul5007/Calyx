#!/usr/bin/env python3
"""Verify Soccer Lab Oracle occurrence contexts are ingested and Oracle-readable."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CALYX = ROOT / "target" / "release" / "calyx"
ROWGEN = ROOT / "tools" / "data" / "generate_soccer_lab_rows.py"
DEFAULT_RAW = ROOT / "scratchpad" / "wc2026" / "raw"
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "oracle_context_ingest" / "report.json"

REAL_FILES = {
    "matches.jsonl": {"rows": 1248, "oracle_rows": 1248},
    "matches-2026.jsonl": {"rows": 85, "oracle_rows": 85},
    "teams-history.jsonl": {"rows": 2496, "oracle_rows": 2496},
    "team-tournaments.jsonl": {"rows": 240, "oracle_rows": 192},
}

DOMAIN_ACTIONS = {
    "soccer_lab.match_result": "predict_match_result",
    "soccer_lab.team_match_result": "predict_team_match_result",
    "soccer_lab.tournament_winner": "predict_tournament_winner",
}

PREDICT_FIXTURES = {
    "soccer_lab.match_result": {
        "action_id": "predict_match_result",
        "expected_outcome": {"enum": "home_win"},
    },
    "soccer_lab.team_match_result": {
        "action_id": "predict_team_match_result",
        "expected_outcome": {"enum": "win"},
    },
}


class OracleContextIngestError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def require(condition: bool, reason: str, detail: dict[str, Any] | None = None) -> None:
    if not condition:
        raise OracleContextIngestError(reason, detail)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_stat(path: Path) -> dict[str, Any]:
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
        raise OracleContextIngestError(reason, {"args": args, "stderr": proc.stderr.decode("utf-8", "replace")[-8000:]})
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
            "matches-2026",
            "--only",
            "teams-history",
            "--only",
            "team-tournaments",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )
    if proc.returncode != 0:
        raise OracleContextIngestError("row_generation_failed", {"stderr": proc.stderr.decode("utf-8", "replace")[-8000:]})
    summary: dict[str, Any] = {}
    rows_by_file: dict[str, list[dict[str, Any]]] = {}
    full_domain_counts: Counter[str] = Counter()
    full_action_counts: Counter[str] = Counter()
    full_outcome_counts: Counter[str] = Counter()
    for name, expected in REAL_FILES.items():
        path = rows_root / name
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows_with_oracle = [row for row in rows if "oracle" in row]
        require(len(rows) == expected["rows"], "row_count_mismatch", {"file": name, "rows": len(rows), "expected": expected})
        require(len(rows_with_oracle) == expected["oracle_rows"], "oracle_row_count_mismatch", {"file": name, "oracle_rows": len(rows_with_oracle), "expected": expected})
        for row in rows_with_oracle:
            oracle = row["oracle"]
            require(oracle.get("domain") in DOMAIN_ACTIONS, "unknown_oracle_domain", {"file": name, "oracle": oracle})
            require(oracle.get("action") == DOMAIN_ACTIONS[oracle["domain"]], "oracle_action_mismatch", {"file": name, "oracle": oracle})
            require(row.get("anchors"), "oracle_row_missing_anchor", {"file": name, "row": row})
            require(str(row["anchors"][0]["value"]) == oracle.get("outcome"), "oracle_anchor_mismatch", {"file": name, "oracle": oracle, "anchor": row["anchors"][0]})
            full_domain_counts[oracle["domain"]] += 1
            full_action_counts[oracle["action"]] += 1
            full_outcome_counts[f"{oracle['domain']}={oracle['outcome']}"] += 1
        rows_by_file[name] = rows_with_oracle
        summary[name] = {
            "file": file_stat(path),
            "rows": len(rows),
            "oracle_rows": len(rows_with_oracle),
        }
    selected_oracle_rows = [
        *rows_by_file["matches-2026.jsonl"],
        *rows_by_file["team-tournaments.jsonl"],
        *rows_by_file["teams-history.jsonl"][:201],
    ]
    selected_domain_counts = Counter(row["oracle"]["domain"] for row in selected_oracle_rows)
    selected_action_counts = Counter(row["oracle"]["action"] for row in selected_oracle_rows)
    selected_outcome_counts = Counter(f"{row['oracle']['domain']}={row['oracle']['outcome']}" for row in selected_oracle_rows)
    return {
        "files": summary,
        "oracle_rows": selected_oracle_rows,
        "full_domain_counts": dict(sorted(full_domain_counts.items())),
        "full_action_counts": dict(sorted(full_action_counts.items())),
        "full_outcome_counts": dict(sorted(full_outcome_counts.items())),
        "selected_domain_counts": dict(sorted(selected_domain_counts.items())),
        "selected_action_counts": dict(sorted(selected_action_counts.items())),
        "selected_outcome_counts": dict(sorted(selected_outcome_counts.items())),
    }


def build_context_vault(work_dir: Path, oracle_rows: list[dict[str, Any]]) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    vault_name = "soccer-oracle-context-ingest"
    create = run_ok(["create-vault", vault_name, "--panel-template", "text-default"], env, "create_vault_failed")
    created = json.loads(create.stdout)
    vault_id = created["vault_id"]
    vault_path = home / "vaults" / vault_id
    batch_path = work_dir / "oracle-context-batch.jsonl"
    batch_path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in oracle_rows) + "\n", encoding="utf-8")
    ingest = run_ok(["ingest", vault_name, "--batch", str(batch_path.relative_to(ROOT)), "--output", "rows"], env, "ingest_failed", timeout=300)
    ingest_rows = [json.loads(line) for line in ingest.stdout.decode("utf-8").splitlines() if line.strip()]
    require(len(ingest_rows) == len(oracle_rows), "ingest_row_count_mismatch", {"ingest_rows": len(ingest_rows), "oracle_rows": len(oracle_rows)})
    require(sum(1 for row in ingest_rows if row.get("new")) == len(oracle_rows), "ingest_new_count_mismatch", {"sample": ingest_rows[:5]})
    replay = run_ok(["ingest", vault_name, "--batch", str(batch_path.relative_to(ROOT)), "--output", "rows"], env, "reingest_failed", timeout=300)
    replay_rows = [json.loads(line) for line in replay.stdout.decode("utf-8").splitlines() if line.strip()]
    require(len(replay_rows) == len(oracle_rows), "replay_row_count_mismatch", {"rows": len(replay_rows)})
    require(not any(row.get("new") for row in replay_rows), "replay_created_new_rows", {"sample": replay_rows[:5]})

    source_by_cx = {ingest_row["cx_id"]: source_row for ingest_row, source_row in zip(ingest_rows, oracle_rows)}
    cx_readback = run_ok(
        ["readback", "cx-list", "--vault", str(vault_path), "--include-slots", "--limit", "24", "--rebuild-base-page-index"],
        env,
        "cx_list_failed",
        timeout=180,
    )
    cx_rows = json.loads(cx_readback.stdout)
    require(len(cx_rows) == 24, "cx_list_sample_count_mismatch", {"rows": len(cx_rows)})
    decoded_samples = decode_sample_contexts(env, vault_path, source_by_cx)
    recurrence_cf = run_ok(["readback", "--cf", "recurrence", "--vault", str(vault_path)], env, "recurrence_cf_failed", timeout=180)
    cf_summary = decode_recurrence_cf(recurrence_cf.stdout, len(oracle_rows))
    predict_results = oracle_predict_readbacks(work_dir, env, vault_path, vault_id, vault_name)
    return {
        "vault_name": vault_name,
        "vault_id": vault_id,
        "vault_path": str(vault_path.relative_to(ROOT)),
        "vault_salt": vault_salt(vault_id, vault_name),
        "batch_file": file_stat(batch_path),
        "ingest_rows": len(ingest_rows),
        "replay_rows": len(replay_rows),
        "ingest_stdout_sha256": sha256_bytes(ingest.stdout),
        "replay_stdout_sha256": sha256_bytes(replay.stdout),
        "cx_list_sample_rows": len(cx_rows),
        "cx_list_sample_sha256": sha256_bytes(cx_readback.stdout),
        "decoded_samples": decoded_samples,
        "recurrence_cf": cf_summary,
        "oracle_predict_readbacks": predict_results,
        "physical_readback": physical_readback(vault_path),
    }


def decode_sample_contexts(env: dict[str, str], vault_path: Path, source_by_cx: dict[str, dict[str, Any]]) -> dict[str, Any]:
    selected: dict[str, str] = {}
    for cx_id, row in source_by_cx.items():
        domain = row["oracle"]["domain"]
        selected.setdefault(domain, cx_id)
    decoded = {}
    for domain, cx_id in sorted(selected.items()):
        series = run_ok(["readback", "recurrence-series", "--vault", str(vault_path), "--cx-id", cx_id], env, "recurrence_series_failed")
        payload = json.loads(series.stdout)
        require(payload.get("occurrence_count") == 1 and payload.get("frequency") == 1, "sample_series_count_mismatch", {"domain": domain, "series": payload})
        context = json.loads(bytes.fromhex(payload["occurrences"][0]["context_hex"]).decode("utf-8"))
        oracle = source_by_cx[cx_id]["oracle"]
        expected_value = {"enum": oracle["outcome"]}
        require(context.get("action_id") == oracle["action"], "sample_context_action_mismatch", {"domain": domain, "context": context, "oracle": oracle})
        require(context.get("outcome_anchor", {}).get("value") == expected_value, "sample_context_outcome_mismatch", {"domain": domain, "context": context, "oracle": oracle})
        consequence = context.get("consequence", {})
        require(consequence.get("action_or_event") == oracle["action"], "sample_context_edge_action_mismatch", {"domain": domain, "context": context, "oracle": oracle})
        require(consequence.get("domain") == oracle["domain"], "sample_context_edge_domain_mismatch", {"domain": domain, "context": context, "oracle": oracle})
        require(consequence.get("outcome", {}).get("value") == expected_value, "sample_context_edge_outcome_mismatch", {"domain": domain, "context": context, "oracle": oracle})
        decoded[domain] = {"cx_id": cx_id, "context": context, "series_sha256": sha256_bytes(series.stdout)}
    return decoded


def decode_recurrence_cf(stdout: bytes, expected_unique: int) -> dict[str, Any]:
    unique: dict[str, dict[str, Any]] = {}
    raw_rows = 0
    context_domains: Counter[str] = Counter()
    for line in stdout.decode("utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        require(len(parts) >= 8 and parts[0] == "CF" and parts[1] == "recurrence", "malformed_recurrence_cf_line", {"line": line[:200]})
        key = parts[parts.index("KEY") + 1]
        value_hex = parts[parts.index("VALUE") + 1]
        raw_rows += 1
        if key in unique:
            continue
        row = json.loads(bytes.fromhex(value_hex).decode("utf-8"))
        context = bytes(row["context"]["bytes"]).decode("utf-8")
        context_json = json.loads(context)
        domain = context_json["consequence"]["domain"]
        context_domains[domain] += 1
        unique[key] = {"id": row["id"], "t_k": row["t_k"], "context": context_json}
    require(len(unique) == expected_unique, "recurrence_unique_count_mismatch", {"unique": len(unique), "expected": expected_unique, "raw_rows": raw_rows})
    return {
        "raw_rows": raw_rows,
        "unique_rows": len(unique),
        "context_domains": dict(sorted(context_domains.items())),
        "stdout_sha256": sha256_bytes(stdout),
    }


def oracle_predict_readbacks(work_dir: Path, env: dict[str, str], vault_path: Path, vault_id: str, vault_name: str) -> dict[str, Any]:
    out = {}
    for domain, spec in PREDICT_FIXTURES.items():
        fixture_path = work_dir / f"oracle-predict-{domain.rsplit('.', 1)[-1]}.json"
        fixture = predict_fixture(domain, spec["action_id"])
        fixture_path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        proc = run_ok(
            [
                "readback",
                "oracle_predict",
                "--vault",
                str(vault_path),
                "--fixture",
                str(fixture_path.relative_to(ROOT)),
                "--vault-id",
                vault_id,
                "--salt",
                vault_salt(vault_id, vault_name),
            ],
            env,
            "oracle_predict_failed",
            timeout=180,
        )
        payload = json.loads(proc.stdout)
        prediction = payload["prediction"]
        require(payload.get("rows_written", 0) > 0, "oracle_predict_fixture_wrote_no_support_rows", {"domain": domain, "payload": payload})
        require(prediction.get("outcome") == spec["expected_outcome"], "oracle_predict_outcome_mismatch", {"domain": domain, "prediction": prediction, "expected": spec})
        require(prediction.get("bound", {}).get("sufficient") is True, "oracle_predict_not_sufficient", {"domain": domain, "prediction": prediction})
        require(prediction.get("confidence", -1) >= 0, "oracle_predict_bad_confidence", {"domain": domain, "prediction": prediction})
        out[domain] = {
            "fixture": file_stat(fixture_path),
            "stdout_sha256": sha256_bytes(proc.stdout),
            "rows_written": payload["rows_written"],
            "outcome": prediction["outcome"],
            "confidence": prediction["confidence"],
            "recurrence_observations": prediction.get("recurrence_observations"),
            "bound": prediction["bound"],
            "consequence_count": len(prediction.get("consequences", [])),
        }
    return out


def predict_fixture(domain: str, action_id: str) -> dict[str, Any]:
    return {
        "domain": domain,
        "action_id": action_id,
        "panel": minimal_panel(),
        "I_panel_oracle": 1.05,
        "outcome_entropy_bits": 1.0,
        "slot_bits": [{"slot": 0, "bits": 0.55}, {"slot": 1, "bits": 0.50}],
        "prediction_observations": [],
        "self_consistency_series": self_consistency_series(),
        "n_samples": 120,
        "trust": "trusted",
        "clock_ts": 1783132200,
    }


def minimal_panel() -> dict[str, Any]:
    return {
        "version": 640,
        "slots": [minimal_slot(0), minimal_slot(1)],
        "created_at": 1783132200,
        "kernel_ref": None,
        "guard_ref": None,
    }


def minimal_slot(slot_id: int) -> dict[str, Any]:
    return {
        "slot_id": slot_id,
        "slot_key": {"id": slot_id, "key": f"oracle-context-slot-{slot_id}"},
        "lens_id": ("%02x" % (slot_id + 1)) * 16,
        "shape": {"dense": 2},
        "modality": "text",
        "asymmetry": "none",
        "quant": "none",
        "axis": "soccer_lab.oracle_context_ingest",
        "retrieval_only": False,
        "excluded_from_dedup": False,
        "bits_about": {},
        "state": "active",
        "added_at_panel_version": 640,
    }


def self_consistency_series() -> list[list[dict[str, Any]]]:
    series = []
    for idx in range(10):
        outcome = {"text": "consistent"}
        truth = {"text": "consistent" if idx < 9 else "other"}
        series.append([{"outcome": outcome, "ground_truth": truth}, {"outcome": outcome, "ground_truth": truth}])
    for idx in range(40):
        outcome = {"text": "valid"}
        series.append([{"outcome": outcome, "ground_truth": outcome}])
    return series


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
        require(path.is_file(), "physical_file_missing", {"name": name, "path": str(path.relative_to(ROOT))})
        stats[name] = file_stat(path)
    cf_stats = {}
    for cf_name in ["base", "anchors", "recurrence"]:
        files = sorted((vault_path / "cf" / cf_name).glob("*.sst"))
        require(files, "physical_cf_missing", {"cf": cf_name})
        cf_stats[cf_name] = {
            "sst_count": len(files),
            "bytes": sum(path.stat().st_size for path in files),
            "first_sha256": sha256_bytes(files[0].read_bytes()),
            "last_sha256": sha256_bytes(files[-1].read_bytes()),
        }
    return {"files": stats, "cf": cf_stats}


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    home = work_dir / "edge_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", "soccer-oracle-context-edge", "--panel-template", "text-default"], env, "edge_create_failed")
    vault_id = json.loads(create.stdout)["vault_id"]
    vault_path = home / "vaults" / vault_id
    bad_rows = {
        "empty_domain": {"text": "edge empty domain", "oracle": {"domain": "", "action": "predict", "outcome": "yes", "outcome_kind": "label:edge"}},
        "empty_action": {"text": "edge empty action", "oracle": {"domain": "edge", "action": "", "outcome": "yes", "outcome_kind": "label:edge"}},
        "missing_outcome": {"text": "edge missing outcome", "oracle": {"domain": "edge", "action": "predict", "outcome_kind": "label:edge"}},
        "negative_t_secs": {"text": "edge negative time", "oracle": {"domain": "edge", "action": "predict", "outcome": "yes", "outcome_kind": "label:edge", "t_secs": -1}},
    }
    observed = {}
    for name, row in bad_rows.items():
        path = work_dir / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
        proc = run(["ingest", "soccer-oracle-context-edge", "--batch", str(path.relative_to(ROOT)), "--output", "rows"], env, timeout=60)
        if proc.returncode == 0:
            raise OracleContextIngestError("synthetic_edge_unexpected_success", {"edge": name, "stdout": proc.stdout.decode("utf-8", "replace")})
        cx = run_ok(["readback", "cx-list", "--vault", str(vault_path), "--allow-unbounded", "--rebuild-base-page-index"], env, "edge_cx_list_failed")
        require(json.loads(cx.stdout) == [], "synthetic_edge_wrote_rows", {"edge": name})
        observed[name] = {"returncode": proc.returncode, "stderr_tail": proc.stderr.decode("utf-8", "replace")[-500:], "row_file": file_stat(path)}
    happy = {
        "text": "synthetic known oracle context",
        "anchors": [{"kind": "label:edge", "value": "yes", "source": "synthetic-known-input", "confidence": 1.0}],
        "oracle": {"domain": "edge", "action": "predict", "outcome": "yes", "outcome_kind": "label:edge", "grounded": True, "t_secs": 1700000000},
    }
    happy_path = work_dir / "happy.jsonl"
    happy_path.write_text(json.dumps(happy, sort_keys=True) + "\n", encoding="utf-8")
    ingest = run_ok(["ingest", "soccer-oracle-context-edge", "--batch", str(happy_path.relative_to(ROOT)), "--output", "rows"], env, "edge_happy_failed")
    cx_id = json.loads(ingest.stdout.decode("utf-8").splitlines()[0])["cx_id"]
    series = run_ok(["readback", "recurrence-series", "--vault", str(vault_path), "--cx-id", cx_id], env, "edge_happy_series_failed")
    context = json.loads(bytes.fromhex(json.loads(series.stdout)["occurrences"][0]["context_hex"]).decode("utf-8"))
    expected = {"action_id": "predict", "outcome_anchor": {"value": {"enum": "yes"}}, "consequence": {"action_or_event": "predict", "domain": "edge", "outcome": {"value": {"enum": "yes"}}}}
    require(context == expected, "synthetic_happy_context_mismatch", {"context": context, "expected": expected})
    observed["happy"] = {"cx_id": cx_id, "context": context, "row_file": file_stat(happy_path)}
    return {"vault_id": vault_id, "vault_path": str(vault_path.relative_to(ROOT)), "edges": observed}


def vault_salt(vault_id: str, name: str) -> str:
    return f"calyx-cli-vault:{vault_id}:{name}"


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
    generated = generate_rows(raw_root, rows_root)
    vault = build_context_vault(work_dir / "real_context_vault", generated["oracle_rows"])
    edges = synthetic_edges(work_dir / "synthetic_edges")
    report = {
        "status": "ok",
        "generation": {key: value for key, value in generated.items() if key != "oracle_rows"},
        "real_context_vault": vault,
        "synthetic_edges": edges,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    require(report_path.read_text(encoding="utf-8") == encoded + "\n", "report_readback_mismatch", {"path": str(report_path.relative_to(ROOT))})
    print(
        json.dumps(
            {
                "status": "ok",
                "oracle_rows": sum(generated["selected_domain_counts"].values()),
                "domains": generated["selected_domain_counts"],
                "recurrence_unique_rows": vault["recurrence_cf"]["unique_rows"],
                "oracle_predict_domains": sorted(vault["oracle_predict_readbacks"]),
                "synthetic_edges": sorted(edges["edges"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
