#!/usr/bin/env python3
"""FSV for the Soccer Lab tournament progression grounding-floor guard."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HARRACHI = ROOT / "scratchpad/wc2026/raw/harrachimustapha/fifa-world-cup-team-dataset.zip"
API = ROOT / "crates/calyx-web-api/src/prediction.rs"
API_TESTS = ROOT / "crates/calyx-web-api/tests/api.rs"
DASHBOARD_CLIENT = ROOT / "apps/soccer-lab-dashboard/src/liveApi.ts"
LIVE_API_VERIFY = ROOT / "apps/soccer-lab-dashboard/scripts/verify-live-api.mjs"
DOC = ROOT / "docs/SOCCER_LAB_ORACLE_CONTEXTS.md"

MIN_CLASS = 50
PLACEMENT_AXES = ["winner", "finalist", "semi_finalist", "quarter_finalist"]
API_AXES = ["winner", "finalist", "semi_finalist"]
EXPECTED_TRAIN_POSITIVES = {
    "winner": 6,
    "finalist": 12,
    "semi_finalist": 24,
    "quarter_finalist": 48,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = resolve(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    physical = physical_counts()
    guard = guard_wiring()
    synthetic = synthetic_cases()
    readback = {
        "status": "ok",
        "surface": "soccer_lab.progression_axis_guard",
        "min_class": MIN_CLASS,
        "source_of_truth": "Harrachi raw zip plus live API guard code",
        "physical_sources": {
            "harrachi_zip": file_readback(HARRACHI),
            "api": file_readback(API),
            "api_tests": file_readback(API_TESTS),
            "dashboard_client": file_readback(DASHBOARD_CLIENT),
            "live_api_verify": file_readback(LIVE_API_VERIFY),
            "doc": file_readback(DOC),
        },
        "physical_counts": physical,
        "guard_wiring": guard,
        "synthetic_cases": synthetic,
    }
    write_json(out / "progression-axis-guard-readback.json", readback)
    write_manifest(out, [out / "progression-axis-guard-readback.json"])
    print(json.dumps(readback, indent=2, sort_keys=True))
    return 0


def physical_counts() -> dict[str, Any]:
    with zipfile.ZipFile(HARRACHI) as archive:
        files = {}
        for name in sorted(archive.namelist()):
            if not name.endswith(".csv"):
                continue
            data = archive.read(name)
            rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
            positives = {
                axis: sum(is_positive(row.get(axis, "")) for row in rows)
                for axis in PLACEMENT_AXES
            }
            negatives = {axis: len(rows) - positives[axis] for axis in PLACEMENT_AXES}
            files[name] = {
                "rows": len(rows),
                "sha256": sha256_bytes(data),
                "positives": positives,
                "negatives": negatives,
                "min_class": {
                    axis: min(positives[axis], negatives[axis])
                    for axis in PLACEMENT_AXES
                },
            }
    train = files.get("train.csv")
    if train is None:
        raise AssertionError("Harrachi zip missing train.csv")
    if train["positives"] != EXPECTED_TRAIN_POSITIVES:
        raise AssertionError(f"unexpected train positives: {train['positives']}")
    under_floor = {
        axis: train["positives"][axis]
        for axis in PLACEMENT_AXES
        if train["positives"][axis] < MIN_CLASS
    }
    if under_floor != EXPECTED_TRAIN_POSITIVES:
        raise AssertionError(f"expected all placement axes under floor, got {under_floor}")
    return {
        "files": files,
        "under_floor_positive_counts": under_floor,
    }


def guard_wiring() -> dict[str, Any]:
    api = API.read_text(encoding="utf-8")
    tests = API_TESTS.read_text(encoding="utf-8")
    client = DASHBOARD_CLIENT.read_text(encoding="utf-8")
    live_verify = LIVE_API_VERIFY.read_text(encoding="utf-8")
    doc = DOC.read_text(encoding="utf-8")

    required_api = [
        "DISABLED_PROGRESSION_AXES",
        "disabled_progression_axis",
        "CALYX_WEB_API_PREDICT_PROGRESSION_AXIS_DISABLED",
        "grounding floor",
    ]
    required_tests = [
        "predict_progression_rejects_under_floor_axis",
        "predict_progression_real_loopback_http_rejects_under_floor_axis",
        "predict_progression_under_floor_axis_precedes_unknown_key_lookup",
    ]
    required_client = ["isDisabledProgression", "grounding floor", "return null"]
    required_live_verify = ["axis: \"winner\"", "grounding floor", "axis: \"quarter_finalist\""]
    required_doc = ["winner=6", "finalist=12", "semi_finalist=24", "quarter_finalist=48"]
    assert_contains(api, required_api, API)
    assert_contains(tests, required_tests, API_TESTS)
    assert_contains(client, required_client, DASHBOARD_CLIENT)
    assert_contains(live_verify, required_live_verify, LIVE_API_VERIFY)
    assert_contains(doc, required_doc, DOC)
    for axis, count in EXPECTED_TRAIN_POSITIVES.items():
        if axis in API_AXES and f"positive class has {count} rows below grounding floor 50" not in api:
            raise AssertionError(f"API guard missing {axis} count {count}")
    return {
        "status": "ok",
        "api_disabled_axes": API_AXES,
        "unknown_axis_rejected": "quarter_finalist",
        "required_terms_checked": len(required_api)
        + len(required_tests)
        + len(required_client)
        + len(required_live_verify)
        + len(required_doc),
    }


def synthetic_cases() -> list[dict[str, Any]]:
    cases = [
        ("happy_balanced", {"0": 50, "1": 50}, "enabled"),
        ("winner_under_floor", {"0": 186, "1": 6}, "disabled"),
        ("finalist_under_floor", {"0": 180, "1": 12}, "disabled"),
        ("semi_finalist_under_floor", {"0": 168, "1": 24}, "disabled"),
        ("quarter_finalist_under_floor", {"0": 144, "1": 48}, "disabled"),
        ("single_class", {"0": 100, "1": 0}, "disabled"),
    ]
    out = []
    for name, counts, expected in cases:
        observed = "enabled" if min(counts.values()) >= MIN_CLASS and len(counts) >= 2 else "disabled"
        if observed != expected:
            raise AssertionError(f"{name} expected {expected}, got {observed}")
        out.append(
            {
                "case": name,
                "counts": counts,
                "expected": expected,
                "observed": observed,
                "min_class_count": min(counts.values()),
            }
        )
    return out


def assert_contains(text: str, terms: list[str], path: Path) -> None:
    missing = [term for term in terms if term not in text]
    if missing:
        raise AssertionError(f"{path.relative_to(ROOT)} missing terms {missing}")


def is_positive(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


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
