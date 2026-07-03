#!/usr/bin/env python3
"""Verify wrong-length Soccer Lab external-cmd vectors fail closed."""

from __future__ import annotations

import argparse
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
TEAM_ATTACK = ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "attack"

FAULTS = {
    "short_vector": {"body": b'{"vectors":[[0.25,0.5]]}', "shape": "Dense(6)"},
    "long_vector": {"body": b'{"vectors":[[0,0,0,0,0,0,0]]}', "shape": "Dense(6)"},
    "missing_vector": {"body": b'{"vectors":[]}', "shape": "Dense(6)"},
    "extra_batch_vector": {"body": b'{"vectors":[[0,0,0,0,0,0],[1,1,1,1,1,1]]}', "shape": "Dense(6)"},
}


def frame(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def decode(stdout: bytes) -> dict[str, Any]:
    if len(stdout) < 4:
        raise AssertionError("missing output frame header")
    size = struct.unpack(">I", stdout[:4])[0]
    body = stdout[4:]
    if len(body) != size:
        raise AssertionError(f"output frame length mismatch expected={size} observed={len(body)}")
    return json.loads(body)


def verify_real_projector_dim() -> dict[str, object]:
    raw = b"trailing_goals_for_per_match=3 home_team=1"
    proc = subprocess.run(
        [str(TEAM_ATTACK)],
        input=frame({"modality": "text", "inputs": [list(raw)]}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if proc.returncode != 0:
        raise AssertionError(f"real projector failed: {proc.stderr.decode('utf-8')}")
    payload = decode(proc.stdout)
    vectors = payload.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != 1:
        raise AssertionError(f"bad vectors payload: {payload}")
    vector = vectors[0]
    if len(vector) != 6:
        raise AssertionError(f"team_match.attack emitted dim {len(vector)} != 6")
    return {
        "projector": str(TEAM_ATTACK.relative_to(ROOT)),
        "input": raw.decode("utf-8"),
        "exit_code": proc.returncode,
        "dim": len(vector),
        "vector": vector,
        "stderr_bytes": len(proc.stderr),
    }


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
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_calyx(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)


def parse_stderr(stderr: bytes) -> dict[str, Any]:
    lines = stderr.decode("utf-8").splitlines()
    if not lines:
        raise AssertionError("process emitted no stderr")
    return json.loads(lines[-1])


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
    for name, fault in FAULTS.items():
        home = work_dir / name / "calyx_home"
        bin_dir = work_dir / name / "bin"
        if home.parent.exists():
            shutil.rmtree(home.parent)
        bin_dir.mkdir(parents=True)
        home.mkdir(parents=True)
        fault_path = bin_dir / "dim_fault_lens"
        fault_sha = write_fault_projector(fault_path, fault["body"])
        env = os.environ.copy()
        env["CALYX_HOME"] = str(home)

        vault_name = f"soccer-dim-{name}"
        create = run_calyx(["create-vault", vault_name, "--panel-template", "text-default"], env)
        if create.returncode != 0:
            raise AssertionError(f"{name}: create-vault failed: {create.stderr.decode('utf-8')}")
        created = json.loads(create.stdout)
        vault_path = home / "vaults" / created["vault_id"]

        add = run_calyx(
            [
                "add-lens",
                vault_name,
                "--name",
                "dim_fault",
                "--runtime",
                "external-cmd",
                "--endpoint",
                str(fault_path),
                "--shape",
                fault["shape"],
                "--modality",
                "text",
            ],
            env,
        )
        if add.returncode != 0:
            raise AssertionError(f"{name}: add-lens failed: {add.stderr.decode('utf-8')}")
        before = read_cx_list(vault_path, env)
        ingest = run_calyx(["ingest", vault_name, "--text", "ok"], env)
        if ingest.returncode == 0:
            raise AssertionError(f"{name}: bad external-cmd lens unexpectedly ingested")
        error = parse_stderr(ingest.stderr)
        if error.get("code") != "CALYX_LENS_DIM_MISMATCH":
            raise AssertionError(f"{name}: expected dim mismatch, observed {error}")
        after = read_cx_list(vault_path, env)
        if before != after:
            raise AssertionError(f"{name}: failed ingest mutated cx-list before={before} after={after}")
        results[name] = {
            "fault_body": fault["body"].decode("utf-8"),
            "fault_script_sha256": fault_sha,
            "declared_shape": fault["shape"],
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
    work_dir = Path(args.out).resolve().parent if args.out else ROOT / "scratchpad" / "wc2026" / "fsv" / "projector_dim_mismatch"
    report = {
        "status": "ok",
        "real_projector": verify_real_projector_dim(),
        "calyx_faults": verify_calyx_faults(work_dir),
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
                "real_projector_dim": report["real_projector"]["dim"],
                "calyx_faults": sorted(report["calyx_faults"]),
                "calyx_error": "CALYX_LENS_DIM_MISMATCH",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
