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

type MatchCard = {
  label: string;
  path: string;
  state: string;
};

const matches: MatchCard[] = [
  {
    label: "Match prediction",
    path: "/v1/predictions/match",
    state: "pending",
  },
  {
    label: "Bracket progression",
    path: "/v1/predictions/bracket",
    state: "pending",
  },
  {
    label: "Player impact",
    path: "/v1/predictions/players",
    state: "pending",
  },
];

const progressions = [
  { label: "Build", value: "ready", detail: "vite production bundle" },
  { label: "Lint", value: "ready", detail: "eslint max warnings zero" },
  { label: "Dev", value: "ready", detail: "127.0.0.1 vite host" },
  { label: "API", value: "pending", detail: "live wiring in follow-up task" },
];

const ledger = [
  { label: "Vault", value: "not connected", tone: "warn" },
  { label: "Search", value: "not connected", tone: "warn" },
  { label: "Provenance", value: "not connected", tone: "warn" },
  { label: "HHEM", value: "not connected", tone: "warn" },
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
            <h1>Prediction Control</h1>
          </div>
          <div className="status-pill">
            <Activity size={16} />
            Live API pending
          </div>
        </header>

        <section className="hero-grid" aria-label="Prediction overview">
          <div className="signal-panel">
            <div className="panel-head">
              <div>
                <p className="eyebrow">Match pulse</p>
                <h2>Endpoint readiness</h2>
              </div>
              <Goal size={24} />
            </div>
            <div className="match-stack">
              {matches.map((item) => (
                <article className="match-row" key={item.path}>
                  <div>
                    <span className="match-id">{item.label}</span>
                    <strong>{item.path}</strong>
                  </div>
                  <p className="pending-copy">Awaiting live web-api wiring.</p>
                  <span className="tag pending">{item.state}</span>
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
              {progressions.map((row) => (
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
                  <b className={item.tone}>{item.value}</b>
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
              <li>
                <span>kernel</span>
                <b>not requested</b>
              </li>
              <li>
                <span>guard</span>
                <b>not requested</b>
              </li>
              <li>
                <span>fusion</span>
                <b>not requested</b>
              </li>
            </ol>
          </div>
        </section>
      </section>
    </main>
  );
}
