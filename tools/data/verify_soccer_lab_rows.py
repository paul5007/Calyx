#!/usr/bin/env python3
"""FSV for Soccer Lab CSV/JSON -> Calyx batch JSONL row generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "tools/data/generate_soccer_lab_rows.py"
RAW_ROOT = ROOT / "scratchpad/wc2026/raw"
ROWS_ROOT = ROOT / "scratchpad/wc2026/rows"

EXPECTED_OUTPUTS = {
    "players": "players.jsonl",
    "matches": "matches.jsonl",
    "teams-history": "teams-history.jsonl",
    "fjelstul": "fjelstul.jsonl",
    "fixtures": "fixtures.jsonl",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fsv-root", required=True)
    args = parser.parse_args()
    root = resolve(args.fsv_root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    physical = physical_row_readback()
    deterministic = deterministic_real_core(root)
    fixtures = fixture_known_input(root)
    edges = [
        {
            "case": "real_core_deterministic",
            "expected": "matching sha256 on rerun",
            "observed": deterministic["status"],
        },
        {
            "case": "fixture_known_input",
            "expected": "1 fixture row with known text fields",
            "observed": fixtures["status"],
        },
        edge_missing_raw(root),
        edge_bad_json(root),
        edge_path_outside_repo(root),
        edge_no_rows(root),
    ]
    readback = {
        "status": "ok",
        "surface": "soccer_lab.row_generation",
        "source_of_truth": "tools/data/generate_soccer_lab_rows.py plus physical JSONL bytes",
        "script": file_readback(GENERATOR),
        "expected_outputs": EXPECTED_OUTPUTS,
        "physical_rows": physical,
        "deterministic_real_core": deterministic,
        "fixture_known_input": fixtures,
        "edges": edges,
    }
    write_json(root / "rowgen-readback.json", readback)
    write_manifest(
        root,
        [
            root / "rowgen-readback.json",
            root / "real-run-a" / "generation_manifest.jsonl",
            root / "real-run-b" / "generation_manifest.jsonl",
            root / "fixtures-out" / "fixtures.jsonl",
        ],
    )
    print(json.dumps(readback, indent=2, sort_keys=True))
    return 0


def physical_row_readback() -> dict[str, Any]:
    files = {}
    for output, filename in EXPECTED_OUTPUTS.items():
        path = ROWS_ROOT / filename
        files[output] = jsonl_readback(path) if path.exists() else None
    return {
        "rows_root": str(ROWS_ROOT.relative_to(ROOT)),
        "files": files,
    }


def deterministic_real_core(root: Path) -> dict[str, Any]:
    outputs = ["players", "matches", "teams-history", "fjelstul"]
    run_a = root / "real-run-a"
    run_b = root / "real-run-b"
    run_generator(RAW_ROOT, run_a, outputs)
    run_generator(RAW_ROOT, run_b, outputs)
    comparisons = {}
    for output in outputs:
        filename = EXPECTED_OUTPUTS[output]
        a = file_readback(run_a / filename)
        b = file_readback(run_b / filename)
        if a["sha256"] != b["sha256"] or a["bytes"] != b["bytes"]:
            raise AssertionError(f"{output} is not deterministic: {a} != {b}")
        comparisons[output] = {"run_a": a, "run_b": b}
    return {
        "status": "ok",
        "outputs": outputs,
        "comparisons": comparisons,
        "manifest_a": file_readback(run_a / "generation_manifest.jsonl"),
        "manifest_b": file_readback(run_b / "generation_manifest.jsonl"),
    }


def fixture_known_input(root: Path) -> dict[str, Any]:
    raw = root / "fixtures-raw"
    payload = {
        "data": [
            {
                "id": "fixture-1",
                "match_number": 1,
                "competition_id": "comp_6107",
                "season_id": "sn_118868",
                "stage": "group",
                "group": "A",
                "kickoff_utc": "2026-06-11T00:00:00Z",
                "home_team": {"id": "CAN"},
                "away_team": {"id": "MEX"},
            }
        ]
    }
    source = raw / "thestatsapi/wc2026_matches.json"
    write_json(source, payload)
    out = root / "fixtures-out"
    run_generator(raw, out, ["fixtures"])
    rows = read_jsonl(out / "fixtures.jsonl")
    if len(rows) != 1:
        raise AssertionError(f"expected 1 fixture row, got {len(rows)}")
    text = rows[0]["text"]
    for expected in ["entity=fixture", "match_id=fixture-1", "home_team_id=CAN", "away_team_id=MEX"]:
        if expected not in text:
            raise AssertionError(f"fixture row missing {expected}: {text}")
    return {
        "status": "ok",
        "source": file_readback(source),
        "output": jsonl_readback(out / "fixtures.jsonl"),
        "row": rows[0],
    }


def edge_missing_raw(root: Path) -> dict[str, Any]:
    out = run_generator_expect_failure(root / "missing-raw", root / "missing-out", ["players"])
    return edge_result("missing_raw", "missing_required_input", out)


def edge_bad_json(root: Path) -> dict[str, Any]:
    raw = root / "bad-json-raw"
    path = raw / "thestatsapi/wc2026_matches.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    out = run_generator_expect_failure(raw, root / "bad-json-out", ["fixtures"])
    return edge_result("invalid_fixture_json", "invalid_json", out)


def edge_path_outside_repo(root: Path) -> dict[str, Any]:
    out = subprocess.run(
        [sys.executable, str(GENERATOR), "--raw-root", str(RAW_ROOT), "--out", "/tmp/calyx-rowgen-outside", "--only", "players"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return edge_result("path_outside_repo", "path_outside_repo", out)


def edge_no_rows(root: Path) -> dict[str, Any]:
    raw = root / "empty-fixtures-raw"
    write_json(raw / "thestatsapi/wc2026_matches.json", {"data": []})
    out = run_generator_expect_failure(raw, root / "empty-fixtures-out", ["fixtures"])
    return edge_result("empty_fixture_data", "missing_fixture_data_array", out)


def run_generator(raw: Path, out: Path, outputs: list[str]) -> None:
    command = [sys.executable, str(GENERATOR), "--raw-root", str(raw), "--out", str(out)]
    for output in outputs:
        command.extend(["--only", output])
    proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"generator failed: stdout={proc.stdout} stderr={proc.stderr}")


def run_generator_expect_failure(raw: Path, out: Path, outputs: list[str]) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(GENERATOR), "--raw-root", str(raw), "--out", str(out)]
    for output in outputs:
        command.extend(["--only", output])
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)


def edge_result(case: str, expected_reason: str, proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        observed = json.loads(proc.stderr.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        observed = {"raw_stderr": proc.stderr}
    return {
        "case": case,
        "expected": expected_reason,
        "observed": observed.get("reason"),
        "stage": observed.get("stage"),
        "exit_code": proc.returncode,
    }


def jsonl_readback(path: Path) -> dict[str, Any]:
    rows = read_jsonl(path)
    return file_readback(path) | {
        "rows": len(rows),
        "entity_counts": entity_counts(rows),
        "first_row_sha256": sha256_bytes(json.dumps(rows[0], sort_keys=True).encode("utf-8")) if rows else None,
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def entity_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        entity = row.get("metadata", {}).get("entity", "unknown")
        counts[entity] = counts.get(entity, 0) + 1
    return dict(sorted(counts.items()))


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
