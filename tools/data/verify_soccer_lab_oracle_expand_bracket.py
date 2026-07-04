#!/usr/bin/env python3
"""Verify Soccer Lab oracle_expand bracket butterfly trees."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import verify_soccer_lab_oracle_context_ingest as oracle_context
import verify_soccer_lab_oracle_match_predictions as match_predictions


ROOT = oracle_context.ROOT
CALYX = oracle_context.CALYX
DEFAULT_RAW = oracle_context.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "oracle_expand_bracket" / "report.json"
DEFAULT_TREE_OUT = ROOT / "docs" / "data" / "soccer_lab_bracket_butterfly_tree.json"

DOMAIN = "soccer_lab.bracket_butterfly"
ROOT_ACTION = "match_104"
ROOT_OUTCOME = {"enum": "W104"}
DESIRED_OUTCOME = {"enum": "W86"}
RUN_DATE = "2026-07-04"
PLACEHOLDER = re.compile(r"^[WL](\d+)$")


class OracleExpandBracketError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def require(condition: bool, reason: str, detail: dict[str, Any] | None = None) -> None:
    if not condition:
        raise OracleExpandBracketError(reason, detail)


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise OracleExpandBracketError(
            reason,
            {
                "args": args,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def write_json(path: Path, payload: Any) -> dict[str, Any]:
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded + "\n", encoding="utf-8")
    require(path.read_text(encoding="utf-8") == encoded + "\n", "json_readback_mismatch", {"path": str(path.relative_to(ROOT))})
    return oracle_context.file_stat(path)


def create_vault(work_dir: Path, name: str) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", name, "--panel-template", "text-default"], env, f"create_{name}_failed")
    created = json.loads(create.stdout)
    vault_id = created["vault_id"]
    return {
        "env": env,
        "vault_name": name,
        "vault_id": vault_id,
        "vault_path": home / "vaults" / vault_id,
        "vault_salt": oracle_context.vault_salt(vault_id, name),
        "create_stdout_sha256": oracle_context.sha256_bytes(create.stdout),
    }


def cf_rows(vault_path: Path, env: dict[str, str], cf: str) -> dict[str, Any]:
    return match_predictions.cf_rows(vault_path, env, cf)


def physical_readback(vault_path: Path) -> dict[str, Any]:
    required = {
        "MANIFEST": vault_path / "MANIFEST",
        "CURRENT": vault_path / "CURRENT",
        "wal": vault_path / "wal" / "00000000000000000000.wal",
    }
    files = {}
    for name, path in required.items():
        require(path.is_file(), "physical_file_missing", {"name": name, "path": str(path.relative_to(ROOT))})
        files[name] = oracle_context.file_stat(path)
    ledger_head = vault_path / "ledger_head" / "current.json"
    if ledger_head.exists():
        files["ledger_head"] = oracle_context.file_stat(ledger_head)
    cf_stats = {}
    for cf_name in ["base", "recurrence", "time_index"]:
        ssts = sorted((vault_path / "cf" / cf_name).glob("*.sst"))
        if not ssts:
            continue
        cf_stats[cf_name] = {
            "sst_count": len(ssts),
            "bytes": sum(path.stat().st_size for path in ssts),
            "first_sha256": oracle_context.sha256_bytes(ssts[0].read_bytes()),
            "last_sha256": oracle_context.sha256_bytes(ssts[-1].read_bytes()),
        }
    require("base" in cf_stats and "recurrence" in cf_stats, "missing_expand_cfs", {"cf": sorted(cf_stats)})
    return {"files": files, "cf": cf_stats}


def recurrence_context_summary(vault_path: Path, env: dict[str, str]) -> dict[str, Any]:
    proc = run_ok(["readback", "--cf", "recurrence", "--vault", str(vault_path)], env, "recurrence_cf_failed")
    actions: dict[str, int] = {}
    consequences: dict[str, int] = {}
    raw_rows = 0
    for line in proc.stdout.decode("utf-8").splitlines():
        if not line.strip():
            continue
        raw_rows += 1
        parts = line.split("\t")
        value_hex = parts[parts.index("VALUE") + 1]
        row = json.loads(bytes.fromhex(value_hex).decode("utf-8"))
        context = json.loads(bytes(row["context"]["bytes"]).decode("utf-8"))
        action = context.get("action") or context.get("action_id")
        if action:
            actions[action] = actions.get(action, 0) + 1
        for consequence in context.get("consequences", []):
            key = f"{action}->{consequence.get('action_or_event')}:{json.dumps(consequence.get('outcome', {}).get('value'), sort_keys=True)}"
            consequences[key] = consequences.get(key, 0) + 1
    return {
        "raw_rows": raw_rows,
        "actions": dict(sorted(actions.items())),
        "consequences": dict(sorted(consequences.items())),
        "stdout_sha256": oracle_context.sha256_bytes(proc.stdout),
    }


def read_openfootball(raw_root: Path) -> tuple[Path, dict[int, dict[str, Any]]]:
    path = raw_root / "openfootball" / "2026" / "worldcup.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("matches")
    require(isinstance(rows, list) and len(rows) == 104, "openfootball_match_count_mismatch", {"path": str(path.relative_to(ROOT)), "rows": len(rows) if isinstance(rows, list) else None})
    by_num = {}
    for row in rows:
        if "num" not in row:
            continue
        num = int(row["num"])
        by_num[num] = row
    require(all(num in by_num for num in range(73, 105)), "numbered_knockout_rows_missing", {"available": sorted(by_num)})
    return path, by_num


def dependencies(row: dict[str, Any]) -> list[tuple[int, str]]:
    out = []
    for side in ["team1", "team2"]:
        value = str(row.get(side, ""))
        match = PLACEHOLDER.match(value)
        if match:
            out.append((int(match.group(1)), value))
    return out


def bracket_fixture(raw_root: Path) -> dict[str, Any]:
    _, rows = read_openfootball(raw_root)
    reachable: set[int] = set()

    def visit(num: int) -> None:
        if num in reachable:
            return
        reachable.add(num)
        for child, token in dependencies(rows[num]):
            if token.startswith("W"):
                visit(child)

    visit(104)
    edges = []
    for num in sorted(reachable):
        for child, token in dependencies(rows[num]):
            if child in rows and token.startswith("W"):
                edges.append(
                    {
                        "from": f"match_{num:03d}",
                        "to": f"match_{child:03d}",
                        "outcome": {"enum": token},
                    }
                )
    return {
        "domain": DOMAIN,
        "root_action": ROOT_ACTION,
        "root_outcome": ROOT_OUTCOME,
        "root_confidence": 1.0,
        "root_hop": 0,
        "desired_outcome": DESIRED_OUTCOME,
        "clock_ts": 1783132200,
        "edges": edges,
    }


def flat_hop_counts(flat: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in flat:
        hop = str(item["hop"])
        counts[hop] = counts.get(hop, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: int(item[0])))


def flat_confidences(flat: list[dict[str, Any]]) -> dict[str, float]:
    observed: dict[str, float] = {}
    for item in flat:
        hop = str(item["hop"])
        observed.setdefault(hop, item["confidence"])
    return dict(sorted(observed.items(), key=lambda item: int(item[0])))


def assert_tree(payload: dict[str, Any], expected_depth: int, expected_hops: dict[str, int], selected: str | None = None) -> None:
    require(payload["requested_depth"] == expected_depth, "requested_depth_mismatch", {"payload": payload["requested_depth"], "expected": expected_depth})
    require(payload["max_depth"] == 4, "max_depth_mismatch", {"payload": payload["max_depth"]})
    require(abs(payload["hop_attenuation"] - 0.7) < 1e-6, "hop_attenuation_mismatch", {"payload": payload["hop_attenuation"]})
    require(abs(payload["min_confidence_threshold"] - 0.05) < 1e-6, "threshold_mismatch", {"payload": payload["min_confidence_threshold"]})
    require(flat_hop_counts(payload["flat"]) == expected_hops, "hop_counts_mismatch", {"observed": flat_hop_counts(payload["flat"]), "expected": expected_hops})
    for hop, confidence in flat_confidences(payload["flat"]).items():
        expected = round(0.7 ** int(hop), 7)
        require(abs(confidence - expected) < 1e-6, "confidence_mismatch", {"hop": hop, "confidence": confidence, "expected": expected})
    if selected is not None:
        require(payload.get("selected", {}).get("action_or_event") == selected, "selected_mismatch", {"selected": payload.get("selected"), "expected": selected})


def run_expand(work_dir: Path, vault: dict[str, Any], fixture: dict[str, Any], name: str, depth: int = 4, expect_success: bool = True) -> dict[str, Any]:
    fixture_path = work_dir / f"{name}.json"
    fixture_stat = write_json(fixture_path, fixture)
    before = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["base", "recurrence"]}
    ledger_before = oracle_context.file_stat(vault["vault_path"] / "ledger_head" / "current.json") if (vault["vault_path"] / "ledger_head" / "current.json").exists() else None
    proc = run(
        [
            "readback",
            "oracle_expand",
            "--vault",
            str(vault["vault_path"]),
            "--fixture",
            str(fixture_path.relative_to(ROOT)),
            "--vault-id",
            vault["vault_id"],
            "--salt",
            vault["vault_salt"],
            "--depth",
            str(depth),
        ],
        vault["env"],
        timeout=180,
    )
    payload = json.loads(proc.stdout.decode("utf-8", "replace")) if proc.stdout else {}
    after = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["base", "recurrence"]}
    ledger_after = oracle_context.file_stat(vault["vault_path"] / "ledger_head" / "current.json") if (vault["vault_path"] / "ledger_head" / "current.json").exists() else None
    if expect_success:
        require(proc.returncode == 0, "oracle_expand_expected_success_failed", {"payload": payload, "stderr": proc.stderr.decode("utf-8", "replace")})
        require(payload["rows_written"] == len(fixture["edges"]) * 2, "rows_written_mismatch", {"payload": payload["rows_written"], "edges": len(fixture["edges"])})
        require(after["base"]["raw_rows"] - before["base"]["raw_rows"] == payload["rows_written"], "base_row_delta_mismatch", {"before": before, "after": after, "payload": payload["rows_written"]})
        require(after["recurrence"]["raw_rows"] - before["recurrence"]["raw_rows"] == payload["rows_written"], "recurrence_row_delta_mismatch", {"before": before, "after": after, "payload": payload["rows_written"]})
        require(ledger_after is not None, "oracle_expand_missing_ledger_head")
    else:
        require(proc.returncode != 0, "oracle_expand_expected_failure_passed", {"payload": payload})
    return {
        "fixture": fixture_stat,
        "returncode": proc.returncode,
        "stdout_sha256": oracle_context.sha256_bytes(proc.stdout),
        "stderr_sha256": oracle_context.sha256_bytes(proc.stderr),
        "stderr_tail": proc.stderr.decode("utf-8", "replace")[-500:],
        "before": before,
        "after": after,
        "ledger_before": ledger_before,
        "ledger_after": ledger_after,
        "payload": payload,
    }


def real_bracket(work_dir: Path, raw_root: Path) -> dict[str, Any]:
    source_path, rows = read_openfootball(raw_root)
    fixture = bracket_fixture(raw_root)
    require(len(fixture["edges"]) == 17, "real_edge_count_mismatch", {"edges": fixture["edges"]})
    vault = create_vault(work_dir, "soccer-oracle-expand-bracket")
    readback = run_expand(work_dir, vault, fixture, "oracle-expand-bracket", depth=4, expect_success=True)
    payload = readback["payload"]
    assert_tree(payload, 4, {"1": 2, "2": 4, "3": 8, "4": 3}, selected="match_086")
    require(payload["max_observed_hop"] == 4, "max_observed_hop_mismatch", {"payload": payload["max_observed_hop"]})
    require(payload["provisional_count"] == 0, "unexpected_provisional_count", {"payload": payload["provisional_count"]})
    return {
        "source": {
            "file": oracle_context.file_stat(source_path),
            "web_cross_checks": [
                "https://github.com/openfootball/worldcup.json",
                "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/articles/knockout-stage-match-schedule-bracket",
            ],
            "final": {key: rows[104][key] for key in ["num", "round", "date", "team1", "team2"]},
            "reachable_match_nums": sorted({104, *[int(edge["to"].split("_")[1]) for edge in fixture["edges"]]}),
        },
        "vault_name": vault["vault_name"],
        "vault_id": vault["vault_id"],
        "vault_path": str(vault["vault_path"].relative_to(ROOT)),
        "vault_salt": vault["vault_salt"],
        "create_stdout_sha256": vault["create_stdout_sha256"],
        "fixture_edge_count": len(fixture["edges"]),
        "fixture_edges": fixture["edges"],
        "readback": readback,
        "recurrence_context_summary": recurrence_context_summary(vault["vault_path"], vault["env"]),
        "physical_readback": physical_readback(vault["vault_path"]),
    }


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    vault = create_vault(work_dir, "soccer-oracle-expand-synthetic")
    base = {
        "domain": "synthetic.bracket",
        "root_action": "root",
        "root_outcome": {"enum": "root"},
        "root_confidence": 1.0,
        "root_hop": 0,
        "clock_ts": 1783132200,
        "edges": [
            {"from": "root", "to": "semi_a", "outcome": {"enum": "A"}},
            {"from": "root", "to": "semi_b", "outcome": {"enum": "B"}},
            {"from": "semi_a", "to": "quarter_a", "outcome": {"enum": "QA"}},
            {"from": "quarter_a", "to": "round_a", "outcome": {"enum": "RA"}},
        ],
    }
    happy = run_expand(work_dir, vault, base, "synthetic-happy", depth=4, expect_success=True)
    assert_tree(happy["payload"], 4, {"1": 2, "2": 1, "3": 1})

    depth_limited = run_expand(work_dir, vault, base, "synthetic-depth-2", depth=2, expect_success=True)
    assert_tree(depth_limited["payload"], 2, {"1": 2, "2": 1})
    require(depth_limited["payload"]["max_observed_hop"] == 2, "depth_limit_failed", {"payload": depth_limited["payload"]})

    threshold_fixture = dict(base)
    threshold_fixture["domain"] = "synthetic.bracket.threshold"
    threshold_fixture["root_confidence"] = 0.06
    threshold = run_expand(work_dir, vault, threshold_fixture, "synthetic-threshold", depth=4, expect_success=True)
    require(threshold["payload"]["flat"] == [], "threshold_prune_emitted_children", {"payload": threshold["payload"]})
    require(threshold["payload"]["max_observed_hop"] == 0, "threshold_max_hop_mismatch", {"payload": threshold["payload"]})

    provisional_fixture = {
        "domain": "synthetic.bracket.provisional",
        "root_action": "root",
        "root_outcome": {"enum": "root"},
        "edges": [
            {"from": "root", "to": "semi_a", "outcome": {"enum": "A"}, "grounded": False},
            {"from": "semi_a", "to": "quarter_a", "outcome": {"enum": "QA"}},
        ],
    }
    provisional = run_expand(work_dir, vault, provisional_fixture, "synthetic-provisional", depth=4, expect_success=True)
    require(flat_hop_counts(provisional["payload"]["flat"]) == {"1": 1}, "provisional_recursed", {"payload": provisional["payload"]})
    require(provisional["payload"]["provisional_count"] == 1, "provisional_count_mismatch", {"payload": provisional["payload"]})

    malformed_fixture = {
        "domain": "synthetic.bracket.malformed",
        "root_action": "root",
        "root_outcome": {"enum": "root"},
        "edges": [{"from": "root", "to": "semi_a", "outcome": {"enum": "A"}, "malformed_context": True}],
    }
    malformed = run_expand(work_dir, vault, malformed_fixture, "synthetic-malformed", depth=4, expect_success=False)
    require(malformed["payload"].get("error_code") == "CALYX_ORACLE_NO_RECURRENCE", "malformed_wrong_error", {"payload": malformed["payload"]})

    invalid_path = work_dir / "synthetic-invalid-depth.json"
    invalid_stat = write_json(invalid_path, base)
    before = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["base", "recurrence"]}
    invalid = run(
        [
            "readback",
            "oracle_expand",
            "--vault",
            str(vault["vault_path"]),
            "--fixture",
            str(invalid_path.relative_to(ROOT)),
            "--vault-id",
            vault["vault_id"],
            "--salt",
            vault["vault_salt"],
            "--depth",
            "5",
        ],
        vault["env"],
        timeout=60,
    )
    after = {cf: cf_rows(vault["vault_path"], vault["env"], cf) for cf in ["base", "recurrence"]}
    require(invalid.returncode != 0, "invalid_depth_passed")
    require(before == after, "invalid_depth_wrote_cf", {"before": before, "after": after})

    return {
        "vault_path": str(vault["vault_path"].relative_to(ROOT)),
        "happy": happy,
        "depth_limited": depth_limited,
        "threshold_prune": threshold,
        "provisional_edge": provisional,
        "malformed_context": malformed,
        "invalid_depth": {
            "fixture": invalid_stat,
            "returncode": invalid.returncode,
            "stderr_tail": invalid.stderr.decode("utf-8", "replace")[-500:],
            "before": before,
            "after": after,
            "stderr_sha256": oracle_context.sha256_bytes(invalid.stderr),
        },
        "physical_readback": physical_readback(vault["vault_path"]),
    }


def write_tree_artifact(path: Path, real: dict[str, Any], report_path: Path) -> dict[str, Any]:
    payload = real["readback"]["payload"]
    records = [
        {
            "action_or_event": item["action_or_event"],
            "domain": item["domain"],
            "outcome": item["outcome"],
            "confidence": item["confidence"],
            "hop": item["hop"],
        }
        for item in payload["flat"]
    ]
    artifact = {
        "schema_version": 1,
        "generated_at": RUN_DATE,
        "domain": DOMAIN,
        "root_action": ROOT_ACTION,
        "root_outcome": ROOT_OUTCOME,
        "source": real["source"],
        "requested_depth": payload["requested_depth"],
        "max_observed_hop": payload["max_observed_hop"],
        "hop_attenuation": payload["hop_attenuation"],
        "min_confidence_threshold": payload["min_confidence_threshold"],
        "hop_counts": flat_hop_counts(payload["flat"]),
        "hop_confidences": flat_confidences(payload["flat"]),
        "provisional_count": payload["provisional_count"],
        "selected": payload["selected"],
        "records": records,
        "provenance": {
            "source_report": str(report_path.relative_to(ROOT)),
            "oracle_stdout_sha256": real["readback"]["stdout_sha256"],
            "oracle_fixture_sha256": real["readback"]["fixture"]["sha256"],
        },
    }
    return write_json(path, artifact)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW.relative_to(ROOT)))
    parser.add_argument("--out", default=str(DEFAULT_OUT.relative_to(ROOT)))
    parser.add_argument("--tree-out", default=str(DEFAULT_TREE_OUT.relative_to(ROOT)))
    return parser.parse_args()


def resolve(path_arg: str) -> Path:
    path = Path(path_arg)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def main() -> int:
    args = parse_args()
    raw_root = resolve(args.raw_root)
    report_path = resolve(args.out)
    tree_path = resolve(args.tree_out)
    work_dir = report_path.parent
    real = real_bracket(work_dir / "real_bracket_vault", raw_root)
    tree_file = write_tree_artifact(tree_path, real, report_path)
    synthetic = synthetic_edges(work_dir / "synthetic_edges")
    report = {
        "status": "ok",
        "run_date": RUN_DATE,
        "real_oracle_expand": real,
        "tree_file": tree_file,
        "synthetic_edges": synthetic,
    }
    report_stat = write_json(report_path, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "report": str(report_path.relative_to(ROOT)),
                "report_sha256": report_stat["sha256"],
                "tree_file": tree_file,
                "fixture_edge_count": real["fixture_edge_count"],
                "hop_counts": flat_hop_counts(real["readback"]["payload"]["flat"]),
                "max_observed_hop": real["readback"]["payload"]["max_observed_hop"],
                "synthetic_edges": ["happy", "depth_limited", "threshold_prune", "provisional_edge", "malformed_context", "invalid_depth"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
