#!/usr/bin/env python3
"""Verify Soccer Lab grounded outcome anchors meet balance floors."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CALYX = ROOT / "target" / "release" / "calyx"
ROWGEN = ROOT / "tools" / "data" / "generate_soccer_lab_rows.py"
DEFAULT_RAW = ROOT / "scratchpad" / "wc2026" / "raw"
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "anchored_outcomes" / "report.json"
MIN_CLASS = 50

ROW_TARGETS = {
    "matches": "matches.jsonl",
    "teams-history": "teams-history.jsonl",
    "matches-2026": "matches-2026.jsonl",
}

SUPPORTED_AXES = {
    "label:match_result": {"away_win": 355, "draw": 232, "home_win": 746},
    "label:team_match_result": {"draw": 420, "lose": 1038, "win": 1038},
}
BALANCED_AXIS_COUNTS = {
    axis: {value: MIN_CLASS for value in counts}
    for axis, counts in SUPPORTED_AXES.items()
}

UNSUPPORTED_AXES = {
    "label:winner": "positive class has 6 rows in current Harrachi source",
    "label:finalist": "positive class has 12 rows in current Harrachi source",
    "label:semi_finalist": "positive class has 24 rows in current Harrachi source",
    "label:quarter_finalist": "positive class has 48 rows in current Harrachi source",
}

FACETS = {
    "attack": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "attack", 6),
    "defense": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "defense", 5),
    "tempo": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "tempo", 4),
    "discipline": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "discipline", 4),
    "pedigree": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "pedigree", 6),
    "form": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "form", 5),
    "context": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "context", 8),
}


class AnchoredOutcomesError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_stat(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "mode": oct(path.stat().st_mode & 0o777),
    }


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise AnchoredOutcomesError(reason, {"args": args, "stderr": proc.stderr.decode("utf-8", "replace")[-8000:]})
    return proc


def generate_rows(raw_root: Path, rows_root: Path) -> dict[str, Any]:
    if rows_root.exists():
        shutil.rmtree(rows_root)
    args = [str(ROWGEN), "--raw-root", str(raw_root.relative_to(ROOT)), "--out", str(rows_root.relative_to(ROOT))]
    for target in ROW_TARGETS:
        args.extend(["--only", target])
    proc = subprocess.run(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if proc.returncode != 0:
        raise AnchoredOutcomesError("row_generation_failed", {"stderr": proc.stderr.decode("utf-8", "replace")})

    combined_path = rows_root / "anchored-outcomes-balanced.jsonl"
    axis_counts: dict[str, Counter[str]] = defaultdict(Counter)
    selected_counts: dict[str, Counter[str]] = defaultdict(Counter)
    row_files = {}
    combined_rows: list[dict[str, Any]] = []
    for filename in ROW_TARGETS.values():
        path = rows_root / filename
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows:
            raise AnchoredOutcomesError("empty_generated_file", {"file": filename})
        for row in rows:
            anchors = row.get("anchors")
            if not isinstance(anchors, list) or len(anchors) != 1:
                raise AnchoredOutcomesError("expected_single_anchor", {"file": filename, "row": row})
            anchor = anchors[0]
            kind = anchor.get("kind")
            value = str(anchor.get("value"))
            if kind not in SUPPORTED_AXES:
                raise AnchoredOutcomesError("unsupported_axis_in_grounding_batch", {"file": filename, "axis": kind})
            if not anchor.get("source") or not (0.0 < float(anchor.get("confidence", 0.0)) <= 1.0):
                raise AnchoredOutcomesError("ungrounded_anchor_in_generated_rows", {"file": filename, "anchor": anchor})
            axis_counts[kind][value] += 1
            if selected_counts[kind][value] < MIN_CLASS:
                selected_counts[kind][value] += 1
                combined_rows.append(row)
        row_files[filename] = {"file": file_stat(path), "rows": len(rows)}

    observed = {axis: dict(sorted(counts.items())) for axis, counts in sorted(axis_counts.items())}
    if observed != SUPPORTED_AXES:
        raise AnchoredOutcomesError("supported_axis_counts_mismatch", {"observed": observed, "expected": SUPPORTED_AXES})
    for axis, counts in observed.items():
        if min(counts.values()) < MIN_CLASS or len(counts) < 2:
            raise AnchoredOutcomesError("axis_floor_not_met", {"axis": axis, "counts": counts, "min_class": MIN_CLASS})
    selected = {axis: dict(sorted(counts.items())) for axis, counts in sorted(selected_counts.items())}
    if selected != BALANCED_AXIS_COUNTS:
        raise AnchoredOutcomesError("balanced_selection_mismatch", {"observed": selected, "expected": BALANCED_AXIS_COUNTS})

    combined_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in combined_rows), encoding="utf-8")
    return {
        "row_files": row_files,
        "combined_file": file_stat(combined_path),
        "rows": len(combined_rows),
        "full_supported_axis_counts": observed,
        "balanced_axis_counts": selected,
        "unsupported_axes_excluded": UNSUPPORTED_AXES,
    }


def build_anchor_vault(work_dir: Path, rows_path: Path, row_count: int) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    vault_name = "soccer-anchored-outcomes"
    create = run_ok(["create-vault", vault_name, "--panel-template", "text-default"], env, "create_vault_failed")
    created = json.loads(create.stdout)
    vault_path = home / "vaults" / created["vault_id"]
    slot_map = {}
    for name, (path, dim) in FACETS.items():
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
    ingest = run_ok(["ingest", vault_name, "--batch", str(rows_path.relative_to(ROOT)), "--output", "rows"], env, "ingest_failed", timeout=600)
    ingest_rows = [json.loads(line) for line in ingest.stdout.decode("utf-8").splitlines() if line.strip()]
    if len(ingest_rows) != row_count or not all(row.get("new") for row in ingest_rows):
        raise AnchoredOutcomesError("ingest_row_count_mismatch", {"observed": len(ingest_rows), "expected": row_count})

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
    if len(cx_rows) != row_count:
        raise AnchoredOutcomesError("cx_list_count_mismatch", {"observed": len(cx_rows), "expected": row_count})
    slot_counts = inspect_slots(cx_rows, slot_map)

    anchors_cf = run_ok(["readback", "--cf", "anchors", "--vault", str(vault_path)], env, "anchors_cf_readback_failed", timeout=240)
    decoded_raw = decode_anchors_cf(anchors_cf.stdout.decode("utf-8"))
    decoded = unique_latest_by_key(decoded_raw)
    axis_summary = summarize_decoded_anchors(decoded)
    if {axis: summary["values"] for axis, summary in axis_summary.items()} != BALANCED_AXIS_COUNTS:
        raise AnchoredOutcomesError("anchors_cf_axis_counts_mismatch", {"observed": axis_summary, "expected": BALANCED_AXIS_COUNTS})
    for axis, summary in axis_summary.items():
        if summary["blank_source_count"] or summary["bad_confidence_count"]:
            raise AnchoredOutcomesError("ungrounded_anchor_cf_row", {"axis": axis, "summary": summary})
        if summary["min_class_count"] < MIN_CLASS or summary["class_count"] < 2:
            raise AnchoredOutcomesError("anchors_cf_balance_floor_not_met", {"axis": axis, "summary": summary})
    if len(decoded) != row_count:
        raise AnchoredOutcomesError("anchors_cf_unique_row_count_mismatch", {"observed": len(decoded), "expected": row_count, "raw_rows": len(decoded_raw)})
    return {
        "vault_id": created["vault_id"],
        "vault_path": str(vault_path.relative_to(ROOT)),
        "slot_map": slot_map,
        "ingest_rows": len(ingest_rows),
        "cx_list_rows": len(cx_rows),
        "slot_counts": slot_counts,
        "anchors_cf_raw_rows": len(decoded_raw),
        "anchors_cf_unique_rows": len(decoded),
        "anchors_cf_stdout_sha256": sha256_bytes(anchors_cf.stdout),
        "axis_summary": axis_summary,
        "sample_decoded_anchors": decoded[:6],
        "physical_readback": physical_readback(vault_path),
    }


def inspect_slots(rows: list[dict[str, Any]], slot_map: dict[str, int]) -> dict[str, int]:
    counts = {facet: 0 for facet in slot_map}
    for row in rows:
        by_slot = {int(entry.get("slot", entry.get("slot_id", -1))): entry for entry in row.get("slots", []) if isinstance(entry, dict)}
        for facet, slot_id in slot_map.items():
            if slot_id not in by_slot:
                raise AnchoredOutcomesError("missing_slot_in_cx_list", {"facet": facet, "slot": slot_id, "cx_id": row.get("cx_id")})
            if by_slot[slot_id].get("kind") != "dense":
                raise AnchoredOutcomesError("expected_dense_slot", {"facet": facet, "slot": slot_id, "entry": by_slot[slot_id]})
            counts[facet] += 1
    return counts


def decode_anchors_cf(stdout: str) -> list[dict[str, Any]]:
    out = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 8 or parts[0] != "CF" or parts[1] != "anchors" or parts[4] != "KEY" or parts[6] != "VALUE":
            raise AnchoredOutcomesError("malformed_anchors_cf_line", {"line": line})
        key_hex = parts[5]
        value_hex = parts[7]
        anchor = decode_anchor(bytes.fromhex(value_hex))
        out.append({"key_hex": key_hex, "cx_id": key_hex[:32], "value_sha256": sha256_bytes(bytes.fromhex(value_hex)), **anchor})
    return out


def unique_latest_by_key(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest = {row["key_hex"]: row for row in rows}
    return [latest[key] for key in sorted(latest)]


def decode_anchor(data: bytes) -> dict[str, Any]:
    cur = Cursor(data)
    kind_tag = cur.u16()
    if kind_tag == 3:
        kind = f"label:{cur.string()}"
    else:
        raise AnchoredOutcomesError("unexpected_anchor_kind_tag", {"tag": kind_tag})
    value_tag = cur.u8()
    if value_tag == 1:
        value = cur.string()
    else:
        raise AnchoredOutcomesError("unexpected_anchor_value_tag", {"tag": value_tag, "kind": kind})
    source = cur.string()
    observed_at = cur.u64()
    confidence = struct.unpack(">f", cur.bytes(4))[0]
    if cur.remaining() != 0:
        raise AnchoredOutcomesError("trailing_anchor_bytes", {"remaining": cur.remaining()})
    return {"kind": kind, "value": value, "source": source, "observed_at": observed_at, "confidence": confidence}


class Cursor:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def bytes(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise AnchoredOutcomesError("anchor_decode_truncated", {"pos": self.pos, "need": n, "len": len(self.data)})
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.bytes(1)[0]

    def u16(self) -> int:
        return int.from_bytes(self.bytes(2), "big")

    def u32(self) -> int:
        return int.from_bytes(self.bytes(4), "big")

    def u64(self) -> int:
        return int.from_bytes(self.bytes(8), "big")

    def string(self) -> str:
        return self.bytes(self.u32()).decode("utf-8")

    def remaining(self) -> int:
        return len(self.data) - self.pos


def summarize_decoded_anchors(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for row in rows:
        axis = row["kind"]
        entry = summary.setdefault(
            axis,
            {"values": Counter(), "sources": Counter(), "blank_source_count": 0, "bad_confidence_count": 0},
        )
        entry["values"][row["value"]] += 1
        entry["sources"][row["source"]] += 1
        if not row["source"]:
            entry["blank_source_count"] += 1
        if not math.isfinite(row["confidence"]) or not (0.0 < row["confidence"] <= 1.0):
            entry["bad_confidence_count"] += 1
    normalized = {}
    for axis, entry in sorted(summary.items()):
        values = dict(sorted(entry["values"].items()))
        normalized[axis] = {
            "values": values,
            "class_count": len(values),
            "min_class_count": min(values.values()) if values else 0,
            "sources": dict(sorted(entry["sources"].items())),
            "blank_source_count": entry["blank_source_count"],
            "bad_confidence_count": entry["bad_confidence_count"],
        }
    return normalized


def physical_readback(vault_path: Path) -> dict[str, Any]:
    required = {
        "MANIFEST": vault_path / "MANIFEST",
        "wal": vault_path / "wal" / "00000000000000000000.wal",
        "base_page_index_manifest": vault_path / "base_page_index_v1" / "manifest.json",
        "search_manifest": vault_path / "idx" / "search" / "manifest.json",
        "ledger_head": vault_path / "ledger_head" / "current.json",
    }
    required_stats = {}
    for name, path in required.items():
        if not path.exists():
            raise AnchoredOutcomesError("missing_physical_file", {"name": name, "path": str(path.relative_to(ROOT))})
        required_stats[name] = file_stat(path)
    cf_stats = {}
    for cf_name in ["base", "anchors"]:
        files = sorted((vault_path / "cf" / cf_name).glob("*.sst"))
        if not files:
            raise AnchoredOutcomesError("missing_cf_sst", {"cf": cf_name})
        cf_stats[cf_name] = {
            "sst_count": len(files),
            "bytes": sum(path.stat().st_size for path in files),
            "sha256_first": sha256_bytes(files[0].read_bytes()),
            "sha256_last": sha256_bytes(files[-1].read_bytes()),
        }
    return {"required_files": required_stats, "cf": cf_stats}


def synthetic_edges(work_dir: Path) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", "anchor-edge", "--panel-template", "text-default"], env, "edge_create_failed")
    vault_path = home / "vaults" / json.loads(create.stdout)["vault_id"]
    bad_cases = {
        "negative_confidence": {"text": "bad negative confidence", "anchors": [{"kind": "label:edge", "value": "yes", "source": "synthetic", "confidence": -0.1}]},
        "over_confidence": {"text": "bad over confidence", "anchors": [{"kind": "label:edge", "value": "yes", "source": "synthetic", "confidence": 1.1}]},
        "unknown_kind": {"text": "bad unknown kind", "anchors": [{"kind": "not-a-kind", "value": "yes", "source": "synthetic", "confidence": 1.0}]},
        "malformed_json": '{"text":"bad json","anchors":[',
    }
    observed: dict[str, Any] = {}
    for name, payload in bad_cases.items():
        before = anchors_cf_count(vault_path, env)
        path = work_dir / f"{name}.jsonl"
        if isinstance(payload, str):
            path.write_text(payload + "\n", encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        proc = run(["ingest", "anchor-edge", "--batch", str(path.relative_to(ROOT)), "--output", "rows"], env, timeout=60)
        if proc.returncode == 0:
            raise AnchoredOutcomesError("synthetic_bad_case_passed", {"edge": name, "stdout": proc.stdout.decode("utf-8", "replace")})
        after = anchors_cf_count(vault_path, env)
        if before != after:
            raise AnchoredOutcomesError("synthetic_bad_case_wrote_anchor", {"edge": name, "before": before, "after": after})
        observed[name] = {"returncode": proc.returncode, "before_anchors_cf": before, "after_anchors_cf": after, "stderr_sha256": sha256_bytes(proc.stderr)}

    unqualified = {"text": "accepted but ungrounded by doctrine", "anchors": [{"kind": "label:edge", "value": "maybe", "source": "", "confidence": 0.0}]}
    unqualified_path = work_dir / "accepted-unqualified.jsonl"
    unqualified_path.write_text(json.dumps(unqualified, sort_keys=True) + "\n", encoding="utf-8")
    before = anchors_cf_count(vault_path, env)
    ingest = run_ok(["ingest", "anchor-edge", "--batch", str(unqualified_path.relative_to(ROOT)), "--output", "rows"], env, "edge_unqualified_ingest_failed", timeout=120)
    after = anchors_cf_count(vault_path, env)
    if after - before != 1:
        raise AnchoredOutcomesError("synthetic_unqualified_anchor_delta_mismatch", {"before": before, "after": after})
    decoded = unique_latest_by_key(decode_anchors_cf(run_ok(["readback", "--cf", "anchors", "--vault", str(vault_path)], env, "edge_unqualified_readback_failed").stdout.decode("utf-8")))
    summary = summarize_decoded_anchors(decoded)
    edge_summary = summary.get("label:edge", {})
    if edge_summary.get("blank_source_count") != 1 or edge_summary.get("bad_confidence_count") != 1:
        raise AnchoredOutcomesError("synthetic_unqualified_not_detected", {"summary": edge_summary})
    observed["accepted_unqualified_zero_confidence"] = {
        "ingest_rows": len([line for line in ingest.stdout.decode("utf-8").splitlines() if line.strip()]),
        "before_anchors_cf": before,
        "after_anchors_cf": after,
        "axis_summary": edge_summary,
        "row_file": file_stat(unqualified_path),
    }

    happy_rows = []
    for idx in range(100):
        value = "yes" if idx < 50 else "no"
        happy_rows.append({"text": f"synthetic balanced anchor {idx}", "anchors": [{"kind": "label:edge", "value": value, "source": "synthetic-edge-fsv", "confidence": 1.0}]})
    happy_path = work_dir / "happy-balanced.jsonl"
    happy_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in happy_rows), encoding="utf-8")
    before = anchors_cf_count(vault_path, env)
    ingest = run_ok(["ingest", "anchor-edge", "--batch", str(happy_path.relative_to(ROOT)), "--output", "rows"], env, "edge_happy_failed", timeout=120)
    after = anchors_cf_count(vault_path, env)
    if after - before != 100:
        raise AnchoredOutcomesError("synthetic_happy_anchor_delta_mismatch", {"before": before, "after": after})
    decoded = unique_latest_by_key(decode_anchors_cf(run_ok(["readback", "--cf", "anchors", "--vault", str(vault_path)], env, "edge_anchor_readback_failed").stdout.decode("utf-8")))
    summary = summarize_decoded_anchors(decoded)
    values = summary.get("label:edge", {}).get("values", {})
    if values.get("no") != 50 or values.get("yes") != 50:
        raise AnchoredOutcomesError("synthetic_happy_balance_mismatch", {"summary": summary})
    observed["happy_balanced"] = {
        "ingest_rows": len([line for line in ingest.stdout.decode("utf-8").splitlines() if line.strip()]),
        "before_anchors_cf": before,
        "after_anchors_cf": after,
        "axis_summary": summary["label:edge"],
        "row_file": file_stat(happy_path),
    }
    return {"vault_path": str(vault_path.relative_to(ROOT)), "edges": observed}


def anchors_cf_count(vault_path: Path, env: dict[str, str]) -> int:
    proc = run(["readback", "--cf", "anchors", "--vault", str(vault_path)], env, timeout=60)
    if proc.returncode != 0:
        return 0
    return len(unique_latest_by_key(decode_anchors_cf(proc.stdout.decode("utf-8"))))


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
    rows_root = work_dir / "rows"
    generation = generate_rows(raw_root, rows_root)
    vault = build_anchor_vault(work_dir, rows_root / "anchored-outcomes-balanced.jsonl", generation["rows"])
    edges = synthetic_edges(work_dir / "synthetic_edges")
    report = {"status": "ok", "generation": generation, "vault": vault, "synthetic_edges": edges}
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    if report_path.read_text(encoding="utf-8") != encoded + "\n":
        raise AnchoredOutcomesError("report_readback_mismatch", {"path": str(report_path.relative_to(ROOT))})
    print(
        json.dumps(
            {
                "status": "ok",
                "rows": generation["rows"],
                "anchors_cf_raw_rows": vault["anchors_cf_raw_rows"],
                "anchors_cf_unique_rows": vault["anchors_cf_unique_rows"],
                "axes": {axis: summary["values"] for axis, summary in vault["axis_summary"].items()},
                "synthetic_edges": sorted(edges["edges"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
