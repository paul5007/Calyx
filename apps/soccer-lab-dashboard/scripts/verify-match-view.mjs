import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const repoRoot = resolve(import.meta.dirname, "../../..");
const matchPath = resolve(repoRoot, "docs/data/soccer_lab_match_predictions.json");
const exportPath = resolve(repoRoot, "docs/data/soccer_lab_prediction_export.json");
const raw = readFileSync(matchPath);
const exportRaw = readFileSync(exportPath);
const parsed = JSON.parse(raw);
const exportParsed = JSON.parse(exportRaw);

function outcomeLanes(record) {
  const confidence = Number(record?.confidence ?? 0);
  const status = record?.prediction_status;
  const value = record?.prediction ?? null;

  if (status === "oracle_insufficient") {
    assert.equal(value, null, "insufficient records must not publish an outcome");
    return ["home_win", "draw", "away_win"].map((outcome) => ({
      outcome,
      confidence: 0,
      available: false,
    }));
  }

  if (status !== "oracle_predicted") {
    throw new Error(`unsupported prediction status: ${status}`);
  }

  if (!["home_win", "draw", "away_win"].includes(value)) {
    throw new Error(`unsupported predicted outcome: ${value}`);
  }

  return ["home_win", "draw", "away_win"].map((outcome) => ({
    outcome,
    confidence: outcome === value ? confidence : 0,
    available: outcome === value,
  }));
}

const matchRecords = parsed.records;
const matchHash = createHash("sha256").update(raw).digest("hex");
const exportHash = createHash("sha256").update(exportRaw).digest("hex");
const firstExportMatch = exportParsed.records.find(
  (record) => record.record_type === "match",
);

assert.equal(parsed.action_id, "predict_match_result");
assert.equal(parsed.domain, "soccer_lab.match_result");
assert.equal(matchRecords.length, 16);
assert.equal(exportParsed.record_counts.match, matchRecords.length);
assert.equal(firstExportMatch.provenance.source_prediction_file_sha256, matchHash);

for (const record of matchRecords) {
  assert.equal(record.domain, "soccer_lab.match_result");
  assert.equal(record.action_id, "predict_match_result");
  assert.match(record.match_id, /^WC-2026-M\d{3}$/);
  assert.equal(record.score_columns_ignored, true);
  assert.equal(record.unplayed_reason, "blank_score");
  assert.equal(record.prediction_status, "oracle_insufficient");
  assert.equal(record.prediction, null);
  assert.equal(record.confidence, 0);
  assert.equal(record.confidence_caps.sufficient, false);
  assert.equal(outcomeLanes(record).filter((lane) => lane.available).length, 0);
}

const base = matchRecords[0];
for (const value of ["home_win", "draw", "away_win"]) {
  const lanes = outcomeLanes({
    ...base,
    prediction_status: "oracle_predicted",
    prediction: value,
    confidence: 0.73,
  });
  assert.equal(lanes.find((lane) => lane.outcome === value).confidence, 0.73);
  assert.equal(lanes.filter((lane) => lane.available).length, 1);
}

assert.throws(
  () =>
    outcomeLanes({
      ...base,
      prediction_status: "oracle_predicted",
      prediction: "coin_flip",
    }),
  /unsupported predicted outcome/,
);

console.log(
  JSON.stringify({
    source: "docs/data/soccer_lab_match_predictions.json",
    match_prediction_sha256: matchHash,
    export_sha256: exportHash,
    match_records: matchRecords.length,
    synthetic_cases: ["home_win", "draw", "away_win", "bad_outcome"],
  }),
);
