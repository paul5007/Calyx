import {
  Activity,
  ArrowUpRight,
  Braces,
  Gauge,
  Goal,
  Network,
  ShieldCheck,
  Trophy,
} from "lucide-react";
import butterflyTree from "../../../docs/data/soccer_lab_bracket_butterfly_tree.json";
import matchPredictions from "../../../docs/data/soccer_lab_match_predictions.json";
import sufficiencyVerdicts from "../../../docs/data/soccer_lab_oracle_sufficiency_verdicts.json";
import playerPredictions from "../../../docs/data/soccer_lab_player_impact_predictions.json";
import progressionPredictions from "../../../docs/data/soccer_lab_tournament_progression_predictions.json";
import reverseSignatures from "../../../docs/data/soccer_lab_reverse_causal_signatures.json";
import routeAudit from "../../../docs/data/soccer_lab_serving_route_audit.json";

type PredictionOutcome = "home_win" | "draw" | "away_win";
type ProgressionAxis = "winner" | "finalist" | "semi_finalist";

type MatchPredictionRecord = {
  domain: string;
  action_id: string;
  match_id: string;
  home_team: string;
  away_team: string;
  confidence: number;
  confidence_caps: {
    dpi_ceiling: number;
    sufficient: boolean;
  };
  date: string;
  prediction: PredictionOutcome | null;
  prediction_status: "oracle_insufficient" | "oracle_predicted";
  round: string;
  score_columns_ignored: boolean;
  source: string;
  source_row_index: number;
  start_time: string;
  unplayed_reason: string;
  venue: string;
  provenance: {
    oracle_error_code: string | null;
    oracle_fixture_sha256: string;
    oracle_stdout_sha256: string;
    source_report: string;
  };
};

type MatchPredictionExport = {
  action_id: string;
  domain: string;
  generated_at: string;
  records: MatchPredictionRecord[];
  run_date: string;
  schema_version: number;
};

type ProgressionRecord = {
  action_id: string;
  axis: ProgressionAxis;
  confidence: number;
  confidence_caps: {
    dpi_ceiling: number;
    sufficient: boolean;
  };
  continent: string;
  domain: string;
  prediction: boolean | null;
  prediction_status: "oracle_insufficient" | "oracle_predicted";
  source_row_index: number;
  team: string;
  version: string;
  provenance: {
    oracle_error_code: string | null;
    oracle_stdout_sha256: string;
  };
};

type ProgressionExport = {
  generated_at: string;
  records: ProgressionRecord[];
  schema_version: number;
};

type ButterflyRecord = {
  action_or_event: string;
  confidence: number;
  domain: "soccer_lab.bracket_butterfly";
  hop: number;
  outcome: {
    enum: string;
  };
};

type ButterflyTree = {
  domain: "soccer_lab.bracket_butterfly";
  generated_at: string;
  hop_counts: Record<string, number>;
  max_observed_hop: number;
  records: ButterflyRecord[];
  root_action: string;
  root_outcome: {
    enum: string;
  };
  selected: ButterflyRecord;
};

type PlayerImpactRecord = {
  action_id: string;
  confidence: number;
  confidence_caps: {
    dpi_ceiling: number;
    sufficient: boolean;
  };
  domain: "soccer_lab.player_impact";
  player_id: string;
  player_name: string;
  position: string;
  prediction: boolean | null;
  prediction_status: "oracle_insufficient" | "oracle_predicted";
  prior_caps: number;
  prior_goals: number;
  source_row_index: number;
  team_id: string;
  team_name: string;
  provenance: {
    oracle_error_code: string | null;
    oracle_stdout_sha256: string;
  };
};

type PlayerImpactExport = {
  action_id: string;
  class_imbalance: {
    support_counts: {
      impact: number;
      no_impact: number;
    };
  };
  domain: "soccer_lab.player_impact";
  generated_at: string;
  records: PlayerImpactRecord[];
  schema_version: number;
};

type ReverseCause = {
  action_or_event: string;
  confidence: number;
  provisional: boolean;
};

type FacetSignature = {
  action: string;
  answer_hits: number;
  facet: string;
  feature: string;
  lift: number;
  precision: number;
  signature_id: string;
  structural_confidence: number;
  total_hits: number;
};

type ReverseSignatureExport = {
  answer: string;
  domain: string;
  generated_at: string;
  prior_answer_rate: number;
  provenance: {
    oracle_stdout_sha256: string;
    source_report: string;
  };
  reverse_query: {
    causes: ReverseCause[];
  };
  selected_signatures: FacetSignature[];
};

type SufficiencyVerdict = {
  I_panel_oracle: number | null;
  deficit_bits: number | null;
  outcome_entropy_bits: number | null;
  panel_bits_gte_outcome_entropy: boolean;
  status: string;
};

type SufficiencyExport = {
  source_report: {
    path: string;
    sha256: string;
  };
  verdicts: Record<string, SufficiencyVerdict>;
};

type RouteAudit = {
  required_soccer_lab_endpoints: Array<{
    method: string;
    path: string;
    status: string;
  }>;
  route_count: number;
};

type OutcomeLane = {
  label: string;
  outcome: PredictionOutcome;
  confidence: number;
  available: boolean;
};

const soccerLabExport = matchPredictions as MatchPredictionExport;
const matchRecords = soccerLabExport.records;
const progressionExport = progressionPredictions as ProgressionExport;
const progressionRecords = progressionExport.records;
const bracketTree = butterflyTree as ButterflyTree;
const playerExport = playerPredictions as PlayerImpactExport;
const playerRecords = playerExport.records;
const reverseExport = reverseSignatures as ReverseSignatureExport;
const sufficiencyExport = sufficiencyVerdicts as SufficiencyExport;
const servingAudit = routeAudit as RouteAudit;

const outcomes: Array<{ label: string; outcome: PredictionOutcome }> = [
  { label: "Home", outcome: "home_win" },
  { label: "Draw", outcome: "draw" },
  { label: "Away", outcome: "away_win" },
];

const matchSummary = {
  total: matchRecords.length,
  publishable: matchRecords.filter(
    (record) => record.prediction_status === "oracle_predicted",
  ).length,
  blocked: matchRecords.filter(
    (record) => record.prediction_status === "oracle_insufficient",
  ).length,
};

const progressionSummary = {
  total: progressionRecords.length,
  teams: new Set(progressionRecords.map((record) => record.team)).size,
  blocked: progressionRecords.filter(
    (record) => record.prediction_status === "oracle_insufficient",
  ).length,
  butterfly: bracketTree.records.length,
};

const playerSummary = {
  total: playerRecords.length,
  teams: new Set(playerRecords.map((record) => record.team_id)).size,
  blocked: playerRecords.filter(
    (record) => record.prediction_status === "oracle_insufficient",
  ).length,
};

function percent(value: number) {
  return `${Math.round(value * 100)}%`;
}

function bits(value: number | null) {
  return value == null ? "n/a" : value.toFixed(3);
}

function outcomeLanes(record: MatchPredictionRecord): OutcomeLane[] {
  return outcomes.map(({ label, outcome }) => ({
    label,
    outcome,
    confidence:
      record.prediction_status === "oracle_predicted" &&
      record.prediction === outcome
        ? record.confidence
        : 0,
    available:
      record.prediction_status === "oracle_predicted" &&
      record.prediction === outcome,
  }));
}

function statusLabel(record: MatchPredictionRecord) {
  if (record.prediction_status === "oracle_predicted") {
    return "predicted";
  }
  return record.provenance.oracle_error_code ?? "oracle_insufficient";
}

const readiness = [
  {
    label: "Teams",
    value: `${progressionSummary.teams}`,
    detail: "progression candidates",
  },
  {
    label: "Records",
    value: `${progressionSummary.total}`,
    detail: "3 axes per team",
  },
  {
    label: "Butterfly",
    value: `${progressionSummary.butterfly}`,
    detail: "reachable expansion nodes",
  },
  {
    label: "Root",
    value: bracketTree.root_outcome.enum,
    detail: bracketTree.root_action,
  },
];

const progressionAxes: Array<{ axis: ProgressionAxis; label: string }> = [
  { axis: "winner", label: "Win" },
  { axis: "finalist", label: "Final" },
  { axis: "semi_finalist", label: "Semi" },
];

const progressionTeams = Array.from(
  progressionRecords
    .reduce((teams, record) => {
      if (!teams.has(record.team)) {
        teams.set(record.team, {
          team: record.team,
          continent: record.continent,
          records: new Map<ProgressionAxis, ProgressionRecord>(),
        });
      }
      teams.get(record.team)?.records.set(record.axis, record);
      return teams;
    }, new Map<string, { team: string; continent: string; records: Map<ProgressionAxis, ProgressionRecord> }>())
    .values(),
).slice(0, 8);

const playerLeaderboard = [...playerRecords]
  .sort(
    (left, right) =>
      right.prior_goals - left.prior_goals || right.prior_caps - left.prior_caps,
  )
  .slice(0, 10);

const topFacetSignatures = reverseExport.selected_signatures.slice(0, 4);
const kernelPath = reverseExport.reverse_query.causes.slice(0, 5);
const sufficiencyRows = Object.entries(sufficiencyExport.verdicts);
const provenanceRoute = servingAudit.required_soccer_lab_endpoints.find(
  (route) => route.path === "/provenance/:id",
);

const butterflyHops = Array.from(
  bracketTree.records
    .reduce((hops, record) => {
      const key = String(record.hop);
      const records = hops.get(key) ?? [];
      records.push(record);
      hops.set(key, records);
      return hops;
    }, new Map<string, ButterflyRecord[]>())
    .entries(),
).sort(([left], [right]) => Number(left) - Number(right));

export function App() {
  return (
    <main className="shell">
      <aside className="rail" aria-label="Dashboard sections">
        <div className="brand-mark">CL</div>
        <button className="rail-button is-active" aria-label="Overview">
          <Gauge size={20} />
        </button>
        <button className="rail-button" aria-label="Predictions">
          <Trophy size={20} />
        </button>
        <button className="rail-button" aria-label="Provenance">
          <Network size={20} />
        </button>
        <button className="rail-button" aria-label="Guardrails">
          <ShieldCheck size={20} />
        </button>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="kicker">Soccer Lab</p>
            <h1>Match Predictions</h1>
          </div>
          <div className="status-pill">
            <Activity size={16} />
            {matchSummary.blocked} Oracle refusals
          </div>
        </header>

        <section className="hero-grid" aria-label="Prediction overview">
          <div className="signal-panel">
            <div className="panel-head">
              <div>
                <p className="eyebrow">Match pulse</p>
                <h2>Win / draw / loss surface</h2>
              </div>
              <Goal size={24} />
            </div>
            <div className="match-stack">
              {matchRecords.map((record) => (
                <article className="match-row" key={record.match_id}>
                  <div className="match-fixture">
                    <span className="match-id">{record.match_id}</span>
                    <strong>
                      {record.home_team} vs {record.away_team}
                    </strong>
                    <small>{record.date} / {record.start_time}</small>
                  </div>
                  <div
                    className="outcome-grid"
                    aria-label={`${record.home_team} vs ${record.away_team} outcomes`}
                  >
                    {outcomeLanes(record).map((lane) => (
                      <div
                        className={`outcome-lane ${lane.available ? "is-live" : "is-blocked"}`}
                        key={lane.outcome}
                      >
                        <span>{lane.label}</span>
                        <b>{lane.available ? percent(lane.confidence) : "0%"}</b>
                      </div>
                    ))}
                  </div>
                  <span className="tag pending">{statusLabel(record)}</span>
                </article>
              ))}
            </div>
          </div>

          <div className="bracket-panel" aria-label="Tournament progression">
            <div className="panel-head compact">
              <p className="eyebrow">Progression</p>
              <ArrowUpRight size={20} />
            </div>
            <div className="radar">
              {readiness.map((row) => (
                <div className="radar-row" key={row.label}>
                  <span>{row.label}</span>
                  <b>{row.value}</b>
                  <small>{row.detail}</small>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="lower-grid">
          <div className="ledger-panel">
            <div className="panel-head compact">
              <p className="eyebrow">Tournament axes</p>
              <ShieldCheck size={20} />
            </div>
            <div className="progression-table">
              {progressionTeams.map((team) => (
                <article className="progression-row" key={team.team}>
                  <div>
                    <strong>{team.team}</strong>
                    <span>{team.continent}</span>
                  </div>
                  {progressionAxes.map(({ axis, label }) => {
                    const record = team.records.get(axis);
                    const available = record?.prediction_status === "oracle_predicted";
                    return (
                      <span className="axis-cell" key={axis}>
                        <small>{label}</small>
                        <b>{available ? percent(record.confidence) : "0%"}</b>
                      </span>
                    );
                  })}
                  <span className="tag pending">
                    {team.records.get("winner")?.provenance.oracle_error_code ??
                      "oracle_insufficient"}
                  </span>
                </article>
              ))}
            </div>
          </div>

          <div className="trace-panel">
            <div className="panel-head compact">
              <p className="eyebrow">Butterfly expansion</p>
              <Braces size={20} />
            </div>
            <div className="butterfly-grid">
              {butterflyHops.map(([hop, records]) => (
                <div className="hop-column" key={hop}>
                  <strong>Hop {hop}</strong>
                  {records.map((record) => (
                    <span
                      className={
                        record.action_or_event === bracketTree.selected.action_or_event
                          ? "is-selected"
                          : undefined
                      }
                      key={`${record.action_or_event}-${record.outcome.enum}`}
                    >
                      {record.action_or_event}
                      <b>{record.outcome.enum}</b>
                      <small>{percent(record.confidence)}</small>
                    </span>
                  ))}
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="player-panel" aria-label="Player impact leaderboard">
          <div className="panel-head compact">
            <div>
              <p className="eyebrow">Player impact</p>
              <h2>Source-backed scorer watchlist</h2>
            </div>
            <Trophy size={20} />
          </div>
          <div className="player-summary">
            <span>
              <b>{playerSummary.total}</b>
              players
            </span>
            <span>
              <b>{playerSummary.teams}</b>
              teams
            </span>
            <span>
              <b>{playerSummary.blocked}</b>
              oracle refusals
            </span>
            <span>
              <b>{playerExport.class_imbalance.support_counts.impact}</b>
              impact support
            </span>
          </div>
          <div className="player-table">
            {playerLeaderboard.map((player, index) => (
              <article className="player-row" key={player.player_id}>
                <span className="rank">{index + 1}</span>
                <div>
                  <strong>{player.player_name}</strong>
                  <small>
                    {player.team_name} / {player.position}
                  </small>
                </div>
                <span>
                  <small>Prior goals</small>
                  <b>{player.prior_goals}</b>
                </span>
                <span>
                  <small>Caps</small>
                  <b>{player.prior_caps}</b>
                </span>
                <span>
                  <small>Impact</small>
                  <b>{player.prediction_status === "oracle_predicted" ? percent(player.confidence) : "0%"}</b>
                </span>
                <span className="tag pending">
                  {player.provenance.oracle_error_code ?? "oracle_insufficient"}
                </span>
              </article>
            ))}
          </div>
        </section>

        <section className="explain-panel" aria-label="Prediction explainability">
          <div className="panel-head compact">
            <div>
              <p className="eyebrow">Explainability</p>
              <h2>Kernel, bits, provenance</h2>
            </div>
            <Braces size={20} />
          </div>
          <div className="explain-grid">
            <div className="facet-card">
              <p className="eyebrow">Contributing facets</p>
              {topFacetSignatures.map((signature) => (
                <article key={signature.signature_id}>
                  <strong>{signature.feature}</strong>
                  <span>{signature.facet}</span>
                  <b>{percent(signature.precision)}</b>
                  <small>{signature.answer_hits}/{signature.total_hits} answer hits</small>
                </article>
              ))}
            </div>

            <div className="kernel-card">
              <p className="eyebrow">Kernel path</p>
              {kernelPath.map((cause) => (
                <article key={cause.action_or_event}>
                  <span>{cause.action_or_event}</span>
                  <b>{percent(cause.confidence)}</b>
                  <small>{cause.provisional ? "provisional" : "anchored"}</small>
                </article>
              ))}
            </div>

            <div className="bits-card">
              <p className="eyebrow">Oracle bits</p>
              {sufficiencyRows.map(([domain, verdict]) => (
                <article key={domain}>
                  <strong>{domain.replace("soccer_lab.", "")}</strong>
                  <span>{verdict.status}</span>
                  <b>{bits(verdict.deficit_bits)}</b>
                </article>
              ))}
            </div>

            <div className="provenance-card">
              <p className="eyebrow">Provenance</p>
              <article>
                <span>reverse report</span>
                <b>{reverseExport.provenance.oracle_stdout_sha256.slice(0, 12)}</b>
              </article>
              <article>
                <span>sufficiency report</span>
                <b>{sufficiencyExport.source_report.sha256.slice(0, 12)}</b>
              </article>
              <article>
                <span>{provenanceRoute?.path ?? "/provenance/:id"}</span>
                <b>{provenanceRoute?.status ?? "missing"}</b>
              </article>
            </div>
          </div>
        </section>
      </section>
    </main>
  );
}
