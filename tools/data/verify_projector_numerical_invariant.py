#!/usr/bin/env python3
"""Verify non-finite Soccer Lab projector values fail as numerical invariants."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CALYX = ROOT / "target" / "release" / "calyx"

PROJECTORS = {
    "team_match.attack": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "attack", b"trailing_goals_for_per_match=NaN"),
    "team_match.defense": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "defense", b"trailing_goals_against_per_match=Inf"),
    "team_match.tempo": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "tempo", b"trailing_extra_time_rate=-Inf"),
    "team_match.discipline": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "discipline", b"trailing_yellow_cards_per_match=NaN"),
    "team_match.pedigree": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "pedigree", b"prior_world_cup_matches=Inf"),
    "team_match.form": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "form", b"trailing_win_rate=NaN"),
    "team_match.context": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "context", b"match_day_of_tournament=Inf"),
    "player.output": (ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "output", b"prior_goals=NaN prior_appearances=1"),
    "player.profile": (ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "profile", b"count_tournaments=Inf"),
    "player.efficiency": (ROOT / "tools" / "lenses" / "soccer_lab" / "player" / "efficiency", b"prior_appearances=NaN"),
}

FAULT_BODIES = {
    "nan_token": b'{"vectors":[[NaN]]}',
    "infinity_token": b'{"vectors":[[Infinity]]}',
    "out_of_range": b'{"vectors":[[1e999]]}',
}


def frame(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def projector_input(raw: bytes) -> bytes:
    return frame({"modality": "text", "inputs": [list(raw)]})


def parse_stderr(stderr: bytes) -> dict[str, Any]:
    lines = stderr.decode("utf-8").splitlines()
    if not lines:
        raise AssertionError("process emitted no stderr")
    return json.loads(lines[-1])


def verify_soccer_projectors() -> dict[str, object]:
    results = {}
    for name, (path, raw) in PROJECTORS.items():
        proc = subprocess.run([str(path)], input=projector_input(raw), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        if proc.returncode == 0:
            raise AssertionError(f"{name}: expected non-zero exit for {raw!r}")
        if proc.stdout:
            raise AssertionError(f"{name}: failed projector wrote stdout bytes={len(proc.stdout)}")
        error = parse_stderr(proc.stderr)
        expected_hash = hashlib.sha256(raw).hexdigest()
        if error.get("facet") != path.name:
            raise AssertionError(f"{name}: facet mismatch {error}")
        if error.get("reason") != "non_finite_number":
            raise AssertionError(f"{name}: reason mismatch {error}")
        if error.get("input_hash") != expected_hash:
            raise AssertionError(f"{name}: input_hash mismatch {error}")
        results[name] = {
            "input": raw.decode("utf-8"),
            "exit_code": proc.returncode,
            "stdout_bytes": len(proc.stdout),
            "stderr": error,
        }
    return results


def write_fault_projector(path: Path, body: bytes) -> str:
    script = (
        "#!/usr/bin/env python3\n"
        "import struct, sys\n"
        "sys.stdin.buffer.read()\n"
        f"body = {body!r}\n"
        "sys.stdout.buffer.write(struct.pack('>I', len(body)) + body)\n"
        "sys.stdout.buffer.flush()\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def verify_calyx_faults(work_dir: Path) -> dict[str, object]:
    results = {}
    for name, body in FAULT_BODIES.items():
        home = work_dir / name / "calyx_home"
        bin_dir = work_dir / name / "bin"
        if home.parent.exists():
            shutil.rmtree(home.parent)
        bin_dir.mkdir(parents=True)
        home.mkdir(parents=True)
        fault_path = bin_dir / "non_finite_lens"
        fault_sha = write_fault_projector(fault_path, body)
        env = os.environ.copy()
        env["CALYX_HOME"] = str(home)

        create = run_calyx(["create-vault", f"soccer-numeric-{name}", "--panel-template", "text-default"], env)
        if create.returncode != 0:
            raise AssertionError(f"{name}: create-vault failed: {create.stderr.decode('utf-8')}")
        created = json.loads(create.stdout)
        vault_path = home / "vaults" / created["vault_id"]

        add = run_calyx(
            [
                "add-lens",
                f"soccer-numeric-{name}",
                "--name",
                "non_finite_fault",
                "--runtime",
                "external-cmd",
                "--endpoint",
                str(fault_path),
                "--shape",
                "Dense(1)",
                "--modality",
                "text",
            ],
            env,
        )
        if add.returncode != 0:
            raise AssertionError(f"{name}: add-lens failed: {add.stderr.decode('utf-8')}")
        before = read_cx_list(vault_path, env)
        ingest = run_calyx(["ingest", f"soccer-numeric-{name}", "--text", "ok"], env)
        if ingest.returncode == 0:
            raise AssertionError(f"{name}: bad external-cmd lens unexpectedly ingested")
        error = parse_stderr(ingest.stderr)
        if error.get("code") != "CALYX_LENS_NUMERICAL_INVARIANT":
            raise AssertionError(f"{name}: expected numerical invariant, observed {error}")
        after = read_cx_list(vault_path, env)
        if before != after:
            raise AssertionError(f"{name}: failed ingest mutated cx-list before={before} after={after}")
        results[name] = {
            "fault_body": body.decode("utf-8"),
            "fault_script_sha256": fault_sha,
            "vault_id": created["vault_id"],
            "added_lens": json.loads(add.stdout),
            "ingest_exit_code": ingest.returncode,
            "ingest_error": error,
            "ingest_stderr_tail": ingest.stderr.decode("utf-8").splitlines()[-6:],
            "cx_list_before": before,
            "cx_list_after": after,
        }
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="", help="optional JSON report path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    work_dir = Path(args.out).resolve().parent if args.out else ROOT / "scratchpad" / "wc2026" / "fsv" / "projector_numerical_invariant"
    report = {
        "status": "ok",
        "projector_non_finite_inputs": verify_soccer_projectors(),
        "calyx_faults": verify_calyx_faults(work_dir),
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
                "projector_count": len(report["projector_non_finite_inputs"]),
                "calyx_faults": sorted(report["calyx_faults"]),
                "calyx_error": "CALYX_LENS_NUMERICAL_INVARIANT",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
