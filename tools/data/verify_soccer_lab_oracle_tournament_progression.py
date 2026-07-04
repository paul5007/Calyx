#!/usr/bin/env python3
"""Verify Soccer Lab tournament progression Oracle predictions."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import verify_soccer_lab_oracle_context_ingest as oracle_context
import verify_soccer_lab_oracle_match_predictions as match_predictions
import verify_soccer_lab_oracle_sufficiency as oracle_sufficiency


ROOT = oracle_context.ROOT
CALYX = oracle_context.CALYX
DEFAULT_RAW = oracle_context.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "oracle_tournament_progression" / "report.json"
DEFAULT_PREDICTIONS_OUT = ROOT / "docs" / "data" / "soccer_lab_tournament_progression_predictions.json"

AXES = {
    "winner": {
        "domain": "soccer_lab.tournament_winner",
        "action_id": "predict_tournament_winner",
        "prediction_field": "winner",
    },
    "finalist": {
        "domain": "soccer_lab.tournament_finalist",
        "action_id": "predict_tournament_finalist",
        "prediction_field": "finalist",
    },
    "semi_finalist": {
        "domain": "soccer_lab.tournament_semi_finalist",
        "action_id": "predict_tournament_semi_finalist",
        "prediction_field": "semi_finalist",
    },
}


class TournamentProgressionError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def require(condition: bool, reason: str, detail: dict[str, Any] | None = None) -> None:
    if not condition:
        raise TournamentProgressionError(reason, detail)


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise TournamentProgressionError(
            reason,
            {
                "args": args,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def read_harrachi(raw_root: Path, name: str) -> list[dict[str, str]]:
    path = raw_root / "harrachimustapha" / "fifa-world-cup-team-dataset.zip"
    with zipfile.ZipFile(path) as archive:
        payload = archive.read(name)
    rows = list(csv.DictReader(payload.decode("utf-8-sig").splitlines()))
    require(rows, "missing_harrachi_rows", {"member": name, "path": str(path.relative_to(ROOT))})
    return rows


def source_summary(raw_root: Path) -> dict[str, Any]:
    path = raw_root / "harrachimustapha" / "fifa-world-cup-team-dataset.zip"
    train = read_harrachi(raw_root, "train.csv")
    test = read_harrachi(raw_root, "test.csv")
    train_counts = {}
    test_counts = {}
    for axis in AXES:
        train_counts[axis] = dict(sorted(Counter(row[axis] for row in train).items()))
        test_counts[axis] = dict(sorted(Counter(row[axis] for row in test).items()))
    return {
        "source_file": oracle_context.file_stat(path),
        "train_rows": len(train),
        "test_rows": len(test),
        "train_counts": train_counts,
        "test_counts": test_counts,
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


def write_json(path: Path, payload: Any) -> dict[str, Any]:
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded + "\n", encoding="utf-8")
    require(path.read_text(encoding="utf-8") == encoded + "\n", "json_readback_mismatch", {"path": str(path.relative_to(ROOT))})
    return oracle_context.file_stat(path)


def fixture(axis: str, counts: dict[str, int], version: int) -> dict[str, Any]:
    spec = AXES[axis]
    panel, slot_bits = oracle_sufficiency.zero_panel(spec["domain"], version)
    return {
        "domain": spec["domain"],
        "action_id": spec["action_id"],
        "panel": panel,
        "I_panel_oracle": 0.0,
        "outcome_entropy_bits": oracle_sufficiency.entropy_bits(counts),
        "slot_bits": slot_bits,
        "prediction_observations": [
            {"outcome": {"enum": outcome}, "count": count}
            for outcome, count in sorted(counts.items())
        ],
        "self_consistency_series": match_predictions.self_consistency_series(),
        "n_samples": sum(counts.values()),
        "trust": "trusted",
        "clock_ts": 1783132200,
    }


def cf_rows(vault_path: Path, env: dict[str, str], cf: str) -> dict[str, Any]:
    return match_predictions.cf_rows(vault_path, env, cf)


def recurrence_context_counts(vault_path: Path, env: dict[str, str]) -> dict[str, Any]:
    return match_predictions.recurrence_context_counts(vault_path, env)


def physical_readback(vault_path: Path) -> dict[str, Any]:
    return match_predictions.physical_readback(vault_path)


def run_predict(work_dir: Path, vault: dict[str, Any], axis: str, fixture_payload: dict[str, Any]) -> dict[str, Any]:
    fixture_path = work_dir / f"oracle-predict-{axis}.json"
    fixture_stat = write_json(fixture_path, fixture_payload)
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
    require(proc.returncode != 0, "tournament_axis_unexpectedly_predicted", {"axis": axis, "payload": payload})
    require(payload.get("error_code") == "CALYX_ORACLE_INSUFFICIENT", "tournament_axis_wrong_error", {"axis": axis, "payload": payload})
    require(payload.get("bound", {}).get("sufficient") is False, "tournament_axis_missing_bound", {"axis": axis, "payload": payload})
    require(ledger_before == ledger_after, "insufficient_prediction_wrote_ledger", {"axis": axis, "before": ledger_before, "after": ledger_after})
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


def run_real_axes(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    train = read_harrachi(raw_root, "train.csv")
    vault = create_vault(work_dir, "soccer-oracle-tournament-progression")
    axes = {}
    for index, axis in enumerate(AXES):
        counts = dict(Counter(row[axis] for row in train))
        require("" not in counts, "train_axis_has_blank_outcomes", {"axis": axis, "counts": counts})
        readback = run_predict(work_dir, vault, axis, fixture(axis, counts, 670 + index))
        axes[axis] = {
            "domain": AXES[axis]["domain"],
            "action_id": AXES[axis]["action_id"],
            "outcome_counts": dict(sorted(counts.items())),
            "outcome_entropy_bits": oracle_sufficiency.entropy_bits(counts),
            "I_panel_oracle": 0.0,
            "deficit_bits": oracle_sufficiency.entropy_bits(counts),
            "readback": readback,
        }
    return {
        "vault_name": vault["vault_name"],
        "vault_id": vault["vault_id"],
        "vault_path": str(vault["vault_path"].relative_to(ROOT)),
        "vault_salt": vault["vault_salt"],
        "create_stdout_sha256": vault["create_stdout_sha256"],
        "axes": axes,
        "recurrence_context_counts": recurrence_context_counts(vault["vault_path"], vault["env"]),
        "physical_readback": physical_readback(vault["vault_path"]),
    }


def write_predictions(path: Path, raw_root: Path, axis_results: dict[str, Any], report_path: Path) -> dict[str, Any]:
    test = read_harrachi(raw_root, "test.csv")
    records = []
    for row_index, row in enumerate(test):
        for axis, result in axis_results.items():
            bound = result["readback"]["payload"]["bound"]
            records.append(
                {
                    "version": row["version"],
                    "team": row["team"],
                    "continent": row["continent"],
                    "source_row_index": row_index,
                    "axis": axis,
                    "domain": result["domain"],
                    "action_id": result["action_id"],
                    "prediction_status": "oracle_insufficient",
                    "prediction": None,
                    "confidence": 0.0,
                    "confidence_caps": {
                        "dpi_ceiling": bound["dpi_ceiling"],
                        "sufficient": bound["sufficient"],
                    },
                    "provenance": {
                        "oracle_error_code": result["readback"]["payload"]["error_code"],
                        "oracle_stdout_sha256": result["readback"]["stdout_sha256"],
                        "oracle_fixture_sha256": result["readback"]["fixture"]["sha256"],
                        "source_report": str(report_path.relative_to(ROOT)),
                    },
                }
            )
    payload = {
        "schema_version": 1,
        "generated_at": "2026-07-04",
        "source": "harrachimustapha/fifa-world-cup-team-dataset test.csv",
        "axes": {axis: {"domain": spec["domain"], "action_id": spec["action_id"]} for axis, spec in AXES.items()},
        "records": records,
    }
    return write_json(path, payload)


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    return match_predictions.synthetic_edges(work_dir)


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
    summary = source_summary(raw_root)
    real = run_real_axes(work_dir / "real_progression_vault", raw_root)
    prediction_file = write_predictions(predictions_path, raw_root, real["axes"], report_path)
    edges = synthetic_edges(work_dir / "synthetic_edges")
    report = {
        "status": "ok",
        "source_summary": summary,
        "real_oracle_predict": real,
        "prediction_file": prediction_file,
        "synthetic_edges": edges,
    }
    report_stat = write_json(report_path, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "report": str(report_path.relative_to(ROOT)),
                "report_sha256": report_stat["sha256"],
                "prediction_file": prediction_file,
                "axes": {
                    axis: {
                        "oracle_predict_status": result["readback"]["payload"]["error_code"],
                        "I_panel_oracle": result["readback"]["payload"]["bound"]["I_panel_oracle"],
                        "dpi_ceiling": result["readback"]["payload"]["bound"]["dpi_ceiling"],
                        "sufficient": result["readback"]["payload"]["bound"]["sufficient"],
                    }
                    for axis, result in real["axes"].items()
                },
                "records": len(json.loads(predictions_path.read_text(encoding="utf-8"))["records"]),
                "synthetic_edges": ["happy_sufficient", "known_insufficient", *sorted(edges["no_write_edges"])],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
