#!/usr/bin/env python3
"""Verify the Soccer Lab Oracle occurrence-context format."""

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
SCHEMA_PATH = ROOT / "docs" / "data" / "soccer_lab_oracle_context_schema.json"
DOC_PATH = ROOT / "docs" / "SOCCER_LAB_ORACLE_CONTEXTS.md"
INGEST_SOURCE = ROOT / "crates" / "calyx-cli" / "src" / "cmd" / "ingest" / "oracle_event.rs"
PREDICT_CONTEXT = ROOT / "crates" / "calyx-oracle" / "src" / "predict" / "context.rs"
EXPANSION_CONTEXT = ROOT / "crates" / "calyx-oracle" / "src" / "butterfly" / "context.rs"
DEFAULT_RAW = ROOT / "scratchpad" / "wc2026" / "raw"
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "oracle_context_format" / "report.json"

EXPECTED_REAL = {
    "matches.jsonl": {
        "rows": 1248,
        "oracle_rows": 1248,
        "domain_class": "fixture",
        "domain": "soccer_lab.match_result",
        "action": "predict_match_result",
        "entity": "match",
    },
    "matches-2026.jsonl": {
        "rows": 85,
        "oracle_rows": 85,
        "domain_class": "fixture",
        "domain": "soccer_lab.match_result",
        "action": "predict_match_result",
        "entity": "match_2026",
    },
    "teams-history.jsonl": {
        "rows": 2496,
        "oracle_rows": 2496,
        "domain_class": "team",
        "domain": "soccer_lab.team_match_result",
        "action": "predict_team_match_result",
        "entity": "team_match_history",
    },
    "team-tournaments.jsonl": {
        "rows": 240,
        "oracle_rows": 192,
        "domain_class": "team",
        "domain": "soccer_lab.tournament_winner",
        "action": "predict_tournament_winner",
        "entity": "team_tournament",
    },
    "players.jsonl": {
        "rows": 10401,
        "oracle_rows": 0,
        "domain_class": "player",
        "entity": "player",
    },
}


class ContextFormatError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


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
        raise ContextFormatError(reason, {"args": args, "stderr": proc.stderr.decode("utf-8", "replace")[-8000:]})
    return proc


def require(condition: bool, reason: str, detail: dict[str, Any] | None = None) -> None:
    if not condition:
        raise ContextFormatError(reason, detail)


def load_schema() -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    require(schema.get("schema_id") == "soccer_lab.oracle_context.v1", "schema_id_mismatch", {"schema_id": schema.get("schema_id")})
    require(schema.get("version") == 1, "schema_version_mismatch", {"version": schema.get("version")})
    domains = schema.get("domains")
    require(isinstance(domains, dict), "schema_domains_missing")
    for domain_class in ["fixture", "team", "player"]:
        spec = domains.get(domain_class)
        require(isinstance(spec, dict), "schema_domain_missing", {"domain_class": domain_class})
        for key in ["domain_ids", "entities", "actions", "outcome_kinds", "required_metadata", "prediction_context", "expansion_context"]:
            require(key in spec, "schema_domain_key_missing", {"domain_class": domain_class, "key": key})
        validate_context(spec["prediction_context"], domain_class, prediction=True)
        validate_context(spec["expansion_context"], domain_class, prediction=False)
    return schema


def validate_context(context: dict[str, Any], name: str, prediction: bool) -> None:
    require(context.get("action_id"), "context_action_id_missing", {"name": name})
    edges = [context.get("consequence")] if "consequence" in context else context.get("consequences", [])
    require(isinstance(edges, list) and edges, "context_edges_missing", {"name": name, "context": context})
    if prediction:
        require(context.get("outcome_anchor", {}).get("value"), "prediction_outcome_anchor_missing", {"name": name, "context": context})
    for edge in edges:
        require(isinstance(edge, dict), "context_edge_not_object", {"name": name, "edge": edge})
        require(edge.get("action_or_event"), "context_edge_action_missing", {"name": name, "edge": edge})
        require(edge.get("domain"), "context_edge_domain_missing", {"name": name, "edge": edge})
        require(edge.get("outcome", {}).get("value"), "context_edge_outcome_missing", {"name": name, "edge": edge})
        require(edge.get("grounded", True) is True and edge.get("provisional", False) is False, "context_edge_not_grounded", {"name": name, "edge": edge})


def verify_docs_and_sources(schema: dict[str, Any]) -> dict[str, Any]:
    doc = DOC_PATH.read_text(encoding="utf-8")
    for token in [
        "PredictionContext JSON",
        "ExpansionContext JSON",
        "oracle.domain",
        "oracle.action",
        "oracle.effect",
        "oracle.structured",
        "soccer_lab.match_result",
        "soccer_lab.team_match_result",
        "soccer_lab.player_impact",
    ]:
        require(token in doc, "doc_required_token_missing", {"token": token})
    source_checks = {
        str(INGEST_SOURCE.relative_to(ROOT)): ["\"action_id\"", "\"action_or_event\"", "\"outcome_anchor\"", "\"consequence\"", "\"grounded\"", "\"provisional\""],
        str(PREDICT_CONTEXT.relative_to(ROOT)): ["struct PredictionContext", "outcome_anchor", "consequence", "consequences", "matches_action"],
        str(EXPANSION_CONTEXT.relative_to(ROOT)): ["struct ExpansionContext", "action_or_event", "grounded", "provisional"],
    }
    source_stats = {}
    for rel, tokens in source_checks.items():
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        missing = [token for token in tokens if token not in text]
        require(not missing, "source_required_token_missing", {"path": rel, "missing": missing})
        source_stats[rel] = file_stat(path)
    domain_ids = sorted({item for spec in schema["domains"].values() for item in spec["domain_ids"]})
    for domain in domain_ids:
        require(domain in doc or domain == "soccer_lab.player_impact", "domain_not_documented", {"domain": domain})
    return {"doc": file_stat(DOC_PATH), "schema": file_stat(SCHEMA_PATH), "sources": source_stats}


def generate_rows(raw_root: Path, rows_root: Path) -> dict[str, list[dict[str, Any]]]:
    if rows_root.exists():
        shutil.rmtree(rows_root)
    rows_root.mkdir(parents=True)
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
            "--only",
            "players",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )
    if proc.returncode != 0:
        raise ContextFormatError("row_generation_failed", {"stderr": proc.stderr.decode("utf-8", "replace")[-8000:]})
    out = {}
    for name in EXPECTED_REAL:
        path = rows_root / name
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        out[name] = rows
    return out


def verify_real_rows(schema: dict[str, Any], rows_root: Path, rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    summary = {}
    samples = []
    for name, expected in EXPECTED_REAL.items():
        payloads = rows[name]
        spec = schema["domains"][expected["domain_class"]]
        oracle_rows = [row for row in payloads if "oracle" in row]
        require(len(payloads) == expected["rows"], "real_row_count_mismatch", {"file": name, "rows": len(payloads), "expected": expected["rows"]})
        require(len(oracle_rows) == expected["oracle_rows"], "real_oracle_row_count_mismatch", {"file": name, "oracle_rows": len(oracle_rows), "expected": expected["oracle_rows"]})
        entities: dict[str, int] = {}
        domains: dict[str, int] = {}
        actions: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        for row in payloads:
            metadata = row.get("metadata", {})
            entity = metadata.get("entity")
            entities[entity] = entities.get(entity, 0) + 1
            require(entity == expected["entity"], "real_entity_mismatch", {"file": name, "entity": entity, "expected": expected["entity"]})
            for key in schema["metadata_keys"]["row_metadata_required"]:
                require(metadata.get(key) not in (None, ""), "row_metadata_required_missing", {"file": name, "key": key, "metadata": metadata})
            for key in spec["required_metadata"]:
                if key in metadata:
                    require(metadata[key] != "", "row_entity_metadata_empty", {"file": name, "key": key, "metadata": metadata})
        for row in oracle_rows:
            oracle = row["oracle"]
            require(oracle.get("domain") == expected.get("domain"), "real_oracle_domain_mismatch", {"file": name, "oracle": oracle, "expected": expected})
            require(oracle.get("action") == expected.get("action"), "real_oracle_action_mismatch", {"file": name, "oracle": oracle, "expected": expected})
            require(oracle.get("domain") in spec["domain_ids"], "real_oracle_domain_not_in_schema", {"file": name, "oracle": oracle})
            require(oracle.get("action") in spec["actions"], "real_oracle_action_not_in_schema", {"file": name, "oracle": oracle})
            require(oracle.get("outcome_kind") in spec["outcome_kinds"], "real_oracle_kind_not_in_schema", {"file": name, "oracle": oracle})
            require(oracle.get("grounded") is True, "real_oracle_not_grounded", {"file": name, "oracle": oracle})
            if "t_secs" in oracle:
                require(isinstance(oracle["t_secs"], int) and oracle["t_secs"] >= 0, "real_oracle_bad_t_secs", {"file": name, "oracle": oracle})
            anchors = row.get("anchors", [])
            require(anchors, "real_oracle_without_anchor", {"file": name, "row": row})
            require(str(anchors[0].get("value")) == oracle.get("outcome"), "real_oracle_anchor_mismatch", {"file": name, "oracle": oracle, "anchor": anchors[0]})
            domains[oracle["domain"]] = domains.get(oracle["domain"], 0) + 1
            actions[oracle["action"]] = actions.get(oracle["action"], 0) + 1
            outcomes[oracle["outcome"]] = outcomes.get(oracle["outcome"], 0) + 1
        samples.extend(oracle_rows[:2])
        summary[name] = {
            "file": file_stat(rows_root / name),
            "rows": len(payloads),
            "oracle_rows": len(oracle_rows),
            "entities": dict(sorted(entities.items())),
            "domains": dict(sorted(domains.items())),
            "actions": dict(sorted(actions.items())),
            "outcomes": dict(sorted(outcomes.items())),
        }
    return {"files": summary, "samples": samples}


def verify_ingested_contexts(work_dir: Path, samples: list[dict[str, Any]]) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", "soccer-oracle-context", "--panel-template", "text-default"], env, "create_vault_failed")
    vault_id = json.loads(create.stdout)["vault_id"]
    vault_path = home / "vaults" / vault_id
    sample_path = work_dir / "oracle-context-samples.jsonl"
    sample_path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in samples) + "\n", encoding="utf-8")
    ingest = run_ok(["ingest", "soccer-oracle-context", "--batch", str(sample_path.relative_to(ROOT)), "--output", "rows"], env, "ingest_samples_failed", timeout=240)
    ingest_rows = [json.loads(line) for line in ingest.stdout.decode("utf-8").splitlines() if line.strip()]
    require(len(ingest_rows) == len(samples), "ingest_sample_count_mismatch", {"rows": ingest_rows})
    decoded = []
    for source_row, ingest_row in zip(samples, ingest_rows):
        oracle = source_row["oracle"]
        cx_id = ingest_row["cx_id"]
        cx_proc = run_ok(
            ["readback", "cx-list", "--vault", str(vault_path), "--cx-id", cx_id, "--include-slots", "--rebuild-base-page-index"],
            env,
            "cx_list_failed",
        )
        cx_rows = json.loads(cx_proc.stdout)
        require(len(cx_rows) == 1, "cx_list_singleton_mismatch", {"cx_id": cx_id, "rows": cx_rows})
        base = bytes.fromhex(cx_rows[0]["base_hex"])
        for token in [b"oracle.domain", b"oracle.action", b"oracle.effect", b"oracle.structured", oracle["domain"].encode(), oracle["action"].encode()]:
            require(token in base, "base_metadata_token_missing", {"cx_id": cx_id, "token": token.decode("utf-8", "replace")})
        series = run_ok(["readback", "recurrence-series", "--vault", str(vault_path), "--cx-id", cx_id], env, "recurrence_series_failed")
        series_json = json.loads(series.stdout)
        require(series_json.get("occurrence_count") == 1, "occurrence_count_mismatch", {"cx_id": cx_id, "series": series_json})
        context = json.loads(bytes.fromhex(series_json["occurrences"][0]["context_hex"]).decode("utf-8"))
        expected_value = {"enum": oracle["outcome"]}
        require(context.get("action_id") == oracle["action"], "persisted_context_action_id_mismatch", {"cx_id": cx_id, "context": context, "oracle": oracle})
        require(context.get("outcome_anchor", {}).get("value") == expected_value, "persisted_context_outcome_mismatch", {"cx_id": cx_id, "context": context, "oracle": oracle})
        consequence = context.get("consequence", {})
        require(consequence.get("action_or_event") == oracle["action"], "persisted_edge_action_mismatch", {"cx_id": cx_id, "context": context, "oracle": oracle})
        require(consequence.get("domain") == oracle["domain"], "persisted_edge_domain_mismatch", {"cx_id": cx_id, "context": context, "oracle": oracle})
        require(consequence.get("outcome", {}).get("value") == expected_value, "persisted_edge_outcome_mismatch", {"cx_id": cx_id, "context": context, "oracle": oracle})
        require(consequence.get("grounded", True) is True and consequence.get("provisional", False) is False, "persisted_edge_grounding_mismatch", {"cx_id": cx_id, "context": context})
        decoded.append({"cx_id": cx_id, "domain": oracle["domain"], "action": oracle["action"], "context": context})
    recurrence_cf = run_ok(["readback", "--cf", "recurrence", "--vault", str(vault_path)], env, "recurrence_cf_readback_failed")
    return {
        "vault_id": vault_id,
        "vault_path": str(vault_path.relative_to(ROOT)),
        "sample_file": file_stat(sample_path),
        "samples": len(decoded),
        "decoded": decoded,
        "recurrence_cf_stdout_sha256": sha256_bytes(recurrence_cf.stdout),
        "physical_files": physical_vault_files(vault_path),
    }


def physical_vault_files(vault_path: Path) -> dict[str, Any]:
    required = {
        "MANIFEST": vault_path / "MANIFEST",
        "wal": vault_path / "wal" / "00000000000000000000.wal",
        "base_page_index_manifest": vault_path / "base_page_index_v1" / "manifest.json",
        "ledger_head": vault_path / "ledger_head" / "current.json",
    }
    stats = {}
    for name, path in required.items():
        require(path.is_file(), "physical_file_missing", {"name": name, "path": str(path.relative_to(ROOT))})
        stats[name] = file_stat(path)
    cf_stats = {}
    for cf_name in ["base", "anchors", "recurrence"]:
        files = sorted((vault_path / "cf" / cf_name).glob("*.sst"))
        require(bool(files), "physical_cf_missing", {"cf": cf_name})
        cf_stats[cf_name] = {"sst_count": len(files), "bytes": sum(path.stat().st_size for path in files), "first_sha256": sha256_bytes(files[0].read_bytes())}
    return {"required": stats, "cf": cf_stats}


def synthetic_edges(schema: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    home = work_dir / "edge_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", "soccer-oracle-context-edge", "--panel-template", "text-default"], env, "edge_create_vault_failed")
    vault_id = json.loads(create.stdout)["vault_id"]
    vault_path = home / "vaults" / vault_id
    player_context = schema["domains"]["player"]["prediction_context"]
    validate_context(player_context, "player", prediction=True)
    bad_rows = {
        "empty_action": {"text": "bad action", "oracle": {"domain": "soccer_lab.player_impact", "action": "", "outcome": "impact", "outcome_kind": "label:player_impact"}},
        "missing_outcome": {"text": "missing outcome", "oracle": {"domain": "soccer_lab.player_impact", "action": "predict_player_impact", "outcome_kind": "label:player_impact"}},
        "bad_t_secs": {"text": "bad time", "oracle": {"domain": "soccer_lab.player_impact", "action": "predict_player_impact", "outcome": "impact", "outcome_kind": "label:player_impact", "t_secs": -1}},
    }
    observed = {}
    for name, row in bad_rows.items():
        path = work_dir / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
        proc = run(["ingest", "soccer-oracle-context-edge", "--batch", str(path.relative_to(ROOT)), "--output", "rows"], env, timeout=60)
        if proc.returncode == 0:
            raise ContextFormatError("synthetic_edge_unexpected_success", {"edge": name, "stdout": proc.stdout.decode("utf-8", "replace")})
        cx = run_ok(["readback", "cx-list", "--vault", str(vault_path), "--allow-unbounded", "--rebuild-base-page-index"], env, "edge_cx_list_failed")
        require(json.loads(cx.stdout) == [], "synthetic_edge_wrote_rows", {"edge": name, "stdout": cx.stdout.decode("utf-8", "replace")})
        observed[name] = {"returncode": proc.returncode, "stderr_tail": proc.stderr.decode("utf-8", "replace")[-500:], "row_file": file_stat(path)}
    happy = {
        "text": "player_id=P001 team_id=T001 prior_minutes=900",
        "metadata": {"project": "Soccer Lab", "entity": "player", "source_dataset": "synthetic.schema", "source": "synthetic-known-input", "source_key": "known", "player_id": "P001", "team_id": "T001"},
        "anchors": [{"kind": "label:player_impact", "value": "impact", "source": "synthetic-known-input", "confidence": 1.0}],
        "oracle": {"domain": "soccer_lab.player_impact", "action": "predict_player_impact", "outcome": "impact", "outcome_kind": "label:player_impact", "grounded": True, "t_secs": 1700000000},
    }
    happy_path = work_dir / "player_happy.jsonl"
    happy_path.write_text(json.dumps(happy, sort_keys=True) + "\n", encoding="utf-8")
    ingest = run_ok(["ingest", "soccer-oracle-context-edge", "--batch", str(happy_path.relative_to(ROOT)), "--output", "rows"], env, "player_happy_ingest_failed")
    cx_id = json.loads(ingest.stdout.decode("utf-8").splitlines()[0])["cx_id"]
    series = run_ok(["readback", "recurrence-series", "--vault", str(vault_path), "--cx-id", cx_id], env, "player_happy_series_failed")
    context = json.loads(bytes.fromhex(json.loads(series.stdout)["occurrences"][0]["context_hex"]).decode("utf-8"))
    require(context == player_context, "player_happy_context_not_schema_example", {"context": context, "expected": player_context})
    observed["player_happy"] = {"cx_id": cx_id, "context": context, "row_file": file_stat(happy_path)}
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
    schema = load_schema()
    docs_and_sources = verify_docs_and_sources(schema)
    rows_root = work_dir / "rows"
    rows = generate_rows(raw_root, rows_root)
    real_rows = verify_real_rows(schema, rows_root, rows)
    sample_vault = verify_ingested_contexts(work_dir / "real_sample_vault", real_rows["samples"])
    edges = synthetic_edges(schema, work_dir / "synthetic_edges")
    report = {
        "status": "ok",
        "docs_and_sources": docs_and_sources,
        "real_rows": {key: value for key, value in real_rows.items() if key != "samples"},
        "sample_vault": sample_vault,
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
                "real_files": sorted(report["real_rows"]["files"]),
                "sample_contexts": sample_vault["samples"],
                "synthetic_edges": sorted(edges["edges"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
