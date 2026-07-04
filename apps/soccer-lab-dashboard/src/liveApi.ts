export type ApiPredictionRecord = {
  action_id: string;
  domain: string;
  generated_at: string;
  input: {
    attributes: Record<string, unknown>;
    display: string;
    entity_id: string;
    entity_type: string;
    source: Record<string, unknown>;
  };
  input_hash: string;
  prediction: {
    confidence: number;
    confidence_caps: {
      dpi_ceiling: number;
      sufficient: boolean;
    };
    status: "oracle_insufficient" | "oracle_predicted";
    value: unknown;
  };
  provenance: {
    oracle_error_code: string | null;
    oracle_fixture_sha256: string;
    oracle_ledger_key_hex: string | null;
    oracle_ledger_ref: unknown | null;
    oracle_stdout_sha256: string;
    source_prediction_file: string;
    source_prediction_file_sha256: string;
    source_record_kind: "match" | "tournament_progression" | "player_impact";
    source_report: string;
    source_report_sha256: string;
  };
  record_id: string;
  record_type: "match" | "tournament_progression" | "player_impact";
  schema_version: 1;
};

export type ApiErrorEnvelope = {
  code: string;
  message: string;
  remediation: string;
};

export type LivePredictionData = {
  matches: ApiPredictionRecord[];
  progressions: ApiPredictionRecord[];
  players: ApiPredictionRecord[];
};

export type ProgressionRequest = {
  version: string;
  team: string;
  axis: string;
};

export type LiveRequestIndex = {
  matchIds: string[];
  progressions: ProgressionRequest[];
  playerIds: string[];
};

type ApiConfig = {
  baseUrl: string;
  bearer: string | null;
};

function config(): ApiConfig {
  const baseUrl = import.meta.env.VITE_CALYX_WEB_API_BASE_URL?.trim();
  const bearer = import.meta.env.VITE_CALYX_WEB_API_BEARER_SECRET?.trim() || null;
  if (!baseUrl) {
    throw new Error("VITE_CALYX_WEB_API_BASE_URL is required");
  }
  return { baseUrl: baseUrl.replace(/\/$/, ""), bearer };
}

async function postPrediction(
  path: "/predict/match" | "/predict/progression" | "/predict/player",
  body: Record<string, string>,
): Promise<ApiPredictionRecord> {
  const api = config();
  const headers: Record<string, string> = {
    "content-type": "application/json",
  };
  if (api.bearer) {
    headers.authorization = `Bearer ${api.bearer}`;
  }
  const response = await fetch(`${api.baseUrl}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  const payload = (await response.json()) as ApiPredictionRecord | ApiErrorEnvelope;
  if (!response.ok) {
    const envelope = payload as ApiErrorEnvelope;
    throw new Error(`${envelope.code}: ${envelope.message}`);
  }
  return payload as ApiPredictionRecord;
}

export async function fetchLivePredictions(
  index: LiveRequestIndex,
): Promise<LivePredictionData> {
  const [matches, progressions, players] = await Promise.all([
    Promise.all(
      index.matchIds.map((matchId) =>
        postPrediction("/predict/match", { matchId }),
      ),
    ),
    Promise.all(
      index.progressions.map((request) =>
        postPrediction("/predict/progression", request),
      ),
    ),
    Promise.all(
      index.playerIds.map((playerId) =>
        postPrediction("/predict/player", { playerId }),
      ),
    ),
  ]);
  return { matches, progressions, players };
}
