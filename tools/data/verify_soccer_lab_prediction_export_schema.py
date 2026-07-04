#!/usr/bin/env python3
"""Verify Soccer Lab stable prediction export schema and provenance."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

import verify_soccer_lab_oracle_context_ingest as oracle_context


ROOT = oracle_context.ROOT
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "prediction_export_schema" / "report.json"
DEFAULT_SCHEMA_OUT = ROOT / "docs" / "data" / "soccer_lab_prediction_record_schema.json"
DEFAULT_EXPORT_OUT = ROOT / "docs" / "data" / "soccer_lab_prediction_export.json"

SOURCE_FILES = {
    "match": ROOT / "docs" / "data" / "soccer_lab_match_predictions.json",
    "tournament_progression": ROOT / "docs" / "data" / "soccer_lab_tournament_progression_predictions.json",
    "player_impact": ROOT / "docs" / "data" / "soccer_lab_player_impact_predictions.json",
}
RUN_DATE = "2026-07-04"


class PredictionExportSchemaError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def require(condition: bool, reason: str, detail: dict[str, Any] | None = None) -> None:
    if not condition:
        raise PredictionExportSchemaError(reason, detail)


def write_json(path: Path, payload: Any) -> dict[str, Any]:
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded + "\n", encoding="utf-8")
    require(path.read_text(encoding="utf-8") == encoded + "\n", "json_readback_mismatch", {"path": str(path.relative_to(ROOT))})
    return oracle_context.file_stat(path)


def sha256_json(payload: Any) -> str:
    return oracle_context.sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def source_report_stat(provenance: dict[str, Any]) -> dict[str, Any]:
    report = provenance.get("source_report")
    require(isinstance(report, str) and report, "source_report_missing", {"provenance": provenance})
    path = ROOT / report
    require(path.is_file(), "source_report_file_missing", {"path": report})
    return oracle_context.file_stat(path)


def canonical_record_id(kind: str, source_record: dict[str, Any]) -> str:
    stable = {
        "kind": kind,
        "domain": source_record["domain"],
        "action_id": source_record["action_id"],
        "source_row_index": source_record.get("source_row_index"),
        "match_id": source_record.get("match_id"),
        "team": source_record.get("team"),
        "axis": source_record.get("axis"),
        "player_id": source_record.get("player_id"),
        "oracle_fixture_sha256": source_record["provenance"]["oracle_fixture_sha256"],
    }
    return f"slpred_{sha256_json(stable)[:24]}"


def confidence_caps(source_record: dict[str, Any]) -> dict[str, Any]:
    caps = source_record.get("confidence_caps")
    require(isinstance(caps, dict), "confidence_caps_missing", {"record": source_record})
    require(isinstance(caps.get("sufficient"), bool), "confidence_caps_sufficient_invalid", {"caps": caps})
    dpi = caps.get("dpi_ceiling")
    require(isinstance(dpi, (int, float)) and dpi >= 0, "confidence_caps_dpi_invalid", {"caps": caps})
    return {"dpi_ceiling": float(dpi), "sufficient": caps["sufficient"]}


def prediction_object(source_record: dict[str, Any]) -> dict[str, Any]:
    status = source_record.get("prediction_status")
    prediction = source_record.get("prediction")
    confidence = source_record.get("confidence")
    require(status in {"oracle_insufficient", "oracle_predicted"}, "prediction_status_invalid", {"status": status})
    require(isinstance(confidence, (int, float)) and 0.0 <= float(confidence) <= 1.0, "confidence_invalid", {"confidence": confidence})
    if status == "oracle_insufficient":
        require(prediction is None, "insufficient_record_has_prediction", {"record": source_record})
    else:
        require(prediction is not None, "predicted_record_missing_prediction", {"record": source_record})
    return {
        "status": status,
        "value": prediction,
        "confidence": float(confidence),
        "confidence_caps": confidence_caps(source_record),
    }


def provenance_object(kind: str, source_file: Path, source_file_stat: dict[str, Any], source_record: dict[str, Any]) -> dict[str, Any]:
    provenance = source_record.get("provenance")
    require(isinstance(provenance, dict), "provenance_missing", {"record": source_record})
    for key in ["oracle_stdout_sha256", "oracle_fixture_sha256", "source_report"]:
        require(isinstance(provenance.get(key), str) and provenance[key], "provenance_key_missing", {"key": key, "provenance": provenance})
    if source_record["prediction_status"] == "oracle_insufficient":
        require(provenance.get("oracle_error_code") == "CALYX_ORACLE_INSUFFICIENT", "insufficient_error_code_mismatch", {"provenance": provenance})
    report_stat = source_report_stat(provenance)
    return {
        "source_prediction_file": str(source_file.relative_to(ROOT)),
        "source_prediction_file_sha256": source_file_stat["sha256"],
        "source_record_kind": kind,
        "source_report": provenance["source_report"],
        "source_report_sha256": report_stat["sha256"],
        "oracle_error_code": provenance.get("oracle_error_code"),
        "oracle_stdout_sha256": provenance["oracle_stdout_sha256"],
        "oracle_fixture_sha256": provenance["oracle_fixture_sha256"],
        "oracle_ledger_ref": None,
        "oracle_ledger_key_hex": None,
    }


def match_input(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_type": "match",
        "entity_id": record["match_id"],
        "display": f"{record['home_team']} vs {record['away_team']}",
        "source": {
            "dataset": record["source"],
            "row_index": record["source_row_index"],
        },
        "attributes": {
            "date": record["date"],
            "start_time": record["start_time"],
            "round": record["round"],
            "venue": record["venue"],
            "home_team": record["home_team"],
            "away_team": record["away_team"],
            "score_columns_ignored": record["score_columns_ignored"],
            "unplayed_reason": record["unplayed_reason"],
        },
    }


def tournament_input(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_type": "team_tournament",
        "entity_id": f"{record['version']}:{record['team']}:{record['axis']}",
        "display": f"{record['team']} {record['axis']}",
        "source": {
            "dataset": "harrachimustapha/fifa-world-cup-team-dataset test.csv",
            "row_index": record["source_row_index"],
        },
        "attributes": {
            "version": record["version"],
            "team": record["team"],
            "continent": record["continent"],
            "axis": record["axis"],
        },
    }


def player_input(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_type": "player",
        "entity_id": record["player_id"],
        "display": record["player_name"],
        "source": {
            "dataset": "mominullptr/fifa-world-cup-2026-dataset squads_and_players.csv",
            "row_index": record["source_row_index"],
        },
        "attributes": {
            "player_id": record["player_id"],
            "player_name": record["player_name"],
            "team_id": record["team_id"],
            "team_name": record["team_name"],
            "position": record["position"],
            "prior_caps": record["prior_caps"],
            "prior_goals": record["prior_goals"],
        },
    }


def normalize_record(kind: str, source_file: Path, source_file_stat: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    if kind == "match":
        input_payload = match_input(record)
    elif kind == "tournament_progression":
        input_payload = tournament_input(record)
    elif kind == "player_impact":
        input_payload = player_input(record)
    else:
        raise PredictionExportSchemaError("unknown_record_kind", {"kind": kind})
    out = {
        "schema_version": 1,
        "record_id": canonical_record_id(kind, record),
        "record_type": kind,
        "generated_at": RUN_DATE,
        "domain": record["domain"],
        "action_id": record["action_id"],
        "input": input_payload,
        "input_hash": f"sha256:{sha256_json(input_payload)}",
        "prediction": prediction_object(record),
        "provenance": provenance_object(kind, source_file, source_file_stat, record),
    }
    validate_record(out)
    return out


def record_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://calyx.local/schemas/soccer_lab_prediction_record.v1.json",
        "title": "Soccer Lab prediction record",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "record_id",
            "record_type",
            "generated_at",
            "domain",
            "action_id",
            "input",
            "input_hash",
            "prediction",
            "provenance",
        ],
        "properties": {
            "schema_version": {"const": 1},
            "record_id": {"type": "string", "pattern": "^slpred_[0-9a-f]{24}$"},
            "record_type": {"enum": ["match", "tournament_progression", "player_impact"]},
            "generated_at": {"type": "string"},
            "domain": {"type": "string", "minLength": 1},
            "action_id": {"type": "string", "minLength": 1},
            "input_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            "input": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entity_type", "entity_id", "display", "source", "attributes"],
                "properties": {
                    "entity_type": {"type": "string", "minLength": 1},
                    "entity_id": {"type": "string", "minLength": 1},
                    "display": {"type": "string", "minLength": 1},
                    "source": {"type": "object"},
                    "attributes": {"type": "object"},
                },
            },
            "prediction": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "value", "confidence", "confidence_caps"],
                "properties": {
                    "status": {"enum": ["oracle_insufficient", "oracle_predicted"]},
                    "value": {},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "confidence_caps": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["dpi_ceiling", "sufficient"],
                        "properties": {
                            "dpi_ceiling": {"type": "number", "minimum": 0.0},
                            "sufficient": {"type": "boolean"},
                        },
                    },
                },
            },
            "provenance": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source_prediction_file",
                    "source_prediction_file_sha256",
                    "source_record_kind",
                    "source_report",
                    "source_report_sha256",
                    "oracle_error_code",
                    "oracle_stdout_sha256",
                    "oracle_fixture_sha256",
                    "oracle_ledger_ref",
                    "oracle_ledger_key_hex",
                ],
                "properties": {
                    "source_prediction_file": {"type": "string", "minLength": 1},
                    "source_prediction_file_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "source_record_kind": {"enum": ["match", "tournament_progression", "player_impact"]},
                    "source_report": {"type": "string", "minLength": 1},
                    "source_report_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "oracle_error_code": {"type": ["string", "null"]},
                    "oracle_stdout_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "oracle_fixture_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "oracle_ledger_ref": {"type": ["object", "null"]},
                    "oracle_ledger_key_hex": {"type": ["string", "null"]},
                },
            },
        },
    }


def validate_record(record: dict[str, Any]) -> None:
    required = set(record_schema()["required"])
    require(required <= set(record), "record_required_missing", {"missing": sorted(required - set(record)), "record": record})
    require(set(record) == required, "record_unexpected_keys", {"extra": sorted(set(record) - required), "record": record})
    require(record["schema_version"] == 1, "record_schema_version_invalid", {"record": record})
    require(record["record_id"].startswith("slpred_") and len(record["record_id"]) == 31, "record_id_invalid", {"record": record})
    require(record["record_type"] in SOURCE_FILES, "record_type_invalid", {"record": record})
    require(isinstance(record["domain"], str) and record["domain"], "record_domain_invalid", {"record": record})
    require(isinstance(record["action_id"], str) and record["action_id"], "record_action_invalid", {"record": record})
    require(record["input_hash"] == f"sha256:{sha256_json(record['input'])}", "record_input_hash_mismatch", {"record": record})
    prediction = record["prediction"]
    require(prediction["status"] in {"oracle_insufficient", "oracle_predicted"}, "record_prediction_status_invalid", {"record": record})
    require(0.0 <= prediction["confidence"] <= 1.0, "record_confidence_invalid", {"record": record})
    if prediction["status"] == "oracle_insufficient":
        require(prediction["value"] is None, "record_insufficient_has_value", {"record": record})
        require(record["provenance"]["oracle_error_code"] == "CALYX_ORACLE_INSUFFICIENT", "record_error_code_invalid", {"record": record})
        require(record["provenance"]["oracle_ledger_ref"] is None, "record_insufficient_has_ledger", {"record": record})
    caps = prediction["confidence_caps"]
    require(caps["dpi_ceiling"] >= 0.0 and isinstance(caps["sufficient"], bool), "record_caps_invalid", {"record": record})
    for key in ["source_prediction_file_sha256", "source_report_sha256", "oracle_stdout_sha256", "oracle_fixture_sha256"]:
        value = record["provenance"][key]
        require(isinstance(value, str) and len(value) == 64, "record_hash_invalid", {"key": key, "value": value})


def build_export() -> dict[str, Any]:
    records = []
    sources = {}
    for kind, path in SOURCE_FILES.items():
        payload = load_json(path)
        source_stat = oracle_context.file_stat(path)
        source_records = payload.get("records")
        require(isinstance(source_records, list), "source_records_missing", {"kind": kind, "path": str(path.relative_to(ROOT))})
        sources[kind] = {
            "file": source_stat,
            "schema_version": payload.get("schema_version"),
            "generated_at": payload.get("generated_at"),
            "records": len(source_records),
        }
        for source_record in source_records:
            records.append(normalize_record(kind, path, source_stat, source_record))
    record_ids = [record["record_id"] for record in records]
    require(len(record_ids) == len(set(record_ids)), "record_id_collision", {"duplicates": len(record_ids) - len(set(record_ids))})
    counts: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for record in records:
        counts[record["record_type"]] = counts.get(record["record_type"], 0) + 1
        statuses[record["prediction"]["status"]] = statuses.get(record["prediction"]["status"], 0) + 1
    return {
        "schema_version": 1,
        "generated_at": RUN_DATE,
        "export_id": f"slexport_{sha256_json(record_ids)[:24]}",
        "record_schema": "docs/data/soccer_lab_prediction_record_schema.json",
        "source_files": sources,
        "record_counts": dict(sorted(counts.items())),
        "status_counts": dict(sorted(statuses.items())),
        "records": records,
    }


def validate_export(export: dict[str, Any], schema_stat: dict[str, Any] | None = None) -> None:
    for key in ["schema_version", "generated_at", "export_id", "record_schema", "source_files", "record_counts", "status_counts", "records"]:
        require(key in export, "export_key_missing", {"key": key})
    require(export["schema_version"] == 1, "export_schema_version_invalid", {"schema_version": export["schema_version"]})
    require(export["export_id"].startswith("slexport_"), "export_id_invalid", {"export_id": export["export_id"]})
    require(sum(export["record_counts"].values()) == len(export["records"]), "export_count_mismatch", {"counts": export["record_counts"], "records": len(export["records"])})
    require(sum(export["status_counts"].values()) == len(export["records"]), "export_status_count_mismatch", {"counts": export["status_counts"], "records": len(export["records"])})
    require(export["record_counts"] == {"match": 16, "player_impact": 1248, "tournament_progression": 144}, "export_expected_counts_mismatch", {"counts": export["record_counts"]})
    require(export["status_counts"] == {"oracle_insufficient": 1408}, "export_expected_status_mismatch", {"status_counts": export["status_counts"]})
    for record in export["records"]:
        validate_record(record)
    if schema_stat is not None:
        require(schema_stat["path"] == export["record_schema"], "export_schema_path_mismatch", {"schema_stat": schema_stat, "export_schema": export["record_schema"]})


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    good = {
        "schema_version": 1,
        "record_id": "slpred_" + "a" * 24,
        "record_type": "match",
        "generated_at": RUN_DATE,
        "domain": "synthetic.domain",
        "action_id": "synthetic_action",
        "input": {
            "entity_type": "match",
            "entity_id": "synthetic-match",
            "display": "A vs B",
            "source": {"dataset": "synthetic", "row_index": 0},
            "attributes": {"home_team": "A", "away_team": "B"},
        },
        "prediction": {
            "status": "oracle_insufficient",
            "value": None,
            "confidence": 0.0,
            "confidence_caps": {"dpi_ceiling": 0.0, "sufficient": False},
        },
        "provenance": {
            "source_prediction_file": "synthetic.json",
            "source_prediction_file_sha256": "0" * 64,
            "source_record_kind": "match",
            "source_report": "synthetic-report.json",
            "source_report_sha256": "1" * 64,
            "oracle_error_code": "CALYX_ORACLE_INSUFFICIENT",
            "oracle_stdout_sha256": "2" * 64,
            "oracle_fixture_sha256": "3" * 64,
            "oracle_ledger_ref": None,
            "oracle_ledger_key_hex": None,
        },
    }
    good["input_hash"] = f"sha256:{sha256_json(good['input'])}"
    validate_record(good)
    good_path = work_dir / "synthetic-good-record.json"
    good_stat = write_json(good_path, good)
    bad_cases = {
        "missing_record_id": lambda value: value.pop("record_id"),
        "bad_confidence": lambda value: value["prediction"].update({"confidence": 1.5}),
        "fabricated_insufficient": lambda value: value["prediction"].update({"value": {"enum": "A"}}),
        "bad_input_hash": lambda value: value.update({"input_hash": "sha256:" + "f" * 64}),
    }
    observed = {}
    for name, mutator in bad_cases.items():
        candidate = copy.deepcopy(good)
        mutator(candidate)
        before = sorted(path.name for path in work_dir.glob("synthetic-bad-*.json"))
        try:
            validate_record(candidate)
        except PredictionExportSchemaError as error:
            after = sorted(path.name for path in work_dir.glob("synthetic-bad-*.json"))
            require(before == after, "synthetic_bad_case_wrote_file", {"case": name, "before": before, "after": after})
            observed[name] = {"reason": error.reason, "detail_keys": sorted(error.detail)}
        else:
            raise PredictionExportSchemaError("synthetic_bad_case_passed", {"case": name})
    return {
        "happy_record": good_stat,
        "bad_cases": observed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT.relative_to(ROOT)))
    parser.add_argument("--schema-out", default=str(DEFAULT_SCHEMA_OUT.relative_to(ROOT)))
    parser.add_argument("--export-out", default=str(DEFAULT_EXPORT_OUT.relative_to(ROOT)))
    return parser.parse_args()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    report_path = resolve(args.out)
    schema_path = resolve(args.schema_out)
    export_path = resolve(args.export_out)
    work_dir = report_path.parent
    schema_stat = write_json(schema_path, record_schema())
    export = build_export()
    validate_export(export, schema_stat)
    export_stat = write_json(export_path, export)
    readback_export = load_json(export_path)
    validate_export(readback_export, schema_stat)
    synthetic = synthetic_edges(work_dir / "synthetic_edges")
    report = {
        "status": "ok",
        "schema_file": schema_stat,
        "export_file": export_stat,
        "source_files": export["source_files"],
        "record_counts": export["record_counts"],
        "status_counts": export["status_counts"],
        "sample_records": {
            "match": next(record for record in export["records"] if record["record_type"] == "match"),
            "tournament_progression": next(record for record in export["records"] if record["record_type"] == "tournament_progression"),
            "player_impact": next(record for record in export["records"] if record["record_type"] == "player_impact"),
        },
        "synthetic_edges": synthetic,
    }
    report_stat = write_json(report_path, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "report": str(report_path.relative_to(ROOT)),
                "report_sha256": report_stat["sha256"],
                "schema_file": schema_stat,
                "export_file": export_stat,
                "record_counts": export["record_counts"],
                "status_counts": export["status_counts"],
                "synthetic_edges": ["happy_record", *sorted(synthetic["bad_cases"])],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
