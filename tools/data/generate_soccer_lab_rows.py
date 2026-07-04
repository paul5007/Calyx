#!/usr/bin/env python3
"""Generate deterministic Calyx batch JSONL rows from Soccer Lab raw data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from collections import Counter
from datetime import date, datetime, time as datetime_time, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW = ROOT / "scratchpad" / "wc2026" / "raw"
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "rows"
FJELSTUL_SOURCE = "Fjelstul World Cup Database"


class RowGenError(RuntimeError):
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


def read_csv_required(path: Path, required: set[str]) -> list[dict[str, str]]:
    if not path.exists():
        raise RowGenError("read_csv", "missing_required_input", {"path": str(path)})
    raw = path.read_bytes()
    if not raw:
        raise RowGenError("read_csv", "empty_required_input", {"path": str(path)})
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RowGenError("read_csv", "invalid_utf8", {"path": str(path), "sha256": sha256_bytes(raw)}) from exc
    reader = csv.DictReader(text.splitlines())
    headers = set(reader.fieldnames or [])
    missing = sorted(required - headers)
    if missing:
        raise RowGenError("read_csv", "missing_required_columns", {"path": str(path), "missing": missing})
    rows = [dict(row) for row in reader]
    if not rows:
        raise RowGenError("read_csv", "no_data_rows", {"path": str(path)})
    return rows


def read_json_required(path: Path) -> Any:
    if not path.exists():
        raise RowGenError("read_json", "missing_required_input", {"path": str(path)})
    raw = path.read_bytes()
    if not raw:
        raise RowGenError("read_json", "empty_required_input", {"path": str(path)})
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RowGenError("read_json", "invalid_json", {"path": str(path), "sha256": sha256_bytes(raw)}) from exc


def token(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if text == "":
        return "NA"
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^A-Za-z0-9_.:+/-]", "", text)
    return text or "NA"


def bool01(value: str) -> str:
    value = value.strip().lower()
    if value in {"1", "true", "yes"}:
        return "1"
    if value in {"0", "false", "no", ""}:
        return "0"
    raise RowGenError("normalize", "invalid_boolean", {"value": value})


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def int_token(row: dict[str, str], key: str, default: str = "0") -> str:
    value = row.get(key, "").strip()
    return value if value not in {"", "NA"} else default


def num(row: dict[str, str], key: str, default: float = 0.0) -> float:
    value = row.get(key, "").strip()
    if value in {"", "NA", "not available", "not applicable"}:
        return default
    return float(value)


def rate(values: list[float], default: float = 0.0) -> float:
    return sum(values) / len(values) if values else default


def match_sort_key(row: dict[str, str]) -> tuple[str, int]:
    return row.get("match_date", ""), int(row.get("key_id", "0") or 0)


def stat_line(pairs: list[tuple[str, Any]]) -> str:
    return " ".join(f"{key}={token(value)}" for key, value in pairs)


def date_t_secs(value: str) -> int:
    day = date.fromisoformat(value)
    return int(datetime.combine(day, datetime_time.min, tzinfo=timezone.utc).timestamp())


def non_negative_date_t_secs(value: str) -> int | None:
    t_secs = date_t_secs(value)
    return t_secs if t_secs >= 0 else None


def metadata(entity: str, dataset: str, source_key: str, row: dict[str, str]) -> dict[str, str]:
    return {
        "project": "Soccer Lab",
        "entity": entity,
        "source_dataset": dataset,
        "source": FJELSTUL_SOURCE,
        "source_key": source_key,
        "source_row_key": row.get("key_id", ""),
    }


def oracle_event(domain: str, action: str, anchor: dict[str, Any], t_secs: int | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "domain": domain,
        "action": action,
        "outcome": str(anchor["value"]),
        "outcome_kind": anchor["kind"],
        "grounded": True,
    }
    if t_secs is not None:
        event["t_secs"] = t_secs
    return event


def match_result_anchor(row: dict[str, str]) -> dict[str, Any]:
    result = row.get("result", "").strip().lower()
    if result == "home team win":
        value = "home_win"
    elif result == "away team win":
        value = "away_win"
    elif result == "draw":
        value = "draw"
    else:
        raise RowGenError("matches", "unknown_match_result", {"match_id": row.get("match_id"), "result": result})
    return {"kind": "label:match_result", "value": value, "source": FJELSTUL_SOURCE, "confidence": 1.0}


def team_result_anchor(row: dict[str, str]) -> dict[str, Any]:
    result = row.get("result", "").strip().lower()
    if result not in {"win", "lose", "draw"}:
        raise RowGenError("teams_history", "unknown_team_result", {"match_id": row.get("match_id"), "team_id": row.get("team_id"), "result": result})
    return {"kind": "label:team_match_result", "value": result, "source": FJELSTUL_SOURCE, "confidence": 1.0}


def booking_counts(raw_root: Path) -> dict[tuple[str, str], dict[str, float]]:
    path = raw_root / "fjelstul" / "data-csv" / "bookings.csv"
    rows = read_csv_required(path, {"match_id", "team_id", "yellow_card", "red_card", "second_yellow_card", "sending_off"})
    counts: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        key = (row["match_id"], row["team_id"])
        entry = counts.setdefault(key, {"yellow": 0.0, "red": 0.0, "second_yellow": 0.0, "sending_off": 0.0})
        entry["yellow"] += num(row, "yellow_card")
        entry["red"] += num(row, "red_card")
        entry["second_yellow"] += num(row, "second_yellow_card")
        entry["sending_off"] += num(row, "sending_off")
    return counts


def read_harrachi_split(raw_root: Path, name: str) -> list[dict[str, str]]:
    path = raw_root / "harrachimustapha" / "fifa-world-cup-team-dataset.zip"
    if not path.exists():
        raise RowGenError("team_tournaments", "missing_required_input", {"path": str(path.relative_to(ROOT))})
    raw = path.read_bytes()
    if not raw:
        raise RowGenError("team_tournaments", "empty_required_input", {"path": str(path.relative_to(ROOT))})
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise RowGenError("team_tournaments", "invalid_zip", {"path": str(path.relative_to(ROOT)), "sha256": sha256_bytes(raw)}) from exc
    required = {
        "version",
        "team",
        "continent",
        "is_host",
        "goals_scored_last_4y",
        "goals_received_last_4y",
        "wins_last_4y",
        "losses_last_4y",
        "draws_last_4y",
        "world_cup_titles_before",
        "squad_total_market_value_eur",
        "fifa_rank_pre_tournament",
        "fifa_points_pre_tournament",
        "squad_avg_age",
        "world_cup_participations_before",
        "groups_passed_before",
        "round16_before",
        "quarterfinals_before",
        "semifinals_before",
        "finals_before",
        "winner",
        "finalist",
        "semi_finalist",
        "quarter_finalist",
    }
    try:
        payload = archive.read(name)
    except KeyError as exc:
        raise RowGenError("team_tournaments", "missing_zip_member", {"path": str(path.relative_to(ROOT)), "member": name}) from exc
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RowGenError("team_tournaments", "invalid_utf8", {"path": str(path.relative_to(ROOT)), "member": name, "sha256": sha256_bytes(payload)}) from exc
    reader = csv.DictReader(text.splitlines())
    headers = set(reader.fieldnames or [])
    missing = sorted(required - headers)
    if missing:
        raise RowGenError("team_tournaments", "missing_required_columns", {"path": str(path.relative_to(ROOT)), "member": name, "missing": missing})
    rows = [dict(row) for row in reader]
    if not rows:
        raise RowGenError("team_tournaments", "empty_zip_member", {"path": str(path.relative_to(ROOT)), "member": name})
    return rows


def read_swaptr_matches(raw_root: Path) -> list[dict[str, str]]:
    path = raw_root / "swaptr" / "fifa-wc-2026-matches.zip"
    if not path.exists():
        raise RowGenError("matches_2026", "missing_required_input", {"path": str(path.relative_to(ROOT))})
    raw = path.read_bytes()
    if not raw:
        raise RowGenError("matches_2026", "empty_required_input", {"path": str(path.relative_to(ROOT))})
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise RowGenError("matches_2026", "invalid_zip", {"path": str(path.relative_to(ROOT)), "sha256": sha256_bytes(raw)}) from exc
    try:
        payload = archive.read("matches.csv")
    except KeyError as exc:
        raise RowGenError("matches_2026", "missing_zip_member", {"path": str(path.relative_to(ROOT)), "member": "matches.csv"}) from exc
    required = {"round", "gameweek", "date", "start_time", "home_team", "away_team", "score", "venue"}
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RowGenError("matches_2026", "invalid_utf8", {"path": str(path.relative_to(ROOT)), "member": "matches.csv", "sha256": sha256_bytes(payload)}) from exc
    reader = csv.DictReader(text.splitlines())
    headers = set(reader.fieldnames or [])
    missing = sorted(required - headers)
    if missing:
        raise RowGenError("matches_2026", "missing_required_columns", {"path": str(path.relative_to(ROOT)), "member": "matches.csv", "missing": missing})
    rows = [dict(row) for row in reader]
    if not rows:
        raise RowGenError("matches_2026", "empty_zip_member", {"path": str(path.relative_to(ROOT)), "member": "matches.csv"})
    return rows


def read_mominull_member(raw_root: Path, name: str, required: set[str], stage: str = "players_2026") -> list[dict[str, str]]:
    path = raw_root / "mominullptr" / "fifa-world-cup-2026-dataset.zip"
    if not path.exists():
        raise RowGenError(stage, "missing_required_input", {"path": str(path.relative_to(ROOT))})
    raw = path.read_bytes()
    if not raw:
        raise RowGenError(stage, "empty_required_input", {"path": str(path.relative_to(ROOT))})
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise RowGenError(stage, "invalid_zip", {"path": str(path.relative_to(ROOT)), "sha256": sha256_bytes(raw)}) from exc
    try:
        payload = archive.read(name)
    except KeyError as exc:
        raise RowGenError(stage, "missing_zip_member", {"path": str(path.relative_to(ROOT)), "member": name}) from exc
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RowGenError(stage, "invalid_utf8", {"path": str(path.relative_to(ROOT)), "member": name, "sha256": sha256_bytes(payload)}) from exc
    reader = csv.DictReader(text.splitlines())
    headers = set(reader.fieldnames or [])
    missing = sorted(required - headers)
    if missing:
        raise RowGenError(stage, "missing_required_columns", {"path": str(path.relative_to(ROOT)), "member": name, "missing": missing})
    rows = [dict(row) for row in reader]
    if not rows:
        raise RowGenError(stage, "empty_zip_member", {"path": str(path.relative_to(ROOT)), "member": name})
    return rows


TEAM_ALIASES = {
    "Bosnia-Herz": "Bosnia and Herzegovina",
    "Bosnia–Herz": "Bosnia and Herzegovina",
    "Cabo Verde": "Cape Verde",
    "Congo DR": "DR Congo",
    "Curaçao": "Cura?o",
    "Czechia": "Czech Republic",
    "Côte d'Ivoire": "Ivory Coast",
    "IR Iran": "Iran",
    "Korea Republic": "South Korea",
    "Türkiye": "Turkey",
}


def canonical_team(name: str) -> str:
    return TEAM_ALIASES.get(name, name)


def harrachi_num(row: dict[str, str], key: str) -> float:
    value = row.get(key, "").strip()
    if value == "":
        return 0.0
    return float(value)


def best_finish(row: dict[str, str]) -> int:
    if harrachi_num(row, "world_cup_titles_before") > 0:
        return 1
    if harrachi_num(row, "finals_before") > 0:
        return 2
    if harrachi_num(row, "semifinals_before") > 0:
        return 4
    if harrachi_num(row, "quarterfinals_before") > 0:
        return 8
    if harrachi_num(row, "round16_before") > 0:
        return 16
    return 32


def harrachi_team_feature_pairs(row: dict[str, str], home_team: Any | None = None, away_team: Any | None = None) -> list[tuple[str, Any]]:
    wins = harrachi_num(row, "wins_last_4y")
    losses = harrachi_num(row, "losses_last_4y")
    draws = harrachi_num(row, "draws_last_4y")
    matches = max(1.0, wins + losses + draws)
    goals_for_per_match = harrachi_num(row, "goals_scored_last_4y") / matches
    goals_against_per_match = harrachi_num(row, "goals_received_last_4y") / matches
    goal_diff = goals_for_per_match - goals_against_per_match
    rank = max(1.0, harrachi_num(row, "fifa_rank_pre_tournament"))
    market_value = harrachi_num(row, "squad_total_market_value_eur")
    home_value = row["is_host"] if home_team is None else home_team
    away_value = (0 if row["is_host"] == "1" else 1) if away_team is None else away_team
    return [
        ("home_team", home_value),
        ("away_team", away_value),
        ("trailing_goals_for_per_match", goals_for_per_match),
        ("trailing_goal_scoring_rate", clamp((wins + draws) / matches)),
        ("trailing_multi_goal_rate", clamp(goals_for_per_match / 2.0)),
        ("trailing_penalties_for_per_match", 0),
        ("trailing_goals_against_per_match", goals_against_per_match),
        ("trailing_clean_sheet_rate", clamp(wins / matches)),
        ("trailing_multi_concede_rate", clamp(goals_against_per_match / 2.0)),
        ("trailing_penalties_against_per_match", 0),
        ("trailing_goal_differential", goal_diff),
        ("trailing_extra_time_rate", 0),
        ("trailing_penalty_shootout_rate", 0),
        ("trailing_replay_rate", 0),
        ("days_since_previous_match", 0),
        ("trailing_yellow_cards_per_match", 0),
        ("trailing_red_cards_per_match", 0),
        ("trailing_second_yellow_rate", 0),
        ("trailing_sending_off_rate", 0),
        ("confederation_code", row["continent"]),
        ("region_name", row["continent"]),
        ("mens_team", 1),
        ("womens_team", 0),
        ("prior_world_cup_matches", row["world_cup_participations_before"]),
        ("prior_best_finish", best_finish(row)),
        ("trailing_win_rate", clamp(wins / matches)),
        ("trailing_draw_rate", clamp(draws / matches)),
        ("trailing_loss_rate", clamp(losses / matches)),
        ("trailing_unbeaten_rate", clamp((wins + draws) / matches)),
        ("fifa_rank_pre_tournament", rank),
        ("fifa_points_pre_tournament", row["fifa_points_pre_tournament"]),
        ("squad_total_market_value_eur", market_value),
        ("squad_avg_age", row["squad_avg_age"]),
    ]


def team_tournament_text(row: dict[str, str], split: str) -> str:
    return stat_line(
        [
            ("entity", "team_tournament"),
            ("dataset_split", split),
            ("version", row["version"]),
            ("team", row["team"]),
            ("team_id", f"{row['version']}:{row['team']}"),
            ("tournament_id", f"WC-{row['version']}"),
            ("match_id", f"WC-{row['version']}:{row['team']}"),
            ("stage_name", "pre_tournament"),
            ("group_name", ""),
            ("group_stage", 1),
            ("knockout_stage", 0),
            ("date", f"{row['version']}-01-01"),
            ("match_day_of_tournament", 0),
            ("kickoff_hour", 0),
            ("host_country", row["is_host"]),
            ("stadium_capacity", 0),
            *harrachi_team_feature_pairs(row),
        ]
    )


def team_tournament_anchor(row: dict[str, str], axis: str) -> dict[str, Any] | None:
    value = row.get(axis, "").strip()
    if value == "":
        return None
    if value not in {"0", "1"}:
        raise RowGenError("team_tournaments", "invalid_outcome_value", {"axis": axis, "value": value, "team": row.get("team"), "version": row.get("version")})
    return {
        "kind": f"label:{axis}",
        "value": value,
        "source": "harrachimustapha/fifa-world-cup-team-dataset",
        "confidence": 1.0,
    }


def team_tournament_rows(raw_root: Path) -> list[dict[str, Any]]:
    source_path = raw_root / "harrachimustapha" / "fifa-world-cup-team-dataset.zip"
    source_key = sha256_file(source_path) if source_path.exists() else ""
    out = []
    for split in ["train", "test"]:
        for idx, row in enumerate(read_harrachi_split(raw_root, f"{split}.csv")):
            anchors = [anchor for axis in ["winner", "finalist", "semi_finalist", "quarter_finalist"] if (anchor := team_tournament_anchor(row, axis))]
            winner_anchor = team_tournament_anchor(row, "winner")
            out.append(
                {
                    "text": team_tournament_text(row, split),
                    "metadata": {
                        "project": "Soccer Lab",
                        "entity": "team_tournament",
                        "source_dataset": f"harrachimustapha.fifa-world-cup-team-dataset.{split}",
                        "source": "Kaggle: harrachimustapha/fifa-world-cup-team-dataset",
                        "source_key": source_key,
                        "source_row_index": str(idx),
                        "team": row["team"],
                        "version": row["version"],
                        "dataset_split": split,
                    },
                    **({"anchors": anchors} if anchors else {}),
                    **(
                        {
                            "oracle": oracle_event(
                                "soccer_lab.tournament_winner",
                                "predict_tournament_winner",
                                winner_anchor,
                                non_negative_date_t_secs(f"{row['version']}-01-01"),
                            )
                        }
                        if winner_anchor
                        else {}
                    ),
                }
            )
    return out


def score_anchor(row: dict[str, str]) -> dict[str, Any]:
    home = num(row, "home_score")
    away = num(row, "away_score")
    if home > away:
        value = "home_win"
    elif away > home:
        value = "away_win"
    else:
        value = "draw"
    return {"kind": "label:match_result", "value": value, "source": "swaptr/fifa-wc-2026-matches", "confidence": 1.0}


def match_2026_rows(raw_root: Path) -> list[dict[str, Any]]:
    source_path = raw_root / "swaptr" / "fifa-wc-2026-matches.zip"
    source_key = sha256_file(source_path) if source_path.exists() else ""
    harrachi = {row["team"]: row for row in read_harrachi_split(raw_root, "test.csv")}
    out = []
    for idx, row in enumerate(read_swaptr_matches(raw_root)):
        home_key = canonical_team(row["home_team"])
        if home_key not in harrachi:
            raise RowGenError("matches_2026", "missing_home_team_prior", {"home_team": row["home_team"], "canonical": home_key})
        home_prior = harrachi[home_key]
        stage = row.get("round", "")
        group_stage = 1 if "group" in stage.lower() else 0
        knockout_stage = 0 if group_stage else 1
        venue = row.get("venue", "")
        match_id = f"WC-2026-M{idx + 1:03d}"
        anchor = score_anchor(row)
        out.append(
            {
                "text": stat_line(
                    [
                        ("entity", "match_2026"),
                        ("match_id", match_id),
                        ("tournament_id", "WC-2026"),
                        ("stage_name", stage),
                        ("group_name", ""),
                        ("group_stage", group_stage),
                        ("knockout_stage", knockout_stage),
                        ("date", row["date"]),
                        ("match_day_of_tournament", idx),
                        ("kickoff_hour", row["start_time"][:2]),
                        ("venue", venue),
                        ("home_team_id", row["home_team"]),
                        ("away_team_id", row["away_team"]),
                        ("team_id", row["home_team"]),
                        ("opponent_id", row["away_team"]),
                        ("host_country", home_prior["is_host"]),
                        ("stadium_capacity", 0),
                        *harrachi_team_feature_pairs(home_prior, home_team=1, away_team=0),
                    ]
                ),
                "metadata": {
                    "project": "Soccer Lab",
                    "entity": "match_2026",
                    "source_dataset": "swaptr.fifa-wc-2026-matches.matches",
                    "source": "Kaggle: swaptr/fifa-wc-2026-matches",
                    "source_key": source_key,
                    "source_row_index": str(idx),
                    "match_id": match_id,
                    "home_team": row["home_team"],
                    "away_team": row["away_team"],
                    "date": row["date"],
                },
                "anchors": [anchor],
                "oracle": oracle_event(
                    "soccer_lab.match_result",
                    "predict_match_result",
                    anchor,
                    non_negative_date_t_secs(row["date"]),
                ),
            }
        )
    return out


def position_flags(position: str) -> dict[str, int]:
    code = position.strip().upper()
    return {
        "goal_keeper": 1 if code == "GK" else 0,
        "defender": 1 if code in {"DEF", "DF", "CB", "LB", "RB", "LWB", "RWB"} else 0,
        "midfielder": 1 if code in {"MID", "MF", "CM", "DM", "AM", "LM", "RM"} else 0,
        "forward": 1 if code in {"FWD", "FW", "ST", "CF", "LW", "RW"} else 0,
    }


def player_2026_rows(raw_root: Path) -> list[dict[str, Any]]:
    source_path = raw_root / "mominullptr" / "fifa-world-cup-2026-dataset.zip"
    source_key = sha256_file(source_path) if source_path.exists() else ""
    players = read_mominull_member(
        raw_root,
        "squads_and_players.csv",
        {"player_id", "team_id", "player_name", "position", "club_team", "market_value_eur", "caps", "date_of_birth", "height_cm", "goals"},
    )
    teams = {
        row["team_id"]: row
        for row in read_mominull_member(
            raw_root,
            "teams.csv",
            {"team_id", "team_name", "fifa_code", "group_letter", "confederation", "fifa_ranking_pre_tournament", "elo_rating", "manager_name"},
        )
    }
    out = []
    for idx, row in enumerate(sorted(players, key=lambda item: int(item["player_id"]))):
        team = teams.get(row["team_id"])
        if team is None:
            raise RowGenError("players_2026", "missing_team_for_player", {"player_id": row["player_id"], "team_id": row["team_id"]})
        caps = harrachi_num(row, "caps")
        goals = harrachi_num(row, "goals")
        appearances = max(caps, 0.0)
        starts = appearances
        flags = position_flags(row["position"])
        trailing_goal_rate = clamp(goals / appearances) if appearances > 0 else 0.0
        out.append(
            {
                "text": stat_line(
                    [
                        ("entity", "player_2026"),
                        ("player_id", row["player_id"]),
                        ("team_id", row["team_id"]),
                        ("team_name", team["team_name"]),
                        ("team_code", team["fifa_code"]),
                        ("group_letter", team["group_letter"]),
                        ("confederation_code", team["confederation"]),
                        ("player_name", row["player_name"]),
                        ("club_team", row["club_team"]),
                        ("position_code", row["position"]),
                        ("female", 0),
                        ("goal_keeper", flags["goal_keeper"]),
                        ("defender", flags["defender"]),
                        ("midfielder", flags["midfielder"]),
                        ("forward", flags["forward"]),
                        ("count_tournaments", 1),
                        ("prior_appearances", appearances),
                        ("prior_goals", goals),
                        ("trailing_goal_rate", trailing_goal_rate),
                        ("prior_penalties_converted", 0),
                        ("prior_penalty_kicks", 0),
                        ("prior_starts", starts),
                        ("prior_substitute_appearances", 0),
                        ("prior_substitute_goals", 0),
                        ("prior_yellow_cards", 0),
                        ("prior_red_cards", 0),
                        ("market_value_eur", row["market_value_eur"]),
                        ("height_cm", row["height_cm"]),
                        ("fifa_rank_pre_tournament", team["fifa_ranking_pre_tournament"]),
                        ("elo_rating", team["elo_rating"]),
                    ]
                ),
                "metadata": {
                    "project": "Soccer Lab",
                    "entity": "player_2026",
                    "source_dataset": "mominullptr.fifa-world-cup-2026-dataset.squads_and_players",
                    "source": "Kaggle: mominullptr/fifa-world-cup-2026-dataset",
                    "source_key": source_key,
                    "source_row_index": str(idx),
                    "player_id": row["player_id"],
                    "team_id": row["team_id"],
                    "team_name": team["team_name"],
                },
            }
        )
    return out


def team_profiles(raw_root: Path) -> dict[str, dict[str, str]]:
    path = raw_root / "fjelstul" / "data-csv" / "teams.csv"
    rows = read_csv_required(path, {"team_id", "confederation_code", "region_name", "mens_team", "womens_team"})
    return {row["team_id"]: row for row in rows}


def tournament_context(raw_root: Path) -> dict[str, dict[str, str]]:
    path = raw_root / "fjelstul" / "data-csv" / "tournaments.csv"
    rows = read_csv_required(path, {"tournament_id", "start_date", "host_country"})
    return {row["tournament_id"]: row for row in rows}


def stadium_capacity(raw_root: Path) -> dict[str, str]:
    path = raw_root / "fjelstul" / "data-csv" / "stadiums.csv"
    rows = read_csv_required(path, {"stadium_id", "stadium_capacity"})
    return {row["stadium_id"]: row["stadium_capacity"] for row in rows}


def tournament_positions(raw_root: Path) -> dict[tuple[str, str], float]:
    path = raw_root / "fjelstul" / "data-csv" / "tournament_standings.csv"
    rows = read_csv_required(path, {"tournament_id", "team_id", "position"})
    return {(row["tournament_id"], row["team_id"]): num(row, "position", 32.0) for row in rows}


def team_prior_stats(
    prior: list[dict[str, Any]],
    row: dict[str, str],
    profiles: dict[str, dict[str, str]],
    tournaments: dict[str, dict[str, str]],
    capacities: dict[str, str],
) -> list[tuple[str, Any]]:
    goals_for = [entry["goals_for"] for entry in prior]
    goals_against = [entry["goals_against"] for entry in prior]
    differentials = [entry["goal_differential"] for entry in prior]
    penalties_for = [entry["penalties_for"] for entry in prior]
    penalties_against = [entry["penalties_against"] for entry in prior]
    wins = [entry["win"] for entry in prior]
    draws = [entry["draw"] for entry in prior]
    losses = [entry["lose"] for entry in prior]
    extra_times = [entry["extra_time"] for entry in prior]
    shootouts = [entry["penalty_shootout"] for entry in prior]
    replays = [entry["replay"] for entry in prior]
    yellows = [entry["yellow"] for entry in prior]
    reds = [entry["red"] for entry in prior]
    second_yellows = [entry["second_yellow"] for entry in prior]
    sending_offs = [entry["sending_off"] for entry in prior]
    profile = profiles.get(row["team_id"], {})
    tournament = tournaments.get(row["tournament_id"], {})
    prior_positions = [entry["position"] for entry in prior if entry.get("position")]
    prior_best_finish = min(prior_positions) if prior_positions else 32.0
    previous_date = prior[-1]["date"] if prior else None
    match_date = date.fromisoformat(row["match_date"])
    days_since_previous_match = (match_date - previous_date).days if previous_date else 0
    start_raw = tournament.get("start_date", row["match_date"])
    tournament_start = date.fromisoformat(start_raw)
    host_country = 1 if token(row.get("country_name", "")) == token(tournament.get("host_country", "")) else 0
    return [
        ("trailing_goals_for_per_match", rate(goals_for)),
        ("trailing_goal_scoring_rate", rate([1.0 if value > 0 else 0.0 for value in goals_for])),
        ("trailing_multi_goal_rate", rate([1.0 if value >= 2 else 0.0 for value in goals_for])),
        ("trailing_penalties_for_per_match", rate(penalties_for)),
        ("trailing_goals_against_per_match", rate(goals_against)),
        ("trailing_clean_sheet_rate", rate([1.0 if value == 0 else 0.0 for value in goals_against])),
        ("trailing_multi_concede_rate", rate([1.0 if value >= 2 else 0.0 for value in goals_against])),
        ("trailing_penalties_against_per_match", rate(penalties_against)),
        ("trailing_goal_differential", rate(differentials)),
        ("trailing_extra_time_rate", rate(extra_times)),
        ("trailing_penalty_shootout_rate", rate(shootouts)),
        ("trailing_replay_rate", rate(replays)),
        ("days_since_previous_match", days_since_previous_match),
        ("trailing_yellow_cards_per_match", rate(yellows)),
        ("trailing_red_cards_per_match", rate(reds)),
        ("trailing_second_yellow_rate", rate([1.0 if value > 0 else 0.0 for value in second_yellows])),
        ("trailing_sending_off_rate", rate([1.0 if value > 0 else 0.0 for value in sending_offs])),
        ("confederation_code", profile.get("confederation_code", "")),
        ("region_name", profile.get("region_name", "")),
        ("mens_team", bool01(profile.get("mens_team", "0"))),
        ("womens_team", bool01(profile.get("womens_team", "0"))),
        ("prior_world_cup_matches", len(prior)),
        ("prior_best_finish", prior_best_finish),
        ("trailing_win_rate", rate(wins)),
        ("trailing_draw_rate", rate(draws)),
        ("trailing_loss_rate", rate(losses)),
        ("trailing_points_per_match", rate([3.0 * win + draw for win, draw in zip(wins, draws)])),
        ("trailing_form_goal_diff", rate(differentials[-5:])),
        ("trailing_unbeaten_rate", rate([1.0 if win or draw else 0.0 for win, draw in zip(wins, draws)])),
        ("match_day_of_tournament", (match_date - tournament_start).days),
        ("kickoff_hour", row.get("match_time", "00:00")[:2]),
        ("host_country", host_country),
        ("stadium_capacity", capacities.get(row.get("stadium_id", ""), "0")),
    ]


def player_rows(raw_root: Path) -> list[dict[str, Any]]:
    path = raw_root / "fjelstul" / "data-csv" / "players.csv"
    rows = read_csv_required(path, {"key_id", "player_id", "family_name", "given_name", "female", "goal_keeper", "defender", "midfielder", "forward", "count_tournaments"})
    source_key = sha256_file(path)
    out = []
    for row in sorted(rows, key=lambda r: r["key_id"]):
        out.append(
            {
                "text": stat_line(
                    [
                        ("entity", "player"),
                        ("player_id", row["player_id"]),
                        ("family_name", row["family_name"]),
                        ("given_name", row["given_name"]),
                        ("female", bool01(row["female"])),
                        ("goal_keeper", bool01(row["goal_keeper"])),
                        ("defender", bool01(row["defender"])),
                        ("midfielder", bool01(row["midfielder"])),
                        ("forward", bool01(row["forward"])),
                        ("count_tournaments", int_token(row, "count_tournaments")),
                    ]
                ),
                "metadata": metadata("player", "fjelstul.players", source_key, row) | {"player_id": row["player_id"]},
            }
        )
    return out


def match_rows(raw_root: Path) -> list[dict[str, Any]]:
    path = raw_root / "fjelstul" / "data-csv" / "matches.csv"
    rows = read_csv_required(path, {"key_id", "tournament_id", "match_id", "stage_name", "group_stage", "knockout_stage", "match_date", "stadium_id", "city_name", "country_name", "home_team_id", "away_team_id", "result"})
    source_key = sha256_file(path)
    out = []
    for row in sorted(rows, key=lambda r: r["key_id"]):
        anchor = match_result_anchor(row)
        out.append(
            {
                "text": stat_line(
                    [
                        ("entity", "match"),
                        ("tournament_id", row["tournament_id"]),
                        ("match_id", row["match_id"]),
                        ("stage", row["stage_name"]),
                        ("group", row.get("group_name", "")),
                        ("group_stage", bool01(row["group_stage"])),
                        ("knockout_stage", bool01(row["knockout_stage"])),
                        ("date", row["match_date"]),
                        ("stadium_id", row["stadium_id"]),
                        ("city", row["city_name"]),
                        ("country", row["country_name"]),
                        ("home_team_id", row["home_team_id"]),
                        ("away_team_id", row["away_team_id"]),
                    ]
                ),
                "metadata": metadata("match", "fjelstul.matches", source_key, row)
                | {"match_id": row["match_id"], "tournament_id": row["tournament_id"]},
                "anchors": [anchor],
                "oracle": oracle_event(
                    "soccer_lab.match_result",
                    "predict_match_result",
                    anchor,
                    non_negative_date_t_secs(row["match_date"]),
                ),
            }
        )
    return out


def teams_history_rows(raw_root: Path) -> list[dict[str, Any]]:
    path = raw_root / "fjelstul" / "data-csv" / "team_appearances.csv"
    rows = read_csv_required(path, {"key_id", "tournament_id", "match_id", "stage_name", "group_name", "group_stage", "knockout_stage", "match_date", "match_time", "stadium_id", "country_name", "team_id", "opponent_id", "home_team", "away_team", "goals_for", "goals_against", "goal_differential", "extra_time", "penalty_shootout", "penalties_for", "penalties_against", "win", "lose", "draw", "result"})
    source_key = sha256_file(path)
    bookings = booking_counts(raw_root)
    profiles = team_profiles(raw_root)
    tournaments = tournament_context(raw_root)
    capacities = stadium_capacity(raw_root)
    positions = tournament_positions(raw_root)
    history: dict[str, list[dict[str, Any]]] = {}
    out = []
    for row in sorted(rows, key=match_sort_key):
        prior = history.setdefault(row["team_id"], [])
        anchor = team_result_anchor(row)
        out.append(
            {
                "text": stat_line(
                    [
                        ("entity", "team_match_history"),
                        ("tournament_id", row["tournament_id"]),
                        ("match_id", row["match_id"]),
                        ("stage_name", row["stage_name"]),
                        ("group_name", row.get("group_name", "")),
                        ("group_stage", bool01(row["group_stage"])),
                        ("knockout_stage", bool01(row["knockout_stage"])),
                        ("date", row["match_date"]),
                        ("team_id", row["team_id"]),
                        ("opponent_id", row["opponent_id"]),
                        ("home_team", bool01(row["home_team"])),
                        ("away_team", bool01(row["away_team"])),
                        *team_prior_stats(prior, row, profiles, tournaments, capacities),
                    ]
                ),
                "metadata": metadata("team_match_history", "fjelstul.team_appearances", source_key, row)
                | {"match_id": row["match_id"], "team_id": row["team_id"], "tournament_id": row["tournament_id"]},
                "anchors": [anchor],
                "oracle": oracle_event(
                    "soccer_lab.team_match_result",
                    "predict_team_match_result",
                    anchor,
                    non_negative_date_t_secs(row["match_date"]),
                ),
            }
        )
        cards = bookings.get((row["match_id"], row["team_id"]), {})
        prior.append(
            {
                "date": date.fromisoformat(row["match_date"]),
                "goals_for": num(row, "goals_for"),
                "goals_against": num(row, "goals_against"),
                "goal_differential": num(row, "goal_differential"),
                "penalties_for": num(row, "penalties_for"),
                "penalties_against": num(row, "penalties_against"),
                "extra_time": num(row, "extra_time"),
                "penalty_shootout": num(row, "penalty_shootout"),
                "replay": num(row, "replay"),
                "win": num(row, "win"),
                "lose": num(row, "lose"),
                "draw": num(row, "draw"),
                "yellow": cards.get("yellow", 0.0),
                "red": cards.get("red", 0.0),
                "second_yellow": cards.get("second_yellow", 0.0),
                "sending_off": cards.get("sending_off", 0.0),
                "position": positions.get((row["tournament_id"], row["team_id"])),
            }
        )
    return out


def fjelstul_rows(raw_root: Path) -> list[dict[str, Any]]:
    base = raw_root / "fjelstul" / "data-csv"
    files = sorted(base.glob("*.csv"))
    if not files:
        raise RowGenError("fjelstul", "missing_fjelstul_csv_files", {"dir": str(base)})
    out = []
    for path in files:
        rows = read_csv_required(path, set())
        source_key = sha256_file(path)
        dataset = path.stem
        for idx, row in enumerate(rows):
            pairs = [("entity", "fjelstul_raw"), ("dataset", dataset)]
            for key in sorted(row):
                pairs.append((key, row[key]))
            out.append(
                {
                    "text": stat_line(pairs),
                    "metadata": metadata("fjelstul_raw", f"fjelstul.{dataset}", source_key, row)
                    | {"dataset": dataset, "source_row_index": str(idx)},
                }
            )
    return out


def fixture_rows(raw_root: Path) -> list[dict[str, Any]]:
    path = raw_root / "thestatsapi" / "wc2026_matches.json"
    payload = read_json_required(path)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        raise RowGenError("fixtures", "missing_fixture_data_array", {"path": str(path)})
    source_key = sha256_file(path)
    out = []
    for idx, row in enumerate(data):
        if not isinstance(row, dict):
            raise RowGenError("fixtures", "fixture_row_not_object", {"index": idx})
        home = row.get("home_team") or row.get("home") or {}
        away = row.get("away_team") or row.get("away") or {}
        out.append(
            {
                "text": stat_line(
                    [
                        ("entity", "fixture"),
                        ("match_id", row.get("id", "")),
                        ("match_number", row.get("match_number", "")),
                        ("competition_id", row.get("competition_id", "")),
                        ("season_id", row.get("season_id", "")),
                        ("stage", row.get("stage") or row.get("stage_name", "")),
                        ("group", row.get("group", "")),
                        ("kickoff_utc", row.get("kickoff_utc") or row.get("utc_date", "")),
                        ("home_team_id", home.get("id", "") if isinstance(home, dict) else ""),
                        ("away_team_id", away.get("id", "") if isinstance(away, dict) else ""),
                    ]
                ),
                "metadata": {
                    "project": "Soccer Lab",
                    "entity": "fixture",
                    "source_dataset": "thestatsapi.wc2026_matches",
                    "source": "TheStatsAPI",
                    "source_key": source_key,
                    "source_row_index": str(idx),
                    "match_id": str(row.get("id", "")),
                },
            }
        )
    return out


GENERATORS: dict[str, tuple[str, Callable[[Path], list[dict[str, Any]]]]] = {
    "players": ("players.jsonl", player_rows),
    "players-2026": ("players-2026.jsonl", player_2026_rows),
    "matches": ("matches.jsonl", match_rows),
    "matches-2026": ("matches-2026.jsonl", match_2026_rows),
    "teams-history": ("teams-history.jsonl", teams_history_rows),
    "team-tournaments": ("team-tournaments.jsonl", team_tournament_rows),
    "fjelstul": ("fjelstul.jsonl", fjelstul_rows),
    "fixtures": ("fixtures.jsonl", fixture_rows),
}


def write_jsonl_checked(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise RowGenError("write_jsonl", "no_rows_to_write", {"path": str(path)})
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = b"".join(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n" for row in rows)
    path.write_bytes(encoded)
    observed = path.read_bytes()
    if observed != encoded:
        raise RowGenError("write_jsonl", "readback_mismatch", {"path": str(path), "expected_sha256": sha256_bytes(encoded), "observed_sha256": sha256_bytes(observed)})
    return {"path": str(path.relative_to(ROOT)), "rows": len(rows), "bytes": len(observed), "sha256": sha256_bytes(observed)}


def validate_jsonl(path: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    with path.open("rb") as fh:
        for index, raw in enumerate(fh):
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RowGenError("validate_jsonl", "invalid_jsonl", {"path": str(path), "line": index + 1}) from exc
            if not isinstance(row.get("text"), str) or not row["text"].strip():
                raise RowGenError("validate_jsonl", "missing_text", {"path": str(path), "line": index + 1})
            if not isinstance(row.get("metadata"), dict):
                raise RowGenError("validate_jsonl", "missing_metadata", {"path": str(path), "line": index + 1})
            counts[row["metadata"].get("entity", "unknown")] += 1
    return counts


def run(args: argparse.Namespace) -> int:
    raw_root = (ROOT / args.raw_root).resolve() if not Path(args.raw_root).is_absolute() else Path(args.raw_root).resolve()
    out_root = (ROOT / args.out).resolve() if not Path(args.out).is_absolute() else Path(args.out).resolve()
    if not str(raw_root).startswith(str(ROOT.resolve())) or not str(out_root).startswith(str(ROOT.resolve())):
        raise RowGenError("config", "path_outside_repo", {"raw_root": str(raw_root), "out": str(out_root)})
    selected = args.only or list(GENERATORS)
    log_path = out_root / "generate.log.jsonl"
    log_event(log_path, {"event": "start", "raw_root": str(raw_root.relative_to(ROOT)), "outputs": selected})
    manifest_files = []
    staging_root = out_root / f".staging-{os.getpid()}"
    if staging_root.exists():
        shutil.rmtree(staging_root)
    try:
        for name in selected:
            filename, generator = GENERATORS[name]
            rows = generator(raw_root)
            record = write_jsonl_checked(staging_root / filename, rows)
            record["path"] = str((out_root / filename).relative_to(ROOT))
            record["output"] = name
            record["entity_counts"] = dict(validate_jsonl(staging_root / filename))
            manifest_files.append(record)
        manifest = {
            "schema_version": 1,
            "project": "Soccer Lab",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "raw_root": str(raw_root.relative_to(ROOT)),
            "files": manifest_files,
        }
        manifest_record = write_jsonl_checked(staging_root / "generation_manifest.jsonl", [{"metadata": {"entity": "manifest"}, "text": json.dumps(manifest, sort_keys=True)}])
        manifest_record["path"] = str((out_root / "generation_manifest.jsonl").relative_to(ROOT))
        out_root.mkdir(parents=True, exist_ok=True)
        for record in manifest_files:
            staged = staging_root / Path(record["path"]).name
            target = ROOT / record["path"]
            staged.replace(target)
        (staging_root / "generation_manifest.jsonl").replace(out_root / "generation_manifest.jsonl")
        shutil.rmtree(staging_root)
        log_event(log_path, {"event": "complete", "files": manifest_files, "manifest": manifest_record})
    except RowGenError as exc:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        log_event(log_path, {"event": "error", "stage": exc.stage, "reason": exc.reason, **exc.detail})
        raise
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW.relative_to(ROOT)), help="raw source directory")
    parser.add_argument("--out", default=str(DEFAULT_OUT.relative_to(ROOT)), help="output directory")
    parser.add_argument("--only", action="append", choices=sorted(GENERATORS), help="generate one or more outputs")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except RowGenError as exc:
        print(json.dumps({"status": "error", "stage": exc.stage, "reason": exc.reason, **exc.detail}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
