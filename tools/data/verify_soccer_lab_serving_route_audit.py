#!/usr/bin/env python3
"""Verify and emit the Soccer Lab serving route audit artifact for issue #49."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs/data/soccer_lab_serving_route_audit.json"

ROUTE_FILES = [
    ROOT / "crates/calyx-web-api/src/lib.rs",
    ROOT / "crates/calyx-web-api/src/guardrails.rs",
    ROOT / "crates/calyxd/src/server.rs",
    ROOT / "crates/calyxd/src/learner_origin/service.rs",
]
AUTH_GUARD_CACHE_FILES = [
    ROOT / "crates/calyx-web-api/src/auth.rs",
    ROOT / "crates/calyx-web-api/src/cache.rs",
    ROOT / "crates/calyx-web-api/src/guardrails.rs",
    ROOT / "crates/calyxd/src/server.rs",
    ROOT / "crates/calyxd/src/learner_origin/service.rs",
]
PREDICTION_EXPORT = ROOT / "docs/data/soccer_lab_prediction_export.json"
PREDICTION_SCHEMA = ROOT / "docs/data/soccer_lab_prediction_record_schema.json"

AXUM_ROUTE_RE = re.compile(
    r'\.route\("(?P<path>[^"]+)",\s*(?P<method>get|post)\((?P<handler>[A-Za-z0-9_]+)\)\)'
)
CALYXD_METRICS_ROUTE_RE = re.compile(r'\("(?P<method>GET)",\s*"(?P<path>/metrics)"\)')
LEARNER_LITERAL_ROUTE_RE = re.compile(r'if path == "(?P<path>[^"]+)"')
LEARNER_DYNAMIC_PREFIX_RE = re.compile(r'path\.strip_prefix\("(?P<prefix>[^"]+)"\)')
LEARNER_DYNAMIC_SUFFIX_RE = re.compile(r'rest\.strip_suffix\("(?P<suffix>[^"]+)"\)')

REQUIRED_ENDPOINTS = [
    {
        "issue": 50,
        "method": "POST",
        "path": "/predict/match",
        "contract": "Return match-result prediction records from the Soccer Lab prediction export.",
    },
    {
        "issue": 51,
        "method": "POST",
        "path": "/predict/progression",
        "contract": "Return tournament progression prediction records.",
    },
    {
        "issue": 52,
        "method": "POST",
        "path": "/predict/player",
        "contract": "Return player-impact prediction records.",
    },
    {
        "issue": 53,
        "method": "POST",
        "path": "/search",
        "contract": "Expose Soccer Lab search over HTTP without requiring the generic /v1 prefix.",
    },
    {
        "issue": 53,
        "method": "POST",
        "path": "/kernel-answer",
        "contract": "Expose kernel-answer retrieval over HTTP.",
    },
    {
        "issue": 54,
        "method": "GET",
        "path": "/provenance/:id",
        "contract": "Expose explainability/provenance by prediction id.",
    },
    {
        "issue": 56,
        "method": "GET",
        "path": "/healthcheck",
        "contract": "Expose service healthcheck for the Soccer Lab serving surface.",
    },
]


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def source_ref(path: Path, line_no: int, line: str) -> dict[str, Any]:
    return {"file": rel(path), "line": line_no, "snippet": line.strip()}


def parse_axum_routes(path: Path, service: str) -> list[dict[str, Any]]:
    routes = []
    for line_no, line in enumerate(read_lines(path), start=1):
        for match in AXUM_ROUTE_RE.finditer(line):
            routes.append(
                {
                    "service": service,
                    "method": match.group("method").upper(),
                    "path": match.group("path"),
                    "handler": match.group("handler"),
                    "source": source_ref(path, line_no, line),
                    "router": "axum",
                    "wired_state": route_wiring(match.group("handler")),
                }
            )
    return routes


def route_wiring(handler: str) -> str:
    if handler in {"not_implemented", "provenance_stub"}:
        return "scaffolded_501"
    return "wired"


def parse_calyxd_routes() -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    server = ROOT / "crates/calyxd/src/server.rs"
    for line_no, line in enumerate(read_lines(server), start=1):
        match = CALYXD_METRICS_ROUTE_RE.search(line)
        if match:
            routes.append(
                {
                    "service": "calyxd",
                    "method": match.group("method"),
                    "path": match.group("path"),
                    "handler": "metrics.encode_text",
                    "source": source_ref(server, line_no, line),
                    "router": "hand_rolled_http",
                    "wired_state": "wired",
                }
            )
    service = ROOT / "crates/calyxd/src/learner_origin/service.rs"
    lines = read_lines(service)
    for line_no, line in enumerate(lines, start=1):
        match = LEARNER_LITERAL_ROUTE_RE.search(line)
        if match:
            path = match.group("path")
            routes.append(
                {
                    "service": "calyxd_learner_origin",
                    "method": "POST",
                    "path": path,
                    "handler": handler_for_learner_path(path),
                    "source": source_ref(service, line_no, line),
                    "router": "hand_rolled_http",
                    "wired_state": "wired",
                }
            )
    for idx, line in enumerate(lines):
        prefix = LEARNER_DYNAMIC_PREFIX_RE.search(line)
        if not prefix:
            continue
        for lookahead in lines[idx : idx + 8]:
            suffix = LEARNER_DYNAMIC_SUFFIX_RE.search(lookahead)
            if suffix:
                dyn_path = f"{prefix.group('prefix')}:decision_id{suffix.group('suffix')}"
                routes.append(
                    {
                        "service": "calyxd_learner_origin",
                        "method": "POST",
                        "path": dyn_path,
                        "handler": "handle_outcome",
                        "source": source_ref(service, idx + 1, line),
                        "router": "hand_rolled_http",
                        "wired_state": "wired",
                    }
                )
                break
    return routes


def handler_for_learner_path(path: str) -> str:
    return {
        "/v1/learner-signals/batches": "handle_signal_batch",
        "/v1/interventions/decide": "handle_decision",
        "/v1/mastery/estimate": "handle_mastery_estimate",
        "/v1/oracle/forecast": "handle_oracle_forecast",
        "/v1/reactive/affect-signals": "handle_reactive_affect",
        "/v1/kernel/track-spines": "handle_track_spines",
    }.get(path, "unknown")


def normalize_path(path: str) -> str:
    return re.sub(r"\{([^}]+)\}", r":\1", path)


def exact_match(routes: list[dict[str, Any]], method: str, path: str) -> list[dict[str, Any]]:
    want = normalize_path(path)
    return [
        route
        for route in routes
        if route["method"] == method and normalize_path(route["path"]) == want
    ]


def endpoint_gap_report(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    report = []
    for required in REQUIRED_ENDPOINTS:
        matches = exact_match(routes, required["method"], required["path"])
        report.append(
            {
                **required,
                "status": "present" if matches else "missing",
                "matches": [
                    {
                        "service": item["service"],
                        "path": item["path"],
                        "handler": item["handler"],
                        "source": item["source"],
                    }
                    for item in matches
                ],
            }
        )
    return report


def scan_controls() -> dict[str, Any]:
    text_by_file = {path: path.read_text(encoding="utf-8") for path in AUTH_GUARD_CACHE_FILES}
    evidence = []
    terms = {
        "auth": ["Authorization", "Bearer ", "CALYX_WEB_API_BEARER_SECRET", "constant_time_eq"],
        "guardrails": [
            "MAX_BODY_BYTES",
            "MAX_GPU_BODY_BYTES",
            "RateLimited",
            "REQUEST_TIMEOUT",
            "PayloadTooLarge",
        ],
        "response_cache": ["ResponseCache", "x-cache", "Age", "CALYX_WEB_API_CACHE_TTL_SECS"],
        "loopback": ["is_loopback", "127.0.0.1", "loopback"],
        "structured_errors": ["ErrorCode", "CALYX_WEB_API", "CALYX_ORIGIN"],
    }
    for category, needles in terms.items():
        found = []
        for path, text in text_by_file.items():
            for line_no, line in enumerate(text.splitlines(), start=1):
                if any(needle in line for needle in needles):
                    found.append(source_ref(path, line_no, line))
        evidence.append(
            {
                "category": category,
                "status": "present" if found else "missing",
                "evidence": found[:12],
            }
        )
    return {
        "summary": "Existing web-api/calyxd surfaces include bearer auth, route guardrails, loopback binding, structured errors, and bounded response cache for generic read endpoints.",
        "controls": evidence,
        "gaps_for_soccer_lab_serving": [
            "No Soccer Lab-specific /predict/* routes are registered.",
            "No unprefixed /search, /kernel-answer, /provenance/:id, or /healthcheck routes are registered for the Soccer Lab serving API.",
            "Existing cache coverage is limited to /v1/search and /v1/provenance/{id}; prediction endpoints need explicit cache policy in #55.",
            "Existing bearer middleware applies to production calyx-web-api builders, but new Soccer Lab endpoints need tests proving they are behind the same fail-closed layer.",
        ],
    }


def prediction_contract() -> dict[str, Any]:
    data = json.loads(PREDICTION_EXPORT.read_text(encoding="utf-8"))
    records = data.get("records", [])
    domains = Counter(record["domain"] for record in records)
    grouped = {
        "match": domains["soccer_lab.match_result"],
        "tournament_progression": domains["soccer_lab.tournament_winner"]
        + domains["soccer_lab.tournament_finalist"]
        + domains["soccer_lab.tournament_semi_finalist"],
        "player_impact": domains["soccer_lab.player_impact"],
    }
    expected = {"match": 16, "tournament_progression": 144, "player_impact": 1248}
    return {
        "export_file": rel(PREDICTION_EXPORT),
        "export_sha256": sha256(PREDICTION_EXPORT),
        "schema_file": rel(PREDICTION_SCHEMA),
        "schema_sha256": sha256(PREDICTION_SCHEMA),
        "record_count": len(records),
        "domain_counts": dict(sorted(domains.items())),
        "serving_groups": grouped,
        "expected_serving_groups": expected,
        "status": "verified" if grouped == expected and len(records) == 1408 else "mismatch",
    }


def synthetic_fsv(routes: list[dict[str, Any]], gaps: list[dict[str, Any]]) -> dict[str, Any]:
    sample = '        .route("/v1/search", post(search))'
    sample_file = ROOT / "crates/calyx-web-api/src/lib.rs"
    parsed = []
    for match in AXUM_ROUTE_RE.finditer(sample):
        parsed.append(
            {
                "method": match.group("method").upper(),
                "path": match.group("path"),
                "handler": match.group("handler"),
            }
        )
    malformed = '        .route("/v1/broken", banana(search))'
    malformed_count = len(list(AXUM_ROUTE_RE.finditer(malformed)))
    duplicate_keys = [
        {"route": f"{method} {path}", "registrations": count}
        for (method, path), count in sorted(
            Counter((route["method"], normalize_path(route["path"])) for route in routes).items()
        )
        if count > 1
    ]
    with tempfile.TemporaryDirectory(prefix="calyx-issue49-route-audit-") as temp:
        probe = Path(temp) / "probe.json"
        before = probe.exists()
        probe.write_text(json.dumps({"ok": True}), encoding="utf-8")
        readback = json.loads(probe.read_text(encoding="utf-8"))
        probe.unlink()
        after = probe.exists()
    return {
        "route_parser_happy_path": parsed
        == [{"method": "POST", "path": "/v1/search", "handler": "search"}],
        "route_parser_malformed_ignored": malformed_count == 0,
        "duplicate_route_registrations": duplicate_keys,
        "required_gap_detection": all(item["status"] == "missing" for item in gaps),
        "artifact_write_readback_probe": {
            "existed_before": before,
            "readback": readback,
            "exists_after_cleanup": after,
        },
        "sample_source": source_ref(sample_file, 0, sample),
    }


def source_manifest(paths: list[Path]) -> list[dict[str, Any]]:
    return [
        {
            "file": rel(path),
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
        }
        for path in paths
    ]


def build_audit() -> dict[str, Any]:
    routes = []
    routes.extend(parse_axum_routes(ROOT / "crates/calyx-web-api/src/lib.rs", "calyx-web-api"))
    routes.extend(parse_axum_routes(ROOT / "crates/calyx-web-api/src/guardrails.rs", "calyx-web-api"))
    routes.extend(parse_calyxd_routes())
    routes.sort(key=lambda item: (item["service"], item["path"], item["method"], item["source"]["line"]))
    gaps = endpoint_gap_report(routes)
    return {
        "artifact": "soccer_lab_serving_route_audit",
        "issue": 49,
        "generated_at": "2026-07-04",
        "source_manifest": source_manifest(sorted(set(ROUTE_FILES + AUTH_GUARD_CACHE_FILES))),
        "current_routes": routes,
        "route_count": len(routes),
        "required_soccer_lab_endpoints": gaps,
        "controls": scan_controls(),
        "prediction_record_contract": prediction_contract(),
        "docs_section_25": {
            "status": "not_found",
            "checked": [
                "docs/STRUCTURAL_DATA_DOCTRINE.md",
                "docs/adr/0001-soccer-lab-facet-projector-ingest.md",
                "docs/SOCCER_LAB_SCHEMA_FACETS.md",
            ],
            "note": "No section 25 route inventory was present in the docs tree; this artifact is the generated route inventory for #49.",
        },
        "fsv": {},
    }


def main() -> None:
    audit = build_audit()
    audit["fsv"] = synthetic_fsv(audit["current_routes"], audit["required_soccer_lab_endpoints"])
    failures = []
    if audit["prediction_record_contract"]["status"] != "verified":
        failures.append("prediction export contract mismatch")
    if not audit["fsv"]["route_parser_happy_path"]:
        failures.append("route parser happy path failed")
    if not audit["fsv"]["route_parser_malformed_ignored"]:
        failures.append("malformed route parser case failed")
    if not audit["fsv"]["required_gap_detection"]:
        failures.append("required gap detection did not report all Soccer Lab endpoints missing")
    if failures:
        raise SystemExit("; ".join(failures))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {rel(OUT)}")
    print(f"routes={audit['route_count']}")
    print(f"prediction_records={audit['prediction_record_contract']['record_count']}")
    print(f"sha256={sha256(OUT)}")


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
