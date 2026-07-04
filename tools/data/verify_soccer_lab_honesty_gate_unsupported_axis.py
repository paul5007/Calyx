#!/usr/bin/env python3
"""Verify Soccer Lab Oracle honesty gate refuses an unsupported exact-score axis."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import verify_soccer_lab_oracle_context_ingest as oracle_context
import verify_soccer_lab_oracle_match_predictions as match_predictions
import verify_soccer_lab_oracle_sufficiency as oracle_sufficiency


ROOT = oracle_context.ROOT
CALYX = oracle_context.CALYX
DEFAULT_RAW = oracle_context.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "honesty_gate_unsupported_axis" / "report.json"
DEFAULT_ARTIFACT_OUT = ROOT / "docs" / "data" / "soccer_lab_honesty_gate_unsupported_axis.json"

DOMAIN = "soccer_lab.exact_scoreline"
ACTION_ID = "predict_exact_scoreline"
RUN_DATE = "2026-07-04"


class HonestyGateUnsupportedAxisError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def require(condition: bool, reason: str, detail: dict[str, Any] | None = None) -> None:
    if not condition:
        raise HonestyGateUnsupportedAxisError(reason, detail)


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise HonestyGateUnsupportedAxisError(
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
    for folder_name in ["panel", "registry"]:
        folder = vault_path / folder_name
        latest = sorted(folder.glob(f"{folder_name}-*.json"))
        if latest:
            files[folder_name] = oracle_context.file_stat(latest[-1])
    cf_stats = {}
    for cf_name in ["assay", "base", "recurrence", "ledger", "time_index"]:
        ssts = sorted((vault_path / "cf" / cf_name).glob("*.sst"))
        if not ssts:
            continue
        cf_stats[cf_name] = {
            "sst_count": len(ssts),
            "bytes": sum(path.stat().st_size for path in ssts),
            "first_sha256": oracle_context.sha256_bytes(ssts[0].read_bytes()),
            "last_sha256": oracle_context.sha256_bytes(ssts[-1].read_bytes()),
        }
    require("assay" in cf_stats, "missing_assay_cf", {"cf": sorted(cf_stats)})
    return {"files": files, "cf": cf_stats}


def exact_scoreline_counts(raw_root: Path) -> dict[str, Any]:
    path = raw_root / "fjelstul" / "data-csv" / "matches.csv"
    with path.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    require(rows, "missing_fjelstul_matches", {"path": str(path.relative_to(ROOT))})
    counts: Counter[str] = Counter()
    samples = []
    for row_index, row in enumerate(rows):
        home = row.get("home_team_score", "").strip()
        away = row.get("away_team_score", "").strip()
        require(home != "" and away != "", "scoreline_missing_score", {"row_index": row_index, "row": row})
        label = f"{int(home)}-{int(away)}"
        counts[label] += 1
        if len(samples) < 8:
            samples.append(
                {
                    "row_index": row_index,
                    "match_id": row["match_id"],
                    "home_team": row["home_team_name"],
                    "away_team": row["away_team_name"],
                    "scoreline": label,
                    "source_key": row.get("key_id"),
                }
            )
    entropy = oracle_sufficiency.entropy_bits(dict(counts))
    require(len(counts) >= 20, "scoreline_axis_not_sparse_enough", {"distinct": len(counts), "counts": dict(counts)})
    require(entropy > 3.0, "scoreline_entropy_too_low", {"entropy": entropy})
    return {
        "source_file": oracle_context.file_stat(path),
        "rows": len(rows),
        "distinct_scorelines": len(counts),
        "counts": dict(sorted(counts.items())),
        "top_scorelines": counts.most_common(12),
        "entropy_bits": entropy,
        "samples": samples,
    }


def exact_scoreline_panel(version: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return oracle_sufficiency.zero_panel(DOMAIN, version)


def exact_scoreline_sufficiency_fixture(scorelines: dict[str, Any]) -> dict[str, Any]:
    panel, slot_bits = exact_scoreline_panel(710)
    return {
        "domain": DOMAIN,
        "panel": panel,
        "I_panel_oracle": 0.0,
        "outcome_entropy_bits": scorelines["entropy_bits"],
        "slot_bits": slot_bits,
        "n_samples": scorelines["rows"],
        "trust": "trusted",
        "clock_ts": 1783132200,
    }


def self_consistency_series(valid: int = 50, invalid: int = 5) -> list[list[dict[str, Any]]]:
    series = []
    for _ in range(valid):
        outcome = {"enum": "stable"}
        series.append([{"outcome": outcome, "ground_truth": outcome}, {"outcome": outcome, "ground_truth": outcome}])
    for _ in range(invalid):
        outcome = {"enum": "unstable"}
        truth = {"enum": "other"}
        series.append([{"outcome": outcome, "ground_truth": truth}, {"outcome": outcome, "ground_truth": truth}])
    return series


def exact_scoreline_predict_fixture(scorelines: dict[str, Any]) -> dict[str, Any]:
    panel, slot_bits = exact_scoreline_panel(711)
    return {
        "domain": DOMAIN,
        "action_id": ACTION_ID,
        "panel": panel,
        "I_panel_oracle": 0.0,
        "outcome_entropy_bits": scorelines["entropy_bits"],
        "slot_bits": slot_bits,
        "prediction_observations": [
            {"outcome": {"enum": scoreline}, "count": count}
            for scoreline, count in sorted(scorelines["counts"].items())
        ],
        "self_consistency_series": self_consistency_series(),
        "n_samples": scorelines["rows"],
        "trust": "trusted",
        "clock_ts": 1783132200,
    }


def ledger_head(vault_path: Path) -> dict[str, Any] | None:
    path = vault_path / "ledger_head" / "current.json"
    return oracle_context.file_stat(path) if path.exists() else None


def run_sufficiency(work_dir: Path, vault: dict[str, Any], fixture: dict[str, Any], name: str, expect_success: bool) -> dict[str, Any]:
    fixture_path = work_dir / f"{name}.json"
    fixture_stat = write_json(fixture_path, fixture)
    before = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["assay"]}
    proc = run(
        [
            "readback",
            "oracle_sufficiency",
            "--vault",
            str(vault["vault_path"]),
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
    after = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["assay"]}
    if expect_success:
        require(proc.returncode == 0, "sufficiency_expected_success_failed", {"payload": payload, "stderr": proc.stderr.decode("utf-8", "replace")})
        require(payload.get("bound", {}).get("sufficient") is True, "sufficiency_missing_success_bound", {"payload": payload})
    else:
        require(proc.returncode != 0, "sufficiency_expected_insufficient_passed", {"payload": payload})
        require(payload.get("error_code") == "CALYX_ORACLE_INSUFFICIENT", "sufficiency_wrong_error", {"payload": payload})
        require(payload.get("bound", {}).get("sufficient") is False, "sufficiency_missing_insufficient_bound", {"payload": payload})
    require(after["assay"]["raw_rows"] >= before["assay"]["raw_rows"] + int(payload.get("assay_rows_written", 0)), "assay_rows_not_written", {"before": before, "after": after, "payload": payload})
    return {
        "fixture": fixture_stat,
        "returncode": proc.returncode,
        "stdout_sha256": oracle_context.sha256_bytes(proc.stdout),
        "stderr_sha256": oracle_context.sha256_bytes(proc.stderr),
        "stderr_tail": proc.stderr.decode("utf-8", "replace")[-500:],
        "before": before,
        "after": after,
        "payload": payload,
    }


def run_predict(work_dir: Path, vault: dict[str, Any], fixture: dict[str, Any], name: str, expect_success: bool) -> dict[str, Any]:
    fixture_path = work_dir / f"{name}.json"
    fixture_stat = write_json(fixture_path, fixture)
    before = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["assay", "base", "recurrence", "ledger"]}
    ledger_before = ledger_head(vault["vault_path"])
    proc = run(
        [
            "readback",
            "oracle_predict",
            "--vault",
            str(vault["vault_path"]),
            "--fixture",
            str(fixture_path.relative_to(ROOT)),
            "--vault-id",
            vault["vault_id"],
            "--salt",
            vault["vault_salt"],
        ],
        vault["env"],
        timeout=240,
    )
    payload = json.loads(proc.stdout.decode("utf-8", "replace")) if proc.stdout else {}
    after = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["assay", "base", "recurrence", "ledger"]}
    ledger_after = ledger_head(vault["vault_path"])
    if expect_success:
        require(proc.returncode == 0, "predict_expected_success_failed", {"payload": payload, "stderr": proc.stderr.decode("utf-8", "replace")})
        require("prediction" in payload and payload["prediction"]["bound"]["sufficient"] is True, "predict_missing_success_prediction", {"payload": payload})
        require(ledger_after is not None, "predict_success_missing_ledger")
    else:
        require(proc.returncode != 0, "predict_expected_insufficient_passed", {"payload": payload})
        require(payload.get("error_code") == "CALYX_ORACLE_INSUFFICIENT", "predict_wrong_error", {"payload": payload})
        require(payload.get("bound", {}).get("sufficient") is False, "predict_missing_insufficient_bound", {"payload": payload})
        require("prediction" not in payload, "predict_fabricated_prediction", {"payload": payload})
        require(ledger_before == ledger_after, "insufficient_prediction_wrote_ledger", {"before": ledger_before, "after": ledger_after})
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


def real_unsupported_axis(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    scorelines = exact_scoreline_counts(raw_root)
    vault = create_vault(work_dir / "vault", "soccer-honesty-unsupported-axis")
    sufficiency = run_sufficiency(work_dir, vault, exact_scoreline_sufficiency_fixture(scorelines), "exact-scoreline-sufficiency", expect_success=False)
    predict = run_predict(work_dir, vault, exact_scoreline_predict_fixture(scorelines), "exact-scoreline-predict", expect_success=False)
    suff_bound = sufficiency["payload"]["bound"]
    pred_bound = predict["payload"]["bound"]
    for bound in [suff_bound, pred_bound]:
        require(bound["I_panel_oracle"] == 0.0, "unsupported_axis_panel_bits_not_zero", {"bound": bound})
        require(bound["dpi_ceiling"] == 0.0, "unsupported_axis_dpi_not_zero", {"bound": bound})
        require(bound["per_sensor_deficit"], "unsupported_axis_missing_deficits", {"bound": bound})
    require(abs(scorelines["entropy_bits"] - exact_scoreline_sufficiency_fixture(scorelines)["outcome_entropy_bits"]) < 1e-9, "entropy_fixture_mismatch")
    return {
        "domain": DOMAIN,
        "action_id": ACTION_ID,
        "unsupported_reason": "exact scoreline is an ex-post high-cardinality target with no Soccer Lab predictive panel bits",
        "scorelines": scorelines,
        "vault_name": vault["vault_name"],
        "vault_id": vault["vault_id"],
        "vault_path": str(vault["vault_path"].relative_to(ROOT)),
        "vault_salt": vault["vault_salt"],
        "create_stdout_sha256": vault["create_stdout_sha256"],
        "oracle_sufficiency": sufficiency,
        "oracle_predict": predict,
        "physical_readback": physical_readback(vault["vault_path"]),
    }


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    vault = create_vault(work_dir / "vault", "soccer-honesty-synthetic")
    panel = oracle_context.minimal_panel()
    happy_fixture = {
        "domain": "synthetic.honesty.supported",
        "action_id": "predict_supported",
        "panel": panel,
        "I_panel_oracle": 1.25,
        "outcome_entropy_bits": 1.0,
        "slot_bits": [{"slot": 0, "bits": 0.65}, {"slot": 1, "bits": 0.60}],
        "prediction_observations": [{"outcome": {"enum": "A"}, "count": 9}, {"outcome": {"enum": "B"}, "count": 3}],
        "self_consistency_series": self_consistency_series(),
        "n_samples": 120,
        "trust": "trusted",
        "clock_ts": 1783132200,
    }
    happy = run_predict(work_dir, vault, happy_fixture, "synthetic-happy", expect_success=True)
    require(happy["payload"]["prediction"]["outcome"] == {"enum": "A"}, "synthetic_happy_prediction_mismatch", {"payload": happy["payload"]})

    insufficient_fixture = dict(happy_fixture)
    insufficient_fixture["domain"] = "synthetic.honesty.insufficient"
    insufficient_fixture["I_panel_oracle"] = 0.1
    insufficient_fixture["slot_bits"] = [{"slot": 0, "bits": 0.05}, {"slot": 1, "bits": 0.05}]
    insufficient = run_predict(work_dir, vault, insufficient_fixture, "synthetic-insufficient", expect_success=False)

    no_write_edges = {}
    bad_cases = {
        "negative_bits": {"I_panel_oracle": -0.01},
        "zero_samples": {"n_samples": 0},
        "negative_slot_bits": {"slot_bits": [{"slot": 0, "bits": -0.01}, {"slot": 1, "bits": 0.1}]},
    }
    for name, patch in bad_cases.items():
        fixture = dict(happy_fixture)
        fixture["domain"] = f"synthetic.honesty.{name}"
        fixture.update(patch)
        path = work_dir / f"{name}.json"
        stat = write_json(path, fixture)
        before = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["assay", "base", "recurrence", "ledger"]}
        proc = run(
            [
                "readback",
                "oracle_predict",
                "--vault",
                str(vault["vault_path"]),
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
        after = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["assay", "base", "recurrence", "ledger"]}
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
        "happy_supported": happy,
        "known_insufficient": insufficient,
        "no_write_edges": no_write_edges,
        "physical_readback": physical_readback(vault["vault_path"]),
    }


def write_artifact(path: Path, real: dict[str, Any], report_path: Path) -> dict[str, Any]:
    artifact = {
        "schema_version": 1,
        "verified_at": RUN_DATE,
        "domain": real["domain"],
        "action_id": real["action_id"],
        "unsupported_axis": "exact_scoreline",
        "unsupported_reason": real["unsupported_reason"],
        "scoreline_source": {
            "file": real["scorelines"]["source_file"],
            "rows": real["scorelines"]["rows"],
            "distinct_scorelines": real["scorelines"]["distinct_scorelines"],
            "entropy_bits": real["scorelines"]["entropy_bits"],
            "top_scorelines": real["scorelines"]["top_scorelines"],
        },
        "oracle_sufficiency": {
            "error_code": real["oracle_sufficiency"]["payload"]["error_code"],
            "bound": real["oracle_sufficiency"]["payload"]["bound"],
            "stdout_sha256": real["oracle_sufficiency"]["stdout_sha256"],
            "fixture_sha256": real["oracle_sufficiency"]["fixture"]["sha256"],
        },
        "oracle_predict": {
            "error_code": real["oracle_predict"]["payload"]["error_code"],
            "fabricated_prediction": "prediction" in real["oracle_predict"]["payload"],
            "bound": real["oracle_predict"]["payload"]["bound"],
            "stdout_sha256": real["oracle_predict"]["stdout_sha256"],
            "fixture_sha256": real["oracle_predict"]["fixture"]["sha256"],
            "ledger_before": real["oracle_predict"]["ledger_before"],
            "ledger_after": real["oracle_predict"]["ledger_after"],
        },
        "provenance": {
            "source_report": str(report_path.relative_to(ROOT)),
        },
    }
    return write_json(path, artifact)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW.relative_to(ROOT)))
    parser.add_argument("--out", default=str(DEFAULT_OUT.relative_to(ROOT)))
    parser.add_argument("--artifact-out", default=str(DEFAULT_ARTIFACT_OUT.relative_to(ROOT)))
    return parser.parse_args()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    raw_root = resolve(args.raw_root)
    report_path = resolve(args.out)
    artifact_path = resolve(args.artifact_out)
    work_dir = report_path.parent
    real = real_unsupported_axis(work_dir / "real", raw_root)
    artifact = write_artifact(artifact_path, real, report_path)
    synthetic = synthetic_edges(work_dir / "synthetic")
    report = {
        "status": "ok",
        "run_date": RUN_DATE,
        "real_unsupported_axis": real,
        "artifact": artifact,
        "synthetic_edges": synthetic,
    }
    report_stat = write_json(report_path, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "report": str(report_path.relative_to(ROOT)),
                "report_sha256": report_stat["sha256"],
                "artifact": artifact,
                "exact_scoreline_entropy_bits": real["scorelines"]["entropy_bits"],
                "distinct_scorelines": real["scorelines"]["distinct_scorelines"],
                "sufficiency_error": real["oracle_sufficiency"]["payload"]["error_code"],
                "predict_error": real["oracle_predict"]["payload"]["error_code"],
                "fabricated_prediction": "prediction" in real["oracle_predict"]["payload"],
                "synthetic_edges": ["happy_supported", "known_insufficient", *sorted(synthetic["no_write_edges"])],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
