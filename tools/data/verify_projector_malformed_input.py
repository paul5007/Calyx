#!/usr/bin/env python3
"""Verify malformed Soccer Lab projector input fails closed."""

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
TEAM_ATTACK = ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "attack"

PROJECTORS = {
    "team_match.attack": ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "attack",
    "team_match.defense": ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "defense",
    "team_match.tempo": ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "tempo",
    "team_match.discipline": ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "discipline",
    "team_match.pedigree": ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "pedigree",
    "team_match.form": ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "form",
    "team_match.context": ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "context",
    "player.output": ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "output",
    "player.profile": ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "profile",
    "player.efficiency": ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "efficiency",
}


def frame(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def projector_input(raw: bytes) -> bytes:
    return frame({"modality": "text", "inputs": [list(raw)]})


def run_raw(path: Path, data: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(path)], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)


def parse_stderr(stderr: bytes) -> dict[str, Any]:
    lines = stderr.decode("utf-8").splitlines()
    if not lines:
        raise AssertionError("projector emitted no stderr")
    return json.loads(lines[-1])


def assert_no_vector_stdout(name: str, stdout: bytes) -> None:
    if stdout:
        raise AssertionError(f"{name}: failed projector wrote stdout bytes={len(stdout)}")


def assert_projector_failure(
    name: str,
    path: Path,
    stdin_bytes: bytes,
    expected_reason: str,
    expected_hash_bytes: bytes,
) -> dict[str, object]:
    proc = run_raw(path, stdin_bytes)
    if proc.returncode == 0:
        raise AssertionError(f"{name}: expected non-zero exit")
    assert_no_vector_stdout(name, proc.stdout)
    error = parse_stderr(proc.stderr)
    observed_hash = error.get("input_hash")
    expected_hash = hashlib.sha256(expected_hash_bytes).hexdigest()
    if error.get("facet") != path.name:
        raise AssertionError(f"{name}: facet mismatch {error}")
    if error.get("reason") != expected_reason:
        raise AssertionError(f"{name}: reason mismatch {error}")
    if observed_hash != expected_hash:
        raise AssertionError(f"{name}: input_hash mismatch expected={expected_hash} observed={observed_hash}")
    return {
        "exit_code": proc.returncode,
        "reason": error["reason"],
        "facet": error["facet"],
        "input_hash": observed_hash,
        "stdout_bytes": len(proc.stdout),
        "stderr": error,
    }


def verify_direct_edges() -> dict[str, object]:
    malformed = b"not_a_pair"
    invalid_utf8 = b"\xff"
    invalid_json_body = b"{"
    truncated_frame = struct.pack(">I", 8) + b'{"x"'

    edges: dict[str, object] = {}
    for name, path in PROJECTORS.items():
        edges[f"{name}.malformed_token"] = assert_projector_failure(
            f"{name}.malformed_token",
            path,
            projector_input(malformed),
            "malformed_token",
            malformed,
        )

    edges["team_match.attack.invalid_utf8"] = assert_projector_failure(
        "team_match.attack.invalid_utf8",
        TEAM_ATTACK,
        projector_input(invalid_utf8),
        "invalid_utf8",
        invalid_utf8,
    )
    edges["team_match.attack.empty_field_name"] = assert_projector_failure(
        "team_match.attack.empty_field_name",
        TEAM_ATTACK,
        projector_input(b"=1"),
        "empty_field_name",
        b"=1",
    )
    edges["team_match.attack.invalid_json_frame"] = assert_projector_failure(
        "team_match.attack.invalid_json_frame",
        TEAM_ATTACK,
        struct.pack(">I", len(invalid_json_body)) + invalid_json_body,
        "invalid_json_frame",
        invalid_json_body,
    )
    edges["team_match.attack.truncated_frame"] = assert_projector_failure(
        "team_match.attack.truncated_frame",
        TEAM_ATTACK,
        truncated_frame,
        "truncated_frame",
        truncated_frame,
    )
    return edges


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

    create = run_calyx(["create-vault", "soccer-malformed-edge", "--panel-template", "text-default"], env)
    if create.returncode != 0:
        raise AssertionError(f"create-vault failed: {create.stderr.decode('utf-8')}")
    created = json.loads(create.stdout)
    vault_path = home / "vaults" / created["vault_id"]

    add = run_calyx(
        [
            "add-lens",
            "soccer-malformed-edge",
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

    ingest = run_calyx(["ingest", "soccer-malformed-edge", "--text", "not_a_pair"], env)
    if ingest.returncode == 0:
        raise AssertionError("malformed Calyx ingest unexpectedly succeeded")
    try:
        error = json.loads(ingest.stderr.decode("utf-8").splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise AssertionError(f"ingest stderr did not end with JSON error: {ingest.stderr.decode('utf-8')}") from exc
    if error.get("code") != "CALYX_LENS_UNREACHABLE":
        raise AssertionError(f"expected CALYX_LENS_UNREACHABLE, observed {error}")

    after = read_cx_list(vault_path, env)
    if before != after:
        raise AssertionError(f"failed ingest mutated cx-list before={before} after={after}")

    return {
        "vault_id": created["vault_id"],
        "added_lens": json.loads(add.stdout),
        "ingest_exit_code": ingest.returncode,
        "ingest_error": error,
        "ingest_stdout_bytes": len(ingest.stdout),
        "ingest_stderr_tail": ingest.stderr.decode("utf-8").splitlines()[-6:],
        "cx_list_before": before,
        "cx_list_after": after,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="", help="optional JSON report path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    work_dir = Path(args.out).resolve().parent if args.out else ROOT / "scratchpad" / "wc2026" / "fsv" / "projector_malformed_input"
    report = {
        "status": "ok",
        "direct_edges": verify_direct_edges(),
        "calyx_surface": verify_calyx_surface(work_dir),
        "projector_count": len(PROJECTORS),
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
                "direct_edge_count": len(report["direct_edges"]),
                "calyx_error": report["calyx_surface"]["ingest_error"]["code"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
