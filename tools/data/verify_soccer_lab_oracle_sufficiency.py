#!/usr/bin/env python3
"""Record Soccer Lab Oracle sufficiency verdicts from physical Assay readbacks."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import verify_soccer_lab_bits_assay as bits_assay
import verify_soccer_lab_oracle_context_ingest as oracle_context


ROOT = oracle_context.ROOT
CALYX = oracle_context.CALYX
DEFAULT_RAW = oracle_context.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "oracle_sufficiency" / "report.json"
DEFAULT_VERDICTS_OUT = ROOT / "docs" / "data" / "soccer_lab_oracle_sufficiency_verdicts.json"

MEASURED_AXES = {
    "soccer_lab.match_result": "label:match_result",
    "soccer_lab.team_match_result": "label:team_match_result",
}

REAL_OUTCOME_AXES = (
    "soccer_lab.match_result",
    "soccer_lab.team_match_result",
    "soccer_lab.tournament_winner",
)


class OracleSufficiencyError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def require(condition: bool, reason: str, detail: dict[str, Any] | None = None) -> None:
    if not condition:
        raise OracleSufficiencyError(reason, detail)


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise OracleSufficiencyError(
            reason,
            {
                "args": args,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def entropy_bits(counts: dict[str, int]) -> float:
    total = sum(counts.values())
    require(total > 0, "empty_outcome_counts", {"counts": counts})
    entropy = 0.0
    for count in counts.values():
        if count:
            p = count / total
            entropy -= p * math.log2(p)
    return entropy


def outcome_counts_by_domain(full_outcome_counts: dict[str, int]) -> dict[str, dict[str, int]]:
    by_domain: dict[str, dict[str, int]] = {}
    for key, count in full_outcome_counts.items():
        domain, outcome = key.split("=", 1)
        by_domain.setdefault(domain, {})[outcome] = count
    return {domain: dict(sorted(counts.items())) for domain, counts in sorted(by_domain.items())}


def build_sufficiency_vault(work_dir: Path) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    vault_name = "soccer-oracle-sufficiency"
    create = run_ok(["create-vault", vault_name, "--panel-template", "text-default"], env, "create_vault_failed")
    created = json.loads(create.stdout)
    vault_id = created["vault_id"]
    vault_path = home / "vaults" / vault_id
    return {
        "env": env,
        "vault_name": vault_name,
        "vault_id": vault_id,
        "vault_path": vault_path,
        "vault_salt": oracle_context.vault_salt(vault_id, vault_name),
        "create_stdout_sha256": oracle_context.sha256_bytes(create.stdout),
    }


def slot_from_bits(slot: dict[str, Any], axis: str) -> dict[str, Any]:
    slot_id = int(slot["slot"])
    return {
        "slot_id": slot_id,
        "slot_key": {"id": slot_id, "key": f"{axis}:{slot['name']}"},
        "lens_id": ("%02x" % ((slot_id % 240) + 1)) * 16,
        "shape": {"dense": 2},
        "modality": "text",
        "asymmetry": "none",
        "quant": "none",
        "axis": axis,
        "retrieval_only": False,
        "excluded_from_dedup": False,
        "bits_about": {},
        "state": "active",
        "added_at_panel_version": 641,
    }


def panel_from_bits(axis: str, per_slot: list[dict[str, Any]], version: int) -> dict[str, Any]:
    return {
        "version": version,
        "slots": [slot_from_bits(slot, axis) for slot in per_slot],
        "created_at": 1783132200,
        "kernel_ref": None,
        "guard_ref": None,
    }


def zero_panel(domain: str, version: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    per_slot = [
        {"slot": 0, "name": "missing_tournament_lens", "bits": 0.0, "low_signal": True},
        {"slot": 1, "name": "missing_market_grounding", "bits": 0.0, "low_signal": True},
    ]
    return panel_from_bits(domain, per_slot, version), [{"slot": slot["slot"], "bits": 0.0} for slot in per_slot]


def assay_rows(vault_path: Path, env: dict[str, str]) -> dict[str, Any]:
    proc = run(["readback", "--cf", "assay", "--vault", str(vault_path)], env, timeout=120)
    if proc.returncode != 0:
        return {"raw_rows": 0, "unique_keys": 0, "stdout_sha256": oracle_context.sha256_bytes(proc.stdout)}
    rows = bits_assay.decode_assay_cf(proc.stdout.decode("utf-8"))
    unique = bits_assay.latest_by_key(rows)
    return {
        "raw_rows": len(rows),
        "unique_keys": len(unique),
        "stdout_sha256": oracle_context.sha256_bytes(proc.stdout),
    }


def sufficiency_physical_readback(vault_path: Path) -> dict[str, Any]:
    required = {
        "MANIFEST": vault_path / "MANIFEST",
        "CURRENT": vault_path / "CURRENT",
        "wal": vault_path / "wal" / "00000000000000000000.wal",
    }
    files = {}
    for name, path in required.items():
        require(path.is_file(), "physical_file_missing", {"name": name, "path": str(path.relative_to(ROOT))})
        files[name] = oracle_context.file_stat(path)
    panel_files = sorted((vault_path / "panel").glob("panel-*.json"))
    registry_files = sorted((vault_path / "registry").glob("registry-*.json"))
    require(panel_files, "physical_panel_missing", {"vault": str(vault_path.relative_to(ROOT))})
    require(registry_files, "physical_registry_missing", {"vault": str(vault_path.relative_to(ROOT))})
    files["panel"] = oracle_context.file_stat(panel_files[-1])
    files["registry"] = oracle_context.file_stat(registry_files[-1])
    cf_stats = {}
    for cf_name in ["assay", "time_index"]:
        ssts = sorted((vault_path / "cf" / cf_name).glob("*.sst"))
        require(ssts, "physical_cf_missing", {"cf": cf_name, "vault": str(vault_path.relative_to(ROOT))})
        cf_stats[cf_name] = {
            "sst_count": len(ssts),
            "bytes": sum(path.stat().st_size for path in ssts),
            "first_sha256": oracle_context.sha256_bytes(ssts[0].read_bytes()),
            "last_sha256": oracle_context.sha256_bytes(ssts[-1].read_bytes()),
        }
    return {"files": files, "cf": cf_stats}


def write_fixture(path: Path, fixture: dict[str, Any]) -> None:
    path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    require(json.loads(path.read_text(encoding="utf-8")) == fixture, "fixture_readback_mismatch", {"path": str(path.relative_to(ROOT))})


def run_sufficiency(
    work_dir: Path,
    vault: dict[str, Any],
    domain: str,
    fixture: dict[str, Any],
    expect_sufficient: bool,
) -> dict[str, Any]:
    fixture_path = work_dir / f"oracle-sufficiency-{domain.rsplit('.', 1)[-1]}.json"
    write_fixture(fixture_path, fixture)
    before = assay_rows(vault["vault_path"], vault["env"])
    proc = run(
        [
            "readback",
            "oracle_sufficiency",
            "--vault",
            str(vault["vault_path"]),
            "--fixture",
            str(fixture_path.relative_to(ROOT)),
            "--vault-id",
            vault["vault_id"],
            "--salt",
            vault["vault_salt"],
        ],
        vault["env"],
        timeout=180,
    )
    after = assay_rows(vault["vault_path"], vault["env"])
    stdout = proc.stdout.decode("utf-8", "replace")
    payload = json.loads(stdout)
    bound = payload.get("bound")
    require(bound is not None, "sufficiency_missing_bound", {"domain": domain, "payload": payload})
    require(bound.get("sufficient") is expect_sufficient, "sufficiency_verdict_mismatch", {"domain": domain, "payload": payload, "expected": expect_sufficient})
    if expect_sufficient:
        require(proc.returncode == 0, "sufficiency_expected_success_failed", {"domain": domain, "payload": payload, "stderr": proc.stderr.decode("utf-8", "replace")})
    else:
        require(proc.returncode != 0, "sufficiency_expected_insufficient_passed", {"domain": domain, "payload": payload})
        require(payload.get("error_code") == "CALYX_ORACLE_INSUFFICIENT", "sufficiency_wrong_error", {"domain": domain, "payload": payload})
    require(after["raw_rows"] >= before["raw_rows"] + int(payload["assay_rows_written"]), "assay_rows_not_physically_written", {"domain": domain, "before": before, "after": after, "payload": payload})
    return {
        "fixture": oracle_context.file_stat(fixture_path),
        "returncode": proc.returncode,
        "stdout_sha256": oracle_context.sha256_bytes(proc.stdout),
        "stderr_sha256": oracle_context.sha256_bytes(proc.stderr),
        "assay_before": before,
        "assay_after": after,
        "payload": payload,
    }


def real_axis_fixture(
    domain: str,
    outcome_counts: dict[str, int],
    panel_bits: float,
    per_slot_bits: list[dict[str, Any]],
    version: int,
) -> dict[str, Any]:
    return {
        "domain": domain,
        "panel": panel_from_bits(domain, per_slot_bits, version),
        "I_panel_oracle": panel_bits,
        "outcome_entropy_bits": entropy_bits(outcome_counts),
        "slot_bits": [{"slot": slot["slot"], "bits": slot["bits"]} for slot in per_slot_bits],
        "n_samples": 150,
        "trust": "trusted",
        "clock_ts": 1783132200,
    }


def record_real_axes(
    work_dir: Path,
    vault: dict[str, Any],
    generated: dict[str, Any],
    bits_report: dict[str, Any],
) -> dict[str, Any]:
    counts_by_domain = outcome_counts_by_domain(generated["full_outcome_counts"])
    verdicts: dict[str, Any] = {}
    for idx, domain in enumerate(REAL_OUTCOME_AXES):
        counts = counts_by_domain[domain]
        h_bits = entropy_bits(counts)
        measured_axis = MEASURED_AXES.get(domain)
        if measured_axis:
            command = bits_report["bits"]["commands"][measured_axis]
            panel_bits = float(command["report"]["panel_sufficiency"])
            per_slot = command["per_slot_bits"]
            fixture = real_axis_fixture(domain, counts, panel_bits, per_slot, 641 + idx)
            evidence = run_sufficiency(work_dir, vault, domain, fixture, expect_sufficient=False)
            source = {
                "kind": "measured_bits_assay",
                "axis": measured_axis,
                "bits_report_sha256": bits_report["report_file"]["sha256"],
            }
        else:
            panel, slot_bits = zero_panel(domain, 641 + idx)
            panel_bits = 0.0
            fixture = {
                "domain": domain,
                "panel": panel,
                "I_panel_oracle": panel_bits,
                "outcome_entropy_bits": h_bits,
                "slot_bits": slot_bits,
                "n_samples": 192,
                "trust": "trusted",
                "clock_ts": 1783132200,
            }
            evidence = run_sufficiency(work_dir, vault, domain, fixture, expect_sufficient=False)
            source = {"kind": "no_measured_panel_bits", "reason": "tournament winner has grounded outcomes but no assayed Oracle panel yet"}
        bound = evidence["payload"]["bound"]
        verdicts[domain] = {
            "outcome_counts": counts,
            "outcome_entropy_bits": h_bits,
            "I_panel_oracle": panel_bits,
            "sufficient_by_floor": panel_bits >= h_bits,
            "deficit_bits": max(0.0, h_bits - panel_bits),
            "source": source,
            "readback": evidence,
            "bound": bound,
        }
        require(
            math.isclose(float(bound["I_panel_oracle"]), panel_bits, rel_tol=1e-6, abs_tol=1e-8),
            "bound_panel_bits_mismatch",
            {"domain": domain, "bound": bound, "panel_bits": panel_bits},
        )
        require(bound["sufficient"] is False, "real_axis_unexpectedly_sufficient", {"domain": domain, "bound": bound})
    verdicts["soccer_lab.player_impact"] = {
        "status": "not_run_no_grounded_outcomes",
        "sufficient_by_floor": False,
        "reason": "no generated Soccer Lab player-impact Oracle rows or outcome anchors exist yet; running a zero-entropy fixture would falsely pass the honesty gate",
    }
    return verdicts


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    vault = build_sufficiency_vault(work_dir)
    happy_fixture = {
        "domain": "synthetic.sufficient",
        "panel": oracle_context.minimal_panel(),
        "I_panel_oracle": 1.25,
        "outcome_entropy_bits": 1.0,
        "slot_bits": [{"slot": 0, "bits": 0.65}, {"slot": 1, "bits": 0.60}],
        "n_samples": 120,
        "trust": "trusted",
        "clock_ts": 1783132200,
    }
    insufficient_fixture = {
        "domain": "synthetic.insufficient",
        "panel": oracle_context.minimal_panel(),
        "I_panel_oracle": 0.25,
        "outcome_entropy_bits": 1.0,
        "slot_bits": [{"slot": 0, "bits": 0.20}, {"slot": 1, "bits": 0.05}],
        "n_samples": 120,
        "trust": "trusted",
        "clock_ts": 1783132200,
    }
    happy = run_sufficiency(work_dir, vault, "synthetic.sufficient", happy_fixture, expect_sufficient=True)
    insufficient = run_sufficiency(work_dir, vault, "synthetic.insufficient", insufficient_fixture, expect_sufficient=False)

    bad_fixture = dict(happy_fixture)
    bad_fixture["domain"] = "synthetic.invalid_bits"
    bad_fixture["I_panel_oracle"] = -0.01
    bad_path = work_dir / "oracle-sufficiency-invalid-bits.json"
    write_fixture(bad_path, bad_fixture)
    before = assay_rows(vault["vault_path"], vault["env"])
    proc = run(
        [
            "readback",
            "oracle_sufficiency",
            "--vault",
            str(vault["vault_path"]),
            "--fixture",
            str(bad_path.relative_to(ROOT)),
            "--vault-id",
            vault["vault_id"],
            "--salt",
            vault["vault_salt"],
        ],
        vault["env"],
        timeout=60,
    )
    after = assay_rows(vault["vault_path"], vault["env"])
    require(proc.returncode != 0, "invalid_bits_fixture_passed")
    require(before == after, "invalid_bits_wrote_assay", {"before": before, "after": after})
    return {
        "vault_path": str(vault["vault_path"].relative_to(ROOT)),
        "happy_sufficient": happy,
        "known_insufficient": insufficient,
        "invalid_bits_no_write": {
            "fixture": oracle_context.file_stat(bad_path),
            "returncode": proc.returncode,
            "stderr_sha256": oracle_context.sha256_bytes(proc.stderr),
            "assay_before": before,
            "assay_after": after,
            "stderr_tail": proc.stderr.decode("utf-8", "replace")[-500:],
        },
    }


def run_bits_report(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    report_path = work_dir / "bits_assay" / "report.json"
    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "tools" / "data" / "verify_soccer_lab_bits_assay.py"),
            "--raw-root",
            str(raw_root.relative_to(ROOT)),
            "--out",
            str(report_path.relative_to(ROOT)),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=420,
    )
    if proc.returncode != 0:
        raise OracleSufficiencyError(
            "bits_assay_verifier_failed",
            {
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["report_file"] = oracle_context.file_stat(report_path)
    report["verifier_stdout_sha256"] = oracle_context.sha256_bytes(proc.stdout)
    report["verifier_stderr_sha256"] = oracle_context.sha256_bytes(proc.stderr)
    return report


def write_verdict_manifest(path: Path, report_path: Path, verdicts: dict[str, Any]) -> dict[str, Any]:
    manifest = {
        "schema_version": 1,
        "verified_at": "2026-07-04",
        "verifier": "tools/data/verify_soccer_lab_oracle_sufficiency.py",
        "source_report": {
            "path": str(report_path.relative_to(ROOT)),
            "sha256": oracle_context.sha256_bytes(report_path.read_bytes()),
        },
        "verdicts": {},
    }
    for domain, verdict in verdicts.items():
        if verdict.get("status") == "not_run_no_grounded_outcomes":
            manifest["verdicts"][domain] = {
                "I_panel_oracle": None,
                "outcome_entropy_bits": None,
                "panel_bits_gte_outcome_entropy": False,
                "deficit_bits": None,
                "status": verdict["status"],
            }
            continue
        source_kind = verdict.get("source", {}).get("kind")
        status = "insufficient_no_measured_panel_bits" if source_kind == "no_measured_panel_bits" else "insufficient"
        manifest["verdicts"][domain] = {
            "I_panel_oracle": verdict["I_panel_oracle"],
            "outcome_entropy_bits": verdict["outcome_entropy_bits"],
            "panel_bits_gte_outcome_entropy": verdict["sufficient_by_floor"],
            "deficit_bits": verdict["deficit_bits"],
            "status": status,
        }
    encoded = json.dumps(manifest, indent=2, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded + "\n", encoding="utf-8")
    require(path.read_text(encoding="utf-8") == encoded + "\n", "verdict_manifest_readback_mismatch", {"path": str(path.relative_to(ROOT))})
    return {"path": str(path.relative_to(ROOT)), "sha256": oracle_context.sha256_bytes(path.read_bytes())}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW.relative_to(ROOT)))
    parser.add_argument("--out", default=str(DEFAULT_OUT.relative_to(ROOT)))
    parser.add_argument("--verdicts-out", default=str(DEFAULT_VERDICTS_OUT.relative_to(ROOT)))
    return parser.parse_args()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    raw_root = resolve(args.raw_root)
    report_path = resolve(args.out)
    verdicts_path = resolve(args.verdicts_out)
    work_dir = report_path.parent
    rows_root = work_dir / "rows"
    generated = oracle_context.generate_rows(raw_root, rows_root)
    bits_report = run_bits_report(work_dir, raw_root)
    vault = build_sufficiency_vault(work_dir / "real_sufficiency_vault")
    verdicts = record_real_axes(work_dir / "real_sufficiency_vault", vault, generated, bits_report)
    edges = synthetic_edges(work_dir / "synthetic_edges")
    real_vault_summary = {
        "vault_name": vault["vault_name"],
        "vault_id": vault["vault_id"],
        "vault_path": str(vault["vault_path"].relative_to(ROOT)),
        "vault_salt": vault["vault_salt"],
        "create_stdout_sha256": vault["create_stdout_sha256"],
        "assay_final": assay_rows(vault["vault_path"], vault["env"]),
        "physical_readback": sufficiency_physical_readback(vault["vault_path"]),
    }
    report = {
        "status": "ok",
        "generation": {key: value for key, value in generated.items() if key != "oracle_rows"},
        "bits_assay": {
            "report_file": bits_report["report_file"],
            "decoded_axes": bits_report["bits"]["decoded_axes"],
            "verifier_stdout_sha256": bits_report["verifier_stdout_sha256"],
            "verifier_stderr_sha256": bits_report["verifier_stderr_sha256"],
        },
        "real_sufficiency_vault": real_vault_summary,
        "verdicts": verdicts,
        "synthetic_edges": edges,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    require(report_path.read_text(encoding="utf-8") == encoded + "\n", "report_readback_mismatch", {"path": str(report_path.relative_to(ROOT))})
    verdict_manifest = write_verdict_manifest(verdicts_path, report_path, verdicts)
    summary = {
        "status": "ok",
        "report": str(report_path.relative_to(ROOT)),
        "verdict_manifest": verdict_manifest,
        "verdicts": {
            domain: {
                "I_panel_oracle": verdict.get("I_panel_oracle"),
                "outcome_entropy_bits": verdict.get("outcome_entropy_bits"),
                "sufficient_by_floor": verdict.get("sufficient_by_floor"),
                "status": verdict.get("status", "readback"),
            }
            for domain, verdict in verdicts.items()
        },
        "synthetic_edges": ["happy_sufficient", "invalid_bits_no_write", "known_insufficient"],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
