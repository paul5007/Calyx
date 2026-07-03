#!/usr/bin/env python3
"""Generate Soccer Lab source-column to facet mapping docs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CODEBOOK = ROOT / "scratchpad" / "wc2026" / "raw" / "fjelstul" / "codebook" / "variables.csv"
DEFAULT_OUT = ROOT / "docs" / "data" / "soccer_lab_column_facets.csv"

IDENTIFIERS = {
    "key_id",
    "tournament_id",
    "award_id",
    "booking_id",
    "goal_id",
    "match_id",
    "penalty_kick_id",
    "player_id",
    "team_id",
    "opponent_id",
    "manager_id",
    "referee_id",
    "stadium_id",
    "confederation_id",
    "substitution_id",
}
NAMES = {
    "tournament_name",
    "award_name",
    "award_description",
    "match_name",
    "team_name",
    "team_code",
    "opponent_name",
    "opponent_code",
    "family_name",
    "given_name",
    "player_team_name",
    "player_team_code",
    "stadium_name",
    "city_name",
    "country_name",
    "host_country",
    "federation_name",
    "region_name",
    "confederation_name",
    "confederation_code",
    "position_name",
    "position_code",
}
LINKS = {
    "player_wikipedia_link",
    "mens_team_wikipedia_link",
    "womens_team_wikipedia_link",
    "federation_wikipedia_link",
    "confederation_wikipedia_link",
    "manager_wikipedia_link",
    "referee_wikipedia_link",
    "stadium_wikipedia_link",
    "city_wikipedia_link",
}
CONTEXT = {
    "year",
    "match_date",
    "match_time",
    "start_date",
    "end_date",
    "stage_number",
    "stage_name",
    "group_name",
    "group_stage",
    "knockout_stage",
    "unbalanced_groups",
    "count_teams",
    "count_scheduled",
    "count_matches",
    "stadium_capacity",
    "home_team",
    "away_team",
}
PEDIGREE = {
    "female",
    "goal_keeper",
    "defender",
    "midfielder",
    "forward",
    "birth_date",
    "count_tournaments",
    "list_tournaments",
    "mens_team",
    "womens_team",
    "shirt_number",
}
RESULTS = {
    "score",
    "home_team_score",
    "away_team_score",
    "home_team_score_margin",
    "away_team_score_margin",
    "score_penalties",
    "home_team_score_penalties",
    "away_team_score_penalties",
    "result",
    "home_team_win",
    "away_team_win",
    "win",
    "wins",
    "lose",
    "losses",
    "draw",
    "draws",
    "winner",
    "host_won",
    "goals_for",
    "goals_against",
    "goal_differential",
    "goal_difference",
    "points",
    "position",
    "advanced",
    "performance",
    "played",
}
EVENTS = {
    "minute_label",
    "minute_regulation",
    "minute_stoppage",
    "match_period",
    "own_goal",
    "penalty",
    "converted",
    "yellow_card",
    "red_card",
    "second_yellow_card",
    "sending_off",
    "substitution",
    "substitute",
    "starter",
    "extra_time",
    "penalty_shootout",
    "penalties_for",
    "penalties_against",
    "replayed",
    "replay",
    "count_replays",
    "count_playoffs",
    "count_walkovers",
    "round_of_16",
    "quarter_finals",
    "semi_finals",
    "third_place_match",
    "final",
    "final_round",
    "second_group_stage",
    "shared",
    "year_introduced",
}
EX_POST_DATASETS = {
    "award_winners",
    "bookings",
    "goals",
    "group_standings",
    "host_countries",
    "manager_appearances",
    "penalty_kicks",
    "player_appearances",
    "qualified_teams",
    "referee_appearances",
    "substitutions",
    "tournament_standings",
}


class MappingError(RuntimeError):
    def __init__(self, stage: str, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(f"{stage}: {reason}")
        self.stage = stage
        self.reason = reason
        self.detail = detail or {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def log_event(log_path: Path, event: dict[str, Any]) -> None:
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as fh:
        fh.write(json.dumps(event, sort_keys=True).encode("utf-8") + b"\n")


def repo_path(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def read_codebook(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise MappingError("read_codebook", "missing_required_input", {"path": str(path)})
    data = path.read_bytes()
    if not data:
        raise MappingError("read_codebook", "empty_required_input", {"path": str(path)})
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MappingError("read_codebook", "invalid_utf8", {"path": str(path), "sha256": sha256_bytes(data)}) from exc
    reader = csv.DictReader(text.splitlines())
    required = {"dataset", "variable", "type", "description"}
    missing = sorted(required - set(reader.fieldnames or []))
    if missing:
        raise MappingError("read_codebook", "missing_required_columns", {"path": str(path), "missing": missing})
    rows = [dict(row) for row in reader]
    if not rows:
        raise MappingError("read_codebook", "no_variable_rows", {"path": str(path)})
    return rows


def normalization(var_type: str, variable: str) -> str:
    var_type = var_type.lower()
    if variable in LINKS:
        return "store as provenance metadata; do not project"
    if variable in IDENTIFIERS:
        return "stable categorical id; metadata/token only"
    if "enum" in var_type or variable in {"result", "winner", "performance"}:
        return "categorical label; anchor enum or one-hot only in explanatory panels"
    if "integer" in var_type or variable.startswith("count_") or variable in RESULTS or variable in CONTEXT:
        return "numeric min/max or bounded tournament-scale normalization"
    if "logical" in var_type or variable in PEDIGREE or variable in EVENTS:
        return "boolean to 0.0/1.0 when projected"
    if "date" in var_type or variable.endswith("_date"):
        return "date to temporal features known before event when ex-ante"
    return "categorical/string token or one-hot family in projector"


def classify(dataset: str, variable: str) -> tuple[str, str, str, str]:
    if variable in LINKS:
        return ("provenance", "metadata", "not_projected", "Link/provenance field.")
    if variable in IDENTIFIERS:
        return ("lineage", "metadata", "not_projected", "Stable id used for joins and readback.")
    if variable in RESULTS:
        return ("outcome_anchor", "ex_post", "anchor_or_explanatory_only", "Realized result/performance; never allowed in future-prediction panel.")
    if variable in EVENTS or dataset in EX_POST_DATASETS:
        return ("event_outcome", "ex_post", "anchor_or_explanatory_only", "In-event or post-event observation; explanatory/anchor use only.")
    if variable in PEDIGREE or dataset in {"players", "teams", "managers", "referees", "confederations", "awards"}:
        return ("pedigree", "ex_ante", "predictive_allowed", "Entity property knowable before the match/tournament.")
    if variable in CONTEXT or dataset in {"matches", "team_appearances", "tournaments", "tournament_stages", "groups", "stadiums", "manager_appointments", "referee_appointments", "squads"}:
        return ("context", "ex_ante", "predictive_allowed", "Fixture, tournament, role, venue, or appointment context.")
    if variable in NAMES:
        return ("lineage", "metadata", "not_projected", "Human-readable label; keep for provenance/UI, not math.")
    return ("lineage", "metadata", "not_projected", "Unclassified codebook field retained as metadata until explicitly promoted.")


def build_rows(codebook: Path) -> list[dict[str, str]]:
    rows = []
    for row in read_codebook(codebook):
        dataset = row["dataset"]
        variable = row["variable"]
        facet, timing, panel_use, rationale = classify(dataset, variable)
        rows.append(
            {
                "dataset": dataset,
                "column": variable,
                "source_type": row["type"],
                "facet": facet,
                "timing": timing,
                "panel_use": panel_use,
                "normalization": normalization(row["type"], variable),
                "description": row["description"].replace("\n", " ").strip(),
                "rationale": rationale,
            }
        )
    rows.sort(key=lambda r: (r["dataset"], r["column"]))
    return rows


def write_csv_checked(path: Path, rows: list[dict[str, str]]) -> dict[str, Any]:
    if not rows:
        raise MappingError("write_mapping", "no_rows")
    staging = path.parent / f".{path.name}.staging-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    staged = staging / path.name
    fields = ["dataset", "column", "source_type", "facet", "timing", "panel_use", "normalization", "description", "rationale"]
    with staged.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    observed = staged.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    staged.replace(path)
    shutil.rmtree(staging)
    final = path.read_bytes()
    if final != observed:
        raise MappingError("write_mapping", "published_readback_mismatch", {"path": str(path)})
    return {"path": repo_path(path), "bytes": len(final), "sha256": sha256_bytes(final), "rows": len(rows)}


def verify_mapping(codebook: Path, mapping: Path) -> dict[str, Any]:
    expected = build_rows(codebook)
    if not mapping.exists():
        raise MappingError("verify_mapping", "missing_mapping", {"path": str(mapping)})
    with mapping.open(encoding="utf-8", newline="") as fh:
        observed = list(csv.DictReader(fh))
    if expected != observed:
        mismatches = []
        by_key = {(r["dataset"], r["column"]): r for r in observed}
        for exp in expected:
            obs = by_key.get((exp["dataset"], exp["column"]))
            if obs != exp:
                mismatches.append({"dataset": exp["dataset"], "column": exp["column"], "expected": exp, "observed": obs})
        raise MappingError("verify_mapping", "mapping_mismatch", {"mismatch_count": len(mismatches), "mismatches": mismatches[:10]})
    return {"path": repo_path(mapping), "bytes": mapping.stat().st_size, "sha256": sha256_file(mapping), "rows": len(observed)}


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def run(args: argparse.Namespace) -> int:
    codebook = resolve(args.codebook)
    out = resolve(args.out)
    log_path = out.parent / "schema_facet_map.log.jsonl"
    if not str(codebook).startswith(str(ROOT.resolve())) or not str(out).startswith(str(ROOT.resolve())):
        raise MappingError("config", "path_outside_repo", {"codebook": str(codebook), "out": str(out)})
    log_event(log_path, {"event": "start", "mode": args.mode, "codebook": repo_path(codebook), "out": repo_path(out)})
    try:
        if args.mode == "write":
            rows = build_rows(codebook)
            record = write_csv_checked(out, rows)
        else:
            record = verify_mapping(codebook, out)
        record["codebook_sha256"] = sha256_file(codebook)
        log_event(log_path, {"event": "complete", "mode": args.mode, **record})
        print(json.dumps({"status": "ok", "mode": args.mode, **record}, sort_keys=True))
    except MappingError as exc:
        log_event(log_path, {"event": "error", "stage": exc.stage, "reason": exc.reason, **exc.detail})
        raise
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["write", "verify"])
    parser.add_argument("--codebook", default=repo_path(DEFAULT_CODEBOOK), help="Fjelstul variables.csv")
    parser.add_argument("--out", default=repo_path(DEFAULT_OUT), help="column mapping CSV")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except MappingError as exc:
        print(json.dumps({"status": "error", "stage": exc.stage, "reason": exc.reason, **exc.detail}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
