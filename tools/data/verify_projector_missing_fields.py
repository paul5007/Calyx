#!/usr/bin/env python3
"""Verify Soccer Lab projectors emit 0.0 for missing fields."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

PROJECTORS = {
    "team_match.attack": ("tools/lenses/soccer_lab/team_match/attack", 6, "trailing_goals_for_per_match=3", [0.5, 0.0, 0.0, 0.0, 0.0, 0.0]),
    "team_match.defense": ("tools/lenses/soccer_lab/team_match/defense", 5, "trailing_goals_against_per_match=1.5", [0.25, 0.0, 0.0, 0.0, 0.0]),
    "team_match.tempo": ("tools/lenses/soccer_lab/team_match/tempo", 4, "days_since_previous_match=7", [0.0, 0.0, 0.5, 0.0]),
    "team_match.discipline": ("tools/lenses/soccer_lab/team_match/discipline", 4, "trailing_yellow_cards_per_match=5", [1.0 / 3.0, 0.0, 0.0, 0.0]),
    "team_match.pedigree": ("tools/lenses/soccer_lab/team_match/pedigree", 6, "prior_world_cup_matches=15", [0.0, 0.0, 0.0, 0.0, 0.5, 0.0]),
    "team_match.form": ("tools/lenses/soccer_lab/team_match/form", 5, "trailing_points_per_match=1.5", [0.0, 0.0, 0.0, 0.5, 0.0]),
    "team_match.context": ("tools/lenses/soccer_lab/team_match/context", 8, "group_stage=1", [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    "player.output": ("tools/lenses/soccer_lab/player/output", 5, "prior_goals=4", [0.0, 0.0, 0.0, 0.0, 0.0]),
    "player.profile": ("tools/lenses/soccer_lab/player/profile", 7, "midfielder=1", [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]),
    "player.efficiency": ("tools/lenses/soccer_lab/player/efficiency", 5, "prior_appearances=10", [0.5, 0.0, 0.0, 0.0, 0.0]),
}


def frame(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def run_projector(path: str, text: str) -> tuple[int, bytes, bytes]:
    payload = {"modality": "text", "inputs": [list(text.encode("utf-8"))]}
    proc = subprocess.run([str(ROOT / path)], input=frame(payload), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    return proc.returncode, proc.stdout, proc.stderr


def decode(stdout: bytes) -> list[float]:
    if len(stdout) < 4:
        raise AssertionError("missing output frame header")
    size = struct.unpack(">I", stdout[:4])[0]
    body = stdout[4:]
    if len(body) != size:
        raise AssertionError(f"output frame length mismatch expected={size} observed={len(body)}")
    payload = json.loads(body)
    vectors = payload.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != 1:
        raise AssertionError(f"bad vectors payload: {payload}")
    return vectors[0]


def assert_close(name: str, observed: list[float], expected: list[float]) -> None:
    if len(observed) != len(expected):
        raise AssertionError(f"{name}: dim mismatch expected={len(expected)} observed={len(observed)}")
    for idx, (obs, exp) in enumerate(zip(observed, expected)):
        if not math.isfinite(obs):
            raise AssertionError(f"{name}: non-finite at {idx}: {obs}")
        if abs(obs - exp) > 1e-6:
            raise AssertionError(f"{name}: value mismatch at {idx}: expected={exp} observed={obs}")


def verify() -> dict[str, object]:
    results = {}
    for name, (path, dim, partial_text, partial_expected) in PROJECTORS.items():
        code, stdout, stderr = run_projector(path, "")
        if code != 0:
            raise AssertionError(f"{name}: empty input failed: {stderr.decode()}")
        empty_vector = decode(stdout)
        assert_close(f"{name}.empty", empty_vector, [0.0] * dim)

        code, stdout, stderr = run_projector(path, partial_text)
        if code != 0:
            raise AssertionError(f"{name}: partial input failed: {stderr.decode()}")
        partial_vector = decode(stdout)
        assert_close(f"{name}.partial", partial_vector, partial_expected)

        results[name] = {
            "empty": empty_vector,
            "partial_input": partial_text,
            "partial": partial_vector,
            "path": path,
            "path_sha256": hashlib.sha256((ROOT / path).read_bytes()).hexdigest(),
        }
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="", help="optional JSON report path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = {"status": "ok", "projectors": verify()}
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(encoded + "\n", encoding="utf-8")
        if out.read_text(encoding="utf-8") != encoded + "\n":
            raise AssertionError("report readback mismatch")
    print(json.dumps({"status": "ok", "projector_count": len(PROJECTORS)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
