import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const repoRoot = resolve(import.meta.dirname, "../../..");
const progressionPath = resolve(
  repoRoot,
  "docs/data/soccer_lab_tournament_progression_predictions.json",
);
const butterflyPath = resolve(repoRoot, "docs/data/soccer_lab_bracket_butterfly_tree.json");
const progressionRaw = readFileSync(progressionPath);
const butterflyRaw = readFileSync(butterflyPath);
const progression = JSON.parse(progressionRaw);
const butterfly = JSON.parse(butterflyRaw);

const axes = ["winner", "finalist", "semi_finalist"];

function axisConfidence(record, axis) {
  if (!axes.includes(axis)) {
    throw new Error(`unsupported progression axis: ${axis}`);
  }
  if (record.prediction_status === "oracle_insufficient") {
    assert.equal(record.prediction, null);
    return { available: false, confidence: 0 };
  }
  if (record.prediction_status !== "oracle_predicted") {
    throw new Error(`unsupported prediction status: ${record.prediction_status}`);
  }
  return { available: record.axis === axis, confidence: record.confidence };
}

assert.equal(progression.schema_version, 1);
assert.equal(progression.records.length, 144);
assert.deepEqual(Object.keys(progression.axes).sort(), axes.sort());
assert.equal(new Set(progression.records.map((record) => record.team)).size, 48);

for (const record of progression.records) {
  assert.equal(record.version, "2026");
  assert.equal(record.prediction_status, "oracle_insufficient");
  assert.equal(record.prediction, null);
  assert.equal(record.confidence, 0);
  assert.equal(record.confidence_caps.sufficient, false);
  assert.equal(axisConfidence(record, record.axis).available, false);
}

assert.equal(butterfly.schema_version, 1);
assert.equal(butterfly.domain, "soccer_lab.bracket_butterfly");
assert.equal(butterfly.root_action, "match_104");
assert.equal(butterfly.records.length, 17);
assert.equal(Object.values(butterfly.hop_counts).reduce((sum, count) => sum + count, 0), 17);
assert.ok(butterfly.records.some((record) => record.action_or_event === butterfly.selected.action_or_event));

for (const [hop, confidence] of Object.entries(butterfly.hop_confidences)) {
  const observed = butterfly.records.filter((record) => String(record.hop) === hop);
  assert.equal(observed.length, butterfly.hop_counts[hop]);
  assert.ok(observed.every((record) => Math.abs(record.confidence - confidence) < 0.000001));
}

const base = progression.records[0];
for (const axis of axes) {
  const result = axisConfidence(
    {
      ...base,
      axis,
      prediction_status: "oracle_predicted",
      prediction: true,
      confidence: 0.66,
    },
    axis,
  );
  assert.deepEqual(result, { available: true, confidence: 0.66 });
}

assert.throws(() => axisConfidence(base, "quarter_finalist"), /unsupported progression axis/);

console.log(
  JSON.stringify({
    progression_source: "docs/data/soccer_lab_tournament_progression_predictions.json",
    progression_sha256: createHash("sha256").update(progressionRaw).digest("hex"),
    butterfly_source: "docs/data/soccer_lab_bracket_butterfly_tree.json",
    butterfly_sha256: createHash("sha256").update(butterflyRaw).digest("hex"),
    progression_records: progression.records.length,
    teams: new Set(progression.records.map((record) => record.team)).size,
    butterfly_records: butterfly.records.length,
    synthetic_cases: ["winner", "finalist", "semi_finalist", "bad_axis"],
  }),
);
