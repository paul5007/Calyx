#!/usr/bin/env python3
"""FSV verifier for Soccer Lab team/match external-cmd projectors."""

from __future__ import annotations

import argparse
import json
import math
import struct
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LENS_DIR = ROOT / "tools" / "lenses" / "soccer_lab" / "team_match"

KNOWN_INPUT = (
    "trailing_goals_for_per_match=3 trailing_goal_scoring_rate=0.8 "
    "trailing_multi_goal_rate=0.4 trailing_penalties_for_per_match=1 "
    "home_team=1 away_team=0 trailing_goals_against_per_match=1.5 "
    "trailing_clean_sheet_rate=0.2 trailing_multi_concede_rate=0.3 "
    "trailing_penalties_against_per_match=0.5 trailing_goal_differential=2 "
    "trailing_extra_time_rate=0.1 trailing_penalty_shootout_rate=0.2 "
    "days_since_previous_match=7 knockout_stage=1 "
    "trailing_yellow_cards_per_match=5 trailing_red_cards_per_match=1 "
    "trailing_second_yellow_rate=0.25 trailing_sending_off_rate=0.1 "
    "confederation_code=UEFA region_name=Europe mens_team=1 womens_team=0 "
    "prior_world_cup_matches=20 prior_best_finish=4 "
    "trailing_win_rate=0.6 trailing_draw_rate=0.2 trailing_loss_rate=0.2 "
    "trailing_points_per_match=2.0 trailing_form_goal_diff=3 "
    "stage_name=group_stage group_name=Group_A group_stage=1 "
    "match_day_of_tournament=10 kickoff_hour=18 host_country=0 "
    "stadium_capacity=60000"
)

EXPECTED = {
    "attack": [0.5, 0.8, 0.4, 0.2, 1.0, 0.0],
    "defense": [0.25, 0.2, 0.3, 0.1, 0.6666666667],
    "tempo": [0.1, 0.2, 0.5, 1.0],
    "discipline": [0.3333333333, 0.2, 0.25, 0.1],
    "pedigree": None,
    "form": [0.6, 0.2, 0.2, 0.6666666667, 0.75],
    "context": None,
}


def stable_hash(value: str) -> float:
    import hashlib

    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") % 1024) / 1023.0


EXPECTED["pedigree"] = [
    stable_hash("UEFA"),
    stable_hash("Europe"),
    1.0,
    0.0,
    20.0 / 30.0,
    (32.0 - 4.0) / 31.0,
]
EXPECTED["context"] = [
    stable_hash("group_stage"),
    stable_hash("Group_A"),
    1.0,
    1.0,
    10.0 / 45.0,
    18.0 / 23.0,
    0.0,
    0.5,
]


def frame(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def run_projector(path: Path, text: str) -> tuple[int, bytes, bytes]:
    payload = {"modality": "text", "inputs": [list(text.encode("utf-8"))]}
    proc = subprocess.run([str(path)], input=frame(payload), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    return proc.returncode, proc.stdout, proc.stderr


def decode(stdout: bytes) -> dict[str, object]:
    if len(stdout) < 4:
        raise AssertionError("missing output frame header")
    size = struct.unpack(">I", stdout[:4])[0]
    body = stdout[4:]
    if len(body) != size:
        raise AssertionError(f"output frame length mismatch expected={size} observed={len(body)}")
    return json.loads(body)


def assert_close(name: str, observed: list[float], expected: list[float]) -> None:
    if len(observed) != len(expected):
        raise AssertionError(f"{name}: dim mismatch expected={len(expected)} observed={len(observed)}")
    for idx, (obs, exp) in enumerate(zip(observed, expected)):
        if not math.isfinite(obs):
            raise AssertionError(f"{name}: non-finite at {idx}: {obs}")
        if abs(obs - exp) > 1e-6:
            raise AssertionError(f"{name}: value mismatch at {idx}: expected={exp} observed={obs}")


def verify_known() -> dict[str, object]:
    results = {}
    for facet, expected in EXPECTED.items():
        path = LENS_DIR / facet
        code, stdout, stderr = run_projector(path, KNOWN_INPUT)
        if code != 0:
            raise AssertionError(f"{facet}: expected success, got code={code} stderr={stderr.decode()}")
        payload = decode(stdout)
        vectors = payload.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise AssertionError(f"{facet}: malformed vectors payload {payload}")
        observed = vectors[0]
        assert_close(facet, observed, expected)
        results[facet] = {"dim": len(observed), "vector": observed}
    return results


def verify_edges() -> dict[str, object]:
    edges = {}
    cases = {
        "empty": ("attack", ""),
        "malformed": ("attack", "not_a_pair"),
        "invalid_number": ("attack", "trailing_goals_for_per_match=abc"),
    }
    for name, (facet, text) in cases.items():
        code, stdout, stderr = run_projector(LENS_DIR / facet, text)
        if name == "empty":
            if code != 0:
                raise AssertionError(f"empty input should emit defined zeros: {stderr.decode()}")
            vector = decode(stdout)["vectors"][0]
            assert_close("empty", vector, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            edges[name] = {"code": code, "vector": vector}
        else:
            if code == 0:
                raise AssertionError(f"{name} should fail closed")
            err = json.loads(stderr.decode().splitlines()[-1])
            edges[name] = {"code": code, "reason": err.get("reason"), "facet": err.get("facet"), "input_hash": err.get("input_hash")}
    return edges


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="", help="optional JSON report path")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    report = {"known": verify_known(), "edges": verify_edges(), "status": "ok"}
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(encoded + "\n", encoding="utf-8")
        observed = out.read_text(encoding="utf-8")
        if observed != encoded + "\n":
            raise AssertionError("report readback mismatch")
    print(json.dumps({"status": "ok", "facets": sorted(EXPECTED), "edges": sorted(report["edges"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
