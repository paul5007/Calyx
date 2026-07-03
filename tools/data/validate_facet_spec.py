#!/usr/bin/env python3
"""Validate the Soccer Lab frozen facet spec against the column map."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC = ROOT / "docs" / "data" / "soccer_lab_facet_spec.json"
DEFAULT_MAP = ROOT / "docs" / "data" / "soccer_lab_column_facets.csv"
DEFAULT_LOG = ROOT / "docs" / "data" / "facet_spec_validation.log.jsonl"

REQUIRED_TEAM_MATCH_FACETS = {"attack", "defense", "tempo", "discipline", "pedigree", "form", "context"}
REQUIRED_PLAYER_FACETS = {"output", "profile", "efficiency"}
EX_POST_ALLOWED_POLICIES = {"prior_match_only", "static_or_prior_tournament_only"}


class SpecError(RuntimeError):
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


def repo_path(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def log_event(log_path: Path, event: dict[str, Any]) -> None:
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as fh:
        fh.write(json.dumps(event, sort_keys=True).encode("utf-8") + b"\n")


def load_json(path: Path) -> Any:
    if not path.exists():
        raise SpecError("load_spec", "missing_required_input", {"path": str(path)})
    data = path.read_bytes()
    if not data:
        raise SpecError("load_spec", "empty_required_input", {"path": str(path)})
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise SpecError("load_spec", "invalid_json", {"path": str(path), "sha256": sha256_bytes(data)}) from exc


def load_column_map(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        raise SpecError("load_column_map", "missing_required_input", {"path": str(path)})
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    required = {"dataset", "column", "timing", "facet"}
    if not rows:
        raise SpecError("load_column_map", "empty_column_map", {"path": str(path)})
    missing = sorted(required - set(rows[0]))
    if missing:
        raise SpecError("load_column_map", "missing_required_columns", {"path": str(path), "missing": missing})
    return {f"{row['dataset']}.{row['column']}": row for row in rows}


def dense_dim(shape: str) -> int:
    match = re.fullmatch(r"Dense\((\d+)\)", shape)
    if not match:
        raise SpecError("validate_spec", "invalid_shape", {"shape": shape})
    return int(match.group(1))


def validate(spec_path: Path, map_path: Path) -> dict[str, Any]:
    spec = load_json(spec_path)
    column_map = load_column_map(map_path)
    panels = spec.get("panels")
    if not isinstance(panels, list) or not panels:
        raise SpecError("validate_spec", "missing_panels")

    panel_facets: dict[str, set[str]] = {}
    total_facets = 0
    total_features = 0
    source_column_count = 0
    ex_post_sources = []

    for panel in panels:
        panel_name = panel.get("name")
        facets = panel.get("facets")
        if not isinstance(panel_name, str) or not isinstance(facets, list) or not facets:
            raise SpecError("validate_spec", "malformed_panel", {"panel": panel})
        panel_facets[panel_name] = set()
        for facet in facets:
            name = facet.get("name")
            if not isinstance(name, str) or not name:
                raise SpecError("validate_spec", "missing_facet_name", {"panel": panel_name})
            panel_facets[panel_name].add(name)
            total_facets += 1
            dim = dense_dim(str(facet.get("shape", "")))
            if facet.get("dense_dim") != dim:
                raise SpecError("validate_spec", "dense_dim_shape_mismatch", {"panel": panel_name, "facet": name, "shape": facet.get("shape"), "dense_dim": facet.get("dense_dim")})
            features = facet.get("features")
            if not isinstance(features, list) or not features:
                raise SpecError("validate_spec", "missing_features", {"panel": panel_name, "facet": name})
            if len(features) != dim:
                raise SpecError("validate_spec", "feature_dim_mismatch", {"panel": panel_name, "facet": name, "dense_dim": dim, "feature_count": len(features)})
            if facet.get("timing") != "ex_ante":
                raise SpecError("validate_spec", "predictive_facet_not_ex_ante", {"panel": panel_name, "facet": name, "timing": facet.get("timing")})
            source_columns = facet.get("source_columns")
            if not isinstance(source_columns, list) or not source_columns:
                raise SpecError("validate_spec", "missing_source_columns", {"panel": panel_name, "facet": name})
            for col in source_columns:
                source_column_count += 1
                if col not in column_map:
                    raise SpecError("validate_spec", "unknown_source_column", {"panel": panel_name, "facet": name, "source_column": col})
                timing = column_map[col]["timing"]
                if timing == "ex_post":
                    policy = facet.get("temporal_policy")
                    if policy not in EX_POST_ALLOWED_POLICIES:
                        raise SpecError("validate_spec", "ex_post_source_without_prior_policy", {"panel": panel_name, "facet": name, "source_column": col, "temporal_policy": policy})
                    ex_post_sources.append({"panel": panel_name, "facet": name, "source_column": col, "temporal_policy": policy})
            total_features += len(features)

    missing_team = sorted(REQUIRED_TEAM_MATCH_FACETS - panel_facets.get("teams_matches", set()))
    missing_players = sorted(REQUIRED_PLAYER_FACETS - panel_facets.get("players", set()))
    if missing_team or missing_players:
        raise SpecError("validate_spec", "missing_required_facets", {"teams_matches": missing_team, "players": missing_players})

    return {
        "spec": repo_path(spec_path),
        "spec_sha256": sha256_file(spec_path),
        "column_map": repo_path(map_path),
        "column_map_sha256": sha256_file(map_path),
        "panels": len(panels),
        "facets": total_facets,
        "features": total_features,
        "source_columns": source_column_count,
        "prior_policy_ex_post_sources": len(ex_post_sources),
        "status": "ok"
    }


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", default=repo_path(DEFAULT_SPEC))
    parser.add_argument("--column-map", default=repo_path(DEFAULT_MAP))
    parser.add_argument("--log", default=repo_path(DEFAULT_LOG))
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    spec_path = resolve(args.spec)
    map_path = resolve(args.column_map)
    log_path = resolve(args.log)
    try:
        log_event(log_path, {"event": "start", "spec": repo_path(spec_path), "column_map": repo_path(map_path)})
        result = validate(spec_path, map_path)
        log_event(log_path, {"event": "complete", **result})
        print(json.dumps(result, sort_keys=True))
        return 0
    except SpecError as exc:
        log_event(log_path, {"event": "error", "stage": exc.stage, "reason": exc.reason, **exc.detail})
        print(json.dumps({"status": "error", "stage": exc.stage, "reason": exc.reason, **exc.detail}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
