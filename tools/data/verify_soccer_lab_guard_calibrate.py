#!/usr/bin/env python3
"""Verify Soccer Lab guard calibration rows in the physical Guard CF."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import verify_soccer_lab_anchored_outcomes as anchored
import verify_soccer_lab_weave_loom as weave


ROOT = anchored.ROOT
CALYX = anchored.CALYX
DEFAULT_RAW = anchored.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "guard_calibrate" / "report.json"
VAULT_NAME = "soccer-guard-calibrate"
DOMAIN = "soccer_lab.team_match_result"
TARGET_FAR = 0.01
CALIBRATION_SLOTS = {
    "attack": 8,
    "defense": 9,
    "tempo": 10,
}
MIN_BAD_SCORES = 50


class GuardFsvError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> weave.subprocess.CompletedProcess[bytes]:
    return weave.subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=weave.subprocess.PIPE, stderr=weave.subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> weave.subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise GuardFsvError(
            reason,
            {
                "args": args,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def guard_key(subject: str) -> str:
    return (b"profile\0" + subject.encode("utf-8")).hex()


def build_real_guard(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    rows_root = work_dir / "rows"
    generation = weave.generate_team_rows(raw_root, rows_root)
    rows_path = rows_root / "team-match-nonzero-balanced.jsonl"
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    calibration = write_calibration_set(work_dir, rows)
    vault = build_guard_vault(work_dir, rows_path, generation["selected_rows"])
    env = os.environ.copy()
    env["CALYX_HOME"] = str(work_dir / "calyx_home")
    guard = run_ok(
        [
            "guard",
            VAULT_NAME,
            "calibrate",
            "--domain",
            DOMAIN,
            "--set",
            str(calibration["file"]["path"]),
            "--target-far",
            str(TARGET_FAR),
        ],
        env,
        "guard_calibrate_failed",
        timeout=180,
    )
    command_report = json.loads(guard.stdout)
    verify_command_report(command_report, calibration)
    readback = read_guard_cf(env, ROOT / vault["vault_path"])
    decoded = verify_guard_readback(readback, command_report)
    checks = run_guard_checks(env, ROOT / vault["vault_path"], vault["sample_cx_ids"])
    return {
        "generation": generation,
        "calibration": calibration,
        "vault": vault,
        "guard_stdout_sha256": anchored.sha256_bytes(guard.stdout),
        "guard_stderr_sha256": anchored.sha256_bytes(guard.stderr),
        "command_report": command_report,
        "guard_cf_readback": decoded,
        "guard_checks": checks,
        "physical_readback": guard_physical_readback(ROOT / vault["vault_path"]),
    }


def write_calibration_set(work_dir: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    facets = {name: anchored.FACETS[name] for name in CALIBRATION_SLOTS}
    vectors = weave.run_projectors([row["text"].encode("utf-8") for row in rows], facets)
    labels = [row["anchors"][0]["value"] for row in rows]
    lines: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for facet, slot in CALIBRATION_SLOTS.items():
        good_scores = [1.0 for _ in rows]
        bad_scores = []
        for index, vector in enumerate(vectors[facet]):
            candidates = [
                cosine(vector, other)
                for other_index, other in enumerate(vectors[facet])
                if labels[other_index] != labels[index]
            ]
            if not candidates:
                raise GuardFsvError("calibration_no_bad_candidates", {"facet": facet})
            bad_scores.append(min(candidates))
        for score in good_scores:
            lines.append({"slot": slot, "score": score, "class": "good"})
        for score in bad_scores:
            lines.append({"slot": slot, "score": score, "class": "injection"})
        summary[str(slot)] = score_summary(good_scores, bad_scores)
    for slot, info in summary.items():
        if info["bad_count"] < MIN_BAD_SCORES:
            raise GuardFsvError("calibration_bad_floor_missed", {"slot": slot, "summary": info})
    path = work_dir / "guard-calibration-scores.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line, sort_keys=True) + "\n" for line in lines), encoding="utf-8")
    return {
        "file": file_readback(path),
        "rows": len(lines),
        "slots": summary,
        "class_counts": {"good": len(rows) * len(CALIBRATION_SLOTS), "injection": len(rows) * len(CALIBRATION_SLOTS)},
        "source": "real Soccer Lab facet vectors; good=self cosine; bad=min cross-outcome cosine per row/facet",
    }


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    l2 = math.sqrt(sum(float(a) * float(a) for a in left))
    r2 = math.sqrt(sum(float(b) * float(b) for b in right))
    if l2 == 0.0 or r2 == 0.0:
        raise GuardFsvError("zero_norm_calibration_vector")
    return max(-1.0, min(1.0, dot / (l2 * r2)))


def score_summary(good_scores: list[float], bad_scores: list[float]) -> dict[str, Any]:
    return {
        "good_count": len(good_scores),
        "bad_count": len(bad_scores),
        "good_min": min(good_scores),
        "good_max": max(good_scores),
        "bad_min": min(bad_scores),
        "bad_max": max(bad_scores),
    }


def build_guard_vault(work_dir: Path, rows_path: Path, row_count: int) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", VAULT_NAME, "--panel-template", "text-default"], env, "create_vault_failed")
    created = json.loads(create.stdout)
    vault_path = home / "vaults" / created["vault_id"]
    slot_map = add_soccer_lenses(env, VAULT_NAME)
    ingest = run_ok(["ingest", VAULT_NAME, "--batch", str(rows_path.relative_to(ROOT)), "--output", "rows"], env, "ingest_failed", timeout=600)
    ingest_rows = [json.loads(line) for line in ingest.stdout.decode("utf-8").splitlines() if line.strip()]
    if len(ingest_rows) != row_count or not all(row.get("new") for row in ingest_rows):
        raise GuardFsvError("ingest_row_count_mismatch", {"observed": len(ingest_rows), "expected": row_count})
    cx = run_ok(
        [
            "readback",
            "cx-list",
            "--vault",
            str(vault_path),
            "--include-slots",
            "--limit",
            str(row_count),
            "--rebuild-base-page-index",
        ],
        env,
        "cx_list_failed",
        timeout=600,
    )
    cx_rows = json.loads(cx.stdout)
    slot_counts = anchored.inspect_slots(cx_rows, slot_map)
    return {
        "vault_id": created["vault_id"],
        "vault_path": str(vault_path.relative_to(ROOT)),
        "slot_map": slot_map,
        "ingest_rows": len(ingest_rows),
        "cx_list_rows": len(cx_rows),
        "slot_counts": slot_counts,
        "sample_cx_ids": [row["cx_id"] for row in cx_rows[:2]],
    }


def add_soccer_lenses(env: dict[str, str], vault_name: str) -> dict[str, int]:
    slot_map = {}
    for name, (path, dim) in anchored.FACETS.items():
        add = run_ok(
            [
                "add-lens",
                vault_name,
                "--name",
                f"team_{name}",
                "--runtime",
                "external-cmd",
                "--endpoint",
                str(path),
                "--shape",
                f"Dense({dim})",
                "--modality",
                "text",
            ],
            env,
            "add_lens_failed",
        )
        slot_map[name] = int(json.loads(add.stdout)["slot_id"])
    return slot_map


def verify_command_report(report: dict[str, Any], calibration: dict[str, Any]) -> None:
    if report.get("domain") != DOMAIN:
        raise GuardFsvError("guard_domain_mismatch", {"report": report})
    if report.get("calibration_corpus_size") != calibration["rows"]:
        raise GuardFsvError("guard_corpus_size_mismatch", {"report": report, "calibration": calibration["rows"]})
    if report.get("far", 1.0) > TARGET_FAR + 1e-6:
        raise GuardFsvError("guard_far_above_target", {"report": report})
    per_slot = report.get("per_slot_tau") or []
    observed = {int(row["slot"]): float(row["tau"]) for row in per_slot}
    if set(observed) != set(CALIBRATION_SLOTS.values()):
        raise GuardFsvError("guard_per_slot_tau_mismatch", {"observed": observed, "expected": CALIBRATION_SLOTS})
    if any(not math.isfinite(value) or value < -1.0 or value > 1.0001 for value in observed.values()):
        raise GuardFsvError("guard_tau_not_finite_or_out_of_range", {"observed": observed})


def read_guard_cf(env: dict[str, str], vault_path: Path) -> dict[str, Any]:
    proc = run_ok(["readback", "--cf", "guard", "--vault", str(vault_path)], env, "guard_cf_readback_failed")
    rows = []
    for line in proc.stdout.decode("utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) != 8 or parts[0] != "CF" or parts[1] != "guard" or parts[4] != "KEY" or parts[6] != "VALUE":
            raise GuardFsvError("malformed_guard_cf_line", {"line": line})
        value = bytes.fromhex(parts[7])
        rows.append(
            {
                "file": parts[3],
                "key_hex": parts[5],
                "value_sha256": anchored.sha256_bytes(value),
                "payload": json.loads(value),
            }
        )
    latest = {row["key_hex"]: row for row in rows}
    return {
        "stdout_sha256": anchored.sha256_bytes(proc.stdout),
        "raw_rows": len(rows),
        "unique_rows": len(latest),
        "latest": {key: latest[key] for key in sorted(latest)},
    }


def verify_guard_readback(readback: dict[str, Any], command_report: dict[str, Any]) -> dict[str, Any]:
    expected_keys = {"default": guard_key("default"), DOMAIN: guard_key(DOMAIN)}
    decoded: dict[str, Any] = {}
    for name, key_hex in expected_keys.items():
        row = readback["latest"].get(key_hex)
        if row is None:
            raise GuardFsvError("guard_profile_key_missing", {"name": name, "key_hex": key_hex, "available": sorted(readback["latest"])})
        payload = row["payload"]
        verify_profile_payload(name, payload, command_report)
        decoded[name] = {
            "key_hex": key_hex,
            "file": row["file"],
            "value_sha256": row["value_sha256"],
            "domain": payload["domain"],
            "panel_version": payload["panel_version"],
            "required_slots": payload["required_slots"],
            "tau": payload["tau"],
            "calibration": payload["calibration"],
        }
    if decoded["default"]["value_sha256"] != decoded[DOMAIN]["value_sha256"]:
        raise GuardFsvError("guard_default_domain_payload_mismatch", {"decoded": decoded})
    return {
        "raw_rows": readback["raw_rows"],
        "unique_rows": readback["unique_rows"],
        "stdout_sha256": readback["stdout_sha256"],
        "decoded_profiles": decoded,
    }


def verify_profile_payload(name: str, payload: dict[str, Any], command_report: dict[str, Any]) -> None:
    if payload.get("domain") != DOMAIN:
        raise GuardFsvError("guard_payload_domain_mismatch", {"name": name, "payload": payload})
    if payload.get("policy") not in {"AllRequired", "all_required"} or payload.get("novelty_action") not in {"RejectClosed", "reject_closed"}:
        raise GuardFsvError("guard_payload_policy_mismatch", {"name": name, "payload": payload})
    required_slots = [int(slot) for slot in payload.get("required_slots") or []]
    if required_slots != sorted(CALIBRATION_SLOTS.values()):
        raise GuardFsvError("guard_required_slots_mismatch", {"name": name, "required_slots": required_slots})
    tau = {int(slot): float(value) for slot, value in (payload.get("tau") or {}).items()}
    command_tau = {int(row["slot"]): float(row["tau"]) for row in command_report["per_slot_tau"]}
    if tau != command_tau:
        raise GuardFsvError("guard_tau_payload_mismatch", {"name": name, "payload_tau": tau, "command_tau": command_tau})
    calibration = payload.get("calibration") or {}
    per_slot = calibration.get("per_slot") or {}
    if sorted(int(slot) for slot in per_slot) != sorted(CALIBRATION_SLOTS.values()):
        raise GuardFsvError("guard_per_slot_meta_mismatch", {"name": name, "per_slot": per_slot})
    if calibration.get("far", 1.0) > TARGET_FAR + 1e-6 or calibration.get("estimator") != "conformal_quantile_v1":
        raise GuardFsvError("guard_calibration_meta_mismatch", {"name": name, "calibration": calibration})


def run_guard_checks(env: dict[str, str], vault_path: Path, sample_cx_ids: list[str]) -> dict[str, Any]:
    if len(sample_cx_ids) < 2:
        raise GuardFsvError("not_enough_sample_cx_ids")
    same = run_ok(["guard", VAULT_NAME, "check", "--cx", sample_cx_ids[0], "--identity-cx", sample_cx_ids[0]], env, "guard_check_same_failed")
    cross = run(["guard", VAULT_NAME, "check", "--cx", sample_cx_ids[1], "--identity-cx", sample_cx_ids[0]], env, timeout=60)
    return {
        "same_identity": json.loads(same.stdout),
        "same_identity_stdout_sha256": anchored.sha256_bytes(same.stdout),
        "cross_identity_returncode": cross.returncode,
        "cross_identity_stdout_sha256": anchored.sha256_bytes(cross.stdout),
        "cross_identity_stderr_sha256": anchored.sha256_bytes(cross.stderr),
        "graph_source": str(vault_path.relative_to(ROOT)),
    }


def guard_physical_readback(vault_path: Path) -> dict[str, Any]:
    stats = anchored.physical_readback(vault_path)
    files = sorted((vault_path / "cf" / "guard").glob("*.sst"))
    if not files:
        raise GuardFsvError("missing_guard_sst", {"vault": str(vault_path.relative_to(ROOT))})
    stats["cf"]["guard"] = {
        "sst_count": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "sha256_first": anchored.sha256_bytes(files[0].read_bytes()),
        "sha256_last": anchored.sha256_bytes(files[-1].read_bytes()),
    }
    return stats


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    happy = synthetic_happy(work_dir / "happy")
    edges = {
        "empty_set": synthetic_bad_case(work_dir / "empty_set", []),
        "insufficient_bad_scores": synthetic_bad_case(work_dir / "insufficient_bad_scores", [{"slot": 8, "score": 1.0, "class": "good"}] * 60 + [{"slot": 8, "score": 0.1, "class": "injection"}] * 49),
        "unknown_slot": synthetic_bad_case(work_dir / "unknown_slot", calibration_rows(999, 60, 60)),
        "sparse_slot": synthetic_bad_case(work_dir / "sparse_slot", calibration_rows(1, 60, 60)),
    }
    return {"happy": happy, "edges": edges}


def synthetic_happy(work_dir: Path) -> dict[str, Any]:
    env, vault_path = synthetic_vault(work_dir)
    rows = calibration_rows(8, 60, 60)
    path = write_jsonl(work_dir / "happy.jsonl", rows)
    proc = run_ok(["guard", "guard-edge", "calibrate", "--domain", "synthetic.guard", "--set", str(path.relative_to(ROOT)), "--target-far", str(TARGET_FAR)], env, "synthetic_guard_happy_failed")
    report = json.loads(proc.stdout)
    readback = read_guard_cf(env, vault_path)
    decoded = verify_synthetic_guard_readback(readback, "synthetic.guard", report)
    return {
        "vault_path": str(vault_path.relative_to(ROOT)),
        "report": report,
        "readback": decoded,
        "calibration_file": file_readback(path),
    }


def synthetic_bad_case(work_dir: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    env, vault_path = synthetic_vault(work_dir)
    path = write_jsonl(work_dir / "bad.jsonl", rows)
    before = guard_unique_count(env, vault_path)
    proc = run(["guard", "guard-edge", "calibrate", "--domain", "synthetic.guard", "--set", str(path.relative_to(ROOT)), "--target-far", str(TARGET_FAR)], env, timeout=60)
    after = guard_unique_count(env, vault_path)
    if proc.returncode == 0:
        raise GuardFsvError("synthetic_bad_case_passed", {"path": str(path), "stdout": proc.stdout.decode("utf-8", "replace")})
    if before != after:
        raise GuardFsvError("synthetic_bad_case_wrote_guard", {"before": before, "after": after})
    return {
        "returncode": proc.returncode,
        "before_guard_rows": before,
        "after_guard_rows": after,
        "stdout_fragment": proc.stdout.decode("utf-8", "replace")[-240:],
        "stderr_sha256": anchored.sha256_bytes(proc.stderr),
    }


def synthetic_vault(work_dir: Path) -> tuple[dict[str, str], Path]:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    home = work_dir / "calyx_home"
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", "guard-edge", "--panel-template", "text-default"], env, "synthetic_create_failed")
    vault_path = home / "vaults" / json.loads(create.stdout)["vault_id"]
    add = run_ok(
        [
            "add-lens",
            "guard-edge",
            "--name",
            "facet_guard",
            "--runtime",
            "external-cmd",
            "--endpoint",
            str(ROOT / "tools/lenses/soccer_lab/team_match/attack"),
            "--shape",
            "Dense(6)",
            "--modality",
            "text",
        ],
        env,
        "synthetic_add_lens_failed",
    )
    if json.loads(add.stdout)["slot_id"] != 8:
        raise GuardFsvError("synthetic_slot_unexpected", {"stdout": add.stdout.decode()})
    return env, vault_path


def calibration_rows(slot: int, good_count: int, bad_count: int) -> list[dict[str, Any]]:
    rows = [{"slot": slot, "score": 1.0, "class": "good"} for _ in range(good_count)]
    rows.extend({"slot": slot, "score": 0.1, "class": "injection"} for _ in range(bad_count))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return path


def verify_synthetic_guard_readback(readback: dict[str, Any], domain: str, report: dict[str, Any]) -> dict[str, Any]:
    expected_keys = {"default": guard_key("default"), domain: guard_key(domain)}
    out = {}
    for name, key in expected_keys.items():
        row = readback["latest"].get(key)
        if row is None:
            raise GuardFsvError("synthetic_guard_key_missing", {"key": key})
        out[name] = {"key_hex": key, "value_sha256": row["value_sha256"], "tau": row["payload"]["tau"]}
    if out["default"]["value_sha256"] != out[domain]["value_sha256"]:
        raise GuardFsvError("synthetic_guard_default_mismatch", {"out": out})
    if report["per_slot_tau"][0]["slot"] != 8:
        raise GuardFsvError("synthetic_guard_report_slot_mismatch", {"report": report})
    return out


def guard_unique_count(env: dict[str, str], vault_path: Path) -> int:
    proc = run(["readback", "--cf", "guard", "--vault", str(vault_path)], env, timeout=60)
    if proc.returncode != 0:
        return 0
    return len(read_guard_cf(env, vault_path)["latest"])


def file_readback(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": len(data),
        "sha256": anchored.sha256_bytes(data),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW.relative_to(ROOT)))
    parser.add_argument("--out", default=str(DEFAULT_OUT.relative_to(ROOT)))
    return parser.parse_args()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    raw_root = resolve(args.raw_root)
    report_path = resolve(args.out)
    work_dir = report_path.parent
    real = build_real_guard(work_dir, raw_root)
    synthetic = synthetic_edges(work_dir / "synthetic_edges")
    report = {"status": "ok", "real": real, "synthetic": synthetic}
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    if report_path.read_text(encoding="utf-8") != encoded + "\n":
        raise GuardFsvError("report_readback_mismatch", {"path": str(report_path.relative_to(ROOT))})
    print(
        json.dumps(
            {
                "status": "ok",
                "vault_id": real["vault"]["vault_id"],
                "guard_rows": real["guard_cf_readback"]["unique_rows"],
                "domain": DOMAIN,
                "required_slots": real["guard_cf_readback"]["decoded_profiles"]["default"]["required_slots"],
                "tau": real["command_report"]["tau"],
                "synthetic_edges": sorted(synthetic["edges"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
