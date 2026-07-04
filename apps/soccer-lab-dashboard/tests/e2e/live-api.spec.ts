import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

const repoRoot = resolve(import.meta.dirname, "../../../..");
const exportPath = resolve(repoRoot, "docs/data/soccer_lab_prediction_export.json");
const exportData = JSON.parse(readFileSync(exportPath, "utf8"));

type PredictionRecord = {
  input: { entity_id: string };
  record_id: string;
  record_type: string;
};

function expectedRecord(recordType: string, entityId: string): PredictionRecord {
  const record = exportData.records.find(
    (candidate: PredictionRecord) =>
      candidate.record_type === recordType && candidate.input.entity_id === entityId,
  );
  expect(record, `missing ${recordType} ${entityId} in prediction export`).toBeTruthy();
  return record;
}

test("renders live prediction records from the same-origin deploy proxy", async ({ page, request, baseURL }) => {
  expect(baseURL, "CALYX_DASHBOARD_PREVIEW_URL/baseURL is required").toBeTruthy();

  const expectedMatch = expectedRecord("match", "WC-2026-M089");
  const readback = await request.post("/api/predict/match", {
    data: { matchId: "WC-2026-M089" },
  });
  expect(readback.status()).toBe(200);
  expect(await readback.json()).toEqual(expectedMatch);

  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];
  const apiResponses: Array<{ status: number; url: string }> = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.method()} ${request.url()}`);
  });
  page.on("response", (response) => {
    if (response.url().includes("/api/predict/")) {
      apiResponses.push({ status: response.status(), url: response.url() });
    }
  });

  await page.goto("/");
  await expect(page).toHaveTitle("Soccer Lab Dashboard");
  await expect(page.getByRole("heading", { name: "Match Predictions" })).toBeVisible();
  await expect(page.getByText("16 Oracle refusals")).toBeVisible();

  const matchCards = page.locator("article").filter({ hasText: /WC-2026-M\d{3}/ });
  await expect(matchCards).toHaveCount(16);
  const firstMatch = matchCards.filter({ hasText: "WC-2026-M089" }).first();
  await expect(firstMatch).toContainText("Paraguay vs France");
  await expect(firstMatch).toContainText("CALYX_ORACLE_INSUFFICIENT");
  await expect(firstMatch).toContainText("Home0%");
  await expect(firstMatch).toContainText("Draw0%");
  await expect(firstMatch).toContainText("Away0%");

  const progressionTeams = ["France", "Spain", "Argentina", "England", "Portugal", "Brazil", "Netherlands", "Morocco"];
  for (const team of progressionTeams) {
    await expect(page.locator("article").filter({ hasText: team }).first()).toContainText(
      "CALYX_ORACLE_INSUFFICIENT",
    );
  }

  const playerCards = page.locator("article").filter({ hasText: "Prior goals" });
  await expect(playerCards).toHaveCount(10);
  const firstPlayer = playerCards.filter({ hasText: "Ronaldo Cristiano Ronaldo" }).first();
  await expect(firstPlayer).toContainText("Portugal / FWD");
  await expect(firstPlayer).toContainText("Impact0%");
  await expect(firstPlayer).toContainText("CALYX_ORACLE_INSUFFICIENT");

  await expect(page.locator(".api-state-panel")).toHaveCount(0);
  expect(consoleErrors).toEqual([]);
  expect(failedRequests).toEqual([]);
  expect(apiResponses.length).toBeGreaterThanOrEqual(34);
  expect(apiResponses.every((response) => response.status === 200)).toBe(true);
  expect(new Set(apiResponses.map((response) => new URL(response.url).pathname))).toEqual(
    new Set(["/api/predict/match", "/api/predict/progression", "/api/predict/player"]),
  );
});

test("same-origin proxy preserves closed API envelopes for invalid inputs", async ({ request }) => {
  const edgeCases = [
    {
      path: "/api/predict/match",
      body: { matchId: "" },
      status: 400,
      code: "CALYX_WEB_API_BAD_REQUEST",
    },
    {
      path: "/api/predict/progression",
      body: { version: "2026", team: "France", axis: "quarter_finalist" },
      status: 400,
      code: "CALYX_WEB_API_BAD_REQUEST",
    },
    {
      path: "/api/predict/player",
      body: { playerId: "not-a-player" },
      status: 404,
      code: "CALYX_WEB_API_NOT_FOUND",
    },
  ];

  for (const edgeCase of edgeCases) {
    const response = await request.post(edgeCase.path, { data: edgeCase.body });
    expect(response.status()).toBe(edgeCase.status);
    const envelope = await response.json();
    expect(envelope.code).toBe(edgeCase.code);
    expect(envelope.message).toBeTruthy();
    expect(envelope.remediation).toBeTruthy();
  }
});
