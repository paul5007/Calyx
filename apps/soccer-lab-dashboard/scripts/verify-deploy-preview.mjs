import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const repoRoot = resolve(import.meta.dirname, "../../..");
const exportPath = resolve(repoRoot, "docs/data/soccer_lab_prediction_export.json");
const exportData = JSON.parse(readFileSync(exportPath, "utf8"));
const previewUrl = (process.env.CALYX_DASHBOARD_PREVIEW_URL ?? "http://127.0.0.1:4173").replace(/\/$/, "");

function expectedRecord(recordType, predicate) {
  const record = exportData.records.find(
    (candidate) => candidate.record_type === recordType && predicate(candidate),
  );
  assert.ok(record, `missing expected ${recordType} record`);
  return record;
}

async function postApi(path, body) {
  const response = await fetch(`${previewUrl}/api${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return { response, json: await response.json() };
}

const home = await fetch(`${previewUrl}/`);
assert.equal(home.status, 200, `/ returned ${home.status}`);
assert.match(home.headers.get("content-type") ?? "", /text\/html/);
const html = await home.text();
const scriptMatch = html.match(/<script[^>]+src="([^"]+\.js)"/);
assert.ok(scriptMatch, "built HTML must reference a JavaScript asset");

const asset = await fetch(`${previewUrl}${scriptMatch[1]}`);
assert.equal(asset.status, 200, `${scriptMatch[1]} returned ${asset.status}`);
assert.match(asset.headers.get("content-type") ?? "", /javascript/);

const expected = expectedRecord(
  "match",
  (record) => record.input.entity_id === "WC-2026-M089",
);
const { response: matchResponse, json: matchJson } = await postApi("/predict/match", {
  matchId: "WC-2026-M089",
});
assert.equal(matchResponse.status, 200, `/api/predict/match returned ${matchResponse.status}`);
assert.deepEqual(matchJson, expected);

const { response: edgeResponse, json: edgeJson } = await postApi("/predict/match", {
  matchId: "",
});
assert.equal(edgeResponse.status, 400);
assert.equal(edgeJson.code, "CALYX_WEB_API_BAD_REQUEST");
assert.ok(edgeJson.message);
assert.ok(edgeJson.remediation);

console.log(
  JSON.stringify({
    preview_url: previewUrl,
    html_bytes: html.length,
    js_asset: scriptMatch[1],
    match_record_id: matchJson.record_id,
    edge_cases: ["/api/predict/match:CALYX_WEB_API_BAD_REQUEST"],
  }),
);
