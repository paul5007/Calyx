#!/usr/bin/env python3
"""Verify the Soccer Lab production runbook against physical repo files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INVALID_DOC = "CALYX_SOCCER_LAB_RUNBOOK_INVALID"
MISSING_REFERENCE = "CALYX_SOCCER_LAB_RUNBOOK_MISSING_REFERENCE"
READBACK_MISMATCH = "CALYX_SOCCER_LAB_RUNBOOK_READBACK_MISMATCH"

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DOC = REPO / "docs/SOCCER_LAB_RUNBOOK.md"

REQUIRED_SECTIONS = [
    "# Soccer Lab production runbook",
    "## 1. Refresh data",
    "## 2. Build pipeline",
    "## 3. Verify operating state",
    "## 4. Serve API",
    "## 5. Deploy UI",
    "## 6. Recovery",
    "## 7. Release checklist",
]

REQUIRED_COMMANDS = [
    "python3 tools/data/acquire_soccer_lab_sources.py",
    "python3 tools/data/provenance_manifest.py",
    "python3 tools/data/build_soccer_lab_pipeline.py",
    "python3 tools/data/run_soccer_lab_fsv_gate.py",
    "target/debug/calyx anneal enable-autotune --vault <vault>",
    "python3 tools/ops/run_ledger_verify_job.py",
    "cargo build --bin calyx-web-api",
    "target/debug/calyx-web-api",
    "VITE_CALYX_WEB_API_BASE_URL=/api npm run build",
    "npm run serve:deploy-preview",
    "npm run verify:deploy-preview",
]

REQUIRED_REFERENCES = [
    "docs/STRUCTURAL_DATA_DOCTRINE.md",
    "docs/SOCCER_LAB_PREMERGE_FSV_GATE.md",
    "docs/SOCCER_LAB_LEDGER_VERIFY_JOB.md",
    "docs/SOCCER_LAB_WEB_API_ENV.md",
    "docs/SOCCER_LAB_MONITORING.md",
    "docs/SOCCER_LAB_DASHBOARD_DEPLOY.md",
    "docs/SOCCER_LAB_ANNEAL_AUTOTUNE.md",
    "docs/data/soccer_lab_prediction_export.json",
    "tools/data/acquire_soccer_lab_sources.py",
    "tools/data/provenance_manifest.py",
    "tools/data/build_soccer_lab_pipeline.py",
    "tools/data/run_soccer_lab_fsv_gate.py",
    "tools/ops/run_ledger_verify_job.py",
    "apps/soccer-lab-dashboard",
    ".github/workflows/soccer-lab-fsv-gate.yml",
    ".github/workflows/soccer-lab-ledger-verify.yml",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc", default=str(DEFAULT_DOC))
    parser.add_argument("--out", default="scratchpad/wc2026/fsv/issue71_runbook/report.json")
    parser.add_argument("--fsv-root", default=None)
    args = parser.parse_args()

    doc = Path(args.doc)
    out = Path(args.out)
    try:
        report = validate_document(doc)
        if args.fsv_root:
            root = Path(args.fsv_root)
            root.mkdir(parents=True, exist_ok=True)
            edges = run_edges(root)
            report["edges"] = edges
            out = root / "runbook-readback.json"
        write_json(out, report)
        readback = json.loads(out.read_text(encoding="utf-8"))
        if readback != report:
            raise RunbookError(
                READBACK_MISMATCH,
                "report readback did not match written report",
                "write the report to a stable JSON file and rerun",
            )
        if args.fsv_root:
            write_manifest(Path(args.fsv_root), [out])
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except RunbookError as error:
        failure = error.to_json()
        write_json(out, failure)
        print(json.dumps(failure, indent=2, sort_keys=True), file=sys.stderr)
        return 2


@dataclass
class RunbookError(Exception):
    code: str
    message: str
    remediation: str

    def to_json(self) -> dict[str, Any]:
        return {
            "status": "error",
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }


def validate_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RunbookError(
            MISSING_REFERENCE,
            f"runbook {path} does not exist",
            "create docs/SOCCER_LAB_RUNBOOK.md",
        )
    text = path.read_text(encoding="utf-8")
    missing_sections = [section for section in REQUIRED_SECTIONS if section not in text]
    missing_commands = [command for command in REQUIRED_COMMANDS if command not in text]
    missing_refs = [ref for ref in REQUIRED_REFERENCES if not (REPO / ref).exists()]
    absent_refs = [ref for ref in REQUIRED_REFERENCES if ref not in text]
    if missing_sections or missing_commands or missing_refs or absent_refs:
        raise RunbookError(
            INVALID_DOC if not missing_refs else MISSING_REFERENCE,
            json.dumps(
                {
                    "missing_sections": missing_sections,
                    "missing_commands": missing_commands,
                    "missing_references_on_disk": missing_refs,
                    "references_not_named_in_doc": absent_refs,
                },
                sort_keys=True,
            ),
            "update the runbook so every lifecycle section, command, and referenced repo file is present",
        )
    return {
        "status": "ok",
        "surface": "soccer_lab.runbook",
        "source_of_truth": "docs/SOCCER_LAB_RUNBOOK.md bytes plus referenced repo paths",
        "doc": str(path),
        "doc_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "sections": REQUIRED_SECTIONS,
        "commands": REQUIRED_COMMANDS,
        "references": REQUIRED_REFERENCES,
        "links_in_doc": sorted(extract_repo_paths(text)),
    }


def extract_repo_paths(text: str) -> set[str]:
    matches = re.findall(
        r"(?<![A-Za-z0-9_./-])((?:docs|tools|apps|\.github)/[A-Za-z0-9_./-]+)",
        text,
    )
    return {match.rstrip(".,);:") for match in matches}


def run_edges(root: Path) -> list[dict[str, Any]]:
    good = validate_document(DEFAULT_DOC)
    missing_doc = root / "missing.md"
    missing_section = root / "missing-section.md"
    missing_section.write_text(
        DEFAULT_DOC.read_text(encoding="utf-8").replace("## 6. Recovery", "## Recovery"),
        encoding="utf-8",
    )
    missing_command = root / "missing-command.md"
    missing_command.write_text(
        DEFAULT_DOC.read_text(encoding="utf-8").replace(
            "python3 tools/data/run_soccer_lab_fsv_gate.py", ""
        ),
        encoding="utf-8",
    )
    missing_reference = root / "missing-reference.md"
    missing_reference.write_text(
        DEFAULT_DOC.read_text(encoding="utf-8").replace(
            "docs/SOCCER_LAB_MONITORING.md",
            "docs/NO_SUCH_SOCCER_LAB_MONITORING.md",
        ),
        encoding="utf-8",
    )
    return [
        {
            "case": "happy_path",
            "expected": "ok",
            "observed": good["status"],
        },
        edge_case("missing_doc", missing_doc, MISSING_REFERENCE),
        edge_case("missing_required_section", missing_section, INVALID_DOC),
        edge_case("missing_required_command", missing_command, INVALID_DOC),
        edge_case("required_reference_not_named", missing_reference, INVALID_DOC),
    ]


def edge_case(name: str, path: Path, expected: str) -> dict[str, Any]:
    try:
        validate_document(path)
    except RunbookError as error:
        return {
            "case": name,
            "expected": expected,
            "observed": error.code,
        }
    return {
        "case": name,
        "expected": expected,
        "observed": "ok",
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_manifest(root: Path, files: list[Path]) -> None:
    lines = []
    for path in files:
        data = path.read_bytes()
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {path.relative_to(root)}")
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
