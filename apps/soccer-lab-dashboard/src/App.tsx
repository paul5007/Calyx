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
import matchPredictions from "../../../docs/data/soccer_lab_match_predictions.json";

type PredictionOutcome = "home_win" | "draw" | "away_win";

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

type OutcomeLane = {
  label: string;
  outcome: PredictionOutcome;
  confidence: number;
  available: boolean;
};

const soccerLabExport = matchPredictions as MatchPredictionExport;
const matchRecords = soccerLabExport.records;

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

const sourceHash = matchRecords[0]?.provenance.oracle_stdout_sha256 ?? "";

function percent(value: number) {
  return `${Math.round(value * 100)}%`;
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
    label: "Match records",
    value: `${matchSummary.total}`,
    detail: "source export rows",
  },
  {
    label: "Published",
    value: `${matchSummary.publishable}`,
    detail: "oracle_predicted",
  },
  {
    label: "Refused",
    value: `${matchSummary.blocked}`,
    detail: "oracle_insufficient",
  },
  {
    label: "Generated",
    value: soccerLabExport.run_date,
    detail: "export timestamp",
  },
];

const ledger = [
  { label: "Export", value: "loaded", tone: "good" },
  { label: "Matches", value: `${matchSummary.total}`, tone: "good" },
  { label: "Confidence", value: "fail-closed", tone: "warn" },
  { label: "Source hash", value: sourceHash.slice(0, 10), tone: "good" },
];

type LedgerTone = (typeof ledger)[number]["tone"];

function toneClass(tone: LedgerTone) {
  return tone === "warn" ? "warn" : "good";
}

type TraceRow = {
  label: string;
  state: string;
};

const traceRows: TraceRow[] = [
  {
    label: "source",
    state: "docs/data/soccer_lab_match_predictions.json",
  },
  {
    label: "domain",
    state: "soccer_lab.match_result",
  },
  {
    label: "contract",
    state: "win/draw/loss lanes",
  },
];

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
              <p className="eyebrow">Tooling</p>
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
              <p className="eyebrow">Origin health</p>
              <ShieldCheck size={20} />
            </div>
            <div className="ledger-grid">
              {ledger.map((item) => (
                <div className="ledger-cell" key={item.label}>
                  <span>{item.label}</span>
                  <b className={toneClass(item.tone)}>{item.value}</b>
                </div>
              ))}
            </div>
          </div>

          <div className="trace-panel">
            <div className="panel-head compact">
              <p className="eyebrow">Explainability trace</p>
              <Braces size={20} />
            </div>
            <ol className="trace-list">
              {traceRows.map((row) => (
                <li key={row.label}>
                  <span>{row.label}</span>
                  <b>{row.state}</b>
                </li>
              ))}
            </ol>
          </div>
        </section>
      </section>
    </main>
  );
}
