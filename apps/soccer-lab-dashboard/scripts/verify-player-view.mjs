import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const repoRoot = resolve(import.meta.dirname, "../../..");
const playerPath = resolve(repoRoot, "docs/data/soccer_lab_player_impact_predictions.json");
const exportPath = resolve(repoRoot, "docs/data/soccer_lab_prediction_export.json");
const raw = readFileSync(playerPath);
const exportRaw = readFileSync(exportPath);
const player = JSON.parse(raw);
const combined = JSON.parse(exportRaw);
const playerHash = createHash("sha256").update(raw).digest("hex");

function impactState(record) {
  if (record.prediction_status === "oracle_insufficient") {
    assert.equal(record.prediction, null);
    return { available: false, confidence: 0, impact: null };
  }
  if (record.prediction_status !== "oracle_predicted") {
    throw new Error(`unsupported prediction status: ${record.prediction_status}`);
  }
  if (typeof record.prediction !== "boolean") {
    throw new Error("player impact prediction must be boolean");
  }
  return {
    available: true,
    confidence: record.confidence,
    impact: record.prediction,
  };
}

assert.equal(player.schema_version, 1);
assert.equal(player.action_id, "predict_player_impact");
assert.equal(player.domain, "soccer_lab.player_impact");
assert.equal(player.records.length, 1248);
assert.equal(new Set(player.records.map((record) => record.team_id)).size, 48);
assert.equal(player.class_imbalance.support_counts.impact, 150);
assert.equal(player.class_imbalance.support_counts.no_impact, 150);
assert.equal(combined.record_counts.player_impact, player.records.length);
assert.equal(
  combined.records.find((record) => record.record_type === "player_impact").provenance
    .source_prediction_file_sha256,
  playerHash,
);

for (const record of player.records) {
  assert.equal(record.domain, "soccer_lab.player_impact");
  assert.equal(record.action_id, "predict_player_impact");
  assert.match(record.player_id, /^\d+$/);
  assert.ok(record.player_name.length > 0);
  assert.ok(record.team_name.length > 0);
  assert.equal(record.prediction_status, "oracle_insufficient");
  assert.equal(record.confidence, 0);
  assert.equal(record.confidence_caps.sufficient, false);
  assert.deepEqual(impactState(record), { available: false, confidence: 0, impact: null });
}

const leaderboard = [...player.records]
  .sort((left, right) => right.prior_goals - left.prior_goals || right.prior_caps - left.prior_caps)
  .slice(0, 10);
assert.equal(leaderboard.length, 10);
assert.ok(leaderboard.every((record, index, rows) => index === 0 || rows[index - 1].prior_goals >= record.prior_goals));

const base = player.records[0];
assert.deepEqual(
  impactState({ ...base, prediction_status: "oracle_predicted", prediction: true, confidence: 0.8 }),
  { available: true, confidence: 0.8, impact: true },
);
assert.deepEqual(
  impactState({ ...base, prediction_status: "oracle_predicted", prediction: false, confidence: 0.4 }),
  { available: true, confidence: 0.4, impact: false },
);
assert.throws(
  () => impactState({ ...base, prediction_status: "oracle_predicted", prediction: "yes" }),
  /must be boolean/,
);
assert.throws(
  () => impactState({ ...base, prediction_status: "offline", prediction: null }),
  /unsupported prediction status/,
);

console.log(
  JSON.stringify({
    source: "docs/data/soccer_lab_player_impact_predictions.json",
    player_sha256: playerHash,
    records: player.records.length,
    teams: new Set(player.records.map((record) => record.team_id)).size,
    synthetic_cases: ["impact_true", "impact_false", "bad_prediction", "bad_status"],
  }),
);
