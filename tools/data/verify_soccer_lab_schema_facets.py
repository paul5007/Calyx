#!/usr/bin/env python3
"""FSV for Soccer Lab schema-to-facet documentation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "tools/data/generate_schema_facet_map.py"
DOC = ROOT / "docs/SOCCER_LAB_SCHEMA_FACETS.md"
MAP = ROOT / "docs/data/soccer_lab_column_facets.csv"
RAW_ROOT = ROOT / "scratchpad/wc2026/raw"
CODEBOOK = RAW_ROOT / "fjelstul/codebook/variables.csv"

REQUIRED_DATASETS = {
    "matches",
    "harrachimustapha.train",
    "harrachimustapha.test",
    "mominullptr.matches",
    "mominullptr.match_team_stats",
    "mominullptr.squads_and_players",
    "swaptr.matches",
    "openfootball.matches",
    "openfootball.matches.score",
    "openfootball.matches.goals1",
}

REQUIRED_CLASSIFICATIONS = {
    ("matches", "result"): ("outcome_anchor", "ex_post", "anchor_or_explanatory_only"),
    ("matches", "home_team_id"): ("context", "ex_ante", "predictive_allowed"),
    ("harrachimustapha.train", "winner"): ("outcome_anchor", "ex_post", "anchor_or_explanatory_only"),
    ("harrachimustapha.train", "fifa_rank_pre_tournament"): ("pedigree", "ex_ante", "predictive_allowed"),
    ("mominullptr.matches", "home_score"): ("outcome_anchor", "ex_post", "anchor_or_explanatory_only"),
    ("mominullptr.matches", "home_team_id"): ("context", "ex_ante", "predictive_allowed"),
    ("mominullptr.squads_and_players", "market_value_eur"): ("pedigree", "ex_ante", "predictive_allowed"),
    ("swaptr.matches", "home_possession"): ("event_outcome", "ex_post", "anchor_or_explanatory_only"),
    ("openfootball.matches", "score"): ("outcome_anchor", "ex_post", "anchor_or_explanatory_only"),
    ("openfootball.matches", "team1"): ("context", "ex_ante", "predictive_allowed"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fsv-root", required=True)
    args = parser.parse_args()
    root = resolve(args.fsv_root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    physical = physical_map(root)
    happy = synthetic_happy_path(root)
    edges = [
        {
            "case": "physical_map_verify",
            "expected": "status ok and physical source coverage",
            "observed": physical["verify"]["status"],
        },
        {
            "case": "synthetic_happy_path",
            "expected": "known codebook/archive/json classifications",
            "observed": happy["status"],
        },
        edge_missing_codebook(root),
        edge_missing_columns(root),
        edge_stale_mapping(root),
        edge_invalid_zip(root),
        edge_invalid_openfootball_json(root),
    ]
    readback = {
        "status": "ok",
        "surface": "soccer_lab.schema_facets",
        "source_of_truth": "physical codebook/archive/JSON schemas plus generated CSV map",
        "generator": file_readback(GENERATOR),
        "doc": file_readback(DOC),
        "map": file_readback(MAP),
        "codebook": file_readback(CODEBOOK),
        "physical": physical,
        "synthetic_happy_path": happy,
        "edges": edges,
    }
    write_json(root / "schema-facets-readback.json", readback)
    write_manifest(
        root,
        [
            root / "schema-facets-readback.json",
            root / "physical" / "soccer_lab_column_facets.csv",
            root / "physical" / "schema_facet_map.log.jsonl",
            root / "happy" / "map.csv",
        ],
    )
    print(json.dumps(readback, indent=2, sort_keys=True))
    return 0


def physical_map(root: Path) -> dict[str, Any]:
    out = root / "physical" / "soccer_lab_column_facets.csv"
    write_proc = run_generator("write", CODEBOOK, RAW_ROOT, out)
    assert_success("physical write", write_proc)
    verify_proc = run_generator("verify", CODEBOOK, RAW_ROOT, out)
    assert_success("physical verify", verify_proc)
    committed_verify = run_generator("verify", CODEBOOK, RAW_ROOT, MAP)
    assert_success("committed verify", committed_verify)
    rows = read_csv(out)
    datasets = {row["dataset"] for row in rows}
    missing = sorted(REQUIRED_DATASETS - datasets)
    if missing:
        raise AssertionError(f"missing required datasets: {missing}")
    samples = sample_classifications(rows)
    counts = {
        "rows": len(rows),
        "datasets": len(datasets),
        "timing": count_by(rows, "timing"),
        "facet": count_by(rows, "facet"),
        "panel_use": count_by(rows, "panel_use"),
    }
    if counts["rows"] < 638 or counts["datasets"] < 46:
        raise AssertionError(f"unexpected low coverage: {counts}")
    return {
        "status": "ok",
        "write": parse_stdout(write_proc),
        "verify": parse_stdout(verify_proc),
        "committed_verify": parse_stdout(committed_verify),
        "generated_map": file_readback(out),
        "coverage": counts,
        "required_datasets": sorted(REQUIRED_DATASETS),
        "sample_classifications": samples,
        "archive_sources": archive_source_readbacks(),
    }


def synthetic_happy_path(root: Path) -> dict[str, Any]:
    raw = root / "happy-raw"
    codebook = raw / "fjelstul/codebook/variables.csv"
    write_text(
        codebook,
        "dataset,variable,type,description\n"
        "matches,result,enum,Match result label\n"
        "matches,home_team_id,text,Home team id\n",
    )
    write_zip_csv(
        raw / "harrachimustapha/fifa-world-cup-team-dataset.zip",
        "train.csv",
        ["team", "fifa_rank_pre_tournament", "winner"],
        [["Canada", "20", "0"], ["Mexico", "14", "1"]],
    )
    write_json(
        raw / "openfootball/2026/worldcup.json",
        {
            "name": "World Cup 2026",
            "matches": [
                {
                    "round": "Matchday 1",
                    "date": "2026-06-11",
                    "team1": "Canada",
                    "team2": "Mexico",
                    "score": {"ft": [1, 0]},
                    "goals1": [{"name": "A", "minute": "12"}],
                    "goals2": [],
                    "ground": "Toronto",
                    "group": "A",
                    "time": "13:00",
                }
            ],
        },
    )
    out = root / "happy" / "map.csv"
    proc = run_generator("write", codebook, raw, out)
    assert_success("synthetic write", proc)
    rows = read_csv(out)
    samples = sample_classifications(rows, required={
        ("matches", "result"): ("outcome_anchor", "ex_post", "anchor_or_explanatory_only"),
        ("harrachimustapha.train", "winner"): ("outcome_anchor", "ex_post", "anchor_or_explanatory_only"),
        ("harrachimustapha.train", "fifa_rank_pre_tournament"): ("pedigree", "ex_ante", "predictive_allowed"),
        ("openfootball.matches", "team1"): ("context", "ex_ante", "predictive_allowed"),
        ("openfootball.matches", "score"): ("outcome_anchor", "ex_post", "anchor_or_explanatory_only"),
    })
    return {
        "status": "ok",
        "map": file_readback(out),
        "rows": len(rows),
        "sample_classifications": samples,
    }


def edge_missing_codebook(root: Path) -> dict[str, Any]:
    proc = run_generator("write", root / "missing.csv", RAW_ROOT, root / "edges/missing.csv")
    return edge_result("missing_codebook", "missing_required_input", proc)


def edge_missing_columns(root: Path) -> dict[str, Any]:
    raw = root / "missing-columns-raw"
    codebook = raw / "fjelstul/codebook/variables.csv"
    write_text(codebook, "dataset,variable\nmatches,result\n")
    proc = run_generator("write", codebook, raw, root / "edges/missing-columns.csv")
    return edge_result("missing_required_columns", "missing_required_columns", proc)


def edge_stale_mapping(root: Path) -> dict[str, Any]:
    raw = root / "stale-raw"
    codebook = raw / "fjelstul/codebook/variables.csv"
    write_text(codebook, "dataset,variable,type,description\nmatches,result,enum,Result\n")
    out = root / "edges/stale.csv"
    assert_success("stale baseline", run_generator("write", codebook, raw, out))
    rows = read_csv(out)
    rows[0]["facet"] = "context"
    write_csv(out, rows)
    proc = run_generator("verify", codebook, raw, out)
    return edge_result("stale_mapping", "mapping_mismatch", proc)


def edge_invalid_zip(root: Path) -> dict[str, Any]:
    raw = root / "bad-zip-raw"
    codebook = raw / "fjelstul/codebook/variables.csv"
    write_text(codebook, "dataset,variable,type,description\nmatches,result,enum,Result\n")
    bad_zip = raw / "harrachimustapha/fifa-world-cup-team-dataset.zip"
    bad_zip.parent.mkdir(parents=True)
    bad_zip.write_bytes(b"not a zip")
    proc = run_generator("write", codebook, raw, root / "edges/bad-zip.csv")
    return edge_result("invalid_zip", "invalid_zip", proc)


def edge_invalid_openfootball_json(root: Path) -> dict[str, Any]:
    raw = root / "bad-json-raw"
    codebook = raw / "fjelstul/codebook/variables.csv"
    write_text(codebook, "dataset,variable,type,description\nmatches,result,enum,Result\n")
    path = raw / "openfootball/2026/worldcup.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    proc = run_generator("write", codebook, raw, root / "edges/bad-json.csv")
    return edge_result("invalid_openfootball_json", "invalid_json", proc)


def sample_classifications(rows: list[dict[str, str]], required: dict[tuple[str, str], tuple[str, str, str]] | None = None) -> dict[str, Any]:
    required = required or REQUIRED_CLASSIFICATIONS
    by_key = {(row["dataset"], row["column"]): row for row in rows}
    samples = {}
    for key, expected in required.items():
        row = by_key.get(key)
        if row is None:
            raise AssertionError(f"missing sample classification {key}")
        observed = (row["facet"], row["timing"], row["panel_use"])
        if observed != expected:
            raise AssertionError(f"{key} expected {expected}, got {observed}")
        samples[f"{key[0]}.{key[1]}"] = {
            "facet": row["facet"],
            "timing": row["timing"],
            "panel_use": row["panel_use"],
            "normalization": row["normalization"],
        }
    return samples


def archive_source_readbacks() -> dict[str, Any]:
    paths = [
        RAW_ROOT / "harrachimustapha/fifa-world-cup-team-dataset.zip",
        RAW_ROOT / "mominullptr/fifa-world-cup-2026-dataset.zip",
        RAW_ROOT / "swaptr/fifa-wc-2026-matches.zip",
        RAW_ROOT / "openfootball/2026/worldcup.json",
    ]
    return {str(path.relative_to(ROOT)): file_readback(path) for path in paths}


def run_generator(mode: str, codebook: Path, raw_root: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            mode,
            "--codebook",
            str(codebook),
            "--raw-root",
            str(raw_root),
            "--out",
            str(out),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_success(label: str, proc: subprocess.CompletedProcess[str]) -> None:
    if proc.returncode != 0:
        raise AssertionError(f"{label} failed: stdout={proc.stdout} stderr={proc.stderr}")


def edge_result(case: str, expected_reason: str, proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        observed = json.loads(proc.stderr.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        observed = {"raw_stderr": proc.stderr}
    if proc.returncode == 0:
        raise AssertionError(f"{case} unexpectedly succeeded: {proc.stdout}")
    if observed.get("reason") != expected_reason:
        raise AssertionError(f"{case} expected {expected_reason}, got {observed}")
    return {
        "case": case,
        "expected": expected_reason,
        "observed": observed.get("reason"),
        "stage": observed.get("stage"),
        "exit_code": proc.returncode,
    }


def parse_stdout(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(proc.stdout.strip().splitlines()[-1])


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        raise AssertionError("cannot write empty CSV")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_zip_csv(path: Path, member: str, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, buf.getvalue())


def count_by(rows: list[dict[str, str]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row[field]] = counts.get(row[field], 0) + 1
    return dict(sorted(counts.items()))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_readback(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def write_manifest(root: Path, files: list[Path]) -> None:
    lines = []
    for path in files:
        data = path.read_bytes()
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {path.relative_to(root)}")
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


if __name__ == "__main__":
    raise SystemExit(main())
