#!/usr/bin/env python3
"""Generate Soccer Lab source-column to facet mapping docs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CODEBOOK = ROOT / "scratchpad" / "wc2026" / "raw" / "fjelstul" / "codebook" / "variables.csv"
DEFAULT_RAW_ROOT = ROOT / "scratchpad" / "wc2026" / "raw"
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
    "away_team_id",
    "away_team_name",
    "away_fifa_code",
    "away_formation",
    "away_manager",
    "capacity",
    "city",
    "club_team",
    "continent",
    "country",
    "date",
    "dayofweek",
    "elevation_meters",
    "elo_rating",
    "fifa_code",
    "fifa_points_pre_tournament",
    "fifa_rank_pre_tournament",
    "fifa_ranking_pre_tournament",
    "gameweek",
    "ground",
    "group",
    "group_letter",
    "height_cm",
    "home_fifa_code",
    "home_formation",
    "home_manager",
    "home_team_id",
    "home_team_name",
    "is_host",
    "is_knockout",
    "kickoff_time_utc",
    "latitude",
    "longitude",
    "manager_name",
    "num",
    "referee",
    "referee_id",
    "round",
    "stage_id",
    "start_time",
    "status",
    "team",
    "team1",
    "team2",
    "tactical_position",
    "time",
    "venue",
    "venue_id",
    "version",
}
PEDIGREE = {
    "female",
    "goal_keeper",
    "defender",
    "midfielder",
    "forward",
    "birth_date",
    "count_tournaments",
    "finals_before",
    "list_tournaments",
    "mens_team",
    "womens_team",
    "shirt_number",
    "avg_cards_per_game",
    "caps",
    "confederation",
    "date_of_birth",
    "fifa_ranking_pre_tournament",
    "goals_received_last_4y",
    "goals_scored_last_4y",
    "groups_passed_before",
    "losses_last_4y",
    "draws_last_4y",
    "market_value_eur",
    "position",
    "quarterfinals_before",
    "round16_before",
    "semifinals_before",
    "squad_avg_age",
    "squad_total_market_value_eur",
    "wins_last_4y",
    "world_cup_participations_before",
    "world_cup_titles_before",
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
    "advanced",
    "performance",
    "played",
    "assists",
    "average_rating",
    "away_score",
    "away_xg",
    "clean_sheets",
    "finalist",
    "goals",
    "goals_conceded",
    "home_score",
    "home_xg",
    "matches_played",
    "matches_started",
    "owngoal",
    "penalty_goals",
    "quarter_finalist",
    "semi_finalist",
    "xg",
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
    "attendance",
    "away_cards_red",
    "away_cards_yellow",
    "away_corners",
    "away_crosses",
    "away_fouls",
    "away_interceptions",
    "away_offsides",
    "away_possession",
    "away_saves",
    "away_sot",
    "away_total_shots",
    "corners",
    "data_source",
    "event_type",
    "fouls",
    "goals1",
    "goals2",
    "home_cards_red",
    "home_cards_yellow",
    "home_captain",
    "home_corners",
    "home_crosses",
    "home_fouls",
    "home_goalkeeper",
    "away_goalkeeper",
    "home_interceptions",
    "home_offsides",
    "home_possession",
    "home_saves",
    "home_sot",
    "home_total_shots",
    "is_starting_xi",
    "last_updated",
    "last_verified",
    "minutes_played",
    "notes",
    "offsides",
    "player_of_the_match",
    "player_of_the_match_id",
    "player_of_the_match_name",
    "possession_pct",
    "saves",
    "shots",
    "shots_on_target",
    "total_shots",
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
    "mominullptr.match_events",
    "mominullptr.match_lineups",
    "mominullptr.match_team_stats",
    "openfootball.matches.goals1",
    "openfootball.matches.goals2",
    "openfootball.matches.score",
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


def infer_type(values: list[str]) -> str:
    non_empty = [value.strip() for value in values if value.strip()]
    if not non_empty:
        return "unknown"
    lowered = {value.lower() for value in non_empty}
    if lowered <= {"0", "1", "true", "false", "yes", "no"}:
        return "boolean"
    try:
        for value in non_empty:
            int(value)
        return "integer"
    except ValueError:
        pass
    try:
        for value in non_empty:
            float(value)
        return "number"
    except ValueError:
        pass
    if all(len(value) >= 8 and value[4:5] == "-" and value[7:8] == "-" for value in non_empty):
        return "date"
    if len(lowered) <= max(20, len(non_empty) // 10):
        return "enum"
    return "text"


def archive_schema_rows(raw_root: Path) -> list[dict[str, str]]:
    files = {
        "harrachimustapha/fifa-world-cup-team-dataset.zip": "harrachimustapha",
        "mominullptr/fifa-world-cup-2026-dataset.zip": "mominullptr",
        "swaptr/fifa-wc-2026-matches.zip": "swaptr",
    }
    rows: list[dict[str, str]] = []
    for rel, source in files.items():
        path = raw_root / rel
        if not path.exists():
            continue
        try:
            archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise MappingError("read_archive_schema", "invalid_zip", {"path": str(path), "sha256": sha256_file(path)}) from exc
        with archive:
            for name in sorted(archive.namelist()):
                if not name.endswith(".csv"):
                    continue
                data = archive.read(name)
                try:
                    text = data.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise MappingError("read_archive_schema", "invalid_utf8", {"path": str(path), "member": name, "sha256": sha256_bytes(data)}) from exc
                reader = csv.DictReader(io.StringIO(text))
                if not reader.fieldnames:
                    raise MappingError("read_archive_schema", "missing_csv_header", {"path": str(path), "member": name})
                sample = list(reader)
                table = Path(name).stem
                dataset = f"{source}.{table}"
                for column in reader.fieldnames:
                    values = [record.get(column, "") for record in sample[:200]]
                    rows.append(
                        {
                            "dataset": dataset,
                            "variable": column,
                            "type": infer_type(values),
                            "description": f"Column `{column}` from `{name}` inside `{rel}`; physical archive rows={len(sample)}.",
                        }
                    )
    return rows


def openfootball_schema_rows(raw_root: Path) -> list[dict[str, str]]:
    path = raw_root / "openfootball/2026/worldcup.json"
    if not path.exists():
        return []
    payload = load_json_schema(path)
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        raise MappingError("read_openfootball_schema", "missing_matches_array", {"path": str(path)})
    root_rows = [
        {
            "dataset": "openfootball.root",
            "variable": key,
            "type": "array" if isinstance(value, list) else type(value).__name__,
            "description": f"Top-level OpenFootball World Cup 2026 JSON field `{key}`.",
        }
        for key, value in sorted(payload.items())
    ]
    match_keys = sorted({key for match in matches if isinstance(match, dict) for key in match})
    match_rows = [
        {
            "dataset": "openfootball.matches",
            "variable": key,
            "type": infer_json_type([match.get(key) for match in matches if isinstance(match, dict)]),
            "description": f"OpenFootball match object field `{key}` from `openfootball/2026/worldcup.json`; matches={len(matches)}.",
        }
        for key in match_keys
    ]
    score_values: dict[str, list[Any]] = {}
    goal_values: dict[tuple[str, str], list[Any]] = {}
    for match in matches:
        if not isinstance(match, dict):
            continue
        score = match.get("score")
        if isinstance(score, dict):
            for key, value in score.items():
                score_values.setdefault(key, []).append(value)
        for array_name in ["goals1", "goals2"]:
            goals = match.get(array_name)
            if isinstance(goals, list):
                for goal in goals:
                    if isinstance(goal, dict):
                        for key, value in goal.items():
                            goal_values.setdefault((array_name, key), []).append(value)
    nested_rows = [
        {
            "dataset": "openfootball.matches.score",
            "variable": key,
            "type": infer_json_type(values),
            "description": f"OpenFootball nested `score.{key}` field.",
        }
        for key, values in sorted(score_values.items())
    ]
    nested_rows.extend(
        {
            "dataset": f"openfootball.matches.{array_name}",
            "variable": key,
            "type": infer_json_type(values),
            "description": f"OpenFootball nested `{array_name}[].{key}` goal field.",
        }
        for (array_name, key), values in sorted(goal_values.items())
    )
    return root_rows + match_rows + nested_rows


def load_json_schema(path: Path) -> Any:
    data = path.read_bytes()
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise MappingError("read_openfootball_schema", "invalid_json", {"path": str(path), "sha256": sha256_bytes(data)}) from exc


def infer_json_type(values: list[Any]) -> str:
    present = [value for value in values if value is not None]
    if not present:
        return "unknown"
    if all(isinstance(value, bool) for value in present):
        return "boolean"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in present):
        return "integer"
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
        return "number"
    if all(isinstance(value, list) for value in present):
        return "array"
    if all(isinstance(value, dict) for value in present):
        return "object"
    return "text"


def normalization(var_type: str, variable: str) -> str:
    variable = variable.lower()
    var_type = var_type.lower()
    if variable in LINKS:
        return "store as provenance metadata; do not project"
    if variable in IDENTIFIERS:
        return "stable categorical id; metadata/token only"
    if "enum" in var_type or variable in {"result", "winner", "performance"}:
        return "categorical label; anchor enum or one-hot only in explanatory panels"
    if "logical" in var_type or "boolean" in var_type:
        return "boolean to 0.0/1.0 when projected"
    if "date" in var_type or variable.endswith("_date"):
        return "date to temporal features known before event when ex-ante"
    if "integer" in var_type or "number" in var_type or variable.startswith("count_") or variable in RESULTS:
        return "numeric min/max or bounded tournament-scale normalization"
    return "categorical/string token or one-hot family in projector"


def classify(dataset: str, variable: str) -> tuple[str, str, str, str]:
    variable = variable.lower()
    if variable in LINKS:
        return ("provenance", "metadata", "not_projected", "Link/provenance field.")
    if variable in IDENTIFIERS:
        return ("lineage", "metadata", "not_projected", "Stable id used for joins and readback.")
    if dataset in {"harrachimustapha.train", "harrachimustapha.test"}:
        if variable in {"winner", "finalist", "semi_finalist", "quarter_finalist"}:
            return ("outcome_anchor", "ex_post", "anchor_or_explanatory_only", "Tournament placement label; never allowed in future-prediction panel.")
        if variable in {"team", "continent", "version", "is_host"}:
            return ("context", "ex_ante", "predictive_allowed", "Tournament/team context knowable before the prediction target.")
        return ("pedigree", "ex_ante", "predictive_allowed", "Historical team feature knowable before the tournament.")
    if dataset == "mominullptr.squads_and_players" and variable == "goals":
        return ("pedigree", "ex_ante", "predictive_allowed", "Player prior/career feature in the squad registry.")
    if variable in RESULTS:
        return ("outcome_anchor", "ex_post", "anchor_or_explanatory_only", "Realized result/performance; never allowed in future-prediction panel.")
    if variable in EVENTS or dataset in EX_POST_DATASETS:
        return ("event_outcome", "ex_post", "anchor_or_explanatory_only", "In-event or post-event observation; explanatory/anchor use only.")
    if variable in PEDIGREE or dataset in {"players", "teams", "managers", "referees", "confederations", "awards", "mominullptr.teams", "mominullptr.squads_and_players", "mominullptr.venues", "mominullptr.tournament_stages", "mominullptr.referees"}:
        return ("pedigree", "ex_ante", "predictive_allowed", "Entity property knowable before the match/tournament.")
    if variable in CONTEXT or dataset in {"matches", "team_appearances", "tournaments", "tournament_stages", "groups", "stadiums", "manager_appointments", "referee_appointments", "squads"}:
        return ("context", "ex_ante", "predictive_allowed", "Fixture, tournament, role, venue, or appointment context.")
    if variable in NAMES:
        return ("lineage", "metadata", "not_projected", "Human-readable label; keep for provenance/UI, not math.")
    return ("lineage", "metadata", "not_projected", "Unclassified codebook field retained as metadata until explicitly promoted.")


def build_rows(codebook: Path, raw_root: Path) -> list[dict[str, str]]:
    rows = []
    if not raw_root.exists():
        raise MappingError("read_raw_schemas", "missing_raw_root", {"path": str(raw_root)})
    source_rows = read_codebook(codebook) + archive_schema_rows(raw_root) + openfootball_schema_rows(raw_root)
    seen: set[tuple[str, str]] = set()
    for row in source_rows:
        dataset = row["dataset"]
        variable = row["variable"]
        key = (dataset, variable)
        if key in seen:
            continue
        seen.add(key)
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


def verify_mapping(codebook: Path, raw_root: Path, mapping: Path) -> dict[str, Any]:
    expected = build_rows(codebook, raw_root)
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
    raw_root = resolve(args.raw_root)
    out = resolve(args.out)
    log_path = out.parent / "schema_facet_map.log.jsonl"
    if not str(codebook).startswith(str(ROOT.resolve())) or not str(raw_root).startswith(str(ROOT.resolve())) or not str(out).startswith(str(ROOT.resolve())):
        raise MappingError("config", "path_outside_repo", {"codebook": str(codebook), "raw_root": str(raw_root), "out": str(out)})
    log_event(log_path, {"event": "start", "mode": args.mode, "codebook": repo_path(codebook), "raw_root": repo_path(raw_root), "out": repo_path(out)})
    try:
        if args.mode == "write":
            rows = build_rows(codebook, raw_root)
            record = write_csv_checked(out, rows)
        else:
            record = verify_mapping(codebook, raw_root, out)
        record["codebook_sha256"] = sha256_file(codebook)
        record["raw_root"] = repo_path(raw_root)
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
    parser.add_argument("--raw-root", default=repo_path(DEFAULT_RAW_ROOT), help="raw source root with archive/JSON schemas")
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
