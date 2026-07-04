import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const repoRoot = resolve(import.meta.dirname, "../../..");
const reversePath = resolve(repoRoot, "docs/data/soccer_lab_reverse_causal_signatures.json");
const sufficiencyPath = resolve(repoRoot, "docs/data/soccer_lab_oracle_sufficiency_verdicts.json");
const auditPath = resolve(repoRoot, "docs/data/soccer_lab_serving_route_audit.json");
const reverseRaw = readFileSync(reversePath);
const sufficiencyRaw = readFileSync(sufficiencyPath);
const auditRaw = readFileSync(auditPath);
const reverse = JSON.parse(reverseRaw);
const sufficiency = JSON.parse(sufficiencyRaw);
const audit = JSON.parse(auditRaw);

function percent(value) {
  if (typeof value !== "number" || value < 0 || value > 1) {
    throw new Error(`invalid confidence: ${value}`);
  }
  return `${Math.round(value * 100)}%`;
}

function bits(value) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  if (typeof value !== "number" || value < 0) {
    throw new Error(`invalid bits: ${value}`);
  }
  return value.toFixed(3);
}

assert.equal(reverse.schema_version, 1);
assert.equal(reverse.domain, "soccer_lab.team_match_result");
assert.equal(reverse.reverse_query.causes.length, 8);
assert.equal(reverse.selected_signatures.length, 6);
assert.ok(reverse.selected_signatures.every((signature) => signature.action.startsWith("facet:")));

assert.equal(sufficiency.schema_version, 1);
assert.equal(Object.keys(sufficiency.verdicts).length, 4);
assert.equal(sufficiency.verdicts["soccer_lab.match_result"].status, "insufficient");
assert.equal(sufficiency.verdicts["soccer_lab.player_impact"].status, "not_run_no_grounded_outcomes");

assert.equal(audit.artifact, "soccer_lab_serving_route_audit");
assert.equal(audit.route_count, 37);
assert.ok(
  audit.required_soccer_lab_endpoints.some(
    (route) => route.path === "/provenance/:id" && route.status === "missing",
  ),
);
assert.ok(audit.controls.controls.every((control) => control.status === "present"));

assert.equal(percent(reverse.reverse_query.causes[0].confidence), "100%");
assert.equal(bits(sufficiency.verdicts["soccer_lab.match_result"].deficit_bits), "1.373");
assert.equal(bits(sufficiency.verdicts["soccer_lab.player_impact"].deficit_bits), "n/a");
assert.throws(() => percent(1.2), /invalid confidence/);
assert.throws(() => bits(-0.1), /invalid bits/);

console.log(
  JSON.stringify({
    reverse_source: "docs/data/soccer_lab_reverse_causal_signatures.json",
    reverse_sha256: createHash("sha256").update(reverseRaw).digest("hex"),
    sufficiency_source: "docs/data/soccer_lab_oracle_sufficiency_verdicts.json",
    sufficiency_sha256: createHash("sha256").update(sufficiencyRaw).digest("hex"),
    audit_source: "docs/data/soccer_lab_serving_route_audit.json",
    audit_sha256: createHash("sha256").update(auditRaw).digest("hex"),
    causes: reverse.reverse_query.causes.length,
    signatures: reverse.selected_signatures.length,
    verdicts: Object.keys(sufficiency.verdicts).length,
    synthetic_cases: ["confidence", "bits", "null_bits", "bad_confidence", "bad_bits"],
  }),
);
