#!/usr/bin/env python3
"""Verify Soccer Lab reverse_query causal facet signatures."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import verify_soccer_lab_oracle_context_ingest as oracle_context
import verify_soccer_lab_oracle_match_predictions as match_predictions


ROOT = oracle_context.ROOT
CALYX = oracle_context.CALYX
DEFAULT_RAW = oracle_context.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "reverse_query_signatures" / "report.json"
DEFAULT_SIGNATURES_OUT = ROOT / "docs" / "data" / "soccer_lab_reverse_causal_signatures.json"
LENS_DIR = ROOT / "tools" / "lenses" / "soccer_lab" / "team_match"

DOMAIN = "soccer_lab.team_match_result"
ANSWER = "win"
RUN_DATE = "2026-07-04"
FACET_FEATURES = {
    "attack": [
        "trailing_goals_for_per_match",
        "trailing_goal_scoring_rate",
        "trailing_multi_goal_rate",
        "trailing_penalties_for_per_match",
        "home_attack_context",
        "away_attack_context",
    ],
    "defense": [
        "trailing_goals_against_per_match",
        "trailing_clean_sheet_rate",
        "trailing_multi_concede_rate",
        "trailing_penalties_against_per_match",
        "trailing_goal_differential_norm",
    ],
    "tempo": [
        "trailing_extra_time_rate",
        "trailing_penalty_shootout_rate",
        "days_since_previous_match_norm",
        "trailing_replay_rate",
    ],
    "discipline": [
        "trailing_yellow_cards_per_match",
        "trailing_red_cards_per_match",
        "trailing_second_yellow_rate",
        "trailing_sending_off_rate",
    ],
    "pedigree": [
        "confederation_hash_norm",
        "region_hash_norm",
        "mens_team_flag",
        "womens_team_flag",
        "prior_world_cup_matches_norm",
        "prior_best_finish_norm",
    ],
    "form": [
        "trailing_win_rate",
        "trailing_draw_rate",
        "trailing_loss_rate",
        "trailing_unbeaten_rate",
        "prior_form_sample_size_norm",
    ],
    "context": [
        "stage_hash_norm",
        "group_hash_norm",
        "group_stage_flag",
        "knockout_stage_flag",
        "match_day_of_tournament_norm",
        "kickoff_hour_norm",
        "host_country_flag",
        "stadium_capacity_norm",
    ],
}


class ReverseQuerySignatureError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def require(condition: bool, reason: str, detail: dict[str, Any] | None = None) -> None:
    if not condition:
        raise ReverseQuerySignatureError(reason, detail)


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise ReverseQuerySignatureError(
            reason,
            {
                "args": args,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def write_json(path: Path, payload: Any) -> dict[str, Any]:
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded + "\n", encoding="utf-8")
    require(path.read_text(encoding="utf-8") == encoded + "\n", "json_readback_mismatch", {"path": str(path.relative_to(ROOT))})
    return oracle_context.file_stat(path)


def create_vault(work_dir: Path, name: str) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", name, "--panel-template", "text-default"], env, f"create_{name}_failed")
    created = json.loads(create.stdout)
    vault_id = created["vault_id"]
    return {
        "env": env,
        "vault_name": name,
        "vault_id": vault_id,
        "vault_path": home / "vaults" / vault_id,
        "vault_salt": oracle_context.vault_salt(vault_id, name),
        "create_stdout_sha256": oracle_context.sha256_bytes(create.stdout),
    }


def cf_rows(vault_path: Path, env: dict[str, str], cf: str) -> dict[str, Any]:
    return match_predictions.cf_rows(vault_path, env, cf)


def physical_readback(vault_path: Path) -> dict[str, Any]:
    required = {
        "MANIFEST": vault_path / "MANIFEST",
        "CURRENT": vault_path / "CURRENT",
        "wal": vault_path / "wal" / "00000000000000000000.wal",
    }
    files = {}
    for name, path in required.items():
        require(path.is_file(), "physical_file_missing", {"name": name, "path": str(path.relative_to(ROOT))})
        files[name] = oracle_context.file_stat(path)
    ledger_head = vault_path / "ledger_head" / "current.json"
    if ledger_head.exists():
        files["ledger_head"] = oracle_context.file_stat(ledger_head)
    cf_stats = {}
    for cf_name in ["base", "recurrence", "ledger", "time_index"]:
        ssts = sorted((vault_path / "cf" / cf_name).glob("*.sst"))
        if not ssts:
            continue
        cf_stats[cf_name] = {
            "sst_count": len(ssts),
            "bytes": sum(path.stat().st_size for path in ssts),
            "first_sha256": oracle_context.sha256_bytes(ssts[0].read_bytes()),
            "last_sha256": oracle_context.sha256_bytes(ssts[-1].read_bytes()),
        }
    require("base" in cf_stats and "ledger" in cf_stats, "missing_reverse_query_cfs", {"cf": sorted(cf_stats)})
    return {"files": files, "cf": cf_stats}


def frame(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def decode_frame(stdout: bytes) -> dict[str, Any]:
    require(len(stdout) >= 4, "projector_missing_frame_header")
    size = struct.unpack(">I", stdout[:4])[0]
    body = stdout[4:]
    require(len(body) == size, "projector_frame_length_mismatch", {"expected": size, "observed": len(body)})
    return json.loads(body)


def run_projector(facet: str, text: str) -> list[float]:
    proc = subprocess.run(
        [str(LENS_DIR / facet)],
        input=frame({"modality": "text", "inputs": [list(text.encode("utf-8"))]}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    require(proc.returncode == 0, "projector_failed", {"facet": facet, "stderr": proc.stderr.decode("utf-8", "replace")[-1000:]})
    payload = decode_frame(proc.stdout)
    vectors = payload.get("vectors")
    require(isinstance(vectors, list) and len(vectors) == 1, "projector_bad_vectors", {"facet": facet, "payload": payload})
    vector = vectors[0]
    require(len(vector) == len(FACET_FEATURES[facet]), "projector_dim_mismatch", {"facet": facet, "vector": vector})
    require(all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector), "projector_non_finite", {"facet": facet, "vector": vector})
    return [float(value) for value in vector]


def rows_for_signatures(raw_root: Path, rows_root: Path) -> dict[str, Any]:
    generated = oracle_context.generate_rows(raw_root, rows_root)
    rows = generated["oracle_rows"]
    team_rows = [row for row in rows if row["oracle"]["domain"] == DOMAIN]
    require(len(team_rows) >= 200, "not_enough_team_rows", {"count": len(team_rows)})
    return {"generation": {key: value for key, value in generated.items() if key != "oracle_rows"}, "rows": team_rows}


def derive_signatures(team_rows: list[dict[str, Any]]) -> dict[str, Any]:
    outcome_counts = Counter(row["anchors"][0]["value"] for row in team_rows)
    require(outcome_counts[ANSWER] >= 50, "answer_not_grounded_enough", {"outcome_counts": dict(outcome_counts)})
    hits: dict[str, dict[str, Any]] = {}
    row_sample_hashes = []
    for row_index, row in enumerate(team_rows):
        text = row["text"]
        outcome = row["anchors"][0]["value"]
        if len(row_sample_hashes) < 8:
            row_sample_hashes.append(oracle_context.sha256_bytes(text.encode("utf-8")))
        for facet in sorted(FACET_FEATURES):
            vector = run_projector(facet, text)
            for feature, value in zip(FACET_FEATURES[facet], vector):
                if value < 0.66:
                    continue
                action = f"facet:{facet}.{feature}:high"
                entry = hits.setdefault(
                    action,
                    {
                        "facet": facet,
                        "feature": feature,
                        "threshold": 0.66,
                        "total_hits": 0,
                        "outcomes": Counter(),
                        "value_sum": 0.0,
                        "sample_rows": [],
                    },
                )
                entry["total_hits"] += 1
                entry["outcomes"][outcome] += 1
                entry["value_sum"] += value
                if len(entry["sample_rows"]) < 5:
                    entry["sample_rows"].append(
                        {
                            "row_index": row_index,
                            "team_id": row["metadata"]["team_id"],
                            "match_id": row["metadata"]["match_id"],
                            "outcome": outcome,
                            "value": value,
                            "text_sha256": oracle_context.sha256_bytes(text.encode("utf-8")),
                        }
                    )
    records = []
    prior = outcome_counts[ANSWER] / sum(outcome_counts.values())
    for action, entry in hits.items():
        answer_hits = entry["outcomes"][ANSWER]
        if answer_hits < 3:
            continue
        precision = answer_hits / entry["total_hits"]
        lift = precision / prior if prior > 0 else 0.0
        confidence = max(0.05, min(1.0, precision * min(lift, 2.0) / 2.0))
        records.append(
            {
                "action": action,
                "facet": entry["facet"],
                "feature": entry["feature"],
                "threshold": entry["threshold"],
                "total_hits": entry["total_hits"],
                "answer_hits": answer_hits,
                "outcomes": dict(sorted(entry["outcomes"].items())),
                "precision": precision,
                "prior": prior,
                "lift": lift,
                "mean_value": entry["value_sum"] / entry["total_hits"],
                "structural_confidence": confidence,
                "sample_rows": entry["sample_rows"],
            }
        )
    records.sort(key=lambda item: (-item["structural_confidence"], -item["answer_hits"], item["action"]))
    require(len(records) >= 6, "not_enough_signatures", {"records": records[:10], "outcome_counts": dict(outcome_counts)})
    selected = []
    for index, record in enumerate(records[:6], start=1):
        selected.append({"signature_id": f"sig_{index:02d}", **record})
    return {
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "prior_answer_rate": prior,
        "row_sample_hashes": row_sample_hashes,
        "selected": selected,
    }


def fixture_from_signatures(signatures: dict[str, Any]) -> dict[str, Any]:
    edges = []
    for record in signatures["selected"][:4]:
        edges.append(
            {
                "from": record["signature_id"],
                "to": ANSWER,
                "outcome": {"text": ANSWER},
                "occurrences": int(record["answer_hits"]),
            }
        )
    for record in signatures["selected"][:3]:
        edges.append(
            {
                "from": f"struct_{record['signature_id']}",
                "to": ANSWER,
                "outcome": {"text": ANSWER},
                "structural_only": True,
                "confidence": record["structural_confidence"],
            }
        )
    first = signatures["selected"][0]
    second = signatures["selected"][1]
    edges.append(
        {
            "from": f"ant_{second['signature_id']}",
            "to": first["signature_id"],
            "outcome": {"text": first["signature_id"]},
            "occurrences": max(1, min(5, int(second["answer_hits"]))),
        }
    )
    return {"domain": DOMAIN, "clock_ts": 1783132200, "edges": edges}


def cause_by_action(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {cause["action_or_event"]: cause for cause in payload.get("causes", [])}


def run_reverse(work_dir: Path, vault: dict[str, Any], fixture: dict[str, Any], name: str, answer: str = ANSWER, domain: str = DOMAIN, expect_success: bool = True) -> dict[str, Any]:
    fixture_path = work_dir / f"{name}.json"
    fixture_stat = write_json(fixture_path, fixture)
    before = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["base", "recurrence"]}
    ledger_before = oracle_context.file_stat(vault["vault_path"] / "ledger_head" / "current.json") if (vault["vault_path"] / "ledger_head" / "current.json").exists() else None
    proc = run(
        [
            "readback",
            "reverse_query",
            "--vault",
            str(vault["vault_path"]),
            "--domain",
            domain,
            "--answer",
            answer,
            "--fixture",
            str(fixture_path.relative_to(ROOT)),
            "--vault-id",
            vault["vault_id"],
            "--salt",
            vault["vault_salt"],
        ],
        vault["env"],
        timeout=180,
    )
    payload = json.loads(proc.stdout.decode("utf-8", "replace")) if proc.stdout else {}
    after = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["base", "recurrence"]}
    ledger_after = oracle_context.file_stat(vault["vault_path"] / "ledger_head" / "current.json") if (vault["vault_path"] / "ledger_head" / "current.json").exists() else None
    if expect_success:
        require(proc.returncode == 0, "reverse_query_expected_success_failed", {"payload": payload, "stderr": proc.stderr.decode("utf-8", "replace")})
        base_written = payload["source_of_truth"]["base_rows_written"]
        recurrence_written = payload["source_of_truth"]["recurrence_rows_written"]
        require(after["base"]["raw_rows"] - before["base"]["raw_rows"] == base_written * 2, "base_row_delta_mismatch", {"before": before, "after": after, "base_written": base_written})
        require(after["recurrence"]["raw_rows"] - before["recurrence"]["raw_rows"] == recurrence_written * 2, "recurrence_row_delta_mismatch", {"before": before, "after": after, "recurrence_written": recurrence_written})
        require(payload["max_reverse_depth"] == 3, "max_reverse_depth_mismatch", {"payload": payload["max_reverse_depth"]})
        require(ledger_after is not None, "reverse_query_missing_ledger_head")
    else:
        require(proc.returncode != 0, "reverse_query_expected_failure_passed", {"payload": payload})
    return {
        "fixture": fixture_stat,
        "returncode": proc.returncode,
        "stdout_sha256": oracle_context.sha256_bytes(proc.stdout),
        "stderr_sha256": oracle_context.sha256_bytes(proc.stderr),
        "stderr_tail": proc.stderr.decode("utf-8", "replace")[-500:],
        "before": before,
        "after": after,
        "ledger_before": ledger_before,
        "ledger_after": ledger_after,
        "payload": payload,
    }


def real_reverse_query(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    rows = rows_for_signatures(raw_root, work_dir / "rows")
    signatures = derive_signatures(rows["rows"])
    fixture = fixture_from_signatures(signatures)
    vault = create_vault(work_dir / "vault", "soccer-reverse-query-signatures")
    readback = run_reverse(work_dir, vault, fixture, "reverse-query-signatures", expect_success=True)
    payload = readback["payload"]
    by_action = cause_by_action(payload)
    for record in signatures["selected"][:4]:
        cause = by_action.get(record["signature_id"])
        require(cause is not None, "grounded_signature_missing", {"record": record, "causes": payload.get("causes")})
        expected = record["answer_hits"] / (record["answer_hits"] + 1)
        require(not cause["provisional"], "grounded_signature_marked_provisional", {"record": record, "cause": cause})
        require(abs(cause["confidence"] - expected) < 1e-6, "grounded_confidence_mismatch", {"record": record, "cause": cause, "expected": expected})
    for record in signatures["selected"][:3]:
        action = f"struct_{record['signature_id']}"
        cause = by_action.get(action)
        require(cause is not None, "structural_signature_missing", {"record": record, "causes": payload.get("causes")})
        require(cause["provisional"], "structural_signature_not_provisional", {"record": record, "cause": cause})
        require(abs(cause["confidence"] - record["structural_confidence"]) < 1e-6, "structural_confidence_mismatch", {"record": record, "cause": cause})
    require(payload["grounded_count"] >= 4, "grounded_count_too_low", {"payload": payload})
    require(payload["provisional_count"] >= 3, "provisional_count_too_low", {"payload": payload})
    return {
        "domain": DOMAIN,
        "answer": ANSWER,
        "generation": rows["generation"],
        "signatures": signatures,
        "fixture_edge_count": len(fixture["edges"]),
        "fixture_edges": fixture["edges"],
        "vault_name": vault["vault_name"],
        "vault_id": vault["vault_id"],
        "vault_path": str(vault["vault_path"].relative_to(ROOT)),
        "vault_salt": vault["vault_salt"],
        "create_stdout_sha256": vault["create_stdout_sha256"],
        "readback": readback,
        "physical_readback": physical_readback(vault["vault_path"]),
    }


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    vault = create_vault(work_dir / "vault", "soccer-reverse-query-synthetic")
    happy = {
        "domain": "synthetic.reverse",
        "edges": [
            {"from": "cause_a", "to": "effect", "outcome": {"text": "effect"}, "occurrences": 3},
            {"from": "cause_b", "to": "effect", "outcome": {"text": "effect"}, "structural_only": True, "confidence": 0.42},
            {"from": "cause_c", "to": "cause_a", "outcome": {"text": "cause_a"}, "occurrences": 2},
        ],
    }
    happy_readback = run_reverse(work_dir, vault, happy, "synthetic-happy", answer="effect", domain="synthetic.reverse", expect_success=True)
    happy_causes = cause_by_action(happy_readback["payload"])
    require(abs(happy_causes["cause_a"]["confidence"] - 0.75) < 1e-6, "synthetic_grounded_confidence_mismatch", {"cause": happy_causes.get("cause_a")})
    require(happy_causes["cause_b"]["provisional"] and abs(happy_causes["cause_b"]["confidence"] - 0.42) < 1e-6, "synthetic_structural_confidence_mismatch", {"cause": happy_causes.get("cause_b")})
    require("cause_c" in happy_causes, "synthetic_antecedent_missing", {"causes": happy_readback["payload"].get("causes")})

    missing = run_reverse(work_dir, vault, happy, "synthetic-missing", answer="missing", domain="synthetic.reverse", expect_success=False)
    require(missing["payload"].get("error_code") == "CALYX_ORACLE_DOMAIN_NOT_FOUND", "missing_wrong_error", {"payload": missing["payload"]})

    malformed = {
        "domain": "synthetic.reverse.malformed",
        "edges": [{"from": "cause_a", "to": "effect", "outcome": {"text": "effect"}, "malformed_context": True}],
    }
    malformed_readback = run_reverse(work_dir, vault, malformed, "synthetic-malformed", answer="effect", domain="synthetic.reverse.malformed", expect_success=False)
    require(malformed_readback["payload"].get("error_code") == "CALYX_ORACLE_NO_RECURRENCE", "malformed_wrong_error", {"payload": malformed_readback["payload"]})

    no_write_edges = {}
    bad_cases = {
        "empty_from": {"domain": "synthetic.reverse.bad", "edges": [{"from": "", "to": "effect", "outcome": {"text": "effect"}}]},
        "zero_occurrences": {"domain": "synthetic.reverse.bad", "edges": [{"from": "cause", "to": "effect", "outcome": {"text": "effect"}, "occurrences": 0}]},
        "bad_confidence": {"domain": "synthetic.reverse.bad", "edges": [{"from": "cause", "to": "effect", "outcome": {"text": "effect"}, "structural_only": True, "confidence": 1.5}]},
    }
    for name, fixture in bad_cases.items():
        path = work_dir / f"synthetic-{name}.json"
        stat = write_json(path, fixture)
        before = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["base", "recurrence"]}
        proc = run(
            [
                "readback",
                "reverse_query",
                "--vault",
                str(vault["vault_path"]),
                "--domain",
                fixture["domain"],
                "--answer",
                "effect",
                "--fixture",
                str(path.relative_to(ROOT)),
                "--vault-id",
                vault["vault_id"],
                "--salt",
                vault["vault_salt"],
            ],
            vault["env"],
            timeout=60,
        )
        after = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["base", "recurrence"]}
        require(proc.returncode != 0, "synthetic_bad_case_passed", {"case": name, "stdout": proc.stdout.decode("utf-8", "replace")})
        require(before == after, "synthetic_bad_case_wrote_cf", {"case": name, "before": before, "after": after})
        no_write_edges[name] = {
            "fixture": stat,
            "returncode": proc.returncode,
            "stderr_tail": proc.stderr.decode("utf-8", "replace")[-500:],
            "stderr_sha256": oracle_context.sha256_bytes(proc.stderr),
            "before": before,
            "after": after,
        }
    return {
        "vault_path": str(vault["vault_path"].relative_to(ROOT)),
        "happy": happy_readback,
        "missing_answer": missing,
        "malformed_context": malformed_readback,
        "no_write_edges": no_write_edges,
        "physical_readback": physical_readback(vault["vault_path"]),
    }


def write_signature_artifact(path: Path, real: dict[str, Any], report_path: Path) -> dict[str, Any]:
    causes = real["readback"]["payload"]["causes"]
    artifact = {
        "schema_version": 1,
        "generated_at": RUN_DATE,
        "domain": real["domain"],
        "answer": real["answer"],
        "answer_anchor_encoding": {"text": real["answer"]},
        "outcome_counts": real["signatures"]["outcome_counts"],
        "prior_answer_rate": real["signatures"]["prior_answer_rate"],
        "selected_signatures": real["signatures"]["selected"],
        "reverse_query": {
            "grounded_count": real["readback"]["payload"]["grounded_count"],
            "provisional_count": real["readback"]["payload"]["provisional_count"],
            "max_reverse_depth": real["readback"]["payload"]["max_reverse_depth"],
            "causes": causes,
        },
        "provenance": {
            "source_report": str(report_path.relative_to(ROOT)),
            "oracle_stdout_sha256": real["readback"]["stdout_sha256"],
            "oracle_fixture_sha256": real["readback"]["fixture"]["sha256"],
        },
    }
    return write_json(path, artifact)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW.relative_to(ROOT)))
    parser.add_argument("--out", default=str(DEFAULT_OUT.relative_to(ROOT)))
    parser.add_argument("--signatures-out", default=str(DEFAULT_SIGNATURES_OUT.relative_to(ROOT)))
    return parser.parse_args()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    raw_root = resolve(args.raw_root)
    report_path = resolve(args.out)
    signatures_path = resolve(args.signatures_out)
    work_dir = report_path.parent
    real = real_reverse_query(work_dir / "real", raw_root)
    signatures_file = write_signature_artifact(signatures_path, real, report_path)
    synthetic = synthetic_edges(work_dir / "synthetic")
    report = {
        "status": "ok",
        "run_date": RUN_DATE,
        "real_reverse_query": real,
        "signatures_file": signatures_file,
        "synthetic_edges": synthetic,
    }
    report_stat = write_json(report_path, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "report": str(report_path.relative_to(ROOT)),
                "report_sha256": report_stat["sha256"],
                "signatures_file": signatures_file,
                "selected_signatures": [record["action"] for record in real["signatures"]["selected"]],
                "grounded_count": real["readback"]["payload"]["grounded_count"],
                "provisional_count": real["readback"]["payload"]["provisional_count"],
                "synthetic_edges": ["happy", "missing_answer", "malformed_context", *sorted(synthetic["no_write_edges"])],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
