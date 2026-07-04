#!/usr/bin/env python3
"""Verify Soccer Lab match-result Oracle predictions for unplayed 2026 fixtures."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import verify_soccer_lab_bits_assay as bits_assay
import verify_soccer_lab_oracle_context_ingest as oracle_context
import verify_soccer_lab_oracle_sufficiency as oracle_sufficiency


ROOT = oracle_context.ROOT
CALYX = oracle_context.CALYX
DEFAULT_RAW = oracle_context.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "oracle_match_predictions" / "report.json"
DEFAULT_PREDICTIONS_OUT = ROOT / "docs" / "data" / "soccer_lab_match_predictions.json"
RUN_DATE = date(2026, 7, 4)
DOMAIN = "soccer_lab.match_result"
ACTION_ID = "predict_match_result"


class OracleMatchPredictionError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def require(condition: bool, reason: str, detail: dict[str, Any] | None = None) -> None:
    if not condition:
        raise OracleMatchPredictionError(reason, detail)


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise OracleMatchPredictionError(
            reason,
            {
                "args": args,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def read_swaptr_matches(raw_root: Path) -> list[dict[str, Any]]:
    path = raw_root / "swaptr" / "fifa-wc-2026-matches.zip"
    with zipfile.ZipFile(path) as archive:
        payload = archive.read("matches.csv")
    rows = list(csv.DictReader(payload.decode("utf-8-sig").splitlines()))
    require(rows, "missing_2026_matches", {"path": str(path.relative_to(ROOT))})
    return [
        {
            "source": "swaptr/fifa-wc-2026-matches",
            "source_file": path,
            "match_id": f"WC-2026-M{idx + 1:03d}",
            "source_row_index": idx,
            "date": row["date"],
            "start_time": row["start_time"],
            "round": row["round"],
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "venue": row["venue"],
            "score": row.get("score", ""),
        }
        for idx, row in enumerate(rows)
    ]


def read_openfootball_matches(raw_root: Path) -> list[dict[str, Any]]:
    path = raw_root / "openfootball" / "2026" / "worldcup.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("matches")
    require(isinstance(rows, list) and rows, "missing_openfootball_matches", {"path": str(path.relative_to(ROOT))})
    return [
        {
            "source": "openfootball/worldcup.json/2026",
            "source_file": path,
            "match_id": f"WC-2026-M{int(row.get('num', idx + 1)):03d}",
            "source_row_index": idx,
            "date": row["date"],
            "start_time": row.get("time", ""),
            "round": row["round"],
            "home_team": row["team1"],
            "away_team": row["team2"],
            "venue": row.get("ground", ""),
            "score": row.get("score", ""),
        }
        for idx, row in enumerate(rows)
    ]


def read_current_matches(raw_root: Path) -> list[dict[str, Any]]:
    openfootball = raw_root / "openfootball" / "2026" / "worldcup.json"
    if openfootball.exists():
        return read_openfootball_matches(raw_root)
    return read_swaptr_matches(raw_root)


def unplayed_fixtures(raw_root: Path) -> list[dict[str, Any]]:
    fixtures = []
    for idx, row in enumerate(read_current_matches(raw_root)):
        fixture_date = date.fromisoformat(row["date"])
        score_blank = not row.get("score")
        if fixture_date < RUN_DATE:
            continue
        if fixture_date == RUN_DATE and not score_blank:
            continue
        fixtures.append(
            {
                "match_id": row["match_id"],
                "date": row["date"],
                "start_time": row["start_time"],
                "round": row["round"],
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "venue": row["venue"],
                "source": row["source"],
                "source_row_index": row["source_row_index"],
                "score_columns_ignored": True,
                "unplayed_reason": "blank_score" if score_blank else "after_run_date",
            }
        )
    return fixtures


def match_source_summary(raw_root: Path) -> dict[str, Any]:
    rows = read_current_matches(raw_root)
    path = rows[0]["source_file"]
    dates = [row["date"] for row in rows]
    blank_scores = [idx for idx, row in enumerate(rows) if not row.get("score")]
    after_run_date = [idx for idx, row in enumerate(rows) if row["date"] > RUN_DATE.isoformat()]
    rounds: dict[str, int] = {}
    for row in rows:
        rounds[row["round"]] = rounds.get(row["round"], 0) + 1
    return {
        "source": rows[0]["source"],
        "source_file": oracle_context.file_stat(path),
        "rows": len(rows),
        "min_date": min(dates),
        "max_date": max(dates),
        "blank_score_rows": len(blank_scores),
        "after_run_date_rows": len(after_run_date),
        "rounds": dict(sorted(rounds.items())),
    }


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
    proc = run(["readback", "--cf", cf, "--vault", str(vault_path)], env, timeout=180)
    if proc.returncode != 0:
        return {"raw_rows": 0, "unique_keys": 0, "stdout_sha256": oracle_context.sha256_bytes(proc.stdout)}
    unique = set()
    rows = 0
    for line in proc.stdout.decode("utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        require(len(parts) >= 8 and parts[0] == "CF" and parts[1] == cf, "malformed_cf_line", {"cf": cf, "line": line[:200]})
        unique.add(parts[parts.index("KEY") + 1])
        rows += 1
    return {"raw_rows": rows, "unique_keys": len(unique), "stdout_sha256": oracle_context.sha256_bytes(proc.stdout)}


def recurrence_context_counts(vault_path: Path, env: dict[str, str]) -> dict[str, Any]:
    proc = run_ok(["readback", "--cf", "recurrence", "--vault", str(vault_path)], env, "recurrence_cf_failed")
    counts: dict[str, int] = {}
    raw_rows = 0
    for line in proc.stdout.decode("utf-8").splitlines():
        if not line.strip():
            continue
        raw_rows += 1
        parts = line.split("\t")
        value_hex = parts[parts.index("VALUE") + 1]
        row = json.loads(bytes.fromhex(value_hex).decode("utf-8"))
        context = json.loads(bytes(row["context"]["bytes"]).decode("utf-8"))
        outcome = context.get("outcome_anchor", {}).get("value") or context.get("oracle_verdict", {}).get("value")
        if outcome:
            counts[json.dumps(outcome, sort_keys=True)] = counts.get(json.dumps(outcome, sort_keys=True), 0) + 1
    return {"raw_rows": raw_rows, "outcomes": dict(sorted(counts.items())), "stdout_sha256": oracle_context.sha256_bytes(proc.stdout)}


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
    for cf_name in ["assay", "base", "recurrence", "time_index"]:
        ssts = sorted((vault_path / "cf" / cf_name).glob("*.sst"))
        if not ssts:
            continue
        cf_stats[cf_name] = {
            "sst_count": len(ssts),
            "bytes": sum(path.stat().st_size for path in ssts),
            "first_sha256": oracle_context.sha256_bytes(ssts[0].read_bytes()),
            "last_sha256": oracle_context.sha256_bytes(ssts[-1].read_bytes()),
        }
    require("assay" in cf_stats and "base" in cf_stats and "recurrence" in cf_stats, "missing_prediction_cfs", {"cf": sorted(cf_stats)})
    return {"files": files, "cf": cf_stats}


def self_consistency_series(validity: int = 48, invalid: int = 2) -> list[list[dict[str, Any]]]:
    series = []
    for idx in range(validity):
        outcome = {"enum": "stable"}
        series.append([{"outcome": outcome, "ground_truth": outcome}, {"outcome": outcome, "ground_truth": outcome}])
    for idx in range(invalid):
        outcome = {"enum": "unstable"}
        truth = {"enum": "other"}
        series.append([{"outcome": outcome, "ground_truth": truth}, {"outcome": outcome, "ground_truth": truth}])
    return series


def fixture_from_bits(bits_report: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    command = bits_report["bits"]["commands"]["label:match_result"]
    panel_bits = float(command["report"]["panel_sufficiency"])
    counts = oracle_sufficiency.outcome_counts_by_domain(generated["full_outcome_counts"])[DOMAIN]
    per_slot = command["per_slot_bits"]
    return {
        "domain": DOMAIN,
        "action_id": ACTION_ID,
        "panel": oracle_sufficiency.panel_from_bits(DOMAIN, per_slot, 650),
        "I_panel_oracle": panel_bits,
        "outcome_entropy_bits": oracle_sufficiency.entropy_bits(counts),
        "slot_bits": [{"slot": slot["slot"], "bits": slot["bits"]} for slot in per_slot],
        "prediction_observations": [
            {
                "outcome": {"enum": outcome},
                "count": count,
            }
            for outcome, count in sorted(counts.items())
        ],
        "self_consistency_series": self_consistency_series(),
        "n_samples": 150,
        "trust": "trusted",
        "clock_ts": 1783132200,
    }


def write_json(path: Path, payload: Any) -> dict[str, Any]:
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded + "\n", encoding="utf-8")
    require(path.read_text(encoding="utf-8") == encoded + "\n", "json_readback_mismatch", {"path": str(path.relative_to(ROOT))})
    return oracle_context.file_stat(path)


def run_predict(work_dir: Path, vault: dict[str, Any], fixture: dict[str, Any], name: str, expect_success: bool) -> dict[str, Any]:
    fixture_path = work_dir / f"{name}.json"
    fixture_stat = write_json(fixture_path, fixture)
    before = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["assay", "base", "recurrence"]}
    ledger_before = oracle_context.file_stat(vault["vault_path"] / "ledger_head" / "current.json") if (vault["vault_path"] / "ledger_head" / "current.json").exists() else None
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
        timeout=180,
    )
    payload = json.loads(proc.stdout.decode("utf-8", "replace"))
    after = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["assay", "base", "recurrence"]}
    ledger_after = oracle_context.file_stat(vault["vault_path"] / "ledger_head" / "current.json") if (vault["vault_path"] / "ledger_head" / "current.json").exists() else None
    if expect_success:
        require(proc.returncode == 0, "oracle_predict_expected_success_failed", {"payload": payload, "stderr": proc.stderr.decode("utf-8", "replace")})
        prediction = payload["prediction"]
        require(prediction["confidence"] <= prediction["bound"]["dpi_ceiling"] + 1e-6, "confidence_exceeds_dpi", {"prediction": prediction})
        require(ledger_after is not None, "prediction_missing_ledger_head")
    else:
        require(proc.returncode != 0, "oracle_predict_expected_failure_passed", {"payload": payload})
        require(payload.get("error_code") == "CALYX_ORACLE_INSUFFICIENT", "oracle_predict_wrong_error", {"payload": payload})
        require(payload.get("bound", {}).get("sufficient") is False, "oracle_predict_missing_insufficient_bound", {"payload": payload})
        require(ledger_before == ledger_after, "insufficient_prediction_wrote_ledger", {"before": ledger_before, "after": ledger_after})
    return {
        "fixture": fixture_stat,
        "returncode": proc.returncode,
        "stdout_sha256": oracle_context.sha256_bytes(proc.stdout),
        "stderr_sha256": oracle_context.sha256_bytes(proc.stderr),
        "before": before,
        "after": after,
        "ledger_before": ledger_before,
        "ledger_after": ledger_after,
        "payload": payload,
    }


def run_bits_report(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    report_path = work_dir / "bits_assay" / "report.json"
    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "tools" / "data" / "verify_soccer_lab_bits_assay.py"),
            "--raw-root",
            str(raw_root.relative_to(ROOT)),
            "--out",
            str(report_path.relative_to(ROOT)),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=420,
    )
    if proc.returncode != 0:
        raise OracleMatchPredictionError(
            "bits_assay_verifier_failed",
            {
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["report_file"] = oracle_context.file_stat(report_path)
    report["verifier_stdout_sha256"] = oracle_context.sha256_bytes(proc.stdout)
    report["verifier_stderr_sha256"] = oracle_context.sha256_bytes(proc.stderr)
    return report


def write_predictions(path: Path, fixtures: list[dict[str, Any]], refusal: dict[str, Any], report_path: Path) -> dict[str, Any]:
    bound = refusal["payload"]["bound"]
    records = []
    for fixture in fixtures:
        records.append(
            {
                **fixture,
                "domain": DOMAIN,
                "action_id": ACTION_ID,
                "prediction_status": "oracle_insufficient",
                "prediction": None,
                "confidence": 0.0,
                "confidence_caps": {
                    "dpi_ceiling": bound["dpi_ceiling"],
                    "sufficient": bound["sufficient"],
                },
                "provenance": {
                    "oracle_error_code": refusal["payload"]["error_code"],
                    "oracle_stdout_sha256": refusal["stdout_sha256"],
                    "oracle_fixture_sha256": refusal["fixture"]["sha256"],
                    "source_report": str(report_path.relative_to(ROOT)),
                },
            }
        )
    payload = {
        "schema_version": 1,
        "generated_at": "2026-07-04",
        "run_date": RUN_DATE.isoformat(),
        "domain": DOMAIN,
        "action_id": ACTION_ID,
        "records": records,
    }
    return write_json(path, payload)


def write_empty_predictions(path: Path, source_summary: dict[str, Any], report_path: Path) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "generated_at": "2026-07-04",
        "run_date": RUN_DATE.isoformat(),
        "domain": DOMAIN,
        "action_id": ACTION_ID,
        "status": "no_unplayed_fixtures_in_current_source",
        "reason": "current pulled 2026 match source has no blank-score rows and no fixture dates after run_date",
        "source_summary": source_summary,
        "source_report": str(report_path.relative_to(ROOT)),
        "records": [],
    }
    return write_json(path, payload)


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    vault = create_vault(work_dir, "soccer-oracle-match-prediction-edges")
    happy = {
        "domain": "synthetic.match_predict",
        "action_id": "predict_synthetic_match",
        "panel": oracle_context.minimal_panel(),
        "I_panel_oracle": 1.1,
        "outcome_entropy_bits": 1.0,
        "slot_bits": [{"slot": 0, "bits": 0.6}, {"slot": 1, "bits": 0.5}],
        "prediction_observations": [
            {"outcome": {"enum": "home_win"}, "count": 14},
            {"outcome": {"enum": "draw"}, "count": 4},
            {"outcome": {"enum": "away_win"}, "count": 2},
        ],
        "self_consistency_series": self_consistency_series(45, 5),
        "n_samples": 120,
        "trust": "trusted",
        "clock_ts": 1783132200,
    }
    happy_result = run_predict(work_dir, vault, happy, "synthetic-happy", expect_success=True)
    require(happy_result["payload"]["prediction"]["outcome"] == {"enum": "home_win"}, "synthetic_happy_outcome_mismatch", happy_result["payload"])

    insufficient = dict(happy)
    insufficient["domain"] = "synthetic.match_predict_insufficient"
    insufficient["I_panel_oracle"] = 0.25
    insufficient["outcome_entropy_bits"] = 1.0
    insufficient_result = run_predict(work_dir, vault, insufficient, "synthetic-insufficient", expect_success=False)

    no_write_edges = {}
    bad_cases = {
        "negative_bits": {"I_panel_oracle": -0.01},
        "empty_action": {"action_id": ""},
        "zero_samples": {"n_samples": 0},
    }
    for name, patch in bad_cases.items():
        fixture = dict(happy)
        fixture["domain"] = f"synthetic.{name}"
        fixture.update(patch)
        path = work_dir / f"synthetic-{name}.json"
        stat = write_json(path, fixture)
        before = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["assay", "base", "recurrence"]}
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
        after = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["assay", "base", "recurrence"]}
        require(proc.returncode != 0, "synthetic_bad_case_passed", {"case": name, "stdout": proc.stdout.decode("utf-8", "replace")})
        require(before == after, "synthetic_bad_case_wrote_cf", {"case": name, "before": before, "after": after})
        no_write_edges[name] = {
            "fixture": stat,
            "returncode": proc.returncode,
            "stderr_sha256": oracle_context.sha256_bytes(proc.stderr),
            "before": before,
            "after": after,
            "stderr_tail": proc.stderr.decode("utf-8", "replace")[-500:],
        }
    return {
        "vault_path": str(vault["vault_path"].relative_to(ROOT)),
        "happy_sufficient": happy_result,
        "known_insufficient": insufficient_result,
        "no_write_edges": no_write_edges,
        "physical_readback": physical_readback(vault["vault_path"]),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW.relative_to(ROOT)))
    parser.add_argument("--out", default=str(DEFAULT_OUT.relative_to(ROOT)))
    parser.add_argument("--predictions-out", default=str(DEFAULT_PREDICTIONS_OUT.relative_to(ROOT)))
    return parser.parse_args()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    raw_root = resolve(args.raw_root)
    report_path = resolve(args.out)
    predictions_path = resolve(args.predictions_out)
    work_dir = report_path.parent
    rows_root = work_dir / "rows"
    generated = oracle_context.generate_rows(raw_root, rows_root)
    source_summary = match_source_summary(raw_root)
    fixtures = unplayed_fixtures(raw_root)
    if not fixtures:
        prediction_file = write_empty_predictions(predictions_path, source_summary, report_path)
        edges = synthetic_edges(work_dir / "synthetic_edges")
        report = {
            "status": "blocked_no_unplayed_fixtures",
            "run_date": RUN_DATE.isoformat(),
            "reason": "current pulled match source has no unplayed fixtures after the run date; refusing to predict past scored rows as unplayed",
            "source_summary": source_summary,
            "generation": {key: value for key, value in generated.items() if key != "oracle_rows"},
            "prediction_file": prediction_file,
            "synthetic_edges": edges,
        }
        report_stat = write_json(report_path, report)
        print(
            json.dumps(
                {
                    "status": "blocked_no_unplayed_fixtures",
                    "report": str(report_path.relative_to(ROOT)),
                    "report_sha256": report_stat["sha256"],
                    "prediction_file": prediction_file,
                    "source_rows": source_summary["rows"],
                    "source_max_date": source_summary["max_date"],
                    "blank_score_rows": source_summary["blank_score_rows"],
                    "after_run_date_rows": source_summary["after_run_date_rows"],
                    "synthetic_edges": ["happy_sufficient", "known_insufficient", *sorted(edges["no_write_edges"])],
                },
                sort_keys=True,
            )
        )
        return 0
    bits_report = run_bits_report(work_dir, raw_root)
    vault = create_vault(work_dir / "real_prediction_vault", "soccer-oracle-match-predictions")
    fixture = fixture_from_bits(bits_report, generated)
    real_readback = run_predict(work_dir / "real_prediction_vault", vault, fixture, "oracle-predict-match-result", expect_success=False)
    recurrence_counts = recurrence_context_counts(vault["vault_path"], vault["env"])
    prediction_file = write_predictions(predictions_path, fixtures, real_readback, report_path)
    edges = synthetic_edges(work_dir / "synthetic_edges")
    report = {
        "status": "ok",
        "run_date": RUN_DATE.isoformat(),
        "unplayed_fixture_count": len(fixtures),
        "unplayed_fixture_sample": fixtures[:8],
        "generation": {key: value for key, value in generated.items() if key != "oracle_rows"},
        "bits_assay": {
            "report_file": bits_report["report_file"],
            "decoded_axes": bits_report["bits"]["decoded_axes"],
            "verifier_stdout_sha256": bits_report["verifier_stdout_sha256"],
            "verifier_stderr_sha256": bits_report["verifier_stderr_sha256"],
        },
        "real_oracle_predict": real_readback,
        "recurrence_context_counts": recurrence_counts,
        "real_prediction_vault": {
            "vault_name": vault["vault_name"],
            "vault_id": vault["vault_id"],
            "vault_path": str(vault["vault_path"].relative_to(ROOT)),
            "vault_salt": vault["vault_salt"],
            "create_stdout_sha256": vault["create_stdout_sha256"],
            "physical_readback": physical_readback(vault["vault_path"]),
        },
        "prediction_file": prediction_file,
        "synthetic_edges": edges,
    }
    report_stat = write_json(report_path, report)
    summary = {
        "status": "ok",
        "report": str(report_path.relative_to(ROOT)),
        "report_sha256": report_stat["sha256"],
        "prediction_file": prediction_file,
        "unplayed_fixture_count": len(fixtures),
        "oracle_predict_status": real_readback["payload"]["error_code"],
        "panel_bits": real_readback["payload"]["bound"]["I_panel_oracle"],
        "dpi_ceiling": real_readback["payload"]["bound"]["dpi_ceiling"],
        "synthetic_edges": ["happy_sufficient", "known_insufficient", *sorted(edges["no_write_edges"])],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
