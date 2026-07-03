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
from collections import Counter
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


def int_token(row: dict[str, str], key: str, default: str = "0") -> str:
    value = row.get(key, "").strip()
    return value if value not in {"", "NA"} else default


def stat_line(pairs: list[tuple[str, Any]]) -> str:
    return " ".join(f"{key}={token(value)}" for key, value in pairs)


def metadata(entity: str, dataset: str, source_key: str, row: dict[str, str]) -> dict[str, str]:
    return {
        "project": "Soccer Lab",
        "entity": entity,
        "source_dataset": dataset,
        "source": FJELSTUL_SOURCE,
        "source_key": source_key,
        "source_row_key": row.get("key_id", ""),
    }


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
                "anchors": [match_result_anchor(row)],
            }
        )
    return out


def teams_history_rows(raw_root: Path) -> list[dict[str, Any]]:
    path = raw_root / "fjelstul" / "data-csv" / "team_appearances.csv"
    rows = read_csv_required(path, {"key_id", "tournament_id", "match_id", "stage_name", "group_stage", "knockout_stage", "match_date", "team_id", "opponent_id", "home_team", "away_team", "result"})
    source_key = sha256_file(path)
    out = []
    for row in sorted(rows, key=lambda r: r["key_id"]):
        out.append(
            {
                "text": stat_line(
                    [
                        ("entity", "team_match_history"),
                        ("tournament_id", row["tournament_id"]),
                        ("match_id", row["match_id"]),
                        ("stage", row["stage_name"]),
                        ("group", row.get("group_name", "")),
                        ("group_stage", bool01(row["group_stage"])),
                        ("knockout_stage", bool01(row["knockout_stage"])),
                        ("date", row["match_date"]),
                        ("team_id", row["team_id"]),
                        ("opponent_id", row["opponent_id"]),
                        ("home_team", bool01(row["home_team"])),
                        ("away_team", bool01(row["away_team"])),
                    ]
                ),
                "metadata": metadata("team_match_history", "fjelstul.team_appearances", source_key, row)
                | {"match_id": row["match_id"], "team_id": row["team_id"], "tournament_id": row["tournament_id"]},
                "anchors": [team_result_anchor(row)],
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
    "matches": ("matches.jsonl", match_rows),
    "teams-history": ("teams-history.jsonl", teams_history_rows),
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
