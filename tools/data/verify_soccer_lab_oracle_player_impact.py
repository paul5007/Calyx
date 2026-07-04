#!/usr/bin/env python3
"""Verify Soccer Lab player impact/scorer Oracle predictions."""

from __future__ import annotations

import argparse
import csv
import json
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import verify_soccer_lab_oracle_context_ingest as oracle_context
import verify_soccer_lab_oracle_match_predictions as match_predictions
import verify_soccer_lab_oracle_sufficiency as oracle_sufficiency


ROOT = oracle_context.ROOT
DEFAULT_RAW = oracle_context.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "oracle_player_impact" / "report.json"
DEFAULT_PREDICTIONS_OUT = ROOT / "docs" / "data" / "soccer_lab_player_impact_predictions.json"
DOMAIN = "soccer_lab.player_impact"
ACTION_ID = "predict_player_impact"
IMPACT = "impact"
NO_IMPACT = "no_impact"


class PlayerImpactError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def require(condition: bool, reason: str, detail: dict[str, Any] | None = None) -> None:
    if not condition:
        raise PlayerImpactError(reason, detail)


def read_csv(path: Path) -> list[dict[str, str]]:
    rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    require(rows, "missing_csv_rows", {"path": str(path.relative_to(ROOT))})
    return rows


def scorer_labels(raw_root: Path) -> dict[str, Any]:
    root = raw_root / "fjelstul" / "data-csv"
    appearances_path = root / "player_appearances.csv"
    goals_path = root / "goals.csv"
    appearances = read_csv(appearances_path)
    goals = read_csv(goals_path)
    scored = {
        (goal["match_id"], goal["player_id"])
        for goal in goals
        if goal.get("player_id") and goal.get("own_goal") != "1"
    }
    labels = []
    for row in appearances:
        label = IMPACT if (row["match_id"], row["player_id"]) in scored else NO_IMPACT
        labels.append(
            {
                "label": label,
                "match_id": row["match_id"],
                "player_id": row["player_id"],
                "team_id": row["team_id"],
                "position_code": row["position_code"],
            }
        )
    counts = Counter(item["label"] for item in labels)
    require(counts[IMPACT] >= 50 and counts[NO_IMPACT] >= 50, "player_impact_class_floor_failed", dict(counts))
    support = []
    per_class_seen = Counter()
    for item in labels:
        if per_class_seen[item["label"]] < 150:
            support.append(item)
            per_class_seen[item["label"]] += 1
        if per_class_seen[IMPACT] == 150 and per_class_seen[NO_IMPACT] == 150:
            break
    require(per_class_seen[IMPACT] == 150 and per_class_seen[NO_IMPACT] == 150, "stratified_support_failed", dict(per_class_seen))
    return {
        "labels": labels,
        "support": support,
        "full_counts": dict(sorted(counts.items())),
        "support_counts": dict(sorted(per_class_seen.items())),
        "source_files": {
            "player_appearances": oracle_context.file_stat(appearances_path),
            "goals": oracle_context.file_stat(goals_path),
        },
    }


def read_2026_players(raw_root: Path) -> dict[str, Any]:
    path = raw_root / "mominullptr" / "fifa-world-cup-2026-dataset.zip"
    with zipfile.ZipFile(path) as archive:
        players = list(csv.DictReader(archive.read("squads_and_players.csv").decode("utf-8-sig").splitlines()))
        teams = {
            row["team_id"]: row
            for row in csv.DictReader(archive.read("teams.csv").decode("utf-8-sig").splitlines())
        }
        stats = list(csv.DictReader(archive.read("player_stats.csv").decode("utf-8-sig").splitlines()))
    require(players and teams and stats, "missing_2026_player_inputs")
    return {
        "source_file": oracle_context.file_stat(path),
        "players": players,
        "teams": teams,
        "stats_counts": dict(sorted(Counter("impact" if float(row.get("goals") or 0.0) > 0 else "no_impact" for row in stats).items())),
        "prior_goal_counts": dict(sorted(Counter("prior_goal" if float(row.get("goals") or 0.0) > 0 else "no_prior_goal" for row in players).items())),
    }


def fixture(label_data: dict[str, Any]) -> dict[str, Any]:
    counts = label_data["full_counts"]
    panel, slot_bits = oracle_sufficiency.zero_panel(DOMAIN, 680)
    return {
        "domain": DOMAIN,
        "action_id": ACTION_ID,
        "panel": panel,
        "I_panel_oracle": 0.0,
        "outcome_entropy_bits": oracle_sufficiency.entropy_bits(counts),
        "slot_bits": slot_bits,
        "prediction_observations": [
            {"outcome": {"enum": item["label"]}, "count": 1}
            for item in label_data["support"]
        ],
        "self_consistency_series": match_predictions.self_consistency_series(),
        "n_samples": sum(label_data["support_counts"].values()),
        "trust": "trusted",
        "clock_ts": 1783132200,
    }


def write_json(path: Path, payload: Any) -> dict[str, Any]:
    return match_predictions.write_json(path, payload)


def run_real(raw_root: Path, work_dir: Path) -> dict[str, Any]:
    labels = scorer_labels(raw_root)
    vault = match_predictions.create_vault(work_dir, "soccer-oracle-player-impact")
    readback = match_predictions.run_predict(work_dir, vault, fixture(labels), "oracle-predict-player-impact", expect_success=False)
    return {
        "vault_name": vault["vault_name"],
        "vault_id": vault["vault_id"],
        "vault_path": str(vault["vault_path"].relative_to(ROOT)),
        "vault_salt": vault["vault_salt"],
        "create_stdout_sha256": vault["create_stdout_sha256"],
        "class_distribution": {
            "full_counts": labels["full_counts"],
            "support_counts": labels["support_counts"],
            "outcome_entropy_bits": oracle_sufficiency.entropy_bits(labels["full_counts"]),
            "imbalance_ratio_no_impact_to_impact": labels["full_counts"][NO_IMPACT] / labels["full_counts"][IMPACT],
        },
        "source_files": labels["source_files"],
        "readback": readback,
        "recurrence_context_counts": match_predictions.recurrence_context_counts(vault["vault_path"], vault["env"]),
        "physical_readback": match_predictions.physical_readback(vault["vault_path"]),
    }


def write_predictions(path: Path, players_2026: dict[str, Any], real: dict[str, Any], report_path: Path) -> dict[str, Any]:
    bound = real["readback"]["payload"]["bound"]
    records = []
    teams = players_2026["teams"]
    for idx, row in enumerate(sorted(players_2026["players"], key=lambda item: int(item["player_id"]))):
        team = teams[row["team_id"]]
        records.append(
            {
                "player_id": row["player_id"],
                "player_name": row["player_name"],
                "team_id": row["team_id"],
                "team_name": team["team_name"],
                "position": row["position"],
                "source_row_index": idx,
                "prior_caps": float(row.get("caps") or 0.0),
                "prior_goals": float(row.get("goals") or 0.0),
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
                    "oracle_error_code": real["readback"]["payload"]["error_code"],
                    "oracle_stdout_sha256": real["readback"]["stdout_sha256"],
                    "oracle_fixture_sha256": real["readback"]["fixture"]["sha256"],
                    "source_report": str(report_path.relative_to(ROOT)),
                },
            }
        )
    payload = {
        "schema_version": 1,
        "generated_at": "2026-07-04",
        "source": "mominullptr/fifa-world-cup-2026-dataset squads_and_players.csv",
        "domain": DOMAIN,
        "action_id": ACTION_ID,
        "class_imbalance": real["class_distribution"],
        "records": records,
    }
    return write_json(path, payload)


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
    players_2026 = read_2026_players(raw_root)
    real = run_real(raw_root, work_dir / "real_player_impact_vault")
    prediction_file = write_predictions(predictions_path, players_2026, real, report_path)
    edges = match_predictions.synthetic_edges(work_dir / "synthetic_edges")
    report = {
        "status": "ok",
        "players_2026_source": {
            "source_file": players_2026["source_file"],
            "player_rows": len(players_2026["players"]),
            "teams": len(players_2026["teams"]),
            "prior_goal_counts": players_2026["prior_goal_counts"],
            "current_stats_scorer_counts_ex_post_not_used": players_2026["stats_counts"],
        },
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
                "records": len(json.loads(predictions_path.read_text(encoding="utf-8"))["records"]),
                "oracle_predict_status": real["readback"]["payload"]["error_code"],
                "I_panel_oracle": real["readback"]["payload"]["bound"]["I_panel_oracle"],
                "dpi_ceiling": real["readback"]["payload"]["bound"]["dpi_ceiling"],
                "full_counts": real["class_distribution"]["full_counts"],
                "support_counts": real["class_distribution"]["support_counts"],
                "synthetic_edges": ["happy_sufficient", "known_insufficient", *sorted(edges["no_write_edges"])],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
