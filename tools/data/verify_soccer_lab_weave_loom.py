#!/usr/bin/env python3
"""Verify Soccer Lab weave-loom physical XTerm and Graph CF rows."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import struct
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import verify_soccer_lab_anchored_outcomes as anchored


ROOT = anchored.ROOT
CALYX = anchored.CALYX
ROWGEN = anchored.ROWGEN
DEFAULT_RAW = anchored.DEFAULT_RAW
DEFAULT_OUT = ROOT / "scratchpad" / "wc2026" / "fsv" / "weave_loom" / "report.json"
MIN_CLASS = 50
TEAM_AXIS = "label:team_match_result"
WEAVE_VAULT = "soccer-weave-loom"
WEAVE_KNN = 4


class WeaveFsvError(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


def run(args: list[str], env: dict[str, str] | None = None, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def run_ok(args: list[str], env: dict[str, str], reason: str, timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    proc = run(args, env, timeout)
    if proc.returncode != 0:
        raise WeaveFsvError(
            reason,
            {
                "args": args,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", "replace")[-8000:],
            },
        )
    return proc


def generate_team_rows(raw_root: Path, rows_root: Path) -> dict[str, Any]:
    if rows_root.exists():
        shutil.rmtree(rows_root)
    args = [
        str(ROWGEN),
        "--raw-root",
        str(raw_root.relative_to(ROOT)),
        "--out",
        str(rows_root.relative_to(ROOT)),
        "--only",
        "teams-history",
    ]
    proc = subprocess.run(args, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if proc.returncode != 0:
        raise WeaveFsvError("row_generation_failed", {"stderr": proc.stderr.decode("utf-8", "replace")})
    source_path = rows_root / "teams-history.jsonl"
    rows = [json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    full_counts = Counter(row["anchors"][0]["value"] for row in rows)
    selected, projector_report = select_nonzero_balanced_rows(rows)
    selected_counts = Counter(row["anchors"][0]["value"] for row in selected)
    if selected_counts != {"draw": MIN_CLASS, "lose": MIN_CLASS, "win": MIN_CLASS}:
        raise WeaveFsvError("balanced_nonzero_selection_mismatch", {"counts": dict(selected_counts)})
    selected_path = rows_root / "team-match-nonzero-balanced.jsonl"
    selected_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in selected), encoding="utf-8")
    return {
        "source_file": anchored.file_stat(source_path),
        "source_rows": len(rows),
        "source_axis_counts": dict(sorted(full_counts.items())),
        "selected_file": anchored.file_stat(selected_path),
        "selected_rows": len(selected),
        "selected_axis_counts": dict(sorted(selected_counts.items())),
        "projector_nonzero_filter": projector_report,
    }


def select_nonzero_balanced_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    vectors = run_projectors([row["text"].encode("utf-8") for row in rows], anchored.FACETS)
    usable: list[dict[str, Any]] = []
    usable_counts: Counter[str] = Counter()
    zero_by_facet: Counter[str] = Counter()
    for index, row in enumerate(rows):
        ok = True
        for facet in anchored.FACETS:
            norm2 = sum(float(value) * float(value) for value in vectors[facet][index])
            if norm2 == 0.0:
                zero_by_facet[facet] += 1
                ok = False
        if ok:
            usable.append(row)
            usable_counts[row["anchors"][0]["value"]] += 1
    selected: list[dict[str, Any]] = []
    selected_counts: Counter[str] = Counter()
    for row in usable:
        value = row["anchors"][0]["value"]
        if selected_counts[value] < MIN_CLASS:
            selected_counts[value] += 1
            selected.append(row)
    return selected, {
        "usable_rows": len(usable),
        "usable_axis_counts": dict(sorted(usable_counts.items())),
        "zero_norm_rows_by_facet": dict(sorted(zero_by_facet.items())),
        "selected_axis_counts": dict(sorted(selected_counts.items())),
    }


def run_projectors(inputs: list[bytes], facets: dict[str, tuple[Path, int]]) -> dict[str, list[list[float]]]:
    out: dict[str, list[list[float]]] = {}
    frame_inputs = [list(value) for value in inputs]
    for facet, (path, dim) in facets.items():
        payload = json.dumps({"modality": "text", "inputs": frame_inputs}, separators=(",", ":")).encode("utf-8")
        proc = subprocess.run(
            [str(path)],
            cwd=ROOT,
            input=struct.pack(">I", len(payload)) + payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if proc.returncode != 0:
            raise WeaveFsvError("projector_failed", {"facet": facet, "stderr": proc.stderr.decode("utf-8", "replace")[-2000:]})
        if len(proc.stdout) < 4:
            raise WeaveFsvError("projector_short_frame", {"facet": facet})
        size = struct.unpack(">I", proc.stdout[:4])[0]
        body = json.loads(proc.stdout[4 : 4 + size])
        vectors = body.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(inputs):
            raise WeaveFsvError("projector_vector_count_mismatch", {"facet": facet})
        for vector in vectors:
            if len(vector) != dim or any(not isinstance(value, (int, float)) for value in vector):
                raise WeaveFsvError("projector_vector_dim_mismatch", {"facet": facet, "expected_dim": dim, "vector": vector})
        out[facet] = vectors
    return out


def build_real_vault(work_dir: Path, rows_path: Path, row_count: int) -> dict[str, Any]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", WEAVE_VAULT, "--panel-template", "text-default"], env, "create_vault_failed")
    created = json.loads(create.stdout)
    vault_path = home / "vaults" / created["vault_id"]
    slot_map = add_soccer_lenses(env, WEAVE_VAULT)
    ingest = run_ok(["ingest", WEAVE_VAULT, "--batch", str(rows_path.relative_to(ROOT)), "--output", "rows"], env, "ingest_failed", timeout=600)
    ingest_rows = [json.loads(line) for line in ingest.stdout.decode("utf-8").splitlines() if line.strip()]
    if len(ingest_rows) != row_count or not all(row.get("new") for row in ingest_rows):
        raise WeaveFsvError("ingest_row_count_mismatch", {"observed": len(ingest_rows), "expected": row_count})
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
    weave = run_ok(["weave-loom", WEAVE_VAULT, "--knn", str(WEAVE_KNN), "--time-budget-ms", "120000"], env, "weave_loom_failed", timeout=300)
    weave_stderr = weave.stderr.decode("utf-8", "replace")
    weave_report = json.loads(weave.stdout)
    if weave_report.get("status") != "ok":
        raise WeaveFsvError("weave_status_not_ok", {"report": weave_report})
    if weave_report.get("constellations_processed") != row_count or weave_report.get("limited"):
        raise WeaveFsvError("weave_not_full_corpus", {"report": weave_report})
    if weave_report["xterm"]["rows_persisted"] <= 0 or weave_report["assoc_graph"]["edges_persisted"] <= 0:
        raise WeaveFsvError("weave_missing_rows_or_edges", {"report": weave_report})
    xterm_readback = read_decode_cf(env, vault_path, "xterm")
    graph_readback = read_decode_cf(env, vault_path, "graph")
    xterm_summary = verify_xterm_readback(xterm_readback, weave_report)
    graph_summary = verify_graph_readback(graph_readback, weave_report)
    return {
        "vault_id": created["vault_id"],
        "vault_path": str(vault_path.relative_to(ROOT)),
        "slot_map": slot_map,
        "ingest_rows": len(ingest_rows),
        "cx_list_rows": len(cx_rows),
        "slot_counts": slot_counts,
        "weave_stdout_sha256": anchored.sha256_bytes(weave.stdout),
        "weave_stderr_sha256": anchored.sha256_bytes(weave.stderr),
        "progress_artifact_seen": "WEAVE_LOOM_PROGRESS=" in weave_stderr,
        "weave_report": weave_report,
        "xterm_readback": xterm_summary,
        "graph_readback": graph_summary,
        "physical_readback": weave_physical_readback(vault_path),
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


def read_decode_cf(env: dict[str, str], vault_path: Path, cf_name: str) -> dict[str, Any]:
    proc = run_ok(["readback", "--cf", cf_name, "--vault", str(vault_path)], env, f"{cf_name}_cf_readback_failed", timeout=240)
    rows = decode_cf_lines(proc.stdout.decode("utf-8"), cf_name)
    latest = latest_by_key(rows)
    return {
        "stdout_sha256": anchored.sha256_bytes(proc.stdout),
        "raw_rows": len(rows),
        "unique_rows": len(latest),
        "rows": rows,
        "latest": latest,
    }


def decode_cf_lines(stdout: str, cf_name: str) -> list[dict[str, Any]]:
    rows = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 8 or parts[0] != "CF" or parts[1] != cf_name or parts[4] != "KEY" or parts[6] != "VALUE":
            raise WeaveFsvError("malformed_cf_line", {"cf": cf_name, "line": line})
        rows.append(
            {
                "file": parts[3],
                "key_hex": parts[5],
                "value_hex": parts[7],
                "value_sha256": anchored.sha256_bytes(bytes.fromhex(parts[7])),
            }
        )
    return rows


def latest_by_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest[row["key_hex"]] = row
    return {key: latest[key] for key in sorted(latest)}


def verify_xterm_readback(readback: dict[str, Any], weave_report: dict[str, Any]) -> dict[str, Any]:
    decoded = [decode_xterm_row(row) for row in readback["latest"].values()]
    expected_rows = int(weave_report["xterm"]["rows_persisted"])
    if len(decoded) != expected_rows:
        raise WeaveFsvError("xterm_unique_count_mismatch", {"observed": len(decoded), "expected": expected_rows})
    pair_counts: Counter[str] = Counter(f"{row['a']}:{row['b']}:{row['kind']}" for row in decoded)
    expected_pairs = {f"{pair['a']}:{pair['b']}:agreement": pair["n"] for pair in weave_report["xterm"]["slot_pairs"]}
    if dict(sorted(pair_counts.items())) != dict(sorted(expected_pairs.items())):
        raise WeaveFsvError("xterm_pair_counts_mismatch", {"observed": dict(pair_counts), "expected": expected_pairs})
    sample = decoded[:6]
    return {
        "raw_rows": readback["raw_rows"],
        "unique_rows": len(decoded),
        "stdout_sha256": readback["stdout_sha256"],
        "pair_counts": dict(sorted(pair_counts.items())),
        "sample": sample,
    }


def decode_xterm_row(row: dict[str, Any]) -> dict[str, Any]:
    key = bytes.fromhex(row["key_hex"])
    if len(key) != 21:
        raise WeaveFsvError("xterm_key_length_mismatch", {"key_hex": row["key_hex"], "len": len(key)})
    payload = json.loads(bytes.fromhex(row["value_hex"]))
    cx_id = key[:16].hex()
    a = int.from_bytes(key[16:18], "big")
    b = int.from_bytes(key[18:20], "big")
    kind_code = key[20]
    kind = {0: "concat", 1: "interaction", 2: "agreement", 3: "delta"}.get(kind_code)
    if payload.get("key") != {"cx_id": cx_id, "a": a, "b": b, "kind": kind}:
        raise WeaveFsvError("xterm_payload_key_mismatch", {"key": row["key_hex"], "payload": payload})
    return {
        "cx_id": cx_id,
        "a": a,
        "b": b,
        "kind": kind,
        "value": payload.get("value"),
        "tag": payload.get("tag"),
        "value_sha256": row["value_sha256"],
    }


def verify_graph_readback(readback: dict[str, Any], weave_report: dict[str, Any]) -> dict[str, Any]:
    decoded = [decode_graph_row(row) for row in readback["latest"].values()]
    counts = Counter(row["kind"] for row in decoded)
    report = weave_report["assoc_graph"]["report"]
    expected_nodes = int(report["node_count"])
    expected_edges = int(weave_report["assoc_graph"]["edges_persisted"])
    if counts["node"] != expected_nodes or counts["edge_out"] != expected_edges or counts["edge_in"] != expected_edges:
        raise WeaveFsvError("graph_counts_mismatch", {"counts": dict(counts), "expected_nodes": expected_nodes, "expected_edges": expected_edges})
    edge_types = Counter(row.get("edge_type", "") for row in decoded if row["kind"] in {"edge_out", "edge_in"})
    if edge_types.get("knn", 0) != expected_edges * 2:
        raise WeaveFsvError("graph_edge_type_mismatch", {"edge_types": dict(edge_types), "expected_knn_rows": expected_edges * 2})
    node_samples = [row for row in decoded if row["kind"] == "node"][:3]
    edge_samples = [row for row in decoded if row["kind"] == "edge_out"][:3]
    return {
        "raw_rows": readback["raw_rows"],
        "unique_rows": len(decoded),
        "stdout_sha256": readback["stdout_sha256"],
        "kind_counts": dict(sorted(counts.items())),
        "edge_types": dict(sorted(edge_types.items())),
        "node_samples": node_samples,
        "edge_samples": edge_samples,
    }


def decode_graph_row(row: dict[str, Any]) -> dict[str, Any]:
    key = bytes.fromhex(row["key_hex"])
    prefix = b"g" + len(b"default").to_bytes(2, "big") + b"default"
    if not key.startswith(prefix) or len(key) <= len(prefix):
        raise WeaveFsvError("graph_key_prefix_mismatch", {"key_hex": row["key_hex"]})
    kind = key[len(prefix)]
    offset = len(prefix) + 1
    if kind == 0:
        if len(key) != offset + 16:
            raise WeaveFsvError("graph_node_key_length_mismatch", {"key_hex": row["key_hex"]})
        payload = json.loads(bytes.fromhex(row["value_hex"]))
        return {
            "kind": "node",
            "cx_id": key[offset : offset + 16].hex(),
            "embedding_dim": len(payload.get("embedding") or []),
            "anchor_count": len(payload.get("anchors") or []),
            "metadata_keys": sorted((payload.get("metadata") or {}).keys())[:8],
            "value_sha256": row["value_sha256"],
        }
    if kind in {1, 2}:
        first = key[offset : offset + 16].hex()
        offset += 16
        if len(key) < offset + 2:
            raise WeaveFsvError("graph_edge_type_short", {"key_hex": row["key_hex"]})
        edge_type_len = int.from_bytes(key[offset : offset + 2], "big")
        offset += 2
        edge_type = key[offset : offset + edge_type_len].decode("utf-8")
        offset += edge_type_len
        second = key[offset : offset + 16].hex()
        if len(key) != offset + 16:
            raise WeaveFsvError("graph_edge_key_length_mismatch", {"key_hex": row["key_hex"]})
        out = {
            "kind": "edge_out" if kind == 1 else "edge_in",
            "edge_type": edge_type,
            "src": first if kind == 1 else second,
            "dst": second if kind == 1 else first,
            "value_sha256": row["value_sha256"],
        }
        if kind == 1:
            payload = json.loads(bytes.fromhex(row["value_hex"]))
            out["cosine"] = payload.get("cosine")
            out["rank"] = payload.get("rank")
        else:
            out["forward_key_hex"] = bytes.fromhex(row["value_hex"]).hex()
        return out
    if kind == 3:
        return {"kind": "csr_manifest", "value_sha256": row["value_sha256"]}
    if kind == 4:
        return {"kind": "metadata", "value_sha256": row["value_sha256"]}
    if kind == 5:
        return {"kind": "csr_segment", "value_sha256": row["value_sha256"]}
    raise WeaveFsvError("unknown_graph_key_kind", {"kind": kind, "key_hex": row["key_hex"]})


def weave_physical_readback(vault_path: Path) -> dict[str, Any]:
    stats = anchored.physical_readback(vault_path)
    for cf_name in ["xterm", "graph"]:
        files = sorted((vault_path / "cf" / cf_name).glob("*.sst"))
        if not files:
            raise WeaveFsvError("missing_cf_sst", {"cf": cf_name})
        stats["cf"][cf_name] = {
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
    projectors = write_synthetic_projectors(work_dir)
    happy = synthetic_happy(work_dir / "happy", projectors)
    edges = {
        "one_content_slot": synthetic_bad_case(work_dir / "one_content_slot", projectors[:1], ["weave-loom", "edge-vault"], "one_content_slot"),
        "limit_one": synthetic_bad_case(work_dir / "limit_one", projectors, ["weave-loom", "edge-vault", "--limit", "1"], "limit_one"),
        "zero_norm_vector": synthetic_bad_case(work_dir / "zero_norm_vector", projectors, ["weave-loom", "edge-vault"], "zero_norm_vector", zero_row=True),
    }
    return {"happy": happy, "edges": edges}


def write_synthetic_projectors(root: Path) -> list[Path]:
    paths = []
    for name, values in [("facet_a", "[1.0, 0.0]"), ("facet_b", "[0.5, 0.5]")]:
        path = root / name
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, struct, sys\n"
            "raw=sys.stdin.buffer.read()\n"
            "n=struct.unpack('>I', raw[:4])[0]\n"
            "payload=json.loads(raw[4:4+n])\n"
            f"vec={values}\n"
            "vectors=[]\n"
            "for item in payload['inputs']:\n"
            "    text=bytes(item).decode('utf-8')\n"
            "    vectors.append([0.0, 0.0] if 'ZERO' in text else vec)\n"
            "out=json.dumps({'vectors': vectors}).encode('utf-8')\n"
            "sys.stdout.buffer.write(struct.pack('>I', len(out))+out)\n",
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        paths.append(path)
    return paths


def synthetic_happy(work_dir: Path, projectors: list[Path]) -> dict[str, Any]:
    env, vault_path = build_synthetic_vault(work_dir, projectors, zero_row=False)
    weave = run_ok(["weave-loom", "edge-vault", "--knn", "1"], env, "synthetic_happy_weave_failed")
    report = json.loads(weave.stdout)
    xterm = read_decode_cf(env, vault_path, "xterm")
    graph = read_decode_cf(env, vault_path, "graph")
    xterm_summary = verify_xterm_readback(xterm, report)
    graph_summary = verify_graph_readback(graph, report)
    if report["xterm"]["rows_persisted"] != 2 or report["assoc_graph"]["edges_persisted"] != 2:
        raise WeaveFsvError("synthetic_happy_counts_mismatch", {"report": report})
    return {
        "vault_path": str(vault_path.relative_to(ROOT)),
        "weave_report": report,
        "xterm_unique_rows": xterm_summary["unique_rows"],
        "graph_kind_counts": graph_summary["kind_counts"],
    }


def synthetic_bad_case(work_dir: Path, projectors: list[Path], weave_args: list[str], name: str, zero_row: bool = False) -> dict[str, Any]:
    env, vault_path = build_synthetic_vault(work_dir, projectors, zero_row=zero_row)
    before = cf_unique_counts(env, vault_path)
    proc = run(weave_args, env, timeout=60)
    after = cf_unique_counts(env, vault_path)
    if proc.returncode == 0:
        raise WeaveFsvError("synthetic_bad_case_passed", {"case": name, "stdout": proc.stdout.decode("utf-8", "replace")})
    if before != after:
        raise WeaveFsvError("synthetic_bad_case_wrote_cf", {"case": name, "before": before, "after": after})
    return {
        "returncode": proc.returncode,
        "before_cf_counts": before,
        "after_cf_counts": after,
        "stderr_sha256": anchored.sha256_bytes(proc.stderr),
        "stdout_fragment": proc.stdout.decode("utf-8", "replace")[-240:],
    }


def build_synthetic_vault(work_dir: Path, projectors: list[Path], zero_row: bool) -> tuple[dict[str, str], Path]:
    home = work_dir / "calyx_home"
    home.mkdir(parents=True)
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_ok(["create-vault", "edge-vault", "--panel-template", "text-default"], env, "synthetic_create_failed")
    vault_path = home / "vaults" / json.loads(create.stdout)["vault_id"]
    for index, projector in enumerate(projectors):
        run_ok(
            [
                "add-lens",
                "edge-vault",
                "--name",
                f"facet_{index}",
                "--runtime",
                "external-cmd",
                "--endpoint",
                str(projector),
                "--shape",
                "Dense(2)",
                "--modality",
                "text",
            ],
            env,
            "synthetic_add_lens_failed",
        )
    rows = [
        {"text": "ROW A", "anchors": [{"kind": "label:edge", "value": "yes", "source": "synthetic-weave-fsv", "confidence": 1.0}]},
        {"text": "ROW ZERO" if zero_row else "ROW B", "anchors": [{"kind": "label:edge", "value": "no", "source": "synthetic-weave-fsv", "confidence": 1.0}]},
    ]
    batch = work_dir / "rows.jsonl"
    batch.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    run_ok(["ingest", "edge-vault", "--batch", str(batch.relative_to(ROOT)), "--output", "rows"], env, "synthetic_ingest_failed")
    run_ok(["readback", "cx-list", "--vault", str(vault_path), "--include-slots", "--limit", "2", "--rebuild-base-page-index"], env, "synthetic_cx_list_failed")
    return env, vault_path


def cf_unique_counts(env: dict[str, str], vault_path: Path) -> dict[str, int]:
    counts = {}
    for cf_name in ["xterm", "graph"]:
        proc = run(["readback", "--cf", cf_name, "--vault", str(vault_path)], env, timeout=60)
        if proc.returncode != 0:
            counts[cf_name] = 0
            continue
        counts[cf_name] = len(latest_by_key(decode_cf_lines(proc.stdout.decode("utf-8"), cf_name)))
    return counts


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
    generation = generate_team_rows(raw_root, rows_root)
    vault = build_real_vault(work_dir, rows_root / "team-match-nonzero-balanced.jsonl", generation["selected_rows"])
    edges = synthetic_edges(work_dir / "synthetic_edges")
    report = {"status": "ok", "generation": generation, "vault": vault, "synthetic": edges}
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    if report_path.read_text(encoding="utf-8") != encoded + "\n":
        raise WeaveFsvError("report_readback_mismatch", {"path": str(report_path.relative_to(ROOT))})
    print(
        json.dumps(
            {
                "status": "ok",
                "vault_id": vault["vault_id"],
                "selected_rows": generation["selected_rows"],
                "xterm_unique_rows": vault["xterm_readback"]["unique_rows"],
                "graph_kind_counts": vault["graph_readback"]["kind_counts"],
                "assoc_edges": vault["weave_report"]["assoc_graph"]["edges_persisted"],
                "synthetic_edges": sorted(edges["edges"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
