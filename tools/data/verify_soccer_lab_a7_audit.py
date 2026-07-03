#!/usr/bin/env python3
"""Verify Soccer Lab team/match facets pass A7 signal and redundancy floors."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CALYX = ROOT / "target" / "release" / "calyx"
ROWGEN = ROOT / "tools" / "data" / "generate_soccer_lab_rows.py"
RAW_ROOT = ROOT / "scratchpad" / "wc2026" / "raw"
MIN_SIGNAL_BITS = 0.05
MAX_PAIRWISE_CORR = 0.6
MI_BINS = 5
SIGNATURE_PER_CLASS = 150

FACETS = {
    "attack": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "attack", 6),
    "defense": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "defense", 5),
    "tempo": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "tempo", 4),
    "discipline": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "discipline", 4),
    "pedigree": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "pedigree", 6),
    "form": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "form", 5),
    "context": (ROOT / "tools" / "lenses" / "soccer_lab" / "team_match" / "context", 8),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frame(payload: dict[str, object]) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(encoded)) + encoded


def run_projector(path: Path, texts: list[str]) -> list[list[float]]:
    proc = subprocess.run(
        [str(path)],
        input=frame({"modality": "text", "inputs": [list(text.encode("utf-8")) for text in texts]}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"{path}: projector failed: {proc.stderr.decode('utf-8')}")
    if len(proc.stdout) < 4:
        raise AssertionError(f"{path}: missing output frame")
    size = struct.unpack(">I", proc.stdout[:4])[0]
    payload = json.loads(proc.stdout[4 : 4 + size])
    vectors = payload.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise AssertionError(f"{path}: bad vectors payload")
    return vectors


def generate_rows(out_dir: Path) -> Path:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    proc = subprocess.run(
        [str(ROWGEN), "--raw-root", str(RAW_ROOT), "--out", str(out_dir), "--only", "teams-history"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"row generation failed: stdout={proc.stdout.decode()} stderr={proc.stderr.decode()}")
    path = out_dir / "teams-history.jsonl"
    if not path.exists():
        raise AssertionError("row generation did not write teams-history.jsonl")
    return path


def read_rows(path: Path) -> tuple[list[str], list[str], dict[str, int]]:
    texts = []
    labels = []
    anchor_counts: collections.Counter[str] = collections.Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        anchors = row.get("anchors", [])
        if len(anchors) != 1 or anchors[0].get("kind") != "label:team_match_result":
            raise AssertionError(f"bad anchor row: {row}")
        anchor = anchors[0]
        if anchor.get("source") == "" or float(anchor.get("confidence", 0.0)) <= 0.0:
            raise AssertionError(f"ungrounded anchor: {anchor}")
        texts.append(row["text"])
        label = anchor["value"]
        labels.append(label)
        anchor_counts[label] += 1
    if len(texts) < 50 or len(anchor_counts) < 2 or min(anchor_counts.values()) < 50:
        raise AssertionError(f"insufficient balanced anchors: {anchor_counts}")
    return texts, labels, dict(anchor_counts)


def discretized_mi(vectors: list[list[float]], labels: list[str], bins: int = MI_BINS) -> tuple[float, int]:
    columns = list(zip(*vectors))
    cuts = []
    for column in columns:
        sorted_col = sorted(column)
        cuts.append([sorted_col[int(len(sorted_col) * idx / bins)] for idx in range(1, bins)])
    feature_keys = []
    for vector in vectors:
        key = []
        for value, feature_cuts in zip(vector, cuts):
            bucket = 0
            while bucket < len(feature_cuts) and value > feature_cuts[bucket]:
                bucket += 1
            key.append(bucket)
        feature_keys.append(tuple(key))
    n = len(labels)
    cx = collections.Counter(feature_keys)
    cy = collections.Counter(labels)
    cxy = collections.Counter(zip(feature_keys, labels))
    bits = 0.0
    for (x, y), count in cxy.items():
        pxy = count / n
        bits += pxy * math.log2(pxy / ((cx[x] / n) * (cy[y] / n)))
    return bits, len(cx)


def signature_indices(labels: list[str], per_class: int = SIGNATURE_PER_CLASS) -> list[int]:
    counts: collections.Counter[str] = collections.Counter()
    indices = []
    for idx, label in enumerate(labels):
        if counts[label] < per_class:
            indices.append(idx)
            counts[label] += 1
    if len(set(labels[idx] for idx in indices)) < len(set(labels)):
        raise AssertionError("signature sample lost an anchor class")
    return indices


def distance_signature(vectors: list[list[float]], indices: list[int]) -> list[float]:
    sampled = [vectors[idx] for idx in indices]
    signature = []
    for left in range(len(sampled)):
        for right in range(left + 1, len(sampled)):
            signature.append(math.sqrt(sum((a - b) ** 2 for a, b in zip(sampled[left], sampled[right]))))
    return signature


def pearson_abs(left: list[float], right: list[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_ss = sum((value - left_mean) ** 2 for value in left)
    right_ss = sum((value - right_mean) ** 2 for value in right)
    if left_ss <= 1e-12 or right_ss <= 1e-12:
        return 0.0
    cov = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    return min(1.0, abs(cov / math.sqrt(left_ss * right_ss)))


def audit_vectors(texts: list[str], labels: list[str]) -> dict[str, object]:
    vectors = {}
    lens_reports = {}
    for name, (path, dim) in FACETS.items():
        facet_vectors = run_projector(path, texts)
        if any(len(vector) != dim or any(not math.isfinite(value) for value in vector) for vector in facet_vectors):
            raise AssertionError(f"{name}: bad vector shape or non-finite output")
        bits, occupied_bins = discretized_mi(facet_vectors, labels)
        if bits < MIN_SIGNAL_BITS:
            raise AssertionError(f"{name}: bits {bits:.6f} below {MIN_SIGNAL_BITS:.2f}")
        lens_reports[name] = {
            "bits_about": bits,
            "occupied_bins": occupied_bins,
            "dim": dim,
            "path": str(path.relative_to(ROOT)),
            "path_sha256": sha256_file(path),
        }
        vectors[name] = facet_vectors
    indices = signature_indices(labels)
    signatures = {name: distance_signature(vector, indices) for name, vector in vectors.items()}
    pair_reports = {}
    max_pair = {"pair": None, "corr": 0.0}
    names = list(FACETS)
    for left_idx, left in enumerate(names):
        for right in names[left_idx + 1 :]:
            corr = pearson_abs(signatures[left], signatures[right])
            if corr > MAX_PAIRWISE_CORR:
                raise AssertionError(f"{left}/{right}: corr {corr:.6f} above {MAX_PAIRWISE_CORR:.2f}")
            pair_reports[f"{left}__{right}"] = corr
            if corr > max_pair["corr"]:
                max_pair = {"pair": [left, right], "corr": corr}
    return {
        "lenses": lens_reports,
        "pairwise_correlation": pair_reports,
        "max_pairwise_correlation": max_pair,
        "signature_sample_size": len(indices),
        "signature_per_class": SIGNATURE_PER_CLASS,
        "mi_bins": MI_BINS,
    }


def run_calyx(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([str(CALYX), *args], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)


def physical_readback(work_dir: Path, rows_path: Path) -> dict[str, object]:
    home = work_dir / "calyx_home"
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    batch = work_dir / "physical_sample.jsonl"
    batch.write_text("\n".join(rows_path.read_text(encoding="utf-8").splitlines()[:12]) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["CALYX_HOME"] = str(home)
    create = run_calyx(["create-vault", "soccer-a7-audit", "--panel-template", "text-default"], env)
    if create.returncode != 0:
        raise AssertionError(f"create-vault failed: {create.stderr.decode('utf-8')}")
    created = json.loads(create.stdout)
    slot_map = {}
    for name, (path, dim) in FACETS.items():
        add = run_calyx(
            [
                "add-lens",
                "soccer-a7-audit",
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
        )
        if add.returncode != 0:
            raise AssertionError(f"add-lens {name} failed: {add.stderr.decode('utf-8')}")
        added = json.loads(add.stdout)
        slot_map[name] = added["slot_id"]
    ingest = run_calyx(["ingest", "soccer-a7-audit", "--batch", str(batch), "--output", "rows"], env)
    if ingest.returncode != 0:
        raise AssertionError(f"ingest failed: {ingest.stderr.decode('utf-8')}")
    vault_path = home / "vaults" / created["vault_id"]
    readback = run_calyx(
        [
            "readback",
            "cx-list",
            "--vault",
            str(vault_path),
            "--include-slots",
            "--limit",
            "12",
            "--rebuild-base-page-index",
        ],
        env,
    )
    if readback.returncode != 0:
        raise AssertionError(f"cx-list failed: {readback.stderr.decode('utf-8')}")
    rows = json.loads(readback.stdout)
    if len(rows) != 12:
        raise AssertionError(f"expected 12 physical rows, observed {len(rows)}")
    dense_dims = collections.Counter()
    for row in rows:
        for slot in row.get("slots", []):
            if slot.get("kind") == "dense" and slot.get("slot") in slot_map.values():
                dense_dims[(slot["slot"], slot["dim"])] += 1
    for name, (_, dim) in FACETS.items():
        key = (slot_map[name], dim)
        if dense_dims[key] != 12:
            raise AssertionError(f"{name}: physical slot {key} observed {dense_dims[key]} rows")
    return {
        "vault_id": created["vault_id"],
        "sample_rows": 12,
        "slot_map": slot_map,
        "dense_slot_readback_counts": {f"slot_{slot:02d}_dim_{dim}": count for (slot, dim), count in sorted(dense_dims.items())},
        "ingest_stdout_sha256": hashlib.sha256(ingest.stdout).hexdigest(),
        "cx_list_sha256": hashlib.sha256(readback.stdout).hexdigest(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="", help="optional JSON report path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    work_dir = Path(args.out).resolve().parent if args.out else ROOT / "scratchpad" / "wc2026" / "fsv" / "a7_audit"
    rows_dir = work_dir / "rows"
    rows_path = generate_rows(rows_dir)
    texts, labels, anchor_counts = read_rows(rows_path)
    audit = audit_vectors(texts, labels)
    report = {
        "status": "ok",
        "thresholds": {"min_signal_bits": MIN_SIGNAL_BITS, "max_pairwise_corr": MAX_PAIRWISE_CORR},
        "rows": {"path": str(rows_path.relative_to(ROOT)), "count": len(texts), "sha256": sha256_file(rows_path), "anchors": anchor_counts},
        "audit": audit,
        "physical_readback": physical_readback(work_dir, rows_path),
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
                "row_count": len(texts),
                "max_pairwise_corr": audit["max_pairwise_correlation"]["corr"],
                "min_bits": min(item["bits_about"] for item in audit["lenses"].values()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
