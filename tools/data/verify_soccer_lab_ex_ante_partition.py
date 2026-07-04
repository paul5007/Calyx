#!/usr/bin/env python3
"""Verify Soccer Lab predictive rows cannot leak ex-post outcomes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = ROOT / "docs" / "data" / "soccer_lab_predictive_partition.json"
DEFAULT_SPEC = ROOT / "docs" / "data" / "soccer_lab_facet_spec.json"
DEFAULT_COLUMN_MAP = ROOT / "docs" / "data" / "soccer_lab_column_facets.csv"
DEFAULT_RAW = ROOT / "scratchpad" / "wc2026" / "raw"
GENERATOR = ROOT / "tools" / "data" / "generate_soccer_lab_rows.py"
PRIOR_ONLY_POLICIES = {"prior_match_only", "static_or_prior_tournament_only"}


class PartitionError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_stat(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def load_json(path: Path) -> Any:
    raw = path.read_bytes()
    if not raw:
        raise PartitionError("empty_json", {"path": str(path)})
    return json.loads(raw)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise PartitionError("empty_csv", {"path": str(path)})
    return rows


def parse_stat_line(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in text.split():
        if "=" not in token:
            raise PartitionError("malformed_stat_token", {"token": token})
        key, value = token.split("=", 1)
        if not key:
            raise PartitionError("empty_stat_key", {"token": token})
        fields[key] = value
    return fields


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            row = json.loads(line)
            if not isinstance(row.get("text"), str):
                raise PartitionError("missing_text", {"path": str(path), "line": line_no})
            if not isinstance(row.get("metadata"), dict):
                raise PartitionError("missing_metadata", {"path": str(path), "line": line_no})
            rows.append(row)
    if not rows:
        raise PartitionError("empty_jsonl", {"path": str(path)})
    return rows


def validate_policy_schema(policy: dict[str, Any]) -> dict[str, object]:
    entities = policy.get("entities")
    if not isinstance(entities, dict) or not entities:
        raise PartitionError("missing_entities")
    summary = {}
    for entity, rule in entities.items():
        for key in [
            "row_file",
            "predictive_panel",
            "predictive_facets",
            "anchor_axes",
            "anchor_required_in_current_rows",
            "allowed_text_keys",
            "allowed_text_key_prefixes",
            "forbidden_current_text_keys",
        ]:
            if key not in rule:
                raise PartitionError("partition_rule_missing_key", {"entity": entity, "key": key})
        if not set(rule["allowed_text_keys"]).isdisjoint(set(rule["forbidden_current_text_keys"])):
            raise PartitionError("allowed_forbidden_overlap", {"entity": entity})
        summary[entity] = {
            "predictive_facets": len(rule["predictive_facets"]),
            "forbidden_current_text_keys": len(rule["forbidden_current_text_keys"]),
            "anchor_required_in_current_rows": rule["anchor_required_in_current_rows"],
        }
    return summary


def validate_spec_partition(spec: dict[str, Any], policy: dict[str, Any], column_map_rows: list[dict[str, str]]) -> dict[str, object]:
    column_map = {f"{row['dataset']}.{row['column']}": row for row in column_map_rows}
    panels = {panel["name"]: panel for panel in spec.get("panels", [])}
    ex_post_source_count = 0
    predictive_facets = {}
    for entity, rule in policy["entities"].items():
        panel = panels.get(rule["predictive_panel"])
        if not panel:
            raise PartitionError("missing_predictive_panel", {"entity": entity, "panel": rule["predictive_panel"]})
        facets = {facet["name"]: facet for facet in panel.get("facets", [])}
        missing = sorted(set(rule["predictive_facets"]) - set(facets))
        if missing:
            raise PartitionError("missing_predictive_facets", {"entity": entity, "missing": missing})
        predictive_facets[entity] = sorted(rule["predictive_facets"])
        for name in rule["predictive_facets"]:
            facet = facets[name]
            if facet.get("timing") != "ex_ante":
                raise PartitionError("predictive_facet_not_ex_ante", {"entity": entity, "facet": name, "timing": facet.get("timing")})
            policy_name = facet.get("temporal_policy")
            for source in facet.get("source_columns", []):
                mapped = column_map.get(source)
                if not mapped:
                    raise PartitionError("unknown_source_column", {"entity": entity, "facet": name, "source_column": source})
                if mapped["timing"] == "ex_post":
                    ex_post_source_count += 1
                    if policy_name not in PRIOR_ONLY_POLICIES:
                        raise PartitionError(
                            "ex_post_source_not_shifted_to_prior",
                            {"entity": entity, "facet": name, "source_column": source, "temporal_policy": policy_name},
                        )
    return {
        "entities": sorted(policy["entities"]),
        "predictive_facets": predictive_facets,
        "ex_post_sources_shifted_to_prior_match_only": ex_post_source_count,
    }


def key_allowed(key: str, rule: dict[str, Any]) -> bool:
    return key in set(rule["allowed_text_keys"]) or any(key.startswith(prefix) for prefix in rule["allowed_text_key_prefixes"])


def validate_rows(rows_root: Path, policy: dict[str, Any]) -> dict[str, object]:
    result = {}
    for entity, rule in policy["entities"].items():
        path = rows_root / rule["row_file"]
        rows = load_jsonl(path)
        anchor_values: Counter[str] = Counter()
        seen_keys: Counter[str] = Counter()
        forbidden_hits = []
        disallowed_hits = []
        for index, row in enumerate(rows):
            fields = parse_stat_line(row["text"])
            if fields.get("entity") != entity:
                raise PartitionError("row_entity_mismatch", {"path": str(path), "line": index + 1, "entity": fields.get("entity"), "expected": entity})
            for key in fields:
                seen_keys[key] += 1
                if key in rule["forbidden_current_text_keys"]:
                    forbidden_hits.append({"line": index + 1, "key": key})
                if not key_allowed(key, rule):
                    disallowed_hits.append({"line": index + 1, "key": key})
            anchors = row.get("anchors") or []
            if rule["anchor_required_in_current_rows"]:
                if not isinstance(anchors, list) or not anchors:
                    raise PartitionError("missing_required_anchor", {"entity": entity, "line": index + 1})
                allowed_axes = set(rule["anchor_axes"])
                for anchor in anchors:
                    if anchor.get("kind") not in allowed_axes:
                        raise PartitionError("unexpected_anchor_axis", {"entity": entity, "line": index + 1, "anchor": anchor})
                    if not anchor.get("source") or not (0 < float(anchor.get("confidence", 0)) <= 1):
                        raise PartitionError("ungrounded_anchor", {"entity": entity, "line": index + 1, "anchor": anchor})
                    anchor_values[str(anchor.get("value"))] += 1
            elif anchors:
                raise PartitionError("unexpected_anchor_for_static_predictor_row", {"entity": entity, "line": index + 1})
        if forbidden_hits:
            raise PartitionError("forbidden_current_text_key_present", {"entity": entity, "examples": forbidden_hits[:5]})
        if disallowed_hits:
            raise PartitionError("disallowed_text_key_present", {"entity": entity, "examples": disallowed_hits[:5]})
        result[entity] = {
            "path": str(path.relative_to(ROOT)),
            "rows": len(rows),
            "text_key_count": len(seen_keys),
            "text_keys": sorted(seen_keys),
            "anchor_values": dict(sorted(anchor_values.items())),
            "file": file_stat(path),
        }
    return result


def expect_partition_error(name: str, func: Any, reason: str) -> dict[str, object]:
    try:
        func()
    except PartitionError as exc:
        if exc.reason != reason:
            raise PartitionError("unexpected_edge_reason", {"edge": name, "expected": reason, "observed": exc.reason, "detail": exc.detail}) from exc
        return {"edge": name, "reason": exc.reason, "status": "ok"}
    raise PartitionError("edge_case_unexpectedly_passed", {"edge": name, "expected": reason})


def write_rows(root: Path, filename: str, rows: list[dict[str, Any]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / filename).write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def verify_synthetic_edges(base_dir: Path, policy: dict[str, Any], spec: dict[str, Any], column_map_rows: list[dict[str, str]]) -> dict[str, object]:
    valid_match = {
        "text": "entity=match tournament_id=WC-1 match_id=M-1 stage=group group=Group_A group_stage=1 knockout_stage=0 date=2026-06-11 stadium_id=S-1 city=Toronto country=Canada home_team_id=T-1 away_team_id=T-2",
        "metadata": {"entity": "match"},
        "anchors": [{"kind": "label:match_result", "value": "home_win", "source": "synthetic-edge", "confidence": 1.0}],
    }
    valid_team = {
        "text": "entity=team_match_history tournament_id=WC-1 match_id=M-1 stage_name=group group_name=Group_A group_stage=1 knockout_stage=0 date=2026-06-11 team_id=T-1 opponent_id=T-2 home_team=1 away_team=0 trailing_goals_for_per_match=0 prior_world_cup_matches=0 days_since_previous_match=0 confederation_code=UEFA region_name=Europe mens_team=1 womens_team=0 match_day_of_tournament=0 kickoff_hour=12 host_country=0 stadium_capacity=50000",
        "metadata": {"entity": "team_match_history"},
        "anchors": [{"kind": "label:team_match_result", "value": "win", "source": "synthetic-edge", "confidence": 1.0}],
    }
    valid_player = {
        "text": "entity=player player_id=P-1 family_name=Known given_name=Input female=0 goal_keeper=0 defender=0 midfielder=1 forward=0 count_tournaments=1",
        "metadata": {"entity": "player"},
    }

    def edge_rows(row: dict[str, Any], entity: str, suffix: str) -> Path:
        root = base_dir / suffix
        for rule_entity, rule in policy["entities"].items():
            if rule_entity == entity:
                write_rows(root, rule["row_file"], [row])
            elif rule_entity == "match":
                write_rows(root, rule["row_file"], [valid_match])
            elif rule_entity == "team_match_history":
                write_rows(root, rule["row_file"], [valid_team])
            elif rule_entity == "player":
                write_rows(root, rule["row_file"], [valid_player])
        return root

    forbidden_row = dict(valid_team)
    forbidden_row["text"] = valid_team["text"] + " result=win"
    missing_anchor = dict(valid_match)
    missing_anchor["anchors"] = []
    ungrounded_anchor = dict(valid_match)
    ungrounded_anchor["anchors"] = [{"kind": "label:match_result", "value": "draw", "source": "", "confidence": 0.0}]

    bad_spec = json.loads(json.dumps(spec))
    bad_spec["panels"][0]["facets"][0]["temporal_policy"] = "current_fixture_allowed"

    return {
        "happy_path": {
            "rows": validate_rows(edge_rows(valid_match, "match", "synthetic_happy"), policy),
            "status": "ok",
        },
        "forbidden_current_text_key": expect_partition_error(
            "forbidden_current_text_key",
            lambda: validate_rows(edge_rows(forbidden_row, "team_match_history", "synthetic_forbidden_key"), policy),
            "forbidden_current_text_key_present",
        ),
        "missing_required_anchor": expect_partition_error(
            "missing_required_anchor",
            lambda: validate_rows(edge_rows(missing_anchor, "match", "synthetic_missing_anchor"), policy),
            "missing_required_anchor",
        ),
        "ungrounded_anchor": expect_partition_error(
            "ungrounded_anchor",
            lambda: validate_rows(edge_rows(ungrounded_anchor, "match", "synthetic_ungrounded_anchor"), policy),
            "ungrounded_anchor",
        ),
        "bad_spec_temporal_policy": expect_partition_error(
            "bad_spec_temporal_policy",
            lambda: validate_spec_partition(bad_spec, policy, column_map_rows),
            "ex_post_source_not_shifted_to_prior",
        ),
    }


def sort_key(row: dict[str, str]) -> tuple[str, int]:
    return row.get("match_date", ""), int(row.get("key_id", "0") or 0)


def num(row: dict[str, str], key: str) -> float:
    raw = row.get(key, "").strip()
    if raw in {"", "NA"}:
        return 0.0
    return float(raw)


def verify_team_history_prior_shift(raw_root: Path, rows_root: Path) -> dict[str, object]:
    raw_rows = read_csv(raw_root / "fjelstul" / "data-csv" / "team_appearances.csv")
    generated = load_jsonl(rows_root / "teams-history.jsonl")
    if len(raw_rows) != len(generated):
        raise PartitionError("team_history_row_count_mismatch", {"raw": len(raw_rows), "generated": len(generated)})

    history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    checked = 0
    nonzero_prior_rows = 0
    first_rows_checked = 0
    anchor_counts: Counter[str] = Counter()
    for raw, generated_row in zip(sorted(raw_rows, key=sort_key), generated):
        fields = parse_stat_line(generated_row["text"])
        team_id = raw["team_id"]
        prior = history[team_id]
        expected_prior_count = len(prior)
        observed_prior_count = int(float(fields["prior_world_cup_matches"]))
        if observed_prior_count != expected_prior_count:
            raise PartitionError(
                "prior_count_not_ex_ante",
                {"team_id": team_id, "match_id": raw["match_id"], "expected": expected_prior_count, "observed": observed_prior_count},
            )
        expected_goals_for = sum(entry["goals_for"] for entry in prior) / len(prior) if prior else 0.0
        observed_goals_for = float(fields["trailing_goals_for_per_match"])
        if abs(observed_goals_for - expected_goals_for) > 1e-12:
            raise PartitionError(
                "trailing_goals_not_prior_only",
                {
                    "team_id": team_id,
                    "match_id": raw["match_id"],
                    "expected": expected_goals_for,
                    "observed": observed_goals_for,
                },
            )
        current_goals_for = num(raw, "goals_for")
        if not prior and observed_goals_for != 0.0:
            raise PartitionError("first_match_has_current_goals_leak", {"team_id": team_id, "match_id": raw["match_id"]})
        if prior and abs(observed_goals_for - current_goals_for) > 1e-12:
            nonzero_prior_rows += 1
        if not prior:
            first_rows_checked += 1
        anchors = generated_row.get("anchors") or []
        for anchor in anchors:
            anchor_counts[str(anchor.get("value"))] += 1
        history[team_id].append(
            {
                "date": date.fromisoformat(raw["match_date"]),
                "goals_for": current_goals_for,
            }
        )
        checked += 1
    return {
        "checked_rows": checked,
        "teams_seen": len(history),
        "first_rows_checked": first_rows_checked,
        "rows_where_prior_mean_differs_from_current_goals_for": nonzero_prior_rows,
        "anchor_values": dict(sorted(anchor_counts.items())),
    }


def run_generator(out_root: Path) -> dict[str, object]:
    if out_root.exists():
        for child in out_root.iterdir():
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                import shutil

                shutil.rmtree(child)
    out_root.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(GENERATOR),
        "--out",
        str(out_root.relative_to(ROOT)),
        "--only",
        "players",
        "--only",
        "matches",
        "--only",
        "teams-history",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if proc.returncode != 0:
        raise PartitionError("row_generation_failed", {"cmd": cmd, "stderr": proc.stderr.decode("utf-8")})
    manifest = out_root / "generation_manifest.jsonl"
    return {
        "cmd": cmd,
        "stdout_bytes": len(proc.stdout),
        "stderr_bytes": len(proc.stderr),
        "manifest": file_stat(manifest),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default=str(DEFAULT_POLICY.relative_to(ROOT)))
    parser.add_argument("--spec", default=str(DEFAULT_SPEC.relative_to(ROOT)))
    parser.add_argument("--column-map", default=str(DEFAULT_COLUMN_MAP.relative_to(ROOT)))
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW.relative_to(ROOT)))
    parser.add_argument("--out", default="")
    return parser.parse_args()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    policy_path = resolve(args.policy)
    spec_path = resolve(args.spec)
    column_map_path = resolve(args.column_map)
    raw_root = resolve(args.raw_root)
    report_path = resolve(args.out) if args.out else ROOT / "scratchpad" / "wc2026" / "fsv" / "ex_ante_partition" / "report.json"
    rows_root = report_path.parent / "rows"

    policy = load_json(policy_path)
    spec = load_json(spec_path)
    column_map_rows = read_csv(column_map_path)
    generation = run_generator(rows_root)
    report = {
        "status": "ok",
        "policy_file": file_stat(policy_path),
        "spec_file": file_stat(spec_path),
        "column_map_file": file_stat(column_map_path),
        "policy_schema": validate_policy_schema(policy),
        "spec_partition": validate_spec_partition(spec, policy, column_map_rows),
        "generation": generation,
        "rows": validate_rows(rows_root, policy),
        "team_history_prior_shift": verify_team_history_prior_shift(raw_root, rows_root),
        "synthetic_edges": verify_synthetic_edges(report_path.parent / "synthetic_edges", policy, spec, column_map_rows),
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    if report_path.read_text(encoding="utf-8") != encoded + "\n":
        raise PartitionError("report_readback_mismatch", {"path": str(report_path)})
    print(
        json.dumps(
            {
                "status": "ok",
                "entities": sorted(report["rows"]),
                "team_history_rows": report["team_history_prior_shift"]["checked_rows"],
                "ex_post_sources_shifted": report["spec_partition"]["ex_post_sources_shifted_to_prior_match_only"],
                "synthetic_edges": sorted(report["synthetic_edges"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
