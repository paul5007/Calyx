import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const repoRoot = resolve(import.meta.dirname, "../../..");
const exportPath = resolve(repoRoot, "docs/data/soccer_lab_prediction_export.json");
const exportData = JSON.parse(readFileSync(exportPath, "utf8"));
const baseUrl = (
  process.env.CALYX_WEB_API_BASE_URL ??
  process.env.VITE_CALYX_WEB_API_BASE_URL ??
  ""
).replace(/\/$/, "");
const bearer =
  process.env.CALYX_WEB_API_BEARER_SECRET ??
  process.env.VITE_CALYX_WEB_API_BEARER_SECRET ??
  "";

if (!baseUrl) {
  throw new Error("CALYX_WEB_API_BASE_URL or VITE_CALYX_WEB_API_BASE_URL is required");
}
if (!bearer) {
  throw new Error("CALYX_WEB_API_BEARER_SECRET or VITE_CALYX_WEB_API_BEARER_SECRET is required");
}

function expectedRecord(recordType, predicate) {
  const record = exportData.records.find(
    (candidate) => candidate.record_type === recordType && predicate(candidate),
  );
  assert.ok(record, `missing expected ${recordType} record`);
  return record;
}

async function post(path, body, token = bearer) {
  const headers = { "content-type": "application/json" };
  if (token) {
    headers.authorization = `Bearer ${token}`;
  }
  const response = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  return { response, json: await response.json() };
}

const happyCases = [
  {
    path: "/predict/match",
    body: { matchId: "WC-2026-M089" },
    expected: expectedRecord(
      "match",
      (record) => record.input.entity_id === "WC-2026-M089",
    ),
  },
  {
    path: "/predict/progression",
    body: { version: "2026", team: "France", axis: "winner" },
    expected: expectedRecord(
      "tournament_progression",
      (record) => record.input.entity_id === "2026:France:winner",
    ),
  },
  {
    path: "/predict/player",
    body: { playerId: "1" },
    expected: expectedRecord(
      "player_impact",
      (record) => record.input.entity_id === "1",
    ),
  },
];

for (const testCase of happyCases) {
  const { response, json } = await post(testCase.path, testCase.body);
  assert.equal(response.status, 200, `${testCase.path} returned ${response.status}`);
  assert.deepEqual(json, testCase.expected);
}

const edges = [
  {
    path: "/predict/match",
    body: { matchId: "" },
    status: 400,
    code: "CALYX_WEB_API_BAD_REQUEST",
  },
  {
    path: "/predict/progression",
    body: { version: "2026", team: "France", axis: "quarter_finalist" },
    status: 400,
    code: "CALYX_WEB_API_BAD_REQUEST",
  },
  {
    path: "/predict/player",
    body: { playerId: "not-a-player" },
    status: 404,
    code: "CALYX_WEB_API_NOT_FOUND",
  },
  {
    path: "/predict/match",
    body: { matchId: "WC-2026-M089" },
    token: "",
    status: 401,
    code: "CALYX_WEB_API_UNAUTHORIZED",
  },
];

for (const edge of edges) {
  const { response, json } = await post(edge.path, edge.body, edge.token ?? bearer);
  assert.equal(response.status, edge.status, `${edge.path} edge returned ${response.status}`);
  assert.equal(json.code, edge.code);
  assert.ok(json.message);
  assert.ok(json.remediation);
}

console.log(
  JSON.stringify({
    base_url: baseUrl,
    happy_cases: happyCases.map((testCase) => testCase.path),
    edge_cases: edges.map((edge) => `${edge.path}:${edge.code}`),
    matched_export_records: happyCases.length,
  }),
);
