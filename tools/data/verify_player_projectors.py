#!/usr/bin/env python3
"""FSV verifier for Soccer Lab player external-cmd projectors."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LENS_DIR = ROOT / "tools" / "lenses" / "soccer_lab" / "player"

KNOWN_INPUT = (
    "prior_goals=4 prior_appearances=10 trailing_goal_rate=0.3 "
    "prior_penalties_converted=2 prior_penalty_kicks=4 prior_starts=8 "
    "prior_substitute_appearances=2 female=0 goal_keeper=0 defender=0 "
    "midfielder=1 forward=0 count_tournaments=3 position_code=MF "
    "prior_substitute_goals=1 prior_yellow_cards=2 prior_red_cards=1"
)

EXPECTED = {
    "output": [0.4, 0.3, 0.5, 0.8, 0.2],
    "profile": None,
    "efficiency": [0.5, 0.5, 0.5, 0.2, 0.1],
}


def stable_hash(value: str) -> float:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") % 1024) / 1023.0


EXPECTED["profile"] = [0.0, 0.0, 0.0, 1.0, 0.0, 0.5, stable_hash("MF")]


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
        "empty": ("output", ""),
        "malformed": ("output", "not_a_pair"),
        "invalid_number": ("output", "prior_goals=abc"),
        "invalid_boolean": ("profile", "female=maybe"),
    }
    for name, (facet, text) in cases.items():
        code, stdout, stderr = run_projector(LENS_DIR / facet, text)
        if name == "empty":
            if code != 0:
                raise AssertionError(f"empty input should emit defined zeros: {stderr.decode()}")
            vector = decode(stdout)["vectors"][0]
            assert_close("empty", vector, [0.0, 0.0, 0.0, 0.0, 0.0])
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
    raise SystemExit(main(__import__("sys").argv[1:]))
