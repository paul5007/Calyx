#!/usr/bin/env python3
"""Verify Soccer Lab projectors emit robust structured JSON errors."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CALYX = ROOT / "target" / "release" / "calyx"

PROJECTORS = {
    "team_match.attack": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "attack", 6),
    "team_match.defense": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "defense", 5),
    "team_match.tempo": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "tempo", 4),
    "team_match.discipline": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "discipline", 4),
    "team_match.pedigree": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "pedigree", 6),
    "team_match.form": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "form", 5),
    "team_match.context": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "context", 8),
    "player.output": (ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "output", 5),
    "player.profile": (ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "profile", 7),
    "player.efficiency": (ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "efficiency", 5),
}

TEAM_ATTACK = PROJECTORS["team_match.attack"][0]
EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def frame(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def projector_input(*raw_inputs: bytes) -> bytes:
    return frame({"modality": "text", "inputs": [list(raw) for raw in raw_inputs]})


def run_raw(path: Path, data: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(path)], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)


def decode_frame(stdout: bytes) -> dict[str, Any]:
    if len(stdout) < 4:
        raise AssertionError(f"stdout missing frame header bytes={len(stdout)}")
    size = struct.unpack(">I", stdout[:4])[0]
    body = stdout[4:]
    if len(body) != size:
        raise AssertionError(f"stdout frame length mismatch expected={size} observed={len(body)}")
    return json.loads(body)


def json_lines(stderr: bytes) -> list[dict[str, Any]]:
    records = []
    for line in stderr.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def embedded_json_objects(stderr: bytes) -> list[dict[str, Any]]:
    text = stderr.decode("utf-8")
    decoder = json.JSONDecoder()
    records = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def projector_error_records(stderr: bytes) -> list[dict[str, Any]]:
    return [record for record in embedded_json_objects(stderr) if record.get("event") == "soccer_lab_projector_error"]


def assert_error_schema(name: str, error: dict[str, Any], path: Path, reason: str, raw: bytes) -> None:
    expected_hash = hashlib.sha256(raw).hexdigest()
    expected = {
        "event": "soccer_lab_projector_error",
        "facet": path.name,
        "input_hash": expected_hash,
        "reason": reason,
        "schema_version": 1,
    }
    for key, value in expected.items():
        if error.get(key) != value:
            raise AssertionError(f"{name}: {key} mismatch expected={value!r} observed={error!r}")


def verify_happy_path() -> dict[str, object]:
    results = {}
    for name, (path, dim) in PROJECTORS.items():
        proc = run_raw(path, projector_input(b""))
        if proc.returncode != 0:
            raise AssertionError(f"{name}: empty input should succeed: {proc.stderr.decode('utf-8')}")
        if proc.stderr:
            raise AssertionError(f"{name}: successful projector emitted stderr: {proc.stderr.decode('utf-8')}")
        payload = decode_frame(proc.stdout)
        vectors = payload.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise AssertionError(f"{name}: invalid vector payload {payload}")
        vector = vectors[0]
        if len(vector) != dim:
            raise AssertionError(f"{name}: dim mismatch expected={dim} observed={len(vector)}")
        results[name] = {
            "exit_code": proc.returncode,
            "stdout_bytes": len(proc.stdout),
            "stderr_bytes": len(proc.stderr),
            "dim": dim,
            "vector": vector,
        }
    return results


def assert_failure(name: str, path: Path, stdin_bytes: bytes, reason: str, hash_raw: bytes) -> dict[str, object]:
    proc = run_raw(path, stdin_bytes)
    if proc.returncode == 0:
        raise AssertionError(f"{name}: expected failure")
    if proc.stdout:
        raise AssertionError(f"{name}: failed projector wrote stdout bytes={len(proc.stdout)}")
    records = projector_error_records(proc.stderr)
    if len(records) != 1:
        raise AssertionError(f"{name}: expected exactly one projector JSON error, observed={proc.stderr!r}")
    error = records[0]
    assert_error_schema(name, error, path, reason, hash_raw)
    return {
        "exit_code": proc.returncode,
        "stdout_bytes": len(proc.stdout),
        "stderr_bytes": len(proc.stderr),
        "stderr_sha256": hashlib.sha256(proc.stderr).hexdigest(),
        "stderr": error,
    }


def verify_direct_failures() -> dict[str, object]:
    results: dict[str, object] = {}
    malformed = b"not_a_pair"
    for name, (path, _dim) in PROJECTORS.items():
        results[f"{name}.malformed_token"] = assert_failure(
            f"{name}.malformed_token",
            path,
            projector_input(malformed),
            "malformed_token",
            malformed,
        )

    invalid_number = b"trailing_goals_for_per_match=abc"
    results["team_match.attack.invalid_number"] = assert_failure(
        "team_match.attack.invalid_number",
        TEAM_ATTACK,
        projector_input(invalid_number),
        "invalid_number",
        invalid_number,
    )

    invalid_boolean = b"home_team=maybe"
    results["team_match.attack.invalid_boolean"] = assert_failure(
        "team_match.attack.invalid_boolean",
        TEAM_ATTACK,
        projector_input(invalid_boolean),
        "invalid_boolean",
        invalid_boolean,
    )

    invalid_utf8 = b"\xff"
    results["team_match.attack.invalid_utf8"] = assert_failure(
        "team_match.attack.invalid_utf8",
        TEAM_ATTACK,
        projector_input(invalid_utf8),
        "invalid_utf8",
        invalid_utf8,
    )

    invalid_json_body = b"{"
    results["team_match.attack.invalid_json_frame"] = assert_failure(
        "team_match.attack.invalid_json_frame",
        TEAM_ATTACK,
        struct.pack(">I", len(invalid_json_body)) + invalid_json_body,
        "invalid_json_frame",
        invalid_json_body,
    )

    results["team_match.attack.missing_frame_header"] = assert_failure(
        "team_match.attack.missing_frame_header",
        TEAM_ATTACK,
        b"",
        "missing_frame_header",
        b"",
    )

    bad_item = [256]
    bad_item_raw = json.dumps(bad_item, separators=(",", ":"), sort_keys=True).encode("utf-8")
    results["team_match.attack.input_not_byte_array"] = assert_failure(
        "team_match.attack.input_not_byte_array",
        TEAM_ATTACK,
        frame({"modality": "text", "inputs": [bad_item]}),
        "input_not_byte_array",
        bad_item_raw,
    )

    valid_first = b"trailing_goals_for_per_match=1 home_team=1 away_team=0"
    invalid_second = b"not_a_pair"
    results["team_match.attack.batch_second_input_fails_closed"] = assert_failure(
        "team_match.attack.batch_second_input_fails_closed",
        TEAM_ATTACK,
        projector_input(valid_first, invalid_second),
        "malformed_token",
        invalid_second,
    )
    return results


def run_calyx(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)


def read_cx_list(vault_path: Path, env: dict[str, str]) -> list[object]:
    proc = run_calyx(
        [
            "readback",
            "cx-list",
            "--vault",
            str(vault_path),
            "--include-slots",
            "--limit",
            "10",
            "--rebuild-base-page-index",
        ],
        env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"cx-list failed: {proc.stderr.decode('utf-8')}")
    return json.loads(proc.stdout)


def verify_calyx_surface(work_dir: Path) -> dict[str, object]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)

    vault_name = "soccer-structured-errors"
    create = run_calyx(["create-vault", vault_name, "--panel-template", "text-default"], env)
    if create.returncode != 0:
        raise AssertionError(f"create-vault failed: {create.stderr.decode('utf-8')}")
    created = json.loads(create.stdout)
    vault_path = home / "vaults" / created["vault_id"]

    add = run_calyx(
        [
            "add-lens",
            vault_name,
            "--name",
            "soccer_attack",
            "--runtime",
            "external-cmd",
            "--endpoint",
            str(TEAM_ATTACK),
            "--shape",
            "Dense(6)",
            "--modality",
            "text",
        ],
        env,
    )
    if add.returncode != 0:
        raise AssertionError(f"add-lens failed: {add.stderr.decode('utf-8')}")

    before = read_cx_list(vault_path, env)
    ingest = run_calyx(["ingest", vault_name, "--text", "not_a_pair"], env)
    if ingest.returncode == 0:
        raise AssertionError("malformed ingest unexpectedly succeeded")
    after = read_cx_list(vault_path, env)
    if before != after:
        raise AssertionError(f"failed ingest mutated cx-list before={before} after={after}")

    projector_records = projector_error_records(ingest.stderr)
    if len(projector_records) != 1:
        raise AssertionError(f"expected propagated projector JSON in Calyx stderr, observed={ingest.stderr!r}")
    assert_error_schema("calyx_surface.projector_stderr", projector_records[0], TEAM_ATTACK, "malformed_token", b"not_a_pair")

    json_records = json_lines(ingest.stderr)
    calyx_records = [record for record in json_records if record.get("code") == "CALYX_LENS_UNREACHABLE"]
    if len(calyx_records) != 1:
        raise AssertionError(f"expected Calyx CALYX_LENS_UNREACHABLE JSON, observed={ingest.stderr!r}")

    return {
        "vault_id": created["vault_id"],
        "added_lens": json.loads(add.stdout),
        "cx_list_before": before,
        "cx_list_after": after,
        "ingest_exit_code": ingest.returncode,
        "ingest_stdout_bytes": len(ingest.stdout),
        "ingest_stderr_bytes": len(ingest.stderr),
        "ingest_stderr_sha256": hashlib.sha256(ingest.stderr).hexdigest(),
        "projector_error": projector_records[0],
        "calyx_error": calyx_records[0],
    }


def file_stat(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="", help="optional JSON report path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    work_dir = Path(args.out).resolve().parent if args.out else ROOT / "scratchpad" / "wc2026" / "fsv" / "projector_structured_errors"
    report = {
        "status": "ok",
        "schema": {
            "event": "soccer_lab_projector_error",
            "required_fields": ["event", "schema_version", "facet", "input_hash", "reason"],
            "input_hash_empty_sha256": EMPTY_HASH,
        },
        "happy_path": verify_happy_path(),
        "direct_failures": verify_direct_failures(),
        "calyx_surface": verify_calyx_surface(work_dir),
        "projector_files": {name: file_stat(path) for name, (path, _dim) in PROJECTORS.items()},
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(encoded + "\n", encoding="utf-8")
        if out.read_text(encoding="utf-8") != encoded + "\n":
            raise AssertionError("report readback mismatch")
    print(
        json.dumps(
            {
                "status": "ok",
                "projector_count": len(PROJECTORS),
                "direct_failure_count": len(report["direct_failures"]),
                "calyx_error": report["calyx_surface"]["calyx_error"]["code"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
